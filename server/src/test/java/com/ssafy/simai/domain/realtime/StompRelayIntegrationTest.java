package com.ssafy.simai.domain.realtime;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

import com.networknt.schema.InputFormat;
import com.networknt.schema.JsonSchema;
import com.networknt.schema.JsonSchemaFactory;
import com.networknt.schema.SchemaLocation;
import com.networknt.schema.SpecVersion;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
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
import java.util.List;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.ThreadLocalRandom;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.simp.stomp.StompFrameHandler;
import org.springframework.scheduling.TaskScheduler;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;
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
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관제 앱 STOMP(S15P31A101-119, 명세 12.4·14장). 실제 포트로 Tier 0(I1)처럼 보내고 관제 앱처럼 /ws 를 구독한다.
 * 받은 메시지는 contracts/ JSON Schema 로 검사한다(관제 앱은 계약에 맞지 않는 메시지를 버린다).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@ActiveProfiles("test")
class StompRelayIntegrationTest {

    private static final String TIER0_TOKEN = "test-only-tier0-token";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final JsonSchemaFactory SCHEMAS = JsonSchemaFactory.getInstance(SpecVersion.VersionFlag.V202012,
            builder -> builder.schemaMappers(m -> m.mapPrefix("https://simai.dev/contracts/",
                    CONTRACTS.toAbsolutePath().toUri().toString())));

    @LocalServerPort
    int port;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    @Qualifier("taskScheduler")
    ThreadPoolTaskScheduler taskScheduler;

    @Autowired
    @Qualifier("messageBrokerTaskScheduler")
    TaskScheduler messageBrokerTaskScheduler;

    private final String gatewayId = "tier0-stomp-" + UUID.randomUUID();
    // 다른 시험·이전 실행과 같은 관측 시각을 쓰지 않는다
    private final long baseMs = Instant.now().toEpochMilli() + ThreadLocalRandom.current().nextLong(1_000_000_000L);

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM skeleton_3d s USING world_track_state w"
                + " WHERE s.observation_at = w.observation_at AND s.world_id = w.world_id AND w.gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM world_track_state WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM event_ack a USING risk_event e WHERE a.event_id = e.event_id AND e.gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM risk_event_update WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM journal_cursor WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", gatewayId);
    }

    @Test
    void 토큰이_없거나_틀리면_CONNECT_를_거부한다() {
        assertThatThrownBy(() -> stomp(null)).isInstanceOf(ExecutionException.class);
        assertThatThrownBy(() -> stomp("not-a-jwt")).isInstanceOf(ExecutionException.class);
    }

    @Test
    void world_state_는_payload_를_바꾸지_않고_봉투만_새로_써서_보낸다() throws Exception {
        try (Viewer viewer = stomp(token("VIEWER")); Tier0 tier0 = tier0()) {
            viewer.subscribe(RealtimeTopics.WORLD);
            ObjectNode sent = worldState(baseMs);
            JsonNode received = viewer.awaitWorld(tier0, sent);

            assertValid("world_state", received);
            assertThat(received.get("source_id").asString()).isEqualTo("ec2");
            assertThat(received.get("msg_id").asString()).isNotEqualTo(sent.get("msg_id").asString());
            assertThat(received.get("schema_version").asString()).isEqualTo(sent.get("schema_version").asString());
            assertThat(received.get("payload")).isEqualTo(sent.get("payload"));
        }
    }

    @Test
    void 동기화_중_해소된_이벤트는_보내지_않고_DELAYED_ACTIVE_와_그_뒤_실시간_전이를_보낸다() throws Exception {
        UUID resolvedDuringSync = uuidV7();
        UUID stillActive = uuidV7();

        try (Viewer viewer = stomp(token("VIEWER")); Tier0 tier0 = tier0()) {
            viewer.subscribe(RealtimeTopics.EVENTS);
            viewer.subscribe(RealtimeTopics.WORLD);
            // 구독이 브로커에 걸렸는지는 world 가 오는 것으로 확인한다
            viewer.awaitWorld(tier0, worldState(baseMs));

            tier0.send(transition(resolvedDuringSync, 1, 0, "RAISED", true));
            tier0.send(transition(resolvedDuringSync, 3, 1, "RESOLVED", true));
            tier0.send(transition(stillActive, 1, 2, "RAISED", true));
            tier0.send(snapshot(2L, active(stillActive)));

            JsonNode delayed = viewer.await(RealtimeTopics.EVENTS);
            assertValid("risk_event", delayed);
            assertThat(delayed.get("source_id").asString()).isEqualTo("ec2");
            assertThat(delayed.at("/payload/event_id").asString()).isEqualTo(stillActive.toString());
            assertThat(delayed.at("/payload/delivery").asString()).isEqualTo("DELAYED_ACTIVE");
            assertThat(delayed.at("/payload/lifecycle").asString()).isEqualTo("RAISED");
            // 스냅샷에 있어 지금 Tier 0 가 들고 있다(S15P31A101-595)
            assertThat(delayed.at("/payload/held_by_tier0").booleanValue()).isTrue();
            // 선택 칸은 값이 없으면 싣지 않는다(REST 목록과 같은 투영)
            assertThat(delayed.at("/payload/resolved_at").isMissingNode()).isTrue();

            // 계약에 맞는 확인 하나와 현장 버튼 ↔ SITE 짝이 틀린 확인 하나(DB CHECK 는 통과한다)
            UUID goodAck = UUID.randomUUID();
            ack(goodAck, stillActive, "CONTROL_ROOM", "ADMIN");
            ack(UUID.randomUUID(), stillActive, "SITE_BUTTON", "ADMIN");

            tier0.send(transition(stillActive, 2, 3, "UPDATED", false));
            JsonNode realtime = viewer.await(RealtimeTopics.EVENTS);
            assertValid("risk_event", realtime);
            assertThat(realtime.at("/payload/event_id").asString()).isEqualTo(stillActive.toString());
            assertThat(realtime.at("/payload/event_sequence").intValue()).isEqualTo(2);
            assertThat(realtime.at("/payload/lifecycle").asString()).isEqualTo("UPDATED");
            assertThat(realtime.at("/payload/delivery").asString()).isEqualTo("REALTIME");
            assertThat(realtime.at("/payload/held_by_tier0").booleanValue()).isTrue();
            assertThat(realtime.at("/payload/acks")).hasSize(1);
            assertThat(realtime.at("/payload/acks/0/ack_id").asString()).isEqualTo(goodAck.toString());

            // 동기화 뒤에 온 낮은 sequence(빈 구간 재전송 등)는 기록만 하고 현재 상태가 그대로라 알리지 않는다
            tier0.send(transition(resolvedDuringSync, 2, 4, "UPDATED", true));
            awaitRows(resolvedDuringSync, 3);

            // 동기화 중 해소된 이벤트는 한 번도 오지 않았다(실시간 재알림 0, G3 D-G3-05)
            assertThat(viewer.remaining(RealtimeTopics.EVENTS)).isEmpty();
        }
    }

    @Test
    void 저널이_빈_새_Tier0_가_붙으면_이전_이벤트는_해소하지_않고_들고_있지_않음으로_내리고_판을_바꾼다() throws Exception {
        UUID eventId = uuidV7();

        try (Viewer viewer = stomp(token("VIEWER"))) {
            viewer.subscribe(RealtimeTopics.EVENTS);
            viewer.subscribe(RealtimeTopics.WORLD);
            try (Tier0 tier0 = tier0()) {
                viewer.awaitWorld(tier0, worldState(baseMs));
                tier0.send(snapshot(null));
                tier0.send(transition(eventId, 1, 0, "RAISED", false));
                JsonNode raised = viewer.await(RealtimeTopics.EVENTS);
                assertValid("risk_event", raised);
                assertThat(raised.at("/payload/held_by_tier0").booleanValue()).isTrue();
                assertThat(eventDetail(eventId).get("held_by_tier0").booleanValue()).isTrue();
            }
            String before = healthRevision();

            // GPU Pod 를 바꿔 저널이 빈 Tier 0 가 같은 gateway_id 로 붙었다(스냅샷 journal_offset null, 활성 없음)
            try (Tier0 fresh = tier0()) {
                fresh.send(snapshot(null));
                awaitHeld(eventId, false);
            }
            JsonNode detail = eventDetail(eventId);
            // 이벤트는 그대로다(EC2 는 전이를 만들지 않는다, 명세 3.2 자동 해소 금지)
            assertThat(detail.get("lifecycle").asString()).isEqualTo("RAISED");
            assertThat(detail.get("held_by_tier0").booleanValue()).isFalse();
            assertThat(healthRevision()).isNotEqualTo(before);
            // 한꺼번에 바뀐 것은 이벤트마다 보내지 않는다(관제 앱이 판을 보고 목록을 다시 받는다)
            assertThat(viewer.remaining(RealtimeTopics.EVENTS)).isEmpty();
        }
    }

    @Test
    void 지운_관리자_토픽은_관리자도_구독하지_못한다() throws Exception {
        // 조치 불일치 /topic/admin/events 는 S15P31A101-411 로 지웠다. 관리자 권한도 정해진 토픽 밖과 같게 끊는다
        try (Viewer admin = stomp(token("ADMIN"))) {
            admin.subscribe("/topic/admin/events");
            admin.awaitDisconnected();
        }
    }

    @Test
    void 정해진_토픽_밖은_구독하지_못한다() throws Exception {
        try (Viewer viewer = stomp(token("VIEWER"))) {
            viewer.subscribe("/topic/anything");
            viewer.awaitDisconnected();
        }
    }

    @Test
    void 관제_화면에서_확인하면_확인을_붙인_이벤트를_다시_보낸다() throws Exception {
        UUID eventId = uuidV7();
        long userId = user("ADMIN");

        try (Viewer viewer = stomp(token("VIEWER")); Tier0 tier0 = tier0()) {
            viewer.subscribe(RealtimeTopics.EVENTS);
            viewer.subscribe(RealtimeTopics.WORLD);
            viewer.awaitWorld(tier0, worldState(baseMs));
            tier0.send(snapshot(null));
            tier0.send(transition(eventId, 1, 0, "RAISED", false));
            assertThat(viewer.await(RealtimeTopics.EVENTS).at("/payload/acks")).isEmpty();

            HttpResponse<String> response = HttpClient.newHttpClient().send(HttpRequest.newBuilder(
                            URI.create("http://localhost:" + port + "/api/events/" + eventId + "/ack"))
                    .header(HttpHeaders.AUTHORIZATION, "Bearer " + token("ADMIN", Duration.ofMinutes(5), String.valueOf(userId)))
                    .header(HttpHeaders.CONTENT_TYPE, "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString("{\"event_sequence\":1}"))
                    .build(), HttpResponse.BodyHandlers.ofString());
            assertThat(response.statusCode()).isEqualTo(200);

            JsonNode acked = viewer.await(RealtimeTopics.EVENTS);
            assertValid("risk_event", acked);
            assertThat(acked.at("/payload/event_id").asString()).isEqualTo(eventId.toString());
            assertThat(acked.at("/payload/acks")).hasSize(1);
            assertThat(acked.at("/payload/acks/0/actor_id").asString()).isEqualTo(String.valueOf(userId));
        }
    }

    @Test
    void 토큰이_만료된_연결은_새_구독을_하지_못한다() throws Exception {
        try (Viewer viewer = stomp(token("VIEWER", Duration.ofSeconds(2)))) {
            Thread.sleep(2_500);
            viewer.subscribe(RealtimeTopics.WORLD);
            viewer.awaitDisconnected();
        }
    }

    @Test
    void 주기_작업은_STOMP_하트비트_스케줄러와_다른_스케줄러에서_돈다() {
        assertThat(taskScheduler.getThreadNamePrefix()).isEqualTo("scheduling-");
        assertThat(taskScheduler).isNotSameAs(messageBrokerTaskScheduler);
    }

    @Test
    void 계약_검사기는_틀린_메시지를_잡는다() throws Exception {
        // 공통 정의($ref)를 못 풀어 무엇이든 통과시키는 검사기가 아닌지 본다
        ObjectNode broken = example("risk_event.json");
        ((ObjectNode) broken.get("payload")).put("event_id", UUID.randomUUID().toString());
        assertThat(schema("risk_event").validate(MAPPER.writeValueAsString(broken), InputFormat.JSON)).isNotEmpty();
    }

    // ---- 도우미

    private static JsonSchema schema(String msgType) {
        return SCHEMAS.getSchema(SchemaLocation.of("https://simai.dev/contracts/messages/" + msgType + ".schema.json"));
    }

    private String token(String role) {
        return token(role, Duration.ofMinutes(5));
    }

    private String token(String role, Duration ttl) {
        return token(role, ttl, "1");
    }

    private String token(String role, Duration ttl, String subject) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject(subject)
                .issuedAt(now)
                .expiresAt(now.plus(ttl))
                .claim(AccessTokenIssuer.ROLE_CLAIM, role)
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private static void assertValid(String msgType, JsonNode message) {
        assertThat(schema(msgType).validate(MAPPER.writeValueAsString(message), InputFormat.JSON)).isEmpty();
    }

    /** 계약의 event_id 는 UUIDv7 이다 */
    private static UUID uuidV7() {
        UUID random = UUID.randomUUID();
        long msb = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long lsb = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(msb, lsb);
    }

    private void ack(UUID ackId, UUID eventId, String channel, String actorRole) {
        long userId = user(actorRole);
        jdbc.update("INSERT INTO event_ack (ack_id, event_id, channel, actor_role, actor_user_id, acked_at)"
                + " VALUES (?, ?, ?, ?, ?, now())", ackId, eventId, channel, actorRole, userId);
    }

    /** actor_user_id 는 app_user 를 가리킨다. 이 시험의 계정을 하나 만든다(login_id 는 시험마다 다르다) */
    private long user(String role) {
        Long userId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at)"
                + " VALUES (?, 'x', ?, now()) ON CONFLICT (login_id) DO UPDATE SET role = EXCLUDED.role RETURNING user_id",
                Long.class, gatewayId, role);
        return userId;
    }

    private void awaitRows(UUID eventId, int expected) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (rows(eventId) < expected && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(rows(eventId)).isEqualTo(expected);
    }

    private int rows(UUID eventId) {
        return jdbc.queryForObject("SELECT count(*) FROM risk_event_update WHERE event_id = ?", Integer.class, eventId);
    }

    private ObjectNode example(String name) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        return root;
    }

    private ObjectNode worldState(long tMs) throws Exception {
        ObjectNode root = example("world_state.json");
        root.put("sent_at", tMs);
        ((ObjectNode) root.get("payload")).put("t_latest_global", tMs);
        return root;
    }

    private JsonNode eventDetail(UUID eventId) throws Exception {
        return getJson("/api/events/" + eventId);
    }

    private String healthRevision() throws Exception {
        return getJson("/api/health").at("/payload/held_events_revision").asString();
    }

    private JsonNode getJson(String path) throws Exception {
        HttpResponse<String> response = HttpClient.newHttpClient().send(HttpRequest.newBuilder(
                        URI.create("http://localhost:" + port + path))
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token("ADMIN"))
                .GET()
                .build(), HttpResponse.BodyHandlers.ofString());
        assertThat(response.statusCode()).as(path).isEqualTo(200);
        return MAPPER.readTree(response.body());
    }

    /** 스냅샷 처리는 I1 수신 스레드에서 끝난다. 상세가 기대한 값을 줄 때까지 기다린다 */
    private void awaitHeld(UUID eventId, boolean expected) throws Exception {
        long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
        while (System.nanoTime() < deadline) {
            if (eventDetail(eventId).get("held_by_tier0").booleanValue() == expected) {
                return;
            }
            Thread.sleep(50);
        }
        throw new AssertionError("held_by_tier0 가 " + expected + " 가 되지 않았다");
    }

    private ObjectNode transition(UUID eventId, int sequence, long offset, String kind, boolean resent) throws Exception {
        ObjectNode root = example("risk_event_transition.json");
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("event_id", eventId.toString());
        p.put("event_sequence", sequence);
        p.put("journal_offset", offset);
        p.put("transition", kind);
        p.put("resent", resent);
        return root;
    }

    private ObjectNode snapshot(Long offset, ObjectNode... activeEvents) throws Exception {
        ObjectNode root = example("event_state_snapshot.json");
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("journal_offset", offset);
        ArrayNode active = p.putArray("active_events");
        for (ObjectNode event : activeEvents) {
            active.add(event);
        }
        return root;
    }

    private ObjectNode active(UUID eventId) throws Exception {
        ObjectNode event = (ObjectNode) example("event_state_snapshot.json").path("payload").path("active_events").get(0);
        event.put("event_id", eventId.toString());
        event.put("event_sequence", 1);
        event.put("lifecycle", "RAISED");
        event.put("last_valid_observation_at", baseMs);
        return event;
    }

    private Viewer stomp(String token) throws Exception {
        WebSocketStompClient client = new WebSocketStompClient(new StandardWebSocketClient());
        StompHeaders connect = new StompHeaders();
        if (token != null) {
            connect.add(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        StompSession session = client.connectAsync(URI.create("ws://localhost:" + port + RealtimeTopics.ENDPOINT),
                new WebSocketHttpHeaders(), connect, new StompSessionHandlerAdapter() { }).get(5, TimeUnit.SECONDS);
        return new Viewer(client, session, new LinkedBlockingQueue<>());
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

    private record Received(String topic, JsonNode message) {
    }

    /** 관제 앱 흉내. 받은 메시지를 토픽과 함께 모은다 */
    private record Viewer(WebSocketStompClient client, StompSession session, BlockingQueue<Received> queue)
            implements AutoCloseable {

        void subscribe(String topic) {
            session.subscribe(topic, new StompFrameHandler() {
                @Override
                public Type getPayloadType(StompHeaders headers) {
                    return byte[].class;
                }

                @Override
                public void handleFrame(StompHeaders headers, Object payload) {
                    queue.add(new Received(topic, MAPPER.readTree(new String((byte[]) payload, StandardCharsets.UTF_8))));
                }
            });
        }

        /** 그 토픽의 다음 메시지. 다른 토픽 메시지는 건너뛴다 */
        JsonNode await(String topic) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                Received r = queue.poll(200, TimeUnit.MILLISECONDS);
                if (r != null && r.topic().equals(topic)) {
                    return r.message();
                }
            }
            throw new AssertionError(topic + " 메시지가 오지 않았다");
        }

        /**
         * 구독이 브로커에 걸리기 전에 보낸 것은 받지 못한다. 받을 때까지 같은 관측 시각의 world_state 를 다시 보낸다(저장은 덮어쓴다).
         * 기다리는 동안 받은 다른 토픽 메시지는 남겨 둔다.
         */
        JsonNode awaitWorld(Tier0 tier0, ObjectNode worldState) throws Exception {
            long tMs = worldState.at("/payload/t_latest_global").longValue();
            for (int attempt = 0; attempt < 20; attempt++) {
                worldState.put("msg_id", UUID.randomUUID().toString());
                tier0.send(worldState);
                long deadline = System.nanoTime() + Duration.ofMillis(500).toNanos();
                while (System.nanoTime() < deadline) {
                    Received r = queue.poll(50, TimeUnit.MILLISECONDS);
                    if (r != null && r.topic().equals(RealtimeTopics.WORLD) && r.message().at("/payload/t_latest_global").longValue() == tMs) {
                        queue.removeIf(x -> x.topic().equals(RealtimeTopics.WORLD));
                        return r.message();
                    }
                    if (r != null && !r.topic().equals(RealtimeTopics.WORLD)) {
                        queue.add(r);
                    }
                }
            }
            throw new AssertionError("/topic/world 메시지가 오지 않았다");
        }

        /** 잠시 기다린 뒤 아직 꺼내지 않은 그 토픽 메시지 */
        List<JsonNode> remaining(String topic) throws InterruptedException {
            Thread.sleep(300);
            return queue.stream().filter(r -> r.topic().equals(topic)).map(Received::message).toList();
        }

        void awaitDisconnected() throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
            while (session.isConnected() && System.nanoTime() < deadline) {
                Thread.sleep(50);
            }
            assertThat(session.isConnected()).as("거부한 구독 뒤 연결을 닫는다").isFalse();
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
