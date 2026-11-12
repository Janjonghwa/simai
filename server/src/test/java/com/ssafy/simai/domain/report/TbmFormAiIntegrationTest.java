package com.ssafy.simai.domain.report;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.user.service.UserService;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
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
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 날짜 작업 TBM 양식 AI 초안(B9)·종료 미팅 복구(B10) 서버 연결(S15P31A101-616 2단계). 가짜 Risk Engine 으로 돈다. */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class TbmFormAiIntegrationTest {

    private static final ZoneId SEOUL = ZoneId.of("Asia/Seoul");
    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired JwtEncoder encoder;
    @Autowired ObjectMapper mapper;
    @Autowired TbmReportService reports;
    @Autowired TbmFormAiService formAi;
    @Autowired UserService userService;
    @Autowired ContractSchemas schemas;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final UUID session = UUID.randomUUID();
    private final String zone = "z-form-" + tag;
    private final String job = "job-form-" + tag;
    private final String cal = "cal-form-" + tag;
    private final String bundle = "cfg-form-" + tag;
    private final AtomicReference<String> draftRequest = new AtomicReference<>();
    private final CountDownLatch engineEntered = new CountDownLatch(1);
    private final CountDownLatch releaseEngine = new CountDownLatch(1);
    private volatile boolean blockEngine;
    private volatile int engineStatus = 200;
    /** null 이면 계약 예시 tbm_form_draft_result.json 을 request_id 만 바꿔 돌려준다. */
    private volatile String responseExample;
    private volatile java.util.function.UnaryOperator<ObjectNode> tamper = r -> r;
    private HttpServer engine;
    private ExecutorService engineThreads;
    private String site;
    private long adminId;
    private String admin;
    private String viewer;

    @BeforeEach
    void setUp() throws IOException {
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        adminId = user("form-admin-" + tag, "ADMIN");
        admin = token(adminId, "ADMIN");
        viewer = token(user("form-viewer-" + tag, "VIEWER"), "VIEWER");
        userService.setProfile("form-admin-" + tag, "곽나혁", "시마이건설", "안전관리자");
        responseExample = "tbm_form_draft_result.json";
        engine = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        engineThreads = Executors.newVirtualThreadPerTaskExecutor();
        engine.setExecutor(engineThreads);
        engine.createContext(TbmFormAiService.DRAFT_PATH, this::handleDraft);
        engine.start();
        reports.configureRiskEngine("http://127.0.0.1:" + engine.getAddress().getPort(), Duration.ofSeconds(3));
    }

    @AfterEach
    void cleanUp() {
        releaseEngine.countDown();
        if (engine != null) engine.stop(0);
        if (engineThreads != null) engineThreads.close();
        String works = "SELECT work_id FROM work_unit WHERE created_by = " + adminId;
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE work_id IN (" + works + ")");
        jdbc.update("DELETE FROM report WHERE work_id IN (" + works + ")");
        jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", bundle);
        jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", bundle);
        jdbc.update("DELETE FROM setup_session_zone WHERE setup_session_id = ?", session);
        jdbc.update("DELETE FROM setup_session WHERE setup_session_id = ?", session);
        jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", job);
        jdbc.update("DELETE FROM zone WHERE zone_id LIKE ?", zone + "%");
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", cal);
        jdbc.update("DELETE FROM work_unit WHERE created_by = ?", adminId);
        jdbc.update("DELETE FROM layout_plan_revision WHERE layout_id IN (SELECT layout_id FROM layout_plan WHERE name = ?)", "form-layout-" + tag);
        jdbc.update("DELETE FROM layout_plan WHERE name = ?", "form-layout-" + tag);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE ?", "%-" + tag);
    }

    @Test
    void 빈_칸만_채우고_revision_을_올리며_요청에_리더와_신원이_없다() throws Exception {
        long workId = work();
        withSetupZones(workId);
        long reportId = reports.createDailyBlank(workId, adminId);
        ObjectNode form = blankWithTasks(reportId);
        ((ObjectNode) form.path("checks").get(0)).put("yes", true);
        reports.updateForm(reportId, form, 0);

        JsonNode accepted = body(draft(admin, reportId).andExpect(status().isAccepted()));
        assertThat(accepted.path("draft_status").asString()).isEqualTo("GENERATING");
        assertThat(accepted.path("form_revision").asInt()).isEqualTo(1);
        JsonNode done = awaitDraft(reportId);

        assertThat(done.path("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("draft_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("form_revision").asInt()).isEqualTo(2);
        assertThat(done.path("report_version").asInt()).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id=?", Integer.class, workId)).isEqualTo(1);
        JsonNode expected = example("tbm_form_draft_result.json").path("form_draft");
        JsonNode f = done.path("form");
        assertThat(f.path("info").path("task_name").asString()).isEqualTo(expected.path("task_name").asString());
        assertThat(f.path("info").path("place").asString()).isEqualTo(expected.path("place").asString());
        assertThat(f.path("info").path("risk_assessed").asBoolean()).isTrue();
        assertThat(f.path("info").path("tasks")).isEqualTo(form.path("info").path("tasks"));
        assertThat(f.path("risks").get(2)).isEqualTo(expected.path("risks").get(2));
        assertThat(f.path("focus")).isEqualTo(expected.path("focus"));
        // 사람이 고른 예·아니오는 그대로, 점검 문구는 서버 값, 조치는 초안
        assertThat(f.path("checks").get(0).path("yes").asBoolean()).isTrue();
        assertThat(f.path("checks").get(1).path("yes").isNull()).isTrue();
        assertThat(f.path("checks").get(0).path("label").asString()).isEqualTo("안전모 등 보호구 착용 상태 확인");
        assertThat(f.path("checks").get(3).path("action").asString())
                .isEqualTo(expected.path("checks").get(3).path("action").asString());
        assertThat(f.path("review").path("daily").asString()).isEqualTo(expected.path("review").path("daily").asString());
        assertThat(f.path("review").path("ending").asString()).isEmpty();
        assertThat(done.path("llm_model").asString()).isEqualTo("gpt-5-mini");
        assertThat(done.path("citations").size()).isEqualTo(1);
        assertThat(done.path("steps").size()).isEqualTo(3);
        assertThat(done.path("leader").path("user_id").asLong()).isEqualTo(adminId);

        String raw = draftRequest.get();
        assertThat(schemas.violations(TbmFormAiService.DRAFT_REQUEST_SCHEMA, raw)).isEmpty();
        JsonNode req = mapper.readTree(raw);
        assertThat(req.propertyNames()).containsExactlyInAnyOrder("request_id", "report_date", "tasks", "rule_tags", "zones", "existing");
        assertThat(req.path("request_id").asString()).isEqualTo("rpt-" + reportId + "-r1");
        assertThat(req.path("report_date").asString()).isEqualTo(LocalDate.now(SEOUL).toString());
        assertThat(req.path("tasks").get(0).path("label").asString()).isEqualTo("슬래브 개구부 덮개 설치");
        assertThat(req.path("tasks").get(1).path("text").asString()).isEqualTo("자재 정리");
        assertThat(req.path("rule_tags").toString()).isEqualTo("[\"R1\",\"R2\",\"R5\"]");
        // 세팅에서 발행한 현재 구역만. 발행 안 한 구역·지난 버전은 빠진다.
        assertThat(req.path("zones").toString()).isEqualTo("[{\"name\":\"개구부 1\",\"zone_kind\":\"OPENING\",\"rule_id\":null}]");
        assertThat(req.path("existing").path("checks").get(0).path("yes").asBoolean()).isTrue();
        assertThat(raw).doesNotContain("곽나혁", "시마이건설", "안전관리자", "form-admin", "leader", "개인정보작업",
                "user_id", zone, "polygon");
    }

    @Test
    void 두_트랜잭션_사이에_사람이_고친_칸은_덮지_않는다() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        reports.updateForm(reportId, blankWithTasks(reportId), 0);
        blockEngine = true;
        draft(admin, reportId).andExpect(status().isAccepted());
        assertThat(engineEntered.await(3, TimeUnit.SECONDS)).isTrue();

        // 초안 요청이 나간 뒤 사람이 고친다(PUT form 은 GENERATING 중에도 된다)
        ObjectNode edited = (ObjectNode) reports.getOne(reportId).path("form").deepCopy();
        ((ObjectNode) edited.path("info")).put("task_name", "사람이 쓴 작업명");
        ((ObjectNode) edited.path("risks").get(0)).put("risk", "사람이 쓴 위험");
        ((ObjectNode) edited.path("checks").get(1)).put("action", "사람이 쓴 조치").put("yes", false);
        reports.updateForm(reportId, edited, 1);
        releaseEngine.countDown();
        JsonNode done = awaitDraft(reportId);

        JsonNode expected = example("tbm_form_draft_result.json").path("form_draft");
        JsonNode f = done.path("form");
        assertThat(done.path("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("form_revision").asInt()).isEqualTo(3);
        assertThat(f.path("info").path("task_name").asString()).isEqualTo("사람이 쓴 작업명");
        assertThat(f.path("info").path("place").asString()).isEqualTo(expected.path("place").asString());
        assertThat(f.path("risks").get(0).path("risk").asString()).isEqualTo("사람이 쓴 위험");
        // AI 는 사람이 새로 쓴 위험을 보지 못했으므로 그 줄의 대책은 비워 둔다
        assertThat(f.path("risks").get(0).path("measure").asString()).isEmpty();
        assertThat(f.path("risks").get(1)).isEqualTo(expected.path("risks").get(1));
        assertThat(f.path("checks").get(1).path("action").asString()).isEqualTo("사람이 쓴 조치");
        assertThat(f.path("checks").get(1).path("yes").asBoolean()).isFalse();
        assertThat(f.path("checks").get(2).path("action").asString())
                .isEqualTo(expected.path("checks").get(2).path("action").asString());
    }

    @Test
    void 엔진_실패와_계약_위반은_FAILED_이고_양식을_건드리지_않으며_다시_요청할_수_있다() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        reports.updateForm(reportId, blankWithTasks(reportId), 0);
        JsonNode before = reports.getOne(reportId).path("form");

        engineStatus = 500;
        draft(admin, reportId).andExpect(status().isAccepted());
        JsonNode failed = awaitDraft(reportId);
        assertThat(failed.path("draft_status").asString()).isEqualTo("FAILED");
        assertThat(failed.path("failure_reason").asString()).isEqualTo("ENGINE_UNAVAILABLE");
        assertThat(failed.path("form")).isEqualTo(before);
        assertThat(failed.path("form_revision").asInt()).isEqualTo(1);

        // 점검 yes 를 채운 초안은 계약(const null) 위반이다
        engineStatus = 200;
        tamper = r -> {
            ((ObjectNode) r.path("form_draft").path("checks").get(0)).put("yes", true);
            return r;
        };
        draft(admin, reportId).andExpect(status().isAccepted());
        JsonNode violated = awaitDraft(reportId);
        assertThat(violated.path("failure_reason").asString()).isEqualTo("ENGINE_CONTRACT_VIOLATION");
        assertThat(violated.path("form")).isEqualTo(before);

        tamper = r -> r.put("request_id", "rpt-other");
        draft(admin, reportId).andExpect(status().isAccepted());
        assertThat(awaitDraft(reportId).path("failure_reason").asString()).isEqualTo("ENGINE_CONTRACT_VIOLATION");

        tamper = r -> r;
        draft(admin, reportId).andExpect(status().isAccepted());
        JsonNode ok = awaitDraft(reportId);
        assertThat(ok.path("draft_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(ok.path("failure_reason").isNull()).isTrue();
        assertThat(ok.path("form_revision").asInt()).isEqualTo(2);
        // 이미 SUCCEEDED 여도 다시 초안을 받을 수 있다. 채울 빈 칸이 없으면 revision 은 그대로다.
        draft(admin, reportId).andExpect(status().isAccepted());
        JsonNode again = awaitDraft(reportId);
        assertThat(again.path("draft_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(again.path("form_revision").asInt()).isEqualTo(2);
    }

    @Test
    void 대체_초안도_SUCCEEDED_로_저장하고_steps_에_대체_사유를_남긴다() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        reports.updateForm(reportId, blankWithTasks(reportId), 0);
        responseExample = "tbm_form_draft_result.fallback.json";
        draft(admin, reportId).andExpect(status().isAccepted());
        JsonNode done = awaitDraft(reportId);
        assertThat(done.path("draft_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(done.path("form_revision").asInt()).isEqualTo(2);
        assertThat(done.path("citations").size()).isZero();
        assertThat(done.path("steps").toString()).contains("FALLBACK", "LLM_TIMEOUT");
        assertThat(done.path("form").path("review").path("daily").asString())
                .isEqualTo(example("tbm_form_draft_result.fallback.json").path("form_draft").path("review").path("daily").asString());
    }

    @Test
    void 시작할_수_없는_상태는_409_권한이_없으면_403() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        draft(admin, reportId).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_FORM_NO_TASKS"));
        assertThat(formAi.tryStartDraftForWork(workId)).isEmpty();
        assertThat(reports.getOne(reportId).path("status").asString()).isEqualTo("PENDING");

        reports.updateForm(reportId, blankWithTasks(reportId), 0);
        draft(viewer, reportId).andExpect(status().isForbidden());

        blockEngine = true;
        // 세팅 완료(-615)가 부르는 작업 id 경로
        assertThat(formAi.startDraftForWork(workId).path("report_id").asLong()).isEqualTo(reportId);
        assertThat(engineEntered.await(3, TimeUnit.SECONDS)).isTrue();
        draft(admin, reportId).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REPORT_IN_PROGRESS"))
                .andExpect(jsonPath("$.report_id").value(reportId));
        releaseEngine.countDown();
        awaitDraft(reportId);

        jdbc.update("UPDATE report SET locked_at = now(), content_sha256 = 'x' WHERE report_id = ?", reportId);
        draft(admin, reportId).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_LOCKED"));

        long oldWork = work();
        long oldReport = jdbc.queryForObject("""
                INSERT INTO report(report_kind, work_id, report_version, status) VALUES ('TBM', ?, 1, 'SUCCEEDED') RETURNING report_id
                """, Long.class, oldWork);
        draft(admin, oldReport).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_NO_FORM"));
        draft(admin, 999_999_999L).andExpect(status().isNotFound());
    }

    @Test
    void 날짜_작업_보고서는_예전_새버전_생성_경로를_409로_거절한다() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        // 세팅 구역을 배치에 반영하면 날짜 작업에도 layout_id 가 생긴다(SetupZoneLayoutService)
        long layout = body(mvc.perform(post("/api/layouts").header(HttpHeaders.AUTHORIZATION, admin)
                .contentType(MediaType.APPLICATION_JSON).content("""
                        {"name":"form-layout-%s","items":[{"item_id":"open-1","kind":"OPENING","center":[1.0,2.0,0.0],
                          "size":[1.2,1.2,0.2],"yaw_rad":0.0,"label":"개구부 1"}]}
                        """.formatted(tag))).andExpect(status().isCreated())).path("layout_id").asLong();
        jdbc.update("UPDATE work_unit SET layout_id = ? WHERE work_id = ?", layout, workId);

        mvc.perform(post("/api/reports/tbm").header(HttpHeaders.AUTHORIZATION, admin)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"work_id\":" + workId + "}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("REPORT_FORM_MANAGED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM report WHERE work_id=?", Integer.class, workId)).isEqualTo(1);
        assertThat(reports.getOne(reportId).path("status").asString()).isEqualTo("PENDING");
    }

    @Test
    void 재기동하면_초안은_INTERRUPTED_종료_미팅은_고정_사실의_결정적_문장으로_마감한다() throws Exception {
        long workId = work();
        long reportId = reports.createDailyBlank(workId, adminId);
        reports.updateForm(reportId, blankWithTasks(reportId), 0);
        JsonNode before = reports.getOne(reportId).path("form");
        String facts = mapper.writeValueAsString(example("tbm_form_ending_request.json").path("facts"));
        jdbc.update("UPDATE report SET status = 'GENERATING', wrap_up_status = 'GENERATING', wrap_up_facts = CAST(? AS jsonb) WHERE report_id = ?",
                facts, reportId);

        assertThat(formAi.recoverEndings()).isPositive();
        reports.recoverInterrupted();
        JsonNode r = reports.getOne(reportId);
        assertThat(r.path("status").asString()).isEqualTo("FAILED");
        assertThat(r.path("failure_reason").asString()).isEqualTo("INTERRUPTED");
        assertThat(r.path("wrap_up_status").asString()).isEqualTo("SUCCEEDED");
        assertThat(r.path("form").path("review").path("ending").asString()).startsWith("작업 종료 미팅. 경보: 총 3건").contains("많이 발생한 구역: 개구부 1 2건");
        assertThat(r.path("form_revision").asInt()).isEqualTo(2);
        assertThat(r.path("wrap_up_summary").isNull()).isTrue();
        assertThat(r.path("form").path("info")).isEqualTo(before.path("info"));
        assertThat(jdbc.queryForObject("SELECT wrap_up_fallback FROM report WHERE report_id = ?", Boolean.class, reportId)).isTrue();
        assertThat(jdbc.queryForObject("SELECT wrap_up_model FROM report WHERE report_id = ?", String.class, reportId))
                .isEqualTo("deterministic");
    }

    @Test
    void 결정적_종료_미팅은_2000자를_넘지_않는다() throws Exception {
        ObjectNode facts = (ObjectNode) example("tbm_form_ending_request.json").path("facts");
        ArrayNode zones = facts.putArray("top_zones");
        for (int i = 0; i < 200; i++) {
            zones.addObject().put("name", "가".repeat(40)).put("count", 1);
        }
        String text = TbmFormAiService.fallbackEnding(facts);
        assertThat(text.codePointCount(0, text.length())).isEqualTo(2000);
    }

    private void handleDraft(HttpExchange ex) throws IOException {
        try {
            String body = new String(ex.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
            draftRequest.set(body);
            engineEntered.countDown();
            if (blockEngine && !releaseEngine.await(5, TimeUnit.SECONDS)) {
                throw new IllegalStateException("engine latch timeout");
            }
            if (engineStatus != 200) {
                write(ex, engineStatus, "{\"error\":\"fail\"}");
                return;
            }
            ObjectNode res = (ObjectNode) example(responseExample);
            res.put("request_id", mapper.readTree(body).path("request_id").asString());
            write(ex, 200, mapper.writeValueAsString(tamper.apply(res)));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            ex.close();
        }
    }

    private static void write(HttpExchange ex, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        ex.getResponseHeaders().set("Content-Type", "application/json");
        ex.sendResponseHeaders(status, bytes.length);
        try (var os = ex.getResponseBody()) {
            os.write(bytes);
        }
    }

    private JsonNode example(String name) throws IOException {
        return mapper.readTree(Files.readString(CONTRACTS.resolve("examples").resolve(name)));
    }

    private ObjectNode blankWithTasks(long reportId) {
        ObjectNode form = (ObjectNode) reports.getOne(reportId).path("form").deepCopy();
        ArrayNode tasks = ((ObjectNode) form.path("info")).putArray("tasks");
        tasks.addObject().put("code", "SLAB_OPENING_COVER");
        tasks.addObject().put("text", "자재 정리");
        return form;
    }

    /** 작업의 현장 세팅 구역: 발행한 현재 구역 1개, 발행 안 한 구역 1개, 지난 버전 1개. */
    private void withSetupZones(long workId) {
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at) VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,now())", job);
        jdbc.update("""
                INSERT INTO setup_session(setup_session_id,site_id,work_unit_id,job_id,status,camera_heights_m,height_tolerance_cm,requested_by,started_at,updated_at)
                VALUES (?,?,?,?,'COMPLETED','{}'::jsonb,5,?,now(),now())
                """, session, site, workId, job, adminId);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ? WHERE work_id = ?", session, workId);
        String draftOnly = zone + "-unpublished";
        String old = zone + "-old";
        insertZone(zone, 1, "예전 이름", "now() - interval '1 day'", "now() - interval '1 hour'");
        insertZone(zone, 2, "개구부 1", "now() - interval '1 hour'", null);
        insertZone(draftOnly, 1, "발행 안 함", "now()", null);
        insertZone(old, 1, "지운 구역", "now() - interval '1 day'", "now() - interval '1 hour'");
        for (String id : new String[] {zone, draftOnly, old}) {
            jdbc.update("INSERT INTO setup_session_zone(setup_session_id,zone_id,zone_version,created_at) VALUES (?,?,1,now())", session, id);
        }
        jdbc.update("""
                INSERT INTO calibration_version(calibration_version,site_id,state,provider_source,reason,effective_at,calibration_ref,calibration_hash,created_at)
                VALUES (?,?,'PROVISIONAL','G1_AUTO','site_setup',now(),'s3://test/cal.json',?,now())
                """, cal, site, "sha256:" + "b".repeat(64));
        jdbc.update("""
                INSERT INTO config_bundle(config_version,site_id,issued_at,issued_by,bundle_uri,bundle_hash,components,calibration_version,rule_profile)
                VALUES (?,?,now(),?,'https://test/cfg',?,'{}'::jsonb,?,'OPERATION')
                """, bundle, site, adminId, "sha256:" + "c".repeat(64), cal);
        jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'ZONE',?,2)", bundle, zone);
        jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'ZONE',?,1)", bundle, old);
    }

    private void insertZone(String id, int version, String name, String from, String to) {
        jdbc.update("INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,z_min_m,z_max_m,severity,valid_from,valid_to) "
                + "VALUES (?,?,?,?,'OPENING','VOLUME_3D',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),0,2,'HIGH',"
                + from + "," + (to == null ? "NULL" : to) + ")", id, version, site, name);
    }

    private JsonNode awaitDraft(long reportId) throws Exception {
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(8);
        while (System.nanoTime() < deadline) {
            JsonNode r = body(mvc.perform(get("/api/reports/tbm/{id}", reportId).header(HttpHeaders.AUTHORIZATION, viewer))
                    .andExpect(status().isOk()));
            if (!"GENERATING".equals(r.path("status").asString())) {
                return r;
            }
            Thread.sleep(25);
        }
        throw new AssertionError("draft did not finish");
    }

    private ResultActions draft(String token, long reportId) throws Exception {
        return mvc.perform(post("/api/reports/tbm/{id}/form-draft", reportId).header(HttpHeaders.AUTHORIZATION, token)
                .contentType(MediaType.APPLICATION_JSON));
    }

    private long work() {
        return jdbc.queryForObject("""
                INSERT INTO work_unit(site_id, work_date, name, starts_at, ends_at, planned_people, created_by, created_at, updated_at)
                VALUES (?, ?, '개인정보작업-' || ?, now(), now() + interval '1 hour', 0, ?, now(), now())
                RETURNING work_id
                """, Long.class, site, LocalDate.now(SEOUL), tag, adminId);
    }

    private long user(String login, String role) {
        return jdbc.queryForObject("INSERT INTO app_user(login_id,password_hash,role,created_at) VALUES (?, 'x', ?, now()) RETURNING user_id",
                Long.class, login, role);
    }

    private String token(long id, String role) {
        Instant now = Instant.now();
        var claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(id)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return "Bearer " + encoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private JsonNode body(ResultActions actions) throws Exception {
        return mapper.readTree(actions.andReturn().getResponse().getContentAsString());
    }
}
