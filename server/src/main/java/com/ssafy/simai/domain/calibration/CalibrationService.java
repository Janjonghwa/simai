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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
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

    public CalibrationService(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper,
            TransactionTemplate transaction, Clock clock) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.clock = clock;
    }

    public ObjectNode upload(String siteId, CalibrationUploadRequest request, long userId) {
        JsonNode doc = request == null ? null : request.document();
        if (doc == null || !doc.isObject()) {
            throw new BusinessException(ErrorCode.Config.INVALID_CALIBRATION);
        }
        byte[] content = mapper.writeValueAsBytes(doc);
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
        // calibration_update 의 calibration_hash 는 문서의 content_hash(버전·시각·상태를 뺀 내용 해시)다. 없으면 원문 해시로 대신한다
        String contentHash = doc.path("content_hash").asString(sha256);
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
                // Tier 0 알림(calibration_update)으로 이미 있는 버전. 같은 보정 내용일 때만 원문을 붙인다
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

    private void insertVersion(String siteId, JsonNode doc, String version, String contentHash, String reason, OffsetDateTime now) {
        String previous = jdbc.queryForList("""
                SELECT calibration_version FROM calibration_version WHERE site_id = :site ORDER BY created_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId), String.class).stream().findFirst().orElse(null);
        List<String> crossCheck = new ArrayList<>();
        doc.path("scale_cross_check_basis").values().forEach(v -> crossCheck.add(v.asString()));
        JsonNode validation = doc.get("validation");
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, previous_calibration_version, state, provider_source,
                    provider_version, reason, effective_at, calibration_ref, calibration_hash, scale_basis, scale_cross_check_basis,
                    validation, created_at)
                VALUES (:v, :site, :previous, :state, :source, :providerVersion, :reason, :now, :ref, :sha, :scaleBasis,
                    CAST(:crossCheck AS text[]), CAST(:validation AS jsonb), :now)
                """, new MapSqlParameterSource()
                .addValue("v", version).addValue("site", siteId).addValue("previous", previous)
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
