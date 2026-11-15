package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.domain.scene.SceneCompatibility;
import com.ssafy.simai.domain.tier0.event.Tier0HeldEvents;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.watch.PersonDisplayNames;
import com.ssafy.simai.domain.watch.WatchHub;
import java.time.Clock;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
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
 *   <li>re_setup(1.3.0, S15P31A101-480): 회전 자동 복구가 '현장 세팅 다시 하기'를 요구한 카메라. Tier 0 가 연결 중이고 마지막 health 가 싣고 있을 때만 그 값(빈 배열 = 없음),
 *       그 밖은 싣지 않는다(모름). 관리자 상단 배너용 안내이며 system_health 를 바꾸지 않는다
 *   <li>watches(1.2.0, 확장 C.6, S15P31A101-336): 등록된 워치마다 I4 연결 상태(WatchHub). 워치는 개인 보조 채널이라 system_health·
 *       remote_state 에 넣지 않는다
 *   <li>laptop.test_input(1.6.0, S15P31A101-731): 녹화 파일·합성 영상 재생(시험 입력) 중인가(HealthState.testInputActive, 워치 진동 차단과 같은
 *       판정). 관제 화면의 「녹화 재생 중 · 실제 알림 꺼짐」 표시용이며 system_health 를 바꾸지 않는다
 *   <li>laptop.replay_test_alerts(1.7.0, S15P31A101-736): EC2 재생 시험 알림 스위치(ReplayTestSwitch.alerts). test_input 과 함께 true 면
 *       관제 화면은 「재생 시험 · 실제 알림 켜짐」을 보인다(워치 진동이 나간다)
 *   <li>held_events_revision(1.4.0, S15P31A101-595): Tier 0 가 들고 있는 이벤트 집합(Tier0HeldEvents)이 스냅샷·유예 만료로 한꺼번에
 *       바뀔 때마다 바뀌는 판. 관제 앱은 바뀌면 이벤트 목록을 다시 받아 「진행 중」과 「지난 관제」를 다시 가른다
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ViewerHealthService {

    public static final String SCHEMA_VERSION = "1.7.0";
    static final String CONNECTED = "CONNECTED";
    static final String DISCONNECTED = "DISCONNECTED";
    static final String UNSAFE_UNKNOWN = "UNSAFE_UNKNOWN";

    private final HealthState state;
    private final SceneCompatibility compatibility;
    private final WatchHub watches;
    private final PersonDisplayNames personDisplayNames;
    private final Tier0HeldEvents held;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final long linkTimeoutMs;
    private final ReplayTestSwitch replayTest;

    public ViewerHealthService(HealthState state, NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock, SceneCompatibility compatibility,
            WatchHub watches, PersonDisplayNames personDisplayNames, Tier0HeldEvents held, ReplayTestSwitch replayTest,
            @Value("${simai.health.link-timeout-ms:3000}") long linkTimeoutMs) {
        this.state = state;
        this.replayTest = replayTest;
        this.compatibility = compatibility;
        this.watches = watches;
        this.personDisplayNames = personDisplayNames;
        this.held = held;
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
        // 1.6.0(S15P31A101-731): 녹화 파일·합성 영상 재생 중이면 true. 워치 진동 차단(-724)과 같은 판정이라 화면의 「실제 알림 꺼짐」이 실제와 같다
        laptopNode.put("test_input", state.testInputActive());
        // 1.7.0(S15P31A101-736): EC2 재생 시험 알림 스위치. 켜져 있으면 시험 입력 중에도 워치 진동이 나간다(화면은 「재생 시험 · 실제 알림 켜짐」)
        laptopNode.put("replay_test_alerts", replayTest.alerts());
        p.put("data_age_ms", world == null ? null : Math.max(0, now.toEpochMilli() - world.tLatestGlobalMs()));
        String calibration = tier0Up && health != null ? health.path("calibration_state").asString(null) : null;
        p.put("calibration_state", calibration == null ? "UNCALIBRATED" : calibration);
        p.set("config", config(tier0Up, health));
        p.set("scene", scene(world));
        p.set("tier0_health", health == null ? null : health.deepCopy());
        ArrayNode reSetup = tier0Up ? reSetup(health) : null;
        if (reSetup != null) {
            p.set("re_setup", reSetup);
        }
        p.set("watches", watches());
        p.put("held_events_revision", held.revision());
        PersonDisplayNames.Snapshot displayNames = personDisplayNames.snapshotForCurrentSite();
        p.set("watch_names", watchNames(displayNames.watchNames()));
        p.set("person_names", personNames(displayNames.personNames()));
        return p;
    }

    private ArrayNode watchNames(List<PersonDisplayNames.WatchNameItem> items) {
        ArrayNode array = mapper.createArrayNode();
        for (PersonDisplayNames.WatchNameItem item : items) {
            ObjectNode n = array.addObject();
            n.put("device_id", item.deviceId());
            n.put("display_name", item.displayName());
        }
        return array;
    }

    private ArrayNode personNames(List<PersonDisplayNames.PersonNameItem> items) {
        ArrayNode array = mapper.createArrayNode();
        for (PersonDisplayNames.PersonNameItem item : items) {
            ObjectNode n = array.addObject();
            n.put("world_id", item.worldId());
            n.put("display_name", item.displayName());
            n.put("name_source", item.nameSource());
        }
        return array;
    }

    /**
     * 지금 Tier 0 가 재세팅을 요구하는 카메라 ID(viewer_health {@code re_setup} 과 같은 판단, 오름차순). Tier 0 가 끊겼거나 마지막 health 에
     * re_setup 이 없으면 null(모름). 세팅의 「이전 맵 재사용」 가능 판정이 직전 보정 대비 카메라 변화 근거로 쓴다(S15P31A101-689).
     */
    public List<String> reSetupCameraIds() {
        HealthState.Received tier0 = state.tier0Health();
        if (tier0 == null || !fresh(tier0.receivedNanos(), state.nanoTime())) {
            return null;
        }
        ArrayNode items = reSetup(tier0.payload());
        if (items == null) {
            return null;
        }
        List<String> ids = new ArrayList<>();
        items.forEach(item -> ids.add(item.get("camera_id").stringValue()));
        return ids;
    }

    /**
     * 재세팅이 필요한 카메라(viewer_health 1.3.0 re_setup, S15P31A101-480). Tier 0 가 연결돼 있고 마지막 health 가 re_setup 배열을 실었을 때만 그 값을
     * camera_id 오름차순으로 옮긴다(빈 배열 = 필요한 카메라 없음). health 가 없거나 필드가 없으면(이전 버전 Tier 0) null: 모름이라 싣지 않는다.
     * 항목은 계약의 세 필드만 옮기고, 형식이 맞지 않는 항목은 버린다(화면 계약을 깨지 않게). 경보·system_health 는 바꾸지 않는다.
     */
    private ArrayNode reSetup(JsonNode health) {
        JsonNode reported = health == null ? null : health.get("re_setup");
        if (reported == null || !reported.isArray()) {
            return null;
        }
        List<ObjectNode> items = new ArrayList<>();
        for (JsonNode item : reported) {
            JsonNode camera = item.get("camera_id");
            JsonNode reason = item.get("reason");
            JsonNode detectedAt = item.get("detected_at");
            if (camera == null || !camera.isString() || reason == null || !reason.isString() || reason.stringValue().isEmpty()
                    || detectedAt == null || !detectedAt.isIntegralNumber()) {
                continue;
            }
            ObjectNode copy = mapper.createObjectNode();
            copy.put("camera_id", camera.stringValue());
            copy.put("reason", reason.stringValue());
            copy.put("detected_at", detectedAt.longValue());
            items.add(copy);
        }
        items.sort(Comparator.comparing(n -> n.get("camera_id").stringValue()));
        ArrayNode array = mapper.createArrayNode();
        items.forEach(array::add);
        return array;
    }

    private ObjectNode watches() {
        ObjectNode w = mapper.createObjectNode();
        var items = w.putArray("items");
        int connected = 0;
        List<WatchHub.WatchStatus> statuses = watches.statuses();
        for (WatchHub.WatchStatus s : statuses) {
            ObjectNode item = items.addObject();
            item.put("device_id", s.deviceId());
            item.put("link", s.connected() ? CONNECTED : "LOST");
            item.put("last_heartbeat_at", s.lastHeartbeatAt() == null ? null : s.lastHeartbeatAt().toEpochMilli());
            item.put("battery_pct", s.batteryPct());
            item.put("transport", s.transport());
            connected += s.connected() ? 1 : 0;
        }
        w.put("registered", statuses.size());
        w.put("connected", connected);
        return w;
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
