package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.health.HealthState;
import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.rule.RuleCatalog;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.regex.Pattern;
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
 * 해소(S15P31A101-634): 보낸 알림의 이벤트가 해소되면 그 워치에 watch_alert_clear 를 한 번 보내 알림을 지우게 하고(cleared_at), 끊긴 워치는
 * 다시 이어질 때 보낸다. 워치가 (다시) 연결되거나 배정되면 그 사람의 열린 실시간 이벤트 중 못 보낸 것을 보낸다(최근 10분 갱신분).
 * 재동기화에서 감사 전용(AUDIT_ONLY)이 된 이벤트는 보내지 않는다.
 * 해소된 이벤트·10분 넘은 알림은 격상하지 않는다. CONFIRMED 만 와도(VIBRATED 를 잃어도) 받은 것으로 본다. 보내기는 송신 버퍼에 넣으면
 * 끝나 반쯤 끊긴 연결로 보낸 것은 SEND_FAILED 가 아니라 기한 뒤 NO_ACK 로 잡힌다. 해소 뒤 다시 활성(resumed)이면 새로 알린다.
 * 워치 ACK·격상은 운영자 확인(3.2)이 아니고 이벤트 상태를 바꾸지 않는다. CRITICAL·HIGH 의 현장 경보는 Tier 0·노트북이 이미 울린다.
 * <p>
 * 수동 알림(S15P31A101-475, 명세 밖 운영 입구): 관리자·반장이 POST /api/watch-alerts 로 워치 하나에 등급·한 단어(·방향)를 직접 보낸다(비상 호출,
 * 시연·시험). 위험 이벤트가 없으므로 origin MANUAL 로 같은 표에 남기고(event_id 는 그 알림만의 UUIDv7, world_id 는 배정이 있으면 그 값),
 * ACK 기록·기한 초과 NO_ACK 격상은 이벤트 알림과 같은 경로를 탄다. 연결되지 않은 워치에는 보내지 않는다(409).
 * <p>
 * 시험 입력(S15P31A101-724, AGENTS 규칙 5): 노트북 하트비트가 녹화 파일·합성 영상 입력을 알리는 동안(HealthState.testInputActive) 받은 이벤트는
 * 워치로 보내지 않는다(Tier 0 는 재생 영상을 LIVE 로 판정한다). 그 이벤트 ID 를 메모리에 기억해 실제 카메라로 돌아온 뒤 갱신·재동기화로 다시 와도
 * 보내지 않는다(EC2 재시작 뒤에는 잊는다: 재동기화는 최근 10분 갱신분만 보내므로 남는 틈은 재시작 직후 10분이다). 기록·화면 표시는 그대로다.
 * 수동 알림은 사람이 직접 보내는 것이라 막지 않는다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchAlertService implements AutoCloseable {

    static final String ALERT_VERSION = "1.0.0";
    static final String CLEAR_VERSION = "1.0.0";
    static final String ESCALATION_VERSION = "1.1.0";
    /**
     * 워치가 (다시) 연결되거나 배정될 때 다시 보낼 열린 이벤트의 범위: 이 시간 안에 갱신된 것만(S15P31A101-634). 오래 열린 채 남은 지난 관제의
     * 이벤트를 새로 붙은 워치에 한꺼번에 울리지 않게 한다. 격상 판정의 10분과 같은 값이다
     */
    static final String RESEND_MAX_AGE = "10 minutes";

    /** 수동 알림 요청(컨트롤러가 JSON 을 옮긴 값. 검증은 sendManual 이 한다) */
    public record ManualRequest(String deviceId, String severity, String word, String direction, Integer ackWithinMs, Boolean needsCheck) {
    }

    /** watch_alert 한 행(조회 API·수동 발송 응답). 시각은 EC2 시계, acked_at·confirmed_at 만 워치 시계 */
    public record AlertRow(long alertId, UUID alertMsgId, String origin, UUID eventId, int eventSequence, String deviceId, String worldId,
            String severity, String vibrationPattern, String direction, String word, boolean needsCheck, int ackWithinMs, Instant sentAt,
            Instant ackedAt, Instant ackReceivedAt, Instant confirmedAt, Instant confirmReceivedAt, Instant escalatedAt,
            String escalationReason, Long requestedByUserId) {
    }

    private static final Set<String> DIRECTIONS = Set.of("FRONT", "BACK", "LEFT", "RIGHT");
    /** 계약 watch_alert.word: 앞뒤 공백 없이(가운데 공백·줄바꿈도 없이) 1~8자(코드포인트) */
    private static final Pattern WORD = Pattern.compile("^\\S(.*\\S)?$");
    private static final int MAX_WORD_LENGTH = 8;
    /** 수동 알림 확인 기한. 계약 하한은 1 ms 지만 왕복보다 짧은 기한은 정상 전송도 격상되므로 1초부터 받는다 */
    private static final int MIN_ACK_WITHIN_MS = 1_000;
    private static final int MAX_ACK_WITHIN_MS = 600_000;
    private static final int MAX_LIST = 200;

    private static final Logger log = LoggerFactory.getLogger(WatchAlertService.class);

    /** 등급 → 진동 패턴(FR-45, 계약에서 생략할 때 워치가 쓰는 것과 같다. 표에는 늘 적는다) */
    private static final Map<String, String> PATTERNS = Map.of(
            "CRITICAL", "DANGER_LONG", "HIGH", "CAUTION_DOUBLE", "CAUTION", "CAUTION_WEAK");

    private static final String RANK_SQL = "CASE %s WHEN 'CAUTION' THEN 1 WHEN 'HIGH' THEN 2 WHEN 'CRITICAL' THEN 3 ELSE 0 END";

    private static final String ESCALATED_COLUMNS = """
            RETURNING origin, event_id, event_sequence, alert_msg_id, device_id, world_id, severity, sent_at, escalated_at, escalation_reason""";

    private static final String ROW_COLUMNS = """
            alert_id, alert_msg_id, origin, event_id, event_sequence, device_id, world_id, severity, vibration_pattern, direction, word,
            needs_check, ack_within_ms, sent_at, acked_at, ack_received_at, confirmed_at, confirm_received_at, escalated_at,
            escalation_reason, requested_by_user_id""";

    private record LiveEvent(UUID eventId, int sequence, int resumeCount, String severity, boolean needsCheck, String ruleId,
            List<String> worldIds) {
    }

    /**
     * 기한이 지난 뒤 이만큼 더 지난 대기 알림은 격상하지 않는다(EC2 가 오래 내려갔다 뜰 때 지난 위험을 한꺼번에 알리지 않게, 알림 대기열 max-age 와
     * 같은 10분). 기한(ack_within_ms) 뒤부터 세므로 기한이 긴 수동 알림(최대 10분)도 격상된다
     */
    private static final String ESCALATION_MAX_AGE = "10 minutes";

    /** 시험 입력 중에 받아 워치로 보내지 않은 이벤트를 기억하는 개수(오래된 것부터 잊는다) */
    static final int TEST_INPUT_MEMORY = 10_000;

    private final NamedParameterJdbcTemplate jdbc;
    private final WatchBindings bindings;
    private final WatchHub hub;
    private final WatchProperties properties;
    private final RealtimePublisher publisher;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final HealthState health;
    /** 시험 입력 중에 받은 이벤트(S15P31A101-724). 실제 카메라로 돌아온 뒤에도 보내지 않는다 */
    private final Set<UUID> testInputEvents = Collections.synchronizedSet(Collections.newSetFromMap(new LinkedHashMap<>() {
        @Override
        protected boolean removeEldestEntry(Map.Entry<UUID, Boolean> eldest) {
            return size() > TEST_INPUT_MEMORY;
        }
    }));
    /** 보내기는 이 스레드 하나에서 한다. I1 수신·커밋 스레드를 붙잡지 않고, 같은 이벤트의 전이가 받은 순서대로 나간다 */
    private final ExecutorService sender = Executors.newSingleThreadExecutor(Thread.ofPlatform().name("watch-alert").daemon().factory());
    private final ScheduledExecutorService escalator = Executors.newSingleThreadScheduledExecutor(
            Thread.ofPlatform().name("watch-escalation").daemon().factory());

    WatchAlertService(NamedParameterJdbcTemplate jdbc, WatchBindings bindings, WatchHub hub, WatchProperties properties,
            RealtimePublisher publisher, ObjectMapper mapper, Clock clock, HealthState health) {
        this.jdbc = jdbc;
        this.bindings = bindings;
        this.hub = hub;
        this.properties = properties;
        this.publisher = publisher;
        this.mapper = mapper;
        this.clock = clock;
        this.health = health;
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

    /**
     * 이 스레드(watch-alert)에서만 부른다. 시험은 직접 부른다.
     * 해소된 LIVE 이벤트면 그 이벤트로 알림을 받은 워치에 지우라는 통지를 보낸다(S15P31A101-634). 재동기화에서 근거 없이 감사 전용(AUDIT_ONLY)으로
     * 분류된 이벤트는 실시간 알림이 아니므로 ACK·요약 갱신으로 다시 와도 보내지 않는다(명세 3.2, 11.3).
     */
    void alert(UUID eventId) {
        Optional<LiveEvent> found = jdbc.query("""
                SELECT event_id, event_sequence, resume_count, severity, needs_check, rule_id, world_ids FROM risk_event
                WHERE event_id = :id AND source = 'LIVE' AND lifecycle <> 'RESOLVED' AND severity IN ('CRITICAL', 'HIGH', 'CAUTION')
                  AND event_sequence >= 1 AND delivery IS DISTINCT FROM 'AUDIT_ONLY'
                """, new MapSqlParameterSource("id", eventId), liveEvent()).stream().findFirst();
        if (found.isEmpty()) {
            clearIfResolved(eventId);
            return;
        }
        if (found.get().worldIds().isEmpty() || suppressedForTestInput(eventId)) {
            return;
        }
        LiveEvent event = found.get();
        bindings.devicesFor(event.worldIds()).forEach((worldId, device) -> {
            if (hub.isConnected(device)) { // LOST: 워치로 보내지 않는다(기존 경로). 배정 없는 사람(UNBOUND)은 목록에 없다
                send(event, worldId, device);
            }
        });
    }

    /** 해소된 LIVE 이벤트면, 그 이벤트로 알림을 받았고 아직 지우지 않은 연결된 워치마다 watch_alert_clear 를 한 번 보낸다 */
    private void clearIfResolved(UUID eventId) {
        List<String> devices = jdbc.queryForList("""
                SELECT DISTINCT w.device_id FROM watch_alert w JOIN risk_event r ON r.event_id = w.event_id AND r.source = w.source
                WHERE w.event_id = :id AND w.origin = 'EVENT' AND w.cleared_at IS NULL AND r.lifecycle = 'RESOLVED'
                """, new MapSqlParameterSource("id", eventId), String.class);
        for (String device : devices) {
            if (hub.isConnected(device)) { // 끊긴 워치는 다시 이어질 때(resync) 보낸다
                sendClear(eventId, device);
                // -723: 이 이벤트에 묶여 진동하지 않은 같은 사람의 열린 하위 이벤트가 있으면 이제 보낸다(이미 보낸 것은 send 가 거른다)
                resync(device);
            }
        }
    }

    /**
     * 워치 하나에 지우라는 통지를 보내고, 보냈으면 그 이벤트·워치의 알림 행에 cleared_at 을 적는다. 보내지 못하면 적지 않아 다음 연결 때 다시 보낸다.
     * 작업자 확인(ACK)이 아니고 워치는 이 통지에 ACK 하지 않는다. 이미 지운 이벤트는 워치가 무시한다(계약).
     */
    private void sendClear(UUID eventId, String deviceId) {
        Instant now = clock.instant();
        ObjectNode payload = mapper.createObjectNode();
        payload.put("event_id", eventId.toString());
        payload.put("reason", "RESOLVED");
        payload.put("sent_at", now.toEpochMilli());
        ObjectNode message = I1Envelope.outbound(mapper, now.toEpochMilli(), "watch_alert_clear", CLEAR_VERSION, payload);
        if (!hub.send(deviceId, message)) {
            return;
        }
        jdbc.update("""
                UPDATE watch_alert SET cleared_at = :now
                WHERE event_id = :id AND device_id = :device AND origin = 'EVENT' AND cleared_at IS NULL
                """, new MapSqlParameterSource().addValue("id", eventId).addValue("device", deviceId).addValue("now", utc(now)));
        log.info("워치 알림 지움 통지 보냄: event={} device={}", eventId, deviceId);
    }

    /** 워치가 (다시) 연결됐다(S15P31A101-634). 그사이 해소된 알림을 지우고, 배정된 사람의 열린 이벤트 중 못 보낸 것을 보낸다 */
    @EventListener
    void onConnected(WatchConnected connected) {
        resyncLater(connected.deviceId());
    }

    /** 워치에 사람이 배정됐다(S15P31A101-634). 배정 전에 난 그 사람의 열린 이벤트를 보낸다. 풀린 쪽(사유 있음)은 할 일이 없다 */
    @TransactionalEventListener(fallbackExecution = true)
    void onBindingChanged(WatchBindingChanged changed) {
        if (changed.unbindReason() == null) {
            resyncLater(changed.deviceId());
        }
    }

    private void resyncLater(String deviceId) {
        try {
            sender.execute(() -> {
                try {
                    resync(deviceId);
                } catch (RuntimeException e) {
                    log.warn("워치 재동기화 실패: device={}", deviceId, e);
                }
            });
        } catch (RejectedExecutionException e) {
            // 서버가 내려가는 중이다
        }
    }

    /**
     * 이 스레드(watch-alert)에서만 부른다. 시험은 직접 부른다.
     * <ol>
     *   <li>이 워치에 보냈는데 그사이 해소된 이벤트: 지우라는 통지
     *   <li>이 워치에 배정된 사람의 열린 실시간 LIVE 이벤트(최근 RESEND_MAX_AGE 안에 갱신): 아직 보내지 않았으면 보낸다. 이미 보낸 것은 send 의 중복 방지로
     *       다시 울리지 않는다(같거나 높은 등급 행이 있으면 넣지 않는다)
     * </ol>
     */
    void resync(String deviceId) {
        if (!hub.isConnected(deviceId)) {
            return;
        }
        List<UUID> resolved = jdbc.queryForList("""
                SELECT DISTINCT w.event_id FROM watch_alert w JOIN risk_event r ON r.event_id = w.event_id AND r.source = w.source
                WHERE w.device_id = :device AND w.origin = 'EVENT' AND w.cleared_at IS NULL AND r.lifecycle = 'RESOLVED'
                """, new MapSqlParameterSource("device", deviceId), UUID.class);
        resolved.forEach(eventId -> sendClear(eventId, deviceId));
        List<String> world = jdbc.queryForList("""
                SELECT world_id FROM watch_binding WHERE device_id = :device AND unbound_at IS NULL
                """, new MapSqlParameterSource("device", deviceId), String.class);
        if (world.isEmpty()) {
            return;
        }
        String worldId = world.getFirst();
        List<LiveEvent> open = jdbc.query("""
                SELECT event_id, event_sequence, resume_count, severity, needs_check, rule_id, world_ids FROM risk_event
                WHERE source = 'LIVE' AND lifecycle <> 'RESOLVED' AND severity IN ('CRITICAL', 'HIGH', 'CAUTION')
                  AND event_sequence >= 1 AND delivery IS DISTINCT FROM 'AUDIT_ONLY' AND :world = ANY(world_ids)
                  AND COALESCE(updated_at, raised_at) > CAST(:now AS timestamptz) - interval '%s'
                ORDER BY %s DESC, COALESCE(updated_at, raised_at)
                """.formatted(RESEND_MAX_AGE, RANK_SQL.formatted("severity")), new MapSqlParameterSource().addValue("world", worldId)
                        .addValue("now", utc(clock.instant())), liveEvent());
        open.stream().filter(event -> !suppressedForTestInput(event.eventId())).forEach(event -> send(event, worldId, deviceId));
    }

    /**
     * 시험 입력 이벤트면 true(보내지 않는다). 지금 시험 입력 중이면 이 이벤트를 기억해 시험 입력이 끝난 뒤에도 보내지 않는다(S15P31A101-724).
     */
    private boolean suppressedForTestInput(UUID eventId) {
        if (health.testInputActive()) {
            if (testInputEvents.add(eventId)) {
                log.info("시험 입력(녹화 파일·합성 영상) 중이라 워치로 보내지 않음: event={}", eventId);
            }
            return true;
        }
        return testInputEvents.contains(eventId);
    }

    private void send(LiveEvent event, String worldId, String deviceId) {
        UUID msgId = UUID.randomUUID();
        Instant now = clock.instant();
        int ackWithinMs = (int) properties.ackWithin().toMillis();
        String word = RuleCatalog.watchWord(event.ruleId());
        // 같은 이벤트·워치에 같거나 높은 등급을 이미 보냈으면 넣지 않는다(재발행·요약 갱신·ACK 로 다시 와도 진동은 한 번, 등급이 오르면 다시).
        // S15P31A101-723: 같은 사람(이 워치)에게 아직 열린 다른 이벤트로 같거나 높은 등급 진동을 이미 보냈고 지우지 않았으면 넣지 않는다
        // (R2 CRITICAL 과 함께 열린 R3 HIGH·R5 CAUTION 은 진동 하나로). 위 이벤트가 해소되면 clearIfResolved 가 resync 로 남은 것을 다시 본다
        List<Long> inserted = jdbc.queryForList("""
                INSERT INTO watch_alert (origin, alert_msg_id, event_id, source, event_sequence, resume_count, device_id, world_id, severity,
                    vibration_pattern, word, needs_check, ack_within_ms, sent_at)
                SELECT 'EVENT', :msgId, :eventId, 'LIVE', :sequence, :resumeCount, :device, :world, :severity, :pattern, :word, :needsCheck,
                    :ackWithin, :now
                WHERE NOT EXISTS (SELECT 1 FROM watch_alert WHERE event_id = :eventId AND resume_count = :resumeCount
                                  AND device_id = :device AND escalation_reason IS DISTINCT FROM 'SEND_FAILED'
                                  AND %1$s >= %2$s)
                  AND NOT EXISTS (SELECT 1 FROM watch_alert w JOIN risk_event r ON r.event_id = w.event_id AND r.source = w.source
                                  WHERE w.device_id = :device AND w.origin = 'EVENT' AND w.event_id <> :eventId
                                    AND w.cleared_at IS NULL AND w.escalation_reason IS DISTINCT FROM 'SEND_FAILED'
                                    AND r.lifecycle <> 'RESOLVED' AND r.resume_count = w.resume_count
                                    AND r.delivery IS DISTINCT FROM 'AUDIT_ONLY' AND %3$s >= %2$s)
                ON CONFLICT DO NOTHING
                RETURNING alert_id
                """.formatted(RANK_SQL.formatted("severity"), RANK_SQL.formatted("CAST(:severity AS text)"),
                        RANK_SQL.formatted("w.severity")),
                new MapSqlParameterSource().addValue("msgId", msgId).addValue("eventId", event.eventId())
                        .addValue("sequence", event.sequence()).addValue("resumeCount", event.resumeCount()).addValue("device", deviceId).addValue("world", worldId)
                        .addValue("severity", event.severity()).addValue("pattern", PATTERNS.get(event.severity()))
                        .addValue("word", word).addValue("needsCheck", event.needsCheck()).addValue("ackWithin", ackWithinMs)
                        .addValue("now", utc(now)), Long.class);
        if (inserted.isEmpty()) {
            return;
        }
        ObjectNode message = alertMessage(msgId, now, event.eventId(), event.sequence(), event.severity(), word, null, ackWithinMs,
                event.needsCheck());
        if (hub.send(deviceId, message)) {
            log.info("워치 알림 보냄: event={} device={} severity={}", event.eventId(), deviceId, event.severity());
        } else {
            sendFailed(inserted.getFirst());
        }
    }

    /**
     * 관리자·반장이 워치 하나에 직접 보내는 알림(POST /api/watch-alerts, S15P31A101-475). 요청 스레드에서 바로 보내고 결과 행을 돌려준다.
     * 등록된 워치가 아니면 WATCH_NOT_FOUND, 연결돼 있지 않거나 보내지 못하면 WATCH_NOT_CONNECTED(보내지 못한 행은 SEND_FAILED 로 남아 격상된다).
     */
    public AlertRow sendManual(ManualRequest request, long userId) {
        String severity = request.severity();
        String word = request.word() == null ? null : request.word().strip();
        String direction = request.direction();
        int ackWithinMs = request.ackWithinMs() == null ? (int) properties.ackWithin().toMillis() : request.ackWithinMs();
        boolean needsCheck = Boolean.TRUE.equals(request.needsCheck());
        if (severity == null || !PATTERNS.containsKey(severity) || word == null || !WORD.matcher(word).matches()
                || word.codePointCount(0, word.length()) > MAX_WORD_LENGTH || (direction != null && !DIRECTIONS.contains(direction))
                || ackWithinMs < MIN_ACK_WITHIN_MS || ackWithinMs > MAX_ACK_WITHIN_MS) {
            throw new BusinessException(ErrorCode.Watch.INVALID_ALERT);
        }
        String deviceId = request.deviceId();
        Integer watches = deviceId == null ? null : jdbc.queryForObject("""
                SELECT count(*) FROM device WHERE device_id = :id AND kind = 'WATCH' AND revoked_at IS NULL
                """, new MapSqlParameterSource("id", deviceId), Integer.class);
        if (watches == null || watches == 0) {
            throw new BusinessException(ErrorCode.Watch.WATCH_NOT_FOUND);
        }
        if (!hub.isConnected(deviceId)) {
            throw new BusinessException(ErrorCode.Watch.WATCH_NOT_CONNECTED);
        }
        // 배정이 있으면 격상 메시지에 익명 World ID 를 싣는다(없어도 보낸다: 수동 알림은 사람이 워치를 고른 것이다)
        String worldId = jdbc.queryForList("""
                SELECT world_id FROM watch_binding WHERE device_id = :device AND unbound_at IS NULL
                """, new MapSqlParameterSource("device", deviceId), String.class).stream().findFirst().orElse(null);
        UUID msgId = UUID.randomUUID();
        UUID eventId = uuid7(); // 이벤트가 없다: 계약 watch_alert.event_id(UUIDv7 필수)를 이 알림만의 ID 로 채운다
        Instant now = clock.instant();
        Long alertId = jdbc.queryForObject("""
                INSERT INTO watch_alert (origin, alert_msg_id, event_id, source, event_sequence, resume_count, device_id, world_id, severity,
                    vibration_pattern, direction, word, needs_check, ack_within_ms, sent_at, requested_by_user_id)
                VALUES ('MANUAL', :msgId, :eventId, NULL, 1, 0, :device, :world, :severity, :pattern, :direction, :word, :needsCheck,
                    :ackWithin, :now, :user)
                RETURNING alert_id
                """, new MapSqlParameterSource().addValue("msgId", msgId).addValue("eventId", eventId).addValue("device", deviceId)
                        .addValue("world", worldId).addValue("severity", severity).addValue("pattern", PATTERNS.get(severity))
                        .addValue("direction", direction).addValue("word", word).addValue("needsCheck", needsCheck)
                        .addValue("ackWithin", ackWithinMs).addValue("now", utc(now)).addValue("user", userId), Long.class);
        long id = alertId == null ? -1 : alertId;
        ObjectNode message = alertMessage(msgId, now, eventId, 1, severity, word, direction, ackWithinMs, needsCheck);
        if (hub.send(deviceId, message)) {
            log.info("수동 워치 알림 보냄: device={} severity={} user={}", deviceId, severity, userId);
        } else {
            sendFailed(id); // 그 사이 끊겼다. 행은 SEND_FAILED 로 남아 관리자 화면에 격상된다(사전 검사의 409 와 구분해 재시도 판단을 맡긴다)
            throw new BusinessException(ErrorCode.Watch.WATCH_SEND_FAILED);
        }
        return jdbc.query("SELECT " + ROW_COLUMNS + " FROM watch_alert WHERE alert_id = :id", new MapSqlParameterSource("id", id), row())
                .getFirst();
    }

    /** 최근 알림(sent_at 내림차순). deviceId 가 null 이면 모든 워치. limit 은 1~200 으로 끌어온다(밖의 값은 거부하지 않는다) */
    public List<AlertRow> list(String deviceId, int limit) {
        int n = Math.max(1, Math.min(MAX_LIST, limit));
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("limit", n);
        // 워치별 조회는 (device_id, sent_at) 색인을 타게 조건을 따로 둔다(OR 로 합치면 generic plan 에서 색인을 못 쓴다)
        String where = deviceId == null ? "" : " WHERE device_id = :device";
        if (deviceId != null) {
            params.addValue("device", deviceId);
        }
        return jdbc.query("SELECT " + ROW_COLUMNS + " FROM watch_alert" + where + " ORDER BY sent_at DESC, alert_id DESC LIMIT :limit",
                params, row());
    }

    /** 워치에 보내는 watch_alert 봉투. 알림 ID = 봉투 msg_id(watch_ack.alert_msg_id 로 돌아온다). direction 은 있을 때만 싣는다 */
    private ObjectNode alertMessage(UUID msgId, Instant now, UUID eventId, int sequence, String severity, String word, String direction,
            int ackWithinMs, boolean needsCheck) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("event_id", eventId.toString());
        payload.put("event_sequence", sequence);
        payload.put("severity", severity);
        payload.put("vibration_pattern", PATTERNS.get(severity));
        if (direction != null) {
            payload.put("direction", direction);
        }
        payload.put("word", word);
        payload.put("ack_within_ms", ackWithinMs);
        payload.put("needs_check", needsCheck);
        ObjectNode message = I1Envelope.outbound(mapper, now.toEpochMilli(), "watch_alert", ALERT_VERSION, payload);
        message.put("msg_id", msgId.toString());
        return message;
    }

    private void sendFailed(long alertId) {
        escalate("""
                UPDATE watch_alert SET escalated_at = :now, escalation_reason = 'SEND_FAILED'
                WHERE alert_id = :id AND escalated_at IS NULL AND ack_received_at IS NULL AND confirm_received_at IS NULL
                """ + ESCALATED_COLUMNS, new MapSqlParameterSource("id", alertId));
    }

    /** 계약 event_id 는 UUIDv7(시각 48비트 + 판 7 + 무작위). 수동 알림의 이벤트 ID 로만 쓴다 */
    static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
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
                    WHERE w.ack_received_at IS NULL AND w.confirm_received_at IS NULL AND w.escalated_at IS NULL
                      AND w.sent_at + w.ack_within_ms * interval '1 millisecond' <= :now
                      AND w.sent_at + w.ack_within_ms * interval '1 millisecond' > CAST(:now AS timestamptz) - interval '%s'
                      AND (w.origin = 'MANUAL' OR EXISTS (SELECT 1 FROM risk_event r
                                                          WHERE r.event_id = w.event_id AND r.source = w.source AND r.lifecycle <> 'RESOLVED'))
                    """.formatted(ESCALATION_MAX_AGE) + """
                    RETURNING w.origin, w.event_id, w.event_sequence, w.alert_msg_id, w.device_id, w.world_id, w.severity, w.sent_at,
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
            payload.put("origin", rs.getString("origin"));
            payload.put("event_id", rs.getObject("event_id", UUID.class).toString());
            payload.put("event_sequence", rs.getInt("event_sequence"));
            payload.put("alert_msg_id", rs.getObject("alert_msg_id", UUID.class).toString());
            payload.put("device_id", rs.getString("device_id"));
            payload.put("world_id", rs.getString("world_id")); // MANUAL 은 배정이 없으면 null(계약 1.1.0)
            payload.put("severity", rs.getString("severity"));
            payload.put("reason", rs.getString("escalation_reason"));
            payload.put("sent_at", rs.getTimestamp("sent_at").toInstant().toEpochMilli());
            payload.put("escalated_at", rs.getTimestamp("escalated_at").toInstant().toEpochMilli());
            return payload;
        });
        for (ObjectNode payload : escalated) {
            log.info("워치 알림 격상: event={} device={} reason={}", payload.path("event_id").asString(),
                    payload.path("device_id").asString(), payload.path("reason").asString());
            try {
                publisher.publish(RealtimeTopics.WATCH, "watch_escalation", ESCALATION_VERSION, payload);
            } catch (RuntimeException e) {
                // 격상 표시는 이미 커밋됐다. 그대로 두면 escalated_at IS NULL 조건에 다시 걸리지 않아 관리자 알림이 영영 안 나가므로
                // 표시를 되돌려 다음 주기에 다시 격상한다
                log.warn("워치 알림 격상 발행 실패라 다음 주기에 다시 한다: alert={}", payload.path("alert_msg_id").asString(), e);
                try {
                    jdbc.update("""
                            UPDATE watch_alert SET escalated_at = NULL, escalation_reason = NULL
                            WHERE alert_msg_id = CAST(:alert AS uuid) AND device_id = :device
                            """, new MapSqlParameterSource("alert", payload.path("alert_msg_id").asString())
                            .addValue("device", payload.path("device_id").asString()));
                } catch (RuntimeException revert) {
                    // 되돌리지 못해도 남은 격상 발행은 이어 간다
                    log.warn("워치 알림 격상 표시를 되돌리지 못함: alert={}", payload.path("alert_msg_id").asString(), revert);
                }
            }
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

    private static RowMapper<AlertRow> row() {
        return (rs, i) -> new AlertRow(rs.getLong("alert_id"), rs.getObject("alert_msg_id", UUID.class), rs.getString("origin"),
                rs.getObject("event_id", UUID.class), rs.getInt("event_sequence"), rs.getString("device_id"), rs.getString("world_id"),
                rs.getString("severity"), rs.getString("vibration_pattern"), rs.getString("direction"), rs.getString("word"),
                rs.getBoolean("needs_check"), rs.getInt("ack_within_ms"), instant(rs, "sent_at"), instant(rs, "acked_at"),
                instant(rs, "ack_received_at"), instant(rs, "confirmed_at"), instant(rs, "confirm_received_at"),
                instant(rs, "escalated_at"), rs.getString("escalation_reason"), rs.getObject("requested_by_user_id", Long.class));
    }

    private static Instant instant(ResultSet rs, String column) throws SQLException {
        Timestamp ts = rs.getTimestamp(column);
        return ts == null ? null : ts.toInstant();
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
