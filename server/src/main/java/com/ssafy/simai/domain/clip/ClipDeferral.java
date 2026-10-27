package com.ssafy.simai.domain.clip;

import static com.ssafy.simai.domain.tier0.i1.I1Json.utc;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;

/**
 * 앞선 메시지를 기다리는 클립 메시지 보관(i1_deferred_message, V11). 클립 기록·보존 기록은 저널 메시지가 아니라 Tier 0 가 다시 보내 주지
 * 않으므로, 이벤트 전이·클립 기록보다 먼저 오면 버리지 않고 받은 봉투 그대로 두었다가 ClipDeferredRetrier 가 다시 처리한다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ClipDeferral {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    ClipDeferral(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 처리할 수 있으면 하고, 앞선 메시지가 없으면 보관한다. 이미 보관한 같은 메시지면 DUPLICATE */
    I1MessageIngestor.Outcome applyOrPark(Deferrable ingestor, I1Envelope envelope, Instant receivedAt) {
        try {
            return ingestor.apply(envelope, receivedAt);
        } catch (WaitingForEarlier waiting) {
            return park(envelope, receivedAt, waiting.getMessage())
                    ? I1MessageIngestor.Outcome.STORED : I1MessageIngestor.Outcome.DUPLICATE;
        }
    }

    boolean park(I1Envelope envelope, Instant receivedAt, String reason) {
        return jdbc.update("""
                INSERT INTO i1_deferred_message (msg_id, msg_type, envelope, reason, received_at, next_attempt_at)
                VALUES (:msgId, :msgType, CAST(:envelope AS jsonb), :reason, :receivedAt, :receivedAt)
                ON CONFLICT (msg_id) DO NOTHING
                """, new MapSqlParameterSource().addValue("msgId", envelope.msgId()).addValue("msgType", envelope.msgType())
                .addValue("envelope", mapper.writeValueAsString(envelope.toContractJson(mapper)))
                .addValue("reason", reason).addValue("receivedAt", utc(receivedAt))) > 0;
    }

    List<Parked> due(Instant now, int limit) {
        return jdbc.query("""
                SELECT msg_id, msg_type, envelope::text AS envelope, received_at, attempts FROM i1_deferred_message
                WHERE next_attempt_at <= :now ORDER BY received_at LIMIT :limit
                """, new MapSqlParameterSource().addValue("now", utc(now)).addValue("limit", limit),
                (rs, i) -> new Parked(rs.getObject("msg_id", UUID.class), rs.getString("msg_type"),
                        I1Envelope.parse(mapper.readTree(rs.getString("envelope"))),
                        rs.getObject("received_at", OffsetDateTime.class).toInstant(), rs.getInt("attempts")));
    }

    void remove(UUID msgId) {
        jdbc.update("DELETE FROM i1_deferred_message WHERE msg_id = :m", new MapSqlParameterSource("m", msgId));
    }

    void retryLater(UUID msgId, String reason, Instant next) {
        jdbc.update("""
                UPDATE i1_deferred_message SET attempts = attempts + 1, reason = :reason, next_attempt_at = :next WHERE msg_id = :m
                """, new MapSqlParameterSource().addValue("m", msgId).addValue("reason", reason).addValue("next", utc(next)));
    }

    record Parked(UUID msgId, String msgType, I1Envelope envelope, Instant receivedAt, int attempts) {

        /** 다시 시도할 때까지 기다리는 시간: 1초부터 두 배씩, 최대 1분 */
        Duration backoff() {
            return Duration.ofSeconds(Math.min(60, 1L << Math.min(attempts, 6)));
        }
    }

    /** 앞선 메시지가 와야 처리할 수 있는 클립 수신기. apply 의 receivedAt 은 처음 받은 시각이다 */
    interface Deferrable extends I1MessageIngestor {

        Outcome apply(I1Envelope envelope, Instant receivedAt);
    }

    /** 앞선 메시지(이벤트 전이·클립 기록)를 아직 받지 못했다. 트랜잭션 안에서 던져 아무것도 쓰지 않고 되돌린다 */
    static final class WaitingForEarlier extends RuntimeException {

        WaitingForEarlier(String reason) {
            super(reason, null, false, false);
        }
    }
}
