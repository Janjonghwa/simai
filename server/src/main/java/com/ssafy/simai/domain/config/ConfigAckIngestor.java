package com.ssafy.simai.domain.config;

import static com.ssafy.simai.domain.tier0.i1.I1Json.requiredText;
import static com.ssafy.simai.domain.tier0.i1.I1Json.text;
import static com.ssafy.simai.domain.tier0.i1.I1Json.time;
import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;

/**
 * config_ack(Tier 0 → EC2, 명세 3.2, 계약 config_ack) → config_ack 기록(추가만). APPLIED 이고 Tier 0 가 계산한 해시가 발행한
 * bundle_hash 와 같을 때만 묶음을 '현장 적용됨'(config_bundle.applied_at, 처음 받은 적용 시각)으로 표시한다.
 * EC2 가 발행하지 않은 config_version 의 응답은 계약 위반으로 버린다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ConfigAckIngestor implements I1MessageIngestor {

    private static final Set<String> RESULTS = Set.of("APPLIED", "REJECTED");
    private static final Set<String> REJECT_REASONS = Set.of("SCHEMA_INVALID", "HASH_MISMATCH", "REFERENCE_INVALID", "DOWNLOAD_FAILED");
    private static final Logger log = LoggerFactory.getLogger(ConfigAckIngestor.class);

    private final NamedParameterJdbcTemplate jdbc;

    ConfigAckIngestor(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    @Override
    public String msgType() {
        return "config_ack";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    @Transactional
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        JsonNode p = envelope.payload();
        String version = requiredText(p, "config_version");
        String hash = requiredText(p, "bundle_hash");
        String result = requiredText(p, "result");
        if (!RESULTS.contains(result)) {
            throw new InvalidI1Message("result 가 APPLIED·REJECTED 가 아니다");
        }
        String rejectReason = text(p, "reject_reason");
        OffsetDateTime appliedAt = time(p, "applied_at");
        boolean consistent = "APPLIED".equals(result)
                ? appliedAt != null && rejectReason == null
                : appliedAt == null && rejectReason != null && REJECT_REASONS.contains(rejectReason);
        if (!consistent) {
            throw new InvalidI1Message("result 와 applied_at·reject_reason 이 맞지 않는다");
        }
        if (!p.has("active_config_version")) {
            throw new InvalidI1Message("active_config_version 이 없다");
        }
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("msgId", envelope.msgId()).addValue("v", version).addValue("gateway", connection.gatewayId())
                .addValue("result", result).addValue("hash", hash).addValue("appliedAt", appliedAt)
                .addValue("reason", rejectReason).addValue("detail", text(p, "reject_detail"))
                .addValue("active", text(p, "active_config_version")).addValue("receivedAt", utc(receivedAt));
        List<String> issued = jdbc.queryForList("SELECT bundle_hash FROM config_bundle WHERE config_version = :v", params, String.class);
        if (issued.isEmpty()) {
            throw new InvalidI1Message("EC2 가 발행하지 않은 config_version 이다");
        }
        int inserted = jdbc.update("""
                INSERT INTO config_ack (ack_msg_id, config_version, gateway_id, result, bundle_hash, applied_at, reject_reason,
                    reject_detail, active_config_version, received_at)
                VALUES (:msgId, :v, :gateway, :result, :hash, :appliedAt, :reason, :detail, :active, :receivedAt)
                ON CONFLICT (ack_msg_id) DO NOTHING
                """, params);
        if (inserted == 0) {
            return Outcome.DUPLICATE;
        }
        if ("APPLIED".equals(result)) {
            if (hash.equals(issued.getFirst())) {
                jdbc.update("UPDATE config_bundle SET applied_at = :appliedAt WHERE config_version = :v AND applied_at IS NULL", params);
            } else {
                log.warn("config_ack APPLIED 인데 해시가 발행한 것과 다르다: config_version={} gateway={}", version, connection.gatewayId());
            }
        } else {
            log.warn("config_ack REJECTED: config_version={} gateway={} reason={} active={}", version, connection.gatewayId(),
                    rejectReason, text(p, "active_config_version"));
        }
        return Outcome.STORED;
    }
}
