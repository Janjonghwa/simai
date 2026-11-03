package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.Tier0HealthStored;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;
import tools.jackson.databind.JsonNode;

/**
 * 화면 상태(viewer_health)를 만드는 데 쓰는 마지막 수신 값(명세 3.2 이중 감시, 12.4). 저장이 끝난 뒤의 값만 둔다.
 * <ul>
 *   <li>Tier 0 health: payload 원문과 EC2 수신 시각(tier0_link 판단, tier0_health 원문)
 *   <li>world_state: t_latest_global·scene_version(데이터 연령, Scene 불일치)
 *   <li>노트북 하트비트: 기기마다 payload 와 EC2 수신 시각(laptop.link, remote_state 구분). 정본은 device_heartbeat 표다
 * </ul>
 * 끊김 판단은 받는 쪽 단조 시계(System.nanoTime)로 잰다(FR-11·NFR-9). 벽시계가 맞춰져도 끊김이 생기거나 사라지지 않는다.
 * Tier 0 처리 진행: health 나 world_state 의 t_latest_global 이 마지막으로 늘어난 때(FR-11 "Tier 0 정상은 처리 진행으로 판정").
 * 메모리에만 둔다. 서버가 다시 뜨면 비어 있다가 다음 수신(1초 주기)으로 찬다. 그 사이는 모름이라 끊김으로 본다.
 * 값이 바뀌면 onChange 를 부른다. 받는 쪽은 받은 스레드(I1·HTTP)를 붙잡지 않아야 한다(HealthPublisher 가 따로 보낸다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class HealthState {

    /** 더 새 world_state 없이 이만큼 지나면 t_latest_global 이 뒤로 가도 받는다(Tier 0 시계 보정·재시작) */
    static final long WORLD_RESYNC_MS = 2_000;

    /** receivedNanos 는 단조 시계(끊김 판단용), receivedAt 은 벽시계(화면 표시·기록용) */
    public record Received(JsonNode payload, Instant receivedAt, long receivedNanos) {
    }

    public record World(long tLatestGlobalMs, String sceneVersion, Instant receivedAt, String calibrationVersion) {
    }

    public record Heartbeat(String deviceId, JsonNode payload, Instant receivedAt, long receivedNanos) {
    }

    /** Tier 0 처리가 마지막으로 나아간 때(t_latest_global 이 늘어남) */
    public record Progress(long tLatestGlobalMs, Instant at, long nanos) {
    }

    private final Clock clock;
    private final Map<String, Heartbeat> laptops = new ConcurrentHashMap<>();
    private volatile Received tier0Health;
    private volatile World world;
    private volatile Progress progress;
    /** 노트북이 마지막으로 정상(Tier 0 제어 연결, 관제 정지 아님)을 알린 때 */
    private volatile Instant laptopOkAt;
    private volatile Runnable onChange = () -> { };

    public HealthState(Clock clock) {
        this.clock = clock;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onTier0Health(Tier0HealthStored stored) {
        // 도착 순서(I1 한 연결)대로 바꾼다. 벽시계로 순서를 정하면 시계를 뒤로 맞출 때 새 health 를 버린다
        tier0Health = new Received(stored.envelope().payload(), stored.receivedAt(), nanoTime());
        JsonNode t = stored.envelope().payload().get("t_latest_global");
        if (t != null && t.isIntegralNumber()) {
            progressed(t.longValue());
        }
        onChange.run();
    }

    /**
     * t_latest_global 이 지금까지 본 값보다 크면 처리가 나아간 것이다. 작아졌는데 그동안(WORLD_RESYNC_MS) 더 큰 값이 없었으면
     * Tier 0 시계가 바뀐 것(시계 차이 재추정·재시작)으로 보고 그 값을 새 기준으로 삼는다(되돌아간 뒤 관제 정지로 굳지 않게)
     */
    private synchronized void progressed(long tMs) {
        Progress previous = progress;
        long now = nanoTime();
        if (previous == null || tMs > previous.tLatestGlobalMs()
                || (tMs < previous.tLatestGlobalMs() && now - previous.nanos() >= WORLD_RESYNC_MS * 1_000_000L)) {
            progress = new Progress(tMs, clock.instant(), now);
        }
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onWorldState(WorldStateStored stored) {
        I1Envelope envelope = stored.envelope();
        JsonNode t = envelope.payload().get("t_latest_global");
        JsonNode scene = envelope.payload().get("scene_version");
        long tMs = t != null && t.isIntegralNumber() ? t.longValue() : -1;
        if (tMs < 0) {
            return;
        }
        Instant now = clock.instant();
        World previous = world;
        // 늦게 온 이전 프레임은 데이터 연령을 되돌리지 않는다. 더 새 프레임이 한동안 없으면 시계가 바뀐 것으로 보고 받는다
        if (previous != null && tMs < previous.tLatestGlobalMs()
                && now.toEpochMilli() - previous.receivedAt().toEpochMilli() < WORLD_RESYNC_MS) {
            return;
        }
        String sceneVersion = scene != null && scene.isString() ? scene.stringValue() : null;
        world = new World(tMs, sceneVersion, now, envelope.payload().path("calibration_version").asString(null));
        progressed(tMs);
        if (previous == null || !Objects.equals(previous.sceneVersion(), sceneVersion)) {
            onChange.run(); // Scene 불일치가 바뀔 수 있다
        }
    }

    void recordHeartbeat(String deviceId, JsonNode payload, Instant receivedAt) {
        laptops.put(deviceId, new Heartbeat(deviceId, payload, receivedAt, nanoTime()));
        if (siteOk(payload)) {
            laptopOkAt = receivedAt;
        }
        onChange.run();
    }

    /** 워치 I4 연결 상태가 바뀌었다(연결·하트비트·끊김, S15P31A101-336). viewer_health.watches 를 바로 보내게 한다 */
    public void watchesChanged() {
        onChange.run();
    }

    /** 받는 쪽 단조 시계(ns) */
    public long nanoTime() {
        return System.nanoTime();
    }

    public Progress progress() {
        return progress;
    }

    public Instant laptopOkAt() {
        return laptopOkAt;
    }

    /** 노트북 정상: Tier 0 제어 연결 CONNECTED 이고 관제 정지를 알리지 않음. control 만 보지 않는다(계약 laptop_heartbeat) */
    static boolean siteOk(JsonNode heartbeat) {
        return "CONNECTED".equals(heartbeat.path("tier0").path("control").asString(null))
                && !"CONTROL_HALTED".equals(heartbeat.path("site_display").asString(null));
    }

    /** 받은 값이 바뀌면 부른다(바뀌면 즉시 보내기, 명세 12.4). 받은 스레드에서 불리므로 빨리 끝나야 한다 */
    void onChange(Runnable listener) {
        onChange = listener;
    }

    public Received tier0Health() {
        return tier0Health;
    }

    public World world() {
        return world;
    }

    /** 노트북마다 마지막 하트비트 */
    public List<Heartbeat> laptops() {
        return List.copyOf(laptops.values());
    }
}
