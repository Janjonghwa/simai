package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.time.LocalDate;
import java.util.List;
import java.util.Map;
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
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.RequestPostProcessor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 변경 의심 구역 VLM 의견(S15P31A101-631): 초안 작업 입력에 변경 의심 구역·이전 세팅 사진·보정을 싣고(job_request 계약 통과, 시간 상한),
 * 콜백 zone_opinions 를 추가 저장하며, 구역 계획 응답 항목에 ai_opinion(대기 중 PENDING, 전·후 서명 주소·크롭 박스)을 싣는 흐름.
 * 의견은 needs_review·구역 목록을 바꾸지 않는다. 저장소는 가짜(서명은 키를 담은 고정 주소).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupZoneOpinionIntegrationTest {

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper mapper;
    @Autowired MachineTokenService machines;
    @Autowired ContractSchemas schemas;
    @Autowired SetupZoneOpinions opinions;

    @MockitoBean ObjectStorage storage;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String setupWorker = "worker-setup-op-" + suffix;
    private final String vlmWorker = "worker-vlm-op-" + suffix;
    private final String calibration = "cal-op-" + suffix;
    private final String prevCalibration = "cal-op-prev-" + suffix;
    private final String previewScene = "scene-op-preview-" + suffix;
    private final String prevPreview = "scene-op-prev-preview-" + suffix;
    private final String prevScene = "scene-op-prev-" + suffix;
    private final String prevJob = "job-op-prev-" + suffix;
    private final UUID prevSession = UUID.randomUUID();
    private final UUID prevLease = UUID.randomUUID();
    private final String zoneNear = "z-op-near-" + suffix;
    private final String zoneFar = "z-op-far-" + suffix;
    private final String zoneSame = "z-op-same-" + suffix;

    private String site;
    private long userId;
    private long workUnitId;
    private String setupToken;
    private String vlmToken;

    @BeforeEach
    void setUp() {
        cleanUp();
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(i -> "s3://test/" + i.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(i -> new SignedRequest("https://storage.test/" + i.getArgument(0), Map.of(),
                Instant.now().plusSeconds(300)));
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        // 현장마다 진행 중 세팅은 하나다. 다른 시험이 남긴 진행 중 세션은 끝난 것으로 둔다
        jdbc.update("UPDATE setup_session SET status = 'FAILED' WHERE site_id = ? AND status NOT IN ('COMPLETED', 'FAILED')", site);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        userId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '의견관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "op-admin-" + suffix);
        workUnitId = jdbc.queryForObject("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now()) RETURNING work_id
                """, Long.class, site, LocalDate.now(), "의견세팅작업-" + suffix, userId);
        for (String[] cal : List.of(new String[] {calibration, "s3://test/cal-op.json", "b"},
                new String[] {prevCalibration, "s3://test/cal-op-prev.json", "a"})) {
            jdbc.update("""
                    INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                        calibration_ref, calibration_hash, created_at)
                    VALUES (?, ?, 'PROVISIONAL', 'G1_AUTO', 'site_setup', now(), ?, ?, now())
                    """, cal[0], site, cal[1], "sha256:" + cal[2].repeat(64));
        }
        for (String[] sv : List.of(new String[] {previewScene, calibration}, new String[] {prevPreview, prevCalibration},
                new String[] {prevScene, prevCalibration})) {
            jdbc.update("""
                    INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, quality,
                        input_provenance, is_upper_bound, job_id, scene_output, requested_by)
                    VALUES (?, ?, ?, 'CANDIDATE', 'static-3d/test/x.glb', '{}'::jsonb, '{}'::jsonb, false, NULL, '{}'::jsonb, ?)
                    """, sv[0], site, sv[1], userId);
        }
        // 이전 관제 Scene 을 만든 세팅 세션(완료): 미리보기·정식 Scene 과 카메라 대표 프레임(미리보기 대여 것 + 앞선 대여의 낡은 것)
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, input, created_at)
                VALUES (?, 'site_setup', 'BACKGROUND', 'SUCCEEDED', 1, 1, 1000, '{}'::jsonb, now())
                """, prevJob);
        jdbc.update("""
                INSERT INTO setup_session (setup_session_id, site_id, work_unit_id, job_id, status, camera_heights_m, height_tolerance_cm,
                    requested_by, started_at, updated_at, calibration_version, preview_scene_version, scene_version)
                VALUES (?, ?, ?, ?, 'COMPLETED', '{}'::jsonb, 5, ?, now() - interval '1 day', now(), ?, ?, ?)
                """, prevSession, site, workUnitId, prevJob, userId, prevCalibration, prevPreview, prevScene);
        artifact(prevSession, "PREVIEW_SCENE", prevPreview, prevJob, prevLease, "s3://test/prev-preview.glb", null);
        artifact(prevSession, "SCENE", prevScene, prevJob, prevLease, "s3://test/prev-scene.glb", null);
        artifact(prevSession, "CAMERA_FRAME", "cam_A@old", prevJob, UUID.randomUUID(), prevFrame("old", "cam_A"), "cam_A");
        artifact(prevSession, "CAMERA_FRAME", "cam_A@aaa", prevJob, prevLease, prevFrame(prevLease.toString(), "cam_A"), "cam_A");
        artifact(prevSession, "CAMERA_FRAME", "cam_B@aaa", prevJob, prevLease, prevFrame(prevLease.toString(), "cam_B"), "cam_B");
        // 지금 관제 구역(이어 쓴 구역)
        zone(zoneNear, "가까운 단상", "POLYGON((0 0,2 0,2 1,0 1,0 0))");
        zone(zoneFar, "먼 개구부", "POLYGON((3 3,4 3,4 4,3 4,3 3))");
        zone(zoneSame, "그대로 구역", "POLYGON((5 5,6 5,6 6,5 6,5 5))");

        setupToken = machines.registerWorker(setupWorker, List.of("site_setup")).token();
        vlmToken = machines.registerWorker(vlmWorker, List.of("vlm_zone_draft")).token();
    }

    @AfterEach
    void tearDown() {
        cleanUp();
    }

    /** 이 시험 클래스가 남긴 것(앞선 실행이 실패해 남긴 것 포함)을 이름 앞부분으로 지운다 */
    private void cleanUp() {
        String work = "의견세팅작업-%";
        List<String> jobs = jdbc.queryForList("""
                SELECT s.job_id FROM setup_session s JOIN work_unit w ON w.work_id = s.work_unit_id WHERE w.name LIKE ?
                UNION SELECT r.job_id FROM setup_zone_draft_run r JOIN setup_session s ON s.setup_session_id = r.setup_session_id
                    JOIN work_unit w ON w.work_id = s.work_unit_id WHERE w.name LIKE ?
                """, String.class, work, work);
        String sessions = "SELECT s.setup_session_id FROM setup_session s JOIN work_unit w ON w.work_id = s.work_unit_id WHERE w.name LIKE ?";
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE name LIKE ?", work);
        jdbc.update("DELETE FROM setup_session_zone_plan WHERE setup_session_id IN (" + sessions + ")", work);
        jdbc.update("DELETE FROM setup_session WHERE setup_session_id IN (" + sessions + ")", work);
        for (String job : jobs) {
            jdbc.update("DELETE FROM job_lease WHERE job_id = ?", job);
            jdbc.update("DELETE FROM analysis_job WHERE job_id = ?", job);
        }
        jdbc.update("DELETE FROM analysis_job WHERE job_id LIKE 'job-op-prev-%'");
        jdbc.update("DELETE FROM zone WHERE zone_id LIKE 'z-op-%'");
        jdbc.update("DELETE FROM scene_version WHERE scene_version LIKE 'scene-op-%'");
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE 'cal-op-%'");
        jdbc.update("DELETE FROM work_unit WHERE name LIKE ?", work);
        jdbc.update("DELETE FROM app_user WHERE login_id LIKE 'op-admin-%'");
        jdbc.update("DELETE FROM worker WHERE worker_id LIKE 'worker-setup-op-%' OR worker_id LIKE 'worker-vlm-op-%'");
    }

    @Test
    void 변경_의심_구역과_이전_세팅_사진을_싣고_의견을_저장해_구역_목록에_전후_사진과_함께_보인다() throws Exception {
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(createRequest().toString()))
                .andExpect(status().isAccepted()).andReturn());
        UUID sessionId = UUID.fromString(created.get("setup_session_id").asString());
        String setupJobId = created.get("job_id").asString();
        String setupLease = body(mvc.perform(leaseRequest(setupToken, setupWorker, "site_setup"))
                .andExpect(status().isOk()).andReturn()).at("/payload/lease_id").asString();

        // 1) 미리보기 전 하트비트로 대여 기준선을 잡고, World 이어짐·높이 비교 판정을 세션에 둔다(같은 대여라 다음 스냅샷이 지우지 않는다)
        mvc.perform(heartbeat(setupJobId, setupLease, 0.6, snapshot(sessionId, "JOINT_BA_AND_VERIFY", 60.0)))
                .andExpect(status().isOk());
        jdbc.update("UPDATE setup_session SET world_anchor = '{\"accepted\": true}'::jsonb, zone_height_changes = CAST(? AS jsonb) "
                + "WHERE setup_session_id = ?", zoneHeightChanges().toString(), sessionId);
        artifact(sessionId, "PREVIEW_SCENE", previewScene, setupJobId, UUID.fromString(setupLease), "s3://test/preview.glb", null);
        artifact(sessionId, "CAMERA_FRAME", "cam_A@bbb", setupJobId, UUID.fromString(setupLease), afterFrame(setupJobId, setupLease), "cam_A");
        jdbc.update("""
                INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                VALUES (?, CAST(? AS jsonb), 1, ?, now())
                """, sessionId, planItems().toString(), userId);

        // 2) READY_FOR_ZONES 로 초안 작업이 들어가며 변경 의심 구역(높이 차이 큰 순)·이전 사진·보정이 실린다
        ObjectNode ready = snapshot(sessionId, "READY_FOR_ZONES", 85.0).put("calibration_version", calibration)
                .put("preview_scene_version", previewScene);
        mvc.perform(heartbeat(setupJobId, setupLease, 0.85, ready)).andExpect(status().isOk());

        // 대기 중에는 의견 자리에 PENDING 이 보인다
        JsonNode pending = zonesView(sessionId);
        assertThat(itemOf(pending, zoneFar).at("/ai_opinion/status").asString()).isEqualTo("PENDING");
        assertThat(itemOf(pending, zoneSame).has("ai_opinion")).isFalse();

        JsonNode leaseEnv = body(mvc.perform(leaseRequest(vlmToken, vlmWorker, "vlm_zone_draft"))
                .andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_request", leaseEnv.toString())).isEmpty();
        JsonNode payload = leaseEnv.get("payload");
        JsonNode input = payload.get("input");
        assertThat(payload.get("timeout_ms").asLong()).isEqualTo(150_000L);
        assertThat(input.at("/suspected_zones/0/zone_id").asString()).isEqualTo(zoneFar);
        assertThat(input.at("/suspected_zones/1/zone_id").asString()).isEqualTo(zoneNear);
        assertThat(input.get("suspected_zones").size()).isEqualTo(2);
        assertThat(input.at("/suspected_zones/0/name").asString()).isEqualTo("먼 개구부");
        assertThat(input.at("/suspected_zones/0/polygon_xy").size()).isEqualTo(4);
        assertThat(input.at("/suspected_zones/0/max_height_diff_m").asDouble()).isEqualTo(0.6);
        assertThat(input.get("previous_setup_session_id").asString()).isEqualTo(prevSession.toString());
        assertThat(input.get("previous_calibration_version").asString()).isEqualTo(prevCalibration);
        assertThat(input.get("previous_calibration_ref").asString()).isEqualTo("s3://test/cal-op-prev.json");
        assertThat(input.get("previous_calibration_hash").asString()).isEqualTo("sha256:" + "a".repeat(64));
        assertThat(input.get("previous_frames").size()).isEqualTo(2);
        assertThat(input.at("/previous_frames/0/frame_uri").asString()).isEqualTo(prevFrame(prevLease.toString(), "cam_A"));
        assertThat(input.at("/frames/0/frame_uri").asString()).isEqualTo(afterFrame(setupJobId, setupLease));

        // 3) 워커 결과(의견 하나 + 의견 없음 하나)를 저장한다
        String jobId = payload.get("job_id").asString();
        ObjectNode result = draftResult(sessionId.toString());
        ArrayNode ops = result.putArray("zone_opinions");
        ObjectNode far = ops.addObject().put("zone_id", zoneFar).put("status", "OPINION").putNull("reason")
                .put("recommended_action", "REMOVE").put("rationale", "개구부 덮개가 덮여 바닥이 평평해졌습니다.").put("confidence", 0.7)
                .put("camera_id", "cam_A").put("model_version", "nvidia/Cosmos-Reason2-2B").put("prompt_version", "vlm-zone-opinion-1.0");
        far.putArray("before_crop_box_norm").add(0.1).add(0.2).add(0.5).add(0.6);
        far.putArray("after_crop_box_norm").add(0.12).add(0.2).add(0.52).add(0.6);
        ops.addObject().put("zone_id", zoneNear).put("status", "NO_OPINION").put("reason", "NOT_VISIBLE")
                .putNull("recommended_action").putNull("rationale").putNull("confidence").putNull("camera_id")
                .putNull("before_crop_box_norm").putNull("after_crop_box_norm").putNull("model_version")
                .put("prompt_version", "vlm-zone-opinion-1.0");
        mvc.perform(callback(jobId, payload.get("lease_id").asString(), result)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_zone_opinion WHERE setup_session_id = ?", Integer.class, sessionId))
                .isEqualTo(2);

        // 4) 구역 목록 응답: 의견·전후 서명 주소·크롭 박스. needs_review 는 그대로다
        JsonNode view = zonesView(sessionId);
        JsonNode farOp = itemOf(view, zoneFar).get("ai_opinion");
        assertThat(farOp.get("status").asString()).isEqualTo("OPINION");
        assertThat(farOp.get("recommended_action").asString()).isEqualTo("REMOVE");
        assertThat(farOp.get("camera_id").asString()).isEqualTo("cam_A");
        assertThat(farOp.at("/before/image_url").asString())
                .isEqualTo("https://storage.test/job-objects/site_setup/" + prevJob + "/" + prevLease + "/frame-cam_A.jpg");
        assertThat(farOp.at("/after/image_url").asString())
                .isEqualTo("https://storage.test/job-objects/site_setup/" + setupJobId + "/" + setupLease + "/frame-cam_A.jpg");
        assertThat(farOp.at("/before/crop_box_norm/2").asDouble()).isEqualTo(0.5);
        assertThat(farOp.get("image_expires_at_ms").isNumber()).isTrue();
        JsonNode nearOp = itemOf(view, zoneNear).get("ai_opinion");
        assertThat(nearOp.get("status").asString()).isEqualTo("NO_OPINION");
        assertThat(nearOp.get("reason").asString()).isEqualTo("NOT_VISIBLE");
        assertThat(nearOp.at("/before/image_url").isNull()).isTrue();
        assertThat(itemOf(view, zoneSame).has("ai_opinion")).isFalse();
        for (String z : List.of(zoneFar, zoneNear, zoneSame)) {
            assertThat(itemOf(view, z).get("needs_review").asBoolean()).isTrue();
        }

        // 5) World 가 이어지지 않으면 의견 입력을 싣지 않는다(기존 초안 그대로)
        jdbc.update("UPDATE setup_session SET world_anchor = '{\"accepted\": false}'::jsonb WHERE setup_session_id = ?", sessionId);
        ObjectNode plain = mapper.createObjectNode();
        assertThat(opinions.addOpinionInput(sessionId, site, plain)).isFalse();
        assertThat(plain.has("suspected_zones")).isFalse();
    }

    @Test
    void 이전_세팅_사진을_못_찾으면_구역만_싣는다() throws Exception {
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(createRequest().toString()))
                .andExpect(status().isAccepted()).andReturn());
        UUID sessionId = UUID.fromString(created.get("setup_session_id").asString());
        ObjectNode zhc = zoneHeightChanges().put("previous_scene_version", "scene-unknown-" + suffix);
        jdbc.update("UPDATE setup_session SET world_anchor = '{\"accepted\": true}'::jsonb, zone_height_changes = CAST(? AS jsonb) "
                + "WHERE setup_session_id = ?", zhc.toString(), sessionId);
        ObjectNode input = mapper.createObjectNode();
        assertThat(opinions.addOpinionInput(sessionId, site, input)).isTrue();
        assertThat(input.get("suspected_zones").size()).isEqualTo(2);
        assertThat(input.has("previous_frames")).isFalse();
        assertThat(input.has("previous_calibration_ref")).isFalse();
    }

    @Test
    void 콜백_의견_항목의_모양을_먼저_본다() {
        ObjectNode ok = mapper.createObjectNode().put("zone_id", "z").put("status", "NO_OPINION").put("reason", "VLM_FAILED")
                .putNull("recommended_action").putNull("rationale").putNull("confidence").put("camera_id", "cam_A")
                .putNull("model_version").put("prompt_version", "p");
        ok.putArray("before_crop_box_norm").add(0.1).add(0.1).add(0.4).add(0.4);
        ok.putArray("after_crop_box_norm").add(0.1).add(0.1).add(0.4).add(0.4);
        assertThat(SetupZoneOpinions.problem(ok)).isNull();
        assertThat(SetupZoneOpinions.problem(ok.deepCopy().put("recommended_action", "KEEP"))).isNotNull();
        assertThat(SetupZoneOpinions.problem(ok.deepCopy().putNull("before_crop_box_norm"))).isNotNull();
        ObjectNode inverted = ok.deepCopy();
        inverted.putArray("after_crop_box_norm").add(0.5).add(0.1).add(0.4).add(0.4);
        assertThat(SetupZoneOpinions.problem(inverted)).isNotNull();
        assertThat(SetupZoneOpinions.problem(ok.deepCopy().put("status", "OPINION").putNull("reason")
                .put("recommended_action", "KEEP").put("rationale", "그대로입니다.").put("model_version", "m"))).isNull();
        assertThat(SetupZoneOpinions.problem(ok.deepCopy().put("status", "OPINION").putNull("reason")
                .put("recommended_action", "DELETE").put("rationale", "x").put("model_version", "m"))).isNotNull();
    }

    // ------------------------------------------------------------------------------------------------ 도움

    private ObjectNode zoneHeightChanges() {
        ObjectNode zh = mapper.createObjectNode().put("previous_scene_version", prevScene).put("compared_scene_version", previewScene)
                .put("cell_m", 0.1).put("height_diff_threshold_m", 0.15).put("changed_area_ratio_threshold", 0.1);
        ArrayNode zones = zh.putArray("zones");
        ObjectNode near = zones.addObject().put("zone_id", zoneNear).put("status", "CHANGE_SUSPECTED").putNull("reason")
                .put("changed_area_ratio", 0.3).put("max_height_diff_m", 0.3).put("total_cells", 200).put("compared_cells", 180)
                .put("changed_cells", 54);
        near.putArray("max_diff_xy").add(1.0).add(0.5);
        ObjectNode far = zones.addObject().put("zone_id", zoneFar).put("status", "CHANGE_SUSPECTED").putNull("reason")
                .put("changed_area_ratio", 0.5).put("max_height_diff_m", 0.6).put("total_cells", 100).put("compared_cells", 100)
                .put("changed_cells", 50);
        far.putArray("max_diff_xy").add(3.5).add(3.5);
        zones.addObject().put("zone_id", zoneSame).put("status", "UNCHANGED").putNull("reason").put("changed_area_ratio", 0.0)
                .put("max_height_diff_m", 0.02).put("total_cells", 100).put("compared_cells", 100).put("changed_cells", 0)
                .putNull("max_diff_xy");
        return zh;
    }

    private ArrayNode planItems() {
        ArrayNode items = mapper.createArrayNode();
        for (String z : List.of(zoneNear, zoneFar, zoneSame)) {
            items.addObject().put("zone_id", z).put("source", "MANUAL").putNull("candidate_id").putNull("zone").putNull("surface")
                    .put("needs_review", true);
        }
        return items;
    }

    private ObjectNode draftResult(String sessionId) {
        ObjectNode res = mapper.createObjectNode().put("setup_session_id", sessionId).put("site_id", site)
                .put("preview_scene_version", previewScene).put("calibration_version", calibration).put("safety_use", false)
                .put("status", "NO_OPINION").putNull("model_version").put("prompt_version", "vlm-zone-draft-1.0").put("latency_ms", 900);
        res.putArray("drafts");
        return res;
    }

    private String prevFrame(String lease, String camera) {
        return "s3://test/job-objects/site_setup/" + prevJob + "/" + lease + "/frame-" + camera + ".jpg";
    }

    private static String afterFrame(String jobId, String lease) {
        return "s3://test/job-objects/site_setup/" + jobId + "/" + lease + "/frame-cam_A.jpg";
    }

    private void artifact(UUID session, String kind, String version, String jobId, UUID lease, String uri, String camera) {
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256,
                    registered_at, camera_id, face_policy)
                VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?, ?)
                """, session, kind, version, jobId, lease, uri, "d".repeat(64), camera, camera == null ? null : "BLURRED");
    }

    private void zone(String id, String name, String wkt) {
        jdbc.update("""
                INSERT INTO zone(zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from)
                VALUES (?, 1, ?, ?, 'OPENING', 'GROUND_FOOTPRINT', ST_GeomFromText(?, 0), 'HIGH', now())
                """, id, site, name, wkt);
    }

    private JsonNode zonesView(UUID sessionId) throws Exception {
        return body(mvc.perform(get("/api/setup-sessions/{id}/zones", sessionId).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
    }

    private static JsonNode itemOf(JsonNode plan, String zoneId) {
        for (JsonNode item : plan.path("items")) {
            if (zoneId.equals(item.path("zone_id").asString(""))) {
                return item;
            }
        }
        throw new AssertionError("목록에 없는 구역: " + zoneId);
    }

    private ObjectNode createRequest() {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam_A", 2.42).put("cam_B", 2.48).put("cam_C", 2.45).put("cam_D", 2.51);
        return req;
    }

    private ObjectNode snapshot(UUID sessionId, String stage, double pct) {
        ObjectNode node = mapper.createObjectNode().put("setup_session_id", sessionId.toString()).put("status", stage)
                .put("progress_pct", pct);
        ObjectNode checks = node.putObject("height_checks");
        Map<String, Double> heights = Map.of("cam_A", 2.42, "cam_B", 2.48, "cam_C", 2.45, "cam_D", 2.51);
        heights.forEach((cam, h) -> checks.putObject(cam).put("input_height_m", h).put("restored_height_m", h)
                .put("difference_cm", 0.0).put("within_tolerance", true));
        return node;
    }

    private org.springframework.test.web.servlet.RequestBuilder leaseRequest(String token, String worker, String jobType) {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add(jobType);
        p.put("wait_ms", 0);
        return post("/api/worker/jobs/lease").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content(envelope("job_lease_request", "2.0.0", worker, p).toString());
    }

    private org.springframework.test.web.servlet.RequestBuilder heartbeat(String jobId, String leaseId, double progress,
            ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode().put("job_id", jobId).put("lease_id", leaseId).put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.14.0", setupWorker, p);
        assertThat(schemas.violations("messages/job_heartbeat", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + jobId + "/heartbeat").header(HttpHeaders.AUTHORIZATION, "Bearer " + setupToken)
                .contentType(MediaType.APPLICATION_JSON).content(env.toString());
    }

    private org.springframework.test.web.servlet.RequestBuilder callback(String jobId, String leaseId, ObjectNode result) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode().put("job_id", jobId).put("lease_id", leaseId).put("job_type", "vlm_zone_draft")
                .put("status", "SUCCEEDED").put("started_at", now - 3_000).put("finished_at", now);
        p.set("result", result);
        ObjectNode env = envelope("job_callback", "2.23.0", vlmWorker, p);
        assertThat(schemas.violations("messages/job_callback", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + jobId + "/result").header(HttpHeaders.AUTHORIZATION, "Bearer " + vlmToken)
                .contentType(MediaType.APPLICATION_JSON).content(env.toString());
    }

    private ObjectNode envelope(String msgType, String version, String source, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode().put("schema_version", version).put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", Instant.now().toEpochMilli()).put("source_id", source);
        env.set("payload", payload);
        return env;
    }

    private RequestPostProcessor adminJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId))).authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }
}
