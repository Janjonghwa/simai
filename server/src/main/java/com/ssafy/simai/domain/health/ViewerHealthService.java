package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.domain.scene.SceneCompatibility;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 화면 상태 viewer_health(계약 1.0.0, 명세 3.2 이중 감시·6.2·12.4, FR-5·11·12)를 만든다. GET /api/health 와 /topic/health 가 같은 값을 쓴다.
 * <ul>
 *   <li>tier0_link·laptop.link: 마지막 수신이 linkTimeoutMs(기본 3초 = 1초 주기 3회 누락, 3.10) 안이면 CONNECTED. 받는 쪽 단조 시계로 잰다
 *   <li>Tier 0 정상 = 연결돼 있고 처리가 나아감(t_latest_global 이 linkTimeoutMs 안에 늘어남, FR-11). health 는 오는데 처리가 멈추면
 *       tier0_link 는 CONNECTED 로 두고 관제 정지(CONTROL_HALTED)로 본다
 *   <li>remote_state: 노트북이 "Tier 0 제어 연결 CONNECTED, 현장 표시 CONTROL_HALTED 아님"이라고 알리는지(노트북 정상)를 함께 본다.
 *       Tier 0 정상이고 노트북 연결이 살아 있고 노트북 정상이면 NORMAL, Tier 0 연결만 끊겼고 Tier 0 를 잃은 뒤 받은 하트비트가 노트북 정상을
 *       알리면 REMOTE_LINK_LOST(현장 경보 동작 중, 그 하트비트가 오기 전에는 모르므로 CONTROL_HALTED),
 *       그 밖(노트북 끊김·노트북이 관제 정지를 알림)은 CONTROL_HALTED. 연결이 살아 있어도 노트북이 관제 정지를 알리면 NORMAL 이 아니다
 *   <li>system_health: remote_state 가 NORMAL 이 아니면 UNSAFE_UNKNOWN, 아니면 Tier 0 health 의 값(모르면 UNSAFE_UNKNOWN)
 *   <li>노트북이 여럿이면(MVP 는 하나) 살아 있는 노트북이 모두 정상일 때만 정상이다. 살아 있는 노트북이 하나라도 있으면 laptop.link 는
 *       CONNECTED 다(현장 노트북이 죽고 예비가 살아 있으면 구분하지 못한다, 설계 문서 남은 일)
 *   <li>calibration_state: Tier 0 가 연결돼 있으면 Tier 0 health 의 값, 끊겼거나 모르면 UNCALIBRATED(모르면 정상으로 보지 않는다)
 *   <li>data_age_ms: 지금 − 마지막 world_state 의 t_latest_global(두 서버 시계는 NTP 로 맞춘다고 본다, 음수는 0). 받은 적 없으면 null
 *   <li>config: 발행한 최근 묶음, 적용 확인(config_bundle.applied_at)된 최근 묶음, sync 는 Tier 0 가 연결돼 있으면 Tier 0 health 의
 *       config_sync, 끊겼으면 UNVERIFIED
 *   <li>scene: COMMITTED Scene 의 최근 버전과 마지막 world_state.scene_version, 둘 다 있고 다르면 mismatch
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ViewerHealthService {

    public static final String SCHEMA_VERSION = "1.1.0";
    static final String CONNECTED = "CONNECTED";
    static final String DISCONNECTED = "DISCONNECTED";
    static final String UNSAFE_UNKNOWN = "UNSAFE_UNKNOWN";

    private final HealthState state;
    private final SceneCompatibility compatibility;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final long linkTimeoutMs;

    public ViewerHealthService(HealthState state, NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock, SceneCompatibility compatibility,
            @Value("${simai.health.link-timeout-ms:3000}") long linkTimeoutMs) {
        this.state = state;
        this.compatibility = compatibility;
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.clock = clock;
        this.linkTimeoutMs = linkTimeoutMs;
    }

    /** 봉투까지 씌운 viewer_health(source_id ec2) */
    public ObjectNode message() {
        Instant now = clock.instant();
        return I1Envelope.outbound(mapper, now.toEpochMilli(), "viewer_health", SCHEMA_VERSION, payload(now));
    }

    ObjectNode payload(Instant now) {
        long nowNanos = state.nanoTime();
        HealthState.Received tier0 = state.tier0Health();
        HealthState.World world = state.world();
        HealthState.Progress progress = state.progress();
        boolean tier0Up = tier0 != null && fresh(tier0.receivedNanos(), nowNanos);
        boolean processing = progress != null && fresh(progress.nanos(), nowNanos);
        List<HealthState.Heartbeat> laptops = state.laptops();
        List<HealthState.Heartbeat> live = laptops.stream().filter(h -> fresh(h.receivedNanos(), nowNanos)).toList();
        boolean laptopUp = !live.isEmpty();
        // 살아 있는 노트북이 모두 정상(Tier 0 제어 연결, 관제 정지 아님)을 알릴 때만 정상으로 본다
        boolean laptopOk = laptopUp && live.stream().allMatch(h -> HealthState.siteOk(h.payload()));
        Long lastHeartbeat = laptops.stream().map(h -> h.receivedAt().toEpochMilli()).max(Long::compare).orElse(null);
        JsonNode health = tier0 == null ? null : tier0.payload();

        // Tier 0 를 잃은 뒤(마지막 health + 끊김 기준) 받은 하트비트만 Tier 0 상태를 안다. 그 전 하트비트로 '현장 경보 동작 중'이라 하지 않는다
        long lostNanos = tier0 == null ? Long.MIN_VALUE : tier0.receivedNanos() + linkTimeoutMs * 1_000_000L;
        List<HealthState.Heartbeat> afterLoss = live.stream().filter(h -> h.receivedNanos() - lostNanos > 0).toList();
        boolean laptopSaysTier0Ok = !afterLoss.isEmpty() && afterLoss.stream().allMatch(h -> HealthState.siteOk(h.payload()));
        String remote = tier0Up && processing && laptopOk ? "NORMAL"
                : !tier0Up && laptopSaysTier0Ok ? "REMOTE_LINK_LOST" : "CONTROL_HALTED";
        ObjectNode p = mapper.createObjectNode();
        p.put("as_of", now.toEpochMilli());
        String reported = health == null ? null : health.path("system_health").asString(null);
        p.put("system_health", "NORMAL".equals(remote) && reported != null ? reported : UNSAFE_UNKNOWN);
        p.put("remote_state", remote);
        p.put("tier0_link", tier0Up ? CONNECTED : DISCONNECTED);
        ObjectNode laptopNode = p.putObject("laptop");
        laptopNode.put("link", laptopUp ? CONNECTED : DISCONNECTED);
        laptopNode.put("last_heartbeat_at", lastHeartbeat);
        p.put("data_age_ms", world == null ? null : Math.max(0, now.toEpochMilli() - world.tLatestGlobalMs()));
        String calibration = tier0Up && health != null ? health.path("calibration_state").asString(null) : null;
        p.put("calibration_state", calibration == null ? "UNCALIBRATED" : calibration);
        p.set("config", config(tier0Up, health));
        p.set("scene", scene(world));
        p.set("tier0_health", health == null ? null : health.deepCopy());
        return p;
    }

    private boolean fresh(long receivedNanos, long nowNanos) {
        return nowNanos - receivedNanos <= linkTimeoutMs * 1_000_000L;
    }

    /**
     * 화면 장애 종류(FR-11, 관제 앱 features/layout/status.ts 와 같은 규칙). 정상이면 null.
     * REMOTE_LINK_LOST 원격 전달 단절, CONTROL_HALTED 중 노트북 하트비트가 끊겼으면 DEVICE_DOWN 현장 장비 응답 없음, 그 밖은 CONTROL_HALTED 관제 정지
     */
    public static String failure(JsonNode payload) {
        String remote = payload.path("remote_state").asString("");
        if ("REMOTE_LINK_LOST".equals(remote)) {
            return remote;
        }
        if (!"CONTROL_HALTED".equals(remote)) {
            return null;
        }
        return DISCONNECTED.equals(payload.path("laptop").path("link").asString(null)) ? "DEVICE_DOWN" : "CONTROL_HALTED";
    }

    long linkTimeoutMs() {
        return linkTimeoutMs;
    }

    private ObjectNode config(boolean tier0Up, JsonNode health) {
        ObjectNode c = mapper.createObjectNode();
        c.put("published_version", first("SELECT config_version FROM config_bundle ORDER BY issued_at DESC LIMIT 1"));
        c.put("applied_version", first("""
                SELECT config_version FROM config_bundle WHERE applied_at IS NOT NULL ORDER BY applied_at DESC LIMIT 1
                """));
        String reported = health == null ? null : health.path("config_sync").asString(null);
        c.put("sync", tier0Up && reported != null ? reported : "UNVERIFIED");
        return c;
    }

    private ObjectNode scene(HealthState.World world) {
        ObjectNode s = mapper.createObjectNode();
        // GET /api/scenes/current 와 같은 Scene(SceneService.CURRENT_COMMITTED)
        String committed = first("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s");
        String live = world == null ? null : world.sceneVersion();
        s.put("committed_version", committed);
        s.put("live_version", live);
        s.put("mismatch", committed != null && live != null && !committed.equals(live));
        if (committed != null) {
            var sources = jdbc.queryForList("SELECT calibration_version FROM scene_version WHERE scene_version = :v",
                    Map.of("v", committed), String.class);
            String source = sources.isEmpty() ? null : sources.getFirst();
            s.set("compatibility", compatibility.compare(committed, source, world == null ? null : world.calibrationVersion()));
        }
        return s;
    }

    private String first(String sql) {
        List<String> rows = jdbc.queryForList(sql, Map.of(), String.class);
        return rows.isEmpty() ? null : rows.getFirst();
    }
}
