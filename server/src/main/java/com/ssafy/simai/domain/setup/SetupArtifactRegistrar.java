package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.calibration.CalibrationService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.ObjectHead;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 현장 세팅 작업(I5 site_setup)이 하트비트·결과의 {@code artifacts}(site_setup 1.2.0)로 알린 새 보정 문서·미리보기/정식 GLB 를
 * EC2 가 등록한다(S15P31A101-467, docs/design/site-setup-pipeline.md 13절).
 * <ul>
 *   <li>객체 키는 이 작업·이 대여의 결과 경로({@code job-objects/site_setup/{job_id}/{lease_id}/{이름}}, JobObjectSignService 의 PUT 키)만
 *       받는다. 다른 작업·다른 대여·다른 버킷의 객체는 거부한다(다른 세션이 만든 산출물을 이 세션 이름으로 등록하지 못하게).
 *   <li>보정 문서는 EC2 가 내려받아 sha256 을 대조하고, 문서의 버전·카메라 집합이 스냅샷·세션과 같아야 하며, 관리자 업로드와 같은
 *       {@link CalibrationService} 검사(계약·현장·content_hash·같은 버전 잠금)를 거쳐 새 calibration_version 으로 추가한다(덮어쓰지 않음).
 *   <li>GLB 는 크기가 커서 내려받지 않는다. HEAD 로 있는지·크기·저장소가 기록한 SHA-256(올릴 때 서명에 묶은 값)을 대조한 뒤
 *       scene_version 후보(CANDIDATE)로 추가한다. 활성화는 관리자가 한다(자동 활성화 없음).
 *   <li>등록한 버전은 {@code setup_session_artifact} 에 이 세션 귀속으로 남긴다(추가만). 버전 계보 검사가 이 귀속을 본다:
 *       다른 세팅 세션에 귀속된 버전은 그 세션 것이다.
 * </ul>
 * 같은 세션의 같은 버전을 같은 해시로 다시 알리면(이어지는 하트비트) 다시 읽지 않는다. 같은 버전에 다른 해시는 거부한다.
 * 어긋나면 모두 INVALID_SETUP_PROGRESS(400)로 거부하고 하트비트·제출 트랜잭션 전체가 되돌아간다. 저장소에 닿지 못하면 그 오류(503)를 그대로 낸다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupArtifactRegistrar {

    private static final Logger log = LoggerFactory.getLogger(SetupArtifactRegistrar.class);

    /** JobObjectSignService 가 site_setup 작업 결과 객체에 붙이는 키 앞부분(PUT_PREFIX 에 없는 유형의 기본값) */
    static final String KEY_PREFIX = "job-objects/site_setup/";
    /** 보정 문서 상한. 카메라 수십 대 문서도 수십 KB 다 */
    static final int MAX_CALIBRATION_BYTES = 4 << 20;
    private static final Pattern NAME = Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$");
    private static final Pattern SHA256_HEX = Pattern.compile("^[0-9a-f]{64}$");

    static final String CALIBRATION = "CALIBRATION";
    static final String PREVIEW_SCENE = "PREVIEW_SCENE";
    static final String SCENE = "SCENE";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final CalibrationService calibrations;
    private final ObjectMapper mapper;
    private final Clock clock;

    SetupArtifactRegistrar(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, CalibrationService calibrations,
            ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.calibrations = calibrations;
        this.mapper = mapper;
        this.clock = clock;
    }

    /** 등록에 쓰는 세션 정보 */
    record Target(UUID setupSessionId, String siteId, String jobId, UUID leaseId, Set<String> cameraIds, Long requestedBy) {
    }

    /**
     * 스냅샷의 artifacts 를 등록한다. 없으면 아무것도 하지 않는다. 보정 → 미리보기 → 정식 순서다(Scene 은 보정을 참조).
     * 참조한 버전 필드(calibration_version 등)가 같은 스냅샷에 있어야 한다.
     */
    void register(Target target, JsonNode snapshot, String calibrationVersion, String previewSceneVersion, String sceneVersion) {
        JsonNode artifacts = snapshot.get("artifacts");
        if (artifacts == null || artifacts.isNull()) {
            return;
        }
        if (!artifacts.isObject()) {
            throw invalid();
        }
        JsonNode cal = artifacts.get("calibration_document");
        JsonNode preview = artifacts.get("preview_scene");
        JsonNode scene = artifacts.get("scene");
        if (cal != null && !cal.isNull()) {
            registerCalibration(target, ObjectRef.of(cal), required(calibrationVersion));
        }
        if (preview != null && !preview.isNull()) {
            registerScene(target, ObjectRef.of(preview), PREVIEW_SCENE, required(previewSceneVersion), required(calibrationVersion),
                    "scene_tier=PREVIEW; preview_scene=true; safety_use=false; source=site_setup");
        }
        if (scene != null && !scene.isNull()) {
            String notes = "scene_tier=FULL; safety_use=false; source=site_setup"
                    + (previewSceneVersion == null ? "" : "; replaces_preview_scene=" + previewSceneVersion);
            registerScene(target, ObjectRef.of(scene), SCENE, required(sceneVersion), required(calibrationVersion), notes);
        }
    }

    /** 이 세션이 artifacts 로 등록한 보정인지 */
    boolean registeredBySession(UUID setupSessionId, String kind, String version) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_session_artifact
                               WHERE setup_session_id = :id AND kind = :kind AND version = :v)
                """, new MapSqlParameterSource("id", setupSessionId).addValue("kind", kind).addValue("v", version),
                Boolean.class));
    }

    private void registerCalibration(Target target, ObjectRef ref, String version) {
        if (alreadyLinked(target, CALIBRATION, version, ref)) {
            return;
        }
        String key = keyOf(target, ref.uri());
        byte[] content = storage.read(key, MAX_CALIBRATION_BYTES).orElseThrow(SetupArtifactRegistrar::invalid);
        if ((ref.sizeBytes() != null && ref.sizeBytes() != content.length) || !ref.sha256().equals(sha256Hex(content))) {
            log.info("세팅 보정 문서 거부: 크기·해시가 알린 값과 다르다(세션 {})", target.setupSessionId());
            throw invalid();
        }
        JsonNode doc;
        try {
            doc = mapper.readTree(content);
        } catch (JacksonException e) {
            throw invalid();
        }
        if (doc == null || !doc.isObject() || !version.equals(doc.path("calibration_version").asString(null))) {
            throw invalid();
        }
        Set<String> docCameras = new TreeSet<>();
        doc.path("cameras").values().forEach(c -> docCameras.add(c.path("camera_id").asString("")));
        if (!docCameras.equals(new TreeSet<>(target.cameraIds()))) {
            log.info("세팅 보정 문서 거부: 카메라 집합 {} 이 세션 {} 과 다르다", docCameras, new TreeSet<>(target.cameraIds()));
            throw invalid();
        }
        // 세팅 출처(계약 2.2.0 setup_session_id)가 이 세션이어야 한다. Tier 0 도 이 출처가 있어야 새 기준으로 채택한다
        if (!target.setupSessionId().toString().equals(doc.path("setup_session_id").asString(null))) {
            log.info("세팅 보정 문서 거부: setup_session_id 가 세션 {} 이 아니다", target.setupSessionId());
            throw invalid();
        }
        try {
            calibrations.registerFromSetup(target.siteId(), content, ref.uri(), target.jobId(),
                    "현장 세팅 " + target.setupSessionId(), doc.path("previous_calibration_version").asString(null));
        } catch (BusinessException e) {
            if (e.getErrorCode() instanceof ErrorCode.Config) {
                log.info("세팅 보정 문서 거부({}): 세션 {}", e.getErrorCode(), target.setupSessionId());
                throw invalid();
            }
            throw e;
        }
        link(target, CALIBRATION, version, ref);
    }

    private void registerScene(Target target, ObjectRef ref, String kind, String version, String calibrationVersion, String notes) {
        if (alreadyLinked(target, kind, version, ref)) {
            return;
        }
        String key = keyOf(target, ref.uri());
        ObjectHead head = storage.head(key).orElseThrow(SetupArtifactRegistrar::invalid);
        if ((ref.sizeBytes() != null && ref.sizeBytes() != head.sizeBytes())
                || (head.sha256Hex() != null && !ref.sha256().equals(head.sha256Hex()))) {
            log.info("세팅 GLB 거부: 크기·저장소 체크섬이 알린 값과 다르다(세션 {}, {})", target.setupSessionId(), kind);
            throw invalid();
        }
        if (head.sha256Hex() == null) {
            // 체크섬을 기록하지 않는 저장소. 서명에 묶은 해시로 올라왔다고 보고 알린 값을 남긴다(대조는 못 함)
            log.warn("세팅 GLB 의 저장소 체크섬이 없어 해시를 대조하지 못했다(세션 {}, {})", target.setupSessionId(), kind);
        }
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("v", version).addValue("site", target.siteId()).addValue("cal", calibrationVersion)
                .addValue("key", key).addValue("user", target.requestedBy())
                .addValue("quality", mapper.writeValueAsString(mapper.createObjectNode().put("notes", notes)));
        Boolean calibrationKnown = jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM calibration_version WHERE calibration_version = :cal AND site_id = :site)
                """, params, Boolean.class);
        if (!Boolean.TRUE.equals(calibrationKnown)) {
            throw invalid();
        }
        // 같은 Scene ID 를 다른 작업이 먼저 주장하는 경우를 한 줄로 세운다(새 버전은 잠글 행이 없다)
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.scene:' || :v))", params);
        List<Map<String, Object>> existing = jdbc.queryForList("""
                SELECT site_id, calibration_version, gltf_key FROM scene_version WHERE scene_version = :v
                """, params);
        if (existing.isEmpty()) {
            // 세팅 미리보기·정식은 같은 작업에서 둘이 나와 job_id(작업당 하나) 대신 setup_session_artifact 로 출처를 남긴다
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                        is_upper_bound, requested_by)
                    VALUES (:v, :site, :cal, 'CANDIDATE', :key, CAST(:quality AS jsonb), false, :user)
                    """, params);
        } else {
            Map<String, Object> row = existing.getFirst();
            if (!target.siteId().equals(row.get("site_id")) || !calibrationVersion.equals(row.get("calibration_version"))
                    || !key.equals(row.get("gltf_key"))) {
                // 다른 산출물이 이미 쓰는 Scene ID. 덮어쓰지 않는다
                throw invalid();
            }
        }
        link(target, kind, version, ref);
    }

    /** 이 세션이 같은 버전을 이미 등록했으면 true(같은 해시만). 다른 해시면 거부 */
    private boolean alreadyLinked(Target target, String kind, String version, ObjectRef ref) {
        List<String> hashes = jdbc.queryForList("""
                SELECT sha256 FROM setup_session_artifact WHERE setup_session_id = :id AND kind = :kind AND version = :v
                """, new MapSqlParameterSource("id", target.setupSessionId()).addValue("kind", kind).addValue("v", version),
                String.class);
        if (hashes.isEmpty()) {
            return false;
        }
        if (!hashes.getFirst().equals(ref.sha256())) {
            throw invalid();
        }
        return true;
    }

    private void link(Target target, String kind, String version, ObjectRef ref) {
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256,
                    size_bytes, registered_at)
                VALUES (:id, :kind, :v, :job, :lease, :uri, :sha, :size, :now)
                ON CONFLICT (setup_session_id, kind, version) DO NOTHING
                """, new MapSqlParameterSource()
                .addValue("id", target.setupSessionId()).addValue("kind", kind).addValue("v", version)
                .addValue("job", target.jobId()).addValue("lease", target.leaseId()).addValue("uri", ref.uri())
                .addValue("sha", ref.sha256()).addValue("size", ref.sizeBytes())
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC)));
    }

    /** 우리 버킷의 이 작업·이 대여 결과 경로, 한 단계 이름만 */
    private String keyOf(Target target, String uri) {
        if (target.leaseId() == null) {
            throw invalid();
        }
        String bucketPrefix = storage.uri("");
        String prefix = KEY_PREFIX + target.jobId() + "/" + target.leaseId() + "/";
        if (!uri.startsWith(bucketPrefix)) {
            throw invalid();
        }
        String key = uri.substring(bucketPrefix.length());
        if (!key.startsWith(prefix)) {
            throw invalid();
        }
        String name = key.substring(prefix.length());
        if (!NAME.matcher(name).matches() || name.contains("..")) {
            throw invalid();
        }
        return key;
    }

    private static String required(String version) {
        if (version == null) {
            throw invalid();
        }
        return version;
    }

    private static String sha256Hex(byte[] content) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
    }

    /** 계약 site_setup#/$defs/object_ref. 형식은 계약 검사를 지났지만 다시 본다 */
    private record ObjectRef(String uri, String sha256, Long sizeBytes) {

        static ObjectRef of(JsonNode node) {
            if (node == null || !node.isObject()) {
                throw invalid();
            }
            String uri = node.path("uri").asString(null);
            String sha = node.path("sha256").asString(null);
            JsonNode size = node.get("size_bytes");
            if (uri == null || !uri.startsWith("s3://") || sha == null || !SHA256_HEX.matcher(sha).matches()
                    || (size != null && !size.isNull() && (!size.isIntegralNumber() || size.longValue() < 1))) {
                throw invalid();
            }
            return new ObjectRef(uri, sha, size == null || size.isNull() ? null : size.longValue());
        }
    }
}
