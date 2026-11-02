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
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import tools.jackson.databind.JsonNode;

/**
 * Tier 0 가 보낸 I1 event_close_result 1.x 수신(명세 6.4·12.3, contracts/messages/event_close_result.schema.json, S15P31A101-221).
 * 결과·거부 사유·event_sequence·결정 시각을 event_close 에 남긴다. 이벤트 상태는 여기서 바꾸지 않는다:
 * 종료는 Tier 0 저널의 RESOLVED 전이(resolution.close_id)로 반영된다(LIVE 생명주기 정본은 Tier 0, AGENTS.md 4).
 * JOURNAL_UNAVAILABLE 은 일시 오류라 EventCloseForwarder 가 같은 close_id 로 다시 보낸다. 커밋 뒤 기다리는 REST 요청을 깨운다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventCloseResultIngestor implements I1MessageIngestor {

    private static final Logger log = LoggerFactory.getLogger(EventCloseResultIngestor.class);
    private static final Set<String> ALLOWED_FIELDS = Set.of(
            "close_id", "event_id", "result", "event_sequence", "reject_reason", "decided_at");
    private static final Set<String> RESULTS = Set.of("ACCEPTED", "DUPLICATE", "REJECTED");
    private static final Set<String> REJECT_REASONS = Set.of(
            "NOT_ACTIVE", "TARGET_NOT_ZONE", "SEVERITY_NOT_ALLOWED", "CONDITION_PRESENT", "JOURNAL_UNAVAILABLE");

    private static final String UPDATE_ACCEPTED = """
            UPDATE event_close SET
                result = 'ACCEPTED',
                reject_reason = NULL,
                event_sequence = :eventSequence,
                decided_at = :decidedAt
            WHERE close_id = :closeId AND event_id = :eventId
            """;

    // 재전송에 DUPLICATE 가 오면 먼저 받은 ACCEPTED 를 지우지 않는다
    private static final String UPDATE_DUPLICATE = """
            UPDATE event_close SET
                result = CASE WHEN result = 'ACCEPTED' THEN 'ACCEPTED' ELSE 'DUPLICATE' END,
                reject_reason = NULL,
                event_sequence = COALESCE(event_sequence, :eventSequence),
                decided_at = COALESCE(decided_at, :decidedAt)
            WHERE close_id = :closeId AND event_id = :eventId
            """;

    // 늦게 온 거부가 이미 받은 수락·중복을 덮지 않는다
    private static final String UPDATE_REJECTED = """
            UPDATE event_close SET
                result = 'REJECTED',
                reject_reason = :rejectReason,
                decided_at = :decidedAt
            WHERE close_id = :closeId AND event_id = :eventId
              AND (result IS NULL OR result = 'REJECTED')
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ApplicationEventPublisher events;
    private final EventCloseWaiters waiters;

    public EventCloseResultIngestor(NamedParameterJdbcTemplate jdbc, ApplicationEventPublisher events,
            EventCloseWaiters waiters) {
        this.jdbc = jdbc;
        this.events = events;
        this.waiters = waiters;
    }

    @Override
    public String msgType() {
        return "event_close_result";
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
                throw new InvalidI1Message("event_close_result 에 허용되지 않은 필드가 있다: " + field);
            }
        }
        UUID closeId = requiredUuid(p, "close_id");
        UUID eventId = requiredUuid(p, "event_id");
        String result = requiredText(p, "result");
        if (!RESULTS.contains(result)) {
            throw new InvalidI1Message("event_close_result.result 가 계약 값이 아니다: " + result);
        }
        OffsetDateTime decidedAt = requiredTime(p, "decided_at");
        Integer eventSequence = null;
        String rejectReason = null;

        if ("REJECTED".equals(result)) {
            if (p.has("event_sequence")) {
                throw new InvalidI1Message("REJECTED 결과에는 event_sequence 를 실을 수 없다");
            }
            rejectReason = requiredText(p, "reject_reason");
            if (!REJECT_REASONS.contains(rejectReason)) {
                throw new InvalidI1Message("event_close_result.reject_reason 이 계약 값이 아니다: " + rejectReason);
            }
        } else {
            if (p.has("reject_reason")) {
                throw new InvalidI1Message(result + " 결과에는 reject_reason 을 실을 수 없다");
            }
            boolean hasSequence = p.has("event_sequence") && !p.get("event_sequence").isNull();
            if ("ACCEPTED".equals(result) && !hasSequence) {
                throw new InvalidI1Message("ACCEPTED 결과에는 1 이상의 event_sequence 가 필요하다");
            }
            if (hasSequence) {
                eventSequence = integer(p, "event_sequence");
                if (eventSequence == null || eventSequence < 1) {
                    throw new InvalidI1Message("event_sequence 가 1 이상이 아니다");
                }
            }
        }

        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("closeId", closeId)
                .addValue("eventId", eventId)
                .addValue("eventSequence", eventSequence)
                .addValue("rejectReason", rejectReason)
                .addValue("decidedAt", decidedAt);
        int updated = switch (result) {
            case "ACCEPTED" -> jdbc.update(UPDATE_ACCEPTED, params);
            case "DUPLICATE" -> jdbc.update(UPDATE_DUPLICATE, params);
            default -> jdbc.update(UPDATE_REJECTED, params);
        };

        log.info("Tier 0 event_close_result 수신: close_id={} event_id={} result={} seq={} reason={} updated={}",
                closeId, eventId, result, eventSequence, rejectReason, updated);
        if (updated == 0) {
            return Outcome.DUPLICATE;
        }
        events.publishEvent(new RiskEventsDelivered(List.of(eventId)));
        // 기다리는 요청이 커밋 전 값을 읽지 않게 커밋 뒤에 깨운다
        if (TransactionSynchronizationManager.isSynchronizationActive()) {
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    waiters.complete(closeId);
                }
            });
        } else {
            waiters.complete(closeId);
        }
        return Outcome.STORED;
    }
}
