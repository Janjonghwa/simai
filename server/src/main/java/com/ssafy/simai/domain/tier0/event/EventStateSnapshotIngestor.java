package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.I1MessageIngestor;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import java.time.Clock;
import java.time.Instant;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;

/**
 * event_state_snapshot(Tier 0 → EC2, 명세 3.2 재동기화 규칙) 으로 동기화를 끝낸다.
 *
 * <ul>
 *   <li>스냅샷이 접어 넣은 offset 까지 빈틈없이 받지 못했으면 다시 요청하고 동기화를 이어 간다(Tier 0 가 다시 보낸 뒤 스냅샷도 다시 보낸다).
 *   <li>동기화 중에 바뀌었고 스냅샷에서 아직 활성인 이벤트: 유효 관측 근거(last_valid_observation_at)가 있으면 DELAYED_ACTIVE
 *       (커밋 뒤 /topic/events 로 알린다), 없으면(재시작 뒤 근거 없음 등) AUDIT_ONLY. 시간 창은 두지 않는다(3.2: 경과 시간만으로 억제하지 않는다).
 *   <li>동기화 중에 바뀌었고 스냅샷에는 없는(그사이 해소된) 이벤트: AUDIT_ONLY. 실시간 재알림 0(G3 D-G3-05).
 *   <li>스냅샷의 활성 이벤트가 EC2 에 없거나 delivery 가 아직 없으면(끝나지 못한 이전 동기화) 같은 규칙으로 분류한다.
 *   <li>스냅샷이 새로 시작한 저널을 알리면(Pod 교체로 offset 이 줄어듦) 저널 세대를 올린다. 새 저널에 기록이 있으면 연결을 닫아
 *       Tier 0 가 처음부터 다시 보내게 한다.
 *   <li>분류가 커밋된 뒤에만 동기화를 끝낸다. 분류가 실패하면 동기화 모드가 이어지고 다음 스냅샷이 다시 분류한다.
 * </ul>
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class EventStateSnapshotIngestor implements I1MessageIngestor {

    private static final Logger log = LoggerFactory.getLogger(EventStateSnapshotIngestor.class);

    private final RiskEventRepository events;
    private final JournalCursorRepository cursors;
    private final JournalService journal;
    private final TransactionTemplate transaction;
    private final EventAckForwarder acks;
    private final EventEscalationForwarder escalations;
    private final EventCloseForwarder closes;
    private final Clock clock;
    private final ApplicationEventPublisher publisher;

    EventStateSnapshotIngestor(RiskEventRepository events, JournalCursorRepository cursors, JournalService journal,
            TransactionTemplate transaction, EventAckForwarder acks, EventEscalationForwarder escalations,
            EventCloseForwarder closes, Clock clock, ApplicationEventPublisher publisher) {
        this.acks = acks;
        this.escalations = escalations;
        this.closes = closes;
        this.events = events;
        this.cursors = cursors;
        this.journal = journal;
        this.transaction = transaction;
        this.clock = clock;
        this.publisher = publisher;
    }

    @Override
    public String msgType() {
        return "event_state_snapshot";
    }

    @Override
    public Set<Integer> supportedMajors() {
        return Set.of(1);
    }

    @Override
    public Outcome ingest(I1Envelope envelope, Instant receivedAt, I1Connection connection) {
        JsonNode p = envelope.payload();
        String gatewayId = connection.gatewayId();
        if (!gatewayId.equals(I1Json.requiredText(p, "gateway_id"))) {
            throw new InvalidI1Message("gateway_id 가 source_id 와 다르다");
        }
        JsonNode active = p.get("active_events");
        if (active == null || !active.isArray()) {
            throw new InvalidI1Message("active_events 가 배열이 아니다");
        }
        Long snapshotOffset = I1Json.whole(p, "journal_offset");
        JournalTracker tracker = journal.tracker(connection);

        if (tracker.isNewJournal(snapshotOffset)) {
            journal.startNewEpoch(connection, tracker);
            if (snapshotOffset != null) {
                connection.close("Tier 0 저널이 새로 시작됨");
                return Outcome.STORED;
            }
        }
        Long missingFrom = tracker.missingBefore(snapshotOffset);
        cursors.saveSnapshot(gatewayId, I1Json.requiredTime(p, "snapshot_at"), snapshotOffset, missingFrom);
        if (missingFrom != null) {
            log.info("I1 스냅샷 전 빈 구간: gateway={} snapshot_offset={} resend_from={}", gatewayId, snapshotOffset, missingFrom);
            journal.send(connection, tracker, missingFrom);
            return Outcome.STORED;
        }

        Set<UUID> touched = tracker.touchedDuringSync();
        transaction.executeWithoutResult(status -> classify(gatewayId, active, touched));
        tracker.finishSync();
        cursors.saveSyncMode(gatewayId, false, I1Json.utc(clock.instant()));
        journal.send(connection, tracker, null);
        // 끊긴 동안 저장만 한 운영자 확인·상향·종료를 같은 ID 로 다시 보낸다(S15P31A101-121, S15P31A101-276, S15P31A101-221)
        try {
            acks.resendPending(connection);
        } catch (RuntimeException e) {
            // 동기화는 끝났다. 다시 보내기는 주기 작업이 이어 한다
            log.warn("I1 미반영 확인 다시 보내기 실패: gateway={} error={}", gatewayId, e.getClass().getSimpleName());
        }
        try {
            escalations.resendPending(connection);
        } catch (RuntimeException e) {
            log.warn("I1 미반영 상향 다시 보내기 실패: gateway={} error={}", gatewayId, e.getClass().getSimpleName());
        }
        try {
            closes.resendPending(connection);
        } catch (RuntimeException e) {
            log.warn("I1 결과 없는 종료 다시 보내기 실패: gateway={} error={}", gatewayId, e.getClass().getSimpleName());
        }
        log.info("I1 저널 동기화 끝: gateway={} active={} changed_during_sync={}", gatewayId, active.size(), touched.size());
        return Outcome.STORED;
    }

    private void classify(String gatewayId, JsonNode active, Set<UUID> touched) {
        Set<UUID> activeIds = new LinkedHashSet<>();
        for (JsonNode event : active.values()) {
            activeIds.add(I1Json.requiredUuid(event, "event_id"));
            events.ensureFromSnapshot(gatewayId, event);
        }
        Set<UUID> undecided = events.withoutDelivery(activeIds);
        Set<UUID> delayed = new LinkedHashSet<>(); // 스냅샷 순서대로 알린다
        Set<UUID> auditOnly = new HashSet<>();
        for (JsonNode event : active.values()) {
            UUID eventId = I1Json.requiredUuid(event, "event_id");
            if (undecided.contains(eventId) || touched.contains(eventId)) {
                boolean hasEvidence = I1Json.time(event, "last_valid_observation_at") != null;
                (hasEvidence ? delayed : auditOnly).add(eventId);
            }
        }
        for (UUID eventId : touched) {
            if (!activeIds.contains(eventId)) {
                auditOnly.add(eventId);
            }
        }
        events.setDelivery(delayed, "DELAYED_ACTIVE");
        if (!delayed.isEmpty()) {
            // 커밋 뒤 /topic/events 로 지연 알림을 보낸다(분류가 되돌려지면 보내지 않는다)
            publisher.publishEvent(new RiskEventsDelivered(List.copyOf(delayed)));
        }
        events.setDelivery(auditOnly, "AUDIT_ONLY");
        int stale = events.countActiveNotIn(gatewayId, activeIds);
        if (stale > 0) {
            // 해소 전이를 받지 못한 채 활성으로 남은 이벤트. 지우거나 바꾸지 않고 드러내기만 한다(EC2 는 전이를 만들지 않는다)
            log.warn("I1 스냅샷에 없는데 EC2 에서 활성인 이벤트: gateway={} count={}", gatewayId, stale);
        }
    }
}
