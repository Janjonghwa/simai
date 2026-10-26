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
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;

/**
 * event_state_snapshot(Tier 0 → EC2, 명세 3.2 재동기화 규칙) 으로 동기화를 끝낸다.
 *
 * <ul>
 *   <li>스냅샷이 접어 넣은 offset 까지 빈틈없이 받지 못했으면 다시 요청하고 동기화를 이어 간다(Tier 0 가 다시 보낸 뒤 스냅샷도 다시 보낸다).
 *   <li>동기화 중에 바뀌었고 스냅샷에서 아직 활성인 이벤트: 유효 관측 근거(last_valid_observation_at)가 있으면 DELAYED_ACTIVE,
 *       없으면(재시작 뒤 근거 없음 등) AUDIT_ONLY. 시간 창은 두지 않는다(3.2: 경과 시간만으로 억제하지 않는다).
 *   <li>동기화 중에 바뀌었고 스냅샷에는 없는(그사이 해소된) 이벤트: AUDIT_ONLY. 실시간 재알림 0(G3 D-G3-05).
 *   <li>스냅샷의 활성 이벤트가 EC2 에 없으면 스냅샷 값으로 만들고 같은 규칙으로 분류한다.
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
    private final Clock clock;

    EventStateSnapshotIngestor(RiskEventRepository events, JournalCursorRepository cursors, JournalService journal,
            TransactionTemplate transaction, Clock clock) {
        this.events = events;
        this.cursors = cursors;
        this.journal = journal;
        this.transaction = transaction;
        this.clock = clock;
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

        Long missingFrom = tracker.missingBefore(snapshotOffset);
        cursors.saveSnapshot(gatewayId, I1Json.requiredTime(p, "snapshot_at"), snapshotOffset, missingFrom);
        if (missingFrom != null) {
            log.info("I1 스냅샷 전 빈 구간: gateway={} snapshot_offset={} resend_from={}", gatewayId, snapshotOffset, missingFrom);
            journal.send(connection, tracker, missingFrom);
            return Outcome.STORED;
        }

        Set<UUID> touched = tracker.finishSync();
        transaction.executeWithoutResult(status -> classify(gatewayId, active, touched));
        cursors.saveSyncMode(gatewayId, false, I1Json.utc(clock.instant()));
        journal.send(connection, tracker, null);
        log.info("I1 저널 동기화 끝: gateway={} active={} changed_during_sync={}", gatewayId, active.size(), touched.size());
        return Outcome.STORED;
    }

    private void classify(String gatewayId, JsonNode active, Set<UUID> touched) {
        Set<UUID> activeIds = new LinkedHashSet<>();
        Set<UUID> delayed = new HashSet<>();
        Set<UUID> auditOnly = new HashSet<>();
        for (JsonNode event : active.values()) {
            UUID eventId = I1Json.requiredUuid(event, "event_id");
            activeIds.add(eventId);
            boolean created = events.ensureFromSnapshot(gatewayId, event);
            if (created || touched.contains(eventId)) {
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
        events.setDelivery(auditOnly, "AUDIT_ONLY");
        int stale = events.countActiveNotIn(gatewayId, activeIds);
        if (stale > 0) {
            // 해소 전이를 받지 못한 채 활성으로 남은 이벤트. 지우거나 바꾸지 않고 드러내기만 한다(EC2 는 전이를 만들지 않는다)
            log.warn("I1 스냅샷에 없는데 EC2 에서 활성인 이벤트: gateway={} count={}", gatewayId, stale);
        }
    }
}
