package com.ssafy.simai.domain.replay;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
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
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 최소 리플레이 조회(S15P31A101-136). world_state·health 는 실제 I1 으로 보내 저장하고, 이벤트 전이는 risk_event_update 에 바로 넣는다
 * (전이 수신 경로는 RiskEventJournalIntegrationTest 가 본다). 되돌린 world_state 가 보낸 payload 와 같은지 본다.
 * 다른 시험의 기록과 시각이 겹치지 않게 시험마다 다른 시각·gateway_id 를 쓰고, 이벤트는 이 시험의 event_id 만 본다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class ReplayIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TOKEN = "test-only-tier0-token";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ContractSchemas contracts;

    private final String gatewayId = "tier0-replay-" + UUID.randomUUID();
    private final long baseMs = Instant.parse("2026-02-01T00:00:00Z").toEpochMilli() + (long) (Math.random() * 1_000_000_000L);
    private final List<UUID> eventIds = new ArrayList<>();
    private final HttpClient http = HttpClient.newHttpClient();

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM skeleton_3d s USING world_track_state w"
                + " WHERE s.observation_at = w.observation_at AND s.world_id = w.world_id AND w.gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM world_track_state WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM system_metric WHERE source_id = ?", gatewayId);
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
        for (UUID eventId : eventIds) {
            jdbc.update("DELETE FROM risk_event_update WHERE event_id = ?", eventId);
            jdbc.update("DELETE FROM risk_event WHERE event_id = ?", eventId);
        }
    }

    @Test
    void 구간의_world_state_를_받은_payload_그대로_되돌린다() throws Exception {
        List<ObjectNode> sent = List.of(worldState(baseMs), worldState(baseMs + 100), worldState(baseMs + 200));
        try (Session session = connect()) {
            for (ObjectNode message : sent) {
                session.send(message);
            }
        }
        // 계약 예시는 트랙 2개
        awaitTrackRows(6);

        JsonNode body = get("/api/replay?from=" + (baseMs - 50) + "&to=" + (baseMs + 250), "VIEWER", 200);
        assertThat(body.get("step_ms").longValue()).isEqualTo(100);
        JsonNode states = body.get("world_states");
        assertThat(states).hasSize(3);
        for (int i = 0; i < 3; i++) {
            JsonNode state = states.get(i);
            assertThat(state.get("msg_type").stringValue()).isEqualTo("world_state");
            assertThat(state.get("schema_version").stringValue()).isEqualTo("2.1.0");
            assertThat(state.get("source_id").stringValue()).isEqualTo("ec2");
            // msg_id 는 Tier 0 가 붙인 원래 값이라 같은 구간을 다시 읽어도 같다
            assertThat(state.get("msg_id").stringValue()).isEqualTo(sent.get(i).get("msg_id").stringValue());
            // 필수 칸의 null(pelvis_xyz 등)·빠진 선택 칸·float 로 저장한 소수(0.08 등)까지 보낸 그대로다
            assertThat(state.get("payload")).isEqualTo(sent.get(i).get("payload"));
        }

        // step_ms 칸마다 가장 이른 tick 하나: [base, base+200) → base, [base+200, …) → base+200
        JsonNode coarse = get("/api/replay?from=" + baseMs + "&to=" + (baseMs + 250) + "&step_ms=200", "VIEWER", 200);
        assertThat(coarse.get("world_states")).hasSize(2);
        assertThat(coarse.get("world_states").get(0).get("payload").get("t_latest_global").longValue()).isEqualTo(baseMs);
        assertThat(coarse.get("world_states").get(1).get("payload").get("t_latest_global").longValue()).isEqualTo(baseMs + 200);

        // 시각 t 직전 1초 안의 마지막 tick. 1초 넘게 기록이 없으면 보간하지 않고 null
        JsonNode at = get("/api/world?at=" + (baseMs + 150), "FOREMAN", 200);
        assertThat(at.get("world_state").get("payload")).isEqualTo(sent.get(1).get("payload"));
        assertThat(get("/api/world?at=" + (baseMs + 1300), "ADMIN", 200).get("world_state").isNull()).isTrue();
    }

    @Test
    void 촬영_시각이_흔들려도_기본_step_은_모든_tick_을_주고_계약에_맞는다() throws Exception {
        // 100 ms 칸 하나에 tick 둘(base+101, base+190)이 들어도 빠지지 않는다
        List<ObjectNode> sent = List.of(worldState(baseMs), worldState(baseMs + 101), worldState(baseMs + 190));
        try (Session session = connect()) {
            for (ObjectNode message : sent) {
                session.send(message);
            }
        }
        awaitTrackRows(6);

        JsonNode states = get("/api/replay?from=" + baseMs + "&to=" + (baseMs + 250), "VIEWER", 200).get("world_states");
        assertThat(states).hasSize(3);
        for (JsonNode state : states) {
            assertThat(contracts.violations("messages/world_state", MAPPER.writeValueAsString(state))).isEmpty();
        }
    }

    @Test
    void 시작_시각에_활성인_이벤트와_구간의_전이를_준다() throws Exception {
        UUID active = event();
        insertUpdate(active, 1, "RAISED", baseMs - 1000, false);
        insertUpdate(active, 2, "UPDATED", baseMs + 100, false);
        // 확인은 생명주기를 바꾸지 않아 상태로 내지 않는다
        insertUpdate(active, 3, "ACKNOWLEDGED", baseMs + 150, false);
        insertUpdate(active, 4, "RESOLVED", baseMs + 200, false);
        UUID resolvedBefore = event();
        insertUpdate(resolvedBefore, 1, "RAISED", baseMs - 2000, false);
        insertUpdate(resolvedBefore, 2, "RESOLVED", baseMs - 1500, false);
        UUID resumed = event();
        insertUpdate(resumed, 1, "RAISED", baseMs - 3000, false);
        insertUpdate(resumed, 2, "RESOLVED", baseMs - 2500, false);
        insertUpdate(resumed, 3, "UPDATED", baseMs + 50, true);

        JsonNode body = get("/api/replay?from=" + baseMs + "&to=" + (baseMs + 250), "VIEWER", 200);
        List<JsonNode> initial = mine(body.get("initial_events"));
        assertThat(initial).hasSize(1);
        JsonNode start = initial.getFirst();
        assertThat(start.get("msg_type").stringValue()).isEqualTo("risk_event");
        assertThat(start.get("schema_version").stringValue()).isEqualTo(ReplayRepository.RISK_EVENT_VERSION);
        JsonNode startPayload = start.get("payload");
        assertThat(startPayload.get("event_id").stringValue()).isEqualTo(active.toString());
        assertThat(startPayload.get("lifecycle").stringValue()).isEqualTo("RAISED");
        assertThat(startPayload.get("event_sequence").intValue()).isEqualTo(1);
        assertThat(startPayload.get("raised_at").longValue()).isEqualTo(baseMs - 1000);
        assertThat(startPayload.get("rationale").isNull()).isTrue();
        // 재시작 복원 표시(delivery): 이 시험의 전이는 실시간(REALTIME)이다
        assertThat(startPayload.get("delivery").asString()).isEqualTo("REALTIME");

        List<JsonNode> events = mine(body.get("events"));
        assertThat(events).extracting(e -> e.get("payload").get("event_sequence").intValue() + "/"
                + e.get("payload").get("lifecycle").stringValue()).containsExactly("3/UPDATED", "2/UPDATED", "4/RESOLVED");
        JsonNode resumedState = events.getFirst().get("payload");
        assertThat(resumedState.get("event_id").stringValue()).isEqualTo(resumed.toString());
        assertThat(resumedState.get("resume_count").intValue()).isEqualTo(1);
        assertThat(resumedState.has("resolved_at")).isFalse();
        for (JsonNode event : events) {
            assertThat(contracts.violations("messages/risk_event", MAPPER.writeValueAsString(event))).isEmpty();
        }
        assertThat(contracts.violations("messages/risk_event", MAPPER.writeValueAsString(start))).isEmpty();
        JsonNode resolved = events.getLast().get("payload");
        // 앞 전이에 기대는 칸은 앞 전이들로 센다
        assertThat(resolved.get("raised_at").longValue()).isEqualTo(baseMs - 1000);
        assertThat(resolved.get("resolved_at").longValue()).isEqualTo(baseMs + 200);
        assertThat(resolved.get("updated_at").longValue()).isEqualTo(baseMs + 200);

        // 시각 t 에 활성인 이벤트
        assertThat(mine(get("/api/world?at=" + (baseMs + 150), "VIEWER", 200).get("events")))
                .extracting(e -> e.get("payload").get("event_id").stringValue() + "/" + e.get("payload").get("lifecycle").stringValue())
                .containsExactlyInAnyOrder(active + "/UPDATED", resumed + "/UPDATED");
        assertThat(mine(get("/api/world?at=" + (baseMs + 300), "VIEWER", 200).get("events")))
                .extracting(e -> e.get("payload").get("event_id").stringValue()).containsExactly(resumed.toString());
    }

    @Test
    void Tier0_health_가_3초_넘게_없던_구간을_기록_없음으로_준다() throws Exception {
        try (Session session = connect()) {
            session.send(health(baseMs + 500));
            session.send(health(baseMs + 1500));
            session.send(health(baseMs + 8000));
        }
        awaitHealthRows(3);

        JsonNode gaps = get("/api/replay?from=" + baseMs + "&to=" + (baseMs + 10_000), "VIEWER", 200).get("gaps");
        assertThat(gaps).hasSize(1);
        assertThat(gaps.get(0).get("from").longValue()).isEqualTo(baseMs + 1500);
        assertThat(gaps.get(0).get("to").longValue()).isEqualTo(baseMs + 8000);
        // health 가 하나도 없으면 구간 전체가 기록 없음이다
        JsonNode empty = get("/api/replay?from=" + (baseMs + 20_000) + "&to=" + (baseMs + 30_000), "VIEWER", 200);
        assertThat(empty.get("gaps")).hasSize(1);
        assertThat(empty.get("gaps").get(0).get("from").longValue()).isEqualTo(baseMs + 20_000);
        assertThat(empty.get("gaps").get(0).get("to").longValue()).isEqualTo(baseMs + 30_000);
    }

    @Test
    void 로그인이_필요하고_시각이_맞지_않거나_구간이_크면_400() throws Exception {
        get("/api/replay?from=" + baseMs + "&to=" + (baseMs + 1000), null, 401);
        get("/api/world?at=" + baseMs, null, 401);
        assertThat(code("/api/replay?to=" + baseMs)).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/replay?from=" + baseMs + "&to=" + baseMs)).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/replay?from=abc&to=" + baseMs)).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/replay?from=-1&to=" + baseMs)).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/replay?from=" + baseMs + "&to=" + (baseMs + 1000) + "&step_ms=50")).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/replay?from=" + baseMs + "&to=" + (baseMs + 1000) + "&step_ms=60001")).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/world")).isEqualTo("INVALID_REPLAY_QUERY");
        assertThat(code("/api/world?at=9000000000000000")).isEqualTo("INVALID_REPLAY_QUERY");
        // 칸 수는 구간으로 센다: 100 ms 로 3000칸까지, 넘으면 거부
        long limit = (ReplayController.MAX_FRAMES - 1) * 100L;
        get("/api/replay?from=" + baseMs + "&to=" + (baseMs + limit), "VIEWER", 200);
        assertThat(code("/api/replay?from=" + baseMs + "&to=" + (baseMs + limit + 100))).isEqualTo("REPLAY_RANGE_TOO_LARGE");
        get("/api/replay?from=" + baseMs + "&to=" + (baseMs + limit + 100) + "&step_ms=200", "VIEWER", 200);
        // step_ms 를 키워도 구간은 1시간까지
        long hour = ReplayController.MAX_RANGE_MS;
        get("/api/replay?from=" + baseMs + "&to=" + (baseMs + hour) + "&step_ms=60000", "VIEWER", 200);
        assertThat(code("/api/replay?from=" + baseMs + "&to=" + (baseMs + hour + 1) + "&step_ms=60000"))
                .isEqualTo("REPLAY_RANGE_TOO_LARGE");
    }

    private ObjectNode worldState(long tMs) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("world_state.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        root.put("sent_at", tMs + 20);
        ((ObjectNode) root.get("payload")).put("t_latest_global", tMs);
        return root;
    }

    private ObjectNode health(long sentAtMs) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("health.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        root.put("sent_at", sentAtMs);
        ((ObjectNode) root.get("payload")).put("t_latest_global", sentAtMs);
        return root;
    }

    private UUID event() {
        UUID eventId = uuid7();
        eventIds.add(eventId);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, metrics, resume_count, retention_hold)
                VALUES (?, 'LIVE', ?, 'R1', 1, 'RAISED', 'VERIFIED', 'HIGH', 'HIGH', false, '{w-0012}', '{}', 0, false)
                """, eventId, gatewayId);
        return eventId;
    }

    /** 계약 uuid_v7(event_id·msg_id 의 판 7 형식) */
    private static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
    }

    private void insertUpdate(UUID eventId, int sequence, String transition, long observationMs, boolean resumed) {
        Timestamp at = new Timestamp(observationMs);
        jdbc.update("""
                INSERT INTO risk_event_update (event_id, event_sequence, msg_id, gateway_id, journal_offset, transition,
                    occurred_at, observation_at, evaluated_at, rule_id, rule_version, config_version, calibration_version,
                    scene_version, source, severity, confidence, verification, needs_check, world_ids, zone_id, metrics,
                    resent, resumed, delivery_mode, received_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'R1', 1, 'cfg-1', 'cal-1', 'scene-1', 'LIVE', 'HIGH', 'HIGH', 'VERIFIED',
                    false, '{w-0012}', 'zone-a', '{"dwell_s": 1.5}', false, ?, 'REALTIME', ?)
                """, eventId, sequence, uuid7(), gatewayId, (long) eventIds.indexOf(eventId) * 100 + sequence,
                transition, at, at, at, resumed, at);
    }

    /** 이 시험의 이벤트만(다른 시험이 남긴 활성 이벤트가 같은 시각에 걸릴 수 있다) */
    private List<JsonNode> mine(JsonNode events) {
        List<String> ids = eventIds.stream().map(UUID::toString).toList();
        return events.valueStream().filter(e -> ids.contains(e.get("payload").get("event_id").stringValue())).toList();
    }

    private void awaitTrackRows(int expected) throws InterruptedException {
        await("SELECT count(*) FROM world_track_state WHERE gateway_id = ?", expected);
    }

    private void awaitHealthRows(int expected) throws InterruptedException {
        await("SELECT count(*) FROM system_health WHERE gateway_id = ?", expected);
    }

    private void await(String sql, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (jdbc.queryForObject(sql, Integer.class, gatewayId) < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(jdbc.queryForObject(sql, Integer.class, gatewayId)).isEqualTo(expected);
    }

    private String code(String path) throws Exception {
        return get(path, "VIEWER", 400).get("code").stringValue();
    }

    private Session connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TOKEN);
        return new Session(new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS));
    }

    private JsonNode get(String path, String role, int expectedStatus) throws Exception {
        HttpRequest.Builder request = HttpRequest.newBuilder(URI.create("http://localhost:" + port + path)).GET();
        if (role != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt(role));
        }
        HttpResponse<String> response = http.send(request.build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(response.body()).isEqualTo(expectedStatus);
        return response.body().isEmpty() ? MAPPER.createObjectNode() : MAPPER.readTree(response.body());
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private record Session(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
