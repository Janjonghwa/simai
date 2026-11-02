package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import java.io.IOException;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관리자의 구역 대상 이벤트 종료를 I1 event_close 1.0.0 으로 Tier 0 에 보낸다(명세 6.4·12.2·12.6, S15P31A101-221).
 * Tier 0 가 끊겨 있으면 event_close 에 저장만 하고, 다시 붙어 동기화가 끝나면 아직 결과가 없거나
 * JOURNAL_UNAVAILABLE 로 일시 거부된 종료를 같은 close_id 로 다시 보낸다(Tier 0 가 close_id 로 중복 제거).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventCloseForwarder {

    static final String EVENT_CLOSE_SCHEMA_VERSION = "1.0.0";
    static final String RESEND_WINDOW = "10 minutes";
    static final String RETRY_AFTER = "5 seconds";
    private static final Logger log = LoggerFactory.getLogger(EventCloseForwarder.class);

    private final I1ConnectionRegistry connections;
    private final NamedParameterJdbcTemplate jdbc;

    public EventCloseForwarder(I1ConnectionRegistry connections, NamedParameterJdbcTemplate jdbc) {
        this.connections = connections;
        this.jdbc = jdbc;
    }

    /** actorId 는 관리자 계정 ID(사람 이름을 넣지 않는다, 명세 11.4-5) */
    public record Close(UUID eventId, UUID closeId, String actorId, long requestedAtMillis, String reason) {
    }

    /** @return 보냈으면 true. 그 Tier 0 연결이 없으면 false(동기화 뒤 다시 보낸다) */
    public boolean forward(String gatewayId, Close close) {
        Optional<I1Connection> connection = connections.all().stream()
                .filter(c -> gatewayId != null && gatewayId.equals(c.gatewayId()) && c.isOpen())
                .findFirst();
        return connection.isPresent() && send(connection.get(), close);
    }

    /** 동기화가 끝난 연결에, 그 Tier 0 의 아직 결과가 없는 종료를 같은 close_id 로 다시 보낸다 */
    public void resendPending(I1Connection connection) {
        resend(connection, "0 seconds");
    }

    /** 열린 연결마다, 보낸 지 RETRY_AFTER 가 지났는데 결과가 없거나 JOURNAL_UNAVAILABLE 인 종료를 다시 보낸다 */
    @Scheduled(fixedDelay = 10_000)
    void retryPending() {
        for (I1Connection connection : connections.all()) {
            if (connection.gatewayId() != null && connection.isOpen()) {
                resend(connection, RETRY_AFTER);
            }
        }
    }

    private void resend(I1Connection connection, String olderThan) {
        var pending = jdbc.query("""
                SELECT c.event_id, c.close_id, c.actor_user_id, c.requested_at, c.reason
                FROM event_close c
                JOIN risk_event e ON e.event_id = c.event_id
                WHERE e.gateway_id = :g AND e.lifecycle <> 'RESOLVED'
                  AND (c.result IS NULL OR (c.result = 'REJECTED' AND c.reject_reason = 'JOURNAL_UNAVAILABLE'))
                  AND c.requested_at > now() - CAST(:window AS interval)
                  AND c.requested_at <= now() - CAST(:olderThan AS interval)
                ORDER BY c.requested_at, c.close_id
                """, new MapSqlParameterSource()
                        .addValue("g", connection.gatewayId())
                        .addValue("window", RESEND_WINDOW)
                        .addValue("olderThan", olderThan),
                (rs, i) -> new Close(
                        UUID.fromString(rs.getString("event_id")),
                        UUID.fromString(rs.getString("close_id")),
                        String.valueOf(rs.getLong("actor_user_id")),
                        rs.getTimestamp("requested_at").getTime(),
                        rs.getString("reason")));
        pending.forEach(close -> send(connection, close));
        if (!pending.isEmpty()) {
            log.info("I1 결과 없는 운영자 종료 다시 보냄: gateway={} count={}", connection.gatewayId(), pending.size());
        }
    }

    private boolean send(I1Connection connection, Close close) {
        ObjectNode payload = connection.newPayload();
        payload.put("event_id", close.eventId().toString());
        ObjectNode operator = payload.putObject("close");
        operator.put("close_id", close.closeId().toString());
        operator.put("channel", "CONTROL_ROOM");
        operator.put("actor_role", "ADMIN");
        operator.put("actor_id", close.actorId());
        operator.put("requested_at", close.requestedAtMillis());
        operator.put("reason", close.reason());
        try {
            connection.send("event_close", EVENT_CLOSE_SCHEMA_VERSION, payload);
            return true;
        } catch (IOException | RuntimeException e) {
            log.warn("event_close 보내기 실패: gateway={} close_id={} error={}",
                    connection.gatewayId(), close.closeId(), e.getClass().getSimpleName());
            return false;
        }
    }
}
