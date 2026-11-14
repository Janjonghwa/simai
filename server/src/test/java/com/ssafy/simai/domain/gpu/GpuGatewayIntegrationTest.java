package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.security.TokenHashes;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
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
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 게이트웨이 자동 전환용 기기 API(S15P31A101-701): 노트북 기기 토큰으로 가장 최근 GPU 세션을 보고(계약 gpu_session_status),
 * 자동 전환 상태를 알린다(계약 gateway_switch_state → 관리자 조회의 gateway). 워치·부팅 비밀값·관리자 JWT 로는 못 쓴다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class GpuGatewayIntegrationTest {

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
    JwtEncoder jwtEncoder;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-gw-" + suffix;
    private final String bootId = "gpu-boot-gw-" + suffix;
    private final String laptop = "laptop-gw-" + suffix;
    private final String laptopToken = "test-only-gw-laptop-" + suffix;
    private final String watchToken = "test-only-gw-watch-" + suffix;
    private String bootSecret;

    @BeforeEach
    void setUp() {
        issued("worker", "register", "--id", workerId, "--job-types", "vlm_rationale,site_setup");
        bootSecret = issued("gpu-boot", "register", "--id", bootId, "--worker-id", workerId);
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'LAPTOP', ?, now())",
                laptop, TokenHashes.sha256Hex(laptopToken));
        jdbc.update("INSERT INTO device (device_id, kind, token_hash, issued_at) VALUES (?, 'WATCH', ?, now())",
                "watch-gw-" + suffix, TokenHashes.sha256Hex(watchToken));
    }

    @Test
    void 노트북은_가장_최근_세션의_주소와_CONTROL_VIDEO_포트만_받고_끝나면_ENDED_로_본다() throws Exception {
        // 등록이 한 번도 없으면 session null(이 트랜잭션 안에서만 지우고 끝나면 되돌린다)
        jdbc.update("DELETE FROM gpu_session_event");
        jdbc.update("DELETE FROM gpu_session");
        String none = gatewaySession();
        assertThat(schemas.violations("messages/gpu_session_status", none)).isEmpty();
        assertThat(mapper.readTree(none).get("session").isNull()).isTrue();

        JsonNode registered = register();
        String id = registered.get("session_id").stringValue();
        String body = gatewaySession();
        assertThat(schemas.violations("messages/gpu_session_status", body)).isEmpty();
        JsonNode session = mapper.readTree(body).get("session");
        assertThat(session.get("session_id").stringValue()).isEqualTo(id);
        assertThat(session.get("state").stringValue()).isEqualTo("ACTIVE");
        assertThat(session.get("stage").stringValue()).isEqualTo("REGISTERED");
        assertThat(session.get("public_host").stringValue()).isEqualTo("203.0.113.24");
        assertThat(session.get("tier0_link").stringValue()).isEqualTo("DISCONNECTED");
        // SSH 포트·공급자 값·가격·워커는 노트북에 주지 않는다
        assertThat(session.get("ports").valueStream().map(p -> p.get("role").stringValue()).toList()).containsExactly("CONTROL", "VIDEO");
        assertThat(session.get("ports").get(0).get("public_port").intValue()).isEqualTo(40123);
        assertThat(session.get("ports").get(0).get("requested_port").intValue()).isEqualTo(17000);
        assertThat(session.has("instance_id") || session.has("price_per_hour_usd") || session.has("worker_id")).isFalse();
        assertThat(body).doesNotContain(registered.at("/tier0/token").stringValue()).doesNotContain(registered.at("/worker/token").stringValue());

        mvc.perform(post("/api/gpu/sessions/" + id + "/reports").header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(reportBody(UUID.fromString(id), "ENDED")))
                .andExpect(status().isNoContent());
        String ended = gatewaySession();
        assertThat(schemas.violations("messages/gpu_session_status", ended)).isEmpty();
        JsonNode endedSession = mapper.readTree(ended).get("session");
        assertThat(endedSession.get("state").stringValue()).isEqualTo("ENDED");
        assertThat(endedSession.get("end_reason").stringValue()).isEqualTo("END_REPORTED");
        assertThat(endedSession.get("ended_at_ms").isNumber()).isTrue();
    }

    @Test
    void 워치_부팅_비밀값_관리자_JWT_로는_세션_상태를_보지_못한다() throws Exception {
        register();
        mvc.perform(get(GpuPaths.DEVICE_SESSION)).andExpect(status().isUnauthorized());
        mvc.perform(get(GpuPaths.DEVICE_SESSION).header(HttpHeaders.AUTHORIZATION, "Bearer " + watchToken))
                .andExpect(status().isForbidden()).andExpect(jsonPath("$.code").value("GPU_NOT_A_LAPTOP"));
        mvc.perform(get(GpuPaths.DEVICE_SESSION).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret))
                .andExpect(status().isUnauthorized());
        mvc.perform(get(GpuPaths.DEVICE_SESSION).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN")))
                .andExpect(status().isUnauthorized());
        // 폐기된 노트북도 못 본다
        jdbc.update("UPDATE device SET revoked_at = now() WHERE device_id = ?", laptop);
        mvc.perform(get(GpuPaths.DEVICE_SESSION).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken))
                .andExpect(status().isUnauthorized());
    }

    @Test
    void 게이트웨이_전환_상태는_계약으로_검사하고_관리자_조회에_마지막_값이_보인다() throws Exception {
        ObjectNode state = (ObjectNode) mapper.readTree("""
                {"schema_version":"1.0.0","observed_at_ms":1794657675000,"auto_enabled":true,"owner":"AUTO","mode":"RUN",
                 "decision":"SWITCH_RUN","switch_ok":true,"reason_code":"READY","session_id":null,
                 "target":{"public_host":"203.0.113.24","control_port":40123,"video_port":40124}}""");
        state.put("observed_at_ms", Instant.now().toEpochMilli());
        mvc.perform(post(GpuPaths.DEVICE_GATEWAY_STATE).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(state)))
                .andExpect(status().isNoContent());
        mvc.perform(get(GpuPaths.CURRENT).header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.gateway.device_id").value(laptop))
                .andExpect(jsonPath("$.gateway.state.reason_code").value("READY"))
                .andExpect(jsonPath("$.gateway.state.target.control_port").value(40123));

        // 계약에 없는 필드(비밀 값)·전환 결과 없는 전환·빈 본문은 400, 워치는 403
        ObjectNode secret = state.deepCopy();
        secret.put("device_token", "not-a-real-token");
        mvc.perform(post(GpuPaths.DEVICE_GATEWAY_STATE).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(secret)))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_GATEWAY_STATE"));
        ObjectNode noResult = state.deepCopy();
        noResult.putNull("switch_ok");
        mvc.perform(post(GpuPaths.DEVICE_GATEWAY_STATE).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(noResult)))
                .andExpect(status().isBadRequest());
        mvc.perform(post(GpuPaths.DEVICE_GATEWAY_STATE).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken)
                        .contentType(MediaType.APPLICATION_JSON)).andExpect(status().isBadRequest());
        mvc.perform(post(GpuPaths.DEVICE_GATEWAY_STATE).header(HttpHeaders.AUTHORIZATION, "Bearer " + watchToken)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(state)))
                .andExpect(status().isForbidden());
    }

    // ---- 도우미

    private String gatewaySession() throws Exception {
        return mvc.perform(get(GpuPaths.DEVICE_SESSION).header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken))
                .andExpect(status().isOk()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
    }

    private JsonNode register() throws Exception {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_register");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", bootId);
        ObjectNode p = root.putObject("payload");
        p.put("provider", "RUNPOD");
        p.put("instance_id", "pod-" + suffix);
        p.put("gpu_type", "NVIDIA H100 80GB HBM3");
        p.put("region", "AP-JP-1");
        p.put("public_host", "203.0.113.24");
        p.set("ports", mapper.readTree("""
                [{"role":"CONTROL","public_port":40123,"requested_port":17000},{"role":"VIDEO","public_port":40124},
                 {"role":"SSH","public_port":22113}]"""));
        p.putNull("image_digest");
        p.put("price_per_hour_usd", 2.99);
        String result = mvc.perform(post(GpuPaths.SESSIONS).header(HttpHeaders.AUTHORIZATION, "Bearer " + bootSecret)
                        .contentType(MediaType.APPLICATION_JSON).content(mapper.writeValueAsString(root)))
                .andExpect(status().isCreated()).andReturn().getResponse().getContentAsString(StandardCharsets.UTF_8);
        return mapper.readTree(result).get("payload");
    }

    private String reportBody(UUID sessionId, String stage) {
        ObjectNode root = mapper.createObjectNode();
        root.put("schema_version", "1.0.0");
        root.put("msg_type", "gpu_session_report");
        root.put("msg_id", UUID.randomUUID().toString());
        root.put("sent_at", Instant.now().toEpochMilli());
        root.put("source_id", bootId);
        ObjectNode p = root.putObject("payload");
        p.put("session_id", sessionId.toString());
        p.put("stage", stage);
        p.putNull("reason_code");
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
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        int code = commands.execute(List.of(args), new CommandIo(print, print, prompt -> null));
        assertThat(code).isZero();
        Matcher matcher = TOKEN.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        return matcher.group(1);
    }
}
