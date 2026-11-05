package com.ssafy.simai.domain.watch;

import com.ssafy.simai.domain.health.HealthState;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.io.IOException;
import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.ConcurrentWebSocketSessionDecorator;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 워치 I4 연결과 연결 상태(명세 부록 C.6, FR-44, 계약 watch_heartbeat, S15P31A101-336, 설계 docs/design/server-watch-link.md).
 * <ul>
 *   <li>워치 하나에 연결 하나. 같은 워치가 다시 붙으면 옛 연결을 REPLACED 로 닫는다(워치는 끊긴 줄 모르고 새로 붙을 수 있다)
 *   <li>watch_heartbeat 를 받으면 바로 echo(받은 seq·EC2 수신 시각)와 interval_ms 를 실어 답한다. 워치는 echo 로 EC2 생존과 시계 차이를 본다
 *   <li>끊김: 마지막 하트비트(연결 직후는 연결 시각)에서 interval × 3 이 지나면 LOST 로 보고 연결을 닫는다(워치가 다시 붙게).
 *       받는 쪽 단조 시계(System.nanoTime)로 잰다(FR-11, 노트북 하트비트와 같다)
 *   <li>하트비트마다 이 연결을 연 토큰이 아직 유효한지 본다. 연결 중 폐기·회전되면 TOKEN_INVALID 로 닫는다(관리 명령은 다른 프로세스라
 *       알림을 받을 수 없다. 늦어도 하트비트 한 주기 안)
 *   <li>상태가 바뀌면 HealthState 에 알려 viewer_health.watches 를 바로 보낸다
 * </ul>
 * 메시지 하나가 틀리면 버리고 연결은 둔다(Tier 0 I1 과 같은 까닭: 끊으면 다시 붙어 같은 것을 되풀이한다). 계속 틀리면 하트비트가 없으니
 * 끊김 판정이 닫는다. 로그에는 기기 ID·msg_type 만 남기고 payload·토큰은 남기지 않는다.
 * watch_ack 는 계약 검사 뒤 WatchAckReceived 로 넘긴다(WatchAlertService, -338). gait_features 는 받기만 한다(저장·배정은 -340).
 * 워치로 보내기(send)는 다른 스레드(WatchAlertService)에서도 부른다. 세션마다 ConcurrentWebSocketSessionDecorator 가 보내기를 줄 세운다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WatchHub implements AutoCloseable {

    static final String SCHEMA_VERSION = "1.0.0";
    static final CloseStatus HEARTBEAT_TIMEOUT = new CloseStatus(4000, "heartbeat_timeout");
    static final CloseStatus REPLACED = new CloseStatus(4001, "replaced");
    static final CloseStatus TOKEN_INVALID = new CloseStatus(4003, "token_invalid");

    private static final Logger log = LoggerFactory.getLogger(WatchHub.class);
    private static final int SEND_TIME_LIMIT_MS = 10_000;
    private static final int SEND_BUFFER_BYTES = 64 * 1024;

    /** viewer_health.watches 의 항목 하나. connected 가 아니면 battery·transport 는 null */
    public record WatchStatus(String deviceId, boolean connected, Instant lastHeartbeatAt, Integer batteryPct, String transport) {
    }

    private static final class Link {
        final WebSocketSession session;
        final String sessionId;
        final String deviceId;
        final String tokenHash;
        final long connectionId;
        final AtomicLong seq = new AtomicLong();
        volatile long lastSeenNanos;
        volatile Instant lastHeartbeatAt;
        volatile Integer batteryPct;
        volatile String transport;
        volatile String appVersion;
        /** watch_connection 에 적는 데 성공한 값. 실패하면 다음 하트비트에 다시 적는다 */
        String recordedTransport;
        String recordedAppVersion;
        boolean recorded;
        boolean ended;

        Link(WebSocketSession session, String sessionId, String deviceId, String tokenHash, long connectionId, long nowNanos) {
            this.session = session;
            this.sessionId = sessionId;
            this.deviceId = deviceId;
            this.tokenHash = tokenHash;
            this.connectionId = connectionId;
            this.lastSeenNanos = nowNanos;
        }
    }

    private final WatchRepository repository;
    private final WatchProperties properties;
    private final HealthState health;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ApplicationEventPublisher events;
    private final Map<String, Link> byDevice = new ConcurrentHashMap<>();
    private final Map<String, Link> bySession = new ConcurrentHashMap<>();
    /** 워치별 연결 잠금. 같은 워치가 겹쳐 붙어도 DB 의 열린 행과 byDevice 가 같은 연결을 가리키게 open 과 교체를 함께 한다 */
    private final Map<String, Object> deviceLocks = new ConcurrentHashMap<>();
    private volatile boolean shuttingDown;
    private final ScheduledExecutorService sweeper = Executors.newSingleThreadScheduledExecutor(
            Thread.ofPlatform().name("watch-sweeper").daemon().factory());

    WatchHub(WatchRepository repository, WatchProperties properties, HealthState health, ContractSchemas schemas,
            ObjectMapper mapper, Clock clock, ApplicationEventPublisher events) {
        this.events = events;
        this.repository = repository;
        this.properties = properties;
        this.health = health;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
        try {
            int closed = repository.closeAllOpen();
            if (closed > 0) {
                log.info("EC2 재시작 전 열린 워치 연결 기록을 닫음: {}건", closed);
            }
        } catch (DataAccessException e) {
            log.warn("열린 워치 연결 기록을 닫지 못함: {}", e.getClass().getSimpleName());
        }
        long period = properties.sweepInterval().toMillis();
        sweeper.scheduleWithFixedDelay(this::sweep, period, period, TimeUnit.MILLISECONDS);
    }

    void connected(WebSocketSession raw, String deviceId, String tokenHash) {
        if (deviceId == null || tokenHash == null) {
            closeQuietly(raw, CloseStatus.POLICY_VIOLATION);
            return;
        }
        Link previous;
        synchronized (deviceLocks.computeIfAbsent(deviceId, id -> new Object())) {
            long connectionId;
            try {
                connectionId = repository.open(deviceId, clock.instant());
            } catch (DataAccessException e) {
                log.warn("워치 연결을 기록하지 못해 닫음: device={} error={}", deviceId, e.getClass().getSimpleName());
                closeQuietly(raw, CloseStatus.SERVICE_RESTARTED);
                return;
            }
            WebSocketSession session = new ConcurrentWebSocketSessionDecorator(raw, SEND_TIME_LIMIT_MS, SEND_BUFFER_BYTES);
            Link link = new Link(session, raw.getId(), deviceId, tokenHash, connectionId, System.nanoTime());
            bySession.put(raw.getId(), link);
            previous = byDevice.put(deviceId, link);
        }
        if (previous != null) {
            // DB 행은 open 이 이미 REPLACED 로 닫았다
            end(previous, null, REPLACED);
        }
        log.info("워치 연결: device={} session={}", deviceId, raw.getId());
        health.watchesChanged();
        // 지금 배정 상태를 한 번 보내게 한다(WatchBindingNotifier, S15P31A101-510)
        events.publishEvent(new WatchConnected(deviceId));
    }

    void received(WebSocketSession raw, String text) {
        Link link = bySession.get(raw.getId());
        if (link == null) {
            return;
        }
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(mapper.readTree(text));
        } catch (InvalidI1Message | JacksonException e) {
            log.warn("워치 봉투를 읽지 못해 버림: device={} reason={}", link.deviceId,
                    e instanceof InvalidI1Message ? e.getMessage() : e.getClass().getSimpleName());
            return;
        }
        if (envelope.major() != 1 || !link.deviceId.equals(envelope.sourceId())) {
            log.warn("워치 메시지를 버림(판 또는 source_id 가 다르다): device={} msg_type={}", link.deviceId, envelope.msgType());
            return;
        }
        switch (envelope.msgType()) {
            case "watch_heartbeat" -> {
                List<String> violations = schemas.violations("messages/watch_heartbeat", text);
                if (!violations.isEmpty()) {
                    log.warn("watch_heartbeat 계약 위반이라 버림: device={} violations={}", link.deviceId, violations.size());
                    return;
                }
                heartbeat(link, envelope);
            }
            case "watch_ack" -> {
                List<String> violations = schemas.violations("messages/watch_ack", text);
                if (!violations.isEmpty()) {
                    log.warn("watch_ack 계약 위반이라 버림: device={} violations={}", link.deviceId, violations.size());
                    return;
                }
                events.publishEvent(new WatchAckReceived(link.deviceId, envelope, clock.instant()));
            }
            case "gait_features" -> {
                List<String> violations = schemas.violations("messages/gait_features", text);
                if (!violations.isEmpty()) {
                    log.warn("gait_features 계약 위반이라 버림: device={} violations={}", link.deviceId, violations.size());
                    return;
                }
                // 저장과 움직임 대조는 WatchMotionMatcher 가 자기 스레드에서 한다(-340)
                events.publishEvent(new GaitFeaturesReceived(link.deviceId, envelope.payload(), clock.instant()));
            }
            default -> log.warn("모르는 워치 메시지를 버림: device={} msg_type={}", link.deviceId, envelope.msgType());
        }
    }

    void closed(WebSocketSession raw, CloseStatus status) {
        Link link = bySession.get(raw.getId());
        if (link != null) {
            // 송신이 밀려 데코레이터가 닫았으면(SESSION_NOT_RELIABLE) EC2 쪽 원인, EC2 종료 중이면 재시작으로 적는다
            String reason = shuttingDown ? "EC2_RESTART"
                    : status.getCode() == CloseStatus.SESSION_NOT_RELIABLE.getCode() ? "SEND_FAILED" : "CLOSED";
            end(link, reason, null);
            log.info("워치 연결 끊김: device={} status={}", link.deviceId, status);
        }
    }

    private void heartbeat(Link link, I1Envelope envelope) {
        Instant now = clock.instant();
        try {
            if (!repository.touch(link.deviceId, link.tokenHash, now)) {
                log.warn("연결 중 워치 토큰이 폐기·회전돼 닫음: device={}", link.deviceId);
                end(link, "TOKEN_INVALID", TOKEN_INVALID);
                return;
            }
        } catch (DataAccessException e) {
            // DB 가 잠깐 안 돼도 개인 알림 채널은 살려 둔다(토큰은 업그레이드 때 확인했다). 다음 하트비트에 다시 본다
            log.warn("워치 하트비트를 기록하지 못함: device={} error={}", link.deviceId, e.getClass().getSimpleName());
        }
        JsonNode p = envelope.payload();
        JsonNode watch = p.path("watch");
        String transport = watch.path("transport").asString(null);
        String appVersion = watch.path("app_version").asString(null);
        boolean described = link.recorded && Objects.equals(transport, link.recordedTransport)
                && Objects.equals(appVersion, link.recordedAppVersion);
        link.lastSeenNanos = System.nanoTime();
        link.lastHeartbeatAt = now;
        link.batteryPct = watch.path("battery_pct").asInt();
        link.transport = transport;
        link.appVersion = appVersion;
        if (!described) {
            try {
                repository.describe(link.connectionId, now, transport, appVersion);
                link.recorded = true;
                link.recordedTransport = transport;
                link.recordedAppVersion = appVersion;
            } catch (DataAccessException e) {
                log.warn("워치 연결 정보를 기록하지 못함: device={} error={}", link.deviceId, e.getClass().getSimpleName());
            }
        }
        // 하트비트로는 워치별 link 가 바뀌지 않는다(연결 직후부터 CONNECTED). 배터리·수신 시각은 1초 주기에 실린다
        reply(link, p.path("heartbeat_seq").asLong(), now);
    }

    private void reply(Link link, long receivedSeq, Instant receivedAt) {
        ObjectNode payload = mapper.createObjectNode();
        payload.put("heartbeat_seq", link.seq.getAndIncrement());
        ObjectNode echo = payload.putObject("echo");
        echo.put("seq", receivedSeq);
        echo.put("received_at", receivedAt.toEpochMilli());
        payload.put("interval_ms", properties.intervalMs());
        ObjectNode message = I1Envelope.outbound(mapper, clock.millis(), "watch_heartbeat", SCHEMA_VERSION, payload);
        try {
            link.session.sendMessage(new TextMessage(mapper.writeValueAsString(message)));
        } catch (IOException | RuntimeException e) {
            // 보내지 못하면 워치가 echo 3회 누락으로 다시 붙는다
            log.warn("워치 하트비트 답을 보내지 못함: device={} error={}", link.deviceId, e.getClass().getSimpleName());
        }
    }

    /** 이 스레드(watch-sweeper)에서만 부른다 */
    private void sweep() {
        try {
            long now = System.nanoTime();
            for (Link link : byDevice.values()) {
                if (now - link.lastSeenNanos > properties.timeoutNanos()) {
                    log.info("워치 하트비트 {}회 누락이라 끊김(LOST)으로 보고 닫음: device={}", WatchProperties.MISSED_HEARTBEATS,
                            link.deviceId);
                    // 반쯤 열린 TCP 에 닫기 프레임을 쓰거나 DB 가 느려도 다른 워치의 판정이 밀리지 않게 따로 닫는다(end 는 한 번만 돈다)
                    Thread.ofVirtual().name("watch-end-" + link.deviceId).start(() -> end(link, "HEARTBEAT_TIMEOUT", HEARTBEAT_TIMEOUT));
                }
            }
        } catch (RuntimeException e) {
            log.warn("워치 끊김 판정 실패", e);
        }
    }

    /**
     * 연결을 끝낸다(한 번만). reason 이 null 이면 DB 행은 다른 곳(open 의 REPLACED)이 닫았다. closeStatus 가 null 이면 이미 닫혔다
     */
    private void end(Link link, String reason, CloseStatus closeStatus) {
        synchronized (link) {
            if (link.ended) {
                return;
            }
            link.ended = true;
        }
        byDevice.remove(link.deviceId, link);
        bySession.remove(link.sessionId, link);
        if (reason != null) {
            try {
                repository.close(link.connectionId, clock.instant(), reason, link.lastHeartbeatAt);
            } catch (DataAccessException e) {
                log.warn("워치 연결 끝을 기록하지 못함: device={} error={}", link.deviceId, e.getClass().getSimpleName());
            }
        }
        if (closeStatus != null) {
            closeQuietly(link.session, closeStatus);
        }
        health.watchesChanged();
    }

    /** 그 워치가 지금 CONNECTED 인가(연결이 열려 있고 하트비트 기한 안). viewer_health 의 link 와 같은 기준 */
    public boolean isConnected(String deviceId) {
        Link link = byDevice.get(deviceId);
        return link != null && link.session.isOpen() && System.nanoTime() - link.lastSeenNanos <= properties.timeoutNanos();
    }

    /**
     * 그 워치의 지금 연결로 메시지 하나를 보낸다(봉투까지 만든 것).
     *
     * @return 보냈으면 true. 연결이 없거나(LOST) 보내다 실패하면 false
     */
    public boolean send(String deviceId, ObjectNode message) {
        Link link = byDevice.get(deviceId);
        if (link == null || !link.session.isOpen()) {
            return false;
        }
        try {
            link.session.sendMessage(new TextMessage(mapper.writeValueAsString(message)));
            return true;
        } catch (IOException | RuntimeException e) {
            log.warn("워치로 보내지 못함: device={} msg_type={} error={}", deviceId, message.path("msg_type").asString(""),
                    e.getClass().getSimpleName());
            return false;
        }
    }

    /** 등록된 워치(폐기 안 됨)마다 지금 상태, device_id 오름차순. viewer_health 가 1초마다 부른다 */
    public List<WatchStatus> statuses() {
        long now = System.nanoTime();
        List<WatchStatus> out = new ArrayList<>();
        for (WatchRepository.RegisteredWatch watch : repository.registered()) {
            Link link = byDevice.get(watch.deviceId());
            boolean connected = link != null && link.session.isOpen() && now - link.lastSeenNanos <= properties.timeoutNanos();
            Instant last = link != null && link.lastHeartbeatAt != null ? link.lastHeartbeatAt : watch.lastHeartbeatAt();
            out.add(connected
                    ? new WatchStatus(watch.deviceId(), true, last, link.batteryPct, link.transport)
                    : new WatchStatus(watch.deviceId(), false, last, null, null));
        }
        return out;
    }

    private static void closeQuietly(WebSocketSession session, CloseStatus status) {
        try {
            session.close(status);
        } catch (IOException | RuntimeException e) {
            // 이미 닫혔다
        }
    }

    /** EC2 를 정상 종료하면 열린 연결을 EC2_RESTART 로 닫는다(DB 가 아직 살아 있을 때, 이 빈이 리포지토리보다 먼저 내려간다) */
    @Override
    public void close() {
        shuttingDown = true;
        sweeper.shutdownNow();
        for (Link link : List.copyOf(byDevice.values())) {
            end(link, "EC2_RESTART", CloseStatus.GOING_AWAY);
        }
    }
}
