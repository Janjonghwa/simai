package com.ssafy.simai.domain.health;

import static org.assertj.core.api.Assertions.assertThat;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.TokenHashes;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.sql.Timestamp;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.simp.stomp.StompFrameHandler;
import org.springframework.messaging.simp.stomp.StompHeaders;
import org.springframework.messaging.simp.stomp.StompSession;
import org.springframework.messaging.simp.stomp.StompSessionHandlerAdapter;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import org.springframework.web.socket.messaging.WebSocketStompClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 노트북 하트비트 수신과 System Health(S15P31A101-262, 명세 3.2 이중 감시·12.4·12.6). 실제 포트로 Tier 0(I1)·노트북(기기 토큰)처럼 보내고
 * GET /api/health 와 /topic/health 가 같은 상태를 보이는지 본다. 시험 설정의 끊김 기준은 1.5초다(운영 3초).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class HealthIntegrationTest {

    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final JsonSchemaFactory SCHEMAS = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012,
            builder -> builder.schemaMappers(m -> m.mapPrefix("https://simai.dev/contracts/",
                    CONTRACTS.toAbsolutePath().toUri().toString())));
    private static final long LINK_TIMEOUT_MS = 1_500;

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    private final HttpClient http = HttpClient.newHttpClient();
    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String laptopId = "laptop-" + tag;
    private final String laptopToken = "test-only-device-token-" + tag;
    private final String gatewayId = "tier0-health-" + tag;
    private long heartbeatSeq;

    @BeforeEach
    void device() {
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptopId, TokenHashes.sha256Hex(laptopToken));
    }

    @AfterEach
    void cleanUp() throws InterruptedException {
        // 화면 상태(HealthState)는 시험 컨텍스트 내내 남는다. 이 시험의 노트북·Tier 0 가 다음 시험에서 살아 있는 것으로 보이지 않게 기다린다
        Thread.sleep(LINK_TIMEOUT_MS + 200);
        jdbc.update("DELETE FROM device_heartbeat WHERE device_id = ?", laptopId);
        jdbc.update("DELETE FROM device WHERE device_id = ?", laptopId);
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM skeleton_3d s USING world_track_state w"
                + " WHERE s.observation_at = w.observation_at AND s.world_id = w.world_id AND w.gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM world_track_state WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", "scene-test-" + tag);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", "cal-health-" + tag);
    }

    @Test
    void 하트비트는_기기_토큰과_같은_노트북만_받아_기록한다() throws Exception {
        assertThat(postHeartbeat(null, heartbeat("CONNECTED", "NORMAL"))).isEqualTo(401);
        ObjectNode other = heartbeat("CONNECTED", "NORMAL");
        other.put("source_id", "laptop-someone-else");
        assertThat(postHeartbeat(laptopToken, other)).isEqualTo(400);
        ObjectNode broken = heartbeat("CONNECTED", "NORMAL");
        ((ObjectNode) broken.get("payload")).put("site_display", "PARTY");
        assertThat(postHeartbeat(laptopToken, broken)).isEqualTo(400);

        assertThat(postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"))).isEqualTo(204);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM device_heartbeat WHERE device_id = ?", Integer.class, laptopId))
                .isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT last_heartbeat_at IS NOT NULL FROM device WHERE device_id = ?", Boolean.class,
                laptopId)).isTrue();
    }

    @Test
    void 노트북_하트비트가_끊기면_GET_과_토픽이_같은_단절을_보이고_Scene_불일치를_표시한다() throws Exception {
        commitScene("scene-test-" + tag);
        try (Tier0 tier0 = tier0(); Viewer viewer = viewer()) {
            ObjectNode health = tier0Health();
            tier0.send(health);
            tier0.send(worldState());
            assertThat(postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"))).isEqualTo(204);
            Thread.sleep(200);

            JsonNode normal = getHealth();
            assertValid(normal);
            JsonNode p = normal.get("payload");
            assertThat(p.get("remote_state").asString()).isEqualTo("NORMAL");
            assertThat(p.get("tier0_link").asString()).isEqualTo("CONNECTED");
            assertThat(p.at("/laptop/link").asString()).isEqualTo("CONNECTED");
            assertThat(p.get("system_health").asString()).isEqualTo(health.at("/payload/system_health").asString());
            assertThat(p.get("tier0_health")).isEqualTo(health.get("payload"));
            assertThat(p.at("/scene/committed_version").asString()).isEqualTo("scene-test-" + tag);
            assertThat(p.at("/scene/live_version").asString()).isEqualTo("scene-3");
            assertThat(p.at("/scene/mismatch").booleanValue()).isTrue();
            assertThat(p.get("data_age_ms").isIntegralNumber()).isTrue();

            // Tier 0 health 는 계속 오는데 노트북 하트비트가 멈춘다
            long deadline = System.currentTimeMillis() + LINK_TIMEOUT_MS + 1_000;
            while (System.currentTimeMillis() < deadline) {
                tier0.send(tier0Health());
                Thread.sleep(300);
            }
            // 확인 직전에도 Tier 0 health 를 받아 둔다(느린 기계에서 Tier 0 쪽까지 끊긴 것으로 보지 않게)
            tier0.send(tier0Health());
            Thread.sleep(200);
            JsonNode lost = getHealth();
            assertValid(lost);
            assertThat(lost.at("/payload/laptop/link").asString()).isEqualTo("DISCONNECTED");
            assertThat(lost.at("/payload/tier0_link").asString()).isEqualTo("CONNECTED");
            assertThat(lost.at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
            assertThat(lost.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");

            JsonNode topic = viewer.await(m -> "DISCONNECTED".equals(m.at("/payload/laptop/link").asString()));
            assertValid(topic);
            assertThat(topic.at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
            assertThat(topic.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");
        }
    }

    @Test
    void Tier0_만_끊기면_노트북이_본_상태로_원격_단절과_관제_정지를_가른다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(tier0Health());
        }
        // Tier 0 health 가 끊긴 동안 노트북은 계속 보낸다
        long deadline = System.currentTimeMillis() + LINK_TIMEOUT_MS + 700;
        while (System.currentTimeMillis() < deadline) {
            postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"));
            Thread.sleep(300);
        }
        JsonNode remote = getHealth();
        assertValid(remote);
        assertThat(remote.at("/payload/tier0_link").asString()).isEqualTo("DISCONNECTED");
        assertThat(remote.at("/payload/remote_state").asString()).isEqualTo("REMOTE_LINK_LOST");
        assertThat(remote.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");
        assertThat(remote.at("/payload/config/sync").asString()).isEqualTo("UNVERIFIED");

        // 노트북이 Tier 0 와 연결돼 있어도 처리가 멈췄다고 알리면 관제 정지다
        postHeartbeat(laptopToken, heartbeat("CONNECTED", "CONTROL_HALTED"));
        assertThat(getHealth().at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
        postHeartbeat(laptopToken, heartbeat("DISCONNECTED", "NORMAL"));
        assertThat(getHealth().at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
    }

    @Test
    void 연결이_모두_살아_있어도_노트북이_관제_정지를_알리면_NORMAL_이_아니다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(tier0Health());
            assertThat(postHeartbeat(laptopToken, heartbeat("CONNECTED", "CONTROL_HALTED"))).isEqualTo(204);
            Thread.sleep(200);
            JsonNode halted = getHealth();
            assertValid(halted);
            assertThat(halted.at("/payload/tier0_link").asString()).isEqualTo("CONNECTED");
            assertThat(halted.at("/payload/laptop/link").asString()).isEqualTo("CONNECTED");
            assertThat(halted.at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
            assertThat(halted.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");
        }
    }

    @Test
    void 재세팅이_필요한_카메라는_Tier0_health_값을_그대로_내주고_해소되면_빈_배열이_되며_모르면_싣지_않는다() throws Exception {
        try (Tier0 tier0 = tier0(); Viewer viewer = viewer()) {
            // 이전 버전 Tier 0(re_setup 없음): 모름 — 재세팅 없음으로 보이게 하지 않는다
            tier0.send(tier0Health());
            Thread.sleep(200);
            JsonNode unknown = getHealth();
            assertValid(unknown);
            assertThat(unknown.get("schema_version").asString()).isEqualTo("1.3.0");
            assertThat(unknown.at("/payload").has("re_setup")).isFalse();

            // 두 카메라가 재세팅을 요구한다(순서는 camera_id 오름차순으로 내준다)
            long detected = Instant.now().toEpochMilli() - 5_000;
            tier0.send(tier0HealthWithReSetup(reSetupItem("cam_C", "매칭 부족", detected), reSetupItem("cam_A", "위치 이동 의심", detected + 1)));
            Thread.sleep(200);
            JsonNode needed = getHealth();
            assertValid(needed);
            JsonNode items = needed.at("/payload/re_setup");
            assertThat(items.size()).isEqualTo(2);
            assertThat(items.get(0).get("camera_id").asString()).isEqualTo("cam_A");
            assertThat(items.get(0).get("reason").asString()).isEqualTo("위치 이동 의심");
            assertThat(items.get(0).get("detected_at").longValue()).isEqualTo(detected + 1);
            assertThat(items.get(1).get("camera_id").asString()).isEqualTo("cam_C");
            // 경보·판정은 바꾸지 않는다: system_health 는 Tier 0 가 보고한 값 그대로다
            assertThat(needed.at("/payload/system_health").asString()).isEqualTo(unknown.at("/payload/system_health").asString());
            assertThat(needed.at("/payload/tier0_health/re_setup").size()).isEqualTo(2);
            JsonNode topic = viewer.await(m -> m.at("/payload/re_setup").size() == 2);
            assertValid(topic);

            // 해소: 빈 배열은 '필요한 카메라 없음'이다
            tier0.send(tier0HealthWithReSetup());
            Thread.sleep(200);
            JsonNode resolved = getHealth();
            assertValid(resolved);
            assertThat(resolved.at("/payload").has("re_setup")).isTrue();
            assertThat(resolved.at("/payload/re_setup").size()).isZero();
            assertValid(viewer.await(m -> m.at("/payload").has("re_setup") && m.at("/payload/re_setup").size() == 0));

            // 형식이 맞지 않는 항목은 화면 계약을 깨지 않게 버린다
            ObjectNode broken = tier0HealthWithReSetup(reSetupItem("cam_A", "사유", detected), reSetupItem("cam_B", "사유", detected));
            ((ObjectNode) broken.at("/payload/re_setup").get(0)).put("detected_at", "yesterday");
            tier0.send(broken);
            Thread.sleep(200);
            JsonNode filtered = getHealth();
            assertValid(filtered);
            assertThat(filtered.at("/payload/re_setup").size()).isEqualTo(1);
            assertThat(filtered.at("/payload/re_setup/0/camera_id").asString()).isEqualTo("cam_B");
        }
    }

    @Test
    void Tier0_연결이_끊기면_재세팅_필요를_싣지_않는다() throws Exception {
        try (Tier0 tier0 = tier0()) {
            tier0.send(tier0HealthWithReSetup(reSetupItem("cam_C", "매칭 부족", Instant.now().toEpochMilli() - 5_000)));
            Thread.sleep(200);
            assertThat(getHealth().at("/payload/re_setup").size()).isEqualTo(1);
        }
        Thread.sleep(LINK_TIMEOUT_MS + 700);
        JsonNode lost = getHealth();
        assertValid(lost);
        assertThat(lost.at("/payload/tier0_link").asString()).isEqualTo("DISCONNECTED");
        assertThat(lost.at("/payload").has("re_setup")).isFalse();
        assertThat(lost.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");
    }

    @Test
    void 선택_항목_형식이_틀리면_500_이_아니라_400_이고_카메라_ID_는_그대로_남긴다() throws Exception {
        ObjectNode badEc2 = heartbeat("CONNECTED", "NORMAL");
        ((ObjectNode) badEc2.get("payload")).putObject("ec2").put("state", 1);
        assertThat(postHeartbeat(laptopToken, badEc2)).isEqualTo(400);
        ObjectNode badAlarm = heartbeat("CONNECTED", "NORMAL");
        ((ObjectNode) badAlarm.get("payload")).put("alarm", "loud");
        assertThat(postHeartbeat(laptopToken, badAlarm)).isEqualTo(400);

        ObjectNode odd = heartbeat("CONNECTED", "NORMAL");
        ((ObjectNode) odd.at("/payload/tier0")).putArray("video_cameras").add("cam,1").add("NULL").add("{x\"");
        assertThat(postHeartbeat(laptopToken, odd)).isEqualTo(204);
        assertThat(jdbc.queryForObject("SELECT array_to_string(tier0_video_cameras, '|') FROM device_heartbeat WHERE device_id = ?",
                String.class, laptopId)).isEqualTo("cam,1|NULL|{x\"");
    }

    @Test
    void health_는_오는데_처리가_멈추면_관제_정지로_보고_장애를_기록한다() throws Exception {
        Instant start = Instant.now();
        long stuckAt = start.toEpochMilli() - 100;
        try (Tier0 tier0 = tier0()) {
            tier0.send(stalledTier0Health(stuckAt - 1));
            // 연결과 하트비트는 살아 있지만 t_latest_global 이 늘지 않는다(FR-11 처리 정체)
            long deadline = System.currentTimeMillis() + LINK_TIMEOUT_MS + 1_200;
            while (System.currentTimeMillis() < deadline) {
                tier0.send(stalledTier0Health(stuckAt));
                postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"));
                Thread.sleep(300);
            }
            JsonNode stalled = getHealth();
            assertValid(stalled);
            assertThat(stalled.at("/payload/tier0_link").asString()).isEqualTo("CONNECTED");
            assertThat(stalled.at("/payload/laptop/link").asString()).isEqualTo("CONNECTED");
            assertThat(stalled.at("/payload/remote_state").asString()).isEqualTo("CONTROL_HALTED");
            assertThat(stalled.at("/payload/system_health").asString()).isEqualTo("UNSAFE_UNKNOWN");
            awaitOutage("CONTROL_HALTED", start);

            // 처리가 다시 나아가면 정상으로 돌아오고 장애 기록이 닫힌다
            tier0.send(tier0Health());
            postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"));
            Thread.sleep(300);
            assertThat(getHealth().at("/payload/remote_state").asString()).isEqualTo("NORMAL");
            awaitClosed("CONTROL_HALTED", start);
        }
    }

    @Test
    void Tier0_시각이_뒤로_가도_다시_나아가면_관제_정지로_굳지_않는다() throws Exception {
        long base = Instant.now().toEpochMilli();
        try (Tier0 tier0 = tier0()) {
            tier0.send(stalledTier0Health(base));
            // Tier 0 시계가 60초 뒤로 바뀐 뒤 그 기준에서 계속 나아간다(시계 차이 재추정·재시작)
            long restart = base - 60_000;
            long deadline = System.currentTimeMillis() + 3_500;
            for (int i = 0; System.currentTimeMillis() < deadline; i++) {
                tier0.send(stalledTier0Health(restart + i * 300L));
                postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"));
                Thread.sleep(300);
            }
            JsonNode health = getHealth();
            assertThat(health.at("/payload/remote_state").asString()).isEqualTo("NORMAL");
        }
    }

    @Test
    void 노트북이_끊기면_10초_안에_현장_장비_응답_없음으로_기록한다() throws Exception {
        Instant start = Instant.now();
        try (Tier0 tier0 = tier0()) {
            tier0.send(tier0Health());
            assertThat(postHeartbeat(laptopToken, heartbeat("CONNECTED", "NORMAL"))).isEqualTo(204);
            long deadline = System.currentTimeMillis() + LINK_TIMEOUT_MS + 1_500;
            while (System.currentTimeMillis() < deadline) {
                tier0.send(tier0Health());
                Thread.sleep(300);
            }
            Map<String, Object> row = awaitOutage("DEVICE_DOWN", start);
            // G3: 마지막 하트비트 → 장애 판정이 10초 안(시험 기준은 끊김 1.5초 + 판정 주기 1초)
            long detectMs = ((Timestamp) row.get("detected_at")).getTime() - ((Timestamp) row.get("last_seen_at")).getTime();
            assertThat(detectMs).isBetween(LINK_TIMEOUT_MS, 10_000L);
        }
    }

    private Map<String, Object> awaitOutage(String kind, Instant since) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (System.nanoTime() < deadline) {
            List<Map<String, Object>> rows = jdbc.queryForList(
                    "SELECT * FROM system_outage WHERE kind = ? AND detected_at >= ? ORDER BY detected_at DESC",
                    kind, Timestamp.from(since));
            if (!rows.isEmpty()) {
                return rows.getFirst();
            }
            Thread.sleep(100);
        }
        throw new AssertionError(kind + " 장애 기록이 생기지 않았다");
    }

    private void awaitClosed(String kind, Instant since) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (System.nanoTime() < deadline) {
            Integer open = jdbc.queryForObject(
                    "SELECT count(*) FROM system_outage WHERE kind = ? AND detected_at >= ? AND ended_at IS NULL",
                    Integer.class, kind, Timestamp.from(since));
            if (open != null && open == 0) {
                return;
            }
            Thread.sleep(100);
        }
        throw new AssertionError(kind + " 장애 기록이 닫히지 않았다");
    }

    // ---- 도우미

    private void commitScene(String scene) {
        String calibration = "cal-health-" + tag;
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, 'classroom-1', 'CALIBRATED', 'G1_AUTO', '시험', now(), 'test', 'sha256:test', now())
                """, calibration);
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, is_upper_bound, committed_at, valid_from)
                VALUES (?, 'classroom-1', ?, 'COMMITTED', false, now() + interval '1 day', now())
                """, scene, calibration);
    }

    private ObjectNode example(String name) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        return root;
    }

    /** 처리가 나아가는(t_latest_global 이 늘어나는) Tier 0 health */
    private ObjectNode tier0Health() throws Exception {
        ObjectNode root = example("health.json");
        root.put("source_id", gatewayId);
        ((ObjectNode) root.get("payload")).put("t_latest_global", Instant.now().toEpochMilli() - 100);
        return root;
    }

    /** 처리가 나아가는 Tier 0 health(2.2.0) + 재세팅 필요 카메라 목록(빈 목록이면 '없음') */
    private ObjectNode tier0HealthWithReSetup(ObjectNode... items) throws Exception {
        ObjectNode root = tier0Health();
        root.put("schema_version", "2.2.0");
        var array = ((ObjectNode) root.get("payload")).putArray("re_setup");
        for (ObjectNode item : items) {
            array.add(item);
        }
        return root;
    }

    private ObjectNode reSetupItem(String cameraId, String reason, long detectedAt) {
        return MAPPER.createObjectNode().put("camera_id", cameraId).put("reason", reason).put("detected_at", detectedAt);
    }

    /** 처리가 멈춘(t_latest_global 이 그대로인) Tier 0 health */
    private ObjectNode stalledTier0Health(long tLatestGlobalMs) throws Exception {
        ObjectNode root = example("health.json");
        root.put("source_id", gatewayId);
        ((ObjectNode) root.get("payload")).put("t_latest_global", tLatestGlobalMs);
        return root;
    }

    private ObjectNode worldState() throws Exception {
        ObjectNode root = example("world_state.json");
        root.put("source_id", gatewayId);
        ((ObjectNode) root.get("payload")).put("t_latest_global", Instant.now().toEpochMilli() - 200);
        return root;
    }

    private ObjectNode heartbeat(String control, String display) throws Exception {
        ObjectNode root = example("laptop_heartbeat.json");
        root.put("source_id", laptopId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("heartbeat_seq", heartbeatSeq++);
        ((ObjectNode) p.get("tier0")).put("control", control);
        p.put("site_display", display);
        return root;
    }

    private int postHeartbeat(String token, JsonNode body) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/devices/heartbeat"))
                .header(HttpHeaders.CONTENT_TYPE, "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(MAPPER.writeValueAsString(body)));
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return http.send(request.build(), HttpResponse.BodyHandlers.discarding()).statusCode();
    }

    private JsonNode getHealth() throws Exception {
        HttpResponse<String> response = http.send(HttpRequest.newBuilder(URI.create("http://localhost:" + port + "/api/health"))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + viewerToken()).GET().build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).isEqualTo(200);
        return MAPPER.readTree(response.body());
    }

    private static void assertValid(JsonNode message) {
        List<String> problems = SCHEMAS.getSchema(SchemaLocation.of("https://simai.dev/contracts/messages/viewer_health.schema.json"))
                .validate(MAPPER.writeValueAsString(message), InputFormat.JSON).stream().map(Object::toString).toList();
        assertThat(problems).isEmpty();
    }

    private String viewerToken() {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, "VIEWER").build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private Tier0 tier0() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        WebSocketSession session = new StandardWebSocketClient().execute(new TextWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session);
    }

    private record Tier0(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }

    private Viewer viewer() throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        StompHeaders connect = new StompHeaders();
        connect.add(HttpHeaders.AUTHORIZATION, "Bearer " + viewerToken());
        StompSession session = client.connectAsync(URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                new WebSocketHttpHeaders(), connect, new StompSessionHandlerAdapter() { }).get(5, TimeUnit.SECONDS);
        BlockingQueue<JsonNode> queue = new LinkedBlockingQueue<>();
        session.subscribe(RealtimeTopics.HEALTH, new StompFrameHandler() {
            @Override
            public Type getPayloadType(StompHeaders headers) {
                return byte[].class;
            }

            @Override
            public void handleFrame(StompHeaders headers, Object payload) {
                queue.add(MAPPER.readTree(new String((byte[]) payload, StandardCharsets.UTF_8)));
            }
        });
        return new Viewer(client, session, queue);
    }

    /** 관제 앱 흉내. /topic/health 를 모은다 */
    private record Viewer(WebSocketStompClient client, StompSession session, BlockingQueue<JsonNode> queue) implements AutoCloseable {

        JsonNode await(Predicate<JsonNode> matches) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = queue.poll(200, TimeUnit.MILLISECONDS);
                if (message != null && matches.test(message)) {
                    return message;
                }
            }
            throw new AssertionError("/topic/health 메시지가 오지 않았다");
        }

        @Override
        public void close() {
            if (session.isConnected()) {
                session.disconnect();
            }
            client.stop();
        }
    }
}
