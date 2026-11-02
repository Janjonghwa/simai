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
import com.ssafy.simai.global.contract.ContractSchemas;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Duration;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
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
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** TBM 작업 단위 보고서 API(POST·GET /api/reports/tbm) 및 V27 마이그레이션 통합 시험 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class TbmReportIntegrationTest {

    private static final String PASSWORD = "correct-horse";
    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    AppUserRepository users;

    @Autowired
    PasswordEncoder passwordEncoder;

    @Autowired
    TbmReportService reportService;

    @Autowired
    ContractSchemas contractSchemas;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String zoneId = "z-tbm-" + suffix;
    private final String gatewayId = "tier0-tbm-" + suffix;
    private final Instant workStart = Instant.now().plus(Duration.ofHours(1)).truncatedTo(ChronoUnit.MINUTES);
    private final Instant workEnd = workStart.plus(Duration.ofHours(3));

    private HttpServer fakeEngine;
    private final AtomicReference<String> lastPlanReqBody = new AtomicReference<>();
    private final AtomicReference<String> lastReportReqBody = new AtomicReference<>();
    private final AtomicInteger planStatus = new AtomicInteger(200);
    private final AtomicInteger reportStatus = new AtomicInteger(200);
    private volatile long engineDelayMs = 0L;
    private volatile String customReportResponseJson = null;
    private volatile CountDownLatch blockLatch = null;
    private volatile CountDownLatch enteredLatch = null;

    @BeforeEach
    void setUp() throws IOException {
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                VALUES (?, 1, ?, '개구부 A', 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText('POLYGON((0 0, 2 0, 2 2, 0 0))', 0), 'CRITICAL', now())
                """, zoneId, site);

        // 최근 7일 안 LIVE 이벤트 2건(HIGH, CRITICAL) + 10일 전 LIVE 1건(제외) + 최근 REPLAY 1건(제외)
        Instant now = Instant.now();
        insertEvent("HIGH", "R1", zoneId, now.minus(Duration.ofDays(2)), "LIVE");
        insertEvent("CRITICAL", "R2", zoneId, now.minus(Duration.ofHours(6)), "LIVE");
        insertEvent("CRITICAL", "R1", zoneId, now.minus(Duration.ofDays(10)), "LIVE");
        insertEvent("CRITICAL", "R1", zoneId, now.minus(Duration.ofHours(2)), "REPLAY");

        fakeEngine = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fakeEngine.setExecutor(Executors.newCachedThreadPool());
        fakeEngine.createContext("/v1/plan/evaluate", this::handlePlanEvaluate);
        fakeEngine.createContext("/v1/report/generate", this::handleReportGenerate);
        fakeEngine.start();

        String engineUrl = "http://127.0.0.1:" + fakeEngine.getAddress().getPort();
        reportService.configureRiskEngine(engineUrl, Duration.ofSeconds(5));
    }

    @AfterEach
    void cleanUp() {
        if (blockLatch != null) {
            blockLatch.countDown();
        }
        if (fakeEngine != null) {
            fakeEngine.stop(0);
        }
        jdbc.update("""
                DELETE FROM report WHERE work_id IN (
                    SELECT work_id FROM work_unit WHERE created_by IN (
                        SELECT user_id FROM app_user WHERE login_id LIKE ?
                    )
                ) OR layout_id IN (SELECT layout_id FROM layout_plan WHERE name LIKE '시험레이아웃-%')
                """, "%-" + suffix);
        jdbc.update("DELETE FROM work_unit WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id LIKE ?) OR layout_id IN (SELECT layout_id FROM layout_plan WHERE name LIKE '시험레이아웃-%')", "%-" + suffix);
        jdbc.update("DELETE FROM layout_plan_revision WHERE layout_id IN (SELECT layout_id FROM layout_plan WHERE name LIKE '시험레이아웃-%')");
        jdbc.update("DELETE FROM layout_plan WHERE name LIKE '시험레이아웃-%'");
        jdbc.update("DELETE FROM risk_event WHERE gateway_id LIKE 'tier0-tbm-%'");
        jdbc.update("DELETE FROM zone WHERE zone_id LIKE 'z-tbm-%'");
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + suffix);
    }

    @Test
    void V27_법령그래프와_보고서_스키마가_정상_적용된다() {
        Integer ragTables = jdbc.queryForObject("""
                SELECT count(*) FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name IN ('rag_source', 'rag_node', 'rag_edge', 'report')
                """, Integer.class);
        assertThat(ragTables).isEqualTo(4);

        Integer templateCols = jdbc.queryForObject("""
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'rag_query_template'
                  AND column_name IN ('rule', 'target', 'query_input')
                """, Integer.class);
        assertThat(templateCols).isEqualTo(3);
    }

    @Test
    void 보고서_비동기_생성_성공_및_재생성_버전_증가와_개인정보_제외를_검증한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        String viewer = token("viewer", Role.VIEWER);

        long layoutId = createLayout(admin);
        long workId = createWork(admin, layoutId, "OPENING_WORK", "기밀작업명-김철수", "특수장소-박영희", 7, "홍길동반장조");

        // 1차 보고서 생성 요청 -> 202 GENERATING
        String accepted1 = mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.work_id").value(workId))
                .andExpect(jsonPath("$.report_version").value(1))
                .andExpect(jsonPath("$.status").value("GENERATING"))
                .andReturn().getResponse().getContentAsString();
        long reportId1 = ((Number) JsonPath.read(accepted1, "$.report_id")).longValue();

        ObjectNode completed1 = awaitReportDone(viewer, reportId1);
        assertThat(completed1.get("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(completed1.get("failure_reason").isNull()).isTrue();
        assertThat(completed1.get("layout_id").asLong()).isEqualTo(layoutId);
        assertThat(completed1.get("layout_revision").asInt()).isEqualTo(1);
        assertThat(completed1.path("work_type").path("code").asString()).isEqualTo("OPENING_WORK");
        assertThat(completed1.path("work_type").path("label").asString()).isEqualTo("개구부 주변·단부 작업");
        assertThat(completed1.path("header").path("name").asString()).isEqualTo("기밀작업명-김철수");
        assertThat(completed1.path("header").path("place").asString()).isEqualTo("특수장소-박영희");
        assertThat(completed1.path("header").path("planned_people").asInt()).isEqualTo(7);
        assertThat(completed1.path("header").path("lead").asString()).isEqualTo("홍길동반장조");
        assertThat(completed1.path("risk_items").size()).isEqualTo(1);
        assertThat(completed1.path("zone_history").size()).isEqualTo(1);
        assertThat(completed1.path("zone_history").get(0).path("zone_id").asString()).isEqualTo(zoneId);
        assertThat(completed1.path("zone_history").get(0).path("live_event_count").asInt()).isEqualTo(2);
        assertThat(completed1.path("zone_history").get(0).path("max_severity").asString()).isEqualTo("CRITICAL");
        assertThat(completed1.path("sections").size()).isEqualTo(4);
        assertThat(completed1.path("citations").size()).isEqualTo(1);
        assertThat(completed1.path("llm_model").asString()).isEqualTo("gpt-6.1-sol");
        assertThat(completed1.path("completed_at_ms").isNumber()).isTrue();

        // 계약 검증: /v1/plan/evaluate 에 보낸 risk_plan_request 가 JSON Schema 를 통과하고 PLANNED 출처인지 확인
        assertThat(lastPlanReqBody.get()).isNotNull();
        assertThat(contractSchemas.violations("messages/risk_plan_request", lastPlanReqBody.get())).isEmpty();
        JsonNode planSent = MAPPER.readTree(lastPlanReqBody.get());
        assertThat(planSent.path("payload").path("source").asString()).isEqualTo("PLANNED");
        assertThat(planSent.path("payload").path("layout").path("zones").size()).isEqualTo(2);
        assertThat(planSent.path("payload").path("layout").path("work_surfaces").size()).isEqualTo(1);

        // 개인정보·작업 메타 경계 검증: /v1/report/generate 에 작업명·인원·조 이름·장소 텍스트가 들어가지 않는다
        String reportSentRaw = lastReportReqBody.get();
        assertThat(reportSentRaw).isNotNull();
        assertThat(reportSentRaw).doesNotContain("기밀작업명-김철수", "특수장소-박영희", "홍길동반장조", "planned_people", "lead_team");
        JsonNode reportSent = MAPPER.readTree(reportSentRaw);
        assertThat(reportSent.has("name")).isFalse();
        assertThat(reportSent.has("planned_people")).isFalse();
        assertThat(reportSent.has("lead")).isFalse();
        assertThat(reportSent.has("place")).isFalse();

        // 2차 재생성 -> report_version 2 생성, 이전 버전 1 보존
        String accepted2 = mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.report_version").value(2))
                .andReturn().getResponse().getContentAsString();
        long reportId2 = ((Number) JsonPath.read(accepted2, "$.report_id")).longValue();
        awaitReportDone(viewer, reportId2);

        mvc.perform(get("/api/reports/tbm").param("work_id", String.valueOf(workId))
                        .header(HttpHeaders.AUTHORIZATION, viewer))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.latest.report_id").value(reportId2))
                .andExpect(jsonPath("$.latest.report_version").value(2))
                .andExpect(jsonPath("$.versions.length()").value(2))
                .andExpect(jsonPath("$.versions[0].report_version").value(2))
                .andExpect(jsonPath("$.versions[1].report_version").value(1));
    }

    @Test
    void 레이아웃_미연결과_중복_생성_409_응답을_검증한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        long workWithoutLayout = createWork(admin, null, "GENERAL", "레이아웃없는작업", null, 2, null);

        mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workWithoutLayout + "}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_HAS_NO_LAYOUT"));

        long layoutId = createLayout(admin);
        long workId = createWork(admin, layoutId, "OPENING_WORK", "동시생성작업", null, 3, "A조");

        blockLatch = new CountDownLatch(1);
        enteredLatch = new CountDownLatch(1);

        String first = mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isAccepted())
                .andReturn().getResponse().getContentAsString();
        long inProgressId = ((Number) JsonPath.read(first, "$.report_id")).longValue();

        assertThat(enteredLatch.await(5, TimeUnit.SECONDS)).isTrue();

        mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_IN_PROGRESS"))
                .andExpect(jsonPath("$.report_id").value(inProgressId));

        blockLatch.countDown();
        awaitReportDone(admin, inProgressId);
    }

    @Test
    void 엔진_오류와_타임아웃_및_인용위반_실패와_재시작_복구를_검증한다() throws Exception {
        String admin = token("admin", Role.ADMIN);
        long layoutId = createLayout(admin);
        long workId = createWork(admin, layoutId, "OPENING_WORK", "실패시험작업", null, 2, null);

        // 1) 엔진 500 -> ENGINE_UNAVAILABLE
        planStatus.set(500);
        long r1 = triggerReport(admin, workId);
        ObjectNode done1 = awaitReportDone(admin, r1);
        assertThat(done1.get("status").asString()).isEqualTo("FAILED");
        assertThat(done1.get("failure_reason").asString()).isEqualTo("ENGINE_UNAVAILABLE");
        planStatus.set(200);

        // 2) 컨테이너 보고서 생성 CITATION_VIOLATION 실패 -> FAILED + 사유 보존
        customReportResponseJson = """
                {
                  "status": "FAILED",
                  "failure_reason": "CITATION_VIOLATION",
                  "sections": [],
                  "citations": [],
                  "subgraph": {"nodes": [{"id": "n1"}], "edges": []},
                  "steps": [{"step": "CODE_VERIFY", "ok": false}],
                  "llm": {"model": "gpt-6.1-sol", "calls": 2}
                }
                """;
        long r2 = triggerReport(admin, workId);
        ObjectNode done2 = awaitReportDone(admin, r2);
        assertThat(done2.get("status").asString()).isEqualTo("FAILED");
        assertThat(done2.get("failure_reason").asString()).isEqualTo("CITATION_VIOLATION");
        assertThat(done2.path("subgraph").path("nodes").size()).isEqualTo(1);
        assertThat(done2.path("steps").size()).isEqualTo(1);
        assertThat(done2.path("sections").size()).isEqualTo(0);
        customReportResponseJson = null;

        // 3) 엔진 타임아웃 -> ENGINE_TIMEOUT
        String engineUrl = "http://127.0.0.1:" + fakeEngine.getAddress().getPort();
        reportService.configureRiskEngine(engineUrl, Duration.ofMillis(200));
        engineDelayMs = 600L;
        long r3 = triggerReport(admin, workId);
        ObjectNode done3 = awaitReportDone(admin, r3);
        assertThat(done3.get("status").asString()).isEqualTo("FAILED");
        assertThat(done3.get("failure_reason").asString()).isEqualTo("ENGINE_TIMEOUT");
        engineDelayMs = 0L;
        reportService.configureRiskEngine(engineUrl, Duration.ofSeconds(5));

        // 4) 서버 재시작 복구: GENERATING 행이 INTERRUPTED 로 전환
        Long stuckId = jdbc.queryForObject("""
                INSERT INTO report (report_kind, work_id, layout_id, layout_revision, report_version, status, created_at)
                VALUES ('TBM', ?, ?, 1, 99, 'GENERATING', now())
                RETURNING report_id
                """, Long.class, workId, layoutId);
        int recovered = reportService.recoverInterrupted();
        assertThat(recovered).isGreaterThanOrEqualTo(1);

        mvc.perform(get("/api/reports/tbm/{id}", stuckId).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.failure_reason").value("INTERRUPTED"));
    }

    // ---- 가짜 Risk Engine 핸들러 및 도우미

    private void handlePlanEvaluate(HttpExchange ex) throws IOException {
        try {
            if (enteredLatch != null) {
                enteredLatch.countDown();
            }
            if (blockLatch != null) {
                blockLatch.await(5, TimeUnit.SECONDS);
            }
            if (engineDelayMs > 0) {
                Thread.sleep(engineDelayMs);
            }
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            lastPlanReqBody.set(body);
            int code = planStatus.get();
            if (code != 200) {
                writeResponse(ex, code, "{\"error\":\"fail\"}");
                return;
            }
            JsonNode req = MAPPER.readTree(body);
            String reqId = req.path("payload").path("request_id").asString();
            String layoutId = req.path("payload").path("layout").path("layout_id").asString();
            String layoutVersion = req.path("payload").path("layout").path("layout_version").asString();
            long evalTime = req.path("payload").path("evaluation_time").asLong();

            ObjectNode res = MAPPER.createObjectNode();
            res.put("schema_version", "1.0.0");
            res.put("msg_type", "risk_plan_result");
            res.put("msg_id", UUID.randomUUID().toString());
            res.put("sent_at", Instant.now().toEpochMilli());
            res.put("source_id", "risk-engine-01");
            ObjectNode payload = res.putObject("payload");
            payload.put("request_id", reqId);
            payload.put("source", "PLANNED");
            payload.put("status", "OK");
            payload.put("computed_at", Instant.now().toEpochMilli());
            payload.put("engine_version", "0.1.0");
            payload.put("evaluation_time", evalTime);
            payload.put("layout_id", layoutId);
            payload.put("layout_version", layoutVersion);
            payload.set("rules", req.path("payload").path("rules"));
            ArrayNode positions = payload.putArray("positions");
            ObjectNode pos = positions.addObject();
            pos.put("position_id", "gen-01");
            pos.putArray("position_xy").add(1.0).add(1.0);
            pos.put("support_surface_id", "floor");
            pos.put("position_source", "GENERATED");
            ArrayNode items = payload.putArray("items");
            ObjectNode item = items.addObject();
            item.put("rule_id", "R1");
            item.put("rule_version", 1);
            item.put("severity", "CRITICAL");
            item.putArray("position_ids").add("gen-01");
            item.put("zone_id", zoneId);
            item.putObject("metrics").put("zone_distance_m", 0.25);
            item.putArray("reason_codes").add("inside_zone");
            item.putArray("confidence_reasons");
            writeResponse(ex, 200, MAPPER.writeValueAsString(res));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            writeResponse(ex, 500, "{}");
        }
    }

    private void handleReportGenerate(HttpExchange ex) throws IOException {
        try {
            if (engineDelayMs > 0) {
                Thread.sleep(engineDelayMs);
            }
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            lastReportReqBody.set(body);
            int code = reportStatus.get();
            if (code != 200) {
                writeResponse(ex, code, "{\"error\":\"fail\"}");
                return;
            }
            if (customReportResponseJson != null) {
                writeResponse(ex, 200, customReportResponseJson);
                return;
            }
            String okJson = """
                    {
                      "status": "SUCCEEDED",
                      "failure_reason": null,
                      "sections": [
                        {"key": "hazards", "title": "주요 위험요인", "body": "개구부 추락 위험", "citations": ["산업안전보건기준규칙 제43조"]},
                        {"key": "measures", "title": "안전대책", "body": "안전난간 및 덮개 설치", "citations": ["산업안전보건기준규칙 제43조"]},
                        {"key": "focus", "title": "중점 위험·중점 대책", "body": "개구부 주변 작업 전 덮개 고정 확인", "citations": ["산업안전보건기준규칙 제43조"]},
                        {"key": "legal", "title": "법령 근거 요약", "body": "제43조 개구부 등의 방호 조치", "citations": ["산업안전보건기준규칙 제43조"]}
                      ],
                      "citations": [
                        {"node_id": "산업안전보건기준규칙 제43조", "source_name": "산업안전보건기준에 관한 규칙", "article_ref": "제43조", "title": "개구부 등의 방호 조치", "excerpt": "사업주는 작업발판 및 통로의 끝이나 개구부로서 추락할 위험이 있는 장소에는 안전난간, 울타리 등을 설치하여야 한다."}
                      ],
                      "subgraph": {"nodes": [{"node_id": "산업안전보건기준규칙 제43조"}], "edges": []},
                      "steps": [{"step": "CANDIDATE_SEARCH"}, {"step": "PRE_FILTER"}, {"step": "LLM_DRAFT"}, {"step": "CODE_VERIFY"}],
                      "llm": {"model": "gpt-6.1-sol", "calls": 1}
                    }
                    """;
            writeResponse(ex, 200, okJson);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            writeResponse(ex, 500, "{}");
        }
    }

    private static void writeResponse(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(status, bytes.length);
        try (OutputStream os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private long triggerReport(String token, long workId) throws Exception {
        String res = mvc.perform(post("/api/reports/tbm")
                        .header(HttpHeaders.AUTHORIZATION, token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isAccepted())
                .andReturn().getResponse().getContentAsString();
        return ((Number) JsonPath.read(res, "$.report_id")).longValue();
    }

    private ObjectNode awaitReportDone(String token, long reportId) throws Exception {
        long deadline = System.currentTimeMillis() + 5_000L;
        while (System.currentTimeMillis() < deadline) {
            String body = mvc.perform(get("/api/reports/tbm/{id}", reportId)
                            .header(HttpHeaders.AUTHORIZATION, token))
                    .andExpect(status().isOk())
                    .andReturn().getResponse().getContentAsString();
            ObjectNode node = (ObjectNode) MAPPER.readTree(body);
            String st = node.path("status").asString("");
            if (!"GENERATING".equals(st)) {
                return node;
            }
            Thread.sleep(50L);
        }
        throw new AssertionError("Report " + reportId + " did not complete within timeout");
    }

    private long createLayout(String adminToken) throws Exception {
        String payload = """
                {
                  "name": "시험레이아웃-%s",
                  "items": [
                    {"item_id": "open-1", "kind": "OPENING", "center": [1.0, 2.0, 0.0], "size": [1.2, 1.2, 0.2], "yaw_rad": 0.0, "label": "개구부 1"},
                    {"item_id": "equip-1", "kind": "EQUIPMENT", "center": [4.0, 2.0, 1.0], "size": [2.0, 1.5, 2.0], "yaw_rad": 0.5, "label": "지게차 구역"},
                    {"item_id": "scaf-1", "kind": "SCAFFOLD", "center": [2.0, 3.0, 1.0], "size": [2.0, 1.0, 2.0], "yaw_rad": 0.0, "label": "비계 1"}
                  ]
                }
                """.formatted(suffix);
        String created = mvc.perform(post("/api/layouts")
                        .header(HttpHeaders.AUTHORIZATION, adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(payload))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        return ((Number) JsonPath.read(created, "$.layout_id")).longValue();
    }

    private long createWork(String adminToken, Long layoutId, String workType, String name, String place, int people, String lead)
            throws Exception {
        ObjectNode b = MAPPER.createObjectNode();
        b.put("date", "2026-11-03");
        b.put("name", name);
        b.put("from_ms", workStart.toEpochMilli());
        b.put("to_ms", workEnd.toEpochMilli());
        b.put("zone_id", zoneId);
        if (place != null) {
            b.put("place", place);
        }
        b.put("planned_people", people);
        if (lead != null) {
            b.put("lead", lead);
        }
        if (layoutId != null) {
            b.put("layout_id", layoutId);
        }
        if (workType != null) {
            b.put("work_type", workType);
        }
        String created = mvc.perform(post("/api/work-units")
                        .header(HttpHeaders.AUTHORIZATION, adminToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(MAPPER.writeValueAsString(b)))
                .andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        return Long.parseLong(JsonPath.read(created, "$.work_id"));
    }

    private void insertEvent(String severity, String ruleId, String zone, Instant raisedAt, String source) {
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence, raised_at, updated_at,
                    delivery, retention_hold)
                VALUES (?, ?, ?, ?, 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, '{w-1}', ?, '{}', 0, 1, ?, ?, 'REALTIME', false)
                """, UUID.randomUUID(), source, gatewayId, ruleId, severity, zone, Timestamp.from(raisedAt), Timestamp.from(raisedAt));
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
