package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.tier0.event.EventCloseForwarder;
import com.ssafy.simai.domain.tier0.event.EventCloseWaiters;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관리자의 구역 대상 이벤트 운영자 종료(명세 6.4·12.6 POST /api/events/{id}/close, FR-78, S15P31A101-221).
 * <ul>
 *   <li>REST 멱등 키는 event_close.idempotency_key(유일)에 두고, close_id 는 EC2 가 따로 발급한다(16장 "운영자 종료 멱등 키").
 *       같은 키로 다시 오면 새 행·새 I1 없이 그 행의 지금 결과를 돌려준다.
 *   <li>커밋 뒤 I1 event_close 를 보내고 event_close_result 를 잠깐 기다린다. 오면 결과(거부 사유 포함)를 200 으로,
 *       Tier 0 가 끊겼거나 시간 안에 오지 않으면 result null 로 202 를 돌려준다(EventCloseForwarder 가 다시 보낸다).
 *   <li>EC2 는 risk_event 를 바꾸지 않는다(AGENTS.md 4). 종료는 Tier 0 저널의 RESOLVED 전이로 반영된다.
 *       사람 대상·HIGH·CRITICAL·지금 조건 성립 여부는 Tier 0 가 판단한다(EC2 는 마지막 판정을 모른다).
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventCloseService {

    static final int MAX_REASON = 500;
    static final int MAX_IDEMPOTENCY_KEY = 128;
    private static final Logger log = LoggerFactory.getLogger(EventCloseService.class);

    private final EventCloseForwarder forwarder;
    private final EventCloseWaiters waiters;
    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final Duration resultWait;

    public EventCloseService(EventCloseForwarder forwarder, EventCloseWaiters waiters, NamedParameterJdbcTemplate jdbc,
            TransactionTemplate transaction, Clock clock,
            @Value("${simai.event-close.result-wait-ms:3000}") long resultWaitMs) {
        this.forwarder = forwarder;
        this.waiters = waiters;
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.clock = clock;
        this.resultWait = Duration.ofMillis(resultWaitMs);
    }

    /** decided 가 false 면 Tier 0 결과를 아직 받지 못했다(202) */
    public record CloseResponse(boolean decided, ObjectNode body) {
    }

    public CloseResponse close(UUID eventId, String rawReason, String rawIdempotencyKey, long userId) {
        String reason = rawReason == null ? "" : rawReason.strip();
        if (!fits(reason, MAX_REASON)) {
            throw new BusinessException(ErrorCode.Event.INVALID_CLOSE_REASON);
        }
        String idempotencyKey = rawIdempotencyKey == null ? "" : rawIdempotencyKey.strip();
        if (!fits(idempotencyKey, MAX_IDEMPOTENCY_KEY)) {
            throw new BusinessException(ErrorCode.Event.INVALID_IDEMPOTENCY_KEY);
        }
        Instant now = clock.instant();

        Created created = transaction.execute(status -> {
            Optional<CloseRow> existing = findByKey(idempotencyKey);
            if (existing.isPresent()) {
                return new Created(replay(existing.get(), eventId, userId), null);
            }
            List<LockedEvent> locked = jdbc.query("""
                    SELECT lifecycle, gateway_id FROM risk_event
                    WHERE event_id = :eventId AND source = 'LIVE' FOR UPDATE
                    """, new MapSqlParameterSource("eventId", eventId),
                    (rs, i) -> new LockedEvent(rs.getString("lifecycle"), rs.getString("gateway_id")));
            if (locked.isEmpty()) {
                throw new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND);
            }
            // 잠금을 기다리는 사이 같은 키의 요청이 먼저 커밋했으면 그 결과를 돌려준다(그사이 닫혔어도 409 가 아니라)
            Optional<CloseRow> raced = findByKey(idempotencyKey);
            if (raced.isPresent()) {
                return new Created(replay(raced.get(), eventId, userId), null);
            }
            if ("RESOLVED".equals(locked.getFirst().lifecycle())) {
                throw new BusinessException(ErrorCode.Event.EVENT_RESOLVED);
            }
            String gatewayId = locked.getFirst().gatewayId();
            if (gatewayId == null) {
                // 보낼 Tier 0 를 모르면 다시 보내기로도 닿지 않아 결과 없이 영영 남는다
                throw new BusinessException(ErrorCode.Event.CLOSE_NO_GATEWAY);
            }
            UUID closeId = UUID.randomUUID();
            int inserted = jdbc.update("""
                    INSERT INTO event_close (close_id, idempotency_key, event_id, actor_user_id, requested_at, reason)
                    VALUES (:closeId, :key, :eventId, :userId, :requestedAt, :reason)
                    ON CONFLICT (idempotency_key) DO NOTHING
                    """, new MapSqlParameterSource()
                    .addValue("closeId", closeId)
                    .addValue("key", idempotencyKey)
                    .addValue("eventId", eventId)
                    .addValue("userId", userId)
                    .addValue("requestedAt", OffsetDateTime.ofInstant(now, ZoneOffset.UTC))
                    .addValue("reason", reason));
            if (inserted == 0) {
                // 같은 키의 동시 요청이 먼저 넣었다. 그 행을 돌려준다
                return new Created(replay(findByKey(idempotencyKey).orElseThrow(), eventId, userId), null);
            }
            return new Created(closeId, gatewayId);
        });

        UUID closeId = created.closeId();
        if (created.gatewayId() != null) {
            waiters.register(closeId);
            try {
                boolean sent = forwarder.forward(created.gatewayId(),
                        new EventCloseForwarder.Close(eventId, closeId, String.valueOf(userId), now.toEpochMilli(), reason));
                if (!sent) {
                    log.info("Tier 0 로 바로 보내지 못해 저장만 함(다시 보냄): event_id={} close_id={}", eventId, closeId);
                } else if (!waiters.await(closeId, resultWait)) {
                    log.info("event_close_result 를 {}ms 안에 받지 못함(다시 보냄): event_id={} close_id={}",
                            resultWait.toMillis(), eventId, closeId);
                }
            } finally {
                waiters.cancel(closeId);
            }
        }

        CloseRow row = findById(closeId).orElseThrow(() -> new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND));
        // JOURNAL_UNAVAILABLE 은 일시 거부라 아직 결정되지 않은 것으로 본다(다시 보내 결과가 바뀐다)
        boolean decided = row.result() != null
                && !("REJECTED".equals(row.result()) && "JOURNAL_UNAVAILABLE".equals(row.rejectReason()));
        return new CloseResponse(decided, body(row));
    }

    /** 앞뒤 공백을 뺀 값이 1~max 글자(계약 maxLength 처럼 코드 포인트로 센다). PostgreSQL text 가 받지 않는 NUL 은 거부한다 */
    private static boolean fits(String value, int max) {
        return !value.isEmpty() && value.indexOf('\u0000') < 0 && value.codePointCount(0, value.length()) <= max;
    }

    /** 같은 키는 같은 사람이 같은 이벤트에 보낸 것이어야 한다. 다른 요청이 키를 잘못 다시 쓴 것을 처음 결과로 덮지 않는다 */
    private static UUID replay(CloseRow row, UUID eventId, long userId) {
        if (!row.eventId().equals(eventId) || row.actorUserId() != userId) {
            throw new BusinessException(ErrorCode.Event.IDEMPOTENCY_KEY_REUSED);
        }
        return row.closeId();
    }

    private Optional<CloseRow> findByKey(String key) {
        return find("idempotency_key = :v", key);
    }

    private Optional<CloseRow> findById(UUID closeId) {
        return find("close_id = :v", closeId);
    }

    private Optional<CloseRow> find(String where, Object value) {
        return jdbc.query("""
                SELECT close_id, event_id, actor_user_id, requested_at, reason,
                       result, reject_reason, event_sequence, decided_at
                FROM event_close WHERE %s
                """.formatted(where), new MapSqlParameterSource("v", value), (rs, i) -> new CloseRow(
                UUID.fromString(rs.getString("close_id")),
                UUID.fromString(rs.getString("event_id")),
                rs.getLong("actor_user_id"),
                rs.getTimestamp("requested_at").toInstant(),
                rs.getString("reason"),
                rs.getString("result"),
                rs.getString("reject_reason"),
                rs.getObject("event_sequence", Integer.class),
                rs.getTimestamp("decided_at") == null ? null : rs.getTimestamp("decided_at").toInstant()))
                .stream().findFirst();
    }

    /** 계약 event_close_result payload 와 같은 snake_case 칸에 요청 쪽(사유·요청 시각)을 더한다. 시각은 epoch ms */
    private static ObjectNode body(CloseRow row) {
        ObjectNode body = JsonNodeFactory.instance.objectNode();
        body.put("close_id", row.closeId().toString());
        body.put("event_id", row.eventId().toString());
        body.put("reason", row.reason());
        body.put("requested_at", row.requestedAt().toEpochMilli());
        if (row.result() == null) {
            body.putNull("result");
        } else {
            body.put("result", row.result());
        }
        if (row.rejectReason() == null) {
            body.putNull("reject_reason");
        } else {
            body.put("reject_reason", row.rejectReason());
        }
        if (row.eventSequence() == null) {
            body.putNull("event_sequence");
        } else {
            body.put("event_sequence", row.eventSequence());
        }
        if (row.decidedAt() == null) {
            body.putNull("decided_at");
        } else {
            body.put("decided_at", row.decidedAt().toEpochMilli());
        }
        return body;
    }

    private record LockedEvent(String lifecycle, String gatewayId) {
    }

    /** gatewayId 가 null 이면 이미 있던 요청(보내지 않는다) */
    private record Created(UUID closeId, String gatewayId) {
    }

    private record CloseRow(UUID closeId, UUID eventId, long actorUserId, Instant requestedAt, String reason,
            String result, String rejectReason, Integer eventSequence, Instant decidedAt) {
    }
}
