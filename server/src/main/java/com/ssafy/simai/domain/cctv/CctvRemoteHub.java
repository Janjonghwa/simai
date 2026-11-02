package com.ssafy.simai.domain.cctv;

import com.ssafy.simai.global.contract.ContractSchemas;
import jakarta.annotation.PostConstruct;
import jakarta.annotation.PreDestroy;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Instant;
import java.time.YearMonth;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.dao.DataAccessException;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.PingMessage;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.ConcurrentWebSocketSessionDecorator;
import tools.jackson.core.JacksonException;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * CCTV 원격 시청 중계(S15P31A101-400, 설계 docs/design/cctv-remote-view.md). 게이트웨이 영상 중계가 올린 cctv_preview 형식을
 * 저장 없이 관리자 브라우저 여럿에 나눠 준다.
 * <ul>
 * <li>수신(/ws/devices/cctv)은 하나만 둔다. 새로 붙으면 이전 연결을 닫는다. 접속 직후와 수요가 바뀔 때 cctv_remote_control demand 를 보낸다.</li>
 * <li>시청자 0 → 1 이면 바로 수요 on, 마지막 시청자가 나가고 demandLinger(초기 15 s) 뒤에도 0 이면 off.</li>
 * <li>status 는 게이트웨이의 hello 를 받으면 live, 게이트웨이 연결이 없거나 아직 hello 전이면 waiting_source.</li>
 * <li>시청자마다 status → 마지막 hello → 카메라별 마지막 codec → 카메라별 다음 키프레임부터 이진 프레임.
 * 대기열이 넘치면 그 시청자의 그 카메라만 다음 키프레임까지 건너뛴다(CctvViewer).</li>
 * <li>동시 시청 maxViewers(초기 5) 를 넘으면 viewer_limit, 월 송출(UTC 달력 월)이 monthlyQuotaBytes(초기 80 GB) 를 넘으면
 * monthly_quota 를 보내고 닫는다. 상한에 닿으면 이미 보던 연결도 닫고 수요를 끈다. 송출 바이트는 usageFlushInterval(초기 1분)마다
 * 사용 기록으로 추가만 한다(cctv_remote_usage).</li>
 * <li>ping pingInterval(초기 15 s), pong 없이 pongTimeout(초기 30 s) 이면 끊는다(수신·시청 모두).</li>
 * </ul>
 * 영상은 디스크·DB·객체 저장소 어디에도 쓰지 않는다. 알림 채널이 아니고 위험 판정과 무관하다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CctvRemoteHub {

    private static final Logger log = LoggerFactory.getLogger(CctvRemoteHub.class);

    static final String PROTOCOL = "simai-cctv-preview/1";
    static final String PREVIEW_SCHEMA = "messages/cctv_preview";
    static final int MAX_HEADER_BYTES = 64 * 1024;
    static final int SOURCE_TEXT_LIMIT_BYTES = 256 * 1024;
    private static final int SOURCE_SEND_TIME_LIMIT_MS = 10_000;
    private static final int SOURCE_SEND_BUFFER_BYTES = 64 * 1024;
    static final CloseStatus VIEWER_LIMIT_CLOSE = new CloseStatus(1013, "viewer_limit");
    static final CloseStatus MONTHLY_QUOTA_CLOSE = new CloseStatus(1008, "monthly_quota");
    static final CloseStatus REPLACED_CLOSE = new CloseStatus(1008, "replaced by newer source");
    static final CloseStatus PONG_TIMEOUT_CLOSE = new CloseStatus(1011, "pong timeout");

    /** 게이트웨이 연결 하나. 보내기(demand·ping)는 ConcurrentWebSocketSessionDecorator 로 줄 세운다. */
    private static final class Source {
        final WebSocketSession raw;
        final ConcurrentWebSocketSessionDecorator out;
        final String deviceId;
        volatile long lastPongNanos = System.nanoTime();

        Source(WebSocketSession raw, String deviceId) {
            this.raw = raw;
            this.out = new ConcurrentWebSocketSessionDecorator(raw, SOURCE_SEND_TIME_LIMIT_MS, SOURCE_SEND_BUFFER_BYTES);
            this.deviceId = deviceId;
        }
    }

    /** 관리자 화면 상태(GET /api/cctv/remote/status). */
    public record Status(String month, long monthlySentBytes, long monthlyQuotaBytes, boolean blocked, int viewers,
            int maxViewers, boolean sourceConnected, boolean demand) {
    }

    private final CctvRemoteProperties properties;
    private final CctvUsageRepository usage;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "cctv-remote");
        t.setDaemon(true);
        return t;
    });

    // 아래 상태는 this 잠금 안에서만 바꾼다. 프레임 나눠 주기는 viewers(복사 목록)와 cameras(바꿀 때 통째로)만 읽는다
    private Source source;
    private String helloText;
    private volatile Set<String> cameras;
    private final Map<String, String> codecTexts = new LinkedHashMap<>();
    private final List<CctvViewer> viewers = new CopyOnWriteArrayList<>();
    private boolean demand;
    private ScheduledFuture<?> linger;
    private volatile boolean blocked;
    private volatile long quotaBytes;

    // 송출 사용량. unflushed 는 아직 기록하지 않은 바이트, flushedMonth 는 이번 달 기록 합
    private final AtomicLong unflushed = new AtomicLong();
    private final AtomicInteger peakViewers = new AtomicInteger();
    private final Object flushLock = new Object();
    private volatile long flushedMonth;
    private volatile YearMonth month;
    private Instant periodStart;

    // 점검용 집계
    private final AtomicLong sourceFrames = new AtomicLong();
    private final AtomicLong sourceDropped = new AtomicLong();

    public CctvRemoteHub(CctvRemoteProperties properties, CctvUsageRepository usage, ContractSchemas schemas,
            ObjectMapper mapper, Clock clock) {
        this.properties = properties;
        this.usage = usage;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
        this.quotaBytes = properties.monthlyQuotaBytes();
    }

    @PostConstruct
    void start() {
        Instant now = clock.instant();
        synchronized (flushLock) {
            periodStart = now;
            refreshMonth(now);
        }
        blocked = monthTotal() >= quotaBytes;
        long ping = properties.pingInterval().toMillis();
        scheduler.scheduleWithFixedDelay(this::pingAll, ping, ping, TimeUnit.MILLISECONDS);
        long flush = properties.usageFlushInterval().toMillis();
        scheduler.scheduleWithFixedDelay(this::flushUsage, flush, flush, TimeUnit.MILLISECONDS);
    }

    @PreDestroy
    void stop() {
        List<CctvViewer> all;
        Source s;
        synchronized (this) {
            all = new ArrayList<>(viewers);
            viewers.clear();
            s = source;
            source = null;
        }
        for (CctvViewer v : all) {
            v.close();
            v.closeQuietly(CloseStatus.GOING_AWAY);
        }
        if (s != null) {
            closeQuietly(s.raw, CloseStatus.GOING_AWAY);
        }
        flushUsage();
        scheduler.shutdownNow();
    }

    // ------------------------------------------------------------------ 수신(게이트웨이)

    void sourceConnected(WebSocketSession session, String deviceId) {
        session.setTextMessageSizeLimit(SOURCE_TEXT_LIMIT_BYTES);
        session.setBinaryMessageSizeLimit(properties.sourceMaxMessageBytes());
        Source previous;
        Source next = new Source(session, deviceId);
        boolean demandNow;
        synchronized (this) {
            previous = source;
            source = next;
            helloText = null;
            codecTexts.clear();
            cameras = null;
            demandNow = demand;
            if (previous != null) {
                broadcastStatus("waiting_source");  // 새 연결의 hello 가 올 때까지
            }
        }
        if (previous != null) {
            log.warn("원격 CCTV 수신이 새로 붙어 이전 연결을 닫는다: 이전 device={} 새 device={}", previous.deviceId, deviceId);
            closeQuietly(previous.raw, REPLACED_CLOSE);
        }
        log.info("원격 CCTV 수신 연결됨: device={} 수요={}", deviceId, demandNow);
        scheduler.execute(() -> sendDemand(next, demandNow));
    }

    void sourceClosed(WebSocketSession session) {
        synchronized (this) {
            if (source == null || source.raw != session) {
                return;
            }
            log.info("원격 CCTV 수신 끊김: device={}", source.deviceId);
            source = null;
            helloText = null;
            codecTexts.clear();
            cameras = null;
            broadcastStatus("waiting_source");
        }
    }

    void sourceText(WebSocketSession session, String text) {
        JsonNode doc;
        try {
            doc = mapper.readTree(text);
        } catch (JacksonException e) {
            sourceDropped.incrementAndGet();
            return;
        }
        String kind = doc.path("kind").asString("");
        if (!kind.equals("hello") && !kind.equals("codec")) {
            sourceDropped.incrementAndGet();
            return;
        }
        List<String> problems = schemas.violations(PREVIEW_SCHEMA, text);
        if (!problems.isEmpty()) {
            log.warn("원격 CCTV 수신 메시지가 계약과 다르다(버림): kind={} {}", kind, problems);
            sourceDropped.incrementAndGet();
            return;
        }
        synchronized (this) {
            if (source == null || source.raw != session) {
                return;
            }
            if (kind.equals("hello")) {
                Set<String> list = new LinkedHashSet<>();
                doc.path("cameras").forEach(c -> list.add(c.asString()));
                helloText = text;
                codecTexts.keySet().retainAll(list);
                cameras = Set.copyOf(list);
                String live = statusText("live");
                for (CctvViewer v : viewers) {
                    v.offerText(live, null, false);
                    v.offerText(text, null, true);
                }
            } else {
                String cameraId = doc.path("codec_info").path("camera_id").asString();
                Set<String> known = cameras;
                if (known != null && !known.contains(cameraId)) {
                    sourceDropped.incrementAndGet();
                    return;
                }
                codecTexts.put(cameraId, text);
                for (CctvViewer v : viewers) {
                    v.offerText(text, cameraId, false);
                }
            }
        }
    }

    void sourceBinary(WebSocketSession session, ByteBuffer payload) {
        Source s = source;
        if (s == null || s.raw != session) {
            return;
        }
        byte[] bytes = new byte[payload.remaining()];
        payload.get(bytes);
        String cameraId;
        boolean keyframe;
        try {
            if (bytes.length < 4) {
                throw new IllegalArgumentException("짧다");
            }
            long length = ByteBuffer.wrap(bytes, 0, 4).getInt() & 0xFFFFFFFFL;
            if (length == 0 || length > MAX_HEADER_BYTES || 4 + length > bytes.length) {
                throw new IllegalArgumentException("헤더 길이");
            }
            JsonNode header = mapper.readTree(new String(bytes, 4, (int) length, StandardCharsets.UTF_8));
            cameraId = header.path("camera_id").asString(null);
            JsonNode key = header.get("keyframe");
            if (cameraId == null || key == null || !key.isBoolean()) {
                throw new IllegalArgumentException("헤더 칸");
            }
            keyframe = key.booleanValue();
        } catch (IllegalArgumentException | JacksonException e) {
            sourceDropped.incrementAndGet();
            return;
        }
        Set<String> known = cameras;
        if (known == null || !known.contains(cameraId)) {
            sourceDropped.incrementAndGet();
            return;
        }
        sourceFrames.incrementAndGet();
        for (CctvViewer v : viewers) {
            v.offerFrame(cameraId, keyframe, bytes);
        }
    }

    // ------------------------------------------------------------------ 시청(관리자 브라우저)

    void viewerConnected(WebSocketSession session, long userId) {
        CctvViewer viewer = new CctvViewer(session, userId, properties.viewerQueueFrames(), properties.viewerQueueBytes());
        session.setTextMessageSizeLimit(4 * 1024);
        session.setBinaryMessageSizeLimit(4 * 1024);
        boolean accepted;
        synchronized (this) {
            if (blocked) {
                viewer.endWith(statusText("monthly_quota"), MONTHLY_QUOTA_CLOSE);
                accepted = false;
            } else if (viewers.size() >= properties.maxViewers()) {
                viewer.endWith(statusText("viewer_limit"), VIEWER_LIMIT_CLOSE);
                accepted = false;
            } else {
                viewer.offerText(statusText(helloText != null ? "live" : "waiting_source"), null, false);
                if (helloText != null) {
                    viewer.offerText(helloText, null, true);
                    for (Map.Entry<String, String> codec : codecTexts.entrySet()) {
                        viewer.offerText(codec.getValue(), codec.getKey(), false);
                    }
                }
                viewers.add(viewer);
                peakViewers.accumulateAndGet(viewers.size(), Math::max);
                if (linger != null) {
                    linger.cancel(false);
                    linger = null;
                }
                setDemand(true);
                accepted = true;
            }
        }
        log.info("원격 CCTV 시청 {}: user={} session={} 시청자={}", accepted ? "시작" : "거절", userId, session.getId(),
                viewers.size());
        session.getAttributes().put(CctvViewer.class.getName(), viewer);
        Thread.ofVirtual().name("cctv-viewer-" + session.getId()).start(() -> viewer.sendLoop(this::countSent));
    }

    void viewerClosed(WebSocketSession session) {
        if (!(session.getAttributes().get(CctvViewer.class.getName()) instanceof CctvViewer viewer)) {
            return;
        }
        viewer.close();
        synchronized (this) {
            if (!viewers.remove(viewer)) {
                return;
            }
            log.info("원격 CCTV 시청 끝: user={} session={} 시청자={} 건너뛴 프레임={}", viewer.userId, session.getId(),
                    viewers.size(), viewer.droppedOverflow());
            if (viewers.isEmpty() && demand) {
                if (linger != null) {
                    linger.cancel(false);
                }
                linger = scheduler.schedule(this::lingerExpired, properties.demandLinger().toMillis(),
                        TimeUnit.MILLISECONDS);
            }
        }
    }

    void pong(WebSocketSession session) {
        Source s = source;
        if (s != null && s.raw == session) {
            s.lastPongNanos = System.nanoTime();
            return;
        }
        if (session.getAttributes().get(CctvViewer.class.getName()) instanceof CctvViewer viewer) {
            viewer.pong();
        }
    }

    // ------------------------------------------------------------------ 상태

    public Status status() {
        YearMonth m = month;
        synchronized (this) {
            return new Status(m == null ? null : m.toString(), monthTotal(), quotaBytes, blocked, viewers.size(),
                    properties.maxViewers(), source != null, demand);
        }
    }

    long sourceFrames() {
        return sourceFrames.get();
    }

    long sourceDropped() {
        return sourceDropped.get();
    }

    /** 시험용: 월 상한을 바꾼다(운영은 설정값만 쓴다). */
    void quotaBytesForTest(long value) {
        quotaBytes = value;
    }

    /** 시험용: 연결·사용량 상태를 처음으로 되돌린다(DB 기록은 시험이 지운다). */
    void resetForTest() {
        List<CctvViewer> all;
        Source s;
        synchronized (this) {
            all = new ArrayList<>(viewers);
            viewers.clear();
            s = source;
            source = null;
            helloText = null;
            codecTexts.clear();
            cameras = null;
            demand = false;
            if (linger != null) {
                linger.cancel(false);
                linger = null;
            }
            blocked = false;
            quotaBytes = properties.monthlyQuotaBytes();
        }
        for (CctvViewer v : all) {
            v.close();
            v.closeQuietly(CloseStatus.GOING_AWAY);
        }
        if (s != null) {
            closeQuietly(s.raw, CloseStatus.GOING_AWAY);
        }
        synchronized (flushLock) {
            unflushed.set(0);
            peakViewers.set(0);
            flushedMonth = 0;
            month = null;
            periodStart = clock.instant();
            refreshMonth(periodStart);
        }
    }

    // ------------------------------------------------------------------ 내부

    private void lingerExpired() {
        synchronized (this) {
            linger = null;
            if (viewers.isEmpty()) {
                setDemand(false);
            }
        }
    }

    /** this 잠금 안에서 부른다. 바뀌면 수신에 보낸다(보내기는 스케줄러 스레드에서, 바뀐 순서대로). */
    private void setDemand(boolean value) {
        if (demand == value) {
            return;
        }
        demand = value;
        log.info("원격 CCTV 수요 {}", value ? "있음" : "없음");
        Source s = source;
        if (s != null) {
            scheduler.execute(() -> sendDemand(s, value));
        }
    }

    private void sendDemand(Source s, boolean value) {
        ObjectNode doc = mapper.createObjectNode();
        doc.put("kind", "demand");
        doc.put("active", value);
        doc.put("sent_at_ms", clock.millis());
        try {
            s.out.sendMessage(new TextMessage(mapper.writeValueAsString(doc)));
        } catch (IOException | IllegalStateException e) {
            log.warn("원격 CCTV 수요를 보내지 못했다: device={} {}", s.deviceId, e.getClass().getSimpleName());
            closeQuietly(s.raw, CloseStatus.SESSION_NOT_RELIABLE);
        }
    }

    /** this 잠금 안에서 부른다. */
    private void broadcastStatus(String state) {
        String text = statusText(state);
        for (CctvViewer v : viewers) {
            v.offerText(text, null, false);
        }
    }

    private String statusText(String state) {
        ObjectNode doc = mapper.createObjectNode();
        doc.put("kind", "status");
        doc.put("protocol", PROTOCOL);
        doc.put("state", state);
        doc.put("monthly_sent_bytes", monthTotal());
        doc.put("monthly_quota_bytes", quotaBytes);
        return mapper.writeValueAsString(doc);
    }

    private long monthTotal() {
        return flushedMonth + unflushed.get();
    }

    private void countSent(long bytes) {
        unflushed.addAndGet(bytes);
        if (!blocked && monthTotal() >= quotaBytes) {
            scheduler.execute(this::enforceQuota);
        }
    }

    private void enforceQuota() {
        List<CctvViewer> all;
        synchronized (this) {
            if (blocked || monthTotal() < quotaBytes) {
                return;
            }
            blocked = true;
            log.warn("원격 CCTV 월 송출 상한을 넘어 시청을 막는다: {} / {} 바이트", monthTotal(), quotaBytes);
            String text = statusText("monthly_quota");
            all = new ArrayList<>(viewers);
            viewers.clear();
            for (CctvViewer v : all) {
                v.endWith(text, MONTHLY_QUOTA_CLOSE);
            }
            if (linger != null) {
                linger.cancel(false);
                linger = null;
            }
            setDemand(false);
        }
    }

    private void pingAll() {
        long now = System.nanoTime();
        long timeout = properties.pongTimeout().toNanos();
        Source s = source;
        if (s != null) {
            if (now - s.lastPongNanos > timeout) {
                log.warn("원격 CCTV 수신이 pong 없이 {} 초 지나 끊는다: device={}", properties.pongTimeout().toSeconds(), s.deviceId);
                closeQuietly(s.raw, PONG_TIMEOUT_CLOSE);
            } else {
                try {
                    s.out.sendMessage(new PingMessage());
                } catch (IOException | IllegalStateException e) {
                    closeQuietly(s.raw, CloseStatus.SESSION_NOT_RELIABLE);
                }
            }
        }
        for (CctvViewer v : viewers) {
            if (now - v.lastPongNanos() > timeout) {
                log.info("원격 CCTV 시청자가 pong 없이 끊는다: session={}", v.session.getId());
                v.close();
                Thread.ofVirtual().start(() -> v.closeQuietly(PONG_TIMEOUT_CLOSE));
            } else {
                v.offerPing();
            }
        }
    }

    /** 지난 기록 뒤 보낸 바이트를 사용 기록 한 줄로 추가한다(덮어쓰지 않음). 실패하면 다음에 함께 남긴다. */
    void flushUsage() {
        synchronized (flushLock) {
            Instant now = clock.instant();
            long bytes = unflushed.getAndSet(0);
            int peak = peakViewers.getAndSet(viewers.size());
            if (bytes > 0) {
                try {
                    usage.insert(periodStart, now, bytes, peak, now);
                } catch (DataAccessException e) {
                    unflushed.addAndGet(bytes);
                    log.warn("원격 CCTV 사용 기록을 남기지 못했다(다음에 다시): {}", e.getClass().getSimpleName());
                    return;
                }
                flushedMonth += bytes;
            }
            periodStart = now;
            refreshMonth(now);
        }
        if (blocked && monthTotal() < quotaBytes) {
            blocked = false;
            log.info("원격 CCTV 월 송출 상한 차단을 푼다(새 달 또는 상한 변경)");
        }
    }

    /** flushLock 안에서 부른다. 달이 바뀌었으면 그 달 기록 합을 DB 에서 다시 읽는다. */
    private void refreshMonth(Instant now) {
        YearMonth current = YearMonth.from(now.atZone(ZoneOffset.UTC));
        if (current.equals(month)) {
            return;
        }
        Instant from = current.atDay(1).atStartOfDay(ZoneOffset.UTC).toInstant();
        Instant to = current.plusMonths(1).atDay(1).atStartOfDay(ZoneOffset.UTC).toInstant();
        try {
            flushedMonth = usage.sentBytes(from, to);
            month = current;
        } catch (DataAccessException e) {
            log.warn("원격 CCTV 이번 달 사용량을 읽지 못했다(다음에 다시): {}", e.getClass().getSimpleName());
        }
    }

    private static void closeQuietly(WebSocketSession session, CloseStatus status) {
        try {
            if (session.isOpen()) {
                session.close(status);
            }
        } catch (IOException | IllegalStateException e) {
            log.debug("원격 CCTV 연결을 닫지 못했다: session={}", session.getId());
        }
    }
}
