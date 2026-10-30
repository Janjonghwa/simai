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
 * 관리자가 승인한 VLM 상향 의견을 I1 event_escalate 1.0.0 으로 Tier 0 에 보낸다(명세 5.4·12.3·12.6, S15P31A101-276).
 * Tier 0 가 끊겨 있으면 event_escalation 에 저장만 하고, 다시 붙어 동기화가 끝나면 아직 응답·저널 반영이 없거나
 * JOURNAL_UNAVAILABLE 로 일시 거부된 상향을 같은 escalation_id 로 다시 보낸다(Tier 0 가 escalation_id 로 중복 제거).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventEscalationForwarder {

    static final String EVENT_ESCALATE_SCHEMA_VERSION = "1.0.0";
    static final String RESEND_WINDOW = "10 minutes";
    static final String RETRY_AFTER = "5 seconds";
    private static final Logger log = LoggerFactory.getLogger(EventEscalationForwarder.class);

    private final I1ConnectionRegistry connections;
    private final NamedParameterJdbcTemplate jdbc;

    public EventEscalationForwarder(I1ConnectionRegistry connections, NamedParameterJdbcTemplate jdbc) {
        this.connections = connections;
        this.jdbc = jdbc;
    }

    public record Escalation(
            UUID eventId,
            UUID escalationId,
            String basis,
            String assessmentJobId,
            String targetSeverity,
            String channel,
            String actorRole,
            String actorId,
            long requestedAtMillis,
            String note) {
    }

    /** @return 보냈으면 true. 그 Tier 0 연결이 없으면 false(동기화 뒤 다시 보낸다) */
    public boolean forward(String gatewayId, Escalation escalation) {
        Optional<I1Connection> connection = connections.all().stream()
                .filter(c -> gatewayId != null && gatewayId.equals(c.gatewayId()) && c.isOpen())
                .findFirst();
        return connection.isPresent() && send(connection.get(), escalation);
    }

    /** 동기화가 끝난 연결에, 그 Tier 0 의 아직 반영되지 않은 상향 승인을 같은 escalation_id 로 다시 보낸다 */
    public void resendPending(I1Connection connection) {
        resend(connection, "0 seconds");
    }

    /** 열린 연결마다, 보낸 지 RETRY_AFTER 가 지났는데 아직 반영되지 않았거나 JOURNAL_UNAVAILABLE 인 상향을 다시 보낸다 */
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
                SELECT es.event_id, es.escalation_id, es.basis, va.job_id, es.target_severity,
                    es.actor_user_id, es.requested_at, es.note
                FROM event_escalation es
                JOIN risk_event e ON e.event_id = es.event_id
                JOIN vlm_assessment va ON va.assessment_id = es.assessment_id
                WHERE e.gateway_id = :g AND e.lifecycle <> 'RESOLVED'
                  AND es.event_sequence IS NULL
                  AND (es.result IS NULL OR (es.result = 'REJECTED' AND es.reject_reason = 'JOURNAL_UNAVAILABLE'))
                  AND es.requested_at > now() - CAST(:window AS interval)
                  AND es.requested_at <= now() - CAST(:olderThan AS interval)
                ORDER BY es.requested_at, es.escalation_id
                """, new MapSqlParameterSource()
                        .addValue("g", connection.gatewayId())
                        .addValue("window", RESEND_WINDOW)
                        .addValue("olderThan", olderThan),
                (rs, i) -> new Escalation(
                        UUID.fromString(rs.getString("event_id")),
                        UUID.fromString(rs.getString("escalation_id")),
                        rs.getString("basis"),
                        rs.getString("job_id"),
                        rs.getString("target_severity"),
                        "CONTROL_ROOM",
                        "ADMIN",
                        String.valueOf(rs.getLong("actor_user_id")),
                        rs.getTimestamp("requested_at").getTime(),
                        rs.getString("note")));
        pending.forEach(esc -> send(connection, esc));
        if (!pending.isEmpty()) {
            log.info("I1 미반영 상향 다시 보냄: gateway={} count={}", connection.gatewayId(), pending.size());
        }
    }

    private boolean send(I1Connection connection, Escalation escalation) {
        ObjectNode payload = connection.newPayload();
        payload.put("event_id", escalation.eventId().toString());
        ObjectNode operator = payload.putObject("escalation");
        operator.put("escalation_id", escalation.escalationId().toString());
        operator.put("basis", escalation.basis());
        operator.put("assessment_job_id", escalation.assessmentJobId());
        operator.put("target_severity", escalation.targetSeverity());
        operator.put("channel", escalation.channel());
        operator.put("actor_role", escalation.actorRole());
        operator.put("actor_id", escalation.actorId());
        operator.put("requested_at", escalation.requestedAtMillis());
        if (escalation.note() != null && !escalation.note().isBlank()) {
            operator.put("note", escalation.note());
        }
        try {
            connection.send("event_escalate", EVENT_ESCALATE_SCHEMA_VERSION, payload);
            return true;
        } catch (IOException | RuntimeException e) {
            log.warn("event_escalate 보내기 실패: gateway={} escalation_id={} error={}",
                    connection.gatewayId(), escalation.escalationId(), e.getClass().getSimpleName());
            return false;
        }
    }
}
