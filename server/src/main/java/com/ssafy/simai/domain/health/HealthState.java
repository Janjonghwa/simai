package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.Tier0HealthStored;
import com.ssafy.simai.domain.tier0.i1.WorldStateStored;
import java.time.Clock;
import java.time.Instant;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import org.springframework.transaction.event.TransactionalEventListener;
import tools.jackson.databind.JsonNode;

/**
 * 화면 상태(viewer_health)를 만드는 데 쓰는 마지막 수신 값(명세 3.2 이중 감시, 12.4). 저장이 끝난 뒤의 값만 둔다.
 * <ul>
 *   <li>Tier 0 health: payload 원문과 EC2 수신 시각(tier0_link 판단, tier0_health 원문)
 *   <li>world_state: t_latest_global·scene_version(데이터 연령, Scene 불일치)
 *   <li>노트북 하트비트: payload 와 EC2 수신 시각(laptop.link, remote_state 구분). 정본은 device_heartbeat 표다
 * </ul>
 * 메모리에만 둔다. 서버가 다시 뜨면 비어 있다가 다음 수신(1초 주기)으로 찬다. 그 사이는 모름이라 끊김으로 본다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class HealthState {

    public record Received(JsonNode payload, Instant receivedAt) {
    }

    public record World(long tLatestGlobalMs, String sceneVersion, Instant receivedAt) {
    }

    public record Heartbeat(String deviceId, JsonNode payload, Instant receivedAt) {
    }

    private final Clock clock;
    private volatile Received tier0Health;
    private volatile World world;
    private volatile Heartbeat laptop;
    private volatile Runnable onChange = () -> { };

    public HealthState(Clock clock) {
        this.clock = clock;
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onTier0Health(Tier0HealthStored stored) {
        Received previous = tier0Health;
        if (previous == null || !stored.receivedAt().isBefore(previous.receivedAt())) {
            tier0Health = new Received(stored.envelope().payload(), stored.receivedAt());
        }
        onChange.run();
    }

    @TransactionalEventListener(fallbackExecution = true)
    void onWorldState(WorldStateStored stored) {
        I1Envelope envelope = stored.envelope();
        JsonNode t = envelope.payload().get("t_latest_global");
        JsonNode scene = envelope.payload().get("scene_version");
        World previous = world;
        long tMs = t != null && t.isIntegralNumber() ? t.longValue() : -1;
        if (tMs < 0 || (previous != null && tMs < previous.tLatestGlobalMs())) {
            return; // 늦게 온 이전 프레임은 데이터 연령을 되돌리지 않는다
        }
        world = new World(tMs, scene != null && scene.isString() ? scene.stringValue() : null, clock.instant());
    }

    void recordHeartbeat(String deviceId, JsonNode payload, Instant receivedAt) {
        laptop = new Heartbeat(deviceId, payload, receivedAt);
        onChange.run();
    }

    /** 받은 값이 바뀌면 부른다(바뀌면 즉시 보내기, 명세 12.4) */
    void onChange(Runnable listener) {
        onChange = listener;
    }

    public Received tier0Health() {
        return tier0Health;
    }

    public World world() {
        return world;
    }

    public Heartbeat laptop() {
        return laptop;
    }
}
