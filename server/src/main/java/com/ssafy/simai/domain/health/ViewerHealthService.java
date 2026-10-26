package com.ssafy.simai.domain.health;

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
 *   <li>tier0_link·laptop.link: 마지막 수신(EC2 시각)이 linkTimeoutMs(기본 3초 = 1초 주기 3회 누락, 3.10) 안이면 CONNECTED
 *   <li>remote_state: 둘 다 연결이면 NORMAL. Tier 0 만 끊겼고 노트북이 Tier 0 제어 연결 CONNECTED 이며 현장 표시가 CONTROL_HALTED 가
 *       아니면 REMOTE_LINK_LOST(현장 경보 동작 중), 그 밖(노트북도 끊김·관제 정지 표시·노트북 하트비트 없음)은 CONTROL_HALTED
 *   <li>system_health: 어느 쪽이든 끊기면 UNSAFE_UNKNOWN, 아니면 Tier 0 health 의 값(모르면 UNSAFE_UNKNOWN)
 *   <li>data_age_ms: 지금 − 마지막 world_state 의 t_latest_global(두 서버 시계는 NTP 로 맞춘다고 본다, 음수는 0). 받은 적 없으면 null
 *   <li>config: 발행한 최근 묶음, 적용 확인(config_bundle.applied_at)된 최근 묶음, sync 는 Tier 0 가 연결돼 있으면 Tier 0 health 의
 *       config_sync, 끊겼으면 UNVERIFIED
 *   <li>scene: COMMITTED Scene 의 최근 버전과 마지막 world_state.scene_version, 둘 다 있고 다르면 mismatch
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ViewerHealthService {

    public static final String SCHEMA_VERSION = "1.0.0";
    static final String CONNECTED = "CONNECTED";
    static final String DISCONNECTED = "DISCONNECTED";
    static final String UNSAFE_UNKNOWN = "UNSAFE_UNKNOWN";

    private final HealthState state;
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final long linkTimeoutMs;

    public ViewerHealthService(HealthState state, NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Clock clock,
            @Value("${simai.health.link-timeout-ms:3000}") long linkTimeoutMs) {
        this.state = state;
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
        HealthState.Received tier0 = state.tier0Health();
        HealthState.Heartbeat laptop = state.laptop();
        HealthState.World world = state.world();
        boolean tier0Up = fresh(tier0 == null ? null : tier0.receivedAt(), now);
        boolean laptopUp = fresh(laptop == null ? null : laptop.receivedAt(), now);
        JsonNode health = tier0 == null ? null : tier0.payload();

        ObjectNode p = mapper.createObjectNode();
        p.put("as_of", now.toEpochMilli());
        String reported = health == null ? null : health.path("system_health").asString(null);
        p.put("system_health", tier0Up && laptopUp && reported != null ? reported : UNSAFE_UNKNOWN);
        p.put("remote_state", remoteState(tier0Up, laptopUp, laptop));
        p.put("tier0_link", tier0Up ? CONNECTED : DISCONNECTED);
        ObjectNode laptopNode = p.putObject("laptop");
        laptopNode.put("link", laptopUp ? CONNECTED : DISCONNECTED);
        laptopNode.put("last_heartbeat_at", laptop == null ? null : laptop.receivedAt().toEpochMilli());
        p.put("data_age_ms", world == null ? null : Math.max(0, now.toEpochMilli() - world.tLatestGlobalMs()));
        String calibration = health == null ? null : health.path("calibration_state").asString(null);
        p.put("calibration_state", calibration == null ? "UNCALIBRATED" : calibration);
        p.set("config", config(tier0Up, health));
        p.set("scene", scene(world));
        p.set("tier0_health", health == null ? null : health.deepCopy());
        return p;
    }

    private boolean fresh(Instant receivedAt, Instant now) {
        return receivedAt != null && now.toEpochMilli() - receivedAt.toEpochMilli() <= linkTimeoutMs;
    }

    private static String remoteState(boolean tier0Up, boolean laptopUp, HealthState.Heartbeat laptop) {
        if (tier0Up && laptopUp) {
            return "NORMAL";
        }
        if (!tier0Up && laptopUp) {
            JsonNode p = laptop.payload();
            boolean controlUp = CONNECTED.equals(p.path("tier0").path("control").asString(null));
            boolean halted = "CONTROL_HALTED".equals(p.path("site_display").asString(null));
            // control 만 보지 않는다: 연결돼 있어도 처리가 멈추면 노트북은 CONTROL_HALTED 로 알린다(계약 laptop_heartbeat)
            return controlUp && !halted ? "REMOTE_LINK_LOST" : "CONTROL_HALTED";
        }
        return "CONTROL_HALTED";
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
        String committed = first("""
                SELECT scene_version FROM scene_version WHERE state = 'COMMITTED' AND valid_to IS NULL
                ORDER BY committed_at DESC NULLS LAST LIMIT 1
                """);
        String live = world == null ? null : world.sceneVersion();
        s.put("committed_version", committed);
        s.put("live_version", live);
        s.put("mismatch", committed != null && live != null && !committed.equals(live));
        return s;
    }

    private String first(String sql) {
        List<String> rows = jdbc.queryForList(sql, Map.of(), String.class);
        return rows.isEmpty() ? null : rows.getFirst();
    }
}
