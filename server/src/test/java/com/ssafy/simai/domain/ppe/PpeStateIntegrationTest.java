package com.ssafy.simai.domain.ppe;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.tier0.Tier0Paths;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.TimeUnit;
import java.util.function.IntSupplier;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
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

/**
 * ppe_state 수신(S15P31A101-516, 명세 12.3). 실제 I1 으로 계약 예시를 보내 이력이 추가되고 현재 상태가 더 새 메시지로만 바뀌는지,
 * 재전송·계약 위반을 거르는지 본다. 시험마다 다른 source_id·world_id 를 써서 다른 시험의 행과 섞이지 않게 하고 끝나면 지운다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class PpeStateIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final String METRIC = "simai.i1.messages";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    MeterRegistry meters;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String sourceId = "tier0-ppe-" + tag;
    private final String worn = "w-ppe-a-" + tag;
    private final String unknown = "w-ppe-b-" + tag;
    /** 촬영 시각 기준. 필드 초기화는 Spring 컨텍스트 기동 전이라, 첫 시험이 기동을 기다리는 동안 만료(10초)를 넘기지 않게 시험 직전에 정한다 */
    private long now;

    @BeforeEach
    void clock() {
        now = System.currentTimeMillis();
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM ppe_state WHERE source_id = ?", sourceId);
        jdbc.update("DELETE FROM ppe_current_state WHERE source_id = ?", sourceId);
    }

    @Test
    void 사람마다_이력을_추가하고_현재_상태를_두며_재전송은_한_번만_쓴다() throws Exception {
        ObjectNode message = message(1_000L, 10, now);
        double stored = counter("stored");
        double duplicate = counter("duplicate");
        try (Tier0 tier0 = connect()) {
            tier0.send(message);
            ObjectNode resent = message.deepCopy();
            resent.put("msg_id", UUID.randomUUID().toString());
            tier0.send(resent);
        }
        awaitCount(() -> (int) (counter("stored") - stored), 1);
        awaitCount(() -> (int) (counter("duplicate") - duplicate), 1);

        assertThat(jdbc.queryForObject("SELECT count(*) FROM ppe_state WHERE source_id = ?", Integer.class, sourceId)).isEqualTo(2);
        Map<String, Object> row = jdbc.queryForMap("""
                SELECT helmet_state, vest_state, helmet_evidence->0->>'camera_id' AS camera, input_provenance->'tracks'->>'source' AS tracks
                FROM ppe_state WHERE source_id = ? AND world_id = ?
                """, sourceId, worn);
        assertThat(row).containsEntry("helmet_state", "WORN").containsEntry("vest_state", "NOT_WORN")
                .containsEntry("camera", "cam_B").containsEntry("tracks", "TIER0_LIVE");
        assertThat(current(worn)).containsEntry("helmet_state", "WORN").containsEntry("state_sequence", 10L);
        assertThat(current(unknown)).containsEntry("helmet_state", "UNKNOWN");
    }

    @Test
    void 과거_만료_이전_판_메시지는_이력에만_남고_현재_상태를_덮지_않는다() throws Exception {
        ObjectNode latest = message(1_000L, 20, now);
        // 순번·촬영 시각이 더 이른 메시지(순서가 뒤바뀌어 늦게 옴)
        ObjectNode older = withHelmet(message(1_000L, 19, now - 500), "NOT_WORN");
        // 받는 순간 이미 만료된 상태(촬영 시각이 수신보다 10초 넘게 이름), 순번은 더 큼
        ObjectNode stale = withHelmet(message(1_000L, 21, now - 60_000), "NOT_WORN");
        // 더 새 촬영 시각이지만 identity_revision 이 낮다(이전 사람의 근거)
        ObjectNode lowerRevision = withHelmet(message(1_000L, 22, now + 500), "NOT_WORN");
        people(lowerRevision).forEach(p -> ((ObjectNode) p).put("identity_revision", 1));

        double stored = counter("stored");
        try (Tier0 tier0 = connect()) {
            tier0.send(latest);
            tier0.send(older);
            tier0.send(stale);
            tier0.send(lowerRevision);
        }
        awaitCount(() -> (int) (counter("stored") - stored), 4);

        assertThat(jdbc.queryForObject("SELECT count(*) FROM ppe_state WHERE source_id = ?", Integer.class, sourceId)).isEqualTo(8);
        assertThat(current(worn)).containsEntry("helmet_state", "WORN").containsEntry("state_sequence", 20L);
    }

    @Test
    void PPE_재시작으로_stream_epoch_가_커져도_더_새_상태로_바뀐다() throws Exception {
        double stored = counter("stored");
        try (Tier0 tier0 = connect()) {
            tier0.send(message(1_000L, 500, now));
            // 재시작하면 순번은 0 부터 다시 시작한다(계약)
            tier0.send(withHelmet(message(2_000L, 0, now + 1_000), "NOT_WORN"));
        }
        awaitCount(() -> (int) (counter("stored") - stored), 2);
        assertThat(current(worn)).containsEntry("helmet_state", "NOT_WORN").containsEntry("stream_epoch", 2_000L)
                .containsEntry("state_sequence", 0L);
    }

    @Test
    void 판정할_사람이_없는_메시지는_받고_계약_위반은_버린다() throws Exception {
        ObjectNode empty = message(1_000L, 30, now);
        ((ObjectNode) empty.get("payload")).set("people", MAPPER.createArrayNode());
        ObjectNode twice = message(1_000L, 31, now);
        people(twice).forEach(p -> ((ObjectNode) p).put("world_id", worn));
        ObjectNode expiresBeforeEvidence = message(1_000L, 32, now);
        ((ObjectNode) people(expiresBeforeEvidence).get(0).get("helmet")).put("expires_at_ms", now - 1);
        ObjectNode evidenceAfterCapture = message(1_000L, 33, now);
        ((ObjectNode) people(evidenceAfterCapture).get(0).get("helmet").get("evidence").get(0)).put("capture_time_ms", now + 1);
        // 스키마 위반: WORN 인데 WORN 근거가 없다(검출 없음은 착용·미착용 근거가 아님)
        ObjectNode wornWithoutEvidence = message(1_000L, 34, now);
        ((ObjectNode) people(wornWithoutEvidence).get(0).get("helmet")).set("evidence", MAPPER.createArrayNode());

        double stored = counter("stored");
        double invalid = counter("invalid");
        try (Tier0 tier0 = connect()) {
            tier0.send(empty);
            tier0.send(twice);
            tier0.send(expiresBeforeEvidence);
            tier0.send(evidenceAfterCapture);
            tier0.send(wornWithoutEvidence);
        }
        awaitCount(() -> (int) (counter("invalid") - invalid), 4);
        awaitCount(() -> (int) (counter("stored") - stored), 1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM ppe_state WHERE source_id = ?", Integer.class, sourceId)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM ppe_current_state WHERE source_id = ?", Integer.class, sourceId))
                .isZero();
    }

    /**
     * 계약 예시(contracts/examples/ppe_state.json)를 이 시험의 source_id·world_id·시각으로 옮긴다.
     * 첫 사람(WORN·NOT_WORN 근거 있음)은 capture 를, 근거 시각은 capture 기준 같은 간격을 유지한다.
     */
    private ObjectNode message(long streamEpoch, long sequence, long capture) throws Exception {
        ObjectNode message = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("ppe_state.json")));
        message.put("msg_id", UUID.randomUUID().toString());
        message.put("source_id", sourceId);
        message.put("sent_at", capture + 60);
        ObjectNode payload = (ObjectNode) message.get("payload");
        payload.put("stream_epoch", streamEpoch);
        payload.put("state_sequence", sequence);
        ArrayNode people = (ArrayNode) payload.get("people");
        long shift = capture - people.get(0).get("capture_time_ms").asLong();
        String[] ids = {worn, unknown};
        for (int i = 0; i < people.size(); i++) {
            ObjectNode person = (ObjectNode) people.get(i);
            person.put("world_id", ids[i]);
            person.put("capture_time_ms", person.get("capture_time_ms").asLong() + shift);
            for (String item : new String[] {"helmet", "vest"}) {
                ObjectNode state = (ObjectNode) person.get(item);
                for (String field : new String[] {"last_evidence_at_ms", "expires_at_ms"}) {
                    if (!state.get(field).isNull()) {
                        state.put(field, state.get(field).asLong() + shift);
                    }
                }
                for (JsonNode evidence : state.get("evidence")) {
                    ((ObjectNode) evidence).put("capture_time_ms", evidence.get("capture_time_ms").asLong() + shift);
                }
            }
        }
        return message;
    }

    /** 첫 사람 안전모를 다른 상태로 바꾼다(근거 후보 상태도 같이 바꿔 계약에 맞게 둔다) */
    private static ObjectNode withHelmet(ObjectNode message, String state) {
        ObjectNode helmet = (ObjectNode) people(message).get(0).get("helmet");
        helmet.put("state", state);
        helmet.put("reason", "WINDOW_" + state);
        helmet.get("evidence").forEach(e -> ((ObjectNode) e).put("candidate_state", state));
        return message;
    }

    private static ArrayNode people(ObjectNode message) {
        return (ArrayNode) message.get("payload").get("people");
    }

    private Map<String, Object> current(String worldId) {
        return jdbc.queryForMap("SELECT helmet_state, stream_epoch, state_sequence FROM ppe_current_state"
                + " WHERE source_id = ? AND world_id = ?", sourceId, worldId);
    }

    private double counter(String result) {
        Counter counter = meters.find(METRIC).tags("msg_type", "ppe_state", "result", result).counter();
        return counter == null ? 0 : counter.count();
    }

    private static void awaitCount(IntSupplier count, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (count.getAsInt() < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(count.getAsInt()).isEqualTo(expected);
    }

    private Tier0 connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TIER0_TOKEN);
        return new Tier0(new StandardWebSocketClient().execute(new AbstractWebSocketHandler() { }, headers,
                URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS));
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
}
