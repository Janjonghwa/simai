package com.ssafy.simai.domain.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.ssafy.simai.domain.tier0.Tier0Paths;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.web.server.LocalServerPort;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.mock.web.MockHttpServletResponse;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.TestPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.RequestBuilder;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 운영자 종료 API(POST /api/events/{id}/close)와 I1 event_close 송신·event_close_result 수신·종료 전이 연결 통합 시험(S15P31A101-221).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureMockMvc
@ActiveProfiles("test")
@TestPropertySource(properties = {
        "spring.flyway.ignore-migration-patterns=*:missing,*:ignored,*:future",
        "simai.event-close.result-wait-ms=1500"
})
class EventCloseIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final String TOKEN = "test-only-tier0-token";
    private static final String REASON = "3번 카메라 철거로 z-04 구역 감시 근거가 오지 않음. 현장 확인 후 종료";
    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @LocalServerPort
    int port;

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    private final String gatewayId = "tier0-close-" + UUID.randomUUID();
    private final String suffix = UUID.randomUUID().toString().substring(0, 8);

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event_update WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM event_close WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM event_ack WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM journal_cursor WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 종료하면_I1_event_close_를_보내고_ACCEPTED_결과를_돌려주며_같은_멱등_키는_행_하나에_같은_결과다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        String key = "close-" + UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.send(snapshot(0L));
            tier0.await("journal_ack", p -> !p.get("sync_mode").booleanValue());

            CompletableFuture<MockHttpServletResponse> pending = closeAsync(event, admin, REASON, key);
            JsonNode sentEnvelope = tier0.awaitEnvelope("event_close", p -> event.toString().equals(p.path("event_id").asString()));
            assertThat(sentEnvelope.get("schema_version").asString()).isEqualTo("1.0.0");
            JsonNode close = sentEnvelope.get("payload").get("close");
            String closeId = close.get("close_id").asString();
            assertThat(close.get("channel").asString()).isEqualTo("CONTROL_ROOM");
            assertThat(close.get("actor_role").asString()).isEqualTo("ADMIN");
            assertThat(close.get("reason").asString()).isEqualTo(REASON);
            // REST 멱등 키와 I1 close_id 는 따로다(16장)
            assertThat(closeId).isNotEqualTo(key);

            tier0.send(closeResult(closeId, event, "ACCEPTED", 2, null));
            MockHttpServletResponse response = pending.get(10, TimeUnit.SECONDS);
            assertThat(response.getStatus()).isEqualTo(200);
            String body = response.getContentAsString();
            assertThat((String) JsonPath.read(body, "$.close_id")).isEqualTo(closeId);
            assertThat((String) JsonPath.read(body, "$.result")).isEqualTo("ACCEPTED");
            assertThat((Integer) JsonPath.read(body, "$.event_sequence")).isEqualTo(2);
            assertThat((Object) JsonPath.read(body, "$.reject_reason")).isNull();

            // EC2 는 결과만으로 이벤트를 닫지 않는다. 저널 종료 전이가 와야 닫힌다
            assertThat(jdbc.queryForObject("SELECT lifecycle FROM risk_event WHERE event_id = ?", String.class, event))
                    .isNotEqualTo("RESOLVED");
            tier0.send(transition(event, 2, 1, "RESOLVED", closeId));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
            mvc.perform(get("/api/events/{id}", event).header(HttpHeaders.AUTHORIZATION, admin))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.lifecycle").value("RESOLVED"))
                    .andExpect(jsonPath("$.resolution.close_id").value(closeId))
                    .andExpect(jsonPath("$.transitions[1].close_id").value(closeId));

            // 같은 키로 다시 보내면 새 행·새 I1 없이 처음 결과를 돌려준다(이벤트가 이미 닫혔어도)
            mvc.perform(closeRequest(event, admin, "다른 사유", key))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.close_id").value(closeId))
                    .andExpect(jsonPath("$.result").value("ACCEPTED"))
                    .andExpect(jsonPath("$.reason").value(REASON));
            assertThat(tier0.poll("event_close", 300)).isNull();
            assertThat(jdbc.queryForObject("SELECT count(*) FROM event_close WHERE event_id = ?", Integer.class, event))
                    .isEqualTo(1);
        }
    }

    @Test
    void Tier0_거부는_사유를_그대로_돌려주고_일시_거부나_시간_안에_결과가_없으면_202_로_돌려준다() throws Exception {
        UUID personEvent = UUID.randomUUID();
        UUID silentEvent = UUID.randomUUID();
        UUID busyEvent = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(personEvent, 1, 0, "RAISED", null));
            tier0.send(transition(silentEvent, 1, 1, "RAISED", null));
            tier0.send(transition(busyEvent, 1, 2, "RAISED", null));
            tier0.send(snapshot(2L));
            tier0.await("journal_ack", p -> !p.get("sync_mode").booleanValue());

            CompletableFuture<MockHttpServletResponse> rejected = closeAsync(personEvent, admin, REASON, "close-" + UUID.randomUUID());
            String closeId = tier0.await("event_close", p -> personEvent.toString().equals(p.path("event_id").asString()))
                    .get("close").get("close_id").asString();
            tier0.send(closeResult(closeId, personEvent, "REJECTED", null, "TARGET_NOT_ZONE"));
            MockHttpServletResponse response = rejected.get(10, TimeUnit.SECONDS);
            String body = response.getContentAsString();
            assertThat(response.getStatus()).isEqualTo(200);
            assertThat((String) JsonPath.read(body, "$.result")).isEqualTo("REJECTED");
            assertThat((String) JsonPath.read(body, "$.reject_reason")).isEqualTo("TARGET_NOT_ZONE");
            assertThat((Object) JsonPath.read(body, "$.event_sequence")).isNull();

            // JOURNAL_UNAVAILABLE 은 일시 거부라 확정 거부가 아니다: 202 에 그대로 싣고 다시 보낸다
            CompletableFuture<MockHttpServletResponse> busy = closeAsync(busyEvent, admin, REASON, "close-" + UUID.randomUUID());
            String busyCloseId = tier0.await("event_close", p -> busyEvent.toString().equals(p.path("event_id").asString()))
                    .get("close").get("close_id").asString();
            tier0.send(closeResult(busyCloseId, busyEvent, "REJECTED", null, "JOURNAL_UNAVAILABLE"));
            MockHttpServletResponse busyResponse = busy.get(10, TimeUnit.SECONDS);
            assertThat(busyResponse.getStatus()).isEqualTo(202);
            assertThat((String) JsonPath.read(busyResponse.getContentAsString(), "$.reject_reason")).isEqualTo("JOURNAL_UNAVAILABLE");

            // 응답하지 않는 Tier 0: 기다림(1.5 s)이 지나면 202, result null
            mvc.perform(closeRequest(silentEvent, admin, REASON, "close-" + UUID.randomUUID()))
                    .andExpect(status().isAccepted())
                    .andExpect(jsonPath("$.result").value((Object) null))
                    .andExpect(jsonPath("$.event_id").value(silentEvent.toString()));
        }
    }

    @Test
    void Tier0_가_끊겨_있으면_202_로_저장하고_재연결_동기화_뒤_같은_close_id_로_보내며_JOURNAL_UNAVAILABLE_도_다시_보낸다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        String key = "close-" + UUID.randomUUID();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 0);
        }

        String body = mvc.perform(closeRequest(event, admin, REASON, key))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.result").value((Object) null))
                .andReturn().getResponse().getContentAsString();
        String closeId = JsonPath.read(body, "$.close_id");

        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(0L));
            tier0.await("event_close", p -> closeId.equals(p.path("close").path("close_id").asString()));
            tier0.send(closeResult(closeId, event, "REJECTED", null, "JOURNAL_UNAVAILABLE"));
            awaitTrue(() -> "JOURNAL_UNAVAILABLE".equals(jdbc.queryForObject(
                    "SELECT reject_reason FROM event_close WHERE close_id = ?::uuid", String.class, closeId)));
        }

        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(0L));
            tier0.await("event_close", p -> closeId.equals(p.path("close").path("close_id").asString()));
            tier0.send(closeResult(closeId, event, "ACCEPTED", 2, null));
            awaitTrue(() -> "ACCEPTED".equals(jdbc.queryForObject(
                    "SELECT result FROM event_close WHERE close_id = ?::uuid", String.class, closeId)));
            // 늦게 온 중복·거부가 수락을 덮지 않는다. 한 연결의 메시지는 차례로 처리되므로 뒤에 보낸 전이의 확인으로 처리 끝을 안다
            tier0.send(closeResult(closeId, event, "DUPLICATE", null, null));
            tier0.send(closeResult(closeId, event, "REJECTED", null, "NOT_ACTIVE"));
            tier0.send(transition(UUID.randomUUID(), 1, 1, "RAISED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
            assertThat(jdbc.queryForObject(
                    "SELECT result FROM event_close WHERE close_id = ?::uuid", String.class, closeId)).isEqualTo("ACCEPTED");
        }

        mvc.perform(closeRequest(event, admin, REASON, key))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.close_id").value(closeId))
                .andExpect(jsonPath("$.result").value("ACCEPTED"))
                .andExpect(jsonPath("$.event_sequence").value(2))
                .andExpect(jsonPath("$.reject_reason").value((Object) null));
    }

    @Test
    void 권한과_입력_검증_비관리자_없는_이벤트_해소된_이벤트_사유_멱등_키_반례를_거부한다() throws Exception {
        UUID activeEvent = UUID.randomUUID();
        UUID resolvedEvent = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        String otherAdmin = token("admin2", Role.ADMIN);
        String foreman = token("foreman", Role.FOREMAN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(activeEvent, 1, 0, "RAISED", null));
            tier0.send(transition(resolvedEvent, 1, 1, "RAISED", null));
            tier0.send(transition(resolvedEvent, 2, 2, "RESOLVED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 2);
        }
        String key = "close-" + UUID.randomUUID();

        mvc.perform(closeRequest(activeEvent, foreman, REASON, key)).andExpect(status().isForbidden());
        mvc.perform(post("/api/events/{id}/close", activeEvent).contentType(MediaType.APPLICATION_JSON)
                        .content("{\"reason\":\"x\",\"idempotency_key\":\"k\"}"))
                .andExpect(status().isUnauthorized());
        mvc.perform(closeRequest(UUID.randomUUID(), admin, REASON, key))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("EVENT_NOT_FOUND"));
        mvc.perform(closeRequest(resolvedEvent, admin, REASON, key))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("EVENT_RESOLVED"));
        mvc.perform(closeRequest(activeEvent, admin, "   ", key))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_CLOSE_REASON"));
        mvc.perform(closeRequest(activeEvent, admin, "가".repeat(501), key))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_CLOSE_REASON"));
        mvc.perform(post("/api/events/{id}/close", activeEvent).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"reason\":\"사유\"}"))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_IDEMPOTENCY_KEY"));
        mvc.perform(closeRequest(activeEvent, admin, REASON, "k".repeat(129)))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_IDEMPOTENCY_KEY"));
        // 위 반례들은 행을 남기지 않는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM event_close WHERE idempotency_key = ?", Integer.class, key))
                .isZero();

        // 500자 경계는 받는다(Tier 0 가 없어 202). 같은 키를 다른 관리자나 다른 이벤트에 쓰면 409
        mvc.perform(closeRequest(activeEvent, admin, "가".repeat(500), key)).andExpect(status().isAccepted());
        mvc.perform(closeRequest(activeEvent, otherAdmin, REASON, key))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("IDEMPOTENCY_KEY_REUSED"));
        mvc.perform(closeRequest(resolvedEvent, admin, REASON, key))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("IDEMPOTENCY_KEY_REUSED"));
        mvc.perform(closeRequest(activeEvent, admin, "널\u0000문자", "close-" + UUID.randomUUID()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_CLOSE_REASON"));
    }

    @Test
    void 같은_멱등_키_동시_요청은_행_하나와_같은_close_id_를_돌려준다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 0);
        }
        String key = "close-" + UUID.randomUUID();

        CompletableFuture<MockHttpServletResponse> first = closeAsync(event, admin, REASON, key);
        CompletableFuture<MockHttpServletResponse> second = closeAsync(event, admin, REASON, key);
        MockHttpServletResponse a = first.get(10, TimeUnit.SECONDS);
        MockHttpServletResponse b = second.get(10, TimeUnit.SECONDS);
        assertThat(a.getStatus()).isEqualTo(202);
        assertThat(b.getStatus()).isEqualTo(202);
        assertThat((String) JsonPath.read(a.getContentAsString(), "$.close_id"))
                .isEqualTo(JsonPath.read(b.getContentAsString(), "$.close_id"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM event_close WHERE idempotency_key = ?", Integer.class, key))
                .isEqualTo(1);
    }

    // ---- 도우미

    private CompletableFuture<MockHttpServletResponse> closeAsync(UUID eventId, String auth, String reason, String key) {
        return CompletableFuture.supplyAsync(() -> {
            try {
                return mvc.perform(closeRequest(eventId, auth, reason, key)).andReturn().getResponse();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    private static RequestBuilder closeRequest(UUID eventId, String auth, String reason, String key) {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("reason", reason);
        body.put("idempotency_key", key);
        return post("/api/events/{id}/close", eventId)
                .header(HttpHeaders.AUTHORIZATION, auth)
                .contentType(MediaType.APPLICATION_JSON)
                .content(MAPPER.writeValueAsString(body));
    }

    private String token(String name, Role role) throws Exception {
        String loginId = name + "-" + suffix;
        if (users.findByLoginId(loginId).isEmpty()) {
            users.saveAndFlush(new AppUser(loginId, passwordEncoder.encode(PASSWORD), role, null, Instant.now()));
        }
        String body = mvc.perform(post("/api/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + loginId + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        return "Bearer " + JsonPath.read(body, "$.accessToken");
    }

    /** 구역 대상 R7 형태(CAUTION) 이벤트 전이. closeId 가 있으면 운영자 종료 해소(resolution)를 싣는다 */
    private ObjectNode transition(UUID eventId, int sequence, long offset, String kind, String closeId) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("risk_event_transition.operator_closed.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("event_id", eventId.toString());
        p.put("event_sequence", sequence);
        p.put("journal_offset", offset);
        p.put("transition", kind);
        if (closeId == null) {
            p.remove("resolution");
        } else {
            ((ObjectNode) p.get("resolution")).put("close_id", closeId);
        }
        return root;
    }

    private ObjectNode closeResult(String closeId, UUID eventId, String result, Integer sequence, String rejectReason)
            throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_close_result.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("close_id", closeId);
        p.put("event_id", eventId.toString());
        p.put("result", result);
        if (sequence != null) {
            p.put("event_sequence", sequence);
        } else {
            p.remove("event_sequence");
        }
        if (rejectReason != null) {
            p.put("reject_reason", rejectReason);
        } else {
            p.remove("reject_reason");
        }
        p.put("decided_at", Instant.now().toEpochMilli());
        return root;
    }

    private ObjectNode snapshot(Long offset) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_state_snapshot.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("journal_offset", offset);
        p.putArray("active_events");
        return root;
    }

    private static void awaitTrue(java.util.function.Supplier<Boolean> condition) throws InterruptedException {
        long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
        while (!Boolean.TRUE.equals(condition.get()) && System.nanoTime() < deadline) {
            Thread.sleep(50);
        }
        assertThat(condition.get()).isTrue();
    }

    private Tier0 connect() throws Exception {
        WebSocketHttpHeaders headers = new WebSocketHttpHeaders();
        headers.add(HttpHeaders.AUTHORIZATION, "Bearer " + TOKEN);
        BlockingQueue<JsonNode> received = new LinkedBlockingQueue<>();
        WebSocketSession session = new StandardWebSocketClient().execute(new TextWebSocketHandler() {
            @Override
            protected void handleTextMessage(WebSocketSession s, TextMessage message) {
                received.add(MAPPER.readTree(message.getPayload()));
            }
        }, headers, URI.create("ws://localhost:" + port + Tier0Paths.I1)).get(5, TimeUnit.SECONDS);
        return new Tier0(session, received);
    }

    private record Tier0(WebSocketSession session, BlockingQueue<JsonNode> received) implements AutoCloseable {

        void send(JsonNode message) throws Exception {
            session.sendMessage(new TextMessage(MAPPER.writeValueAsString(message)));
        }

        JsonNode await(String msgType, Predicate<JsonNode> matches) throws InterruptedException {
            return awaitEnvelope(msgType, matches).get("payload");
        }

        JsonNode awaitEnvelope(String msgType, Predicate<JsonNode> matches) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = received.poll(200, TimeUnit.MILLISECONDS);
                if (message != null && msgType.equals(message.path("msg_type").asString())
                        && matches.test(message.get("payload"))) {
                    return message;
                }
            }
            throw new AssertionError("기다린 " + msgType + " 가 오지 않았다");
        }

        JsonNode poll(String msgType, long timeoutMs) throws InterruptedException {
            long deadline = System.nanoTime() + Duration.ofMillis(timeoutMs).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = received.poll(50, TimeUnit.MILLISECONDS);
                if (message != null && msgType.equals(message.path("msg_type").asString())) {
                    return message.get("payload");
                }
            }
            return null;
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
