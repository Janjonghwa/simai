package com.ssafy.simai.domain.calibration;

import com.ssafy.simai.domain.config.ConfigFiles;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관리자가 올린 보정 문서를 받는다(S15P31A101-120). 구성 묶음에는 보정 문서가 들어가야 하는데(명세 3.2), Tier 0 는 보정을
 * 주소·해시로만 알리고(calibration_update) 문서 본문을 EC2 로 보내는 길이 아직 없다. 그래서 관리자가 Tier 0 의 보정 문서를 올린다.
 * 계약(contracts/providers/calibration)에 맞고 현장이 같아야 받는다. 같은 버전은 같은 문서만 다시 받는다(덮어쓰지 않는다).
 * Tier 0 는 묶음의 보정 버전이 자기 보정 저장소에 있어야 받으므로, Tier 0 가 만든 문서를 그대로 올려야 한다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CalibrationService {

    private static final Logger log = LoggerFactory.getLogger(CalibrationService.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final CalibrationViews views;

    public CalibrationService(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper,
            TransactionTemplate transaction, Clock clock, CalibrationViews views) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.clock = clock;
        this.views = views;
    }

    public ObjectNode upload(String siteId, CalibrationUploadRequest request, long userId) {
        if (request == null || (request.document() == null) == (request.documentText() == null)) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        JsonNode doc;
        byte[] content;
        if (request.documentText() != null) {
            // 글자 그대로 받은 문서는 그대로 저장한다(실수 표기를 바꾸지 않는다)
            try {
                doc = mapper.readTree(request.documentText());
            } catch (JacksonException e) {
                throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
            }
            content = request.documentText().getBytes(StandardCharsets.UTF_8);
        } else {
            doc = request.document();
            content = mapper.writeValueAsBytes(doc);
        }
        if (doc == null || !doc.isObject()) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        List<String> problems = schemas.violations("providers/calibration", new String(content, StandardCharsets.UTF_8));
        if (!problems.isEmpty()) {
            log.info("보정 문서 거부: {}", problems);
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        if (!siteId.equals(doc.path("coordinate_frame").path("site_id").asString(null))) {
            throw new BusinessException(ErrorCode.Config.CALIBRATION_SITE_MISMATCH);
        }
        String version = doc.get("calibration_version").asString();
        String sha256 = ConfigFiles.sha256(content);
        // 버전 행의 해시는 Tier 0 의 calibration_update.calibration_hash 와 같은 content_hash 다. 문서가 적은 값은 믿지 않고 다시 잰다
        String contentHash = CalibrationContentHash.of(doc);
        String declared = doc.path("content_hash").asString(null);
        if (declared != null && !declared.equals(contentHash)) {
            // 문서를 고쳤거나, 숫자 표기가 바뀌었거나(1000.0 → 1000), Python 과 다르게 잰 경우다. 거부하지는 않는다(다시 잰 값이 기준)
            log.warn("보정 문서의 content_hash 가 다시 잰 값과 다르다: calibration_version={} declared={} computed={}",
                    doc.path("calibration_version").asString(null), declared, contentHash);
        }
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        String reason = request.reason() == null || request.reason().isBlank() ? "관리자 업로드" : request.reason().strip();

        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("v", version);
            // 같은 새 버전을 동시에 올려도(두 번 누름) 한 쪽씩 처리한다. 새 버전은 잠글 행이 없어 FOR UPDATE 로는 막지 못한다
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.calibration:' || :v))", params);
            List<String> existing = jdbc.queryForList(
                    "SELECT sha256 FROM calibration_document WHERE calibration_version = :v FOR UPDATE", params, String.class);
            if (!existing.isEmpty()) {
                if (!existing.getFirst().equals(sha256)) {
                    throw new BusinessException(ErrorCode.Config.CALIBRATION_VERSION_CONFLICT);
                }
                return view(version);
            }
            List<String> known = jdbc.queryForList(
                    "SELECT calibration_hash FROM calibration_version WHERE calibration_version = :v", params, String.class);
            if (known.isEmpty()) {
                insertVersion(siteId, doc, version, contentHash, reason, now);
            } else if (!contentHash.equals(known.getFirst())) {
                // Tier 0 알림(calibration_update)으로 이미 있는 버전. 같은 보정 내용(다시 잰 content_hash)일 때만 원문을 붙인다
                throw new BusinessException(ErrorCode.Config.CALIBRATION_VERSION_CONFLICT);
            }
            jdbc.update("""
                    INSERT INTO calibration_document (calibration_version, content, sha256, uploaded_by, uploaded_at)
                    VALUES (:v, :content, :sha, :user, :now)
                    """, new MapSqlParameterSource().addValue("v", version).addValue("content", content)
                    .addValue("sha", sha256).addValue("user", userId).addValue("now", now));
            return view(version);
        });
    }

    /**
     * 현장 세팅 작업(I5 site_setup)이 객체 저장소에 올린 새 보정 문서를 새 calibration_version 으로 등록한다(S15P31A101-467).
     * 관리자 업로드와 같은 검사(계약·현장·content_hash 다시 재기·같은 버전 잠금)를 거치고 덮어쓰지 않는다. 같은 버전·같은 원문이면
     * 그대로 둔다(재전송·재시도). 이전 버전은 Tier 0 가 알린 지금 활성 보정이다(EC2 가 그 버전을 알 때만, 모르면 비움).
     * 호출자 트랜잭션(세팅 하트비트·결과 제출)에 들어간다.
     *
     * @param content 객체 저장소에서 읽은 문서 바이트 그대로(실수 표기를 바꾸지 않는다)
     * @param calibrationRef 그 문서의 s3:// 주소(vlm_zone_draft 등 후속 작업 입력의 calibration_ref)
     * @param declaredPrevious 문서가 적은 이전 보정(계약 2.2.0 previous_calibration_version). 있으면 그것을, 없으면 Tier 0 가 알린
     *     지금 활성 보정을 이전 버전으로 남긴다(EC2 가 그 버전을 알 때만)
     * @return 등록(또는 이미 있던) 보정 버전
     */
    public String registerFromSetup(String siteId, byte[] content, String calibrationRef, String sourceJobId, String reason,
            String declaredPrevious) {
        JsonNode doc;
        try {
            doc = mapper.readTree(content);
        } catch (JacksonException e) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        if (doc == null || !doc.isObject()) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        List<String> problems = schemas.violations("providers/calibration", new String(content, StandardCharsets.UTF_8));
        if (!problems.isEmpty()) {
            log.info("세팅 보정 문서 거부: {}", problems);
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        if (!siteId.equals(doc.path("coordinate_frame").path("site_id").asString(null))) {
            throw new BusinessException(ErrorCode.Config.CALIBRATION_SITE_MISMATCH);
        }
        String version = doc.get("calibration_version").asString();
        String sha256 = ConfigFiles.sha256(content);
        String contentHash = CalibrationContentHash.of(doc);
        String declared = doc.path("content_hash").asString(null);
        if (declared != null && !declared.equals(contentHash)) {
            log.warn("세팅 보정 문서의 content_hash 가 다시 잰 값과 다르다: calibration_version={} declared={} computed={}",
                    version, declared, contentHash);
        }
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        String previous = declaredPrevious != null ? declaredPrevious
                : views.currentVersion().filter(v -> !v.equals(version)).orElse(null);
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("v", version);
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.calibration:' || :v))", params);
            List<String> existing = jdbc.queryForList(
                    "SELECT sha256 FROM calibration_document WHERE calibration_version = :v FOR UPDATE", params, String.class);
            if (!existing.isEmpty()) {
                if (existing.getFirst().equals(sha256)) {
                    return version;
                }
                // 같은 보정 내용(content_hash)인데 계보·출처 키(setup_session_id 등, 해시 제외)만 다르면 재시도 세션이 같은 입력으로 다시 만든
                // 같은 보정이다. 원문은 처음 것을 그대로 둔다(덮어쓰지 않음)
                List<String> knownHash = jdbc.queryForList(
                        "SELECT calibration_hash FROM calibration_version WHERE calibration_version = :v AND site_id = :site",
                        new MapSqlParameterSource("v", version).addValue("site", siteId), String.class);
                if (!knownHash.isEmpty() && contentHash.equals(knownHash.getFirst())) {
                    return version;
                }
                throw new BusinessException(ErrorCode.Config.CALIBRATION_VERSION_CONFLICT);
            }
            List<Map<String, Object>> known = jdbc.queryForList(
                    "SELECT site_id, calibration_hash FROM calibration_version WHERE calibration_version = :v", params);
            if (known.isEmpty()) {
                JsonNode validation = doc.get("validation");
                List<String> crossCheck = new ArrayList<>();
                doc.path("scale_cross_check_basis").values().forEach(v -> crossCheck.add(v.asString()));
                jdbc.update("""
                        INSERT INTO calibration_version (calibration_version, site_id, previous_calibration_version, state,
                            provider_source, provider_version, reason, effective_at, calibration_ref, calibration_hash, scale_basis,
                            scale_cross_check_basis, validation, source_job_id, created_at)
                        VALUES (:v, :site, (SELECT calibration_version FROM calibration_version WHERE calibration_version = :previous),
                            :state, :source, :providerVersion, :reason, :now, :ref, :sha, :scaleBasis,
                            CAST(:crossCheck AS text[]), CAST(:validation AS jsonb), :job, :now)
                        """, new MapSqlParameterSource()
                        .addValue("v", version).addValue("site", siteId).addValue("previous", previous)
                        .addValue("state", doc.get("state").asString()).addValue("source", doc.get("provider_source").asString())
                        .addValue("providerVersion", doc.path("provider_version").asString(null)).addValue("reason", reason)
                        .addValue("now", now).addValue("ref", calibrationRef).addValue("sha", contentHash)
                        .addValue("scaleBasis", doc.path("scale_basis").asString(null))
                        .addValue("crossCheck", crossCheck.isEmpty() ? null : I1Json.arrayLiteral(crossCheck))
                        .addValue("validation", validation == null || validation.isNull() ? null
                                : mapper.writeValueAsString(validation))
                        .addValue("job", sourceJobId));
            } else if (!siteId.equals(known.getFirst().get("site_id"))
                    || !contentHash.equals(known.getFirst().get("calibration_hash"))) {
                // 다른 현장이거나, 같은 버전 ID 에 다른 보정 내용. 덮어쓰지 않는다(4.5)
                throw new BusinessException(ErrorCode.Config.CALIBRATION_VERSION_CONFLICT);
            }
            jdbc.update("""
                    INSERT INTO calibration_document (calibration_version, content, sha256, uploaded_by, uploaded_at)
                    VALUES (:v, :content, :sha, NULL, :now)
                    """, new MapSqlParameterSource().addValue("v", version).addValue("content", content)
                    .addValue("sha", sha256).addValue("now", now));
            return version;
        });
    }

    /**
     * 이전 맵 재사용(S15P31A101-714): 고른 맵의 보정 {@code sourceVersion} 을 내용 그대로 새 버전 {@code newVersion} 으로 낸다. Tier 0 는 구성 묶음으로
     * 받은 저장소에 없는 보정을 세팅 보정으로 채택할 때 "이전 버전 = 지금 활성 보정"(계보)을 보므로, 다른 보정을 쓰고 있는 Tier 0 가 그 맵의 보정으로
     * 돌아가려면 계보 키만 바꾼 새 버전이 필요하다(보정은 덮어쓰지 않고 새 버전을 더한다, 규칙 6).
     * <p>바꾸는 키는 content_hash 에 넣지 않는 계보·출처 키뿐이다: {@code calibration_version}·{@code previous_calibration_version}(모르면 뺀다)·
     * {@code setup_session_id}·{@code created_at}. 다시 잰 content_hash 가 원래 보정과 다르면(내용이 바뀌었으면) 내지 않는다.
     *
     * @return 등록한 새 버전
     */
    public String reissueForReuse(String siteId, String sourceVersion, String newVersion, String previousVersion,
            String setupSessionId, long createdAtMs) {
        List<byte[]> docs = jdbc.queryForList("""
                SELECT d.content FROM calibration_document d JOIN calibration_version v ON v.calibration_version = d.calibration_version
                WHERE d.calibration_version = :v AND v.site_id = :site
                """, new MapSqlParameterSource("v", sourceVersion).addValue("site", siteId), byte[].class);
        if (docs.isEmpty()) {
            throw new BusinessException(ErrorCode.Config.CALIBRATION_MISSING);
        }
        JsonNode source;
        try {
            source = mapper.readTree(docs.getFirst());
        } catch (JacksonException e) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        if (!(source instanceof ObjectNode sourceDoc)) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        ObjectNode doc = sourceDoc.deepCopy();
        doc.put("calibration_version", newVersion);
        if (previousVersion == null) {
            doc.remove("previous_calibration_version");
        } else {
            doc.put("previous_calibration_version", previousVersion);
        }
        doc.put("setup_session_id", setupSessionId);
        doc.put("created_at", createdAtMs);
        if (!CalibrationContentHash.of(sourceDoc).equals(CalibrationContentHash.of(doc))) {
            throw new IllegalStateException("재사용 보정 " + sourceVersion + " 의 내용이 새 버전에서 달라졌다");
        }
        return registerFromSetup(siteId, mapper.writeValueAsBytes(doc), "ec2:calibration_document/" + newVersion, null,
                "이전 맵 재사용 " + setupSessionId + ": " + sourceVersion + " 을 내용 그대로 다시 냄", previousVersion);
    }

    /** 두 보정이 같은 내용(content_hash)인지. EC2 가 두 원문(calibration_document)에서 다시 잰 해시로만 본다. 모르면 false */
    public boolean sameContent(String a, String b) {
        if (a == null || b == null) {
            return false;
        }
        if (a.equals(b)) {
            return true;
        }
        List<String> hashes = jdbc.queryForList("""
                SELECT v.calibration_hash FROM calibration_version v
                WHERE v.calibration_version IN (:a, :b) AND v.calibration_hash IS NOT NULL
                  AND EXISTS (SELECT 1 FROM calibration_document d WHERE d.calibration_version = v.calibration_version)
                """, new MapSqlParameterSource("a", a).addValue("b", b), String.class);
        return hashes.size() == 2 && hashes.get(0).equals(hashes.get(1));
    }

    /** 문서에는 이전 버전이 없어 비워 둔다(추측하지 않는다). 이 버전의 calibration_update 가 오면 Tier 0 값으로 채운다 */
    private void insertVersion(String siteId, JsonNode doc, String version, String contentHash, String reason, OffsetDateTime now) {
        List<String> crossCheck = new ArrayList<>();
        doc.path("scale_cross_check_basis").values().forEach(v -> crossCheck.add(v.asString()));
        JsonNode validation = doc.get("validation");
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, previous_calibration_version, state, provider_source,
                    provider_version, reason, effective_at, calibration_ref, calibration_hash, scale_basis, scale_cross_check_basis,
                    validation, created_at)
                VALUES (:v, :site, NULL, :state, :source, :providerVersion, :reason, :now, :ref, :sha, :scaleBasis,
                    CAST(:crossCheck AS text[]), CAST(:validation AS jsonb), :now)
                """, new MapSqlParameterSource()
                .addValue("v", version).addValue("site", siteId)
                .addValue("state", doc.get("state").asString()).addValue("source", doc.get("provider_source").asString())
                .addValue("providerVersion", doc.path("provider_version").asString(null)).addValue("reason", reason)
                .addValue("now", now).addValue("ref", "ec2:calibration_document/" + version).addValue("sha", contentHash)
                .addValue("scaleBasis", doc.path("scale_basis").asString(null))
                .addValue("crossCheck", crossCheck.isEmpty() ? null : I1Json.arrayLiteral(crossCheck))
                .addValue("validation", validation == null || validation.isNull() ? null : mapper.writeValueAsString(validation)));
    }

    private ObjectNode view(String version) {
        return jdbc.queryForObject("""
                SELECT d.calibration_version, d.sha256, d.uploaded_at, v.state, v.provider_source
                FROM calibration_document d JOIN calibration_version v ON v.calibration_version = d.calibration_version
                WHERE d.calibration_version = :v
                """, new MapSqlParameterSource("v", version), (rs, i) -> {
            ObjectNode node = mapper.createObjectNode();
            node.put("calibration_version", rs.getString("calibration_version"));
            node.put("sha256", rs.getString("sha256"));
            node.put("state", rs.getString("state"));
            node.put("provider_source", rs.getString("provider_source"));
            node.put("uploaded_at", rs.getObject("uploaded_at", OffsetDateTime.class).toInstant().toEpochMilli());
            return node;
        });
    }
}
