package com.ssafy.simai.domain.notification;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * 실제 알림(반장 PWA Web Push 등, 명세 7.3·10장) 대기열. LIVE 이벤트만 알린다(7.4 출처 분리, 11.4-3, S15P31A101-123).
 * 두 겹으로 막는다:
 * <ul>
 *   <li>넣기(enqueue): 알릴 전이가 LIVE 이벤트의 발생·갱신(RAISED·UPDATED)이고, 이벤트가 아직 해소되지 않았고, CRITICAL·HIGH 이며
 *       AUDIT_ONLY 가 아닐 때만 notification 행을 만든다(7.3, 해소된 이벤트는 감사 기록으로만). DB 도 source='LIVE' CHECK 와
 *       risk_event(event_id, source) 복합 FK 로 비LIVE 행을 거부한다(V1).</li>
 *   <li>꺼내기(claim): 발송 워커는 이 메서드로만 보낼 알림을 받는다. LIVE 이벤트의 대기 알림만 내준다.</li>
 * </ul>
 * 꺼내기는 임대 방식이다: 한 문장으로 claimed_until 을 쓰고 바로 커밋하므로 트랜잭션 밖에서 불러도 같은 알림을 두 워커가 받지 않고,
 * 외부 푸시를 부르는 동안 행 잠금·DB 연결을 쥐지 않는다. 보낸 뒤 markSent·markFailed 로 끝낸다. 끝내기 전에 워커가 죽으면 임대가 지나
 * 다시 꺼내질 수 있다(최소 한 번). 같은 이벤트의 푸시는 Topic=event_id 로 앞 알림을 대체하므로(10장) 기기에는 한 번만 보인다.
 * 발송 자체(Web Push VAPID)는 S15P31A101-124 가 붙인다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class NotificationQueue {

    private static final Set<String> CHANNELS = Set.of("WEB_PUSH", "WATCH");

    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;
    private final Duration maxAge;

    public NotificationQueue(NamedParameterJdbcTemplate jdbc, Clock clock,
            @Value("${simai.notification.max-age:10m}") Duration maxAge) {
        this.jdbc = jdbc;
        this.clock = clock;
        this.maxAge = maxAge;
    }

    /** 채널은 notification.channel(WEB_PUSH·WATCH). 대상은 구독 또는 사용자(둘 다 비우면 채널 전체) */
    public record Request(UUID eventId, int eventSequence, String channel, Long subscriptionId, Long targetUserId, String summary) {
    }

    public enum Result {
        QUEUED,
        /** 같은 전이·채널·대상이 이미 들어 있다(재전송·재처리) */
        DUPLICATE,
        /** 채널이 없거나 모르는 값, 요약이 비었다 */
        INVALID_REQUEST,
        /** 구독·사용자가 없다(해지·삭제) */
        TARGET_MISSING,
        /** 이벤트나 그 전이를 모른다 */
        UNKNOWN_TRANSITION,
        /** PLANNED·WHAT_IF·REPLAY 등 LIVE 가 아닌 이벤트(7.4) */
        NOT_LIVE,
        /** 해소·확인 전이, 또는 이미 해소된 이벤트(감사 기록으로만) */
        NOT_ALERTING,
        /** CAUTION·INFO 는 실제 알림 채널로 보내지 않는다(7.3) */
        BELOW_ALERT_SEVERITY,
        /** 감사용 전이(AUDIT_ONLY)는 알리지 않는다 */
        AUDIT_ONLY
    }

    /**
     * 꺼낸 알림 한 건. needsCheck·confidence 는 그 전이의 값이다(신뢰도가 낮으면 같은 채널에 "확인 필요"를 붙인다, 7.3).
     * observationAt 은 지연 측정 기준 시각.
     */
    public record Pending(long notificationId, UUID eventId, int eventSequence, String channel, Long subscriptionId,
            Long targetUserId, String severity, String summary, String delivery, boolean needsCheck, String confidence,
            Instant observationAt) {
    }

    /** 알릴 전이를 고르는 조건. 넣기와 진단이 같은 조건을 쓴다 */
    private static final String ALERTING = """
            e.source = 'LIVE' AND e.lifecycle <> 'RESOLVED' AND u.transition IN ('RAISED', 'UPDATED')
                AND u.severity IN ('CRITICAL', 'HIGH') AND u.delivery_mode IN ('REALTIME', 'DELAYED_ACTIVE')
            """;

    public Result enqueue(Request request) {
        if (request.channel() == null || !CHANNELS.contains(request.channel())
                || request.summary() == null || request.summary().isBlank()) {
            return Result.INVALID_REQUEST;
        }
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("eventId", request.eventId()).addValue("seq", request.eventSequence())
                .addValue("channel", request.channel()).addValue("subscriptionId", request.subscriptionId())
                .addValue("targetUserId", request.targetUserId()).addValue("summary", request.summary());
        // 대상이 없으면 FK 예외 대신 결과로 알린다(호출자가 구독마다 넣는 반복을 끊지 않게)
        if ((request.subscriptionId() != null && jdbc.queryForList(
                "SELECT 1 FROM push_subscription WHERE subscription_id = :subscriptionId", params).isEmpty())
                || (request.targetUserId() != null && jdbc.queryForList(
                "SELECT 1 FROM app_user WHERE user_id = :targetUserId", params).isEmpty())) {
            return Result.TARGET_MISSING;
        }
        // 확인한 값이 아니라 DB 의 행을 조건으로 걸어 넣는다(읽은 뒤 바뀌어도 비LIVE 행이 생기지 않게).
        // source 는 risk_event 에서 가져오므로 비LIVE 면 CHECK·복합 FK 에도 걸린다
        List<Long> inserted = jdbc.queryForList("""
                INSERT INTO notification (event_id, source, event_sequence, channel, subscription_id, target_user_id, severity,
                    summary, delivery, observation_at, status)
                SELECT e.event_id, e.source, u.event_sequence, :channel, :subscriptionId, :targetUserId, u.severity,
                    :summary, u.delivery_mode, u.observation_at, 'QUEUED'
                FROM risk_event e JOIN risk_event_update u ON u.event_id = e.event_id AND u.event_sequence = :seq
                WHERE e.event_id = :eventId AND
                """ + ALERTING + """
                ON CONFLICT (event_id, event_sequence, channel, subscription_id, target_user_id) DO NOTHING
                RETURNING notification_id
                """, params, Long.class);
        return inserted.isEmpty() ? whyNotQueued(params) : Result.QUEUED;
    }

    /** 넣지 못한 이유. 정상 경로(넣음)는 이 질의를 돌지 않는다 */
    private Result whyNotQueued(MapSqlParameterSource params) {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT e.source, e.lifecycle, u.transition, u.severity, u.delivery_mode,
                    EXISTS (SELECT 1 FROM notification n WHERE n.event_id = e.event_id AND n.event_sequence = u.event_sequence
                        AND n.channel = :channel AND n.subscription_id IS NOT DISTINCT FROM :subscriptionId
                        AND n.target_user_id IS NOT DISTINCT FROM :targetUserId) AS queued
                FROM risk_event e JOIN risk_event_update u ON u.event_id = e.event_id AND u.event_sequence = :seq
                WHERE e.event_id = :eventId
                """, params);
        if (rows.isEmpty()) {
            return Result.UNKNOWN_TRANSITION;
        }
        Map<String, Object> t = rows.getFirst();
        if (Boolean.TRUE.equals(t.get("queued"))) {
            return Result.DUPLICATE;
        }
        if (!"LIVE".equals(t.get("source"))) {
            return Result.NOT_LIVE;
        }
        if ("RESOLVED".equals(t.get("lifecycle")) || !Set.of("RAISED", "UPDATED").contains((String) t.get("transition"))) {
            return Result.NOT_ALERTING;
        }
        if (!Set.of("CRITICAL", "HIGH").contains((String) t.get("severity"))) {
            return Result.BELOW_ALERT_SEVERITY;
        }
        return Result.AUDIT_ONLY;
    }

    /**
     * 보낼 알림을 lease 동안 빌려 꺼낸다(한 문장, 바로 커밋). LIVE 이벤트의 QUEUED 알림만, 임대가 없거나 지난 것만,
     * CRITICAL·REALTIME 먼저 오래된 순. 관측 시각이 maxAge 보다 오래된 대기 알림은 지금 일어난 위험처럼 보내지 않고 먼저 EXPIRED 로 끝낸다(10장 짧은 TTL).
     */
    public List<Pending> claim(int limit, Duration lease) {
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("limit", limit).addValue("now", now)
                .addValue("until", now.plus(lease)).addValue("staleBefore", now.minus(maxAge));
        jdbc.update("""
                UPDATE notification SET status = 'EXPIRED', error = 'max-age'
                WHERE status = 'QUEUED' AND observation_at < :staleBefore AND (claimed_until IS NULL OR claimed_until < :now)
                """, params);
        // n.source·e.source 조건은 V1 CHECK·복합 FK 와 겹치는 방어용이다(명세 11.4-3 "발송 워커도 LIVE 만 소비").
        // 고르는 순서는 하위 질의가 정하지만 UPDATE … RETURNING 은 순서를 지키지 않으므로 받은 뒤 같은 순서로 다시 정렬한다
        List<Pending> claimed = jdbc.query("""
                UPDATE notification n SET claimed_until = :until
                FROM risk_event_update u
                WHERE n.notification_id IN (
                        SELECT q.notification_id FROM notification q JOIN risk_event e ON e.event_id = q.event_id AND e.source = q.source
                        WHERE q.status = 'QUEUED' AND q.source = 'LIVE' AND e.source = 'LIVE'
                            AND (q.claimed_until IS NULL OR q.claimed_until < :now)
                        ORDER BY (q.severity = 'CRITICAL') DESC, (q.delivery = 'REALTIME') DESC, q.notification_id
                        LIMIT :limit FOR UPDATE OF q SKIP LOCKED)
                    AND u.event_id = n.event_id AND u.event_sequence = n.event_sequence
                RETURNING n.notification_id, n.event_id, n.event_sequence, n.channel, n.subscription_id, n.target_user_id, n.severity,
                    n.summary, n.delivery, n.observation_at, u.needs_check, u.confidence
                """, params, (rs, i) -> new Pending(rs.getLong("notification_id"),
                rs.getObject("event_id", UUID.class), rs.getInt("event_sequence"), rs.getString("channel"),
                rs.getObject("subscription_id", Long.class), rs.getObject("target_user_id", Long.class),
                rs.getString("severity"), rs.getString("summary"), rs.getString("delivery"), rs.getBoolean("needs_check"),
                rs.getString("confidence"), toInstant(rs.getObject("observation_at", OffsetDateTime.class))));
        return claimed.stream().sorted(PRIORITY).toList();
    }

    /** CRITICAL 먼저, 그 안에서 REALTIME 먼저, 같으면 먼저 넣은 것(하위 질의의 ORDER BY 와 같다) */
    private static final Comparator<Pending> PRIORITY = Comparator
            .comparing((Pending p) -> !"CRITICAL".equals(p.severity()))
            .thenComparing(p -> !"REALTIME".equals(p.delivery()))
            .thenComparingLong(Pending::notificationId);

    /** 끝냈으면 true. 이미 끝났거나(다른 워커·만료) 없는 알림이면 false 라 호출자가 알 수 있다 */
    public boolean markSent(long notificationId) {
        return finish(notificationId, "SENT", null);
    }

    /** error 에는 응답 코드 같은 짧은 원인만 남긴다(구독 주소·키는 남기지 않는다). 실패한 알림은 다시 꺼내지 않는다 */
    public boolean markFailed(long notificationId, String error) {
        return finish(notificationId, "FAILED", error);
    }

    private boolean finish(long notificationId, String status, String error) {
        return jdbc.update("""
                UPDATE notification SET status = :status, sent_at = CASE WHEN :status = 'SENT' THEN :now ELSE sent_at END,
                    error = :error, claimed_until = NULL
                WHERE notification_id = :id AND status = 'QUEUED'
                """, new MapSqlParameterSource().addValue("id", notificationId).addValue("status", status).addValue("error", error)
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC))) > 0;
    }

    private static Instant toInstant(OffsetDateTime time) {
        return time == null ? null : time.toInstant();
    }
}
