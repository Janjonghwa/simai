package com.ssafy.simai.domain.setup;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobProgress;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.ObjectHead;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import java.util.regex.Pattern;
import java.security.SecureRandom;
import java.time.Clock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 맵 보강 촬영과 다시 굽기(S15P31A101-607, docs/design/S15P31A101-607-supplement-capture-flow.md, 계약 site_setup 1.10.0 -605).
 * <ul>
 *   <li>미리보기 맵이 나온 세팅 세션(READY_FOR_ZONES 이후)에 관리자가 휴대폰·PC 로 찍은 동영상·사진을 올린다. 파일마다 서버가 키
 *       ({@code setup/<세션>/supplement/<media_id><확장자>})를 정해 서명 PUT 주소를 내고, 올린 뒤 확인(HEAD: 있음·크기·체크섬)을 받아
 *       {@code setup_supplement_media} 에 남긴다. 찍은 기기는 등록 카메라가 아니다(camera 표·스트림·관제에 넣지 않는다).
 *   <li>다시 굽기 요청은 그 세션의 처음 site_setup 입력에 {@code mode=SUPPLEMENT_REBAKE}·올린 파일({@code supplement_media})·
 *       이미 보관한 기준({@code rebake_base}: 보정 문서·미리보기·현재 맵·굽기 입력 사진)을 더해 새 site_setup 작업으로 넣는다.
 *       세션의 원래 세팅 작업이 끝났을 때(정식 맵 굽기 완료), 세션당 하나, 관제 중(IN_PROGRESS) 작업이 없을 때만.
 *   <li>하트비트(BAKING_TEXTURE)는 다시 굽기 행의 진행률만 바꾸고, 결과의 {@code supplement.status} 가 APPLIED 면 새 GLB 를 이 세션 귀속
 *       SCENE 후보로 등록하고 세션의 맵({@code scene_version})을 그 버전으로 옮긴다. 현재 Scene 으로 올리는 것은 호출한
 *       {@link SetupService} 가 세팅 정식 Scene 과 같은 절차로 한다(지금 현재 Scene 이 기준 맵일 때만, 관제 중이면 안 함).
 *       FALLBACK·SKIPPED 는 아무 Scene 도 등록하지 않고 원래 맵을 둔다(작업은 SUCCEEDED).
 * </ul>
 * 위치·판정·보정에는 들어가지 않는다(맵 Visual 층만, AGENTS 규칙 1).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupSupplementService {

    private static final Logger log = LoggerFactory.getLogger(SetupSupplementService.class);

    /** 계약 supplement_media maxItems */
    static final int MAX_ITEMS = 32;
    /** 동영상 한 개 상한 1 GiB: 4K30 H.264 약 2.5분·1080p 약 8분. 천천히 한 바퀴(1~2분)면 충분하고 현장 Wi-Fi·워커 내려받기 시간을 묶는다 */
    static final long MAX_VIDEO_BYTES = 1L << 30;
    /** 사진 한 장 상한 50 MiB: DSLR 최고화질 JPEG(10~25 MB)·4K PNG(20~40 MB) */
    static final long MAX_IMAGE_BYTES = 50L << 20;
    /** 세션 전체 상한 4 GiB: 워커가 30분 예산 안에서 내려받아 프레임을 고를 수 있는 양(GPU 인스턴스 임시 디스크 포함) */
    static final long MAX_TOTAL_BYTES = 4L << 30;
    /** 다시 굽기는 배경 수집·보정 없이 프레임 고르기·등록·굽기만 한다. 계약 예시와 같은 30분 */
    static final int REBAKE_TIMEOUT_MS = 1_800_000;
    static final int REBAKE_MAX_ATTEMPTS = 2;
    static final String MODE = "SUPPLEMENT_REBAKE";

    /** 받는 형식 → (종류, 키 확장자). HEIC 는 워커 디코더가 보장되지 않아 받지 않는다(iOS 는 image/* 입력을 JPEG 로 바꿔 준다) */
    private static final Map<String, String[]> TYPES = Map.of(
            "video/mp4", new String[] {"VIDEO", ".mp4"},
            "video/quicktime", new String[] {"VIDEO", ".mov"},
            "image/jpeg", new String[] {"IMAGE", ".jpg"},
            "image/png", new String[] {"IMAGE", ".png"});
    static final List<String> CONTENT_TYPES = List.of("video/mp4", "video/quicktime", "image/jpeg", "image/png");

    private static final Set<String> READY_OR_LATER = Set.of("READY_FOR_ZONES", "BAKING_TEXTURE", "COMPLETED");
    private static final Set<String> SOURCES = Set.of("PHONE_UPLOAD", "FILE_UPLOAD");
    private static final Set<String> OUTCOMES = Set.of("APPLIED", "FALLBACK", "SKIPPED");
    private static final Pattern SHA256_HEX = Pattern.compile("^[0-9a-f]{64}$");
    private static final Pattern MEDIA_ID = Pattern.compile("^[A-Za-z0-9_-]{1,64}$");
    private static final Pattern BUNDLE_ID = Pattern.compile("^bundle-[0-9a-f]{32}$");
    private static final SecureRandom RANDOM = new SecureRandom();

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final AnalysisJobService jobs;
    private final ObjectStorage storage;
    private final SetupArtifactRegistrar artifacts;
    private final Clock clock;

    public SetupSupplementService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SiteLookup sites, AnalysisJobService jobs,
            ObjectStorage storage, SetupArtifactRegistrar artifacts, Clock clock) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.sites = sites;
        this.jobs = jobs;
        this.storage = storage;
        this.artifacts = artifacts;
        this.clock = clock;
    }

    /** POST /api/setup-sessions/{id}/supplement-media 본문. 형식·크기는 필수, 나머지는 모르면 비운다 */
    public record IssueRequest(
            @JsonProperty("content_type") String contentType,
            @JsonProperty("size_bytes") Long sizeBytes,
            @JsonProperty("sha256") String sha256,
            @JsonProperty("source") String source,
            @JsonProperty("file_name") String fileName,
            @JsonProperty("captured_at_ms") Long capturedAtMs,
            @JsonProperty("device") Device device) {
    }

    public record Device(@JsonProperty("make") String make, @JsonProperty("model") String model) {
    }

    /** 결과가 APPLIED 일 때 호출한 쪽이 현재 Scene 으로 올리는 데 쓰는 값 */
    record Applied(UUID setupSessionId, String siteId, String calibrationVersion, String sceneVersion, String baseSceneVersion) {
    }

    // ---------------------------------------------------------------- 업로드

    /** 보강 촬영 파일 하나의 서명 PUT 주소를 내고 기록을 만든다(올리기 전, PENDING_UPLOAD). */
    @Transactional
    public ObjectNode issue(UUID setupSessionId, IssueRequest request, long userId) {
        if (request == null) {
            throw invalidMedia();
        }
        String contentType = request.contentType() == null ? "" : request.contentType().strip().toLowerCase(Locale.ROOT);
        String[] type = TYPES.get(contentType);
        long size = request.sizeBytes() == null ? 0 : request.sizeBytes();
        if (type == null || size < 1 || size > ("VIDEO".equals(type[0]) ? MAX_VIDEO_BYTES : MAX_IMAGE_BYTES)) {
            throw invalidMedia();
        }
        String sha256 = request.sha256() == null || request.sha256().isBlank() ? null
                : request.sha256().strip().toLowerCase(Locale.ROOT);
        if (sha256 != null && !SHA256_HEX.matcher(sha256).matches()) {
            throw invalidMedia();
        }
        String source = request.source() == null || request.source().isBlank() ? null : request.source().strip();
        if (source != null && !SOURCES.contains(source)) {
            throw invalidMedia();
        }
        if (request.capturedAtMs() != null && request.capturedAtMs() < 0) {
            throw invalidMedia();
        }
        String fileName = displayName(request.fileName());
        String make = deviceText(request.device() == null ? null : request.device().make());
        String model = deviceText(request.device() == null ? null : request.device().model());

        String siteId = sites.siteId();
        SessionInfo session = loadSession(siteId, setupSessionId, true);
        String blocked = uploadBlockedReason(session);
        if (blocked != null) {
            throw new BusinessException(errorOf(blocked));
        }
        Map<String, Object> usage = jdbc.queryForMap("""
                SELECT count(*) AS n, COALESCE(sum(size_bytes), 0) AS total
                FROM setup_supplement_media WHERE setup_session_id = :id AND removed_at IS NULL
                """, new MapSqlParameterSource("id", setupSessionId));
        long count = ((Number) usage.get("n")).longValue();
        long total = ((Number) usage.get("total")).longValue();
        if (count >= MAX_ITEMS || total + size > MAX_TOTAL_BYTES) {
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_LIMIT_EXCEEDED);
        }
        if (!storage.configured()) {
            throw new BusinessException(ErrorCode.Storage.STORAGE_NOT_CONFIGURED);
        }

        String mediaId = "sup-" + randomHex(8);
        String key = "setup/" + setupSessionId + "/supplement/" + mediaId + type[1];
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                INSERT INTO setup_supplement_media (setup_session_id, media_id, kind, source, content_type, object_key, size_bytes,
                    sha256, file_name, captured_at, device_make, device_model, requested_by, created_at)
                VALUES (:id, :media, :kind, :source, :type, :key, :size, :sha, :name, :captured, :make, :model, :user, :now)
                """, new MapSqlParameterSource()
                .addValue("id", setupSessionId).addValue("media", mediaId).addValue("kind", type[0])
                .addValue("source", source).addValue("type", contentType).addValue("key", key).addValue("size", size)
                .addValue("sha", sha256).addValue("name", fileName)
                .addValue("captured", request.capturedAtMs() == null ? null
                        : utc(Instant.ofEpochMilli(request.capturedAtMs())))
                .addValue("make", make).addValue("model", model).addValue("user", userId).addValue("now", now));
        // 서명이 실패하면(자격 증명 없음 등 503) 트랜잭션이 되돌아가 기록도 남지 않는다
        SignedRequest signed = storage.signPut(key, contentType, size, sha256);
        ObjectNode out = mediaView(setupSessionId, mediaId);
        ObjectNode upload = out.putObject("upload");
        upload.put("method", "PUT");
        upload.put("url", signed.url());
        ObjectNode headers = upload.putObject("headers");
        signed.headers().forEach(headers::put);
        upload.put("expires_at_ms", signed.expiresAt().toEpochMilli());
        return out;
    }

    /** 올리기를 마쳤다는 알림. 저장소 HEAD 로 있음·크기·(해시를 알렸고 저장소가 기록했으면) 체크섬을 확인한다. 이미 확인했으면 그대로 돌려준다 */
    @Transactional
    public ObjectNode complete(UUID setupSessionId, String mediaId) {
        String siteId = sites.siteId();
        loadSession(siteId, setupSessionId, false);
        MediaRow media = findMediaForUpdate(setupSessionId, mediaId);
        if (media.uploaded()) {
            return mediaView(setupSessionId, mediaId);
        }
        ObjectHead head = storage.head(media.objectKey())
                .orElseThrow(() -> new BusinessException(ErrorCode.Setup.SUPPLEMENT_UPLOAD_INCOMPLETE));
        if (head.sizeBytes() != media.sizeBytes()
                || (media.sha256() != null && head.sha256Hex() != null && !media.sha256().equals(head.sha256Hex()))) {
            log.info("보강 촬영 파일 확인 거부: 크기·체크섬이 알린 값과 다르다(세션 {}, {})", setupSessionId, mediaId);
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_UPLOAD_INCOMPLETE);
        }
        jdbc.update("""
                UPDATE setup_supplement_media SET uploaded_at = :now WHERE setup_session_id = :id AND media_id = :media
                """, new MapSqlParameterSource("id", setupSessionId).addValue("media", mediaId)
                .addValue("now", utc(clock.instant())));
        return mediaView(setupSessionId, mediaId);
    }

    /** 목록에서 뺀다(removed_at 만 남김). 진행 중인 다시 굽기가 쓰는 파일은 뺄 수 없다 */
    @Transactional
    public void remove(UUID setupSessionId, String mediaId) {
        String siteId = sites.siteId();
        loadSession(siteId, setupSessionId, true);
        findMediaForUpdate(setupSessionId, mediaId);
        Boolean inUse = jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_supplement_rebake
                               WHERE setup_session_id = :id AND status IN ('QUEUED', 'RUNNING')
                                 AND media_ids @> jsonb_build_array(CAST(:media AS text)))
                """, new MapSqlParameterSource("id", setupSessionId).addValue("media", mediaId), Boolean.class);
        if (Boolean.TRUE.equals(inUse)) {
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_MEDIA_IN_USE);
        }
        jdbc.update("""
                UPDATE setup_supplement_media SET removed_at = :now WHERE setup_session_id = :id AND media_id = :media
                """, new MapSqlParameterSource("id", setupSessionId).addValue("media", mediaId)
                .addValue("now", utc(clock.instant())));
    }

    // ---------------------------------------------------------------- 다시 굽기 요청·취소

    /** 올린 파일 전부로 SUPPLEMENT_REBAKE 작업을 넣는다. 막히면 그 사유의 409 */
    @Transactional
    public void requestRebake(UUID setupSessionId, long userId) {
        String siteId = sites.siteId();
        // 같은 현장의 세팅 시작·다른 다시 굽기와 한 줄로 세운다(SetupService.create 도 site 행을 잠근다)
        jdbc.queryForList("SELECT site_id FROM site WHERE site_id = :site FOR UPDATE",
                new MapSqlParameterSource("site", siteId), String.class);
        SessionInfo session = loadSession(siteId, setupSessionId, true);
        String blocked = rebakeBlockedReason(session);
        if (blocked != null) {
            throw new BusinessException(errorOf(blocked));
        }
        Base base = rebakeBase(session);
        if (base == null) {
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_REBAKE_BASE_MISSING);
        }
        List<MediaRow> media = uploadedMedia(setupSessionId);

        String rawInput = jdbc.queryForObject("SELECT input::text FROM analysis_job WHERE job_id = :job",
                new MapSqlParameterSource("job", session.jobId()), String.class);
        ObjectNode input = (ObjectNode) mapper.readTree(rawInput);
        List.of("mode", "supplement_media", "rebake_base").forEach(input::remove);
        ArrayNode items = input.putArray("supplement_media");
        ArrayNode mediaIds = mapper.createArrayNode();
        for (MediaRow m : media) {
            ObjectNode item = items.addObject();
            item.put("media_id", m.mediaId());
            item.put("kind", m.kind());
            item.put("uri", storage.uri(m.objectKey()));
            if (m.source() != null) {
                item.put("source", m.source());
            }
            if (m.capturedAtMs() != null) {
                item.put("captured_at_ms", m.capturedAtMs());
            }
            if (m.deviceMake() != null || m.deviceModel() != null) {
                ObjectNode device = item.putObject("device");
                if (m.deviceMake() != null) {
                    device.put("make", m.deviceMake());
                }
                if (m.deviceModel() != null) {
                    device.put("model", m.deviceModel());
                }
            }
            if (m.sha256() != null) {
                item.put("sha256", m.sha256());
            }
            item.put("size_bytes", m.sizeBytes());
            mediaIds.add(m.mediaId());
        }
        input.put("mode", MODE);
        input.set("rebake_base", base.node());

        // 계약 검사(job_request)는 등록 때 AnalysisJobService 가 한다. 어긋나면 400 INVALID_JOB 로 등록하지 않는다
        String jobId = jobs.enqueue(new JobSpec("site_setup", input, REBAKE_MAX_ATTEMPTS, REBAKE_TIMEOUT_MS, null, userId));
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                INSERT INTO setup_supplement_rebake (rebake_id, setup_session_id, job_id, status, progress_pct, media_ids,
                    base_calibration_version, base_preview_scene_version, base_scene_version, base_bake_job_id, base_bake_lease_id,
                    requested_by, requested_at, updated_at)
                VALUES (:rid, :id, :job, 'QUEUED', 0, CAST(:media AS jsonb), :cal, :preview, :scene, :bakeJob, :bakeLease,
                    :user, :now, :now)
                """, new MapSqlParameterSource()
                .addValue("rid", UUID.randomUUID()).addValue("id", setupSessionId).addValue("job", jobId)
                .addValue("media", mapper.writeValueAsString(mediaIds))
                .addValue("cal", base.calibrationVersion()).addValue("preview", base.previewSceneVersion())
                .addValue("scene", base.sceneVersion()).addValue("bakeJob", base.bakeJobId())
                .addValue("bakeLease", base.bakeLeaseId()).addValue("user", userId).addValue("now", now));
        log.info("세팅 세션 {} 맵 보강 다시 굽기 작업 {} 등록(파일 {}개, 기준 맵 {})", setupSessionId, jobId, media.size(),
                base.sceneVersion());
    }

    /** 진행 중인 다시 굽기를 취소한다(워커가 멈췄거나 대기열에 묶였을 때). 원래 맵은 그대로다 */
    @Transactional
    public void cancelRebake(UUID setupSessionId) {
        String siteId = sites.siteId();
        loadSession(siteId, setupSessionId, true);
        List<String> running = jdbc.queryForList("""
                SELECT job_id FROM setup_supplement_rebake WHERE setup_session_id = :id AND status IN ('QUEUED', 'RUNNING')
                """, new MapSqlParameterSource("id", setupSessionId), String.class);
        if (running.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_REBAKE_NOT_RUNNING);
        }
        // 작업 취소 알림(JobFinished CANCELLED)이 SetupService → onRebakeFinished 로 와서 행을 CANCELLED 로 닫는다
        if (!jobs.cancel(running.getFirst())) {
            closeRebake(running.getFirst(), "CANCELLED", "CANCELLED", "다시 굽기를 취소했습니다");
        }
    }

    /** 같은 현장에 진행 중인 다시 굽기가 있는지(새 세팅 시작을 막는 데 쓴다. GPU 인스턴스는 하나다) */
    boolean rebakeRunningInSite(String siteId) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_supplement_rebake r JOIN setup_session s ON s.setup_session_id = r.setup_session_id
                               WHERE s.site_id = :site AND r.status IN ('QUEUED', 'RUNNING'))
                """, new MapSqlParameterSource("site", siteId), Boolean.class));
    }

    // ---------------------------------------------------------------- 진행·결과 받기(SetupService 가 부른다)

    boolean isRebakeJob(String jobId) {
        return Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM setup_supplement_rebake WHERE job_id = :job)",
                new MapSqlParameterSource("job", jobId), Boolean.class));
    }

    /** 다시 굽기 하트비트. BAKING_TEXTURE 만 받고 진행률은 같은 대여 안에서 줄지 않는다. 어긋나면 400(INVALID_SETUP_PROGRESS) */
    void onRebakeProgress(JobProgress event) {
        RebakeRow rebake = findRebakeForUpdate(event.jobId());
        if (rebake == null || !rebake.running()) {
            throw invalidProgress();
        }
        JsonNode snap = event.setup();
        checkSnapshot(rebake, snap);
        if (!"BAKING_TEXTURE".equals(snap.path("status").asString(null))) {
            throw invalidProgress();
        }
        double pct = progressOf(snap);
        boolean sameLease = rebake.progressLeaseId() != null && rebake.progressLeaseId().equals(event.leaseId());
        if (sameLease && pct < rebake.progressPct() - 1e-6) {
            throw invalidProgress();
        }
        if (snap.has("artifacts")) {
            // 다시 굽기 산출물(새 GLB)은 결과에서만 등록한다
            log.debug("다시 굽기 하트비트의 artifacts 는 등록하지 않는다(작업 {})", event.jobId());
        }
        jdbc.update("""
                UPDATE setup_supplement_rebake
                SET status = 'RUNNING', stage = 'BAKING_TEXTURE', progress_pct = :pct, progress_lease_id = :lease, updated_at = :now
                WHERE rebake_id = :rid
                """, new MapSqlParameterSource("rid", rebake.rebakeId()).addValue("pct", pct).addValue("lease", event.leaseId())
                .addValue("now", utc(clock.instant())));
    }

    /**
     * 다시 굽기 작업이 끝났다. SUCCEEDED 면 결과를 검사해 APPLIED 는 새 Scene 을 이 세션 귀속 후보로 등록하고 세션 맵을 옮긴 뒤
     * {@link Applied} 를 돌려준다(현재 Scene 올리기는 호출한 쪽). FALLBACK·SKIPPED 는 아무것도 등록하지 않는다.
     * 실패·시간 초과·취소면 행만 닫는다(원래 맵 유지). 결과가 어긋나면 400 으로 제출 전체를 되돌린다.
     */
    Applied onRebakeFinished(JobFinished event) {
        RebakeRow rebake = findRebakeForUpdate(event.jobId());
        if (rebake == null || !rebake.running()) {
            return null;
        }
        if (!"SUCCEEDED".equals(event.status())) {
            String code = event.status();
            String message = "CANCELLED".equals(event.status()) ? "다시 굽기를 취소했습니다" : "맵 다시 굽기 작업이 중단되었습니다";
            JsonNode err = event.error();
            if (err != null && err.isObject()) {
                String errCode = err.path("code").asString(null);
                String errMsg = err.path("message").asString(null);
                if (errCode != null && !errCode.isBlank()) {
                    code = errCode;
                }
                if (errMsg != null && !errMsg.isBlank()) {
                    message = errMsg;
                }
            }
            closeRebake(event.jobId(), "CANCELLED".equals(event.status()) ? "CANCELLED" : "FAILED", code, message);
            return null;
        }

        JsonNode result = event.result();
        checkSnapshot(rebake, result);
        if (!"COMPLETED".equals(result.path("status").asString(null)) || Math.abs(progressOf(result) - 100.0) > 1e-6) {
            throw invalidProgress();
        }
        JsonNode supplement = result.get("supplement");
        if (supplement == null || !supplement.isObject()) {
            throw invalidProgress();
        }
        String outcome = supplement.path("status").asString(null);
        if (outcome == null || !OUTCOMES.contains(outcome)) {
            throw invalidProgress();
        }
        Set<String> requested = new TreeSet<>();
        rebake.mediaIds().forEach(n -> requested.add(n.asString()));
        for (JsonNode m : supplement.path("media").values()) {
            if (!requested.contains(m.path("media_id").asString(""))) {
                throw invalidProgress();
            }
        }
        String sceneVersion = result.path("scene_version").isString() ? result.get("scene_version").asString() : null;
        JsonNode resultArtifacts = result.get("artifacts");
        boolean hasArtifacts = resultArtifacts != null && !resultArtifacts.isNull() && !resultArtifacts.isEmpty();
        OffsetDateTime now = utc(clock.instant());

        if ("APPLIED".equals(outcome)) {
            JsonNode sceneRef = hasArtifacts ? resultArtifacts.get("scene") : null;
            if (sceneVersion == null || sceneVersion.equals(rebake.baseSceneVersion()) || sceneRef == null || sceneRef.isNull()
                    || resultArtifacts.size() != 1) {
                throw invalidProgress();
            }
            SessionInfo session = loadSessionById(rebake.setupSessionId());
            UUID submittedLease = jdbc.queryForList("""
                    SELECT l.lease_id FROM job_lease l JOIN analysis_job j ON j.job_id = l.job_id AND j.attempt = l.attempt
                    WHERE l.job_id = :job AND l.status = 'SUBMITTED'
                    """, new MapSqlParameterSource("job", event.jobId()), UUID.class).stream().findFirst().orElse(null);
            artifacts.registerRebakeScene(new SetupArtifactRegistrar.Target(session.setupSessionId(), session.siteId(),
                            event.jobId(), submittedLease, new TreeSet<>(session.cameraIds()), rebake.requestedBy()),
                    sceneRef, sceneVersion, rebake.baseCalibrationVersion(), rebake.baseSceneVersion());
            jdbc.update("""
                    UPDATE setup_supplement_rebake
                    SET status = 'SUCCEEDED', stage = 'COMPLETED', progress_pct = 100, outcome = 'APPLIED',
                        result_scene_version = :scene, supplement = CAST(:sup AS jsonb), completed_at = :now, updated_at = :now
                    WHERE rebake_id = :rid
                    """, new MapSqlParameterSource("rid", rebake.rebakeId()).addValue("scene", sceneVersion)
                    .addValue("sup", mapper.writeValueAsString(supplement)).addValue("now", now));
            // 이 세션의 맵을 다시 구운 버전으로 옮긴다(다음 다시 굽기의 기준). 이력은 다시 굽기 행에 남는다
            jdbc.update("UPDATE setup_session SET scene_version = :scene, updated_at = :now WHERE setup_session_id = :id",
                    new MapSqlParameterSource("id", session.setupSessionId()).addValue("scene", sceneVersion)
                            .addValue("now", now));
            return new Applied(session.setupSessionId(), session.siteId(), rebake.baseCalibrationVersion(), sceneVersion,
                    rebake.baseSceneVersion());
        }
        // FALLBACK·SKIPPED: 기준 맵 그대로, 새 GLB 없음
        if (!rebake.baseSceneVersion().equals(sceneVersion) || hasArtifacts) {
            throw invalidProgress();
        }
        jdbc.update("""
                UPDATE setup_supplement_rebake
                SET status = 'SUCCEEDED', stage = 'COMPLETED', progress_pct = 100, outcome = :outcome,
                    supplement = CAST(:sup AS jsonb), completed_at = :now, updated_at = :now
                WHERE rebake_id = :rid
                """, new MapSqlParameterSource("rid", rebake.rebakeId()).addValue("outcome", outcome)
                .addValue("sup", mapper.writeValueAsString(supplement)).addValue("now", now));
        return null;
    }

    /** 세션·모드·기준 보정·미리보기가 이 다시 굽기와 같은지(진행·결과 공통) */
    private void checkSnapshot(RebakeRow rebake, JsonNode snap) {
        if (snap == null || !snap.isObject()
                || !rebake.setupSessionId().toString().equals(snap.path("setup_session_id").asString(null))
                || !MODE.equals(snap.path("mode").asString(null))) {
            throw invalidProgress();
        }
        String cal = snap.path("calibration_version").asString(null);
        String preview = snap.path("preview_scene_version").asString(null);
        if ((cal != null && !cal.equals(rebake.baseCalibrationVersion()))
                || (preview != null && !preview.equals(rebake.basePreviewSceneVersion()))) {
            throw invalidProgress();
        }
    }

    private static double progressOf(JsonNode snap) {
        JsonNode pct = snap.get("progress_pct");
        if (pct == null || !pct.isNumber() || !Double.isFinite(pct.doubleValue()) || pct.doubleValue() < 0
                || pct.doubleValue() > 100) {
            throw invalidProgress();
        }
        return pct.doubleValue();
    }

    private void closeRebake(String jobId, String status, String code, String message) {
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                UPDATE setup_supplement_rebake
                SET status = :status, failure_code = :code, failure_message = :msg, completed_at = :now, updated_at = :now
                WHERE job_id = :job AND status IN ('QUEUED', 'RUNNING')
                """, new MapSqlParameterSource("job", jobId).addValue("status", status).addValue("code", code)
                .addValue("msg", message).addValue("now", now));
    }

    // ---------------------------------------------------------------- 조회

    /** GET /api/setup-sessions/{id} 의 supplement(올린 파일·상한·막힌 사유·마지막 다시 굽기). */
    ObjectNode view(String siteId, UUID setupSessionId) {
        SessionInfo session = loadSession(siteId, setupSessionId, false);
        ObjectNode out = mapper.createObjectNode();
        ArrayNode media = out.putArray("media");
        jdbc.query(MEDIA_SELECT + " WHERE setup_session_id = :id AND removed_at IS NULL ORDER BY created_at, media_id",
                new MapSqlParameterSource("id", setupSessionId), rs -> {
                    media.add(mediaJson(mapMedia(rs)));
                });
        ObjectNode limits = out.putObject("limits");
        limits.put("max_items", MAX_ITEMS);
        limits.put("max_video_bytes", MAX_VIDEO_BYTES);
        limits.put("max_image_bytes", MAX_IMAGE_BYTES);
        limits.put("max_total_bytes", MAX_TOTAL_BYTES);
        ArrayNode types = limits.putArray("content_types");
        CONTENT_TYPES.forEach(types::add);
        out.put("can_upload", uploadBlockedReason(session) == null);
        String blocked = rebakeBlockedReason(session);
        if (blocked == null && rebakeBase(session) == null) {
            blocked = "BASE_MISSING";
        }
        out.put("can_rebake", blocked == null);
        if (blocked == null) {
            out.putNull("rebake_blocked_reason");
        } else {
            out.put("rebake_blocked_reason", blocked);
        }
        List<ObjectNode> latest = jdbc.query("""
                SELECT r.rebake_id, r.job_id,
                       CASE WHEN r.status = 'QUEUED' AND j.status = 'LEASED' THEN 'RUNNING' ELSE r.status END AS status,
                       r.stage, r.progress_pct, r.media_ids::text AS media_ids, r.base_scene_version, r.result_scene_version,
                       r.outcome, r.supplement::text AS supplement, r.failure_code, r.failure_message, r.requested_at,
                       r.completed_at,
                       CASE WHEN r.result_scene_version IS NULL THEN NULL ELSE EXISTS (
                           SELECT 1 FROM scene_activation a WHERE a.site_id = :site AND a.scene_version = r.result_scene_version
                             AND a.actor = :actor) END AS scene_activated
                FROM setup_supplement_rebake r JOIN analysis_job j ON j.job_id = r.job_id
                WHERE r.setup_session_id = :id
                ORDER BY r.requested_at DESC
                LIMIT 1
                """, new MapSqlParameterSource("id", setupSessionId).addValue("site", siteId)
                .addValue("actor", "setup:" + setupSessionId), (rs, i) -> {
                    ObjectNode r = mapper.createObjectNode();
                    r.put("rebake_id", rs.getObject("rebake_id", UUID.class).toString());
                    r.put("job_id", rs.getString("job_id"));
                    r.put("status", rs.getString("status"));
                    putText(r, "stage", rs.getString("stage"));
                    r.put("progress_pct", rs.getDouble("progress_pct"));
                    r.set("media_ids", mapper.readTree(rs.getString("media_ids")));
                    r.put("base_scene_version", rs.getString("base_scene_version"));
                    putText(r, "scene_version", rs.getString("result_scene_version"));
                    putText(r, "outcome", rs.getString("outcome"));
                    String sup = rs.getString("supplement");
                    JsonNode supNode = sup == null ? mapper.nullNode() : mapper.readTree(sup);
                    JsonNode reason = supNode.path("reason");
                    putText(r, "reason", reason.isString() ? reason.asString() : null);
                    r.set("result", supNode);
                    boolean activated = rs.getBoolean("scene_activated");
                    if (rs.wasNull()) {
                        r.putNull("scene_activated");
                    } else {
                        r.put("scene_activated", activated);
                    }
                    putText(r, "failure_code", rs.getString("failure_code"));
                    putText(r, "failure_message", rs.getString("failure_message"));
                    r.put("requested_at_ms", rs.getTimestamp("requested_at").getTime());
                    var completed = rs.getTimestamp("completed_at");
                    if (completed == null) {
                        r.putNull("completed_at_ms");
                    } else {
                        r.put("completed_at_ms", completed.getTime());
                    }
                    return r;
                });
        if (latest.isEmpty()) {
            out.putNull("rebake");
        } else {
            out.set("rebake", latest.getFirst());
        }
        return out;
    }

    // ---------------------------------------------------------------- 상태 검사

    /**
     * 업로드가 막힌 사유(없으면 null): 이전 모델 재사용 세션, 미리보기 이전·실패, 더 새 세팅 세션.
     * 이전 모델 재사용(REUSE, S15P31A101-614)으로 끝났거나 진행 중인 세션은 이 세션이 만든 맵(미리보기·굽기 입력 사진)이 없어 다시 구울
     * 기준이 없다(현재 관제 맵은 다른 세션의 것). 맵을 보강하려면 새 모델로 세팅한다.
     * 보강은 READY_FOR_ZONES 부터 세팅 완료(C17) 전까지만 한다(S15P31A101-615). 완료 뒤에는 SETUP_COMPLETED.
     */
    private String uploadBlockedReason(SessionInfo s) {
        if ("REUSE".equals(s.mode())) {
            return "REUSED_SCENE";
        }
        if (!READY_OR_LATER.contains(s.status()) || s.previewSceneVersion() == null || s.calibrationVersion() == null) {
            return "SESSION_NOT_READY";
        }
        if (superseded(s)) {
            return "SUPERSEDED";
        }
        if (setupCompleted(s)) {
            return "SETUP_COMPLETED";
        }
        return null;
    }

    /** 이 세션으로 세팅을 완료했는지(작업에 걸린 세션이고 작업의 setup_completed_at 이 있다) */
    private boolean setupCompleted(SessionInfo s) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM work_unit
                               WHERE active_setup_session_id = :id AND setup_completed_at IS NOT NULL)
                """, new MapSqlParameterSource("id", s.setupSessionId()), Boolean.class));
    }

    /**
     * 다시 굽기가 막힌 사유(없으면 null). 순서: 세션 상태 → 원래 세팅 작업 진행 중(정식 맵 굽기) → 다시 굽기 진행 중 → 관제 중 작업
     * → 올린 파일 없음 → 워커 없음. 기준 자료 없음(BASE_MISSING)은 기준을 모을 때 따로 본다.
     */
    private String rebakeBlockedReason(SessionInfo s) {
        String upload = uploadBlockedReason(s);
        if (upload != null) {
            return upload;
        }
        MapSqlParameterSource p = new MapSqlParameterSource("job", s.jobId()).addValue("id", s.setupSessionId())
                .addValue("site", s.siteId());
        if (Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM analysis_job WHERE job_id = :job AND status IN ('QUEUED', 'LEASED'))", p,
                Boolean.class))) {
            return "SETUP_JOB_RUNNING";
        }
        if (Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_supplement_rebake WHERE setup_session_id = :id AND status IN ('QUEUED', 'RUNNING'))
                """, p, Boolean.class))) {
            return "REBAKE_RUNNING";
        }
        // 세팅 시작과 같은 LIVE 가드(SetupService.create): 관제 시간에는 오프라인 굽기 작업을 넣지 않는다
        if (Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS')", p, Boolean.class))) {
            return "LIVE_WORK";
        }
        if (!Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_supplement_media
                               WHERE setup_session_id = :id AND removed_at IS NULL AND uploaded_at IS NOT NULL)
                """, p, Boolean.class))) {
            return "NO_MEDIA";
        }
        if (!Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM worker WHERE revoked_at IS NULL AND 'site_setup' = ANY(job_types))", p,
                Boolean.class))) {
            return "NO_WORKER";
        }
        return null;
    }

    private boolean superseded(SessionInfo s) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM setup_session o
                               WHERE o.site_id = :site AND o.setup_session_id <> :id AND o.started_at > :started)
                """, new MapSqlParameterSource("site", s.siteId()).addValue("id", s.setupSessionId())
                .addValue("started", s.startedAt()), Boolean.class));
    }

    private static ErrorCode errorOf(String reason) {
        return switch (reason) {
            case "SESSION_NOT_READY" -> ErrorCode.Setup.SUPPLEMENT_SESSION_NOT_READY;
            case "REUSED_SCENE" -> ErrorCode.Setup.SUPPLEMENT_REUSED_SCENE;
            case "SUPERSEDED" -> ErrorCode.Setup.SUPPLEMENT_SESSION_SUPERSEDED;
            case "SETUP_COMPLETED" -> ErrorCode.Setup.SUPPLEMENT_SETUP_COMPLETED;
            case "SETUP_JOB_RUNNING" -> ErrorCode.Setup.SUPPLEMENT_SETUP_JOB_RUNNING;
            case "REBAKE_RUNNING" -> ErrorCode.Setup.SUPPLEMENT_REBAKE_IN_PROGRESS;
            case "LIVE_WORK" -> ErrorCode.Setup.SETUP_BLOCKED_BY_LIVE_WORK;
            case "NO_MEDIA" -> ErrorCode.Setup.SUPPLEMENT_NO_MEDIA;
            case "NO_WORKER" -> ErrorCode.Setup.SETUP_WORKER_UNAVAILABLE;
            default -> ErrorCode.Setup.SUPPLEMENT_REBAKE_BASE_MISSING;
        };
    }

    // ---------------------------------------------------------------- 기준(rebake_base)

    private record Base(ObjectNode node, String calibrationVersion, String previewSceneVersion, String sceneVersion,
            String bakeJobId, UUID bakeLeaseId) {
    }

    /**
     * 이 세션이 이미 보관한 값으로 rebake_base 를 만든다. 없거나 모자라면 null(지어내지 않는다).
     * <ul>
     *   <li>보정 문서: 세션 calibration_version 을 이 세션이 등록한 CALIBRATION 행(주소·sha256·크기).
     *   <li>현재 맵: 세션 scene_version, 정식 맵이 없으면(-597 굽기 회수 등) 미리보기.
     *   <li>굽기 입력 사진: 원래 세팅 작업의 한 대여가 알린 BAKE_IMAGE(-593). 정식 Scene 을 등록한 대여를 먼저, 없으면 최근 대여.
     *       세션 카메라마다 하나씩, 묶음 ID 가 하나여야 한다.
     * </ul>
     */
    private Base rebakeBase(SessionInfo s) {
        if (s.calibrationVersion() == null || s.previewSceneVersion() == null) {
            return null;
        }
        MapSqlParameterSource p = new MapSqlParameterSource("id", s.setupSessionId()).addValue("cal", s.calibrationVersion())
                .addValue("job", s.jobId());
        List<Map<String, Object>> cal = jdbc.queryForList("""
                SELECT object_uri, sha256, size_bytes FROM setup_session_artifact
                WHERE setup_session_id = :id AND kind = 'CALIBRATION' AND version = :cal
                """, p);
        if (cal.isEmpty()) {
            return null;
        }
        List<Map<String, Object>> bakeRows = jdbc.queryForList("""
                SELECT b.lease_id, b.camera_id, b.object_uri, b.sha256, b.size_bytes, b.face_policy, b.bundle_id,
                       EXISTS (SELECT 1 FROM setup_session_artifact s
                               WHERE s.setup_session_id = b.setup_session_id AND s.job_id = b.job_id AND s.lease_id = b.lease_id
                                 AND s.kind = 'SCENE') AS scene_lease,
                       max(b.registered_at) OVER (PARTITION BY b.lease_id) AS lease_at
                FROM setup_session_artifact b
                WHERE b.setup_session_id = :id AND b.job_id = :job AND b.kind = 'BAKE_IMAGE'
                ORDER BY scene_lease DESC, lease_at DESC, b.lease_id, b.camera_id
                """, p);
        Map<UUID, List<Map<String, Object>>> byLease = new LinkedHashMap<>();
        for (Map<String, Object> row : bakeRows) {
            byLease.computeIfAbsent((UUID) row.get("lease_id"), k -> new ArrayList<>()).add(row);
        }
        Set<String> cameras = new TreeSet<>(s.cameraIds());
        for (Map.Entry<UUID, List<Map<String, Object>>> lease : byLease.entrySet()) {
            Set<String> covered = new TreeSet<>();
            Set<String> bundles = new LinkedHashSet<>();
            lease.getValue().forEach(r -> {
                covered.add((String) r.get("camera_id"));
                bundles.add((String) r.get("bundle_id"));
            });
            if (!covered.equals(cameras) || bundles.size() != 1 || !BUNDLE_ID.matcher(bundles.iterator().next()).matches()) {
                continue;
            }
            ObjectNode node = mapper.createObjectNode();
            node.put("calibration_version", s.calibrationVersion());
            node.set("calibration_document", objectRef(cal.getFirst()));
            node.put("preview_scene_version", s.previewSceneVersion());
            String scene = s.sceneVersion() != null ? s.sceneVersion() : s.previewSceneVersion();
            node.put("scene_version", scene);
            ObjectNode bake = node.putObject("bake_images");
            bake.put("bundle_id", bundles.iterator().next());
            ArrayNode images = bake.putArray("images");
            for (Map<String, Object> r : lease.getValue()) {
                ObjectNode image = objectRef(r);
                ObjectNode item = images.addObject();
                item.put("camera_id", (String) r.get("camera_id"));
                item.setAll(image);
                item.put("face_policy", (String) r.get("face_policy"));
            }
            return new Base(node, s.calibrationVersion(), s.previewSceneVersion(), scene, s.jobId(), lease.getKey());
        }
        return null;
    }

    private ObjectNode objectRef(Map<String, Object> row) {
        ObjectNode ref = mapper.createObjectNode();
        ref.put("uri", (String) row.get("object_uri"));
        ref.put("sha256", (String) row.get("sha256"));
        Object size = row.get("size_bytes");
        if (size instanceof Number n) {
            ref.put("size_bytes", n.longValue());
        }
        return ref;
    }

    // ---------------------------------------------------------------- 행 읽기

    private record SessionInfo(UUID setupSessionId, String siteId, String jobId, String status, String calibrationVersion,
            String previewSceneVersion, String sceneVersion, OffsetDateTime startedAt, List<String> cameraIds, String mode) {
    }

    private static final String SESSION_SELECT = """
            SELECT setup_session_id, site_id, job_id, status, calibration_version, preview_scene_version, scene_version,
                   started_at, camera_heights_m::text AS camera_heights_m, mode
            FROM setup_session
            """;

    private SessionInfo loadSession(String siteId, UUID setupSessionId, boolean forUpdate) {
        List<SessionInfo> list = jdbc.query(SESSION_SELECT + " WHERE site_id = :site AND setup_session_id = :id"
                        + (forUpdate ? " FOR UPDATE" : ""),
                new MapSqlParameterSource("site", siteId).addValue("id", setupSessionId), (rs, i) -> mapSession(rs));
        if (list.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        return list.getFirst();
    }

    private SessionInfo loadSessionById(UUID setupSessionId) {
        return jdbc.query(SESSION_SELECT + " WHERE setup_session_id = :id", new MapSqlParameterSource("id", setupSessionId),
                (rs, i) -> mapSession(rs)).stream().findFirst().orElseThrow(SetupSupplementService::invalidProgress);
    }

    private SessionInfo mapSession(java.sql.ResultSet rs) throws java.sql.SQLException {
        List<String> cameras = new ArrayList<>();
        JsonNode heights = mapper.readTree(rs.getString("camera_heights_m"));
        heights.properties().forEach(e -> cameras.add(e.getKey()));
        return new SessionInfo(rs.getObject("setup_session_id", UUID.class), rs.getString("site_id"), rs.getString("job_id"),
                rs.getString("status"), rs.getString("calibration_version"), rs.getString("preview_scene_version"),
                rs.getString("scene_version"), rs.getObject("started_at", OffsetDateTime.class), cameras, rs.getString("mode"));
    }

    private record MediaRow(String mediaId, String kind, String source, String contentType, String objectKey, long sizeBytes,
            String sha256, String fileName, Long capturedAtMs, String deviceMake, String deviceModel, long createdAtMs,
            Long uploadedAtMs) {

        boolean uploaded() {
            return uploadedAtMs != null;
        }
    }

    private static final String MEDIA_SELECT = """
            SELECT media_id, kind, source, content_type, object_key, size_bytes, sha256, file_name, captured_at, device_make,
                   device_model, created_at, uploaded_at
            FROM setup_supplement_media
            """;

    private static MediaRow mapMedia(java.sql.ResultSet rs) throws java.sql.SQLException {
        var captured = rs.getTimestamp("captured_at");
        var uploaded = rs.getTimestamp("uploaded_at");
        return new MediaRow(rs.getString("media_id"), rs.getString("kind"), rs.getString("source"), rs.getString("content_type"),
                rs.getString("object_key"), rs.getLong("size_bytes"), rs.getString("sha256"), rs.getString("file_name"),
                captured == null ? null : captured.getTime(), rs.getString("device_make"), rs.getString("device_model"),
                rs.getTimestamp("created_at").getTime(), uploaded == null ? null : uploaded.getTime());
    }

    private MediaRow findMediaForUpdate(UUID setupSessionId, String mediaId) {
        if (mediaId == null || !MEDIA_ID.matcher(mediaId).matches()) {
            throw new BusinessException(ErrorCode.Setup.SUPPLEMENT_MEDIA_NOT_FOUND);
        }
        return jdbc.query(MEDIA_SELECT + " WHERE setup_session_id = :id AND media_id = :media AND removed_at IS NULL FOR UPDATE",
                        new MapSqlParameterSource("id", setupSessionId).addValue("media", mediaId), (rs, i) -> mapMedia(rs))
                .stream().findFirst()
                .orElseThrow(() -> new BusinessException(ErrorCode.Setup.SUPPLEMENT_MEDIA_NOT_FOUND));
    }

    private List<MediaRow> uploadedMedia(UUID setupSessionId) {
        return jdbc.query(MEDIA_SELECT + """
                 WHERE setup_session_id = :id AND removed_at IS NULL AND uploaded_at IS NOT NULL
                ORDER BY created_at, media_id
                LIMIT :max
                """, new MapSqlParameterSource("id", setupSessionId).addValue("max", MAX_ITEMS), (rs, i) -> mapMedia(rs));
    }

    private ObjectNode mediaView(UUID setupSessionId, String mediaId) {
        return jdbc.query(MEDIA_SELECT + " WHERE setup_session_id = :id AND media_id = :media",
                        new MapSqlParameterSource("id", setupSessionId).addValue("media", mediaId), (rs, i) -> mediaJson(mapMedia(rs)))
                .stream().findFirst()
                .orElseThrow(() -> new BusinessException(ErrorCode.Setup.SUPPLEMENT_MEDIA_NOT_FOUND));
    }

    private ObjectNode mediaJson(MediaRow m) {
        ObjectNode out = mapper.createObjectNode();
        out.put("media_id", m.mediaId());
        out.put("kind", m.kind());
        putText(out, "source", m.source());
        out.put("content_type", m.contentType());
        putText(out, "file_name", m.fileName());
        out.put("size_bytes", m.sizeBytes());
        putText(out, "sha256", m.sha256());
        if (m.capturedAtMs() == null) {
            out.putNull("captured_at_ms");
        } else {
            out.put("captured_at_ms", m.capturedAtMs());
        }
        if (m.deviceMake() == null && m.deviceModel() == null) {
            out.putNull("device");
        } else {
            ObjectNode device = out.putObject("device");
            if (m.deviceMake() != null) {
                device.put("make", m.deviceMake());
            }
            if (m.deviceModel() != null) {
                device.put("model", m.deviceModel());
            }
        }
        out.put("status", m.uploaded() ? "UPLOADED" : "PENDING_UPLOAD");
        out.put("created_at_ms", m.createdAtMs());
        if (m.uploadedAtMs() == null) {
            out.putNull("uploaded_at_ms");
        } else {
            out.put("uploaded_at_ms", m.uploadedAtMs());
        }
        return out;
    }

    private record RebakeRow(UUID rebakeId, UUID setupSessionId, String status, double progressPct, UUID progressLeaseId,
            JsonNode mediaIds, String baseCalibrationVersion, String basePreviewSceneVersion, String baseSceneVersion,
            Long requestedBy) {

        boolean running() {
            return "QUEUED".equals(status) || "RUNNING".equals(status);
        }
    }

    private RebakeRow findRebakeForUpdate(String jobId) {
        return jdbc.query("""
                SELECT rebake_id, setup_session_id, status, progress_pct, progress_lease_id, media_ids::text AS media_ids,
                       base_calibration_version, base_preview_scene_version, base_scene_version, requested_by
                FROM setup_supplement_rebake WHERE job_id = :job FOR UPDATE
                """, new MapSqlParameterSource("job", jobId), (rs, i) -> new RebakeRow(
                rs.getObject("rebake_id", UUID.class), rs.getObject("setup_session_id", UUID.class), rs.getString("status"),
                rs.getDouble("progress_pct"), rs.getObject("progress_lease_id", UUID.class),
                mapper.readTree(rs.getString("media_ids")), rs.getString("base_calibration_version"),
                rs.getString("base_preview_scene_version"), rs.getString("base_scene_version"),
                rs.getObject("requested_by", Long.class))).stream().findFirst().orElse(null);
    }

    // ---------------------------------------------------------------- 도우미

    /** 표시용 파일 이름. 경로를 떼고 제어 문자를 지우고 255자로 자른다. 비면 null */
    static String displayName(String raw) {
        if (raw == null) {
            return null;
        }
        String name = raw;
        int slash = Math.max(name.lastIndexOf('/'), name.lastIndexOf('\\'));
        if (slash >= 0) {
            name = name.substring(slash + 1);
        }
        name = name.replaceAll("\\p{Cntrl}", "").strip();
        if (name.isEmpty()) {
            return null;
        }
        return name.length() > 255 ? name.substring(0, 255) : name;
    }

    private static String deviceText(String raw) {
        if (raw == null) {
            return null;
        }
        String text = raw.replaceAll("\\p{Cntrl}", "").strip();
        if (text.isEmpty()) {
            return null;
        }
        return text.length() > 80 ? text.substring(0, 80) : text;
    }

    private static String randomHex(int bytes) {
        byte[] b = new byte[bytes];
        RANDOM.nextBytes(b);
        return HexFormat.of().formatHex(b);
    }

    private static void putText(ObjectNode node, String field, String value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private static BusinessException invalidMedia() {
        return new BusinessException(ErrorCode.Setup.INVALID_SUPPLEMENT_MEDIA);
    }

    private static BusinessException invalidProgress() {
        return new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
    }

    /** 시험용: 형식 → 종류 */
    static Optional<String> kindOf(String contentType) {
        String[] type = TYPES.get(contentType == null ? "" : contentType.strip().toLowerCase(Locale.ROOT));
        return type == null ? Optional.empty() : Optional.of(type[0]);
    }
}
