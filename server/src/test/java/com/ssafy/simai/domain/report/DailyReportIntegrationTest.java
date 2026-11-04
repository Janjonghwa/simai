package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.jayway.jsonpath.JsonPath;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.LocalDateTime;
import java.time.ZoneId;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** 일일 안전 일지 API(POST·GET /api/reports/daily, S15P31A101-33) 와 V36 통합 시험 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class DailyReportIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final ZoneId KST = ZoneId.of("Asia/Seoul");
    // 다른 시험이 이벤트를 넣는 '지금' 근처와 겹치지 않는 지난 날짜들
    private static final LocalDate DAY = LocalDate.of(2025, 3, 11);
    private static final LocalDate EMPTY_DAY = LocalDate.of(2025, 3, 12);
    private static final LocalDate FAIL_DAY = LocalDate.of(2025, 3, 13);
    private static final LocalDate RACE_DAY = LocalDate.of(2025, 3, 14);
    private static final LocalDate MANY_DAY = LocalDate.of(2025, 3, 15);

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    @Autowired
    TbmReportService tbmService;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String zoneId = "z-daily-" + suffix;
    private final String gatewayId = "tier0-daily-" + suffix;

    private HttpServer fakeEngine;
    private final AtomicReference<String> lastBody = new AtomicReference<>();
    private final AtomicInteger engineStatus = new AtomicInteger(200);
    private volatile String engineResponse = null;
    private volatile CountDownLatch block = null;

    @BeforeEach
    void setUp() throws IOException {
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                VALUES (?, 1, ?, '개구부 B', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0, 2 0, 2 2, 0 0))', 0), 'CRITICAL', now())
                """, zoneId, site);
        fakeEngine = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fakeEngine.setExecutor(Executors.newCachedThreadPool());
        fakeEngine.createContext("/v1/report/generate", this::handleGenerate);
        fakeEngine.start();
        // TBM 과 일일 일지가 같은 Risk Engine 호출부를 쓴다
        tbmService.configureRiskEngine("http://127.0.0.1:" + fakeEngine.getAddress().getPort(), Duration.ofSeconds(5));
    }

    @AfterEach
    void cleanUp() {
        if (block != null) {
            block.countDown();
        }
        if (fakeEngine != null) {
            fakeEngine.stop(0);
        }
        jdbc.update("DELETE FROM report WHERE report_kind = 'DAILY' AND report_date IN (?, ?, ?, ?, ?)",
                DAY, EMPTY_DAY, FAIL_DAY, RACE_DAY, MANY_DAY);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gatewayId);
        jdbc.update("DELETE FROM zone WHERE zone_id = ?", zoneId);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void 그날_LIVE_이벤트만_사람_ID_없이_넘겨_일지를_만들고_날짜별로_다시_만들면_버전이_오른다() throws Exception {
        // 그날(KST) 0시 직후·23시 59분 두 건, 전날 23시 59분·다음날 0시·REPLAY 는 빠진다
        insertEvent("CRITICAL", "R2", "RESOLVED", kst(DAY, 0, 1), "LIVE", true);
        insertEvent("HIGH", "R1", "RAISED", kst(DAY, 23, 59), "LIVE", false);
        insertEvent("HIGH", "R1", "RAISED", kst(DAY.minusDays(1), 23, 59), "LIVE", false);
        insertEvent("HIGH", "R1", "RAISED", kst(DAY.plusDays(1), 0, 0), "LIVE", false);
        insertEvent("CRITICAL", "R3", "RAISED", kst(DAY, 12, 0), "REPLAY", false);
        String admin = token("admin", Role.ADMIN);

        long first = trigger(admin, DAY.toString(), 1);
        ObjectNode done = awaitDone(admin, first);
        assertThat(done.path("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("report_kind").asString()).isEqualTo("DAILY");
        assertThat(done.path("sections").size()).isEqualTo(4);
        assertThat(done.path("llm_model").asString()).isEqualTo("gpt-6.1-sol");
        assertThat(done.path("events").size()).isEqualTo(2);
        JsonNode summary = done.path("event_summary");
        assertThat(summary.path("event_count").asInt()).isEqualTo(2);
        assertThat(summary.path("resolved_count").asInt()).isEqualTo(1);
        assertThat(summary.path("needs_check_count").asInt()).isEqualTo(1);
        assertThat(summary.path("severity_counts").path("CRITICAL").asInt()).isEqualTo(1);
        assertThat(summary.path("rule_counts").path("R1").asInt()).isEqualTo(1);

        JsonNode sent = MAPPER.readTree(lastBody.get());
        assertThat(sent.path("report_kind").asString()).isEqualTo("DAILY");
        assertThat(sent.path("report_date").asString()).isEqualTo("2025-03-11");
        assertThat(sent.path("events").size()).isEqualTo(2);
        assertThat(sent.path("events").get(0).path("rule_id").asString()).isEqualTo("R2");
        assertThat(sent.path("zones").get(0).path("zone_kind").asString()).isEqualTo("OPENING");
        assertThat(sent.path("legal_basis").isArray()).isTrue();
        // 사람 ID·근거 문장은 엔진으로 가지 않는다(5.4)
        assertThat(lastBody.get()).doesNotContain("w-비밀").doesNotContain("world_ids").doesNotContain("비밀근거");

        long second = trigger(admin, DAY.toString(), 2);
        awaitDone(admin, second);
        mvc.perform(get("/api/reports/daily").param("date", DAY.toString()).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.latest.report_id").value(second))
                .andExpect(jsonPath("$.versions.length()").value(2))
                .andExpect(jsonPath("$.versions[1].report_version").value(1));
    }

    @Test
    void 권한과_날짜_검증과_없는_보고서() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String viewer = token("viewer", Role.VIEWER);
        mvc.perform(post("/api/reports/daily").contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/api/reports/daily").header(HttpHeaders.AUTHORIZATION, viewer)
                        .contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isForbidden());
        String tomorrow = LocalDate.now(KST).plusDays(1).toString();
        for (String bad : new String[] {tomorrow, "2025-13-01", "2025/03/11", "어제"}) {
            mvc.perform(post("/api/reports/daily").header(HttpHeaders.AUTHORIZATION, admin)
                            .contentType(MediaType.APPLICATION_JSON).content("{\"report_date\":\"" + bad + "\"}"))
                    .andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_DAILY_DATE"));
        }
        mvc.perform(get("/api/reports/daily").param("date", "x").header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isBadRequest());
        mvc.perform(get("/api/reports/daily/{id}", 999_999_999L).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isNotFound());
        // 열람 계정도 조회는 된다(비어 있으면 latest null)
        mvc.perform(get("/api/reports/daily").param("date", EMPTY_DAY.toString()).header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.latest").isEmpty())
                .andExpect(jsonPath("$.versions.length()").value(0));
    }

    @Test
    void 같은_날짜를_생성_중이면_409_와_report_id_를_주고_이벤트가_없어도_만든다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        block = new CountDownLatch(1);
        long running = trigger(admin, EMPTY_DAY.toString(), 1);
        mvc.perform(post("/api/reports/daily").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"report_date\":\"" + EMPTY_DAY + "\"}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("DAILY_REPORT_IN_PROGRESS"))
                .andExpect(jsonPath("$.report_id").value(running));
        block.countDown();
        ObjectNode done = awaitDone(admin, running);
        assertThat(done.path("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("event_summary").path("event_count").asInt()).isZero();
        assertThat(MAPPER.readTree(lastBody.get()).path("events").size()).isZero();
    }

    @Test
    void 엔진_오류와_인용_위반은_FAILED_로_남고_그날_사실은_남는다() throws Exception {
        insertEvent("HIGH", "R1", "RAISED", kst(FAIL_DAY, 9, 0), "LIVE", false);
        String admin = token("admin", Role.ADMIN);

        engineStatus.set(500);
        ObjectNode down = awaitDone(admin, trigger(admin, FAIL_DAY.toString(), 1));
        assertThat(down.path("status").asString()).isEqualTo("FAILED");
        assertThat(down.path("failure_reason").asString()).isEqualTo("ENGINE_UNAVAILABLE");
        assertThat(down.path("events").size()).isEqualTo(1);
        assertThat(down.path("sections").size()).isZero();

        engineStatus.set(200);
        engineResponse = """
                {"status": "FAILED", "failure_reason": "CITATION_VIOLATION", "sections": [], "citations": [],
                 "subgraph": {"nodes": [], "edges": []}, "steps": [{"step": "CODE_VERIFY", "status": "FAILED"}],
                 "llm": {"model": "gpt-6.1-sol", "calls": 2}}
                """;
        ObjectNode violated = awaitDone(admin, trigger(admin, FAIL_DAY.toString(), 2));
        assertThat(violated.path("failure_reason").asString()).isEqualTo("CITATION_VIOLATION");
        assertThat(violated.path("steps").get(0).path("step").asString()).isEqualTo("CODE_VERIFY");
    }

    @Test
    void 같은_날짜를_동시에_여러_번_요청해도_500_없이_하나만_202_다른_건_409() throws Exception {
        String admin = token("admin", Role.ADMIN);
        block = new CountDownLatch(1);
        var pool = Executors.newFixedThreadPool(8);
        try {
            var start = new CountDownLatch(1);
            var futures = new java.util.ArrayList<java.util.concurrent.Future<Integer>>();
            for (int i = 0; i < 8; i++) {
                futures.add(pool.submit(() -> {
                    start.await();
                    return mvc.perform(post("/api/reports/daily").header(HttpHeaders.AUTHORIZATION, admin)
                                    .contentType(MediaType.APPLICATION_JSON).content("{\"report_date\":\"" + RACE_DAY + "\"}"))
                            .andReturn().getResponse().getStatus();
                }));
            }
            start.countDown();
            java.util.List<Integer> codes = new java.util.ArrayList<>();
            for (var f : futures) {
                codes.add(f.get(10, TimeUnit.SECONDS));
            }
            assertThat(codes).containsOnly(202, 409);
            assertThat(codes.stream().filter(c -> c == 202).count()).isEqualTo(1);
        } finally {
            block.countDown();
            pool.shutdown();
        }
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE report_kind = 'DAILY' AND report_date = ?",
                Integer.class, RACE_DAY)).isEqualTo(1);
    }

    @Test
    void 이벤트가_100건을_넘으면_높은_등급부터_고르고_잘림을_표시한다() throws Exception {
        for (int i = 0; i < 104; i++) {
            insertEvent("CAUTION", "R5", "RAISED", kst(MANY_DAY, 8, 0).plusSeconds(i), "LIVE", false);
        }
        insertEvent("CRITICAL", "R1", "RAISED", kst(MANY_DAY, 23, 0), "LIVE", false);
        String admin = token("admin", Role.ADMIN);
        ObjectNode done = awaitDone(admin, trigger(admin, MANY_DAY.toString(), 1));
        assertThat(done.path("events").size()).isEqualTo(100);
        assertThat(done.path("event_summary").path("event_count").asInt()).isEqualTo(105);
        assertThat(done.path("event_summary").path("events_truncated").asBoolean()).isTrue();
        JsonNode sent = MAPPER.readTree(lastBody.get()).path("events");
        // 가장 늦게 난 CRITICAL 이 남고, 발생 순이라 맨 끝이다
        assertThat(sent.get(99).path("severity").asString()).isEqualTo("CRITICAL");
        assertThat(sent.get(0).path("raised_at_ms").asLong()).isLessThan(sent.get(1).path("raised_at_ms").asLong());
    }

    private void handleGenerate(HttpExchange ex) throws IOException {
        try {
            if (block != null) {
                block.await(5, TimeUnit.SECONDS);
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        lastBody.set(new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
        if (engineStatus.get() != 200) {
            write(ex, engineStatus.get(), "{\"error\":\"fail\"}");
            return;
        }
        write(ex, 200, engineResponse != null ? engineResponse : """
                {"status": "SUCCEEDED", "failure_reason": null,
                 "sections": [
                   {"key": "summary", "title": "하루 요약", "body": "이벤트 2건", "citations": ["산업안전보건기준규칙 제43조"]},
                   {"key": "events", "title": "이벤트 되돌아보기", "body": "개구부 접근", "citations": ["산업안전보건기준규칙 제43조"]},
                   {"key": "measures", "title": "다음 작업 대책", "body": "덮개 고정", "citations": ["산업안전보건기준규칙 제43조"]},
                   {"key": "legal", "title": "법령 근거 요약", "body": "제43조", "citations": ["산업안전보건기준규칙 제43조"]}],
                 "citations": [{"node_id": "산업안전보건기준규칙 제43조", "article_ref": "제43조"}],
                 "subgraph": {"nodes": [{"node_id": "산업안전보건기준규칙 제43조"}], "edges": []},
                 "steps": [{"step": "CANDIDATE_SEARCH"}, {"step": "PRE_FILTER"}, {"step": "LLM_DRAFT"}, {"step": "CODE_VERIFY"}],
                 "llm": {"model": "gpt-6.1-sol", "calls": 1}}
                """);
    }

    private static void write(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private long trigger(String token, String date, int expectedVersion) throws Exception {
        String res = mvc.perform(post("/api/reports/daily").header(HttpHeaders.AUTHORIZATION, token)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"report_date\":\"" + date + "\"}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.status").value("GENERATING"))
                .andExpect(jsonPath("$.report_date").value(date))
                .andExpect(jsonPath("$.report_version").value(expectedVersion))
                .andReturn().getResponse().getContentAsString();
        return ((Number) JsonPath.read(res, "$.report_id")).longValue();
    }

    private ObjectNode awaitDone(String token, long reportId) throws Exception {
        long deadline = System.currentTimeMillis() + 10_000L;
        while (System.currentTimeMillis() < deadline) {
            String body = mvc.perform(get("/api/reports/daily/{id}", reportId).header(HttpHeaders.AUTHORIZATION, token))
                    .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
            ObjectNode node = (ObjectNode) MAPPER.readTree(body);
            if (!"GENERATING".equals(node.path("status").asString(""))) {
                return node;
            }
            Thread.sleep(50L);
        }
        throw new AssertionError("Daily report " + reportId + " did not complete");
    }

    private static Instant kst(LocalDate day, int hour, int minute) {
        return LocalDateTime.of(day.getYear(), day.getMonth(), day.getDayOfMonth(), hour, minute).atZone(KST).toInstant();
    }

    private void insertEvent(String severity, String ruleId, String lifecycle, Instant raisedAt, String source, boolean needsCheck) {
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, rationale, resume_count, event_sequence, raised_at,
                    updated_at, resolved_at, delivery, retention_hold)
                VALUES (?, ?, ?, ?, 1, ?, 'VERIFIED', ?, 'HIGH', ?, '{w-비밀}', ?, '{}', '비밀근거', 0, 1, ?, ?, ?, 'REALTIME', false)
                """, UUID.randomUUID(), source, gatewayId, ruleId, lifecycle, severity, needsCheck, zoneId,
                Timestamp.from(raisedAt), Timestamp.from(raisedAt),
                "RESOLVED".equals(lifecycle) ? Timestamp.from(raisedAt.plusSeconds(60)) : null);
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
}
