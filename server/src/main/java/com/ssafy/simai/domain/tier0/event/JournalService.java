package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import java.io.IOException;
import java.time.Clock;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import tools.jackson.databind.node.ObjectNode;

/**
 * I1 연결별 저널 수신 위치(JournalTracker)를 두고 journal_ack 를 보낸다(명세 3.2, 계약 journal_ack).
 * Tier 0 는 ack 를 기다리지 않고 저널 끝까지 보내지만, ack 가 없으면 저널을 정리하지 못하고 재연결 때 전부 다시 보낸다.
 * 그래서 빈 구간을 찾았을 때·스냅샷을 받았을 때는 바로, 그 밖에는 나아간 acked 를 1초마다 몰아서 보낸다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class JournalService {

    static final String JOURNAL_ACK_SCHEMA_VERSION = "1.2.0";
    private static final String STATE_KEY = "journal";
    private static final Logger log = LoggerFactory.getLogger(JournalService.class);

    private final JournalCursorRepository cursors;
    private final I1ConnectionRegistry connections;
    private final Clock clock;

    public JournalService(JournalCursorRepository cursors, I1ConnectionRegistry connections, Clock clock) {
        this.cursors = cursors;
        this.connections = connections;
        this.clock = clock;
    }

    /** 연결의 첫 저널 메시지에서 저장된 커서를 읽고 동기화 모드로 들어간다(연결마다, 3.2 재동기화 규칙) */
    public JournalTracker tracker(I1Connection connection) {
        return connection.state(STATE_KEY, () -> {
            String gatewayId = connection.gatewayId();
            Long acked = cursors.loadAcked(gatewayId);
            cursors.saveSyncMode(gatewayId, true, I1Json.utc(clock.instant()));
            log.info("I1 저널 동기화 시작: gateway={} acked={}", gatewayId, acked);
            return new JournalTracker(acked);
        });
    }

    /** 전이 하나가 저장됐다. 빈 구간을 처음 보면 바로 다시 요청한다 */
    public void afterPersisted(I1Connection connection, JournalTracker tracker, long offset) {
        Long before = tracker.acked();
        Long resendFrom = tracker.onPersisted(offset);
        Long after = tracker.acked();
        if (after != null && after >= 0 && !after.equals(before)) {
            cursors.saveAcked(connection.gatewayId(), after, I1Json.utc(clock.instant()));
        }
        if (resendFrom != null) {
            log.info("I1 저널 빈 구간 다시 요청: gateway={} acked={} resend_from={}", connection.gatewayId(), after, resendFrom);
            send(connection, tracker, resendFrom);
        }
    }

    /** acked 와 (있으면) 다시 보낼 시작 offset, 지금 동기화 여부를 보낸다. acked 를 모르거나 음수면 보내지 않는다 */
    public void send(I1Connection connection, JournalTracker tracker, Long resendFrom) {
        tracker.takePendingAck();
        Long acked = tracker.acked();
        if (acked == null || acked < 0 || !connection.isOpen()) {
            return;
        }
        ObjectNode payload = connection.newPayload();
        payload.put("gateway_id", connection.gatewayId());
        payload.put("acked_offset", acked);
        if (resendFrom != null && resendFrom > acked) {
            payload.put("resend_from_offset", resendFrom);
        }
        payload.put("sync_mode", tracker.syncMode());
        try {
            connection.send("journal_ack", JOURNAL_ACK_SCHEMA_VERSION, payload);
        } catch (IOException | IllegalStateException e) {
            // 보내지 못해도 저장은 끝났다. 다음 주기나 재연결 때 다시 보낸다
            log.warn("journal_ack 보내기 실패: gateway={} error={}", connection.gatewayId(), e.getClass().getSimpleName());
        }
    }

    @Scheduled(fixedDelay = 1000)
    void flushPendingAcks() {
        for (I1Connection connection : connections.all()) {
            JournalTracker tracker = connection.stateIfPresent(STATE_KEY);
            if (tracker != null && tracker.hasPendingAck()) {
                send(connection, tracker, null);
            }
        }
    }
}
