package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;


import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDate;
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
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.RequestPostProcessor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 세팅 세션 REST 및 I5 {@code site_setup} 계약·생명주기 통합 시험(S15P31A101-452).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupSessionIntegrationTest {

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    MachineTokenService machines;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    SetupQueueTimeout queueTimeout;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String workerA = "worker-setup-a-" + suffix;
    private final String workerB = "worker-setup-b-" + suffix;
    private final String calibration = "cal-setup-" + suffix;
    private final String previewScene = "scene-preview-" + suffix;
    private final String bakedScene = "scene-baked-" + suffix;
    private final String configVer = "cfg-setup-" + suffix;

    private String site;
    private long userId;
    private long workUnitId;
    private String tokenA;
    private String tokenB;

    @BeforeEach
    void setUp() {
        cleanUpTables();
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        jdbc.update("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '세팅관리자', 'ADMIN', now())
                """, "setup-admin-" + suffix);
        userId = jdbc.queryForObject("SELECT user_id FROM app_user WHERE login_id = ?", Long.class, "setup-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "세팅작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "세팅작업-" + suffix);

        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'PROVISIONAL', 'G1_AUTO', 'site_setup', now(), 's3://test/cal.json', ?, now())
                """, calibration, site, "sha256:" + "b".repeat(64));
        for (String sv : List.of(previewScene, bakedScene)) {
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                        input_provenance, is_upper_bound, job_id, scene_output, requested_by)
                    VALUES (?, ?, ?, 'CANDIDATE', 'static-3d/test/scene.glb', '{}'::jsonb, '{}'::jsonb, false, NULL, '{}'::jsonb, ?)
                    """, sv, site, calibration, userId);
        }
        jdbc.update("""
                INSERT INTO config_bundle (config_version, previous_config_version, site_id, issued_at, issued_by,
                    bundle_uri, bundle_hash, components, calibration_version, rule_profile)
                VALUES (?, NULL, ?, now(), ?, 'https://test/cfg', ?, '{}'::jsonb, ?, 'OPERATION')
                """, configVer, site, userId, "sha256:" + "c".repeat(64), calibration);
    }

    @AfterEach
    void tearDown() {
        cleanUpTables();
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
    }

    private void cleanUpTables() {
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE active_setup_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_session");
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", configVer);
        jdbc.update("DELETE FROM scene_version WHERE scene_version IN (?, ?)", previewScene, bakedScene);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "세팅작업-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "setup-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id IN (?, ?)", workerA, workerB);
    }

    @Test
    void 기본값은_제출_전에_null_이고_워커_부재로_세션이_거절돼도_제출한_높이는_기본값으로_남는다() throws Exception {
        // 권한 확인: 미인증 401, VIEWER 403
        mvc.perform(get("/api/setup-sessions/camera-height-defaults")).andExpect(status().isUnauthorized());
        mvc.perform(get("/api/setup-sessions/camera-height-defaults").with(viewerJwt())).andExpect(status().isForbidden());

        // 한 번도 제출된 적 없으면 카메라별 높이는 null(기본값을 임의로 꾸며내지 않음)
        JsonNode initial = body(mvc.perform(get("/api/setup-sessions/camera-height-defaults").with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.site_id").value(site))
                .andExpect(jsonPath("$.height_tolerance_cm").value(5.0))
                .andExpect(jsonPath("$.all_present").value(false))
                .andReturn());
        assertThat(initial.at("/camera_heights_m/cam1").isNull()).isTrue();
        assertThat(initial.at("/camera_heights_m/cam2").isNull()).isTrue();
        assertThat(initial.at("/camera_heights_m/cam3").isNull()).isTrue();
        assertThat(initial.at("/camera_heights_m/cam4").isNull()).isTrue();

        // 기본값도 없고 요청에도 높이가 없으면 400 CAMERA_HEIGHT_REQUIRED
        ObjectNode emptyReq = mapper.createObjectNode().put("work_unit_id", workUnitId);
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(emptyReq.toString()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("CAMERA_HEIGHT_REQUIRED"));

        // 0 이하 높이 또는 존재하지 않는 카메라 ID는 400 INVALID_SETUP_REQUEST(저장되지 않음)
        ObjectNode zeroReq = mapper.createObjectNode().put("work_unit_id", workUnitId);
        zeroReq.putObject("camera_heights_m").put("cam1", 0.0).put("cam2", 2.4).put("cam3", 2.4).put("cam4", 2.4);
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(zeroReq.toString()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_REQUEST"));

        // 유효한 높이를 제출했으나 활성 site_setup 워커가 없으면 409 SETUP_WORKER_UNAVAILABLE 이지만 제출한 높이는 기본값으로 저장된다
        ObjectNode validReq = createRequest(2.42, 2.48, 2.45, 2.51);
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(validReq.toString()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_WORKER_UNAVAILABLE"));

        JsonNode savedDefaults = body(mvc.perform(get("/api/setup-sessions/camera-height-defaults").with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.all_present").value(true))
                .andReturn());
        assertThat(savedDefaults.at("/camera_heights_m/cam1").doubleValue()).isEqualTo(2.42);
        assertThat(savedDefaults.at("/camera_heights_m/cam2").doubleValue()).isEqualTo(2.48);
        assertThat(savedDefaults.at("/camera_heights_m/cam3").doubleValue()).isEqualTo(2.45);
        assertThat(savedDefaults.at("/camera_heights_m/cam4").doubleValue()).isEqualTo(2.51);

        // 이제 워커를 등록하고 camera_heights_m 없이(저장된 기본값 사용) 세션을 시작하면 202 Accepted
        registerWorkers();
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(emptyReq.toString()))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.status").value("COLLECTING_BACKGROUND"))
                .andExpect(jsonPath("$.progress_pct").value(0.0))
                .andReturn());
        String sessionId = created.get("setup_session_id").asString();
        assertThat(created.at("/camera_heights_m/cam1").doubleValue()).isEqualTo(2.42);

        // 같은 현장에 진행 중인 세션이 있으면 두 번째 생성은 409 SETUP_ALREADY_IN_PROGRESS
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(emptyReq.toString()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ALREADY_IN_PROGRESS"))
                .andExpect(jsonPath("$.setup_session_id").value(sessionId))
                .andExpect(jsonPath("$.work_id").value(String.valueOf(workUnitId)));

        // 작업 단위 조회(GET /api/work-units/{id}, GET /api/work-units)에 active_setup_session_id 가 포함된다
        mvc.perform(get("/api/work-units/{id}", workUnitId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.active_setup_session_id").value(sessionId));
    }

    @Test
    void 관리자_취소는_세션을_FAILED_로_닫고_워커_하트비트는_JOB_CANCELLED_를_받고_새_세팅이_가능하다() throws Exception {
        registerWorkers();
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted()).andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String jobId = created.get("job_id").asString();
        String lease = body(mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk()).andReturn())
                .at("/payload/lease_id").asString();

        // 관리자만 취소할 수 있다
        mvc.perform(post("/api/setup-sessions/{id}/cancel", sessionId).with(viewerJwt())).andExpect(status().isForbidden());
        mvc.perform(post("/api/setup-sessions/{id}/cancel", sessionId).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.failure_code").value("CANCELLED_BY_ADMIN"));
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, jobId))
                .isEqualTo("CANCELLED");

        // 워커의 다음 하트비트는 취소를 받는다
        ObjectNode snap = progressSnapshot(sessionId, "COLLECTING_BACKGROUND", 25.0);
        String hb = mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, lease, 0.25, snap))
                .andReturn().getResponse().getContentAsString();
        assertThat(hb).contains("JOB_CANCELLED");

        // 끝난 세션은 다시 취소할 수 없고, 같은 현장에 새 세팅을 시작할 수 있다
        mvc.perform(post("/api/setup-sessions/{id}/cancel", sessionId).with(adminJwt()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_CANCEL_NOT_ALLOWED"));
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted());
    }

    @Test
    void 워커_하트비트_진행률과_높이검사_재할당_실패후_재시도_및_완료까지_계약에_맞게_전이한다() throws Exception {
        registerWorkers();
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted())
                .andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String jobId = created.get("job_id").asString();

        // 워커 대여: job_request 가 contracts/messages/job_request 스키마를 통과한다
        JsonNode leaseEnvelope = body(mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_request", leaseEnvelope.toString())).isEmpty();
        JsonNode leasePayload = leaseEnvelope.get("payload");
        assertThat(leasePayload.get("job_id").asString()).isEqualTo(jobId);
        assertThat(leasePayload.get("job_type").asString()).isEqualTo("site_setup");
        String leaseA = leasePayload.get("lease_id").asString();

        // 1) COLLECTING_BACKGROUND 하트비트
        ObjectNode snap1 = progressSnapshot(sessionId, "COLLECTING_BACKGROUND", 25.0);
        snap1.putObject("background_collection_pct")
                .put("cam1", 100.0).put("cam2", 80.0).put("cam3", 90.0).put("cam4", 75.0);
        mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, leaseA, 0.25, snap1))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACTIVE"));

        // 2) JOINT_BA_AND_VERIFY 하트비트 + 높이 검사(cam2 가 +7.0 cm 로 허용치 5 cm 초과) + 삼각콘·개구부 표지판 교차 확인
        ObjectNode snap2 = progressSnapshot(sessionId, "JOINT_BA_AND_VERIFY", 55.0);
        snap2.putObject("background_collection_pct")
                .put("cam1", 100.0).put("cam2", 100.0).put("cam3", 100.0).put("cam4", 100.0);
        snap2.set("height_checks", heightChecks(2.42, 2.40, -2.0, true,
                2.48, 2.55, 7.0, false,
                2.45, 2.46, 1.0, true,
                2.51, 2.49, -2.0, true));
        ArrayNode crossChecks = snap2.putArray("object_cross_checks");
        crossChecks.addObject().put("kind", "TRAFFIC_CONE").put("passed", true)
                .put("message", "삼각콘 2개 바닥 투영 일치");
        crossChecks.addObject().put("kind", "OPENING_SIGN").put("passed", true)
                .put("message", "개구부 표지판 1개 바닥면 인접 확인");
        mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, leaseA, 0.55, snap2))
                .andExpect(status().isOk());

        // VIEWER 권한으로 GET /api/setup-sessions/{id} 조회 시 높이 경고 안내 문구와 교차 확인 확인
        JsonNode midView = body(mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("JOINT_BA_AND_VERIFY"))
                .andExpect(jsonPath("$.progress_pct").value(55.0))
                .andExpect(jsonPath("$.height_checks.cam2.within_tolerance").value(false))
                .andReturn());
        assertThat(midView.at("/height_checks/cam2/guidance").asString())
                .isEqualTo("카메라 cam2 높이가 7.0 cm 안 맞음 — 거치대 확인");
        assertThat(midView.get("height_warnings")).hasSize(1);
        assertThat(midView.at("/object_cross_checks/0/kind").asString()).isEqualTo("TRAFFIC_CONE");

        // 같은 대여(leaseA)에서 이전 단계(COLLECTING_BACKGROUND)나 낮은 진행률(40%)로 역행하면 400 으로 거절되고 상태는 유지된다
        ObjectNode regressSnap = progressSnapshot(sessionId, "COLLECTING_BACKGROUND", 40.0);
        mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, leaseA, 0.40, regressSnap))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // 높이 차이 계산이 틀린(difference_cm 불일치) 스냅샷도 400 으로 거절된다
        ObjectNode badHeightSnap = progressSnapshot(sessionId, "PREVIEW_AND_VLM_DRAFT", 65.0);
        badHeightSnap.set("height_checks", heightChecks(2.42, 2.40, 99.0, true,
                2.48, 2.55, 7.0, false,
                2.45, 2.46, 1.0, true,
                2.51, 2.49, -2.0, true));
        mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, leaseA, 0.65, badHeightSnap))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // 대여 만료 후 workerB 로 자동 재할당되면 새 대여의 첫 스냅샷부터 다시 시작할 수 있다
        jdbc.update("UPDATE job_lease SET lease_expires_at = now() - interval '1 second' WHERE lease_id = ?::uuid", leaseA);
        JsonNode reassigned = body(mvc.perform(leaseRequest(tokenB, workerB)).andExpect(status().isOk()).andReturn());
        String leaseB = reassigned.at("/payload/lease_id").asString();
        assertThat(leaseB).isNotEqualTo(leaseA);

        ObjectNode restartSnap = progressSnapshot(sessionId, "INITIAL_POSE_AND_WORLD", 30.0);
        mvc.perform(heartbeatWithSetup(tokenB, workerB, jobId, leaseB, 0.30, restartSnap))
                .andExpect(status().isOk());

        // 진행 중인 세션은 retry 할 수 없다 (409 SETUP_RETRY_NOT_ALLOWED)
        mvc.perform(post("/api/setup-sessions/{id}/retry", sessionId).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_RETRY_NOT_ALLOWED"));

        // 워커가 최종 실패(retryable=false)를 보고하면 세션이 FAILED 가 되고 실패 사유가 기록되지만 기본 높이는 보존된다
        mvc.perform(submitCallback(tokenB, workerB, failedCallback(jobId, leaseB, "BA_DIVERGED", "카메라 cam2 거치대 흔들림")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.failure_code").value("BA_DIVERGED"))
                .andExpect(jsonPath("$.failure_message").value("카메라 cam2 거치대 흔들림"));

        mvc.perform(get("/api/setup-sessions/camera-height-defaults").with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.camera_heights_m.cam2").value(2.48));

        // 실패한 세션을 cam2 높이만 2.55 m 로 고쳐 retry 하면 새 세션이 만들어지고 기본값도 2.55 m 로 갱신된다
        ObjectNode retryBody = mapper.createObjectNode();
        retryBody.putObject("camera_heights_m").put("cam2", 2.55);
        JsonNode retried = body(mvc.perform(post("/api/setup-sessions/{id}/retry", sessionId).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(retryBody.toString()))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.retry_of").value(sessionId))
                .andExpect(jsonPath("$.status").value("COLLECTING_BACKGROUND"))
                .andReturn());
        String retrySessionId = retried.get("setup_session_id").asString();
        String retryJobId = retried.get("job_id").asString();
        assertThat(retried.at("/camera_heights_m/cam1").doubleValue()).isEqualTo(2.42);
        assertThat(retried.at("/camera_heights_m/cam2").doubleValue()).isEqualTo(2.55);

        // 재시도 작업을 대여받아 READY_FOR_ZONES → COMPLETED 까지 완료
        String retryLease = body(mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk()).andReturn())
                .at("/payload/lease_id").asString();

        ObjectNode readySnap = progressSnapshot(retrySessionId, "READY_FOR_ZONES", 72.0);
        readySnap.put("calibration_version", calibration)
                .put("preview_scene_version", previewScene)
                .put("zone_drafts_ref", "s3://simai-test-bucket/site-setup/" + retryJobId + "/zone_drafts.json");
        readySnap.set("height_checks", heightChecks(2.42, 2.40, -2.0, true,
                2.55, 2.55, 0.0, true,
                2.45, 2.46, 1.0, true,
                2.51, 2.49, -2.0, true));
        mvc.perform(heartbeatWithSetup(tokenA, workerA, retryJobId, retryLease, 0.72, readySnap))
                .andExpect(status().isOk());

        JsonNode readyView = body(mvc.perform(get("/api/setup-sessions/{id}", retrySessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("READY_FOR_ZONES"))
                .andExpect(jsonPath("$.calibration_version").value(calibration))
                .andExpect(jsonPath("$.preview_scene_version").value(previewScene))
                .andExpect(jsonPath("$.height_warnings").isEmpty())
                .andReturn());
        assertThat(readyView.get("ready_at_ms").isNumber()).isTrue();

        // 완료 콜백(COMPLETED)
        ObjectNode completedResult = progressSnapshot(retrySessionId, "COMPLETED", 100.0);
        completedResult.put("calibration_version", calibration)
                .put("preview_scene_version", previewScene)
                .put("scene_version", bakedScene)
                .put("config_version", configVer)
                .put("zone_drafts_ref", "s3://simai-test-bucket/site-setup/" + retryJobId + "/zone_drafts.json");
        completedResult.set("height_checks", heightChecks(2.42, 2.40, -2.0, true,
                2.55, 2.55, 0.0, true,
                2.45, 2.46, 1.0, true,
                2.51, 2.49, -2.0, true));
        mvc.perform(submitCallback(tokenA, workerA, succeededCallback(retryJobId, retryLease, completedResult)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        JsonNode finalView = body(mvc.perform(get("/api/setup-sessions/{id}", retrySessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.progress_pct").value(100.0))
                .andExpect(jsonPath("$.scene_version").value(bakedScene))
                .andExpect(jsonPath("$.config_version").value(configVer))
                .andReturn());
        assertThat(finalView.get("completed_at_ms").isNumber()).isTrue();
        // 다른 구역의 생성이나 저장만으로는 발행된 세팅 구역으로 추정하지 않는다.
        assertThat(finalView.get("zones_committed_at").isNull()).isTrue();
        String zoneId = "zone-474-" + suffix;
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                VALUES (?, 1, ?, '발행구역', 'RESTRICTED', 'GROUND_FOOTPRINT',
                        ST_GeomFromText('POLYGON((0 0,1 0,1 1,0 0))', 0), 'HIGH', now())
                """, zoneId, site);
        try {
            mvc.perform(get("/api/setup-sessions/{id}", retrySessionId).with(viewerJwt()))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.zones_committed_at").doesNotExist());
            jdbc.update("INSERT INTO setup_session_zone(setup_session_id,zone_id,zone_version,created_at) VALUES (?,?,1,now())",
                    UUID.fromString(retrySessionId), zoneId);
            mvc.perform(get("/api/setup-sessions/{id}", retrySessionId).with(viewerJwt()))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.zones_committed_at").doesNotExist());
            jdbc.update("INSERT INTO config_bundle_item(config_version,item_type,item_id,item_version) VALUES (?,'ZONE',?,1)", configVer, zoneId);
            mvc.perform(get("/api/setup-sessions/{id}", retrySessionId).with(viewerJwt()))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.zones_committed_at").isNumber());
        } finally {
            jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ? AND item_id = ?", configVer, zoneId);
            jdbc.update("DELETE FROM setup_session_zone WHERE setup_session_id = ? AND zone_id = ?", UUID.fromString(retrySessionId), zoneId);
            jdbc.update("DELETE FROM zone WHERE zone_id = ?", zoneId);
        }

        // 자동 활성화 금지 확인: site.established_calibration_version 과 scene_activation 은 바뀌지 않는다
        assertThat(jdbc.queryForObject("SELECT established_calibration_version FROM site WHERE site_id = ?", String.class, site))
                .isNull();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_activation WHERE site_id = ?", Integer.class, site))
                .isZero();

        // work_unit.active_setup_session_id 는 완료된 재시도 세션을 가리킨다
        mvc.perform(get("/api/work-units/{id}", workUnitId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.active_setup_session_id").value(retrySessionId));
    }

    @Test
    void 굽는_중_작업이_시작되면_하트비트가_회수를_알리고_정식_Scene_없이_완료된다() throws Exception {
        registerWorkers();
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted()).andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String jobId = created.get("job_id").asString();
        String lease = body(mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk()).andReturn())
                .at("/payload/lease_id").asString();

        // 작업 전: site_setup 대여의 ACTIVE 답은 짧은 주기와 live_work_active false 를 싣고 계약(job_lease_status 2.1.0)을 지킨다
        ObjectNode readySnap = progressSnapshot(sessionId, "READY_FOR_ZONES", 85.0);
        readySnap.put("calibration_version", calibration).put("preview_scene_version", previewScene);
        readySnap.set("height_checks", heightChecks(2.42, 2.40, -2.0, true,
                2.48, 2.48, 0.0, true,
                2.45, 2.46, 1.0, true,
                2.51, 2.49, -2.0, true));
        JsonNode before = body(mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, lease, 0.85, readySnap))
                .andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_lease_status", before.toString())).isEmpty();
        assertThat(before.at("/schema_version").asString()).isEqualTo("2.1.0");
        assertThat(before.at("/payload/live_work_active").booleanValue()).isFalse();
        assertThat(before.at("/payload/heartbeat_interval_ms").intValue()).isEqualTo(5_000);

        // 굽는 동안 같은 현장 작업이 시작되면(IN_PROGRESS) 다음 하트비트가 회수 신호를 싣는다
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now(), actual_ended_at = NULL WHERE work_id = ?",
                workUnitId);
        ObjectNode bakingSnap = readySnap.deepCopy();
        bakingSnap.put("status", "BAKING_TEXTURE").put("progress_pct", 95.0);
        JsonNode during = body(mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, lease, 0.95, bakingSnap))
                .andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_lease_status", during.toString())).isEmpty();
        assertThat(during.at("/payload/live_work_active").booleanValue()).isTrue();

        // 정식 Scene 을 비우면서 이유가 없으면 계약 위반으로 거절된다. submitCallback 은 보내기 전에 계약을 단언하므로
        // 일부러 어긴 이 메시지는 직접 보낸다(계약 위반임을 먼저 확인)
        ObjectNode noReason = readySnap.deepCopy();
        noReason.put("status", "COMPLETED").put("progress_pct", 100.0).putNull("scene_version");
        ObjectNode badEnv = envelope("job_callback", "2.6.0", workerA, succeededCallback(jobId, lease, noReason));
        assertThat(schemas.violations("messages/job_callback", badEnv.toString())).isNotEmpty();
        mvc.perform(post("/api/worker/jobs/" + jobId + "/result")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + tokenA)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(badEnv.toString()))
                .andExpect(status().isBadRequest());

        // 회수 결과(bake_skipped LIVE_WORK_STARTED, scene_version null)는 받고 세션은 COMPLETED, 정식 Scene 없음
        ObjectNode skipped = noReason.deepCopy();
        skipped.put("bake_skipped", "LIVE_WORK_STARTED");
        mvc.perform(submitCallback(tokenA, workerA, succeededCallback(jobId, lease, skipped)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));
        mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.bake_skipped").value("LIVE_WORK_STARTED"))
                .andExpect(jsonPath("$.scene_version").doesNotExist())
                .andExpect(jsonPath("$.preview_scene_version").value(previewScene));
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
    }

    @Test
    void 세팅_조회는_기다리는_이유를_있는_상태에서만_고른다() throws Exception {
        registerWorkers();
        String gateway = "gw-583-" + suffix;
        try {
            JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                            .contentType(MediaType.APPLICATION_JSON)
                            .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                    .andExpect(status().isAccepted()).andReturn());
            String sessionId = created.get("setup_session_id").asString();
            String jobId = created.get("job_id").asString();
            assertThat(created.at("/wait_reason/since_ms").isNumber()).isTrue();

            // 1) 워커가 방금 대여를 물었다 → 곧 가져간다
            jdbc.update("UPDATE worker SET last_seen_at = now() WHERE worker_id IN (?, ?)", workerA, workerB);
            JsonNode polling = body(mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                    .andExpect(status().isOk()).andReturn());
            assertThat(polling.at("/wait_reason/code").asString()).isEqualTo("WAITING_FOR_WORKER");
            assertThat(polling.at("/wait_reason/worker_last_seen_ms").isNumber()).isTrue();

            // 2) 워커가 10분째 묻지 않고, Tier 0 health 가 지연을 모른다(UNSAFE_UNKNOWN) → health 근거 접수 중지 추정
            jdbc.update("UPDATE worker SET last_seen_at = now() - interval '10 minutes' WHERE worker_id IN (?, ?)",
                    workerA, workerB);
            jdbc.update("""
                    INSERT INTO system_health (reported_at, gateway_id, msg_id, system_health, pipeline_lag_ms,
                        calibration_state, cameras, received_at)
                    VALUES (now(), ?, ?, 'UNSAFE_UNKNOWN', NULL, 'CALIBRATED', '[]'::jsonb, now())
                    """, gateway, UUID.randomUUID());
            JsonNode paused = body(mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                    .andExpect(status().isOk()).andReturn());
            assertThat(paused.at("/wait_reason/code").asString()).isEqualTo("WORKER_PAUSED_TIER0");
            assertThat(paused.at("/wait_reason/basis").asString()).isEqualTo("TIER0_HEALTH");
            assertThat(paused.at("/wait_reason/worker_last_seen_ms").isNumber()).isTrue();
            assertThat(paused.at("/wait_reason/waited_s").isNumber()).isTrue();

            // 3) 대여했지만 이 대여의 세팅 하트비트가 아직 없다 → 시작 준비
            String lease = body(mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk()).andReturn())
                    .at("/payload/lease_id").asString();
            mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.wait_reason.code").value("WORKER_STARTING"));

            // 4) 배경 수집 하트비트: 덜 모인 카메라만 싣는다
            ObjectNode snap = progressSnapshot(sessionId, "COLLECTING_BACKGROUND", 10.0);
            snap.putObject("background_collection_pct")
                    .put("cam1", 100.0).put("cam2", 100.0).put("cam3", 15.0).put("cam4", 100.0);
            mvc.perform(heartbeatWithSetup(tokenA, workerA, jobId, lease, 0.1, snap)).andExpect(status().isOk());
            JsonNode collecting = body(mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                    .andExpect(status().isOk()).andReturn());
            assertThat(collecting.at("/wait_reason/code").asString()).isEqualTo("BACKGROUND_INCOMPLETE");
            assertThat(collecting.at("/wait_reason/cameras")).hasSize(1);
            assertThat(collecting.at("/wait_reason/cameras/0/camera_id").asString()).isEqualTo("cam3");
            assertThat(collecting.at("/wait_reason/cameras/0/collection_pct").asDouble()).isEqualTo(15.0);

            // 5) 워커가 Tier 0 우선으로 반납하고 그 뒤 대여를 묻지 않는다 → 워커 근거
            long now = Instant.now().toEpochMilli();
            ObjectNode release = mapper.createObjectNode()
                    .put("job_id", jobId).put("lease_id", lease).put("job_type", "site_setup").put("status", "FAILED")
                    .put("started_at", now - 1_000).put("finished_at", now);
            release.putObject("error").put("code", "TIER0_PRIORITY")
                    .put("message", "Tier 0 우선으로 시작하지 않았다: 지연 초과").put("retryable", true)
                    .put("consume_attempt", false);
            mvc.perform(submitCallback(tokenA, workerA, release)).andExpect(status().isOk());
            jdbc.update("UPDATE worker SET last_seen_at = now() - interval '10 minutes' WHERE worker_id IN (?, ?)",
                    workerA, workerB);
            JsonNode released = body(mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                    .andExpect(status().isOk()).andReturn());
            assertThat(released.at("/wait_reason/code").asString()).isEqualTo("WORKER_PAUSED_TIER0");
            assertThat(released.at("/wait_reason/basis").asString()).isEqualTo("WORKER_RELEASE");
            assertThat(released.at("/wait_reason/detail").asString()).contains("Tier 0 우선");
            assertThat(released.at("/wait_reason/previous_error_code").isNull()).isTrue();

            // 6) 끝난 세션은 이유가 없다
            mvc.perform(post("/api/setup-sessions/{id}/cancel", sessionId).with(adminJwt()))
                    .andExpect(status().isOk())
                    .andExpect(jsonPath("$.wait_reason").value(org.hamcrest.Matchers.nullValue()));
        } finally {
            jdbc.update("DELETE FROM system_health WHERE gateway_id = ?", gateway);
        }
    }

    @Test
    void GPU_가_가져가지_않은_세팅은_대기_시간을_넘기면_닫히고_새_세팅을_막지_않는다() throws Exception {
        registerWorkers();
        String sessionId = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted()).andReturn()).get("setup_session_id").asString();
        String jobId = jdbc.queryForObject("SELECT job_id FROM setup_session WHERE setup_session_id = ?::uuid", String.class, sessionId);

        // 아직 기준 시간(10분) 전이면 닫지 않는다
        assertThat(queueTimeout.closeUnclaimed()).isZero();

        // 한 번도 대여되지 않은 채 11분이 지났고 세팅 워커도 그동안 보이지 않았다(GPU 인스턴스가 꺼져 있음).
        // 시험 DB 는 다른 시험과 함께 쓰므로 남은 워커의 접속 시각도 오래전으로 돌린다
        jdbc.update("UPDATE analysis_job SET created_at = now() - interval '11 minutes' WHERE job_id = ?", jobId);
        jdbc.update("UPDATE worker SET last_seen_at = now() - interval '1 day' WHERE last_seen_at IS NOT NULL");
        assertThat(queueTimeout.closeUnclaimed()).isEqualTo(1);

        assertThat(jdbc.queryForObject("SELECT status FROM setup_session WHERE setup_session_id = ?::uuid", String.class, sessionId))
                .isEqualTo("FAILED");
        assertThat(jdbc.queryForObject("SELECT failure_code FROM setup_session WHERE setup_session_id = ?::uuid", String.class,
                sessionId)).isEqualTo("SETUP_QUEUE_TIMEOUT");
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, jobId)).isEqualTo("CANCELLED");
        // 다시 돌려도 이미 닫힌 세션은 건드리지 않는다
        assertThat(queueTimeout.closeUnclaimed()).isZero();

        // 막던 세션이 닫혀 같은 현장의 새 세팅을 시작할 수 있다
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted());
    }

    @Test
    void 세팅_워커가_보이는_동안은_닫지_않고_보이지_않게_되면_대여됐던_작업도_닫는다() throws Exception {
        registerWorkers();
        String sessionId = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted()).andReturn()).get("setup_session_id").asString();
        String jobId = jdbc.queryForObject("SELECT job_id FROM setup_session WHERE setup_session_id = ?::uuid", String.class, sessionId);
        mvc.perform(leaseRequest(tokenA, workerA)).andExpect(status().isOk());
        // 워커가 죽어 대여가 만료되고 작업이 다시 대기열로 돌아왔다. 작업은 11분 전에 들어왔다
        jdbc.update("UPDATE job_lease SET status = 'EXPIRED' WHERE job_id = ?", jobId);
        jdbc.update("UPDATE analysis_job SET status = 'QUEUED', created_at = now() - interval '11 minutes' WHERE job_id = ?", jobId);

        // 방금 대여 요청을 보낸 워커가 있으면(GPU 가 켜져 있음) 기다린다
        assertThat(queueTimeout.closeUnclaimed()).isZero();

        // 대여 요청은 오래됐지만 다른 작업을 돌리며 하트비트를 보내는 워커가 있으면(바쁨) 기다린다
        jdbc.update("UPDATE worker SET last_seen_at = now() - interval '1 day' WHERE last_seen_at IS NOT NULL");
        jdbc.update("UPDATE job_lease SET status = 'ACTIVE', last_heartbeat_at = now() WHERE job_id = ?", jobId);
        assertThat(queueTimeout.closeUnclaimed()).isZero();
        assertThat(jdbc.queryForObject("SELECT status FROM setup_session WHERE setup_session_id = ?::uuid", String.class, sessionId))
                .isNotEqualTo("FAILED");

        // 하트비트도 끊겼다(GPU 인스턴스가 꺼짐). 대여됐던 작업도 닫는다
        jdbc.update("UPDATE job_lease SET status = 'EXPIRED' WHERE job_id = ?", jobId);
        assertThat(queueTimeout.closeUnclaimed()).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT failure_code FROM setup_session WHERE setup_session_id = ?::uuid", String.class,
                sessionId)).isEqualTo("SETUP_QUEUE_TIMEOUT");
    }

    private void registerWorkers() {
        if (tokenA == null) {
            tokenA = machines.registerWorker(workerA, List.of("site_setup")).token();
            tokenB = machines.registerWorker(workerB, List.of("site_setup")).token();
        }
    }

    private ObjectNode createRequest(double cam1, double cam2, double cam3, double cam4) {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m")
                .put("cam1", cam1)
                .put("cam2", cam2)
                .put("cam3", cam3)
                .put("cam4", cam4);
        return req;
    }

    private ObjectNode progressSnapshot(String sessionId, String status, double progressPct) {
        return mapper.createObjectNode()
                .put("setup_session_id", sessionId)
                .put("status", status)
                .put("progress_pct", progressPct);
    }

    private ObjectNode heightChecks(
            double in1, double res1, double diff1, boolean ok1,
            double in2, double res2, double diff2, boolean ok2,
            double in3, double res3, double diff3, boolean ok3,
            double in4, double res4, double diff4, boolean ok4) {
        ObjectNode checks = mapper.createObjectNode();
        checks.putObject("cam1").put("input_height_m", in1).put("restored_height_m", res1)
                .put("difference_cm", diff1).put("within_tolerance", ok1);
        checks.putObject("cam2").put("input_height_m", in2).put("restored_height_m", res2)
                .put("difference_cm", diff2).put("within_tolerance", ok2);
        checks.putObject("cam3").put("input_height_m", in3).put("restored_height_m", res3)
                .put("difference_cm", diff3).put("within_tolerance", ok3);
        checks.putObject("cam4").put("input_height_m", in4).put("restored_height_m", res4)
                .put("difference_cm", diff4).put("within_tolerance", ok4);
        return checks;
    }

    private org.springframework.test.web.servlet.RequestBuilder leaseRequest(String token, String worker) {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add("site_setup");
        p.put("wait_ms", 0);
        return post("/api/worker/jobs/lease")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(envelope("job_lease_request", "2.0.0", worker, p).toString());
    }

    private org.springframework.test.web.servlet.RequestBuilder heartbeatWithSetup(
            String token, String worker, String jobId, String leaseId, double progress, ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.1.0", worker, p);
        assertThat(schemas.violations("messages/job_heartbeat", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + jobId + "/heartbeat")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(env.toString());
    }

    private ObjectNode succeededCallback(String jobId, String leaseId, ObjectNode result) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", "site_setup")
                .put("status", "SUCCEEDED")
                .put("started_at", now - 10_000)
                .put("finished_at", now);
        p.set("result", result);
        return p;
    }

    private ObjectNode failedCallback(String jobId, String leaseId, String code, String message) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", "site_setup")
                .put("status", "FAILED")
                .put("started_at", now - 5_000)
                .put("finished_at", now);
        p.putObject("error")
                .put("code", code)
                .put("message", message)
                .put("retryable", false);
        return p;
    }

    private org.springframework.test.web.servlet.RequestBuilder submitCallback(String token, String worker, ObjectNode payload) {
        String jobId = payload.get("job_id").asString();
        ObjectNode env = envelope("job_callback", "2.6.0", worker, payload);
        assertThat(schemas.violations("messages/job_callback", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + jobId + "/result")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(env.toString());
    }

    private ObjectNode envelope(String msgType, String version, String source, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode()
                .put("schema_version", version)
                .put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString())
                .put("sent_at", Instant.now().toEpochMilli())
                .put("source_id", source);
        env.set("payload", payload);
        return env;
    }

    private RequestPostProcessor adminJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId)))
                .authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
    }

    private RequestPostProcessor viewerJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId)))
                .authorities(new SimpleGrantedAuthority("ROLE_VIEWER"));
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }
}
