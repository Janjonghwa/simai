package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.sql.Array;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.event.EventListener;
import org.springframework.dao.DataAccessException;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.event.TransactionalEventListener;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 위험 이벤트 → BOUND 워치 진동 알림과 ACK·격상(명세 부록 C.6, FR-45·46, 7.3 워치 개인 채널, S15P31A101-338,
 * 설계 docs/design/server-watch-alert.md).
 * <ul>
 *   <li>보내기: 화면에 알릴 LIVE 이벤트(RiskEventsDelivered, 커밋 뒤)가 아직 해소되지 않았고 CRITICAL·HIGH·CAUTION 이면, 그 world_ids 중
 *       워치가 배정돼 있고(watch_binding) 연결 중(CONNECTED)인 사람에게 watch_alert 를 보낸다. 배정이 없거나(UNBOUND) 끊긴(LOST) 사람은
 *       보내지 않는다(기존 경로: 현장 경보·반장 PWA·관리자 화면). 같은 이벤트·워치에는 등급이 오를 때만 다시 보낸다
 *   <li>기록: watch_alert 행을 먼저 넣고 보낸다(봉투 msg_id = alert_msg_id). LIVE 만(복합 FK)
 *   <li>ACK: watch_ack VIBRATED 는 acked_at(워치 시계)·ack_received_at(EC2 시각), CONFIRMED 는 confirmed_at. 같은 종류는 처음 것만 남는다
 *   <li>격상: sent_at + ack_within_ms 까지 VIBRATED ACK 가 EC2 에 닿지 않으면(NO_ACK) 또는 보내지 못하면(SEND_FAILED) escalated_at 을 적고
 *       관리자 화면에 watch_escalation(/topic/watch)을 보낸다. 한 알림에 한 번. 판정은 DB 시각 조건으로 해서 EC2 재시작 뒤에도 이어진다
 * </ul>
 * 해소된 이벤트·10분 넘은 알림은 격상하지 않는다. CONFIRMED 만 와도(VIBRATED 를 잃어도) 받은 것으로 본다. 보내기는 송신 버퍼에 넣으면
 * 끝나 반쯤 끊긴 연결로 보낸 것은 SEND_FAILED 가 아니라 기한 뒤 NO_ACK 로 잡힌다. 해소 뒤 다시 활성(resumed)이면 새로 알린다.
 * 워치 ACK·격상은 운영자 확인(3.2)이 아니고 이벤트 상태를 바꾸지 않는다. CRITICAL·HIGH 의 현장 경보는 Tier 0·노트북이 이미 울린다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchAlertService implements AutoCloseable {

    static final String ALERT_VERSION = "1.0.0";
    static final String ESCALATION_VERSION = "1.0.0";

    private static final Logger log = LoggerFactory.getLogger(WatchAlertService.class);

    /** 규칙별 한 단어(계약 1~8자, 갤럭시워치6 원형 화면 한 줄). 모르는 규칙은 "위험" */
    private static final Map<String, String> WORDS = Map.of(
            "R1", "위험구역", "R2", "개구부", "R3", "고소", "R4", "낙상", "R5", "진입주의",
            "R6", "구역확인", "R7", "감시저하", "R8", "장비", "R9", "안전모", "R10", "점유주의");

    /** 등급 → 진동 패턴(FR-45, 계약에서 생략할 때 워치가 쓰는 것과 같다. 표에는 늘 적는다) */
    private static final Map<String, String> PATTERNS = Map.of(
            "CRITICAL", "DANGER_LONG", "HIGH", "CAUTION_DOUBLE", "CAUTION", "CAUTION_WEAK");

    private static final String RANK_SQL = "CASE %s WHEN 'CAUTION' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'CRITICAL' THEN 3 ELSE 0 END";

    private static final String ESCALATED_COLUMNS = """
            RETURNING event_id, event_sequence, alert_msg_id, device_id, world_id, severity, sent_at, escalated_at, escalation_reason""";

    private record LiveEvent(UUID eventId, int sequence, int resumeCount, String severity, boolean needsCheck, String ruleId,
            List<String> worldIds) {
    }

    /** 이만큼 오래된 대기 알림은 격상하지 않는다(EC2 가 오래 내려갔다 뜰 때 지난 위험을 한꺼번에 알리지 않게, 알림 대기열 max-age 와 같은 10분) */
    private static final String ESCALATION_MAX_AGE = "10 minutes";

    private final NamedParameterJdbcTemplate jdbc;
    private final WatchBindings bindings;
    private final WatchHub hub;
    private final WatchProperties properties;
    private final RealtimePublisher publisher;
    private final ObjectMapper mapper;
    private final Clock clock;
    /** 보내기는 이 스레드 하나에서 한다. I1 수신·커밋 스레드를 붙잡지 않고, 같은 이벤트의 전이가 받은 순서대로 나간다 */
    private final ExecutorService sender = Executors.newSingleThreadExecutor(Thread.ofPlatform().name("watch-alert").daemon().factory());
    private final ScheduledExecutorService escalator = Executors.newSingleThreadScheduledExecutor(
            Thread.ofPlatform().name("watch-escalation").daemon().factory());

    WatchAlertService(NamedParameterJdbcTemplate jdbc, WatchBindings bindings, WatchHub hub, WatchProperties properties,
            RealtimePublisher publisher, ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.bindings = bindings;
        this.hub = hub;
        this.properties = properties;
        this.publisher = publisher;
        this.mapper = mapper;
        this.clock = clock;
        long period = properties.escalationInterval().toMillis();
        escalator.scheduleWithFixedDelay(this::escalateOverdue, period, period, TimeUnit.MILLISECONDS);
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onRiskEvents(RiskEventsDelivered delivered) {
        for (UUID eventId : delivered.eventIds()) {
            try {
                sender.execute(() -> {
                    try {
                        alert(eventId);
                    } catch (RuntimeException e) {
                        log.warn("워치 알림을 보내지 못함: event={}", eventId, e);
                    }
                });
            } catch (RejectedExecutionException e) {
                return; // 서버가 내려가는 중이다
            }
        }
    }

    /** 이 스레드(watch-alert)에서만 부른다. 시험은 직접 부른다 */
    void alert(UUID eventId) {
        Optional<LiveEvent> found = jdbc.query("""
                SELECT event_id, event_sequence, resume_count, severity, needs_check, rule_id, world_ids FROM risk_event
                WHERE event_id = :id AND source = 'LIVE' AND lifecycle <> 'RESOLVED' AND severity IN ('CRITICAL', 'HIGH', 'CAUTION')
                  AND event_sequence >= 1
                """, new MapSqlParameterSource("id", eventId), liveEvent()).stream().findFirst();
        if (found.isEmpty() || found.get().worldIds().isEmpty()) {
            return;
        }
        LiveEvent event = found.get();
        bindings.devicesFor(event.worldIds()).forEach((worldId, device) -> {
            if (hub.isConnected(device)) { // LOST: 워치로 보내지 않는다(기존 경로). 배정 없는 사람(UNBOUND)은 목록에 없다
                send(event, worldId, device);
            }
        });
    }

    private void send(LiveEvent event, String worldId, String deviceId) {
        UUID msgId = UUID.randomUUID();
        Instant now = clock.instant();
        int ackWithinMs = (int) properties.ackWithin().toMillis();
        String word = WORDS.getOrDefault(event.ruleId(), "위험");
        // 같은 이벤트·워치에 같거나 높은 등급을 이미 보냈으면 넣지 않는다(재발행·요약 갱신·ACK 로 다시 와도 진동은 한 번, 등급이 오르면 다시)
        List<Long> inserted = jdbc.queryForList("""
                INSERT INTO watch_alert (alert_msg_id, event_id, source, event_sequence, resume_count, device_id, world_id, severity,
                    vibration_pattern, word, needs_check, ack_within_ms, sent_at)
                SELECT :msgId, :eventId, 'LIVE', :sequence, :resumeCount, :device, :world, :severity, :pattern, :word, :needsCheck,
                    :ackWithin, :now
                WHERE NOT EXISTS (SELECT 1 FROM watch_alert WHERE event_id = :eventId AND resume_count = :resumeCount
                                  AND device_id = :device AND escalation_reason IS DISTINCT FROM 'SEND_FAILED'
                                  AND %s >= %s)
                ON CONFLICT DO NOTHING
                RETURNING alert_id
                """.formatted(RANK_SQL.formatted("severity"), RANK_SQL.formatted("CAST(:severity AS text)")),
                new MapSqlParameterSource().addValue("msgId", msgId).addValue("eventId", event.eventId())
                        .addValue("sequence", event.sequence()).addValue("resumeCount", event.resumeCount()).addValue("device", deviceId).addValue("world", worldId)
                        .addValue("severity", event.severity()).addValue("pattern", PATTERNS.get(event.severity()))
                        .addValue("word", word).addValue("needsCheck", event.needsCheck()).addValue("ackWithin", ackWithinMs)
                        .addValue("now", utc(now)), Long.class);
        if (inserted.isEmpty()) {
            return;
        }
        ObjectNode payload = mapper.createObjectNode();
        payload.put("event_id", event.eventId().toString());
        payload.put("event_sequence", event.sequence());
        payload.put("severity", event.severity());
        payload.put("vibration_pattern", PATTERNS.get(event.severity()));
        payload.put("word", word);
        payload.put("ack_within_ms", ackWithinMs);
        payload.put("needs_check", event.needsCheck());
        ObjectNode message = I1Envelope.outbound(mapper, now.toEpochMilli(), "watch_alert", ALERT_VERSION, payload);
        message.put("msg_id", msgId.toString()); // 알림 ID = 봉투 msg_id(watch_ack.alert_msg_id 로 돌아온다)
        if (hub.send(deviceId, message)) {
            log.info("워치 알림 보냄: event={} device={} severity={}", event.eventId(), deviceId, event.severity());
        } else {
            escalate("""
                    UPDATE watch_alert SET escalated_at = :now, escalation_reason = 'SEND_FAILED'
                    WHERE alert_id = :id AND escalated_at IS NULL AND ack_received_at IS NULL AND confirm_received_at IS NULL
                    """ + ESCALATED_COLUMNS, new MapSqlParameterSource("id", inserted.getFirst()));
        }
    }

    /** watch_ack 를 받은 WebSocket 스레드를 DB 가 붙잡지 않게 보내기 스레드로 넘긴다(느린 DB 가 그 워치의 하트비트 처리를 밀지 않게) */
    @EventListener
    void onAck(WatchAckReceived ack) {
        try {
            sender.execute(() -> record(ack));
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이다
        }
    }

    /** watch_ack 기록. 이 워치에 보낸 알림만 고친다(다른 워치의 alert_msg_id 로는 못 고친다) */
    private void record(WatchAckReceived ack) {
        JsonNode p = ack.envelope().payload();
        UUID alertMsgId;
        try {
            alertMsgId = UUID.fromString(p.path("alert_msg_id").asString(""));
        } catch (IllegalArgumentException e) {
            return; // 계약 검사를 지나 오지 않는다
        }
        boolean vibrated = "VIBRATED".equals(p.path("kind").asString(null));
        String sql = vibrated
                ? "UPDATE watch_alert SET acked_at = COALESCE(acked_at, :acked), ack_received_at = COALESCE(ack_received_at, :received)"
                : "UPDATE watch_alert SET confirmed_at = COALESCE(confirmed_at, :acked), confirm_received_at = COALESCE(confirm_received_at, :received)";
        try {
            int rows = jdbc.update(sql + " WHERE alert_msg_id = :id AND device_id = :device",
                    new MapSqlParameterSource().addValue("id", alertMsgId).addValue("device", ack.deviceId())
                            .addValue("acked", utc(Instant.ofEpochMilli(p.path("acked_at").asLong())))
                            .addValue("received", utc(ack.receivedAt())));
            if (rows == 0) {
                log.warn("모르는 알림의 watch_ack 를 버림: device={} kind={}", ack.deviceId(), p.path("kind").asString(""));
            }
        } catch (DataAccessException e) {
            log.warn("watch_ack 를 기록하지 못함: device={} error={}", ack.deviceId(), e.getClass().getSimpleName());
        }
    }

    /** 기한이 지난 알림을 격상한다. 이 스레드(watch-escalation)에서만 부른다. 시험은 직접 부른다 */
    void escalateOverdue() {
        try {
            escalate("""
                    UPDATE watch_alert w SET escalated_at = :now, escalation_reason = 'NO_ACK'
                    FROM risk_event r
                    WHERE r.event_id = w.event_id AND r.source = w.source AND r.lifecycle <> 'RESOLVED'
                      AND w.ack_received_at IS NULL AND w.confirm_received_at IS NULL AND w.escalated_at IS NULL
                      AND w.sent_at + w.ack_within_ms * interval '1 millisecond' <= :now
                      AND w.sent_at > CAST(:now AS timestamptz) - interval '%s'
                    """.formatted(ESCALATION_MAX_AGE) + """
                    RETURNING w.event_id, w.event_sequence, w.alert_msg_id, w.device_id, w.world_id, w.severity, w.sent_at,
                        w.escalated_at, w.escalation_reason""", new MapSqlParameterSource());
        } catch (RuntimeException e) {
            log.warn("워치 알림 격상 판정 실패", e);
        }
    }

    /** UPDATE … RETURNING 으로 격상한 행마다 관리자 화면에 알린다. 행을 먼저 바꿔 한 알림에 한 번만 나간다 */
    private void escalate(String sql, MapSqlParameterSource params) {
        params.addValue("now", utc(clock.instant()));
        List<ObjectNode> escalated = jdbc.query(sql, params, (rs, i) -> {
            ObjectNode payload = mapper.createObjectNode();
            payload.put("event_id", rs.getObject("event_id", UUID.class).toString());
            payload.put("event_sequence", rs.getInt("event_sequence"));
            payload.put("alert_msg_id", rs.getObject("alert_msg_id", UUID.class).toString());
            payload.put("device_id", rs.getString("device_id"));
            payload.put("world_id", rs.getString("world_id"));
            payload.put("severity", rs.getString("severity"));
            payload.put("reason", rs.getString("escalation_reason"));
            payload.put("sent_at", rs.getTimestamp("sent_at").toInstant().toEpochMilli());
            payload.put("escalated_at", rs.getTimestamp("escalated_at").toInstant().toEpochMilli());
            return payload;
        });
        for (ObjectNode payload : escalated) {
            log.info("워치 알림 격상: event={} device={} reason={}", payload.path("event_id").asString(),
                    payload.path("device_id").asString(), payload.path("reason").asString());
            publisher.publish(RealtimeTopics.WATCH, "watch_escalation", ESCALATION_VERSION, payload);
        }
    }

    private static RowMapper<LiveEvent> liveEvent() {
        return (rs, i) -> {
            Array array = rs.getArray("world_ids");
            List<String> worldIds;
            try {
                worldIds = array == null ? List.of() : List.of((String[]) array.getArray());
            } catch (SQLException e) {
                worldIds = List.of();
            }
            return new LiveEvent(rs.getObject("event_id", UUID.class), rs.getInt("event_sequence"), rs.getInt("resume_count"),
                    rs.getString("severity"), rs.getBoolean("needs_check"), rs.getString("rule_id"), worldIds);
        };
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    @Override
    public void close() {
        sender.shutdownNow();
        escalator.shutdownNow();
    }
}
