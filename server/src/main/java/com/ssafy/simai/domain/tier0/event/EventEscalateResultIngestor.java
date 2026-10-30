package com.ssafy.simai.domain.tier0.event;

import static com.ssafy.simai.domain.tier0.i1.I1Json.*;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;

/**
 * Tier 0 가 보낸 I1 event_escalate_result 1.x 수신(명세 5.4·12.3, contracts/messages/event_escalate_result.schema.json, S15P31A101-276).
 * ACCEPTED 는 저널 event_sequence 를 기록하고, DUPLICATE 는 멱등 완료로 남기며,
 * REJECTED 는 reject_reason(NOT_ACTIVE·NOT_UPWARD·JOURNAL_UNAVAILABLE)을 기록한다.
 * JOURNAL_UNAVAILABLE 은 저널 일시 오류라 EventEscalationForwarder 가 같은 escalation_id 로 재시도하고,
 * NOT_ACTIVE·NOT_UPWARD 는 재시도하지 않으며 남은 상향 의견 배지(proposed_severity)를 내린다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventEscalateResultIngestor implements I1MessageIngestor {

    private static final Logger log = LoggerFactory.getLogger(EventEscalateResultIngestor.class);
    private static final Set<String> ALLOWED_FIELDS = Set.of(
            "escalation_id", "event_id", "result", "event_sequence", "reject_reason", "decided_at");
    private static final Set<String> RESULTS = Set.of("ACCEPTED", "DUPLICATE", "REJECTED");
    private static final Set<String> REJECT_REASONS = Set.of("NOT_ACTIVE", "NOT_UPWARD", "JOURNAL_UNAVAILABLE");

    private static final String UPDATE_ACCEPTED = """
            UPDATE event_escalation SET
                result = 'ACCEPTED',
                reject_reason = NULL,
                event_sequence = :eventSequence,
                decided_at = :decidedAt
            WHERE escalation_id = :escalationId AND event_id = :eventId
            """;

    private static final String UPDATE_DUPLICATE = """
            UPDATE event_escalation SET
                result = CASE WHEN result = 'ACCEPTED' THEN 'ACCEPTED' ELSE 'DUPLICATE' END,
                reject_reason = NULL,
                event_sequence = COALESCE(event_sequence, :eventSequence),
                decided_at = COALESCE(decided_at, :decidedAt)
            WHERE escalation_id = :escalationId AND event_id = :eventId
            """;

    private static final String UPDATE_REJECTED = """
            UPDATE event_escalation SET
                result = 'REJECTED',
                reject_reason = :rejectReason,
                decided_at = :decidedAt
            WHERE escalation_id = :escalationId AND event_id = :eventId
              AND (result IS NULL OR result = 'REJECTED')
            """;

    private static final String CLEAR_PROPOSED_ON_TERMINAL_REJECT = """
            UPDATE risk_event SET proposed_severity = NULL
            WHERE event_id = :eventId AND source = 'LIVE' AND proposed_severity IS NOT NULL
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ApplicationEventPublisher events;

    public EventEscalateResultIngestor(NamedParameterJdbcTemplate jdbc, ApplicationEventPublisher events) {
        this.jdbc = jdbc;
        this.events = events;
    }

    @Override
    public String msgType() {
        return "event_escalate_result";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    @Transactional
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        JsonNode p = envelope.payload();
        for (String field : p.propertyNames()) {
            if (!ALLOWED_FIELDS.contains(field)) {
                throw new InvalidI1Message("event_escalate_result 에 허용되지 않은 필드가 있다: " + field);
            }
        }
        UUID escalationId = requiredUuid(p, "escalation_id");
        UUID eventId = requiredUuid(p, "event_id");
        String result = requiredText(p, "result");
        if (!RESULTS.contains(result)) {
            throw new InvalidI1Message("event_escalate_result.result 가 계약 값이 아니다: " + result);
        }
        OffsetDateTime decidedAt = requiredTime(p, "decided_at");
        Integer eventSequence = null;
        String rejectReason = null;

        if ("ACCEPTED".equals(result)) {
            if (p.has("reject_reason")) {
                throw new InvalidI1Message("ACCEPTED 결과에는 reject_reason 을 실을 수 없다");
            }
            eventSequence = integer(p, "event_sequence");
            if (eventSequence == null || eventSequence < 1) {
                throw new InvalidI1Message("ACCEPTED 결과에는 1 이상의 event_sequence 가 필요하다");
            }
        } else if ("DUPLICATE".equals(result)) {
            if (p.has("reject_reason")) {
                throw new InvalidI1Message("DUPLICATE 결과에는 reject_reason 을 실을 수 없다");
            }
            if (p.has("event_sequence") && !p.get("event_sequence").isNull()) {
                eventSequence = integer(p, "event_sequence");
                if (eventSequence == null || eventSequence < 1) {
                    throw new InvalidI1Message("event_sequence 가 1 이상이 아니다");
                }
            }
        } else {
            if (p.has("event_sequence")) {
                throw new InvalidI1Message("REJECTED 결과에는 event_sequence 를 실을 수 없다");
            }
            rejectReason = requiredText(p, "reject_reason");
            if (!REJECT_REASONS.contains(rejectReason)) {
                throw new InvalidI1Message("event_escalate_result.reject_reason 이 계약 값이 아니다: " + rejectReason);
            }
        }

        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("escalationId", escalationId)
                .addValue("eventId", eventId)
                .addValue("eventSequence", eventSequence)
                .addValue("rejectReason", rejectReason)
                .addValue("decidedAt", decidedAt);

        int updated;
        if ("ACCEPTED".equals(result)) {
            updated = jdbc.update(UPDATE_ACCEPTED, params);
        } else if ("DUPLICATE".equals(result)) {
            updated = jdbc.update(UPDATE_DUPLICATE, params);
        } else {
            updated = jdbc.update(UPDATE_REJECTED, params);
            if (updated > 0 && !"JOURNAL_UNAVAILABLE".equals(rejectReason)) {
                jdbc.update(CLEAR_PROPOSED_ON_TERMINAL_REJECT, params);
            }
        }

        log.info("Tier 0 event_escalate_result 수신: escalation_id={} event_id={} result={} seq={} reason={} updated={}",
                escalationId, eventId, result, eventSequence, rejectReason, updated);
        if (updated > 0) {
            events.publishEvent(new RiskEventsDelivered(List.of(eventId)));
            return Outcome.STORED;
        }
        return Outcome.DUPLICATE;
    }
}
