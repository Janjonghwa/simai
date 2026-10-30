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
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.util.Map;
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
import org.springframework.test.context.TestPropertySource;
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
 * 관리자 VLM 상향 의견 승인·거절 API 와 I1 event_escalate 송신·event_escalate_result 수신·escalations 투영 통합 시험(S15P31A101-276).
 */
@SpringBootTest(webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT)
@AutoConfigureMockMvc
@ActiveProfiles("test")
@TestPropertySource(properties = "spring.flyway.ignore-migration-patterns=*:missing,*:ignored,*:future")
class EventEscalationIntegrationTest {

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

    private final String gatewayId = "tier0-esc-" + UUID.randomUUID();
    private final String suffix = UUID.randomUUID().toString().substring(0, 8);

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM risk_event_update WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM event_escalation WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM vlm_assessment WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM analysis_job WHERE job_id LIKE ?", "job-esc-%-" + suffix);
        jdbc.update("DELETE FROM event_ack WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gatewayId);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM journal_cursor WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 승인하면_I1_event_escalate_를_보내고_Tier0_결과와_UPDATED_전이가_오면_등급과_escalations_를_반영한다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", "HIGH", null));
            tier0.send(snapshot(0L));
            tier0.await("journal_ack", p -> !p.get("sync_mode").booleanValue());

            String jobId = "job-esc-approve-" + suffix;
            long assessmentId = seedProposal(event, 1, jobId, "HIGH", "CRITICAL");

            String response = mvc.perform(post("/api/events/{id}/escalation", event)
                            .header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("""
                                    {
                                      "assessmentId": %d,
                                      "decision": "APPROVED",
                                      "note": "CCTV 2번 화면에서 비계 위 안전모 미착용 확인"
                                    }
                                    """.formatted(assessmentId)))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.event_id").value(event.toString()))
                    .andExpect(jsonPath("$.decision").value("APPROVED"))
                    .andExpect(jsonPath("$.assessment_id").value(assessmentId))
                    // EC2 는 승인 직후 등급을 직접 바꾸지 않는다(AGENTS.md 4: Tier 0 전이가 정본)
                    .andExpect(jsonPath("$.severity").value("HIGH"))
                    .andExpect(jsonPath("$.proposed_severity").value((Object) null))
                    .andReturn().getResponse().getContentAsString();
            String escalationId = JsonPath.read(response, "$.escalation_id");

            JsonNode sentEnvelope = tier0.awaitEnvelope("event_escalate",
                    p -> escalationId.equals(p.path("escalation").path("escalation_id").asString()));
            assertThat(sentEnvelope.get("schema_version").asString()).isEqualTo("1.0.0");
            JsonNode sent = sentEnvelope.get("payload");
            assertThat(sent.get("event_id").asString()).isEqualTo(event.toString());
            JsonNode esc = sent.get("escalation");
            assertThat(esc.get("basis").asString()).isEqualTo("VLM_PROPOSAL");
            assertThat(esc.get("assessment_job_id").asString()).isEqualTo(jobId);
            assertThat(esc.get("target_severity").asString()).isEqualTo("CRITICAL");
            assertThat(esc.get("channel").asString()).isEqualTo("CONTROL_ROOM");
            assertThat(esc.get("actor_role").asString()).isEqualTo("ADMIN");
            assertThat(esc.get("note").asString()).isEqualTo("CCTV 2번 화면에서 비계 위 안전모 미착용 확인");

            // Tier 0 가 event_escalate_result(ACCEPTED)와 UPDATED 전이(escalation 포함)를 보낸다
            tier0.send(escalateResult(escalationId, event, "ACCEPTED", 2, null));
            awaitTrue(() -> "ACCEPTED".equals(jdbc.queryForObject(
                    "SELECT result FROM event_escalation WHERE escalation_id = ?::uuid", String.class, escalationId)));
            assertThat(jdbc.queryForObject(
                    "SELECT event_sequence FROM event_escalation WHERE escalation_id = ?::uuid", Integer.class, escalationId))
                    .isEqualTo(2);

            tier0.send(transition(event, 2, 1, "UPDATED", "CRITICAL", (ObjectNode) esc));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);

            mvc.perform(get("/api/events/{id}", event).header(HttpHeaders.AUTHORIZATION, admin))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.severity").value("CRITICAL"))
                    .andExpect(jsonPath("$.proposed_severity").value((Object) null))
                    .andExpect(jsonPath("$.escalations.length()").value(1))
                    .andExpect(jsonPath("$.escalations[0].escalation_id").value(escalationId))
                    .andExpect(jsonPath("$.escalations[0].basis").value("VLM_PROPOSAL"))
                    .andExpect(jsonPath("$.escalations[0].assessment_job_id").value(jobId))
                    .andExpect(jsonPath("$.escalations[0].target_severity").value("CRITICAL"))
                    .andExpect(jsonPath("$.escalations[0].channel").value("CONTROL_ROOM"))
                    .andExpect(jsonPath("$.escalations[0].actor_role").value("ADMIN"))
                    .andExpect(jsonPath("$.escalations[0].note").value("CCTV 2번 화면에서 비계 위 안전모 미착용 확인"))
                    .andExpect(jsonPath("$.transitions[1].escalation_id").value(escalationId))
                    .andExpect(jsonPath("$.assessments[0].decision").value("APPROVED"));
        }
    }

    @Test
    void 거절하면_vlm_assessment_에만_기록하고_I1_event_escalate_를_보내지_않으며_등급은_그대로다() throws Exception {
        UUID event = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event, 1, 0, "RAISED", "HIGH", null));
            tier0.send(snapshot(0L));
            tier0.await("journal_ack", p -> !p.get("sync_mode").booleanValue());

            String jobId = "job-esc-reject-" + suffix;
            long assessmentId = seedProposal(event, 1, jobId, "HIGH", "CRITICAL");

            mvc.perform(post("/api/events/{id}/escalation", event)
                            .header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("""
                                    {
                                      "assessmentId": "%s",
                                      "decision": "REJECTED",
                                      "note": "안전 통로 바깥이라 상향 불필요"
                                    }
                                    """.formatted(jobId)))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.decision").value("REJECTED"))
                    .andExpect(jsonPath("$.assessment_id").value(assessmentId))
                    .andExpect(jsonPath("$.severity").value("HIGH"))
                    .andExpect(jsonPath("$.proposed_severity").value((Object) null))
                    .andExpect(jsonPath("$.escalation_id").doesNotExist());

            Map<String, Object> assessment = jdbc.queryForMap(
                    "SELECT decision, decided_by, decided_at FROM vlm_assessment WHERE assessment_id = ?", assessmentId);
            assertThat(assessment.get("decision")).isEqualTo("REJECTED");
            assertThat(assessment.get("decided_by")).isNotNull();
            assertThat(assessment.get("decided_at")).isNotNull();

            Integer escCount = jdbc.queryForObject(
                    "SELECT count(*) FROM event_escalation WHERE event_id = ?", Integer.class, event);
            assertThat(escCount).isZero();
            assertThat(tier0.poll("event_escalate", 300)).isNull();

            // 이미 거절된 의견은 다시 승인·거절할 수 없다(409)
            mvc.perform(post("/api/events/{id}/escalation", event)
                            .header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("""
                                    {"assessmentId": %d, "decision": "APPROVED"}
                                    """.formatted(assessmentId)))
                    .andExpect(status().isConflict())
                    .andExpect(jsonPath("$.code").value("ASSESSMENT_ALREADY_DECIDED"));
        }
    }

    @Test
    void Tier0_가_끊겨_있거나_JOURNAL_UNAVAILABLE_이면_재연결_동기화_뒤_같은_escalation_id_로_다시_보내고_NOT_UPWARD_는_재시도하지_않는다() throws Exception {
        UUID event1 = UUID.randomUUID();
        UUID event2 = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(event1, 1, 0, "RAISED", "HIGH", null));
            tier0.send(transition(event2, 1, 1, "RAISED", "HIGH", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 1);
        }

        long assessment1 = seedProposal(event1, 1, "job-esc-retry-" + suffix, "HIGH", "CRITICAL");
        long assessment2 = seedProposal(event2, 1, "job-esc-noup-" + suffix, "HIGH", "CRITICAL");

        // Tier 0 가 끊긴 상태에서 두 상향 의견을 승인한다
        String body1 = mvc.perform(post("/api/events/{id}/escalation", event1)
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"assessmentId\":" + assessment1 + ",\"decision\":\"APPROVED\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        String escId1 = JsonPath.read(body1, "$.escalation_id");

        String body2 = mvc.perform(post("/api/events/{id}/escalation", event2)
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"assessmentId\":" + assessment2 + ",\"decision\":\"APPROVED\"}"))
                .andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString();
        String escId2 = JsonPath.read(body2, "$.escalation_id");

        // 재연결 후 스냅샷 동기화가 끝나면 같은 escalation_id 로 둘 다 보낸다
        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(1L));
            JsonNode resent1 = tier0.await("event_escalate",
                    p -> escId1.equals(p.path("escalation").path("escalation_id").asString()));
            JsonNode resent2 = tier0.await("event_escalate",
                    p -> escId2.equals(p.path("escalation").path("escalation_id").asString()));
            assertThat(resent1.get("event_id").asString()).isEqualTo(event1.toString());
            assertThat(resent2.get("event_id").asString()).isEqualTo(event2.toString());

            // event1 은 JOURNAL_UNAVAILABLE(재시도 대상), event2 는 NOT_UPWARD(영구 거부) 응답
            tier0.send(escalateResult(escId1, event1, "REJECTED", null, "JOURNAL_UNAVAILABLE"));
            tier0.send(escalateResult(escId2, event2, "REJECTED", null, "NOT_UPWARD"));
            awaitTrue(() -> "NOT_UPWARD".equals(jdbc.queryForObject(
                    "SELECT reject_reason FROM event_escalation WHERE escalation_id = ?::uuid", String.class, escId2)));
            awaitTrue(() -> "JOURNAL_UNAVAILABLE".equals(jdbc.queryForObject(
                    "SELECT reject_reason FROM event_escalation WHERE escalation_id = ?::uuid", String.class, escId1)));
        }

        // 다시 연결되면 JOURNAL_UNAVAILABLE 인 escId1 만 같은 escalation_id 로 재전송되고, NOT_UPWARD 인 escId2 는 재전송되지 않는다
        try (Tier0 tier0 = connect()) {
            tier0.send(snapshot(1L));
            JsonNode retried1 = tier0.await("event_escalate",
                    p -> escId1.equals(p.path("escalation").path("escalation_id").asString()));
            assertThat(retried1.get("event_id").asString()).isEqualTo(event1.toString());
            assertThat(tier0.poll("event_escalate", 300)).isNull();

            // Tier 0 가 이미 처리된 escalation_id 에 DUPLICATE 를 돌려주면 완료로 기록한다
            tier0.send(escalateResult(escId1, event1, "DUPLICATE", null, null));
            awaitTrue(() -> "DUPLICATE".equals(jdbc.queryForObject(
                    "SELECT result FROM event_escalation WHERE escalation_id = ?::uuid", String.class, escId1)));
            assertThat(jdbc.queryForObject(
                    "SELECT reject_reason FROM event_escalation WHERE escalation_id = ?::uuid", String.class, escId1))
                    .isNull();
        }
    }

    @Test
    void 권한과_입력_검증_반례_비관리자_해소된_이벤트_현재_등급_이하_잘못된_요청과_계약_위반_결과를_거부한다() throws Exception {
        UUID activeEvent = UUID.randomUUID();
        UUID resolvedEvent = UUID.randomUUID();
        String admin = token("admin", Role.ADMIN);
        String foreman = token("foreman", Role.FOREMAN);
        String viewer = token("viewer", Role.VIEWER);

        try (Tier0 tier0 = connect()) {
            tier0.send(transition(activeEvent, 1, 0, "RAISED", "CRITICAL", null));
            tier0.send(transition(resolvedEvent, 1, 1, "RAISED", "HIGH", null));
            tier0.send(transition(resolvedEvent, 2, 2, "RESOLVED", "HIGH", null));
            tier0.await("journal_ack", p -> p.get("acked_offset").longValue() == 2);

            long notUpwardAssessment = seedProposal(activeEvent, 1, "job-esc-same-" + suffix, "HIGH", "CRITICAL");
            long resolvedAssessment = seedProposal(resolvedEvent, 1, "job-esc-res-" + suffix, "HIGH", "CRITICAL");

            // 1. 권한 검사: 반장(FOREMAN)·뷰어(VIEWER)·미인증은 승인/거절 불가
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, foreman)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isForbidden());
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, viewer)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isForbidden());
            mvc.perform(post("/api/events/{id}/escalation", activeEvent)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isUnauthorized());

            // 2. 이미 CRITICAL 인 이벤트에 CRITICAL 상향 승인 시 409 ESCALATION_NOT_UPWARD
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isConflict())
                    .andExpect(jsonPath("$.code").value("ESCALATION_NOT_UPWARD"));

            // 3. 해소된 이벤트는 409 EVENT_RESOLVED
            mvc.perform(post("/api/events/{id}/escalation", resolvedEvent).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + resolvedAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isConflict())
                    .andExpect(jsonPath("$.code").value("EVENT_RESOLVED"));

            // 4. 없는 이벤트·없는 평가·잘못된 decision·500자 초과 메모
            mvc.perform(post("/api/events/{id}/escalation", UUID.randomUUID()).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isNotFound())
                    .andExpect(jsonPath("$.code").value("EVENT_NOT_FOUND"));
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":999999999,\"decision\":\"APPROVED\"}"))
                    .andExpect(status().isNotFound())
                    .andExpect(jsonPath("$.code").value("ASSESSMENT_NOT_FOUND"));
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"HOLD\"}"))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_ESCALATION_DECISION"));
            mvc.perform(post("/api/events/{id}/escalation", activeEvent).header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON)
                            .content("{\"assessmentId\":" + notUpwardAssessment + ",\"decision\":\"REJECTED\",\"note\":\""
                                    + "가".repeat(501) + "\"}"))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_ESCALATION_NOTE"));
        }
    }

    // ---- 도우미

    private long seedProposal(UUID eventId, int sequence, String jobId, String ruleSeverity, String proposedSeverity) {
        Timestamp now = Timestamp.from(Instant.now());
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, event_id, input, created_at)
                VALUES (?, 'vlm_rationale', 'VLM_FAST', 'SUCCEEDED', 1, 1, 5000, ?, '{}'::jsonb, ?)
                """, jobId, eventId, now);
        jdbc.update("""
                UPDATE risk_event SET rationale = '작업자 안전모 미착용 위험', proposed_severity = ?
                WHERE event_id = ?
                """, proposedSeverity, eventId);
        return jdbc.queryForObject("""
                INSERT INTO vlm_assessment (
                    event_id, event_sequence, job_id, job_type, model_version,
                    rule_severity, text, proposed_severity, decision,
                    event_observed_at, created_at, event_to_rationale_ms
                ) VALUES (?, ?, ?, 'vlm_rationale', 'qwen2.5-vl-7b', ?, '작업자 안전모 미착용 위험', ?, 'PENDING', ?, ?, 120)
                RETURNING assessment_id
                """, Long.class, eventId, sequence, jobId, ruleSeverity, proposedSeverity, now, now);
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

    private ObjectNode transition(UUID eventId, int sequence, long offset, String kind, String severity,
            ObjectNode escalation) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("risk_event_transition.json")));
        root.put("schema_version", "2.1.0");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("gateway_id", gatewayId);
        p.put("event_id", eventId.toString());
        p.put("event_sequence", sequence);
        p.put("journal_offset", offset);
        p.put("transition", kind);
        p.put("severity", severity);
        if (escalation != null) {
            p.set("escalation", escalation);
        }
        return root;
    }

    private ObjectNode escalateResult(String escalationId, UUID eventId, String result, Integer sequence,
            String rejectReason) throws Exception {
        ObjectNode root = (ObjectNode) MAPPER.readTree(Files.readString(EXAMPLES.resolve("event_escalate_result.json")));
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("source_id", gatewayId);
        ObjectNode p = (ObjectNode) root.get("payload");
        p.put("escalation_id", escalationId);
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
