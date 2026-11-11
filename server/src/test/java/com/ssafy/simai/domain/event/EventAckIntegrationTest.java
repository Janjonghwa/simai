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
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketHttpHeaders;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.client.standard.StandardWebSocketClient;
import org.springframework.web.socket.handler.TextWebSocketHandler;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 목록·확인 API 와 Tier 0 전달(S15P31A101-121, 명세 12.6·3.2·3.10). 실제 포트 I1 로 Tier 0 를 흉내 내 event_ack 를 받는다.
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureMockMvc
@ActiveProfiles("test")
class EventAckIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final String TOKEN = "test-only-tier0-token";
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

    private final String gatewayId = "tier0-ack-" + UUID.randomUUID();
    private final String suffix = UUID.randomUUID().toString().substring(0, 8);

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event_update WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM event_ack WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM journal_cursor WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 확인하면_Tier0_에_event_ack_를_보내고_새_확인을_붙인_이벤트를_돌려준다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.send(transition(event, 2, 1, "UPDATED", null));
            tier0.send(snapshot(1L));
            tier0.await("journal_ack", p -> !p.get("sync_mode").booleanValue());

            String body = mvc.perform(post("/api/events/{id}/ack", event).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content("{\"event_sequence\": 1}"))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.event_id").value(event.toString()))
                    .andExpect(jsonPath("$.event_sequence").value(2))
                    .andExpect(jsonPath("$.acks.length()").value(1))
                    .andExpect(jsonPath("$.acks[0].channel").value("CONTROL_ROOM"))
                    .andExpect(jsonPath("$.acks[0].actor_role").value("ADMIN"))
                    .andReturn().getResponse().getContentAsString();
            String ackId = JsonPath.read(body, "$.acks[0].ack_id");

            JsonNode sent = tier0.await("event_ack", p -> ackId.equals(p.path("ack").path("ack_id").asString()));
            assertThat(sent.get("event_id").asString()).isEqualTo(event.toString());
            assertThat(sent.get("event_sequence").intValue()).isEqualTo(1);
            assertThat(sent.path("ack").path("actor_role").asString()).isEqualTo("ADMIN");

            // Tier 0 가 확인을 저널에 기록하고 같은 ack_id 의 ACKNOWLEDGED 전이를 돌려준다
            tier0.send(transition(event, 3, 2, "ACKNOWLEDGED", ack(ackId, "CONTROL_ROOM", "ADMIN", "0")));
            awaitTrue(() -> jdbc.queryForObject("SELECT journal_event_sequence = 3 FROM event_ack WHERE ack_id = ?::uuid",
                    Boolean.class, ackId));
        }
    }

    @Test
    void Tier0_가_끊겨_있으면_저장만_하고_다시_붙어_동기화가_끝나면_같은_ack_id_로_보낸다() throws Exception {
        UUID event = UUID.randomUUID();
        String foreman = token("foreman", Role.FOREMAN);
        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 0);
        }

        // 반장 PWA 에서 확인(channel 을 싣는다)
        String body = mvc.perform(post("/api/events/{id}/ack", event).header(HttpHeaders.AUTHORIZATION, foreman)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"channel\": \"FOREMAN_PWA\"}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.acks[0].channel").value("FOREMAN_PWA"))
                .andReturn().getResponse().getContentAsString();
        String ackId = JsonPath.read(body, "$.acks[0].ack_id");

        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(0L));
            JsonNode resent = tier0.await("event_ack", p -> ackId.equals(p.path("ack").path("ack_id").asString()));
            assertThat(resent.get("event_sequence").intValue()).isEqualTo(1);
        }
    }

    @Test
    void 현장_버튼_확인은_전이가_오면_event_ack_로_남는다() throws Exception {
        UUID event = UUID.randomUUID();
        String siteAck = UUID.randomUUID().toString();

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.send(transition(event, 2, 1, "ACKNOWLEDGED", ack(siteAck, "SITE_BUTTON", "SITE", "laptop-01")));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
        }

        assertThat(jdbc.queryForObject("SELECT channel FROM event_ack WHERE ack_id = ?::uuid", String.class, siteAck))
                .isEqualTo("SITE_BUTTON");
        assertThat(jdbc.queryForObject("SELECT journal_event_sequence FROM event_ack WHERE ack_id = ?::uuid", Integer.class, siteAck))
                .isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT ack_id::text FROM risk_event_update WHERE event_id = ? AND event_sequence = 2",
                String.class, event)).isEqualTo(siteAck);
    }

    @Test
    void Tier0_저널에_남은_현장_버튼_확인은_재연결_동기화로_EC2_에_반영된다() throws Exception {
        // G3 D-G3-15 의 EC2 몫: Tier 0 저널에 남은 ACKNOWLEDGED(SITE_BUTTON) 전이가 재연결 뒤 resent 로 다시 오고,
        // 동기화가 끝나면 EC2 가 같은 확인을 기록하고 이벤트 상세에 붙인다
        UUID event = UUID.randomUUID();
        String siteAck = UUID.randomUUID().toString();
        String foreman = token("foreman", Role.FOREMAN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 0);
        }
        // 연결이 끊긴 동안 노트북의 현장 버튼 확인은 Tier 0 저널에만 쌓인다. 다시 붙으면 Tier 0 가 그 전이를 다시 보낸다

        try (Tier0 tier0 = connect()) {
            ObjectNode resent = transition(event, 2, 1, "ACKNOWLEDGED", ack(siteAck, "SITE_BUTTON", "SITE", "laptop-01"));
            ((ObjectNode) resent.get("payload")).put("resent", true);
            tier0.send(resent);
            tier0.send(snapshot(1L));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1 && !p.get("sync_mode").booleanValue());
        }

        assertThat(jdbc.queryForObject("SELECT channel FROM event_ack WHERE ack_id = ?::uuid", String.class, siteAck))
                .isEqualTo("SITE_BUTTON");
        assertThat(jdbc.queryForObject("SELECT journal_event_sequence FROM event_ack WHERE ack_id = ?::uuid", Integer.class, siteAck))
                .isEqualTo(2);
        // 관제 화면 상세에도 현장 확인이 붙는다
        mvc.perform(get("/api/events/{id}", event).header(HttpHeaders.AUTHORIZATION, foreman))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.acks[0].ack_id").value(siteAck))
                .andExpect(jsonPath("$.acks[0].channel").value("SITE_BUTTON"));
    }

    @Test
    void 해소된_이벤트와_없는_이벤트는_확인하지_않는다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", null));
            tier0.send(transition(event, 2, 1, "RESOLVED", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
        }

        mvc.perform(post("/api/events/{id}/ack", event).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("EVENT_RESOLVED"));
        mvc.perform(post("/api/events/{id}/ack", UUID.randomUUID()).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("EVENT_NOT_FOUND"));
        mvc.perform(post("/api/events/{id}/ack", event).header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"event_sequence\": 0}"))
                .andExpect(status().isBadRequest());
    }

    @Test
    void 목록은_반장_관리자만_최신_발생순으로_나눠_받는다() throws Exception {
        UUID older = UUID.randomUUID();
        UUID newer = UUID.randomUUID();
        try (Tier0 tier0 = connect()) {
            ObjectNode first = transition(older, 1, 0, "RAISED", null);
            ((ObjectNode) first.get("payload")).put("occurred_at", 1760342400000L);
            ObjectNode second = transition(newer, 1, 1, "RAISED", null);
            ((ObjectNode) second.get("payload")).put("occurred_at", 4102444800000L);
            tier0.send(first);
            tier0.send(second);
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
        }

        mvc.perform(get("/api/events").param("size", "1").header(HttpHeaders.AUTHORIZATION, token("foreman", Role.FOREMAN)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.content.length()").value(1))
                .andExpect(jsonPath("$.content[0].event_id").value(newer.toString()))
                .andExpect(jsonPath("$.content[0].raised_at").value(4102444800000L))
                .andExpect(jsonPath("$.content[0].acks").isArray())
                // 관제 앱 스키마의 선택 항목은 값이 없으면 빠진다(null 이면 검증 실패)
                .andExpect(jsonPath("$.content[0].resolution").doesNotExist())
                .andExpect(jsonPath("$.content[0].delivery").doesNotExist())
                .andExpect(jsonPath("$.content[0].resolved_at").doesNotExist())
                .andExpect(jsonPath("$.size").value(1));
        mvc.perform(get("/api/events").header(HttpHeaders.AUTHORIZATION, token("viewer", Role.VIEWER)))
                .andExpect(status().isForbidden());
        mvc.perform(get("/api/events")).andExpect(status().isUnauthorized());
    }

    @Test
    void 목록은_해소_전_이벤트를_해소된_것보다_앞에_둔다() throws Exception {
        // 더 최근에 해소된 이벤트가 쪽을 채워도 오래된 활성 경보가 첫 쪽에 온다. 관제 앱은 활성 없는 쪽에서 받기를 멈춘다(감사 F01, -621)
        UUID olderActive = UUID.randomUUID();
        UUID newerResolved = UUID.randomUUID();
        try (Tier0 tier0 = connect()) {
            ObjectNode active = transition(olderActive, 1, 0, "RAISED", null);
            ((ObjectNode) active.get("payload")).put("occurred_at", 4102444850000L);
            ObjectNode raised = transition(newerResolved, 1, 1, "RAISED", null);
            ((ObjectNode) raised.get("payload")).put("occurred_at", 4102444900000L);
            ObjectNode resolved = transition(newerResolved, 2, 2, "RESOLVED", null);
            ((ObjectNode) resolved.get("payload")).put("occurred_at", 4102444900000L);
            tier0.send(active);
            tier0.send(raised);
            tier0.send(resolved);
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 2);
        }

        mvc.perform(get("/api/events").param("size", "1").header(HttpHeaders.AUTHORIZATION, token("foreman", Role.FOREMAN)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.content[0].event_id").value(olderActive.toString()))
                .andExpect(jsonPath("$.content[0].lifecycle").value("RAISED"));
    }

    // ---- 도우미

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

    private ObjectNode ack(String ackId, String channel, String role, String actor) {
        ObjectNode ack = MAPPER.createObjectNode();
        ack.put("ack_id", ackId);
        ack.put("channel", channel);
        ack.put("actor_role", role);
        ack.put("actor_id", actor);
        ack.put("acked_at", 1760342402500L);
        return ack;
    }

    private ObjectNode transition(UUID eventId, int sequence, long offset, String kind, ObjectNode ack) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("risk_event_transition.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("event_id", eventId.toString());
        p.put("event_sequence", sequence);
        p.put("journal_offset", offset);
        p.put("transition", kind);
        if (ack != null) {
            p.set("ack", ack);
        }
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
            long deadline = System.nanoTime() + Duration.ofSeconds(10).toNanos();
            while (System.nanoTime() < deadline) {
                JsonNode message = received.poll(200, TimeUnit.MILLISECONDS);
                if (message != null && msgType.equals(message.path("msg_type").asString()) && matches.test(message.get("payload"))) {
                    return message.get("payload");
                }
            }
            throw new AssertionError("기다린 " + msgType + " 가 오지 않았다");
        }

        @Override
        public void close() throws Exception {
            session.close();
        }
    }
}
