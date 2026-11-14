package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.gpu.provider.RunPodProvider;
import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
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
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 켜기·끄기·자동 정지·GPU 설정(S15P31A101-702). 실제 RunPod 를 부르지 않고 가짜 RunPod 서버(재고 목록·Pod 만들기·지우기·조회)를 띄운다.
 * 다른 시험이 남긴 진행 중 작업·세팅·열린 GPU 세션은 이 시험 트랜잭션 안에서만 닫는다(되돌림).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class GpuPowerIntegrationTest {

    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");
    private static final String KEY = "test-only-runpod-key";
    private static final String HBM3 = "NVIDIA H100 80GB HBM3";
    private static final String PCIE = "NVIDIA H100 PCIe";

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    AdminCommands commands;

    @Autowired
    RunPodProvider runpod;

    @Autowired
    GpuPowerService power;

    @Autowired
    GpuSessionService sessions;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private HttpServer fake;
    private final List<String> calls = new CopyOnWriteArrayList<>();
    private final List<JsonNode> created = new CopyOnWriteArrayList<>();
    private volatile String catalog;
    private volatile int createStatus = 201;
    private volatile String createBody;
    private volatile boolean podGone;
    private Long adminId;
    private String admin;
    private String bootId;

    @BeforeEach
    void setUp() throws Exception {
        fake = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        fake.createContext("/", this::handle);
        fake.start();
        String base = "http://127.0.0.1:" + fake.getAddress().getPort();
        runpod.useEndpoints(base + "/v1", base, KEY);
        power.useClock(Clock.systemUTC());
        catalog = catalog(true);
        createBody = "{\"id\":\"pod-" + suffix + "\",\"costPerHr\":2.99,\"desiredStatus\":\"RUNNING\"}";

        // 다른 시험이 남긴 상태를 이 트랜잭션 안에서만 닫는다
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = greatest(now(), actual_started_at) WHERE status = 'IN_PROGRESS'");
        jdbc.update("UPDATE setup_session SET status = 'FAILED' WHERE status NOT IN ('COMPLETED', 'FAILED')");
        jdbc.update("UPDATE setup_supplement_rebake SET status = 'CANCELLED' WHERE status IN ('QUEUED', 'RUNNING')");
        for (String open : jdbc.queryForList("""
                SELECT s.session_id::text FROM gpu_session s
                WHERE NOT EXISTS (SELECT 1 FROM gpu_session_event e WHERE e.session_id = s.session_id AND e.kind = 'ENDED')
                """, String.class)) {
            sessions.end(UUID.fromString(open), GpuSessionService.EndReason.SUPERSEDED);
        }
        for (String open : jdbc.queryForList("""
                SELECT l.launch_id::text FROM gpu_launch l WHERE NOT EXISTS (SELECT 1 FROM gpu_launch_event e
                    WHERE e.launch_id = l.launch_id AND e.kind IN ('CREATE_FAILED', 'STOPPED', 'GONE'))
                """, String.class)) {
            jdbc.update("INSERT INTO gpu_launch_event (launch_id, kind, at) VALUES (?::uuid, 'GONE', now())", open);
        }

        adminId = jdbc.queryForObject("INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now())"
                + " RETURNING user_id", Long.class, "gpu-admin-" + suffix);
        admin = "Bearer " + jwt(adminId, "ADMIN");
        String workerId = "worker-gpu-" + suffix;
        bootId = "gpu-boot-" + suffix;
        issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale,site_setup");
        issued("gpu-boot", "register", "--id", bootId, "--worker-id", workerId);
        saveSettings(null, 30);
    }

    @AfterEach
    void tearDown() {
        runpod.useEndpoints("https://rest.runpod.io/v1", "https://api.runpod.io", "");
        power.useClock(Clock.systemUTC());
        fake.stop(0);
    }

    @Test
    void 켜기는_선호_GPU_순서로_재고를_골라_템플릿과_비밀_참조로_Pod_를_만들고_요청을_기록한다() throws Exception {
        // 재고 표: HBM3 는 일본에 없고 루마니아에, PCIe 는 일본에 있다. 선호 GPU 순서가 지역보다 먼저다
        mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.default.gpu_type").value(HBM3))
                .andExpect(jsonPath("$.default.region").value("EU-RO-1"))
                .andExpect(jsonPath("$.regions[0].region").value("AP-JP-1"))
                .andExpect(jsonPath("$.regions[0].preferred").value(true))
                .andExpect(jsonPath("$.cells[0].gpu_type").value(HBM3))
                .andExpect(jsonPath("$.cells[0].region").value("AP-JP-1"))
                .andExpect(jsonPath("$.cells[0].stock").value("NONE"));

        JsonNode launch = launch(null, null).andExpect(status().isCreated()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8)
                .transform(this::read);
        assertThat(launch.get("state").stringValue()).isEqualTo("RUNNING");
        assertThat(launch.get("instance_id").stringValue()).isEqualTo("pod-" + suffix);
        assertThat(launch.get("auto_selected").booleanValue()).isTrue();

        JsonNode pod = created.getFirst();
        assertThat(pod.get("templateId").stringValue()).isEqualTo("tpl-simai");
        assertThat(pod.get("gpuTypeIds").get(0).stringValue()).isEqualTo(HBM3);
        assertThat(pod.get("dataCenterIds").get(0).stringValue()).isEqualTo("EU-RO-1");
        assertThat(pod.get("cloudType").stringValue()).isEqualTo("SECURE");
        assertThat(pod.get("interruptible").booleanValue()).isFalse();
        assertThat(pod.get("gpuCount").intValue()).isEqualTo(1);
        JsonNode env = pod.get("env");
        // 부팅 비밀값 원문은 서버에 없다: RunPod Secret 참조 글자만 넣는다
        assertThat(env.get("SIMAI_GPU_BOOT_SECRET").stringValue()).isEqualTo("{{ RUNPOD_SECRET_simai_gpu_boot_secret }}");
        assertThat(env.get("SIMAI_TIER0_TLS_PRIVATE_KEY").stringValue()).isEqualTo("{{ RUNPOD_SECRET_simai_tier0_tls_key }}");
        assertThat(env.get("SIMAI_GPU_BOOT_ID").stringValue()).isEqualTo("gpu-boot-runpod");
        assertThat(env.get("SIMAI_EC2_BASE_URL").stringValue()).isEqualTo("https://ec2.test");
        assertThat(env.get("SIMAI_PRICE_PER_HOUR_USD").stringValue()).isEqualTo("2.99");

        // 누가·언제·무엇을 켰는지 남는다
        assertThat(jdbc.queryForObject("SELECT requested_by FROM gpu_launch WHERE launch_id = ?::uuid", Long.class,
                launch.get("launch_id").stringValue())).isEqualTo(adminId);
        assertThat(jdbc.queryForObject("SELECT gpu_type || '/' || region || '/' || price_per_hour_usd FROM gpu_launch WHERE launch_id = ?::uuid",
                String.class, launch.get("launch_id").stringValue())).isEqualTo(HBM3 + "/EU-RO-1/2.9900");

        // 등록 전에는 켜는 중, 등록하면 준비 중, READY 면 연결
        current().andExpect(jsonPath("$.power").value("STARTING")).andExpect(jsonPath("$.launch.state").value("RUNNING"));
        UUID sessionId = register("pod-" + suffix);
        current().andExpect(jsonPath("$.power").value("PREPARING"));
        report(sessionId, "READY");
        current().andExpect(jsonPath("$.power").value("CONNECTED")).andExpect(jsonPath("$.stop_block").isEmpty())
                .andExpect(jsonPath("$.session.price_per_hour_usd").value(2.99));

        // 한 번에 GPU 하나
        launch(null, null).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_ALREADY_ON"));
        assertThat(created).hasSize(1);
    }

    @Test
    void 가격_상한을_넘는_칸은_고르지_않고_재고_없는_칸은_거부한다() throws Exception {
        saveSettings(2.5, 30);
        // HBM3(2.99)는 상한 밖이라 PCIe(2.39) 일본을 고른다
        mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.default.gpu_type").value(PCIE))
                .andExpect(jsonPath("$.default.region").value("AP-JP-1"));
        launch(HBM3, "EU-RO-1").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_PRICE_OVER_CAP"));
        launch(PCIE, "EU-RO-1").andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_OUT_OF_STOCK"));
        saveSettings(2.0, 30);
        launch(null, null).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_PRICE_OVER_CAP"));
        assertThat(created).isEmpty();

        // 공급자가 재고 없음으로 거절하면 실패를 남기고 다음 켜기를 막지 않는다
        saveSettings(null, 30);
        createBody = "{\"error\":\"There are no longer any instances available with the requested specifications.\"}";
        createStatus = 400;
        launch(null, null).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_OUT_OF_STOCK"));
        current().andExpect(jsonPath("$.launch.state").value("FAILED")).andExpect(jsonPath("$.power").value("ERROR"));
        createStatus = 201;
        createBody = "{\"id\":\"pod-" + suffix + "\"}";
        launch(PCIE, "AP-JP-1").andExpect(status().isCreated());
    }

    @Test
    void 관제나_세팅_중에는_끄지_못하고_끄면_인스턴스를_지우고_세션_토큰을_폐기한다() throws Exception {
        launch(null, null).andExpect(status().isCreated());
        UUID sessionId = register("pod-" + suffix);
        report(sessionId, "READY");

        long work = liveWork();
        current().andExpect(jsonPath("$.stop_block").value("LIVE"));
        stop().andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_STOP_BLOCKED_LIVE"));
        endWork(work);

        UUID setup = activeSetup();
        stop().andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_STOP_BLOCKED_SETUP"));
        jdbc.update("UPDATE setup_session SET status = 'FAILED' WHERE setup_session_id = ?", setup);
        assertThat(calls).noneMatch(c -> c.startsWith("DELETE"));

        stop().andExpect(status().isNoContent());
        assertThat(calls).contains("DELETE /v1/pods/pod-" + suffix);
        assertThat(sessions.endedAmong(List.of(sessionId))).containsExactly(sessionId);
        assertThat(jdbc.queryForObject("SELECT end_reason FROM gpu_session_event WHERE session_id = ? AND kind = 'ENDED'", String.class,
                sessionId)).isEqualTo("ADMIN_STOPPED");
        assertThat(jdbc.queryForList("SELECT kind || ':' || coalesce(reason_code, '') || ':' || coalesce(actor_user_id::text, '')"
                + " FROM gpu_launch_event WHERE instance_id = ? ORDER BY event_id", String.class, "pod-" + suffix))
                .containsExactly("CREATED::" + adminId, "STOP_REQUESTED:ADMIN:" + adminId, "STOPPED:ADMIN:" + adminId);
        current().andExpect(jsonPath("$.power").value("OFF")).andExpect(jsonPath("$.launch.state").value("STOPPED"));
        stop().andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_NOT_ON"));
    }

    @Test
    void 자동_정지는_유휴_N분이_지나면_끄지만_관제_세팅_업로드_중에는_끄지_않는다() throws Exception {
        launch(null, null).andExpect(status().isCreated());
        UUID sessionId = register("pod-" + suffix);
        report(sessionId, "READY");
        assertThat(power.autoStop()).isEmpty();

        // 31분 뒤(유휴 30분)
        shift(Duration.ofMinutes(31));
        long work = liveWork();
        assertThat(power.autoStop()).isEmpty();
        endWork(work);
        // 실행이 막 끝났으면 그때부터 다시 센다
        shift(Duration.ZERO);
        UUID setup = activeSetup();
        shift(Duration.ofMinutes(31));
        assertThat(power.autoStop()).isEmpty();
        jdbc.update("UPDATE setup_session SET status = 'FAILED', updated_at = now() - interval '2 hours'"
                + " WHERE setup_session_id = ?", setup);
        jdbc.update("UPDATE work_unit SET actual_started_at = actual_started_at - interval '3 hours',"
                + " actual_ended_at = actual_ended_at - interval '2 hours' WHERE work_id = ?", work);
        jdbc.update("UPDATE work_run SET started_at = started_at - interval '3 hours', ended_at = ended_at - interval '2 hours'"
                + " WHERE work_id = ?", work);
        // 녹화 조각 업로드 서명이 방금 있었으면(옮긴 시계로 1분 전) 업로드 중이다
        jdbc.update("""
                INSERT INTO upload_grant (object_key, purpose, requester, content_type, size_bytes, sha256, issued_at, expires_at)
                VALUES (?, 'RECORDING_SEGMENT', 'TIER0', 'video/mp4', 1, ?, now() + interval '30 minutes', now() + interval '35 minutes')
                """, "recordings/test-" + suffix, "0".repeat(64));
        assertThat(power.autoStop()).isEmpty();
        jdbc.update("UPDATE upload_grant SET issued_at = now() - interval '2 hours', expires_at = now() - interval '2 hours'"
                + " WHERE object_key = ?", "recordings/test-" + suffix);
        assertThat(calls).noneMatch(c -> c.startsWith("DELETE"));

        assertThat(power.autoStop()).contains("IDLE");
        assertThat(calls).contains("DELETE /v1/pods/pod-" + suffix);
        assertThat(jdbc.queryForObject("SELECT end_reason FROM gpu_session_event WHERE session_id = ? AND kind = 'ENDED'", String.class,
                sessionId)).isEqualTo("ADMIN_STOPPED");
        assertThat(jdbc.queryForObject("SELECT reason_code FROM gpu_launch_event WHERE instance_id = ? AND kind = 'STOPPED'",
                String.class, "pod-" + suffix)).isEqualTo("IDLE");
    }

    @Test
    void 자동_정지_0분은_유휴로_끄지_않고_등록이_없거나_세션이_끝난_인스턴스는_끈다() throws Exception {
        saveSettings(null, 0);
        launch(null, null).andExpect(status().isCreated());
        shift(Duration.ofMinutes(20));
        assertThat(power.autoStop()).isEmpty();
        // 켠 뒤 25분 넘게 등록이 없다
        shift(Duration.ofMinutes(26));
        assertThat(power.autoStop()).contains("BOOT_TIMEOUT");
        assertThat(calls).contains("DELETE /v1/pods/pod-" + suffix);

        // 다음: 등록했지만 세션이 무응답으로 끝났는데 인스턴스는 남았다
        shift(Duration.ZERO);
        createBody = "{\"id\":\"pod2-" + suffix + "\"}";
        launch(null, null).andExpect(status().isCreated());
        UUID sessionId = register("pod2-" + suffix);
        sessions.end(sessionId, GpuSessionService.EndReason.STALE);
        current().andExpect(jsonPath("$.power").value("ERROR"));
        assertThat(power.autoStop()).contains("SESSION_ENDED");
        assertThat(calls).contains("DELETE /v1/pods/pod2-" + suffix);

        // 공급자에서 이미 사라진 인스턴스는 끄지 않고 닫기만 한다
        createBody = "{\"id\":\"pod3-" + suffix + "\"}";
        launch(null, null).andExpect(status().isCreated());
        podGone = true;
        assertThat(power.autoStop()).isEmpty();
        assertThat(calls).doesNotContain("DELETE /v1/pods/pod3-" + suffix);
        current().andExpect(jsonPath("$.launch.state").value("STOPPED")).andExpect(jsonPath("$.power").value("OFF"));
    }

    @Test
    void GPU_설정은_새_버전으로_쌓이고_관리자만_바꾼다() throws Exception {
        Long before = jdbc.queryForObject("SELECT count(*) FROM gpu_settings_version", Long.class);
        saveSettings(3.5, 45);
        mvc.perform(get("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.settings.max_price_per_hour_usd").value(3.5))
                .andExpect(jsonPath("$.settings.auto_stop_idle_minutes").value(45))
                .andExpect(jsonPath("$.settings.preferred_gpu_types[0]").value(HBM3))
                .andExpect(jsonPath("$.providers[0].id").value("RUNPOD"))
                .andExpect(jsonPath("$.providers[0].configured").value(true))
                .andExpect(jsonPath("$.providers[1].id").value("AWS"))
                .andExpect(jsonPath("$.providers[1].configured").value(false));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM gpu_settings_version", Long.class)).isEqualTo(before + 1);
        // 이전 버전은 그대로 남는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM gpu_settings_version WHERE auto_stop_idle_minutes = 30 AND created_by = ?",
                Long.class, adminId)).isEqualTo(1);

        ObjectNode bad = settingsBody(null, 30);
        bad.putArray("preferred_gpu_types");
        mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(bad))).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_GPU_SETTINGS"));
        ObjectNode secret = settingsBody(null, 30);
        secret.put("runpod_template_id", "bad id with spaces");
        mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(secret))).andExpect(status().isBadRequest());

        String viewer = "Bearer " + jwt(adminId, "VIEWER");
        mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, viewer).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(settingsBody(null, 30)))).andExpect(status().isForbidden());
        mvc.perform(post("/api/gpu/launches").header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isForbidden());
        mvc.perform(post("/api/gpu/stop").header(HttpHeaders.AUTHORIZATION, viewer)).andExpect(status().isForbidden());
    }

    @Test
    void 재고_표는_카탈로그의_모든_GPU_를_추천과_VRAM_등급_순으로_싣고_기본은_추천_중_켤_수_있는_첫_칸이다() throws Exception {
        // S15P31A101-705: 선호는 A40(추천 아님) 하나. B200 은 카탈로그에만 있고 어느 지역에도 없다
        // S15P31A101-713: 이름·VRAM 을 모르는 항목(id "unknown" · 0 GB, 이름 없음 · 0 GB)은 행이 되지 않는다
        catalog = """
                {"gpus":[
                  {"id":"NVIDIA RTX A4000","name":"RTX A4000","memory":16,"price":{"secure":0.25},
                   "dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"HIGH"}]},
                  {"id":"NVIDIA A40","name":"A40","memory":48,"price":{"secure":0.44},
                   "dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"MEDIUM"}]},
                  {"id":"NVIDIA GeForce RTX 4090","name":"RTX 4090","memory":24,"price":{"secure":0.69},
                   "dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"LOW"}]},
                  {"id":"NVIDIA B200","name":"B200","memory":180,"price":{"secure":5.99}},
                  {"id":"NVIDIA H200","name":"H200 SXM","memory":141,"price":{"secure":3.99},
                   "dataCenters":[{"id":"EU-RO-1","name":"Europe Romania 1","availability":"HIGH"}]},
                  {"id":"%s","name":"H100 PCIe","memory":80,"price":{"secure":2.39},
                   "dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"NONE"}]},
                  {"id":"unknown","name":"unknown","memory":0,"price":{"secure":0.10},
                   "dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"HIGH"}]},
                  {"id":"NVIDIA Mystery","memory":0,"price":{"secure":0.20}}
                ]}""".formatted(PCIE);
        ObjectNode body = settingsBody(null, 30);
        body.putArray("preferred_gpu_types").add("NVIDIA A40");
        mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body))).andExpect(status().isCreated());

        JsonNode offers = mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8).transform(this::read);
        // 추천(H100·H200 계열) → VRAM 등급 → 선호 순서 → VRAM 큰 순
        List<String> rows = new ArrayList<>();
        offers.get("gpu_types").forEach(g -> rows.add(g.get("gpu_type").stringValue() + "/" + g.get("tier").stringValue() + "/"
                + g.get("recommended").booleanValue() + "/" + g.get("preferred").booleanValue() + "/" + g.get("available")));
        assertThat(rows).containsExactly(
                "NVIDIA H200/RECOMMENDED/true/false/true",
                PCIE + "/RECOMMENDED/true/false/false",
                "NVIDIA B200/RECOMMENDED/false/false/false",
                "NVIDIA A40/BELOW_RECOMMENDED/false/true/true",
                "NVIDIA GeForce RTX 4090/LIMITED/false/false/true",
                "NVIDIA RTX A4000/RISKY/false/false/true");
        // 칸: 재고 등급(stock)은 그대로, 켤 수 있음(available)을 더한다. 지역 없는 B200 도 행 × 지역 칸이 있다(없음, 가격은 보인다)
        List<String> regions = new ArrayList<>();
        offers.get("regions").forEach(r -> regions.add(r.get("region").stringValue()));
        assertThat(regions).containsExactly("AP-JP-1", "EU-RO-1");
        assertThat(offers.get("cells").size()).isEqualTo(12);
        JsonNode first = offers.get("cells").get(0);
        assertThat(first.get("gpu_type").stringValue() + "/" + first.get("region").stringValue() + "/" + first.get("stock").stringValue()
                + "/" + first.get("available").booleanValue()).isEqualTo("NVIDIA H200/AP-JP-1/NONE/false");
        JsonNode b200 = offers.get("cells").get(4);
        assertThat(b200.get("gpu_type").stringValue()).isEqualTo("NVIDIA B200");
        assertThat(b200.get("available").booleanValue()).isFalse();
        assertThat(b200.get("price_per_hour_usd").decimalValue()).isEqualByComparingTo("5.99");
        // 기본: 추천 중 켤 수 있는 첫 칸(선호 A40 보다 먼저)
        assertThat(offers.get("default").get("gpu_type").stringValue()).isEqualTo("NVIDIA H200");
        assertThat(offers.get("default").get("region").stringValue()).isEqualTo("EU-RO-1");

        // 선호 밖·추천 아닌 GPU 도 표에서 골라 켤 수 있다(등급은 막지 않는다)
        launch("NVIDIA RTX A4000", "AP-JP-1").andExpect(status().isCreated());
        assertThat(created.getFirst().get("gpuTypeIds").get(0).stringValue()).isEqualTo("NVIDIA RTX A4000");
    }

    @Test
    void API_키가_없으면_재고_조회와_켜기가_503_이다() throws Exception {
        runpod.useEndpoints("http://127.0.0.1:1/v1", "http://127.0.0.1:1", "");
        mvc.perform(get("/api/gpu/offers").header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isServiceUnavailable()).andExpect(jsonPath("$.code").value("GPU_PROVIDER_NOT_CONFIGURED"));
        launch(null, null).andExpect(status().isServiceUnavailable());
        current().andExpect(jsonPath("$.power").value("OFF"));
    }

    // ---- 도우미

    private void handle(HttpExchange exchange) throws IOException {
        String method = exchange.getRequestMethod();
        String path = exchange.getRequestURI().getPath();
        calls.add(method + " " + path);
        if (!("Bearer " + KEY).equals(exchange.getRequestHeaders().getFirst("Authorization"))) {
            respond(exchange, 401, "{}");
            return;
        }
        if (method.equals("GET") && path.equals("/v2/catalog/gpus")) {
            respond(exchange, 200, catalog);
        } else if (method.equals("POST") && path.equals("/v1/pods")) {
            created.add(mapper.readTree(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8)));
            respond(exchange, createStatus, createBody);
        } else if (method.equals("DELETE") && path.startsWith("/v1/pods/")) {
            respond(exchange, 200, "{}");
        } else if (method.equals("GET") && path.startsWith("/v1/pods/")) {
            if (podGone) {
                respond(exchange, 404, "{}");
            } else {
                respond(exchange, 200, "{\"id\":\"" + path.substring("/v1/pods/".length()) + "\",\"desiredStatus\":\"RUNNING\"}");
            }
        } else {
            respond(exchange, 404, "{}");
        }
    }

    private static void respond(HttpExchange exchange, int status, String body) throws IOException {
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().add("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        exchange.getResponseBody().write(bytes);
        exchange.close();
    }

    /** RunPod /v2/catalog/gpus?include=AVAILABILITY 모양(문서 예시와 같은 필드) */
    private static String catalog(boolean withStock) {
        return """
                {"gpus":[
                  {"id":"%s","name":"H100 SXM","memory":80,"secure":true,"community":true,"price":{"secure":2.99,"community":2.69},
                   "availability":"HIGH","dataCenters":[{"id":"EU-RO-1","name":"Europe Romania 1","availability":"HIGH"}]},
                  {"id":"%s","name":"H100 PCIe","memory":80,"secure":true,"community":false,"price":{"secure":2.39},
                   "availability":"LOW","dataCenters":[{"id":"AP-JP-1","name":"Asia Japan 1","availability":"%s"}]},
                  {"id":"NVIDIA GeForce RTX 4090","name":"RTX 4090","memory":24,"price":{"secure":0.69},
                   "dataCenters":[{"id":"US-TX-3","name":"US Texas 3","availability":"HIGH"}]}
                ]}""".formatted(HBM3, PCIE, withStock ? "LOW" : "NONE");
    }

    private ResultActions launch(String gpuType, String region) throws Exception {
        ObjectNode body = mapper.createObjectNode();
        if (gpuType != null) {
            body.put("gpu_type", gpuType);
            body.put("region", region);
        }
        return mvc.perform(post("/api/gpu/launches").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body)));
    }

    private ResultActions stop() throws Exception {
        return mvc.perform(post("/api/gpu/stop").header(HttpHeaders.AUTHORIZATION, admin));
    }

    private ResultActions current() throws Exception {
        return mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin)).andExpect(status().isOk());
    }

    private ObjectNode settingsBody(Double cap, int idleMinutes) {
        ObjectNode body = mapper.createObjectNode();
        body.put("provider", "RUNPOD");
        body.putArray("preferred_gpu_types").add(HBM3).add(PCIE);
        body.putArray("preferred_regions").add("AP-JP-1").add("EU-RO-1");
        body.put("cloud_type", "SECURE");
        body.put("interruptible", false);
        if (cap == null) {
            body.putNull("max_price_per_hour_usd");
        } else {
            body.put("max_price_per_hour_usd", cap);
        }
        body.put("auto_stop_idle_minutes", idleMinutes);
        body.put("runpod_template_id", "tpl-simai");
        return body;
    }

    private void saveSettings(Double cap, int idleMinutes) throws Exception {
        mvc.perform(post("/api/gpu/settings").header(HttpHeaders.AUTHORIZATION, admin).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(settingsBody(cap, idleMinutes)))).andExpect(status().isCreated());
    }

    /** 공급자 인스턴스가 G1 등록을 보낸 것처럼 세션을 연다(-700 이 RUNPOD_POD_ID 를 instance_id 로 싣는다) */
    private UUID register(String instanceId) {
        ObjectNode p = mapper.createObjectNode();
        p.put("provider", "RUNPOD");
        p.put("instance_id", instanceId);
        p.put("gpu_type", HBM3);
        p.put("region", "EU-RO-1");
        p.put("public_host", "203.0.113.24");
        p.set("ports", mapper.readTree("[{\"role\":\"CONTROL\",\"public_port\":17000},{\"role\":\"VIDEO\",\"public_port\":17001}]"));
        p.putNull("image_digest");
        p.put("price_per_hour_usd", 2.99);
        return sessions.register(bootId, UUID.randomUUID(), p).sessionId();
    }

    private void report(UUID sessionId, String stage) {
        ObjectNode p = mapper.createObjectNode();
        p.put("session_id", sessionId.toString());
        p.put("stage", stage);
        p.put("reported_at", Instant.now().toEpochMilli());
        sessions.report(bootId, sessionId, p);
    }

    private long liveWork() {
        return jdbc.queryForObject("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, actual_started_at, created_by,
                    created_at, updated_at)
                VALUES ('classroom-1', current_date, 'gpu-test', now(), now() + interval '1 hour', 1, 'IN_PROGRESS', now(), ?, now(), now())
                RETURNING work_id
                """, Long.class, adminId);
    }

    private void endWork(long work) {
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", work);
        jdbc.update("""
                INSERT INTO work_run (work_id, run_no, site_id, started_at, ended_at)
                SELECT work_id, 1, site_id, actual_started_at, actual_ended_at FROM work_unit WHERE work_id = ?
                """, work);
    }

    private UUID activeSetup() {
        Long work = jdbc.queryForObject("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES ('classroom-1', current_date, 'gpu-setup', now(), now() + interval '1 hour', 1, 'PLANNED', ?, now(), now())
                RETURNING work_id
                """, Long.class, adminId);
        String job = "job-gpu-" + UUID.randomUUID();
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at)"
                + " VALUES (?, 'site_setup','BACKGROUND','QUEUED',1,1,1000,'{}'::jsonb,now())", job);
        UUID session = UUID.randomUUID();
        jdbc.update("""
                INSERT INTO setup_session(setup_session_id,site_id,work_unit_id,job_id,status,camera_heights_m,height_tolerance_cm,requested_by,
                    started_at,updated_at)
                VALUES (?,'classroom-1',?,?,'COLLECTING_BACKGROUND','{}'::jsonb,5,?,now(),now())
                """, session, work, job, adminId);
        return session;
    }

    /** 자동 정지 시계를 지금 + d 로 옮긴다 */
    private void shift(Duration d) {
        power.useClock(Clock.offset(Clock.system(ZoneOffset.UTC), d));
    }

    private JsonNode read(String body) {
        return mapper.readTree(body);
    }

    private String jwt(Long userId, String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject(String.valueOf(userId)).issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private String issued(String... args) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int code = commands.execute(List.of(args), io(out));
        assertThat(code).isZero();
        Matcher matcher = TOKEN.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        return matcher.group(1);
    }

    private static CommandIo io(ByteArrayOutputStream out) {
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        return new CommandIo(print, print, prompt -> null);
    }
}
