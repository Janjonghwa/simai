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
import org.springframework.stereotype.Component;
import tools.jackson.databind.node.ObjectNode;

/**
 * 운영자 확인을 I1 event_ack 로 Tier 0 에 보낸다(EC2 → Tier 0 → 노트북 alarm_stop, 명세 3.2·3.10, 계약 event_ack).
 * Tier 0 는 결과를 돌려주지 않는다. 확인이 반영되면 ACKNOWLEDGED 전이(같은 ack_id)가 저널로 돌아오고 event_ack.journal_event_sequence 가 찬다.
 * Tier 0 가 끊겨 있으면 저장만 하고, 다시 붙어 동기화가 끝나면 아직 저널에 잡히지 않은 확인을 같은 ack_id 로 다시 보낸다
 * (Tier 0 는 ack_id 로 중복을 거른다). 연결이 열려 있는데 보내기에 실패한 확인도 10초마다 다시 보낸다(RETRY_AFTER 지난 것).
 * 오래된 확인(RESEND_WINDOW 밖)은 다시 보내지 않는다. 그사이 바뀐 경보를 잘못 멈추지 않게.
 * 이벤트의 gateway_id 를 모르면(받은 전이에 없었음) 보낼 Tier 0 를 정할 수 없어 저장만 한다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventAckForwarder {

    static final String EVENT_ACK_SCHEMA_VERSION = "2.1.0";
    static final String RESEND_WINDOW = "10 minutes";
    static final String RETRY_AFTER = "5 seconds";
    private static final Logger log = LoggerFactory.getLogger(EventAckForwarder.class);

    private final I1ConnectionRegistry connections;
    private final NamedParameterJdbcTemplate jdbc;

    public EventAckForwarder(I1ConnectionRegistry connections, NamedParameterJdbcTemplate jdbc) {
        this.connections = connections;
        this.jdbc = jdbc;
    }

    public record Ack(UUID eventId, Integer target, UUID ackId, String channel, String actorRole, String actorId, long ackedAtMillis) {
    }

    /** @return 보냈으면 true. 그 Tier 0 연결이 없으면 false(동기화 뒤 다시 보낸다) */
    public boolean forward(String gatewayId, Ack ack) {
        Optional<I1Connection> connection = connections.all().stream()
                .filter(c -> gatewayId != null && gatewayId.equals(c.gatewayId()) && c.isOpen())
                .findFirst();
        return connection.isPresent() && send(connection.get(), ack);
    }

    /** 동기화가 끝난 연결에, 그 Tier 0 의 아직 저널에 잡히지 않은 EC2 확인을 다시 보낸다 */
    public void resendPending(I1Connection connection) {
        resend(connection, "0 seconds");
    }

    /** 열린 연결마다, 보낸 지 RETRY_AFTER 가 지났는데 아직 저널에 잡히지 않은 확인을 다시 보낸다 */
    @org.springframework.scheduling.annotation.Scheduled(fixedDelay = 10_000)
    void retryUnjournaled() {
        for (I1Connection connection : connections.all()) {
            if (connection.gatewayId() != null && connection.isOpen()) {
                resend(connection, RETRY_AFTER);
            }
        }
    }

    private void resend(I1Connection connection, String olderThan) {
        var pending = jdbc.query("""
                SELECT a.event_id, a.target_event_sequence, a.ack_id, a.channel, a.actor_role, a.actor_user_id, a.acked_at
                FROM event_ack a JOIN risk_event e ON e.event_id = a.event_id
                WHERE e.gateway_id = :g AND e.lifecycle <> 'RESOLVED' AND a.journal_event_sequence IS NULL
                  AND a.channel IN ('CONTROL_ROOM', 'FOREMAN_PWA')
                  AND a.acked_at > now() - CAST(:window AS interval)
                  AND a.acked_at <= now() - CAST(:olderThan AS interval)
                ORDER BY a.acked_at
                """, new MapSqlParameterSource().addValue("g", connection.gatewayId()).addValue("window", RESEND_WINDOW)
                        .addValue("olderThan", olderThan),
                (rs, i) -> {
                    // getInt 뒤 wasNull 은 바로 앞에 읽은 열 기준이라, 다른 열을 읽기 전에 NULL 을 그대로 받는다
                    Integer target = rs.getObject("target_event_sequence", Integer.class);
                    return new Ack(UUID.fromString(rs.getString("event_id")), target,
                            UUID.fromString(rs.getString("ack_id")), rs.getString("channel"), rs.getString("actor_role"),
                            String.valueOf(rs.getLong("actor_user_id")), rs.getTimestamp("acked_at").getTime());
                });
        pending.forEach(ack -> send(connection, ack));
        if (!pending.isEmpty()) {
            log.info("I1 미반영 확인 다시 보냄: gateway={} count={}", connection.gatewayId(), pending.size());
        }
    }

    private boolean send(I1Connection connection, Ack ack) {
        ObjectNode payload = connection.newPayload();
        payload.put("event_id", ack.eventId().toString());
        if (ack.target() != null) {
            payload.put("event_sequence", ack.target());
        }
        ObjectNode operator = payload.putObject("ack");
        operator.put("ack_id", ack.ackId().toString());
        operator.put("channel", ack.channel());
        operator.put("actor_role", ack.actorRole());
        operator.put("actor_id", ack.actorId());
        operator.put("acked_at", ack.ackedAtMillis());
        try {
            connection.send("event_ack", EVENT_ACK_SCHEMA_VERSION, payload);
            return true;
        } catch (IOException | RuntimeException e) {
            // 보내기 한도 초과(SessionLimitExceededException) 등도 잡는다. 저장은 끝났고 주기 작업이 다시 보낸다
            log.warn("event_ack 보내기 실패: gateway={} ack_id={} error={}", connection.gatewayId(), ack.ackId(),
                    e.getClass().getSimpleName());
            return false;
        }
    }
}
