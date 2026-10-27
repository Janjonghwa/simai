package com.ssafy.simai.domain.calibration;

import static com.ssafy.simai.domain.tier0.i1.I1Json.json;
import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * calibration_update(Tier 0 → EC2, I1, 명세 12.2·4.4·4.5) → calibration_update_message 기록 + calibration_version 행.
 * 처음 보는 버전이면 버전 행을 만들고, 이미 있으면 같은 보정 내용(calibration_hash)일 때만 그 버전의 가장 늦은 알림으로 상태·이유·
 * 운영 중 재투영 오차를 바꾼다(DRIFTED 표시는 새 버전을 만들지 않는다). 버전 행에 쓰므로 계약 스키마 전체로 검사한다.
 * 관리자 업로드(CalibrationService)와 같은 버전 잠금을 써서 둘이 동시에 같은 버전을 만들지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CalibrationUpdateIngestor implements I1MessageIngestor {

    static final String MSG_TYPE = "calibration_update";
    private static final String SCHEMA = "messages/calibration_update";

    private static final String INSERT_VERSION = """
            INSERT INTO calibration_version (calibration_version, site_id, previous_calibration_version, state, provider_source,
                reason, effective_at, calibration_ref, calibration_hash, operating_reprojection, created_at)
            VALUES (:v, :site, (SELECT calibration_version FROM calibration_version WHERE calibration_version = :previous),
                :state, :source, :reason, :effectiveAt, :ref, :hash, CAST(:cameras AS jsonb), :receivedAt)
            """;

    // previous 는 이 버전이 켜지기 전 버전이라 바뀌지 않는다. 처음에 몰랐던(아직 받지 못한) 이전 버전만 채운다
    private static final String UPDATE_VERSION = """
            UPDATE calibration_version SET state = :state, reason = :reason, effective_at = :effectiveAt, calibration_ref = :ref,
                operating_reprojection = CAST(:cameras AS jsonb),
                previous_calibration_version = COALESCE(previous_calibration_version,
                    (SELECT calibration_version FROM calibration_version WHERE calibration_version = :previous))
            WHERE calibration_version = :v
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final SiteLookup sites;

    CalibrationUpdateIngestor(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper,
            TransactionTemplate transaction, SiteLookup sites) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.sites = sites;
    }

    @Override
    public String msgType() {
        return MSG_TYPE;
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(2);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        List<String> problems = schemas.violations(SCHEMA, mapper.writeValueAsString(message(envelope)));
        if (!problems.isEmpty()) {
            // 검사 문구는 경로와 규칙만 담는다(값 없음)
            throw new InvalidI1Message("calibration_update 가 계약에 맞지 않는다: " + problems);
        }
        JsonNode p = envelope.payload();
        String siteId = sites.siteId();
        return transaction.execute(status -> {
            MapSqlParameterSource params = new MapSqlParameterSource("v", p.get("calibration_version").asString())
                    .addValue("msgId", envelope.msgId()).addValue("sentAt", utc(envelope.sentAt()));
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.calibration:' || :v))", params);
            if (!jdbc.queryForList("SELECT 1 FROM calibration_update_message WHERE msg_id = :msgId", params).isEmpty()) {
                return Outcome.DUPLICATE;
            }
            List<String> known = jdbc.queryForList(
                    "SELECT calibration_hash FROM calibration_version WHERE calibration_version = :v FOR UPDATE", params, String.class);
            if (known.isEmpty()) {
                write(INSERT_VERSION, siteId, p, receivedAt);
            } else if (!known.getFirst().equals(p.get("calibration_hash").asString())) {
                // 같은 버전 ID 에 다른 보정 내용. 버전은 덮어쓰지 않는다(4.5)
                throw new InvalidI1Message("같은 calibration_version 에 다른 calibration_hash");
            } else if (jdbc.queryForList("""
                    SELECT 1 FROM calibration_update_message WHERE calibration_version = :v AND sent_at > :sentAt LIMIT 1
                    """, params).isEmpty()) {
                // 늦게 도착한 옛 알림(재전송)은 기록만 하고 상태를 되돌리지 않는다
                write(UPDATE_VERSION, siteId, p, receivedAt);
            }
            jdbc.update("""
                    INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                        sent_at, received_at, payload)
                    VALUES (:msgId, :gateway, :v, :state, :reason, :sentAt, :receivedAt, CAST(:payload AS jsonb))
                    """, params.addValue("gateway", envelope.sourceId()).addValue("state", p.get("calibration_state").asString())
                    .addValue("reason", p.get("reason").asString()).addValue("receivedAt", utc(receivedAt))
                    .addValue("payload", mapper.writeValueAsString(p)));
            return Outcome.STORED;
        });
    }

    /** 이전 버전을 EC2 가 모르면(그 알림을 받기 전) 참조할 수 없어 비워 둔다. 원문은 calibration_update_message 에 남는다 */
    private void write(String sql, String siteId, JsonNode p, Instant receivedAt) {
        jdbc.update(sql, new MapSqlParameterSource()
                .addValue("v", p.get("calibration_version").asString()).addValue("site", siteId)
                .addValue("previous", I1Json.text(p, "previous_calibration_version"))
                .addValue("state", p.get("calibration_state").asString()).addValue("source", p.get("provider_source").asString())
                .addValue("reason", p.get("reason").asString()).addValue("effectiveAt", I1Json.requiredTime(p, "effective_at"))
                .addValue("ref", p.get("calibration_ref").asString()).addValue("hash", p.get("calibration_hash").asString())
                .addValue("cameras", json(p, "cameras")).addValue("receivedAt", utc(receivedAt)));
    }

    /** 받은 봉투를 계약 모양으로 다시 쓴다. 봉투의 모르는 필드는 I1Envelope 가 이미 버렸다(계약상 무시) */
    private ObjectNode message(I1Envelope envelope) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", envelope.schemaVersion());
        root.put("msg_type", envelope.msgType());
        root.put("msg_id", envelope.msgId().toString());
        root.put("sent_at", envelope.sentAt().toEpochMilli());
        root.put("source_id", envelope.sourceId());
        root.set("payload", envelope.payload());
        return root;
    }
}
