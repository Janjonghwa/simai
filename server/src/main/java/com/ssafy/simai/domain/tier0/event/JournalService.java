package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Connection;
import com.ssafy.simai.domain.tier0.i1.I1ConnectionRegistry;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import io.micrometer.core.instrument.MeterRegistry;
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
 * ack 는 수신 스레드와 주기 작업 스레드가 함께 보내므로 연결마다(tracker 잠금) 만들기·커서 저장·보내기를 한 번에 한다.
 * 커서는 ack 를 보낼 때만 저장한다. DB 의 acked 는 Tier 0 가 받은 어떤 ack 보다도 작지 않다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class JournalService {

    static final String JOURNAL_ACK_SCHEMA_VERSION = "1.2.0";
    static final String SKIPPED_METRIC = "simai.i1.journal.skipped";
    private static final String STATE_KEY = "journal";
    private static final Logger log = LoggerFactory.getLogger(JournalService.class);

    private final JournalCursorRepository cursors;
    private final I1ConnectionRegistry connections;
    private final MeterRegistry meters;
    private final Clock clock;

    public JournalService(JournalCursorRepository cursors, I1ConnectionRegistry connections, MeterRegistry meters, Clock clock) {
        this.cursors = cursors;
        this.connections = connections;
        this.meters = meters;
        this.clock = clock;
    }

    /** 연결의 첫 저널 메시지에서 저장된 커서를 읽고 동기화 모드로 들어간다(연결마다, 3.2 재동기화 규칙) */
    public JournalTracker tracker(I1Connection connection) {
        return connection.state(STATE_KEY, () -> {
            String gatewayId = connection.gatewayId();
            JournalCursorRepository.Cursor cursor = cursors.load(gatewayId);
            cursors.saveSyncMode(gatewayId, true, I1Json.utc(clock.instant()));
            log.info("I1 저널 동기화 시작: gateway={} epoch={} acked={}", gatewayId, cursor.epoch(), cursor.acked());
            return new JournalTracker(cursor.acked(), cursor.epoch());
        });
    }

    /** 전이 하나가 저장됐다(또는 이미 있었다). 빈 구간을 처음 보면 바로 다시 요청한다 */
    public void afterPersisted(I1Connection connection, JournalTracker tracker, long offset) {
        Long resendFrom = tracker.onPersisted(offset);
        if (resendFrom != null) {
            log.info("I1 저널 빈 구간 다시 요청: gateway={} acked={} resend_from={}", connection.gatewayId(), tracker.acked(), resendFrom);
            send(connection, tracker, resendFrom);
        }
    }

    /**
     * 계약 위반이라 다시 받아도 저장할 수 없는 전이. 건너뛴 것으로 치고 빈 구간이 영원히 남지 않게 한다(남으면 동기화가 끝나지 않는다).
     * DB 장애처럼 다시 받으면 될 수 있는 실패에는 부르지 않는다.
     */
    public void skip(I1Connection connection, JournalTracker tracker, long offset, String reason) {
        meters.counter(SKIPPED_METRIC).increment();
        log.error("I1 저널 offset 을 저장할 수 없어 건너뜀: gateway={} epoch={} offset={} reason={}",
                connection.gatewayId(), tracker.epoch(), offset, reason);
        afterPersisted(connection, tracker, offset);
    }

    /** Tier 0 저널이 새로 시작됐다(Pod 교체 등). 세대를 올리고 받은 위치를 비운다 */
    public void startNewEpoch(I1Connection connection, JournalTracker tracker) {
        int epoch = cursors.startNewEpoch(connection.gatewayId(), I1Json.utc(clock.instant()));
        tracker.startEpoch(epoch);
        log.warn("I1 Tier 0 저널이 새로 시작됨: gateway={} epoch={}", connection.gatewayId(), epoch);
    }

    /** acked 와 (있으면) 다시 보낼 시작 offset, 지금 동기화 여부를 보낸다. acked 를 모르거나 음수면 보내지 않는다 */
    public void send(I1Connection connection, JournalTracker tracker, Long resendFrom) {
        synchronized (tracker) {
            Long acked = tracker.takeAck();
            if (acked == null || !connection.isOpen()) {
                return;
            }
            cursors.saveAcked(connection.gatewayId(), acked, I1Json.utc(clock.instant()));
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
