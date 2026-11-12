package com.ssafy.simai.domain.ppe;

import static org.assertj.core.api.Assertions.assertThat;

import com.ssafy.simai.domain.realtime.RealtimeTopics;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.lang.reflect.Type;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
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
import org.springframework.web.socket.handler.AbstractWebSocketHandler;
import org.springframework.web.socket.messaging.WebSocketStompClient;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * PPE 화면 전달(S15P31A101-531, 명세 12.4·13.3). 실제 I1 로 ppe_state 를 보내 /topic/ppe 로 계약(ppe_view)에 맞는 스냅샷이 오는지,
 * GET /api/ppe/current 가 같은 모양을 주는지, 만료·오래된 상태를 어떻게 싣는지 본다. 시험마다 다른 source_id·world_id 를 쓴다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class PpeViewIntegrationTest {

    /** application-test.yaml 의 simai.tier0.token-sha256 원문 */
    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ContractSchemas schemas;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String sourceId = "tier0-ppev-" + tag;
    private final String worn = "w-ppev-a-" + tag;
    private final String unknown = "w-ppev-b-" + tag;
    private final List<AutoCloseable> closeables = new ArrayList<>();
    private long now;

    @BeforeEach
    void clock() {
        now = System.currentTimeMillis();
    }

    @AfterEach
    void cleanUp() {
        for (AutoCloseable c : closeables) {
            try {
                c.close();
            } catch (Exception e) {
                // 이미 닫힘
            }
        }
        jdbc.update("DELETE FROM ppe_state WHERE source_id = ?", sourceId);
        jdbc.update("DELETE FROM ppe_current_state WHERE source_id = ?", sourceId);
    }

    @Test
    void ppe_state_를_받으면_그_Tier0_스냅샷을_topic_ppe_로_보낸다() throws Exception {
        BlockingQueue<JsonNode> screen = subscribe(RealtimeTopics.PPE);
        try (Tier0 tier0 = connect()) {
            tier0.send(message(1_000L, 1, now));
            JsonNode view = nextFor(screen, sourceId);
            assertThat(view).as("10초 안에 이 Tier 0 의 ppe_view 가 와야 한다").isNotNull();
            assertThat(schemas.violations("messages/ppe_view", MAPPER.writeValueAsString(view))).isEmpty();
            assertThat(view.get("msg_type").asString()).isEqualTo("ppe_view");
            assertThat(view.get("source_id").asString()).isEqualTo("ec2");
            JsonNode a = person(view.get("payload"), worn);
            assertThat(a.get("identity_revision").asInt()).isEqualTo(2);
            assertThat(a.get("capture_time_ms").asLong()).isEqualTo(now);
            assertThat(a.get("helmet").get("state").asString()).isEqualTo("WORN");
            assertThat(a.get("helmet").get("reported_state").asString()).isEqualTo("WORN");
            assertThat(a.get("vest").get("state").asString()).isEqualTo("NOT_WORN");
            assertThat(a.get("helmet").has("evidence")).as("근거 내부 값은 싣지 않는다").isFalse();
            assertThat(person(view.get("payload"), unknown).get("helmet").get("state").asString()).isEqualTo("UNKNOWN");
        }
    }

    @Test
    void 처음_상태_REST_는_같은_모양이고_만료를_반영하며_60초가_지난_상태는_싣지_않는다() throws Exception {
        // 지금 촬영이지만 근거가 이미 만료된 착용 상태, 70초 전 촬영 상태(범위 밖)
        insertCurrent(worn, now, "WORN", now - 12_000, now - 2_000);
        insertCurrent(unknown, now - 70_000, "NOT_WORN", now - 70_000, now - 60_000);

        HttpResponse<String> response = get(jwt("VIEWER"));
        assertThat(response.statusCode()).isEqualTo(200);
        JsonNode mine = null;
        for (JsonNode payload : MAPPER.readTree(response.body())) {
            if (sourceId.equals(payload.get("tier0_source_id").asString())) {
                mine = payload;
            }
        }
        assertThat(mine).isNotNull();
        ObjectNode envelope = MAPPER.createObjectNode().put("schema_version", "1.0.0").put("msg_type", "ppe_view")
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", now).put("source_id", "ec2");
        envelope.set("payload", mine);
        assertThat(schemas.violations("messages/ppe_view", MAPPER.writeValueAsString(envelope))).isEmpty();

        ArrayNode people = (ArrayNode) mine.get("people");
        assertThat(people).hasSize(1);
        JsonNode helmet = people.get(0).get("helmet");
        assertThat(helmet.get("state").asString()).isEqualTo("UNKNOWN");
        assertThat(helmet.get("reported_state").asString()).isEqualTo("WORN");
        assertThat(helmet.get("reason").asString()).isEqualTo(PpeViewRepository.EXPIRED_REASON);
        assertThat(helmet.get("last_evidence_at_ms").asLong()).isEqualTo(now - 12_000);
    }

    @Test
    void 사람이_없는_ppe_state_도_마지막_수신_시각으로_처음_상태_REST_에_실린다() throws Exception {
        // -634: PPE 가 살아 있는지 화면이 알 근거. 사람 0명 메시지는 DB 행을 남기지 않지만 수신 시각은 남는다
        ObjectNode empty = message(2_000L, 1, now);
        ((ObjectNode) empty.get("payload")).putArray("people");
        long before = Instant.now().toEpochMilli();
        try (Tier0 tier0 = connect()) {
            tier0.send(empty);
            JsonNode mine = null;
            long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
            while (mine == null && System.nanoTime() < deadline) {
                for (JsonNode payload : MAPPER.readTree(get(jwt("VIEWER")).body())) {
                    if (sourceId.equals(payload.get("tier0_source_id").asString())) {
                        mine = payload;
                    }
                }
                if (mine == null) {
                    Thread.sleep(100);
                }
            }
            assertThat(mine).as("사람이 없어도 받은 적이 있는 Tier 0 는 스냅샷을 준다").isNotNull();
            assertThat(mine.get("people")).isEmpty();
            assertThat(mine.get("last_received_at_ms").asLong()).isBetween(before, mine.get("snapshot_at_ms").asLong());
            ObjectNode envelope = MAPPER.createObjectNode().put("schema_version", "1.1.0").put("msg_type", "ppe_view")
                    .put("msg_id", UUID.randomUUID().toString()).put("sent_at", now).put("source_id", "ec2");
            envelope.set("payload", mine);
            assertThat(schemas.violations("messages/ppe_view", MAPPER.writeValueAsString(envelope))).isEmpty();
        }
    }

    @Test
    void 처음_상태_REST_는_로그인이_필요하다() throws Exception {
        assertThat(get(null).statusCode()).isEqualTo(401);
    }

    /** 계약 예시(ppe_state.json)를 이 시험의 source_id·world_id·시각으로 옮긴다 */
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

    private void insertCurrent(String worldId, long capture, String helmet, long lastEvidence, long expires) {
        jdbc.update("""
                INSERT INTO ppe_current_state (source_id, world_id, identity_revision, observation_at, stream_epoch, state_sequence,
                    helmet_state, helmet_reason, helmet_last_evidence_at, helmet_expires_at, vest_state, vest_reason,
                    vest_last_evidence_at, vest_expires_at, model_version, policy_version, updated_at)
                VALUES (?, ?, 0, ?, 1, 1, ?, 'WINDOW_' || ?, ?, ?, 'UNKNOWN', 'NO_OBSERVATION', NULL, NULL, 'm', 'p', ?)
                """, sourceId, worldId, ts(capture), helmet, helmet, ts(lastEvidence), ts(expires), ts(capture + 50));
    }

    private static Timestamp ts(long millis) {
        return Timestamp.from(Instant.ofEpochMilli(millis));
    }

    private static JsonNode person(JsonNode payload, String worldId) {
        for (JsonNode p : payload.get("people")) {
            if (worldId.equals(p.get("world_id").asString())) {
                return p;
            }
        }
        throw new AssertionError("스냅샷에 " + worldId + " 가 없다");
    }

    /** 다른 시험의 스냅샷은 건너뛰고 이 Tier 0 의 것을 기다린다 */
    private static JsonNode nextFor(BlockingQueue<JsonNode> queue, String source) throws InterruptedException {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(10);
        while (System.nanoTime() < deadline) {
            JsonNode m = queue.poll(200, TimeUnit.MILLISECONDS);
            if (m != null && source.equals(m.get("payload").get("tier0_source_id").asString())) {
                return m;
            }
        }
        return null;
    }

    private HttpResponse<String> get(String token) throws Exception {
        HttpRequest.Builder b = HttpRequest.newBuilder(URI.create("http://localhost:" + port + PpeViewController.API)).GET();
        if (token != null) {
            b.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        try (HttpClient client = HttpClient.newHttpClient()) {
            return client.send(b.build(), HttpResponse.BodyHandlers.ofString());
        }
    }

    private String jwt(String role) {
        Instant at = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(at)
                .expiresAt(at.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private BlockingQueue<JsonNode> subscribe(String topic) throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        StompHeaders connect = new StompHeaders();
        connect.add(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER"));
        StompSession session = client.connectAsync(URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                new WebSocketHttpHeaders(), connect, new StompSessionHandlerAdapter() { }).get(5, TimeUnit.SECONDS);
        BlockingQueue<JsonNode> queue = new LinkedBlockingQueue<>();
        session.subscribe(topic, new StompFrameHandler() {
            @Override
            public Type getPayloadType(StompHeaders headers) {
                return byte[].class;
            }

            @Override
            public void handleFrame(StompHeaders headers, Object payload) {
                queue.add(MAPPER.readTree(new String((byte[]) payload, StandardCharsets.UTF_8)));
            }
        });
        closeables.add(session::disconnect);
        Thread.sleep(300); // 구독이 브로커에 닿을 때까지
        return queue;
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
