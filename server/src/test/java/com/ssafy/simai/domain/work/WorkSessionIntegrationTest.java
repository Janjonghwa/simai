package com.ssafy.simai.domain.work;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.report.TbmReportService;
import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;
import com.ssafy.simai.domain.user.repository.AppUserRepository;
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Import;
import org.springframework.context.annotation.Primary;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.crypto.password.PasswordEncoder;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** -439: 실제 시각·동시 시작·LIVE 집계·append-only 마무리·엔진 실패 폴백 통합 시험. Lead 실행. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Import(WorkSessionIntegrationTest.ClockConfiguration.class)
class WorkSessionIntegrationTest {
    private static final JsonMapper MAPPER = JsonMapper.builder().build();
    private static final String PASSWORD = "correct-horse";
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired AppUserRepository users;
    @Autowired PasswordEncoder passwords;
    @Autowired TbmReportService reports;
    @Autowired SessionClock clock;
    @Autowired com.ssafy.simai.global.contract.ContractSchemas schemas;
    private final AtomicReference<String> endingBody = new AtomicReference<>();

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String zone = "z-session-" + suffix;
    private final String outside = "z-session-out-" + suffix;
    private final String gateway = "tier0-session-" + suffix;
    private final AtomicReference<String> requestBody = new AtomicReference<>();
    private final CountDownLatch engineEntered = new CountDownLatch(1);
    private final CountDownLatch releaseEngine = new CountDownLatch(1);
    private ExecutorService engineThreads;
    private HttpServer engine;
    private volatile int engineStatus = 200;
    private volatile boolean blockEngine;
    private Instant started;
    private String admin;

    @BeforeEach
    void setUp() throws Exception {
        started = Instant.now().truncatedTo(ChronoUnit.MILLIS);
        clock.now.set(started);
        String site = jdbc.queryForObject("SELECT site_id FROM site", String.class);
        for (String id : List.of(zone, outside)) {
            jdbc.update("""
                    INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                    VALUES (?, 1, ?, '시험 구역', 'OPENING', 'GROUND_FOOTPRINT',
                        ST_GeomFromText('POLYGON((0 0, 1 0, 1 1, 0 0))', 0), 'HIGH', now())
                    """, id, site);
        }
        admin = token("admin", Role.ADMIN);
        // 작업 시작·종료는 안전모 요구를 실은 구성 묶음을 만든다(S15P31A101-518). 묶음에 실을 보정 문서를 올려 둔다
        ObjectNode calibration = (ObjectNode) MAPPER.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        calibration.put("calibration_version", "cal-session-" + suffix);
        ObjectNode upload = MAPPER.createObjectNode().put("reason", "시험 보정");
        upload.set("document", calibration);
        mvc.perform(post("/api/calibrations").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(MAPPER.writeValueAsString(upload))).andExpect(status().isCreated());
        engine = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        engineThreads = Executors.newVirtualThreadPerTaskExecutor();
        engine.setExecutor(engineThreads);
        engine.createContext("/v1/report/wrap-up", exchange -> {
            requestBody.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            engineEntered.countDown();
            try {
                if (blockEngine && !releaseEngine.await(5, TimeUnit.SECONDS)) throw new IllegalStateException("engine latch timeout");
                byte[] body = "{\"text\":\"집계 사실에 따른 마무리 문단\",\"fallback\":false,\"model\":\"fake-llm\"}"
                        .getBytes(StandardCharsets.UTF_8);
                exchange.getResponseHeaders().set("Content-Type", "application/json");
                exchange.sendResponseHeaders(engineStatus, body.length);
                try (var stream = exchange.getResponseBody()) {
                    stream.write(body);
                }
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                exchange.close();
            }
        });
        // 날짜 작업 종료 미팅(S15P31A101-616 B10). request_id 를 그대로 돌려준다
        engine.createContext("/v1/report/form-ending", exchange -> {
            String requestText = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            endingBody.set(requestText);
            ObjectNode res = MAPPER.createObjectNode();
            res.put("request_id", MAPPER.readTree(requestText).path("request_id").asString());
            res.put("ending", "엔진이 쓴 종료 미팅 문단").put("fallback", false).putNull("fallback_reason").put("model", "fake-llm");
            byte[] body = MAPPER.writeValueAsString(res).getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().set("Content-Type", "application/json");
            exchange.sendResponseHeaders(engineStatus, body.length);
            try (var stream = exchange.getResponseBody()) {
                stream.write(body);
            }
        });
        engine.start();
        reports.configureRiskEngine("http://127.0.0.1:" + engine.getAddress().getPort(), Duration.ofSeconds(2));
    }

    @AfterEach
    void cleanUp() {
        releaseEngine.countDown();
        if (engine != null) engine.stop(0);
        if (engineThreads != null) engineThreads.close();
        jdbc.update("DELETE FROM event_ack WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gateway);
        jdbc.update("DELETE FROM event_close WHERE event_id IN (SELECT event_id FROM risk_event WHERE gateway_id = ?)", gateway);
        jdbc.update("DELETE FROM risk_event WHERE gateway_id = ?", gateway);
        jdbc.update("DELETE FROM report WHERE work_id IN (SELECT work_id FROM work_unit WHERE name = ?)", "개인정보작업-" + suffix);
        jdbc.update("DELETE FROM static_object WHERE work_id IN (SELECT work_id FROM work_unit WHERE name = ?)", "개인정보작업-" + suffix);
        jdbc.update("DELETE FROM zone WHERE work_id IN (SELECT work_id FROM work_unit WHERE name = ?)", "개인정보작업-" + suffix);
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "개인정보작업-" + suffix);
        jdbc.update("DELETE FROM layout_plan_revision WHERE layout_id IN (SELECT layout_id FROM layout_plan WHERE name = ?)", "배치-" + suffix);
        jdbc.update("DELETE FROM layout_plan WHERE name = ?", "배치-" + suffix);
        jdbc.update("DELETE FROM zone WHERE zone_id IN (?, ?)", zone, outside);
        String users = "SELECT user_id FROM app_user WHERE login_id LIKE '%" + suffix + "'";
        List<String> versions = jdbc.queryForList("SELECT config_version FROM config_bundle WHERE issued_by IN (" + users
                + ") ORDER BY CAST(substring(config_version FROM 5) AS integer) DESC", String.class);
        for (String version : versions) {
            for (String table : List.of("config_ack", "config_bundle_file", "config_bundle_item", "config_bundle")) {
                jdbc.update("DELETE FROM " + table + " WHERE config_version = ?", version);
            }
        }
        jdbc.update("DELETE FROM calibration_document WHERE calibration_version = ?", "cal-session-" + suffix);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", "cal-session-" + suffix);
        jdbc.update("DELETE FROM refresh_token WHERE user_id IN (SELECT user_id FROM app_user WHERE login_id LIKE ?)", "%" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%" + suffix);
        clock.now.set(Instant.now());
    }

    @Test
    void 시작과_종료는_실제시각을_기록하고_보고서가_없어도_종료한다() throws Exception {
        long work = work(zone);
        mvc.perform(post("/api/work-units/{id}/end", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("IN_PROGRESS"))
                .andExpect(jsonPath("$.actual_started_at").value(started.toEpochMilli()))
                .andExpect(jsonPath("$.actual_ended_at").isEmpty());
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        // 같은 밀리초에 종료해도 유효하다.
        ObjectNode ended = end(work);
        assertThat(ended.path("work_unit").path("status").asString()).isEqualTo("DONE");
        assertThat(ended.path("work_unit").path("actual_ended_at").asLong()).isGreaterThanOrEqualTo(started.toEpochMilli());
        assertThat(ended.path("wrap_up").path("status").asString()).isEqualTo("SKIPPED");
        assertThat(ended.path("wrap_up").path("report_id").isNull()).isTrue();
        assertThat(ended.path("wrap_up").path("reason").asString()).isEqualTo("NO_REPORT");
        mvc.perform(post("/api/work-units/{id}/end", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        mvc.perform(get("/api/work-units/{id}", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("DONE"));
        mvc.perform(get("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.works[?(@.work_id == '" + work + "')].status")
                        .value(org.hamcrest.Matchers.contains("DONE")));
    }

    @Test
    void 세팅_완료_전에는_시작하지_않고_완료_뒤에_시작한다() throws Exception {
        // S15P31A101-642: 새 맵·보정·구역은 세팅 완료(C17) 때 관제에 들어간다. 완료 전 시작은 이전 맵으로 관제하므로 막는다
        long work = work(zone);
        jdbc.update("UPDATE work_unit SET setup_completed_at = NULL WHERE work_id = ?", work);
        Integer bundlesBefore = jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class);
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_SETUP_NOT_COMPLETED"));
        assertThat(jdbc.queryForObject("SELECT status FROM work_unit WHERE work_id = ?", String.class, work)).isEqualTo("PLANNED");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle", Integer.class)).isEqualTo(bundlesBefore);

        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", work);
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("IN_PROGRESS"));
        end(work);
    }

    @Test
    void 완료를_되돌리면_계획이_되고_다시_시작하고_종료할_수_있다() throws Exception {
        // S15P31A101-649: 시연 중 실수로 누른 완료 복구
        long work = todayWork(zone);
        start(work);
        clock.now.set(started.plusSeconds(13));
        end(work);
        clock.now.set(started.plusSeconds(60));
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("PLANNED"))
                .andExpect(jsonPath("$.actual_started_at").isEmpty()).andExpect(jsonPath("$.actual_ended_at").isEmpty());
        // 되돌린 뒤에는 정상 경로 그대로 다시 시작·종료한다(새 실제 시각)
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("IN_PROGRESS"))
                .andExpect(jsonPath("$.actual_started_at").value(started.plusSeconds(60).toEpochMilli()));
        clock.now.set(started.plusSeconds(120));
        ObjectNode ended = end(work);
        assertThat(ended.path("work_unit").path("status").asString()).isEqualTo("DONE");
        assertThat(ended.path("work_unit").path("actual_ended_at").asLong()).isEqualTo(started.plusSeconds(120).toEpochMilli());
    }

    @Test
    void 되돌리면_첫_실행의_종료_미팅을_비워_다시_종료가_새_사실로_채운다() throws Exception {
        long work = todayWork(zone);
        long report = reports.createDailyBlank(work, adminUserId());
        start(work);
        clock.now.set(started.plusSeconds(13));
        end(work);
        ObjectNode first = awaitWrapUp(report);
        assertThat(first.path("form").path("review").path("ending").asString()).startsWith("엔진이 쓴 종료 미팅 문단");
        int revision = first.path("form_revision").asInt();

        // 마무리를 만드는 중이면 그 결과가 되돌린 뒤 옛 사실로 양식을 채우므로 받지 않는다
        jdbc.update("UPDATE report SET wrap_up_status = 'GENERATING' WHERE report_id = ?", report);
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_WRAP_UP_GENERATING"));
        jdbc.update("UPDATE report SET wrap_up_status = 'SUCCEEDED' WHERE report_id = ?", report);

        clock.now.set(started.plusSeconds(60));
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isOk());
        ObjectNode cleared = getReport(report);
        assertThat(cleared.path("form").path("review").path("ending").asString()).isEmpty();
        assertThat(cleared.path("form_revision").asInt()).isEqualTo(revision + 1);

        start(work);
        event("LIVE", "R1", "CRITICAL", zone, started.plusSeconds(70));
        clock.now.set(started.plusSeconds(120));
        end(work);
        ObjectNode second = awaitWrapUp(report);
        // 되돌려도 실행 기록(work_run)은 지우지 않아(추가 전용, S15P31A101-696) 다시 시작이 실행 2 가 되고 실행 요약이 붙는다
        assertThat(second.path("form").path("review").path("ending").asString())
                .startsWith("실행 2회: ").contains("엔진이 쓴 종료 미팅 문단", "관제 카드 기준 사건: 위험 1건");
        assertThat(second.path("form_revision").asInt()).isEqualTo(revision + 2);
        // 두 번째 종료 요청은 새 실행 구간의 사실이다
        ObjectNode request = (ObjectNode) MAPPER.readTree(endingBody.get());
        assertThat(request.path("window_ms").get(0).asLong()).isEqualTo(started.plusSeconds(60).toEpochMilli());
        assertThat(request.path("facts").path("event_count").asInt()).isEqualTo(1);
    }

    @Test
    void 종료_뒤_한_번_더_실행하면_실행_2가_열리고_마무리는_모든_실행을_요약하며_제출하면_더_실행하지_않는다() throws Exception {
        // S15P31A101-696 방식 B: 리허설(실행 1) → 본 시연(실행 2). 기록을 지우지 않고 실행을 더한다
        long work = todayWork(zone);
        long report = reports.createDailyBlank(work, adminUserId());
        start(work);
        clock.now.set(started.plusSeconds(30));
        end(work);
        assertThat(awaitWrapUp(report).path("form").path("review").path("ending").asString()).startsWith("엔진이 쓴 종료 미팅 문단");

        clock.now.set(started.plusSeconds(300));
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("IN_PROGRESS"))
                // 하루 요약: 첫 실행 시작은 그대로, 진행 중이라 종료는 비움
                .andExpect(jsonPath("$.actual_started_at").value(started.toEpochMilli()))
                .andExpect(jsonPath("$.actual_ended_at").isEmpty())
                .andExpect(jsonPath("$.runs.length()").value(2))
                .andExpect(jsonPath("$.runs[0].run_no").value(1))
                .andExpect(jsonPath("$.runs[0].ended_at").value(started.plusSeconds(30).toEpochMilli()))
                .andExpect(jsonPath("$.runs[1].run_no").value(2))
                .andExpect(jsonPath("$.runs[1].started_at").value(started.plusSeconds(300).toEpochMilli()))
                .andExpect(jsonPath("$.runs[1].ended_at").isEmpty());
        // 진행 중 실행이 있으면 또 시작하지 않는다
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        // 실행 2 묶음의 ppe_policy: 같은 작업, 효력은 실행 2 시작
        byte[] raw = jdbc.queryForObject("""
                SELECT f.content FROM config_bundle_file f JOIN config_bundle b ON b.config_version = f.config_version
                WHERE f.name = 'ppe_policy' ORDER BY b.issued_at DESC, CAST(substring(b.config_version FROM 5) AS integer) DESC LIMIT 1
                """, byte[].class);
        String policy = new String(raw, java.nio.charset.StandardCharsets.UTF_8);
        assertThat(MAPPER.readTree(policy).path("work_id").asLong()).isEqualTo(work);
        assertThat(MAPPER.readTree(policy).path("effective_from_ms").asLong()).isEqualTo(started.plusSeconds(300).toEpochMilli());

        event("LIVE", "R1", "CRITICAL", zone, started.plusSeconds(100)); // 실행 사이(경보 꺼짐): 세지 않는다
        event("LIVE", "R1", "CRITICAL", zone, started.plusSeconds(310));
        clock.now.set(started.plusSeconds(400));
        ObjectNode ended = end(work);
        assertThat(ended.path("work_unit").path("status").asString()).isEqualTo("DONE");
        assertThat(ended.path("work_unit").path("actual_ended_at").asLong()).isEqualTo(started.plusSeconds(400).toEpochMilli());
        ObjectNode done = awaitWrapUp(report);
        // 사람이 고치지 않은 지난 종료 미팅은 모든 실행 요약을 앞에 붙여 다시 쓴다
        String ending = done.path("form").path("review").path("ending").asString();
        assertThat(ending).startsWith("실행 2회: 1회 ").contains("2회 ", "(위험 1·경고 0)", "엔진이 쓴 종료 미팅 문단", "관제 카드 기준 사건: 위험 1건");
        assertThat(MAPPER.readTree(endingBody.get()).path("facts").path("event_count").asInt()).isEqualTo(1);
        // 실행 기록: 시작·종료한 사람과 시작 전 확인 스냅샷
        long adminId = adminUserId();
        assertThat(jdbc.queryForList("SELECT started_by FROM work_run WHERE work_id = ? ORDER BY run_no", Long.class, work))
                .containsExactly(adminId, adminId);
        assertThat(jdbc.queryForObject("SELECT ended_by FROM work_run WHERE work_id = ? AND run_no = 2", Long.class, work))
                .isEqualTo(adminId);
        assertThat(MAPPER.readTree(jdbc.queryForObject("SELECT preflight::text FROM work_run WHERE work_id = ? AND run_no = 2",
                String.class, work)).path("helmet").asString()).isEqualTo("REQUIRED");

        mvc.perform(post("/api/reports/tbm/{id}/submit", report).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk());
        clock.now.set(started.plusSeconds(500));
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_REPORT_SUBMITTED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM work_run WHERE work_id = ?", Integer.class, work)).isEqualTo(2);
    }

    @Test
    void 사람이_고친_종료_미팅은_다음_실행_종료에도_두고_요약만_붙이지_않는다() throws Exception {
        long work = todayWork(zone);
        long report = reports.createDailyBlank(work, adminUserId());
        start(work);
        clock.now.set(started.plusSeconds(30));
        end(work);
        ObjectNode first = awaitWrapUp(report);
        ObjectNode form = (ObjectNode) first.path("form").deepCopy();
        ((ObjectNode) form.path("review")).put("ending", "사람이 고친 종료 미팅");
        reports.updateForm(report, form, first.path("form_revision").asInt());
        clock.now.set(started.plusSeconds(60));
        start(work);
        clock.now.set(started.plusSeconds(90));
        end(work);
        assertThat(awaitWrapUp(report).path("form").path("review").path("ending").asString()).isEqualTo("사람이 고친 종료 미팅");
    }

    @Test
    void 완료_되돌리기는_오늘의_완료된_미제출_작업만_받는다() throws Exception {
        long work = todayWork(zone);
        // 계획·진행 중은 되돌릴 완료가 없다
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        start(work);
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        end(work);

        // 다른 작업이 진행 중이면 되돌려도 시작할 수 없다
        long other = todayWork(outside);
        start(other);
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_ALREADY_IN_PROGRESS"))
                .andExpect(jsonPath("$.work_id").value(String.valueOf(other)));
        end(other);

        // 제출한 TBM 보고서는 그 실행의 기록이다
        long submitted = report(work, "SUCCEEDED");
        jdbc.update("UPDATE report SET submitted_at = now(), submitted_by = ? WHERE report_id = ?", adminUserId(), submitted);
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_REPORT_SUBMITTED"));
        jdbc.update("UPDATE report SET submitted_at = NULL, submitted_by = NULL WHERE report_id = ?", submitted);

        // 지난 날짜 작업은 바꾸지 않는다
        jdbc.update("UPDATE work_unit SET work_date = work_date - 1 WHERE work_id = ?", work);
        mvc.perform(post("/api/work-units/{id}/reopen", work).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_NOT_TODAY"));
        assertThat(jdbc.queryForObject("SELECT status FROM work_unit WHERE work_id = ?", String.class, work)).isEqualTo("DONE");
    }

    @Test
    void 시작과_종료는_관리자만_가능하고_없는_작업은_404다() throws Exception {
        long work = work(zone);
        String viewer = token("viewer", Role.VIEWER);
        for (String action : List.of("start", "end", "reopen")) {
            mvc.perform(post("/api/work-units/{id}/" + action, work)).andExpect(status().isUnauthorized());
            mvc.perform(post("/api/work-units/{id}/" + action, work).header(HttpHeaders.AUTHORIZATION, viewer))
                    .andExpect(status().isForbidden());
            mvc.perform(post("/api/work-units/{id}/" + action, 999_999_999L).header(HttpHeaders.AUTHORIZATION, admin))
                    .andExpect(status().isNotFound());
        }
    }

    @Test
    void 서로_다른_작업을_동시에_시작해도_하나만_진행된다() throws Exception {
        long first = work(zone);
        long second = work(outside);
        CountDownLatch go = new CountDownLatch(1);
        try (ExecutorService pool = Executors.newVirtualThreadPerTaskExecutor()) {
            var a = pool.submit(() -> concurrentStart(first, go));
            var b = pool.submit(() -> concurrentStart(second, go));
            go.countDown();
            MvcResult ra = a.get(5, TimeUnit.SECONDS);
            MvcResult rb = b.get(5, TimeUnit.SECONDS);
            assertThat(List.of(ra.getResponse().getStatus(), rb.getResponse().getStatus())).containsExactlyInAnyOrder(200, 409);
            MvcResult rejected = ra.getResponse().getStatus() == 409 ? ra : rb;
            long winner = ra.getResponse().getStatus() == 200 ? first : second;
            ObjectNode error = (ObjectNode) MAPPER.readTree(rejected.getResponse().getContentAsString());
            assertThat(error.path("code").asString()).isEqualTo("WORK_ALREADY_IN_PROGRESS");
            assertThat(error.path("work_id").asString()).isEqualTo(String.valueOf(winner));
            assertThat(jdbc.queryForObject("SELECT count(*) FROM work_unit WHERE status = 'IN_PROGRESS'", Long.class)).isEqualTo(1);
        }
    }

    @Test
    void 종료는_LIVE_사실로_새버전을_만들고_기존_본문과_잠금을_보존한다() throws Exception {
        long work = work(zone);
        long base = report(work, "SUCCEEDED");
        jdbc.update("UPDATE report SET locked_at = ?, content_sha256 = ? WHERE report_id = ?",
                Timestamp.from(started), "a".repeat(64), base);
        start(work);
        UUID critical = event("LIVE", "R1", "CRITICAL", zone, started);
        event("LIVE", "R2", "HIGH", zone, started.plusSeconds(10));
        event("LIVE", "R1", "HIGH", zone, started.plusSeconds(20));
        event("LIVE", "R1", "HIGH", outside, started.plusSeconds(5));
        event("LIVE", "R3", "HIGH", outside, started.plusSeconds(6));
        event("REPLAY", "R1", "CRITICAL", zone, started.plusSeconds(10));
        event("LIVE", "R1", "CRITICAL", zone, started.minusMillis(1));
        event("LIVE", "R1", "CRITICAL", zone, started.plusSeconds(20).plusMillis(1));
        for (int i = 0; i < 2; i++) {
            jdbc.update("""
                    INSERT INTO event_ack (ack_id, event_id, channel, actor_role, acked_at)
                    VALUES (?, ?, 'CONTROL_ROOM', 'ADMIN', ?)
                    """, UUID.randomUUID(), critical, Timestamp.from(started.plusSeconds(15)));
        }
        Long user = jdbc.queryForObject("SELECT user_id FROM app_user WHERE login_id = ?", Long.class, "admin-" + suffix);
        jdbc.update("""
                INSERT INTO event_close (close_id, idempotency_key, event_id, actor_user_id, requested_at, reason, result, decided_at)
                VALUES (?, ?, ?, ?, ?, '시험', 'ACCEPTED', ?)
                """, UUID.randomUUID(), "close-" + suffix, critical, user,
                Timestamp.from(started.plusSeconds(16)), Timestamp.from(started.plusSeconds(17)));
        blockEngine = true;
        clock.now.set(started.plusSeconds(20));
        ObjectNode ended = end(work);
        assertThat(ended.path("wrap_up").path("status").asString()).isEqualTo("GENERATING");
        long next = ended.path("wrap_up").path("report_id").asLong();
        assertThat(next).isNotEqualTo(base);
        assertThat(engineEntered.await(2, TimeUnit.SECONDS)).isTrue();
        ObjectNode pending = getReport(next);
        assertThat(pending.path("report_version").asInt()).isEqualTo(2);
        assertThat(pending.path("wrap_up_status").asString()).isEqualTo("GENERATING");
        assertThat(pending.path("locked_at").isNull()).isTrue();
        assertThat(pending.path("content_sha256").isNull()).isTrue();
        assertThat(pending.path("signature_count").asInt()).isZero();
        ObjectNode request = (ObjectNode) MAPPER.readTree(requestBody.get());
        assertThat(request.propertyNames()).containsExactlyInAnyOrder("work_type", "window_ms", "facts");
        assertThat(request.path("window_ms").get(0).asLong()).isEqualTo(started.toEpochMilli());
        assertThat(request.path("window_ms").get(1).asLong()).isEqualTo(started.plusSeconds(20).toEpochMilli());
        assertThat(request.path("facts").path("event_count").asInt()).isEqualTo(3);
        assertThat(request.path("facts").path("severity_counts").path("CRITICAL").asInt()).isEqualTo(1);
        assertThat(request.path("facts").path("severity_counts").path("HIGH").asInt()).isEqualTo(2);
        assertThat(request.path("facts").path("rule_counts").path("R1").asInt()).isEqualTo(2);
        assertThat(request.path("facts").path("rule_counts").path("R2").asInt()).isEqualTo(1);
        assertThat(request.path("facts").path("max_severity").asString()).isEqualTo("CRITICAL");
        assertThat(request.path("facts").path("operator_ack_count").asInt()).isEqualTo(1);
        assertThat(request.path("facts").path("operator_close_count").asInt()).isEqualTo(1);
        assertThat(request.path("facts").path("outside_work_entry_count").asInt()).isEqualTo(1);
        assertThat(requestBody.get()).doesNotContain("개인정보", zone, outside, "coordinates", "world_ids", "sections");
        releaseEngine.countDown();
        ObjectNode complete = awaitReport(next);
        assertThat(complete.path("wrap_up_summary").asString()).isEqualTo("집계 사실에 따른 마무리 문단");
        assertThat(complete.path("wrap_up_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(complete.path("sections")).isEqualTo(getReport(base).path("sections"));
        assertThat(getReport(base).path("locked_at").asLong()).isEqualTo(started.toEpochMilli());
        assertThat(getReport(base).path("wrap_up_summary").isNull()).isTrue();
    }

    @Test
    void 날짜_작업은_새버전_없이_같은_행의_빈_종료_미팅만_채운다() throws Exception {
        long work = work(zone);
        long report = reports.createDailyBlank(work, adminUserId());
        start(work);
        event("LIVE", "R1", "CRITICAL", zone, started.plusSeconds(5));
        clock.now.set(started.plusSeconds(20));
        ObjectNode ended = end(work);
        assertThat(ended.path("wrap_up").path("mode").asString()).isEqualTo("FORM_ENDING");
        assertThat(ended.path("wrap_up").path("status").asString()).isEqualTo("GENERATING");
        assertThat(ended.path("wrap_up").path("report_id").asLong()).isEqualTo(report);
        ObjectNode done = awaitWrapUp(report);
        assertThat(done.path("wrap_up_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("form").path("review").path("ending").asString()).startsWith("엔진이 쓴 종료 미팅 문단");
        assertThat(done.path("form_revision").asInt()).isEqualTo(1);
        assertThat(done.path("report_version").asInt()).isEqualTo(1);
        assertThat(done.path("status").asString()).isEqualTo("PENDING");
        // 서명 해시가 덮는 wrap_up_summary 는 날짜 작업에서 쓰지 않는다
        assertThat(done.path("wrap_up_summary").isNull()).isTrue();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id = ?", Integer.class, work)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT wrap_up_model FROM report WHERE report_id = ?", String.class, report)).isEqualTo("fake-llm");
        String raw = endingBody.get();
        assertThat(schemas.violations("messages/tbm_form_ending_request", raw)).isEmpty();
        ObjectNode request = (ObjectNode) MAPPER.readTree(raw);
        assertThat(request.propertyNames()).containsExactlyInAnyOrder("request_id", "window_ms", "facts");
        assertThat(request.path("request_id").asString()).isEqualTo("rpt-" + report + "-end");
        assertThat(request.path("facts").path("event_count").asInt()).isEqualTo(1);
        assertThat(raw).doesNotContain("개인정보", zone, "admin-");
    }

    @Test
    void 날짜_작업의_종료_미팅이_이미_있으면_두고_엔진이_실패하면_결정적_문장을_기록한다() throws Exception {
        long work = work(zone);
        long report = reports.createDailyBlank(work, adminUserId());
        ObjectNode form = (ObjectNode) getReport(report).path("form").deepCopy();
        ((ObjectNode) form.path("review")).put("ending", "사람이 쓴 종료 미팅");
        reports.updateForm(report, form, 0);
        start(work);
        engineStatus = 500;
        clock.now.set(started.plusSeconds(20));
        assertThat(end(work).path("wrap_up").path("report_id").asLong()).isEqualTo(report);
        ObjectNode done = awaitWrapUp(report);
        assertThat(done.path("wrap_up_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("form").path("review").path("ending").asString()).isEqualTo("사람이 쓴 종료 미팅");
        assertThat(done.path("form_revision").asInt()).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT wrap_up_fallback FROM report WHERE report_id = ?", Boolean.class, report)).isTrue();
        assertThat(jdbc.queryForObject("SELECT wrap_up_model FROM report WHERE report_id = ?", String.class, report))
                .isEqualTo("deterministic");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id = ?", Integer.class, work)).isEqualTo(1);
    }

    @Test
    void 생성중_보고서는_건너뛰고_FAILED만_있으면_NO_REPORT다() throws Exception {
        for (String state : List.of("GENERATING", "FAILED")) {
            long work = work(zone);
            long report = report(work, state);
            start(work);
            ObjectNode ended = end(work);
            assertThat(ended.path("work_unit").path("status").asString()).isEqualTo("DONE");
            assertThat(ended.path("wrap_up").path("status").asString()).isEqualTo("SKIPPED");
            if ("GENERATING".equals(state)) {
                assertThat(ended.path("wrap_up").path("report_id").asLong()).isEqualTo(report);
                assertThat(ended.path("wrap_up").path("reason").asString()).isEqualTo("REPORT_GENERATING");
            } else {
                assertThat(ended.path("wrap_up").path("report_id").isNull()).isTrue();
                assertThat(ended.path("wrap_up").path("reason").asString()).isEqualTo("NO_REPORT");
            }
            assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id = ?", Long.class, work)).isEqualTo(1);
        }
        assertThat(requestBody.get()).isNull();
    }

    @Test
    void 구역이_없는_작업은_현장전체를_세고_엔진실패에도_결정론_요약을_저장한다() throws Exception {
        long work = work(null);
        report(work, "SUCCEEDED");
        start(work);
        event("LIVE", "R1", "HIGH", zone, started);
        event("LIVE", "R1", "CRITICAL", outside, started);
        engineStatus = 503;
        long next = end(work).path("wrap_up").path("report_id").asLong();
        ObjectNode complete = awaitReport(next);
        assertThat(complete.path("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(complete.path("wrap_up_summary").asString()).contains("경보: 총 2건").doesNotContain("작업 외 진입 0건");
        assertThat(jdbc.queryForObject("SELECT wrap_up_fallback FROM report WHERE report_id = ?", Boolean.class, next)).isTrue();
    }

    @Test
    void 엔진_타임아웃도_결정론_요약으로_마감하고_보고서_목록에_마무리가_나온다() throws Exception {
        long work = work(zone);
        report(work, "SUCCEEDED");
        start(work);
        blockEngine = true;
        reports.configureRiskEngine("http://127.0.0.1:" + engine.getAddress().getPort(), Duration.ofMillis(100));
        long next = end(work).path("wrap_up").path("report_id").asLong();
        ObjectNode completed = awaitReport(next);
        assertThat(completed.path("wrap_up_summary").asString()).contains("경보: 없음");
        assertThat(completed.path("wrap_up_status").asString()).isEqualTo("SUCCEEDED");
        mvc.perform(get("/api/reports/tbm").param("work_id", String.valueOf(work)).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$.latest.report_id").value(next))
                .andExpect(jsonPath("$.latest.wrap_up_status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.latest.signature_count").value(0))
                .andExpect(jsonPath("$.versions[0].wrap_up_status").value("SUCCEEDED"));
        releaseEngine.countDown();
    }

    @Test
    void 작업_배치가_구역과_묶음에_반영되고_수정과_종료에_따라_갱신된다() throws Exception {
        String items = """
                [{"item_id":"open","kind":"OPENING","center":[2,3,0.05],"size":[2,1,0.1],"yaw_rad":0},
                 {"item_id":"surface","kind":"WORK_SURFACE","center":[5,3,0.5],"size":[2,1,1],"yaw_rad":0}]
                """;
        String body = "{\"name\":\"배치-" + suffix + "\",\"items\":" + items + "}";
        String created = mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isCreated())
                .andReturn().getResponse().getContentAsString();
        long layout = MAPPER.readTree(created).path("layout_id").asLong();
        long work = work(zone);
        jdbc.update("UPDATE work_unit SET layout_id = ? WHERE work_id = ?", layout, work);
        start(work);
        String generated = jdbc.queryForObject("SELECT zone_id FROM zone WHERE work_id = ? AND layout_item_id = 'open' AND valid_to IS NULL", String.class, work);
        String surface = jdbc.queryForObject("SELECT object_id FROM static_object WHERE work_id = ? AND layout_item_id = 'surface' AND valid_to IS NULL", String.class, work);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle_item WHERE item_id = ?", Integer.class, generated)).isPositive();
        mvc.perform(get("/api/zones/{id}/versions", generated).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andExpect(jsonPath("$[0].layout_id").value(layout))
                .andExpect(jsonPath("$[0].layout_item_id").value("open"));
        mvc.perform(delete("/api/zones/{id}", generated).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LAYOUT_ZONE_LOCKED"));
        mvc.perform(delete("/api/static-objects/{id}", surface).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LAYOUT_ZONE_LOCKED"));
        clock.now.set(started.plusSeconds(1));
        mvc.perform(put("/api/layouts/{id}", layout).header(HttpHeaders.AUTHORIZATION, admin)
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT max(zone_version) FROM zone WHERE zone_id = ?", Integer.class, generated)).isEqualTo(1);
        String changed = "[{\"item_id\":\"open\",\"kind\":\"OPENING\",\"center\":[4,3,0.05],\"size\":[2,1,0.1],\"yaw_rad\":0}]";
        mvc.perform(put("/api/layouts/{id}", layout).header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content("{\"name\":\"배치-" + suffix + "\",\"items\":" + changed + "}"))
                .andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT max(zone_version) FROM zone WHERE zone_id = ?", Integer.class, generated)).isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM static_object WHERE object_id = ? AND valid_to IS NULL", Integer.class, surface)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE work_id = ? AND layout_item_id = 'surface' AND valid_to IS NULL", Integer.class, work)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle_item WHERE item_id = ? AND item_version = 2", Integer.class, generated)).isPositive();
        clock.now.set(started.plusSeconds(2));
        end(work);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE work_id = ? AND valid_to IS NULL", Integer.class, work)).isZero();
        mvc.perform(delete("/api/zones/{id}", generated).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LAYOUT_ZONE_LOCKED"));
    }

    private MvcResult concurrentStart(long work, CountDownLatch go) throws Exception {
        if (!go.await(2, TimeUnit.SECONDS)) throw new AssertionError("start latch timeout");
        return mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin)).andReturn();
    }

    private void start(long work) throws Exception {
        mvc.perform(post("/api/work-units/{id}/start", work).header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isOk());
    }

    private ObjectNode end(long work) throws Exception {
        return (ObjectNode) MAPPER.readTree(mvc.perform(post("/api/work-units/{id}/end", work)
                .header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isAccepted())
                .andReturn().getResponse().getContentAsString());
    }

    private ObjectNode getReport(long report) throws Exception {
        return (ObjectNode) MAPPER.readTree(mvc.perform(get("/api/reports/tbm/{id}", report)
                .header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isOk())
                .andReturn().getResponse().getContentAsString());
    }

    private ObjectNode awaitReport(long report) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < deadline) {
            ObjectNode node = getReport(report);
            if (!"GENERATING".equals(node.path("status").asString())) return node;
            Thread.sleep(25);
        }
        throw new AssertionError("wrap-up did not finish");
    }

    private ObjectNode awaitWrapUp(long report) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < deadline) {
            ObjectNode node = getReport(report);
            if (!"GENERATING".equals(node.path("wrap_up_status").asString())) return node;
            Thread.sleep(25);
        }
        throw new AssertionError("form ending did not finish");
    }

    private long adminUserId() {
        return jdbc.queryForObject("SELECT user_id FROM app_user WHERE login_id = ?", Long.class, "admin-" + suffix);
    }

    private long work(String targetZone) throws Exception {
        ObjectNode body = MAPPER.createObjectNode();
        body.put("date", started.atOffset(ZoneOffset.UTC).toLocalDate().toString());
        body.put("name", "개인정보작업-" + suffix);
        body.put("from_ms", started.minusSeconds(3600).toEpochMilli());
        body.put("to_ms", started.plusSeconds(3600).toEpochMilli());
        body.put("planned_people", 2).put("work_type", "OPENING_WORK");
        if (targetZone != null) body.put("zone_id", targetZone);
        String response = mvc.perform(post("/api/work-units").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content(MAPPER.writeValueAsString(body)))
                .andExpect(status().isCreated()).andExpect(jsonPath("$.status").value("PLANNED"))
                .andExpect(jsonPath("$.actual_started_at").isEmpty()).andReturn().getResponse().getContentAsString();
        long id = Long.parseLong(MAPPER.readTree(response).path("work_id").asString());
        // 작업 시작은 세팅 완료 뒤다(-642). 이 시험들은 세팅 뒤의 시작·종료를 보므로 세팅을 마친 작업으로 둔다
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", id);
        return id;
    }

    /** 작업 날짜를 현장 날짜(Asia/Seoul) 오늘로 둔 작업. 되돌리기는 오늘 작업만 받는다(work() 는 UTC 날짜라 자정 근처에 다르다) */
    private long todayWork(String targetZone) throws Exception {
        long id = work(targetZone);
        jdbc.update("UPDATE work_unit SET work_date = ? WHERE work_id = ?",
                java.sql.Date.valueOf(started.atZone(WorkUnitRepository.SITE_ZONE).toLocalDate()), id);
        return id;
    }

    private long report(long work, String state) {
        return jdbc.queryForObject("""
                INSERT INTO report (work_id, report_kind, report_version, status, work_type, sections, citations, created_at)
                VALUES (?, 'TBM', 1, ?, 'OPENING_WORK', '[{"key":"hazards","body":"기존 본문"}]'::jsonb,
                    '[]'::jsonb, ?) RETURNING report_id
                """, Long.class, work, state, Timestamp.from(started));
    }

    private UUID event(String source, String rule, String severity, String zoneId, Instant at) {
        UUID id = UUID.randomUUID();
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification,
                    severity, confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence,
                    raised_at, updated_at, delivery, retention_hold)
                VALUES (?, ?, ?, ?, 1, 'RAISED', 'VERIFIED', ?, 'HIGH', false, '{w-1}', ?, '{}', 0, 1, ?, ?, 'REALTIME', false)
                """, id, source, gateway, rule, severity, zoneId, Timestamp.from(at), Timestamp.from(at));
        return id;
    }

    private String token(String name, Role role) throws Exception {
        String login = name + "-" + suffix;
        users.saveAndFlush(new AppUser(login, passwords.encode(PASSWORD), role, null, Instant.now()));
        String response = mvc.perform(post("/api/auth/login").contentType(MediaType.APPLICATION_JSON)
                        .content("{\"username\":\"" + login + "\",\"password\":\"" + PASSWORD + "\"}"))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString();
        return "Bearer " + MAPPER.readTree(response).path("accessToken").asString();
    }

    @TestConfiguration(proxyBeanMethods = false)
    static class ClockConfiguration {
        @Bean @Primary SessionClock sessionClock() { return new SessionClock(); }
    }

    static class SessionClock extends Clock {
        final AtomicReference<Instant> now = new AtomicReference<>(Instant.now());
        @Override public ZoneId getZone() { return ZoneOffset.UTC; }
        @Override public Clock withZone(ZoneId zone) { return Clock.fixed(now.get(), zone); }
        @Override public Instant instant() { return now.get(); }
    }
}
