package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
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
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 3D 배경(Scene) 재생성 REST API 및 상태 전이 통합 테스트(S15P31A101-428).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SceneRegenerateIntegrationTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");

    @Autowired
    MockMvc mvc;

    @Autowired
    MachineTokenService machines;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ContractSchemas schemas;

    @MockitoBean
    ObjectStorage storage;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String workerId = "worker-regen-" + tag;
    private final String calibrated = "cal-428-" + tag;
    private final List<String> createdSceneVersions = new ArrayList<>();
    private final List<String> createdBundles = new ArrayList<>();
    private long adminId;
    private String siteId;
    private String workerToken;

    @BeforeEach
    void setUp() {
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(i -> "s3://simai-test-bucket/" + i.getArgument(0));
        when(storage.size(anyString())).thenAnswer(i -> {
            String key = i.getArgument(0);
            return key.endsWith(".glb") ? java.util.OptionalLong.of(48213004L) : java.util.OptionalLong.of(412337L);
        });
        jdbc.update("DELETE FROM scene_regenerate_request");
        jdbc.update("DELETE FROM job_lease WHERE job_id IN (SELECT job_id FROM analysis_job WHERE job_type = 'scene_regenerate')");
        jdbc.update("DELETE FROM analysis_job WHERE job_type = 'scene_regenerate'");

        siteId = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        adminId = jdbc.queryForObject(
                "INSERT INTO app_user (login_id, password_hash, role, created_at) VALUES (?, 'x', 'ADMIN', now()) RETURNING user_id",
                Long.class,
                "regen-admin-" + tag);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'regen test', now(), 's3://test/cal.json', ?, now())
                """, calibrated, siteId, "sha256:" + "a".repeat(64));
        workerToken = machines.registerWorker(workerId, List.of("scene_regenerate", "static_reconstruct")).token();
    }

    @AfterEach
    void tearDown() {
        jdbc.update("DELETE FROM scene_regenerate_request");
        for (String v : createdSceneVersions) {
            jdbc.update("DELETE FROM scene_activation WHERE scene_version = ?", v);
            jdbc.update("DELETE FROM scene_version WHERE scene_version = ?", v);
        }
        jdbc.update("DELETE FROM job_lease WHERE worker_id = ?", workerId);
        jdbc.update("DELETE FROM analysis_job WHERE job_type = 'scene_regenerate'");
        for (String b : createdBundles) {
            jdbc.update("DELETE FROM job_lease WHERE job_id IN (SELECT job_id FROM analysis_job WHERE input->>'clean_background_bundle_id' = ?)", b);
            jdbc.update("DELETE FROM analysis_job WHERE input->>'clean_background_bundle_id' = ?", b);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibrated);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", workerId);
        jdbc.update("DELETE FROM app_user WHERE user_id = ?", adminId);
    }

    @Test
    void 권한_및_입력_검증과_빈_최신_조회() throws Exception {
        // 미인증 401, VIEWER·FOREMAN 은 POST 403
        mvc.perform(post("/api/scenes/regenerate"))
                .andExpect(status().isUnauthorized());
        mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("FOREMAN")))
                .andExpect(status().isForbidden());

        // 아직 요청이 없을 때 latest=1 은 204 (VIEWER 도 조회 가능)
        mvc.perform(get("/api/scenes/regenerate").param("latest", "1")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isNoContent());

        // 없는 request_id 는 404
        mvc.perform(get("/api/scenes/regenerate/999999")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("REGENERATE_NOT_FOUND"));

        // reason 120자 초과 및 잘못된 필드는 400
        ObjectNode tooLong = mapper.createObjectNode().put("reason", "가".repeat(121));
        mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(tooLong.toString()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_REGENERATE_REQUEST"));

        ObjectNode extraField = mapper.createObjectNode().put("reason", "정상").put("extra", 1);
        mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(extraField.toString()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_REGENERATE_REQUEST"));
    }

    @Test
    void 전체_상태_전이_REQUESTED_CAPTURING_RECONSTRUCTING_CANDIDATE_READY_및_활성화() throws Exception {
        // 1. 관리자 재생성 요청 -> 202 REQUESTED
        ObjectNode reqBody = mapper.createObjectNode().put("reason", "자재 반입");
        JsonNode created = body(mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(reqBody.toString()))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.status").value("REQUESTED"))
                .andReturn());
        long requestId = created.get("request_id").longValue();
        assertThat(requestId).isPositive();

        // 진행 중 중복 요청 시 409 REGENERATE_IN_PROGRESS + request_id 반환
        mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN"))
                        .contentType(MediaType.APPLICATION_JSON)
                        .content("{}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("REGENERATE_IN_PROGRESS"))
                .andExpect(jsonPath("$.request_id").value(requestId));

        // 2. 워커가 scene_regenerate 잡을 대여하면 CAPTURING 으로 전이
        JsonNode leasedEnvelope = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(leaseRequest(List.of("scene_regenerate")).toString()))
                .andExpect(status().isOk())
                .andReturn());
        assertThat(leasedEnvelope.path("schema_version").asString()).isEqualTo("3.8.0");
        assertThat(schemas.violations("messages/job_request", leasedEnvelope.toString())).isEmpty();
        JsonNode exampleRequest = mapper.readTree(Files.readString(EXAMPLES.resolve("job_request.scene_regenerate.json")));
        assertThat(leasedEnvelope.at("/payload/job_type")).isEqualTo(exampleRequest.at("/payload/job_type"));
        JsonNode leasedPayload = leasedEnvelope.get("payload");
        String regenJobId = leasedPayload.get("job_id").stringValue();
        String regenLeaseId = leasedPayload.get("lease_id").stringValue();
        assertThat(leasedPayload.get("job_type").stringValue()).isEqualTo("scene_regenerate");
        assertThat(leasedPayload.get("priority").stringValue()).isEqualTo("BACKGROUND");
        assertThat(leasedPayload.at("/input/request_id").longValue()).isEqualTo(requestId);
        assertThat(leasedPayload.at("/input/reason").stringValue()).isEqualTo("자재 반입");
        assertThat(leasedPayload.get("input").has("requested_by")).isFalse();

        mvc.perform(get("/api/scenes/regenerate/" + requestId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.request_id").value(requestId))
                .andExpect(jsonPath("$.status").value("CAPTURING"))
                .andExpect(jsonPath("$.failure_reason").isEmpty())
                .andExpect(jsonPath("$.scene_version").isEmpty());

        // 3. 워커가 static-reconstruct 에 request_id 를 실어 호출하면 RECONSTRUCTING 으로 전이
        String bundle = "bundle-" + UUID.randomUUID().toString().replace("-", "");
        createdBundles.add(bundle);
        JsonNode submitRes = body(mvc.perform(post("/api/worker/scene-jobs/static-reconstruct")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(staticReconstructSubmit(bundle, requestId).toString()))
                .andExpect(status().isCreated())
                .andReturn());
        String reconstructJobId = submitRes.at("/payload/job_id").stringValue();

        mvc.perform(get("/api/scenes/regenerate/" + requestId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("RECONSTRUCTING"));

        // 워커가 scene_regenerate 잡 성공 콜백 제출
        ObjectNode callback = (ObjectNode) mapper.readTree(Files.readString(EXAMPLES.resolve("job_callback.scene_regenerate.json")));
        callback.put("source_id", workerId);
        ObjectNode callbackPayload = (ObjectNode) callback.get("payload");
        callbackPayload.put("job_id", regenJobId).put("lease_id", regenLeaseId);
        ObjectNode regenResult = (ObjectNode) callbackPayload.get("result");
        regenResult.put("bundle_id", bundle).put("static_reconstruct_job_id", reconstructJobId);
        assertThat(schemas.violations("messages/job_callback", callback.toString())).isEmpty();

        // 계약의 패턴과 닫힌 결과 객체를 우회해 제출할 수 없다.
        regenResult.put("bundle_id", "invalid");
        mvc.perform(post("/api/worker/jobs/" + regenJobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON).content(callback.toString()))
                .andExpect(status().isBadRequest());
        regenResult.put("bundle_id", bundle).put("extra", "invalid");
        mvc.perform(post("/api/worker/jobs/" + regenJobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON).content(callback.toString()))
                .andExpect(status().isBadRequest());
        regenResult.remove("extra");
        regenResult.put("bundle_id", "bundle-" + "b".repeat(32));
        mvc.perform(post("/api/worker/jobs/" + regenJobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON).content(callback.toString()))
                .andExpect(status().isBadRequest());
        regenResult.put("bundle_id", bundle);
        mvc.perform(post("/api/worker/jobs/" + regenJobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON).content(callback.toString()))
                .andExpect(status().isOk());

        // 4. 워커가 static_reconstruct 잡을 대여하고 완료하면 CANDIDATE_READY + scene_version 기록
        JsonNode reconLeased = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(leaseRequest(List.of("static_reconstruct")).toString()))
                .andExpect(status().isOk())
                .andReturn());
        String reconLeaseId = reconLeased.at("/payload/lease_id").stringValue();
        assertThat(reconLeased.at("/payload/job_id").stringValue()).isEqualTo(reconstructJobId);

        String candidateVersion = "scene-428-" + tag;
        createdSceneVersions.add(candidateVersion);
        ObjectNode sceneOut = sceneOutput(candidateVersion, reconstructJobId, reconLeaseId);
        mvc.perform(post("/api/worker/jobs/" + reconstructJobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(jobCallback(reconstructJobId, reconLeaseId, "static_reconstruct", "SUCCEEDED", sceneOut, null).toString()))
                .andExpect(status().isOk());

        mvc.perform(get("/api/scenes/regenerate?latest=1")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.request_id").value(requestId))
                .andExpect(jsonPath("$.status").value("CANDIDATE_READY"))
                .andExpect(jsonPath("$.scene_version").value(candidateVersion))
                .andExpect(jsonPath("$.failure_reason").isEmpty());

        // 5. 후보 Scene 활성화(commit) 연동 확인
        mvc.perform(post("/api/scenes/" + candidateVersion + "/commit")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.state").value("COMMITTED"));
    }

    @Test
    void 워커_실패_사유_기록_및_10분_미임대_TIMEOUT_처리() throws Exception {
        // 1. 워커가 BACKGROUND_NOT_READY 로 실패 보고 -> FAILED(BACKGROUND_NOT_READY)
        JsonNode first = body(mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN")))
                .andExpect(status().isAccepted())
                .andReturn());
        long firstId = first.get("request_id").longValue();

        JsonNode leased = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(leaseRequest(List.of("scene_regenerate")).toString()))
                .andExpect(status().isOk())
                .andReturn());
        String jobId = leased.at("/payload/job_id").stringValue();
        String leaseId = leased.at("/payload/lease_id").stringValue();

        ObjectNode error = mapper.createObjectNode()
                .put("code", "BACKGROUND_NOT_READY")
                .put("message", "사람 없는 프레임이 부족합니다")
                .put("retryable", false);
        mvc.perform(post("/api/worker/jobs/" + jobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(jobCallback(jobId, leaseId, "scene_regenerate", "FAILED", null, error).toString()))
                .andExpect(status().isOk());

        mvc.perform(get("/api/scenes/regenerate/" + firstId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.failure_reason").value("BACKGROUND_NOT_READY"));

        // 2. FAILED 후에는 새 요청 가능. 10분 이상 REQUESTED 상태로 방치되면 FAILED(TIMEOUT) 처리
        JsonNode second = body(mvc.perform(post("/api/scenes/regenerate")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("ADMIN")))
                .andExpect(status().isAccepted())
                .andReturn());
        long secondId = second.get("request_id").longValue();

        jdbc.update("UPDATE scene_regenerate_request SET created_at = now() - interval '11 minutes' WHERE id = ?", secondId);

        mvc.perform(get("/api/scenes/regenerate/" + secondId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + userToken("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.failure_reason").value("TIMEOUT"));
    }

    private ObjectNode leaseRequest(List<String> jobTypes) {
        ObjectNode payload = mapper.createObjectNode().put("wait_ms", 0);
        ArrayNode arr = payload.putArray("job_types");
        jobTypes.forEach(arr::add);
        return workerEnvelope("job_lease_request", "2.0.0", payload);
    }

    private ObjectNode jobCallback(String jobId, String leaseId, String jobType, String status, JsonNode result, JsonNode error) {
        long now = System.currentTimeMillis();
        ObjectNode payload = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", jobType)
                .put("status", status)
                .put("started_at", now - 1000)
                .put("finished_at", now);
        if (result != null) {
            payload.set("result", result);
        }
        if (error != null) {
            payload.set("error", error);
        }
        return workerEnvelope("job_callback", "scene_regenerate".equals(jobType) ? "2.5.0" : "2.1.0", payload);
    }

    private ObjectNode staticReconstructSubmit(String bundleId, long requestId) throws Exception {
        ObjectNode in = (ObjectNode) mapper.readTree(Files.readString(EXAMPLES.resolve("static_reconstruct_submit.json")))
                .at("/payload/input");
        in.put("calibration_version", calibrated);
        ((ObjectNode) in.get("input_provenance").get("calibration")).put("calibration_version", calibrated);
        ObjectNode payload = mapper.createObjectNode()
                .put("bundle_id", bundleId)
                .put("request_id", requestId);
        payload.set("input", in);
        return workerEnvelope("static_reconstruct_submit", "1.0.0", payload);
    }

    private ObjectNode sceneOutput(String sceneVersion, String jobId, String leaseId) throws Exception {
        ObjectNode out = (ObjectNode) mapper.readTree(Files.readString(EXAMPLES.resolve("scene_output.json")));
        out.put("scene_version", sceneVersion);
        out.put("calibration_version", calibrated);
        ((ObjectNode) out.get("coordinate_frame")).put("site_id", siteId);
        ((ObjectNode) out.get("input_provenance").get("calibration")).put("calibration_version", calibrated);
        String prefix = storage.uri("static-3d/" + jobId + "/" + leaseId + "/");
        ArrayNode assets = (ArrayNode) out.get("assets");
        ((ObjectNode) assets.get(0)).put("uri", prefix + "scene.glb");
        ((ObjectNode) assets.get(1)).put("uri", prefix + "surface_source_0000.png");
        return out;
    }

    private ObjectNode workerEnvelope(String msgType, String version, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode()
                .put("schema_version", version)
                .put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString())
                .put("sent_at", System.currentTimeMillis())
                .put("source_id", workerId);
        env.set("payload", payload);
        return env;
    }

    private String userToken(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder()
                .issuer(JwtConfig.ISSUER)
                .subject(String.valueOf(adminId))
                .issuedAt(now)
                .expiresAt(now.plusSeconds(300))
                .claim(AccessTokenIssuer.ROLE_CLAIM, role)
                .build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }
}
