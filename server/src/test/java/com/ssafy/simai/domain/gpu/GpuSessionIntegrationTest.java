package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.annotation.Import;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 세션 등록·중복 등록·권한·무응답 폐기(S15P31A101-699). 부팅 비밀값·워커는 관리 명령으로 만든다.
 * Tier 0 토큰은 업로드 서명 체인(Tier 0 토큰을 받는 REST 경로)으로 인증 여부만 본다. 실제 I1 연결 끊기는 GpuSessionI1IntegrationTest.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
@Import(GpuSessionIntegrationTest.ProbeController.class)
class GpuSessionIntegrationTest {

    private static final Pattern TOKEN = Pattern.compile("토큰\\(이번 한 번만 표시\\): (\\S+)");

    @Autowired
    MockMvc mvc;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    GpuSessionService sessions;

    @Autowired
    GpuSessionSweeper sweeper;

    @Autowired
    JwtEncoder jwtEncoder;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-gpu-" + suffix;
    private final String bootId = "gpu-boot-" + suffix;
    private String bootSecret;

    @BeforeEach
    void setUp() {
        issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale,site_setup");
        bootSecret = issued("gpu-boot", "register", "--id", bootId, "--worker-id", workerId);
    }

    @Test
    void 등록하면_워커_Tier0_토큰과_기동_설정을_한_번_주고_DB_에는_해시만_남는다() throws Exception {
        MvcResult result = mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isCreated())
                .andExpect(header().string(HttpHeaders.CACHE_CONTROL, "no-store"))
                .andReturn();
        String body = result.getResponse().getContentAsString(StandardCharsets.UTF_8);
        // 응답은 계약 그대로다(토큰 형식·닫힌 객체·start 의 모르면 비움 규칙)
        assertThat(schemas.violations("messages/gpu_session_register_result", body)).isEmpty();
        JsonNode p = mapper.readTree(body).get("payload");
        String workerToken = p.at("/worker/token").stringValue();
        String tier0Token = p.at("/tier0/token").stringValue();
        UUID sessionId = UUID.fromString(p.get("session_id").stringValue());
        assertThat(p.at("/worker/worker_id").stringValue()).isEqualTo(workerId);
        assertThat(p.at("/report/stale_after_ms").longValue()).isEqualTo(600_000);
        assertThat(p.at("/start/ec2_base_url").stringValue()).isEqualTo("https://ec2.test");

        // 워커 토큰은 워커 경로에서, Tier 0 토큰은 Tier 0 경로에서 통한다
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken))
                .andExpect(status().isOk());
        assertThat(tier0Accepted(tier0Token)).isTrue();
        assertThat(sessions.activeSessionForTier0Token(com.ssafy.simai.global.security.TokenHashes.sha256Hex(tier0Token)))
                .contains(sessionId);

        // DB 에는 원문이 없다
        String stored = jdbc.queryForObject("SELECT tier0_token_hash FROM gpu_session WHERE session_id = ?", String.class, sessionId);
        assertThat(stored).hasSize(64).isNotEqualTo(tier0Token);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM gpu_session WHERE tier0_token_hash = ? OR ports::text LIKE ?",
                Integer.class, tier0Token, "%" + tier0Token + "%")).isZero();
        String bootHash = jdbc.queryForObject("SELECT token_hash FROM gpu_boot_secret WHERE boot_id = ?", String.class, bootId);
        assertThat(bootHash).hasSize(64).isNotEqualTo(bootSecret);
        // 등록 사실은 그대로 남는다
        assertThat(jdbc.queryForObject("SELECT provider || '/' || region || '/' || gpu_type FROM gpu_session WHERE session_id = ?",
                String.class, sessionId)).isEqualTo("RUNPOD/AP-JP-1/NVIDIA H100 80GB HBM3");
    }

    @Test
    void 다시_등록하면_이전_세션은_끝나고_이전_토큰은_모두_무효다() throws Exception {
        JsonNode first = register();
        JsonNode second = register();
        UUID firstId = UUID.fromString(first.get("session_id").stringValue());

        assertThat(second.get("superseded_session_ids").valueStream().map(JsonNode::stringValue).toList())
                .containsExactly(firstId.toString());
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + first.at("/worker/token").stringValue()))
                .andExpect(status().isUnauthorized());
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + second.at("/worker/token").stringValue()))
                .andExpect(status().isOk());
        assertThat(tier0Accepted(first.at("/tier0/token").stringValue())).isFalse();
        assertThat(tier0Accepted(second.at("/tier0/token").stringValue())).isTrue();
        assertThat(jdbc.queryForObject("SELECT end_reason FROM gpu_session_event WHERE session_id = ? AND kind = 'ENDED'",
                String.class, firstId)).isEqualTo("SUPERSEDED");
        // 밀린 세션의 진행 보고는 끝났다고 알린다(인스턴스는 서비스를 멈춘다)
        mvc.perform(post("/api/gpu/sessions/" + firstId + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(reportBody(bootId, firstId, "READY", null)))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("GPU_SESSION_ENDED"));
        // 열린 세션은 늘 하나다(다른 부팅 비밀값의 세션도 밀린다. 여기서는 이 시험의 두 세션만 본다)
        assertThat(jdbc.queryForObject("""
                SELECT count(*) FROM gpu_session s WHERE s.boot_id = ?
                  AND NOT EXISTS (SELECT 1 FROM gpu_session_event e WHERE e.session_id = s.session_id AND e.kind = 'ENDED')
                """, Integer.class, bootId)).isEqualTo(1);
    }

    @Test
    void 맞지_않거나_폐기된_부팅_비밀값과_다른_source_id_는_거부한다() throws Exception {
        mvc.perform(post(GpuPaths.SESSIONS).contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isUnauthorized());
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret + "x")
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isUnauthorized());
        // 관리자 JWT 로도 등록하지 못한다(부팅 비밀값 체인)
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN"))
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isUnauthorized());
        // 워커 토큰도 부팅 비밀값이 아니다
        String workerToken = issued("worker", "rotate", "--id", workerId);
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isUnauthorized());
        // source_id 는 비밀값 ID 와 같아야 한다
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody("gpu-boot-other")))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("GPU_SOURCE_MISMATCH"));
        // 계약에 없는 필드(비밀 값)를 실으면 400
        ObjectNode withSecret = (ObjectNode) mapper.readTree(registerBody(bootId));
        ((ObjectNode) withSecret.get("payload")).put("hf_token", "hf_not_real");
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(withSecret)))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_GPU_MESSAGE"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM gpu_session WHERE boot_id = ?", Integer.class, bootId)).isZero();

        // 폐기하면 그 비밀값이 연 세션도 끝나고 토큰이 무효다
        JsonNode session = register();
        assertThat(run("gpu-boot", "revoke", "--id", bootId)).isZero();
        mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isUnauthorized());
        assertThat(tier0Accepted(session.at("/tier0/token").stringValue())).isFalse();
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + session.at("/worker/token").stringValue()))
                .andExpect(status().isUnauthorized());
        assertThat(jdbc.queryForObject("SELECT end_reason FROM gpu_session_event WHERE session_id = ?::uuid AND kind = 'ENDED'",
                String.class, session.get("session_id").stringValue())).isEqualTo("BOOT_SECRET_REVOKED");
    }

    @Test
    void 부팅_비밀값으로는_등록과_보고_말고_다른_API_를_쓰지_못한다() throws Exception {
        String bearer = "Bearer " + bootSecret;
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());
        mvc.perform(post("/api/worker/jobs/lease").header(HttpHeaders.AUTHORIZATION, bearer)
                .contentType(MediaType.APPLICATION_JSON).content("{}")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/devices/gpu-probe").header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/endpoints/tier0").header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/events").header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());
        assertThat(tier0Accepted(bootSecret)).isFalse();
        mvc.perform(get("/ws/tier0").header(HttpHeaders.AUTHORIZATION, bearer)).andExpect(status().isUnauthorized());

        // 반대로 GPU 세션 Tier 0 토큰·워커 토큰은 등록·보고에 쓰지 못한다
        JsonNode session = register();
        for (String token : List.of(session.at("/tier0/token").stringValue(), session.at("/worker/token").stringValue())) {
            mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                    .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId))).andExpect(status().isUnauthorized());
        }
        // 상태 조회는 관리자만
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER"))).andExpect(status().isForbidden());
    }

    @Test
    void 진행_보고는_상태_조회에_보이고_종료_보고는_세션을_끝낸다() throws Exception {
        JsonNode session = register();
        UUID id = UUID.fromString(session.get("session_id").stringValue());
        report(id, "WEIGHTS", null).andExpect(status().isNoContent());
        report(id, "FAILED", "ENGINE_BUILD_FAILED").andExpect(status().isNoContent());
        // FAILED 는 세션을 끝내지 않는다(다시 시도)
        report(id, "ENGINE", null).andExpect(status().isNoContent());

        String admin = "Bearer " + jwt("ADMIN");
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.session.session_id").value(id.toString()))
                .andExpect(jsonPath("$.session.state").value("ACTIVE"))
                .andExpect(jsonPath("$.session.stage").value("ENGINE"))
                .andExpect(jsonPath("$.session.gpu_type").value("NVIDIA H100 80GB HBM3"))
                .andExpect(jsonPath("$.session.region").value("AP-JP-1"))
                .andExpect(jsonPath("$.session.tier0_link").value("DISCONNECTED"))
                .andExpect(jsonPath("$.session.end_reason").isEmpty());

        // 다른 부팅 비밀값은 이 세션에 보고하지 못한다
        String otherBoot = "gpu-boot-o" + suffix;
        String otherSecret = issued("gpu-boot", "register", "--id", otherBoot, "--worker-id", workerId);
        mvc.perform(post("/api/gpu/sessions/" + id + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + otherSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(reportBody(otherBoot, id, "READY", null)))
                .andExpect(status().isNotFound());
        // 경로와 본문의 세션이 다르면 400
        mvc.perform(post("/api/gpu/sessions/" + UUID.randomUUID() + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(reportBody(bootId, id, "READY", null)))
                .andExpect(status().isBadRequest());

        report(id, "ENDED", "PROVIDER_STOP").andExpect(status().isNoContent());
        // 같은 종료 보고를 다시 보내도 같다
        report(id, "ENDED", null).andExpect(status().isNoContent());
        report(id, "READY", null).andExpect(status().isConflict());
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.session.state").value("ENDED"))
                .andExpect(jsonPath("$.session.end_reason").value("END_REPORTED"))
                .andExpect(jsonPath("$.session.end_reason_code").value("PROVIDER_STOP"));
        assertThat(tier0Accepted(session.at("/tier0/token").stringValue())).isFalse();
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + session.at("/worker/token").stringValue()))
                .andExpect(status().isUnauthorized());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM gpu_session_event WHERE session_id = ? AND kind = 'ENDED'",
                Integer.class, id)).isEqualTo(1);
    }

    @Test
    void 호스트_CPU_측정은_보고에서_저장되고_가장_최근_값을_관리자_조회에_준다() throws Exception {
        JsonNode session = register();
        UUID id = UUID.fromString(session.get("session_id").stringValue());
        String admin = "Bearer " + jwt("ADMIN");
        report(id, "WEIGHTS", null).andExpect(status().isNoContent());
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.session.host_cpu").isEmpty());

        reportWithHostCpu(id, "WEIGHTS", "{\"model\":\"Intel(R) Xeon(R) Platinum 8460Y+\",\"logical_cpus\":160,\"cpu_quota\":null,"
                + "\"single_thread_score\":1480.25,\"measured_at\":1794700919000}").andExpect(status().isNoContent());
        // host_cpu 가 없는 다음 보고가 와도 마지막 측정이 남는다
        report(id, "ENGINE", null).andExpect(status().isNoContent());
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, admin))
                .andExpect(jsonPath("$.session.stage").value("ENGINE"))
                .andExpect(jsonPath("$.session.host_cpu.model").value("Intel(R) Xeon(R) Platinum 8460Y+"))
                .andExpect(jsonPath("$.session.host_cpu.logical_cpus").value(160))
                .andExpect(jsonPath("$.session.host_cpu.cpu_quota").isEmpty())
                .andExpect(jsonPath("$.session.host_cpu.single_thread_score").value(1480.25));

        // 계약에 맞지 않는 host_cpu(점수가 문자열)는 400
        reportWithHostCpu(id, "WEIGHTS", "{\"model\":null,\"logical_cpus\":8,\"cpu_quota\":null,\"single_thread_score\":\"fast\","
                + "\"measured_at\":1794700919000}").andExpect(status().isBadRequest());
    }

    private org.springframework.test.web.servlet.ResultActions reportWithHostCpu(UUID id, String stage, String hostCpuJson) throws Exception {
        ObjectNode root = (ObjectNode) mapper.readTree(reportBody(bootId, id, stage, null));
        root.put("schema_version", "1.1.0");
        ((ObjectNode) root.get("payload")).set("host_cpu", mapper.readTree(hostCpuJson));
        return mvc.perform(post("/api/gpu/sessions/" + id + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(root)));
    }

    @Test
    void 무응답이_stale_after_를_넘으면_끝내고_토큰을_폐기하지만_I1_이_붙어_있으면_두다() throws Exception {
        JsonNode session = register();
        UUID id = UUID.fromString(session.get("session_id").stringValue());
        sweeper.sweep();
        assertThat(sessions.endedAmong(Set.of(id))).isEmpty();

        // 마지막 보고를 11분 전으로 돌린다(시계를 돌리는 대신 수신 시각을 옮긴다)
        jdbc.update("UPDATE gpu_session_event SET received_at = received_at - interval '11 minutes' WHERE session_id = ?", id);
        // 이 세션 Tier 0 토큰으로 I1 이 붙어 있으면 끝내지 않는다
        assertThat(sessions.endStale(Set.of(id))).isEmpty();
        sweeper.sweep();
        assertThat(sessions.endedAmong(Set.of(id))).containsExactly(id);
        assertThat(jdbc.queryForObject("SELECT end_reason FROM gpu_session_event WHERE session_id = ? AND kind = 'ENDED'",
                String.class, id)).isEqualTo("STALE");
        assertThat(tier0Accepted(session.at("/tier0/token").stringValue())).isFalse();
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + session.at("/worker/token").stringValue()))
                .andExpect(status().isUnauthorized());

        // 다음 등록은 폐기된 워커를 새 토큰으로 되살린다
        JsonNode next = register();
        assertThat(next.get("superseded_session_ids").isEmpty()).isTrue();
        mvc.perform(get("/api/worker/gpu-probe").header(HttpHeaders.AUTHORIZATION, "Bearer " + next.at("/worker/token").stringValue()))
                .andExpect(status().isOk());
    }

    // ---- 도우미

    private JsonNode register() throws Exception {
        MvcResult result = mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(registerBody(bootId)))
                .andExpect(status().isCreated()).andReturn();
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8)).get("payload");
    }

    private org.springframework.test.web.servlet.ResultActions report(UUID id, String stage, String reason) throws Exception {
        return mvc.perform(post("/api/gpu/sessions/" + id + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                .contentType(MediaType.APPLICATION_JSON).content(reportBody(bootId, id, stage, reason)));
    }

    /** Tier 0 토큰을 받는 REST 경로(업로드 서명)에서 401 이 아니면 인증된 것이다(빈 본문이라 400 등) */
    private boolean tier0Accepted(String token) throws Exception {
        int status = mvc.perform(post("/api/uploads/sign").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content("{}")).andReturn().getResponse().getStatus();
        return status != 401 && status != 403;
    }

    private String registerBody(String sourceId) throws Exception {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_register");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("provider", "RUNPOD");
        p.put("instance_id", "pod-" + suffix);
        p.put("gpu_type", "NVIDIA H100 80GB HBM3");
        p.put("region", "AP-JP-1");
        p.put("public_host", "203.0.113.24");
        p.set("ports", mapper.readTree("""
                [{"role":"CONTROL","public_port":17000,"requested_port":70000},{"role":"VIDEO","public_port":17001},
                 {"role":"SSH","public_port":22113}]"""));
        p.putNull("image_digest");
        p.put("price_per_hour_usd", 2.99);
        return mapper.writeValueAsString(root);
    }

    private String reportBody(String sourceId, UUID sessionId, String stage, String reason) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_report");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", sourceId);
        ObjectNode p = root.putObject("payload");
        p.put("session_id", sessionId.toString());
        p.put("stage", stage);
        p.put("reason_code", reason);
        p.put("reported_at", Instant.now().toEpochMilli());
        return mapper.writeValueAsString(root);
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
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

    private int run(String... args) {
        return commands.execute(List.of(args), io(new ByteArrayOutputStream()));
    }

    private static CommandIo io(ByteArrayOutputStream out) {
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        return new CommandIo(print, print, prompt -> null);
    }

    @RestController
    static class ProbeController {

        @GetMapping({"/api/devices/gpu-probe", "/api/worker/gpu-probe"})
        String whoAmI(@AuthenticationPrincipal String id) {
            return id;
        }
    }
}
