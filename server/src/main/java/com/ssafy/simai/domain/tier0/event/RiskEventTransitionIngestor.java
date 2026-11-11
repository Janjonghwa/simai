package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.event.RiskEventRepository.Recorded;
import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;

/**
 * risk_event_transition(Tier 0 → EC2, 명세 12.3·3.2) → risk_event_update 기록·risk_event 반영·저널 위치.
 * 동기화 중(연결 직후 ~ 스냅샷)에 받은 전이는 delivery_mode AUDIT_ONLY 로 기록하고 실시간 알림 대상이 아니다.
 * 스냅샷 뒤에 받은 전이만 REALTIME 이고, 그 전이가 현재 상태를 바꿨으면 커밋 뒤 /topic/events 로 알린다(RiskEventsDelivered).
 * 저장 트랜잭션이 끝난 뒤에 저널 위치를 옮긴다(되돌려진 저장을 ack 하지 않게).
 * 같은 offset 에 다른 메시지가 오면 Tier 0 저널이 새로 시작된 것이라 세대를 올리고 다시 기록한다.
 * 계약 위반으로 버린 전이는 건너뛴 것으로 쳐서 빈 구간이 영원히 남지 않게 한다(onRejected).
 * 현재 상태를 바꾼 실시간 전이는 알리기 전에 Tier 0 가 들고 있는 이벤트(Tier0HeldEvents)에도 반영한다(S15P31A101-595).
 * 동기화 중 재전송 전이는 반영하지 않는다(이어지는 스냅샷이 집합을 바꾼다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RiskEventTransitionIngestor implements I1MessageIngestor {

    private final RiskEventRepository events;
    private final JournalService journal;
    private final TransactionTemplate transaction;
    private final ApplicationEventPublisher publisher;
    private final Tier0HeldEvents held;

    RiskEventTransitionIngestor(RiskEventRepository events, JournalService journal, TransactionTemplate transaction,
            ApplicationEventPublisher publisher, Tier0HeldEvents held) {
        this.events = events;
        this.journal = journal;
        this.transaction = transaction;
        this.publisher = publisher;
        this.held = held;
    }

    @Override
    public String msgType() {
        return "risk_event_transition";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1, 2);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        if (!connection.gatewayId().equals(I1Json.requiredText(envelope.payload(), "gateway_id"))) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        long offset = I1Json.requiredWhole(envelope.payload(), "journal_offset");
        JournalTracker tracker = journal.tracker(connection);
        tracker.noteReceived(offset);
        Recorded recorded = record(envelope, receivedAt, tracker);
        if (recorded == Recorded.OFFSET_CONFLICT) {
            journal.startNewEpoch(connection, tracker);
            recorded = record(envelope, receivedAt, tracker);
        }
        if (recorded.inserted()) {
            tracker.touched(I1Json.requiredUuid(envelope.payload(), "event_id"));
        }
        journal.afterPersisted(connection, tracker, offset);
        return recorded.inserted() ? Outcome.STORED : Outcome.DUPLICATE;
    }

    private Recorded record(I1Envelope envelope, Instant receivedAt, JournalTracker tracker) {
        String deliveryMode = tracker.syncMode() ? "AUDIT_ONLY" : "REALTIME";
        int epoch = tracker.epoch();
        return transaction.execute(status -> {
            Recorded recorded = events.recordTransition(envelope, deliveryMode, receivedAt, epoch);
            if (recorded == Recorded.APPLIED) {
                JsonNode p = envelope.payload();
                UUID eventId = I1Json.requiredUuid(p, "event_id");
                // '들고 있음'은 동기화 중에도 Tier 0 의 지금 상태를 따른다(S15P31A101-621). 동기화 중 새로 난 이벤트가 REST 투영에서
                // 「지난 관제」로 내려가지 않게 한다. 스냅샷이 오면 Tier0HeldEvents.replace 가 다시 맞춘다.
                // 알림(커밋 뒤 다른 스레드)이 바뀐 값을 싣도록 알리기 전에 고친다
                held.apply(envelope.sourceId(), eventId, I1Json.requiredText(p, "transition"));
                // 현재 상태를 바꾼 실시간 전이만 커밋 뒤 알린다. 동기화 중 전이는 스냅샷 분류가 DELAYED_ACTIVE 로 알린다.
                // 늦게 온 낮은 sequence(빈 구간 재전송 등)는 기록만 한다
                if ("REALTIME".equals(deliveryMode)) {
                    publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
                }
            }
            return recorded;
        });
    }

    @Override
    public void onRejected(I1Envelope envelope, I1Connection connection, String reason) {
        Long offset;
        try {
            offset = I1Json.whole(envelope.payload(), "journal_offset");
        } catch (InvalidI1Message e) {
            offset = null;
        }
        if (offset != null && connection.gatewayId() != null) {
            journal.skip(connection, journal.tracker(connection), offset, reason);
        }
    }
}
