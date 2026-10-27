package com.ssafy.simai.domain.notification;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.notification.NotificationQueue.Pending;
import com.ssafy.simai.domain.notification.NotificationQueue.Request;
import com.ssafy.simai.domain.notification.NotificationQueue.Result;
import java.security.SecureRandom;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.dao.DataIntegrityViolationException;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;

/**
 * LIVE 이벤트만 알림(S15P31A101-123, 명세 7.3·7.4·11.4-3). 명세대로 DB 기록 차단과 실제 발송 차단을 각각 본다:
 * DB 는 비LIVE notification 행을 CHECK·복합 FK 로 거부하고, 대기열은 비LIVE·해소·CAUTION·AUDIT_ONLY 를 넣지 않으며, 발송 워커가 쓰는 claim 은
 * LIVE 이벤트의 대기 알림만 임대로 내준다. 다른 시험의 알림과 섞이지 않게 이 시험이 만든 이벤트의 알림만 센다(꺼내기는 넉넉한 limit).
 */
@SpringBootTest
@ActiveProfiles("test")
class NotificationQueueIntegrationTest {

    private static final SecureRandom RANDOM = new SecureRandom();
    private static final Duration LEASE = Duration.ofMinutes(1);
    private static final int ALL = 10_000;

    @Autowired
    NotificationQueue queue;

    @Autowired
    JdbcTemplate jdbc;

    private final String gatewayId = "tier0-notify-" + UUID.randomUUID().toString().substring(0, 8);
    private final List<UUID> events = new ArrayList<>();
    private final List<Long> users = new ArrayList<>();

    @AfterEach
    void cleanUp() {
        for (UUID event : events) {
            jdbc.update("DELETE FROM notification WHERE event_id = ?", event);
            jdbc.update("DELETE FROM risk_event_update WHERE event_id = ?", event);
            jdbc.update("DELETE FROM risk_event WHERE event_id = ?", event);
        }
        users.forEach(user -> jdbc.update("DELETE FROM app_user WHERE user_id = ?", user));
    }

    @Test
    void LIVE_의_CRITICAL_전이는_한_번만_대기열에_들어가고_발송_뒤에는_다시_꺼내지_않는다() {
        UUID event = event("LIVE", "CRITICAL", "REALTIME");
        long foreman = user();

        assertThat(queue.enqueue(request(event, null))).isEqualTo(Result.QUEUED);
        // Tier 0 재전송·재처리
        assertThat(queue.enqueue(request(event, null))).isEqualTo(Result.DUPLICATE);
        // 같은 전이라도 다른 대상은 따로
        assertThat(queue.enqueue(request(event, foreman))).isEqualTo(Result.QUEUED);

        List<Pending> pending = mine(queue.claim(ALL, LEASE));
        pending.forEach(p -> assertThat(queue.markSent(p.notificationId())).isTrue());

        assertThat(pending).hasSize(2);
        assertThat(pending.getFirst().severity()).isEqualTo("CRITICAL");
        assertThat(pending.getFirst().delivery()).isEqualTo("REALTIME");
        assertThat(pending.getFirst().summary()).isEqualTo("R1 위험 구역 진입");
        assertThat(pending.getFirst().observationAt()).isNotNull();
        assertThat(jdbc.queryForList("SELECT status FROM notification WHERE event_id = ?", String.class, event))
                .containsOnly("SENT");
        assertThat(mine(queue.claim(ALL, LEASE))).isEmpty();
        // 이미 끝낸 알림은 다시 끝내지 않는다(호출자가 알 수 있다)
        assertThat(queue.markSent(pending.getFirst().notificationId())).isFalse();
    }

    @Test
    void 비LIVE_해소_CAUTION_감사용_모르는_전이와_틀린_요청은_대기열에_넣지_않는다() {
        UUID replay = event("REPLAY", "CRITICAL", "REALTIME");
        UUID caution = event("LIVE", "CAUTION", "REALTIME");
        UUID audit = event("LIVE", "HIGH", "AUDIT_ONLY");
        UUID resolvedTransition = event("LIVE", "CRITICAL", "REALTIME", "RESOLVED", "RAISED");
        UUID resolvedEvent = event("LIVE", "CRITICAL", "REALTIME", "RAISED", "RESOLVED");

        assertThat(queue.enqueue(request(replay, null))).isEqualTo(Result.NOT_LIVE);
        assertThat(queue.enqueue(request(caution, null))).isEqualTo(Result.BELOW_ALERT_SEVERITY);
        assertThat(queue.enqueue(request(audit, null))).isEqualTo(Result.AUDIT_ONLY);
        // 해소 전이, 이미 해소된 이벤트의 옛 전이는 감사 기록으로만
        assertThat(queue.enqueue(request(resolvedTransition, null))).isEqualTo(Result.NOT_ALERTING);
        assertThat(queue.enqueue(request(resolvedEvent, null))).isEqualTo(Result.NOT_ALERTING);
        assertThat(queue.enqueue(new Request(UUID.randomUUID(), 1, "WEB_PUSH", null, null, "x"))).isEqualTo(Result.UNKNOWN_TRANSITION);
        assertThat(queue.enqueue(new Request(caution, 99, "WEB_PUSH", null, null, "x"))).isEqualTo(Result.UNKNOWN_TRANSITION);
        // 틀린 요청·없는 대상은 예외가 아니라 결과로(구독마다 넣는 반복이 끊기지 않게)
        assertThat(queue.enqueue(new Request(audit, 1, "SMS", null, null, "x"))).isEqualTo(Result.INVALID_REQUEST);
        assertThat(queue.enqueue(new Request(audit, 1, "WEB_PUSH", null, null, " "))).isEqualTo(Result.INVALID_REQUEST);
        UUID live = event("LIVE", "HIGH", "REALTIME");
        assertThat(queue.enqueue(new Request(live, 1, "WEB_PUSH", 987_654_321L, null, "x"))).isEqualTo(Result.TARGET_MISSING);
        assertThat(queue.enqueue(new Request(live, 1, "WEB_PUSH", null, 987_654_321L, "x"))).isEqualTo(Result.TARGET_MISSING);

        assertThat(events.stream().mapToInt(e -> jdbc.queryForObject("SELECT count(*) FROM notification WHERE event_id = ?",
                Integer.class, e)).sum()).isZero();
    }

    @Test
    void DB_는_비LIVE_이벤트의_알림_행을_직접_넣어도_거부한다() {
        UUID replay = event("REPLAY", "CRITICAL", "REALTIME");

        // source 칸 CHECK(source IN ('LIVE'))
        assertThatThrownBy(() -> insertNotification(replay, "REPLAY")).isInstanceOf(DataIntegrityViolationException.class);
        // source 를 LIVE 로 적어도 risk_event(event_id, 'LIVE') 가 없어 복합 FK 에 걸린다
        assertThatThrownBy(() -> insertNotification(replay, "LIVE")).isInstanceOf(DataIntegrityViolationException.class);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notification WHERE event_id = ?", Integer.class, replay)).isZero();
    }

    @Test
    void 발송_워커는_REPLAY_이벤트를_한_건도_보내지_않고_LIVE_만_보낸다() {
        UUID live = event("LIVE", "HIGH", "DELAYED_ACTIVE");
        UUID replay = event("REPLAY", "HIGH", "DELAYED_ACTIVE");
        queue.enqueue(request(live, null));
        queue.enqueue(request(replay, null));

        List<Pending> sent = mine(queue.claim(ALL, LEASE));
        sent.forEach(p -> queue.markSent(p.notificationId()));

        assertThat(sent).extracting(Pending::eventId).containsExactly(live);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM notification WHERE event_id = ? AND status = 'SENT'", Integer.class,
                replay)).isZero();
    }

    @Test
    void 꺼낸_알림은_임대_중에는_다른_워커가_받지_않고_임대가_지나면_다시_꺼내며_실패는_다시_꺼내지_않는다() {
        UUID first = event("LIVE", "CRITICAL", "REALTIME");
        UUID second = event("LIVE", "CRITICAL", "REALTIME");
        queue.enqueue(request(first, null));
        queue.enqueue(request(second, null));

        // 워커 1 이 둘 다 빌려 갔다(트랜잭션 없이, 한 문장)
        List<Pending> worker1 = mine(queue.claim(ALL, LEASE));
        // 워커 2 는 임대 중인 알림을 받지 않는다
        assertThat(worker1).hasSize(2);
        assertThat(mine(queue.claim(ALL, LEASE))).isEmpty();

        assertThat(queue.markFailed(worker1.getFirst().notificationId(), "410")).isTrue();
        // 워커 1 이 둘째를 끝내기 전에 죽었다: 임대가 지나면 다시 꺼낸다
        jdbc.update("UPDATE notification SET claimed_until = now() - interval '1 second' WHERE notification_id = ?",
                worker1.get(1).notificationId());
        List<Pending> retried = mine(queue.claim(ALL, LEASE));

        assertThat(retried).extracting(Pending::notificationId).containsExactly(worker1.get(1).notificationId());
        assertThat(jdbc.queryForObject("SELECT error FROM notification WHERE notification_id = ?", String.class,
                worker1.getFirst().notificationId())).isEqualTo("410");
    }

    @Test
    void CRITICAL_REALTIME_을_먼저_꺼내고_확인_필요를_함께_주며_오래된_대기_알림은_만료한다() {
        UUID high = event("LIVE", "HIGH", "DELAYED_ACTIVE");
        UUID critical = event("LIVE", "CRITICAL", "REALTIME");
        UUID stale = event("LIVE", "CRITICAL", "REALTIME");
        jdbc.update("UPDATE risk_event_update SET needs_check = true, confidence = 'LOW' WHERE event_id = ?", critical);
        jdbc.update("UPDATE risk_event_update SET observation_at = now() - interval '1 hour' WHERE event_id = ?", stale);
        queue.enqueue(request(high, null));
        queue.enqueue(request(critical, null));
        queue.enqueue(request(stale, null));

        List<Pending> claimed = mine(queue.claim(ALL, LEASE));

        // 먼저 넣은 HIGH 보다 CRITICAL 이 앞, 한 시간 전 관측은 보내지 않는다(max-age 10분)
        assertThat(claimed).extracting(Pending::eventId).containsExactly(critical, high);
        assertThat(claimed.getFirst().needsCheck()).isTrue();
        assertThat(claimed.getFirst().confidence()).isEqualTo("LOW");
        assertThat(jdbc.queryForObject("SELECT status FROM notification WHERE event_id = ?", String.class, stale))
                .isEqualTo("EXPIRED");
    }

    // ---- 도우미

    private List<Pending> mine(List<Pending> claimed) {
        return claimed.stream().filter(p -> events.contains(p.eventId())).toList();
    }

    private static Request request(UUID event, Long targetUserId) {
        return new Request(event, 1, "WEB_PUSH", null, targetUserId, "R1 위험 구역 진입");
    }

    private long user() {
        long user = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', 'FOREMAN', now()) RETURNING user_id", Long.class, gatewayId + "-" + users.size());
        users.add(user);
        return user;
    }

    private UUID event(String source, String severity, String deliveryMode) {
        return event(source, severity, deliveryMode, "RAISED", "RAISED");
    }

    /**
     * 이벤트와 그 전이(event_sequence 1). 비LIVE 이벤트는 전이 표(LIVE 만)에 없으므로 LIVE 로 남긴 전이에 출처만 다르게 둔다.
     * transition 은 그 전이의 종류, lifecycle 은 이벤트의 현재 상태다
     */
    private UUID event(String source, String severity, String deliveryMode, String transition, String lifecycle) {
        UUID event = UUID.randomUUID();
        events.add(event);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence,
                    needs_check, world_ids, metrics, resume_count, raised_at, retention_hold)
                VALUES (?, ?, ?, 'R1', 1, ?, 'VERIFIED', ?, 'HIGH', false, '{}', '{}'::jsonb, 0, now(), false)
                """, event, source, gatewayId, lifecycle, severity);
        jdbc.update("""
                INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition, occurred_at,
                    observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version, scene_version, source,
                    severity, confidence, verification, needs_check, world_ids, metrics, resent, resumed, delivery_mode, received_at)
                VALUES (?, 1, ?, ?, ?, ?, now(), now(), now(), 'R1', 1, 'cfg-1', 'cal-1', 'scene-1', 'LIVE', ?, 'HIGH', 'VERIFIED',
                    false, '{}', '{}'::jsonb, false, false, ?, now())
                """, event, UUID.randomUUID(), gatewayId, RANDOM.nextLong(1, Long.MAX_VALUE), transition, severity, deliveryMode);
        return event;
    }

    private void insertNotification(UUID event, String source) {
        jdbc.update("""
                INSERT INTO notification (event_id, source, event_sequence, channel, severity, summary, delivery, status)
                VALUES (?, ?, 1, 'WEB_PUSH', 'CRITICAL', 'x', 'REALTIME', 'QUEUED')
                """, event, source);
    }
}
