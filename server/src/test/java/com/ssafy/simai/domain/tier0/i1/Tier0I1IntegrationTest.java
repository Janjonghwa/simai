package com.ssafy.simai.domain.tier0.i1;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.function.Supplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** I1(/ws/tier0) 을 실제 포트로 띄우고 contracts/examples 의 world_state·health 를 보내 저장을 본다(S15P31A101-103). */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class Tier0I1IntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TOKEN = "test-only-tier0-token";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MeterRegistry meters;

    /** 시험마다 다른 gateway_id 를 써서 다른 시험·이전 실행의 행과 섞이지 않게 한다 */
    private final String gatewayId = "tier0-test-" + UUID.randomUUID();
    private final long baseMs = Instant.parse("2026-01-01T00:00:00Z").toEpochMilli() + (long) (Math.random() * 1_000_000_000L);

    @AfterEach
    void cleanUp() {
        // (시각, world_id) 쌍으로 지운다. 둘을 따로 거르면 다른 시험의 골격까지 지울 수 있다
        jdbc.update("DELETE FROM skeleton_3d s USING world_track_state w"
                + " WHERE s.observation_at = w.observation_at AND s.world_id = w.world_id AND w.gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM world_track_state WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gatewayId);
    }

    @Test
    void 맞는_토큰이면_world_state_의_트랙과_골격_health_를_저장한다() throws Exception {
        ObjectNode worldState = example("world_state.json", baseMs);
        ObjectNode health = example("health.json", baseMs);
        int tracks = worldState.path("payload").path("tracks").size();

        try (Closer session = connect(TOKEN)) {
            session.send(worldState);
            session.send(health);
        }

        awaitCount(() -> count("world_track_state"), tracks);
        awaitCount(() -> count("system_health"), 1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM skeleton_3d s JOIN world_track_state w"
                + " ON s.world_id = w.world_id AND s.observation_at = w.observation_at WHERE w.gateway_id = ?",
                Integer.class, gatewayId)).isEqualTo(tracksWithSkeleton(worldState));
        // 예시의 첫 트랙: 위치는 SRID 0 점, 두 번째 관절은 null → 원소 NULL
        JsonNode first = worldState.path("payload").path("tracks").get(0);
        assertThat(jdbc.queryForObject("SELECT ST_X(position) FROM world_track_state WHERE gateway_id = ? AND world_id = ?",
                Double.class, gatewayId, first.get("world_id").asString()))
                .isEqualTo(first.path("position_xy").get(0).doubleValue());
        assertThat(jdbc.queryForObject("SELECT joints[2][1] IS NULL FROM skeleton_3d WHERE world_id = ? AND observation_at = "
                + "(SELECT observation_at FROM world_track_state WHERE gateway_id = ? AND world_id = ?)",
                Boolean.class, first.get("world_id").asString(), gatewayId, first.get("world_id").asString())).isTrue();
        assertThat(jdbc.queryForObject("SELECT system_health FROM system_health WHERE gateway_id = ?", String.class, gatewayId))
                .isEqualTo(health.path("payload").get("system_health").asString());
    }

    @Test
    void 같은_메시지를_다시_받아도_행이_늘지_않는다() throws Exception {
        ObjectNode worldState = example("world_state.json", baseMs);
        ObjectNode health = example("health.json", baseMs);

        try (Closer session = connect(TOKEN)) {
            session.send(worldState);
            session.send(health);
            session.send(worldState);
            session.send(health);
            // 마지막에 다른 health 하나를 더 보내 앞의 재전송이 다 처리된 것을 확인한다(한 세션의 메시지는 차례로 처리된다)
            session.send(example("health.json", baseMs + 1000));
        }

        awaitCount(() -> count("system_health"), 2);
        assertThat(count("world_track_state")).isEqualTo(worldState.path("payload").path("tracks").size());
    }

    @Test
    void 같은_시각에_다른_world_state_가_오면_나중_상태로_덮고_골격도_맞춘다() throws Exception {
        // 카메라가 멈춰 t_latest_global 이 그대로인 채 트랙 상태만 바뀐 tick
        ObjectNode first = example("world_state.json", baseMs);
        ObjectNode later = example("world_state.json", baseMs);
        ObjectNode laterTrack = (ObjectNode) later.path("payload").path("tracks").get(0);
        laterTrack.put("observation_state", "PREDICTED");
        laterTrack.remove("skeleton");
        String worldId = laterTrack.get("world_id").asString();
        double storedBefore = counter("world_state", "stored");

        try (Closer session = connect(TOKEN)) {
            session.send(first);
            session.send(later);
        }

        awaitCount(() -> (int) (counter("world_state", "stored") - storedBefore), 2);
        assertThat(jdbc.queryForObject("SELECT observation_state FROM world_track_state WHERE gateway_id = ? AND world_id = ?",
                String.class, gatewayId, worldId)).isEqualTo("PREDICTED");
        assertThat(jdbc.queryForObject("SELECT msg_id::text FROM world_track_state WHERE gateway_id = ? AND world_id = ?",
                String.class, gatewayId, worldId)).isEqualTo(later.get("msg_id").asString());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM skeleton_3d WHERE world_id = ? AND observation_at = "
                + "(SELECT observation_at FROM world_track_state WHERE gateway_id = ? AND world_id = ?)",
                Integer.class, worldId, gatewayId, worldId)).isZero();
    }

    @Test
    void 재전송은_duplicate_로_센다() throws Exception {
        ObjectNode worldState = example("world_state.json", baseMs);
        double duplicateBefore = counter("world_state", "duplicate");

        try (Closer session = connect(TOKEN)) {
            session.send(worldState);
            session.send(worldState);
        }

        awaitCount(() -> (int) (counter("world_state", "duplicate") - duplicateBefore), 1);
    }

    @Test
    void 타입이_틀린_트랙은_계약_위반으로_버리고_연결은_유지한다() throws Exception {
        ObjectNode nullCoordinate = example("world_state.json", baseMs);
        ObjectNode track = (ObjectNode) nullCoordinate.path("payload").path("tracks").get(0);
        track.putArray("position_xy").addNull().add(1.0);
        ObjectNode stringNumber = example("world_state.json", baseMs + 100);
        ((ObjectNode) stringNumber.path("payload").path("tracks").get(0)).put("head_z", "1.2");
        ObjectNode fractionalTime = example("health.json", baseMs);
        ((ObjectNode) fractionalTime.get("payload")).put("t_latest_global", baseMs + 0.5);
        ObjectNode hugeMajor = example("health.json", baseMs + 200);
        hugeMajor.put("schema_version", "99999999999.0.0");
        double invalidBefore = counter("world_state", "invalid");

        try (Closer session = connect(TOKEN)) {
            session.send(nullCoordinate);
            session.send(stringNumber);
            session.send(fractionalTime);
            session.send(hugeMajor);
            session.send(example("health.json", baseMs + 1000));
        }

        awaitCount(() -> count("system_health"), 1);
        assertThat(count("world_track_state")).isZero();
        assertThat(counter("world_state", "invalid") - invalidBefore).isEqualTo(2.0);
    }

    @Test
    void 인증_방식_이름은_대소문자를_가리지_않는다() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "bearer " + TOKEN);
        WebSocketSession session = new StandardWebSocketClient()
                .execute(new AbstractWebSocketHandler() { }, headers,
                        java.net.URI.create("ws://localhost:" + port + Tier0Paths.I1))
                .get(5, TimeUnit.SECONDS);
        assertThat(session.isOpen()).isTrue();
        session.close();
    }

    @Test
    void 기본_한도_8KiB_를_넘는_world_state_도_받는다() throws Exception {
        ObjectNode worldState = example("world_state.json", baseMs);
        ArrayNode tracks = (ArrayNode) worldState.path("payload").get("tracks");
        ObjectNode template = (ObjectNode) tracks.get(0);
        tracks.removeAll();
        for (int i = 0; i < 40; i++) {
            tracks.add(template.deepCopy().put("world_id", "w-big-" + i));
        }
        assertThat(MAPPER.writeValueAsString(worldState).length()).isGreaterThan(8 * 1024);

        try (Closer session = connect(TOKEN)) {
            session.send(worldState);
        }

        awaitCount(() -> count("world_track_state"), 40);
    }

    @Test
    void 틀린_토큰이면_연결을_거부한다() {
        assertThatThrownBy(() -> connect("wrong-token")).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> connect(null)).isInstanceOf(ExecutionException.class);
    }

    @Test
    void 깨진_메시지와_읽을_수_없는_메이저는_버리고_연결은_유지한다() throws Exception {
        ObjectNode futureMajor = example("health.json", baseMs);
        futureMajor.put("schema_version", "9.0.0");

        try (Closer session = connect(TOKEN)) {
            session.sendRaw("{not json");
            session.sendRaw("{\"msg_type\":\"health\"}");
            session.send(futureMajor);
            session.send(example("health.json", baseMs + 1000));
        }

        awaitCount(() -> count("system_health"), 1);
        assertThat(jdbc.queryForObject("SELECT extract(epoch FROM reported_at) * 1000 FROM system_health WHERE gateway_id = ?",
                Double.class, gatewayId)).isEqualTo((double) (baseMs + 1000));
    }

    // ---- 도우미

    /** 예시를 읽어 이 시험의 gateway_id·새 msg_id·시각으로 바꾼다 */
    private ObjectNode example(String file, long sentAtMs) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve(file)));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        root.put("sent_at", sentAtMs);
        ObjectNode payload = (ObjectNode) root.get("payload");
        if (payload.has("t_latest_global") && !payload.get("t_latest_global").isNull()) {
            payload.put("t_latest_global", sentAtMs);
        }
        return root;
    }

    private static int tracksWithSkeleton(JsonNode worldState) {
        return (int) worldState.path("payload").path("tracks").values().stream()
                .filter(t -> t.path("skeleton").isObject()).count();
    }

    /** 지표는 앱 전체에서 누적된다. 시험은 보내기 전후 차이만 본다 */
    private double counter(String msgType, String result) {
        Counter counter = meters.find(I1MessageRouter.METRIC).tags("msg_type", msgType, "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private int count(String table) {
        return jdbc.queryForObject("SELECT count(*) FROM " + table + " WHERE gateway_id = ?", Integer.class, gatewayId);
    }

    private static void awaitCount(Supplier<Integer> actual, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (actual.get() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(actual.get()).isEqualTo(expected);
    }

    private Closer connect(String token) throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        if (token != null) {
            headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        WebSocketSession session = new StandardWebSocketClient()
                .execute(new AbstractWebSocketHandler() { }, headers,
                        java.net.URI.create("ws://localhost:" + port + Tier0Paths.I1))
                .get(5, TimeUnit.SECONDS);
        return new Closer(session);
    }

    private record Closer(WebSocketSession session) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            sendRaw(MAPPER.writeValueAsString(message));
        }

        void sendRaw(String text) throws Exception {
            session.sendMessage(new TextMessage(text));
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
