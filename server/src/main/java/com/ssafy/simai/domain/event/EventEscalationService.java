package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.tier0.event.EventEscalationForwarder;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관리자의 VLM 상향 의견 승인·거절 처리(명세 5.4·12.3·12.6, S15P31A101-276).
 * <ul>
 *   <li>승인(APPROVED): vlm_assessment(decision='APPROVED')와 event_escalation 을 저장하고, 트랜잭션 커밋 뒤 I1 event_escalate 로 Tier 0 에 보낸다.
 *       EC2 는 risk_event.severity 를 직접 바꾸지 않는다(AGENTS.md 4). Tier 0 가 UPDATED 전이(escalation)를 저널에 남기면 그때 반영된다.
 *   <li>거절(REJECTED): vlm_assessment(decision='REJECTED')만 기록하고 상향 의견 배지(proposed_severity)를 내린다. I1 은 보내지 않는다.
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventEscalationService {

    private static final Logger log = LoggerFactory.getLogger(EventEscalationService.class);
    private static final Set<String> DECISIONS = Set.of("APPROVED", "REJECTED");
    private static final Set<String> ESCALATION_TARGETS = Set.of("CRITICAL", "HIGH", "CAUTION");
    private static final String BASIS = "VLM_PROPOSAL";
    private static final String CHANNEL = "CONTROL_ROOM";
    private static final String ACTOR_ROLE = "ADMIN";

    private final EventQueryRepository events;
    private final EventEscalationForwarder forwarder;
    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final ApplicationEventPublisher publisher;

    public EventEscalationService(EventQueryRepository events, EventEscalationForwarder forwarder,
            NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, Clock clock,
            ApplicationEventPublisher publisher) {
        this.events = events;
        this.forwarder = forwarder;
        this.jdbc = jdbc;
        this.transaction = transaction;
        this.clock = clock;
        this.publisher = publisher;
    }

    public ObjectNode decide(UUID eventId, String rawAssessmentId, String decision, String rawNote, long userId) {
        if (rawAssessmentId == null || rawAssessmentId.isBlank()) {
            throw new BusinessException(ErrorCode.Event.INVALID_ASSESSMENT_ID);
        }
        if (decision == null || !DECISIONS.contains(decision)) {
            throw new BusinessException(ErrorCode.Event.INVALID_ESCALATION_DECISION);
        }
        String note = rawNote == null || rawNote.isBlank() ? null : rawNote;
        if (note != null && note.length() > 500) {
            throw new BusinessException(ErrorCode.Event.INVALID_ESCALATION_NOTE);
        }

        String assessmentKey = rawAssessmentId.strip();
        Long numericId = parseLongOrNull(assessmentKey);
        Instant now = clock.instant();
        OffsetDateTime utcNow = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);

        DecisionOutcome outcome = transaction.execute(status -> {
            List<LockedEvent> eventRows = jdbc.query("""
                    SELECT lifecycle, severity, gateway_id
                    FROM risk_event WHERE event_id = :eventId AND source = 'LIVE' FOR UPDATE
                    """, new MapSqlParameterSource("eventId", eventId), (rs, i) -> new LockedEvent(
                    rs.getString("lifecycle"), rs.getString("severity"), rs.getString("gateway_id")));
            if (eventRows.isEmpty()) {
                throw new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND);
            }
            LockedEvent lockedEvent = eventRows.getFirst();
            if ("RESOLVED".equals(lockedEvent.lifecycle())) {
                throw new BusinessException(ErrorCode.Event.EVENT_RESOLVED);
            }

            List<LockedAssessment> assessmentRows = jdbc.query("""
                    SELECT assessment_id, job_id, proposed_severity, decision
                    FROM vlm_assessment
                    WHERE event_id = :eventId
                      AND ((CAST(:numericId AS bigint) IS NOT NULL AND assessment_id = CAST(:numericId AS bigint))
                        OR job_id = :jobId)
                    ORDER BY assessment_id DESC
                    LIMIT 1
                    FOR UPDATE
                    """, new MapSqlParameterSource()
                    .addValue("eventId", eventId)
                    .addValue("numericId", numericId)
                    .addValue("jobId", assessmentKey), (rs, i) -> new LockedAssessment(
                    rs.getLong("assessment_id"),
                    rs.getString("job_id"),
                    rs.getString("proposed_severity"),
                    rs.getString("decision")));
            if (assessmentRows.isEmpty()) {
                throw new BusinessException(ErrorCode.Event.ASSESSMENT_NOT_FOUND);
            }
            LockedAssessment assessment = assessmentRows.getFirst();
            if ("APPROVED".equals(assessment.decision()) || "REJECTED".equals(assessment.decision())) {
                throw new BusinessException(ErrorCode.Event.ASSESSMENT_ALREADY_DECIDED);
            }
            String proposed = assessment.proposedSeverity();
            if (proposed == null) {
                throw new BusinessException(ErrorCode.Event.ESCALATION_NOT_UPWARD);
            }
            if ("APPROVED".equals(decision) && (!ESCALATION_TARGETS.contains(proposed)
                    || severityRank(proposed) <= severityRank(lockedEvent.severity()))) {
                throw new BusinessException(ErrorCode.Event.ESCALATION_NOT_UPWARD);
            }
            if ("APPROVED".equals(decision) && lockedEvent.gatewayId() == null) {
                // 보낼 Tier 0 를 모르면 다시 보내기(gateway_id 로 고른다)로도 닿지 않아, 승인했는데 등급이 영영 안 오른다
                throw new BusinessException(ErrorCode.Event.ESCALATION_NO_GATEWAY);
            }

            jdbc.update("""
                    UPDATE vlm_assessment SET
                        decision = :decision,
                        decided_by = :userId,
                        decided_at = :decidedAt
                    WHERE assessment_id = :assessmentId
                    """, new MapSqlParameterSource()
                    .addValue("decision", decision)
                    .addValue("userId", userId)
                    .addValue("decidedAt", utcNow)
                    .addValue("assessmentId", assessment.assessmentId()));

            jdbc.update("UPDATE risk_event SET proposed_severity = NULL WHERE event_id = :eventId",
                    new MapSqlParameterSource("eventId", eventId));

            UUID escalationId = null;
            if ("APPROVED".equals(decision)) {
                escalationId = UUID.randomUUID();
                jdbc.update("""
                        INSERT INTO event_escalation (
                            escalation_id, event_id, assessment_id, basis, target_severity,
                            actor_user_id, requested_at, note
                        ) VALUES (
                            :escalationId, :eventId, :assessmentId, :basis, :targetSeverity,
                            :userId, :requestedAt, :note
                        )
                        """, new MapSqlParameterSource()
                        .addValue("escalationId", escalationId)
                        .addValue("eventId", eventId)
                        .addValue("assessmentId", assessment.assessmentId())
                        .addValue("basis", BASIS)
                        .addValue("targetSeverity", proposed)
                        .addValue("userId", userId)
                        .addValue("requestedAt", utcNow)
                        .addValue("note", note));
            }
            publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
            return new DecisionOutcome(lockedEvent.gatewayId(), assessment, escalationId);
        });

        if ("APPROVED".equals(decision) && outcome != null && outcome.escalationId() != null) {
            boolean sent = forwarder.forward(outcome.gatewayId(), new EventEscalationForwarder.Escalation(
                    eventId,
                    outcome.escalationId(),
                    BASIS,
                    outcome.assessment().jobId(),
                    outcome.assessment().proposedSeverity(),
                    CHANNEL,
                    ACTOR_ROLE,
                    String.valueOf(userId),
                    now.toEpochMilli(),
                    note));
            if (!sent) {
                log.info("Tier 0 로 바로 보내지 못해 저장만 함(다시 보냄): event_id={} escalation_id={}",
                        eventId, outcome.escalationId());
            }
        }

        ObjectNode response = events.findOne(eventId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND));
        if (outcome != null) {
            response.put("assessment_id", outcome.assessment().assessmentId());
            response.put("decision", decision);
            if (outcome.escalationId() != null) {
                response.put("escalation_id", outcome.escalationId().toString());
            }
        }
        return response;
    }

    private static int severityRank(String severity) {
        if (severity == null) {
            return -1;
        }
        return switch (severity) {
            case "INFO" -> 0;
            case "CAUTION" -> 1;
            case "HIGH" -> 2;
            case "CRITICAL" -> 3;
            default -> -1;
        };
    }

    private static Long parseLongOrNull(String value) {
        if (value == null || value.isEmpty() || value.length() > 18 || !value.chars().allMatch(Character::isDigit)) {
            return null;
        }
        return Long.parseLong(value);
    }

    private record LockedEvent(String lifecycle, String severity, String gatewayId) {
    }

    private record LockedAssessment(long assessmentId, String jobId, String proposedSeverity, String decision) {
    }

    private record DecisionOutcome(String gatewayId, LockedAssessment assessment, UUID escalationId) {
    }
}
