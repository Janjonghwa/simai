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
 * 현장 세팅 중 VLM 위험구역 초안({@code vlm_zone_draft}) 자동 발행·콜백 저장·조회·실패 격리 통합 시험(S15P31A101-455).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupZoneDraftIntegrationTest {

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

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String setupWorker = "worker-setup-zd-" + suffix;
    private final String vlmWorker = "worker-vlm-zd-" + suffix;
    private final String calibration = "cal-zd-" + suffix;
    private final String previewScene = "scene-preview-zd-" + suffix;
    private final String previewScene2 = "scene-preview2-zd-" + suffix;
    private final String bakedScene = "scene-baked-zd-" + suffix;
    private final String configVer = "cfg-zd-" + suffix;

    private String site;
    private long userId;
    private long workUnitId;
    private String setupToken;
    private String vlmToken;

    @BeforeEach
    void setUp() {
        cleanUpTables();
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        jdbc.update("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '초안관리자', 'ADMIN', now())
                """, "zd-admin-" + suffix);
        userId = jdbc.queryForObject("SELECT user_id FROM app_user WHERE login_id = ?", Long.class, "zd-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "초안세팅작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "초안세팅작업-" + suffix);

        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'PROVISIONAL', 'G1_AUTO', 'site_setup', now(), 's3://test/cal.json', ?, now())
                """, calibration, site, "sha256:" + "b".repeat(64));
        for (String sv : List.of(previewScene, previewScene2, bakedScene)) {
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                        input_provenance, is_upper_bound, job_id, scene_output, requested_by)
                    VALUES (?, ?, ?, 'CANDIDATE', 'static-3d/test/preview.glb', '{}'::jsonb, '{}'::jsonb, false, NULL, '{}'::jsonb, ?)
                    """, sv, site, calibration, userId);
        }
        jdbc.update("""
                INSERT INTO config_bundle (config_version, previous_config_version, site_id, issued_at, issued_by,
                    bundle_uri, bundle_hash, components, calibration_version, rule_profile)
                VALUES (?, NULL, ?, now(), ?, 'https://test/cfg', ?, '{}'::jsonb, ?, 'OPERATION')
                """, configVer, site, userId, "sha256:" + "c".repeat(64), calibration);

        setupToken = machines.registerWorker(setupWorker, List.of("site_setup")).token();
        vlmToken = machines.registerWorker(vlmWorker, List.of("vlm_zone_draft")).token();
    }

    @AfterEach
    void tearDown() {
        cleanUpTables();
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
    }

    private void cleanUpTables() {
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE active_setup_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_zone_draft");
        jdbc.update("DELETE FROM setup_zone_draft_run");
        jdbc.update("DELETE FROM setup_session");
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", configVer);
        jdbc.update("DELETE FROM scene_version WHERE scene_version IN (?, ?, ?)", previewScene, previewScene2, bakedScene);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "초안세팅작업-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "zd-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id IN (?, ?)", setupWorker, vlmWorker);
    }

    @Test
    void 프리뷰_맵_준비시_vlm_zone_draft_자동_발행_중복방지_및_초안_콜백_저장과_조회가_계약대로_동작한다() throws Exception {
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted())
                .andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String setupJobId = created.get("job_id").asString();

        // 1) 프리뷰 맵 생성 전 조회 시 NOT_REQUESTED, safety_use=false, 빈 배열 반환
        mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.setup_session_id").value(sessionId))
                .andExpect(jsonPath("$.status").value("NOT_REQUESTED"))
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.drafts").isEmpty());

        String setupLease = body(mvc.perform(leaseRequest(setupToken, setupWorker, "site_setup"))
                .andExpect(status().isOk()).andReturn())
                .at("/payload/lease_id").asString();

        // 세팅 세션이 등록한 미리보기 Scene 주소가 vlm_zone_draft 입력에 실린다
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256, registered_at)
                VALUES (?::uuid, 'PREVIEW_SCENE', ?, ?, ?::uuid, 's3://test/preview-474.glb', ?, now())
                """, sessionId, previewScene, setupJobId, setupLease, "d".repeat(64));

        // 2) READY_FOR_ZONES 스냅샷에 calibration_version 과 preview_scene_version 보고 시 vlm_zone_draft 자동 발행
        ObjectNode readySnap = progressSnapshot(sessionId, "READY_FOR_ZONES", 75.0)
                .put("calibration_version", calibration)
                .put("preview_scene_version", previewScene);
        mvc.perform(heartbeatWithSetup(setupToken, setupWorker, setupJobId, setupLease, 0.75, readySnap))
                .andExpect(status().isOk());

        JsonNode queuedDrafts = body(mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("QUEUED"))
                .andExpect(jsonPath("$.preview_scene_version").value(previewScene))
                .andExpect(jsonPath("$.calibration_version").value(calibration))
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.drafts").isEmpty())
                .andReturn());
        String draftJobId = queuedDrafts.get("job_id").asString();
        String draftRunId = queuedDrafts.get("draft_run_id").asString();
        assertThat(draftJobId).isNotBlank();
        assertThat(draftRunId).isNotBlank();

        // 3) 같은 preview_scene_version 으로 다시 하트비트가 와도 vlm_zone_draft 가 중복 발행되지 않는다
        ObjectNode bakeSnap = progressSnapshot(sessionId, "BAKING_TEXTURE", 88.0)
                .put("calibration_version", calibration)
                .put("preview_scene_version", previewScene);
        mvc.perform(heartbeatWithSetup(setupToken, setupWorker, setupJobId, setupLease, 0.88, bakeSnap))
                .andExpect(status().isOk());
        Integer draftJobCount = jdbc.queryForObject(
                "SELECT count(*) FROM analysis_job WHERE job_type = 'vlm_zone_draft'", Integer.class);
        assertThat(draftJobCount).isEqualTo(1);

        // 4) VLM 워커가 vlm_zone_draft 를 대여하면 job_request(3.8.0)가 계약을 통과한다
        JsonNode draftLeaseEnv = body(mvc.perform(leaseRequest(vlmToken, vlmWorker, "vlm_zone_draft"))
                .andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_request", draftLeaseEnv.toString())).isEmpty();
        JsonNode draftLeasePayload = draftLeaseEnv.get("payload");
        assertThat(draftLeasePayload.get("job_id").asString()).isEqualTo(draftJobId);
        assertThat(draftLeasePayload.get("job_type").asString()).isEqualTo("vlm_zone_draft");
        assertThat(draftLeasePayload.get("priority").asString()).isEqualTo("BACKGROUND");
        assertThat(draftLeasePayload.at("/input/setup_session_id").asString()).isEqualTo(sessionId);
        assertThat(draftLeasePayload.at("/input/preview_scene_version").asString()).isEqualTo(previewScene);
        assertThat(draftLeasePayload.at("/input/calibration_version").asString()).isEqualTo(calibration);
        assertThat(draftLeasePayload.at("/input/calibration_ref").asString()).isEqualTo("s3://test/cal.json");
        assertThat(draftLeasePayload.at("/input/preview_scene_uri").asString()).isEqualTo("s3://test/preview-474.glb");
        String draftLeaseId = draftLeasePayload.get("lease_id").asString();

        // 5) VLM 워커가 기하 후보 2건(ELEVATED_WORK_SURFACE, OPENING) + Cosmos3-Nano 추천 결과를 콜백 제출
        ObjectNode draftResult = buildDraftSucceededResult(sessionId, previewScene, calibration);
        mvc.perform(submitDraftCallback(vlmToken, vlmWorker,
                        draftSucceededCallback(draftJobId, draftLeaseId, draftResult)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        // 6) GET /api/setup-sessions/{id}/zone-drafts 에서 초안 목록과 safety_use=false 확인
        JsonNode finalDrafts = body(mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.model_version").value("nvidia/Cosmos-Reason2-2B"))
                .andExpect(jsonPath("$.prompt_version").value("vlm-zone-draft-1.0"))
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.drafts.length()").value(2))
                .andExpect(jsonPath("$.drafts[0].candidate_id").value("cand-elev-01"))
                .andExpect(jsonPath("$.drafts[0].candidate_kind").value("ELEVATED_WORK_SURFACE"))
                .andExpect(jsonPath("$.drafts[0].zone_kind").value("WORK_SURFACE"))
                .andExpect(jsonPath("$.drafts[0].eval_type").value("VOLUME_3D"))
                .andExpect(jsonPath("$.drafts[0].recommended_name").value("중앙 작업대 상부 추락주의 구역"))
                .andExpect(jsonPath("$.drafts[0].recommended_severity").value("HIGH"))
                .andExpect(jsonPath("$.drafts[0].vlm_status").value("VLM"))
                .andExpect(jsonPath("$.drafts[1].candidate_id").value("cand-open-01"))
                .andExpect(jsonPath("$.drafts[1].candidate_kind").value("OPENING"))
                .andExpect(jsonPath("$.drafts[1].zone_kind").value("OPENING"))
                .andExpect(jsonPath("$.drafts[1].eval_type").value("GROUND_FOOTPRINT"))
                .andExpect(jsonPath("$.drafts[1].recommended_severity").value("CRITICAL"))
                .andReturn());
        assertThat(finalDrafts.at("/drafts/0/top_height_m").doubleValue())
                .isCloseTo(0.75, org.assertj.core.data.Offset.offset(1e-4));

        // 세션 상세에도 zone_drafts_ref 가 연결된다
        mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.zone_drafts_ref").value("zone-draft-run:" + draftRunId));
    }

    @Test
    void VLM_미응답_NO_OPINION_초안과_vlm_zone_draft_작업_실패가_현장_세팅_세션을_실패시키지_않는다() throws Exception {
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(createRequest(2.42, 2.48, 2.45, 2.51).toString()))
                .andExpect(status().isAccepted())
                .andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String setupJobId = created.get("job_id").asString();

        String setupLease = body(mvc.perform(leaseRequest(setupToken, setupWorker, "site_setup"))
                .andExpect(status().isOk()).andReturn())
                .at("/payload/lease_id").asString();

        // 1) previewScene 으로 READY_FOR_ZONES 진입 → 첫 번째 vlm_zone_draft 발행 → NO_OPINION 콜백
        ObjectNode readySnap1 = progressSnapshot(sessionId, "READY_FOR_ZONES", 72.0)
                .put("calibration_version", calibration)
                .put("preview_scene_version", previewScene);
        mvc.perform(heartbeatWithSetup(setupToken, setupWorker, setupJobId, setupLease, 0.72, readySnap1))
                .andExpect(status().isOk());

        JsonNode lease1 = body(mvc.perform(leaseRequest(vlmToken, vlmWorker, "vlm_zone_draft"))
                .andExpect(status().isOk()).andReturn()).get("payload");
        String draftJob1 = lease1.get("job_id").asString();
        String draftLease1 = lease1.get("lease_id").asString();

        ObjectNode noOpinionResult = buildNoOpinionResult(sessionId, previewScene, calibration);
        mvc.perform(submitDraftCallback(vlmToken, vlmWorker,
                        draftSucceededCallback(draftJob1, draftLease1, noOpinionResult)))
                .andExpect(status().isOk());

        mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("NO_OPINION"))
                .andExpect(jsonPath("$.model_version").isEmpty())
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.drafts.length()").value(1))
                .andExpect(jsonPath("$.drafts[0].vlm_status").value("NO_OPINION"))
                .andExpect(jsonPath("$.drafts[0].rationale").value("AI 의견 없음"));

        // 2) 새 프리뷰 버전(previewScene2)이 보고되면 새 vlm_zone_draft 가 발행되고, 이 작업이 FAILED 돼도 setup_session 은 정상 완료된다
        ObjectNode readySnap2 = progressSnapshot(sessionId, "READY_FOR_ZONES", 80.0)
                .put("calibration_version", calibration)
                .put("preview_scene_version", previewScene2);
        mvc.perform(heartbeatWithSetup(setupToken, setupWorker, setupJobId, setupLease, 0.80, readySnap2))
                .andExpect(status().isOk());

        JsonNode lease2 = body(mvc.perform(leaseRequest(vlmToken, vlmWorker, "vlm_zone_draft"))
                .andExpect(status().isOk()).andReturn()).get("payload");
        String draftJob2 = lease2.get("job_id").asString();
        String draftLease2 = lease2.get("lease_id").asString();

        mvc.perform(submitDraftCallback(vlmToken, vlmWorker,
                        draftFailedCallback(draftJob2, draftLease2, "VLM_TIMEOUT", "Cosmos3-Nano 응답 시간 초과")))
                .andExpect(status().isOk());

        // 초안 조회는 FAILED 상태를 보여주지만
        mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.error_code").value("VLM_TIMEOUT"))
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.drafts").isEmpty());

        // 현장 세팅 세션 자체는 READY_FOR_ZONES 를 유지하며 COMPLETED 로 정상 마감된다
        mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("READY_FOR_ZONES"));

        ObjectNode completedResult = progressSnapshot(sessionId, "COMPLETED", 100.0)
                .put("calibration_version", calibration)
                .put("preview_scene_version", previewScene2)
                .put("scene_version", bakedScene)
                .put("config_version", configVer);
        mvc.perform(submitDraftCallback(setupToken, setupWorker,
                        setupSucceededCallback(setupJobId, setupLease, completedResult)))
                .andExpect(status().isOk());

        mvc.perform(get("/api/setup-sessions/{id}", sessionId).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.scene_version").value(bakedScene));
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
        ObjectNode node = mapper.createObjectNode()
                .put("setup_session_id", sessionId)
                .put("status", status)
                .put("progress_pct", progressPct);
        ObjectNode checks = node.putObject("height_checks");
        checks.putObject("cam1").put("input_height_m", 2.42).put("restored_height_m", 2.42)
                .put("difference_cm", 0.0).put("within_tolerance", true);
        checks.putObject("cam2").put("input_height_m", 2.48).put("restored_height_m", 2.48)
                .put("difference_cm", 0.0).put("within_tolerance", true);
        checks.putObject("cam3").put("input_height_m", 2.45).put("restored_height_m", 2.45)
                .put("difference_cm", 0.0).put("within_tolerance", true);
        checks.putObject("cam4").put("input_height_m", 2.51).put("restored_height_m", 2.51)
                .put("difference_cm", 0.0).put("within_tolerance", true);
        return node;
    }

    private org.springframework.test.web.servlet.RequestBuilder leaseRequest(
            String token, String worker, String jobType) {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add(jobType);
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

    private ObjectNode buildDraftSucceededResult(String sessionId, String previewVer, String calVer) {
        ObjectNode res = mapper.createObjectNode()
                .put("setup_session_id", sessionId)
                .put("site_id", site)
                .put("preview_scene_version", previewVer)
                .put("calibration_version", calVer)
                .put("safety_use", false)
                .put("status", "SUCCEEDED")
                .put("model_version", "nvidia/Cosmos-Reason2-2B")
                .put("prompt_version", "vlm-zone-draft-1.0")
                .put("latency_ms", 3820);
        ArrayNode drafts = res.putArray("drafts");

        ObjectNode d1 = drafts.addObject()
                .put("candidate_id", "cand-elev-01")
                .put("candidate_kind", "ELEVATED_WORK_SURFACE")
                .put("zone_kind", "WORK_SURFACE")
                .put("eval_type", "VOLUME_3D")
                .put("z_min_m", 0.0)
                .put("z_max_m", 2.5)
                .put("top_height_m", 0.75)
                .put("recommended_name", "중앙 작업대 상부 추락주의 구역")
                .put("recommended_severity", "HIGH")
                .put("rationale", "바닥 대비 0.75m 높이의 평탄 작업 상판이 관측되어 올라섬 시 추락 위험이 있습니다.")
                .put("vlm_status", "VLM")
                .put("confidence", 0.86);
        addPolygon(d1, -0.6, -0.4, 0.6, 0.4);
        ObjectNode obb = d1.putObject("obb");
        obb.putArray("center").add(0.0).add(0.0).add(0.375);
        obb.putArray("size").add(1.2).add(0.8).add(0.75);
        obb.put("yaw_rad", 0.0);
        d1.putArray("evidence_cameras").add("cam1").add("cam2");

        ObjectNode d2 = drafts.addObject()
                .put("candidate_id", "cand-open-01")
                .put("candidate_kind", "OPENING")
                .put("zone_kind", "OPENING")
                .put("eval_type", "GROUND_FOOTPRINT")
                .put("z_min_m", -0.5)
                .put("z_max_m", 0.2)
                .putNull("top_height_m")
                .putNull("obb")
                .put("recommended_name", "남측 바닥 개구부 추락위험 구역")
                .put("recommended_severity", "CRITICAL")
                .put("rationale", "바닥면 내부에 깊이 0.50m의 개구부 후보가 탐지되어 추락 위험이 높습니다.")
                .put("vlm_status", "VLM")
                .put("confidence", 0.91);
        addPolygon(d2, -1.2, -0.8, -0.4, -0.1);
        d2.putArray("evidence_cameras").add("cam1");
        return res;
    }

    private ObjectNode buildNoOpinionResult(String sessionId, String previewVer, String calVer) {
        ObjectNode res = mapper.createObjectNode()
                .put("setup_session_id", sessionId)
                .put("site_id", site)
                .put("preview_scene_version", previewVer)
                .put("calibration_version", calVer)
                .put("safety_use", false)
                .put("status", "NO_OPINION")
                .putNull("model_version")
                .put("prompt_version", "vlm-zone-draft-1.0")
                .put("latency_ms", 120);
        ArrayNode drafts = res.putArray("drafts");
        ObjectNode d1 = drafts.addObject()
                .put("candidate_id", "cand-elev-01")
                .put("candidate_kind", "ELEVATED_WORK_SURFACE")
                .put("zone_kind", "WORK_SURFACE")
                .put("eval_type", "VOLUME_3D")
                .put("z_min_m", 0.0)
                .put("z_max_m", 2.5)
                .put("top_height_m", 0.75)
                .put("recommended_name", "고소 작업면 후보 (cand-elev-01)")
                .put("recommended_severity", "HIGH")
                .put("rationale", "AI 의견 없음")
                .put("vlm_status", "NO_OPINION")
                .putNull("confidence");
        addPolygon(d1, -0.6, -0.4, 0.6, 0.4);
        ObjectNode obb = d1.putObject("obb");
        obb.putArray("center").add(0.0).add(0.0).add(0.375);
        obb.putArray("size").add(1.2).add(0.8).add(0.75);
        obb.put("yaw_rad", 0.0);
        d1.putArray("evidence_cameras").add("cam1");
        return res;
    }

    private void addPolygon(ObjectNode node, double x1, double y1, double x2, double y2) {
        ArrayNode poly = node.putArray("polygon_xy");
        poly.addArray().add(x1).add(y1);
        poly.addArray().add(x2).add(y1);
        poly.addArray().add(x2).add(y2);
        poly.addArray().add(x1).add(y2);
    }

    private ObjectNode draftSucceededCallback(String jobId, String leaseId, ObjectNode result) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", "vlm_zone_draft")
                .put("status", "SUCCEEDED")
                .put("started_at", now - 3_000)
                .put("finished_at", now);
        p.set("result", result);
        return p;
    }

    private ObjectNode draftFailedCallback(String jobId, String leaseId, String code, String message) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", "vlm_zone_draft")
                .put("status", "FAILED")
                .put("started_at", now - 3_000)
                .put("finished_at", now);
        p.putObject("error")
                .put("code", code)
                .put("message", message)
                .put("retryable", false);
        return p;
    }

    private ObjectNode setupSucceededCallback(String jobId, String leaseId, ObjectNode result) {
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

    private org.springframework.test.web.servlet.RequestBuilder submitDraftCallback(
            String token, String worker, ObjectNode payload) {
        String jobId = payload.get("job_id").asString();
        ObjectNode env = envelope("job_callback", "2.7.0", worker, payload);
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
