package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Instant;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * risk_event_transition(Tier 0 → EC2, 명세 12.3·3.2) → risk_event_update 기록·risk_event 반영·저널 위치.
 * 동기화 중(연결 직후 ~ 스냅샷)에 받은 전이는 delivery_mode AUDIT_ONLY 로 기록하고 실시간 알림 대상이 아니다.
 * 스냅샷 뒤에 받은 전이만 REALTIME 이다. 저장 트랜잭션이 끝난 뒤에 저널 위치를 옮긴다(되돌려진 저장을 ack 하지 않게).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RiskEventTransitionIngestor implements I1MessageIngestor {

    private final RiskEventRepository events;
    private final JournalService journal;
    private final TransactionTemplate transaction;

    RiskEventTransitionIngestor(RiskEventRepository events, JournalService journal, TransactionTemplate transaction) {
        this.events = events;
        this.journal = journal;
        this.transaction = transaction;
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
        String deliveryMode = tracker.syncMode() ? "AUDIT_ONLY" : "REALTIME";
        boolean inserted = Boolean.TRUE.equals(
                transaction.execute(status -> events.recordTransition(envelope, deliveryMode, receivedAt)));
        if (inserted) {
            tracker.touched(I1Json.requiredUuid(envelope.payload(), "event_id"));
        }
        journal.afterPersisted(connection, tracker, offset);
        return inserted ? Outcome.STORED : Outcome.DUPLICATE;
    }
}
