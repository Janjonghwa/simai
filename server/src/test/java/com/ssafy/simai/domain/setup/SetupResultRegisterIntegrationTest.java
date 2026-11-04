package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.scene.SceneActivationService;
import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.LocalDate;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalLong;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;
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
import org.springframework.test.web.servlet.RequestBuilder;
import org.springframework.test.web.servlet.request.RequestPostProcessor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 결과 서버 등록(S15P31A101-467): site_setup 하트비트·결과의 artifacts 로 새 보정 문서를 calibration_version 으로, 미리보기·정식 GLB 를
 * scene_version 후보로 등록하고, 세팅 성공 때 새 보정을 실은 구성 묶음(config_version)을 발행한다. 해시 불일치·다른 작업 경로·다른 세션 버전은
 * 거부하고, 실패한 세팅은 구성 묶음을 만들지 않는다. 객체 저장소는 가짜(키 → 바이트)다.
 * S15P31A101-479: 미리보기 스냅샷의 artifacts.camera_frames(카메라별 JPEG)를 세션 귀속(CAMERA_FRAME)으로 등록하고, AI 구역 초안
 * 입력 frames 에 카메라·주소·얼굴 정책으로 싣는다. 해시·JPEG·경로·카메라 집합이 어긋나면 거부한다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupResultRegisterIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String BUCKET = "s3://simai-test-bucket/";

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
    ConfigBundleService configs;

    @Autowired
    SceneActivationService sceneActivations;

    @MockitoBean
    ObjectStorage storage;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String worker = "worker-setup-reg-" + suffix;
    private final String baseCalibration = "cal-reg-base-" + suffix;
    private final String newCalibration = "cal-reg-" + suffix + "-jba2-0001";
    private final String otherCalibration = "cal-reg-" + suffix + "-jba2-0002";
    private final Map<String, byte[]> objects = new ConcurrentHashMap<>();

    private String site;
    private long userId;
    private long workUnitId;
    private String token;

    @BeforeEach
    void setUp() {
        cleanUpTables();
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(i -> BUCKET + i.getArgument(0));
        when(storage.read(anyString(), anyInt())).thenAnswer(i -> Optional.ofNullable(objects.get((String) i.getArgument(0))));
        when(storage.head(anyString())).thenAnswer(i -> Optional.ofNullable(objects.get((String) i.getArgument(0)))
                .map(b -> new ObjectStorage.ObjectHead(b.length, sha256Hex(b))));
        when(storage.size(anyString())).thenAnswer(i -> Optional.ofNullable(objects.get((String) i.getArgument(0)))
                .map(b -> OptionalLong.of(b.length)).orElse(OptionalLong.empty()));

        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        userId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '세팅등록관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "setup-reg-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "세팅등록작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "세팅등록작업-" + suffix);

        // Tier 0 가 지금 쓰는 보정(이전 활성). 새 보정은 이 버전을 previous 로 잇는다
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'bootstrap', now(), 'file:///cal/base.json', ?, now())
                """, baseCalibration, site, "sha256:" + "a".repeat(64));
        jdbc.update("""
                INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                    sent_at, received_at, payload)
                VALUES (?, ?, ?, 'CALIBRATED', 'INITIAL', now() + interval '1 hour', now() + interval '1 hour', ?::jsonb)
                """, UUID.randomUUID(), "tier0-reg-" + suffix, baseCalibration,
                "{\"calibration_version\": \"" + baseCalibration + "\", \"calibration_state\": \"CALIBRATED\"}");

        token = machines.registerWorker(worker, List.of("site_setup")).token();
    }

    @AfterEach
    void tearDown() {
        cleanUpTables();
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
    }

    private void cleanUpTables() {
        List<String> cals = List.of(baseCalibration, newCalibration, otherCalibration);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE active_setup_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_zone_draft");
        jdbc.update("DELETE FROM setup_zone_draft_run");
        jdbc.update("DELETE FROM setup_session");
        // 세팅 보정 행(source_job_id)이 작업을 참조하므로 보정·Scene·묶음을 먼저 지운 뒤 작업을 지운다
        for (String cal : cals) {
            for (String v : jdbc.queryForList("SELECT config_version FROM config_bundle WHERE calibration_version = ?",
                    String.class, cal)) {
                jdbc.update("UPDATE config_bundle SET previous_config_version = NULL WHERE previous_config_version = ?", v);
                jdbc.update("DELETE FROM config_ack WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", v);
            }
            jdbc.update("DELETE FROM scene_version WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_update_message WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_document WHERE calibration_version = ?", cal);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version IN (?, ?)", newCalibration, otherCalibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", baseCalibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "세팅등록작업-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "setup-reg-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void 하트비트로_새_보정과_미리보기를_등록하고_초안을_넣고_성공하면_정식_Scene_과_구성_묶음이_이어진다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";

        // 1) JOINT_BA_AND_VERIFY: 새 보정 문서를 올리고 알린다 → EC2 가 내려받아 해시·계약을 보고 새 calibration_version 으로 등록
        byte[] calDoc = calibrationDocument(newCalibration, s.sessionId());
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calDoc);
        ObjectNode jointSnap = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        jointSnap.putObject("artifacts").set("calibration_document", calRef);
        mvc.perform(heartbeat(s, 0.65, jointSnap)).andExpect(status().isOk());

        Map<String, Object> calRow = jdbc.queryForMap("""
                SELECT site_id, previous_calibration_version, state, provider_source, calibration_ref, source_job_id
                FROM calibration_version WHERE calibration_version = ?
                """, newCalibration);
        assertThat(calRow.get("site_id")).isEqualTo(site);
        assertThat(calRow.get("previous_calibration_version")).isEqualTo(baseCalibration);
        assertThat(calRow.get("state")).isEqualTo("PROVISIONAL");
        assertThat(calRow.get("calibration_ref")).isEqualTo(BUCKET + prefix + "calibration-" + newCalibration + ".json");
        assertThat(calRow.get("source_job_id")).isEqualTo(s.jobId());
        assertThat(jdbc.queryForObject("SELECT sha256 FROM calibration_document WHERE calibration_version = ?", String.class,
                newCalibration)).isEqualTo("sha256:" + sha256Hex(calDoc));
        // 이전 보정 행은 그대로(덮어쓰지 않음)
        assertThat(jdbc.queryForObject("SELECT state FROM calibration_version WHERE calibration_version = ?", String.class,
                baseCalibration)).isEqualTo("CALIBRATED");

        // 2) READY_FOR_ZONES: 미리보기 GLB 를 알린다 → scene_version 후보 등록 → -455 초안 작업이 이어진다
        String preview = "scene-preview-" + s.jobId() + "-" + s.leaseId();
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview);
        ObjectNode readyArtifacts = readySnap.putObject("artifacts");
        readyArtifacts.set("calibration_document", calRef);
        readyArtifacts.set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());

        Map<String, Object> previewRow = jdbc.queryForMap(
                "SELECT site_id, calibration_version, state, gltf_key, quality::text AS quality FROM scene_version WHERE scene_version = ?",
                preview);
        assertThat(previewRow.get("calibration_version")).isEqualTo(newCalibration);
        // 후보로 등록된 뒤 READY 커밋 뒤 자동 현재화로 COMMITTED(활성 이력 추가)
        assertThat(previewRow.get("state")).isEqualTo("COMMITTED");
        assertThat(previewRow.get("gltf_key")).isEqualTo(prefix + "preview_scene.glb");
        assertThat((String) previewRow.get("quality")).contains("scene_tier=PREVIEW");
        JsonNode draftInput = mapper.readTree(jdbc.queryForObject(
                "SELECT input::text FROM analysis_job WHERE job_type = 'vlm_zone_draft'", String.class));
        assertThat(draftInput.get("calibration_version").asString()).isEqualTo(newCalibration);
        assertThat(draftInput.get("preview_scene_version").asString()).isEqualTo(preview);
        assertThat(draftInput.get("calibration_ref").asString()).isEqualTo(BUCKET + prefix + "calibration-" + newCalibration + ".json");
        // READY_FOR_ZONES(새 보정·미리보기 등록 완료)에서 새 보정을 실은 구성 묶음을 발행해 세션에 연결한다
        String configVersion = jdbc.queryForObject(
                "SELECT config_version FROM config_bundle WHERE calibration_version = ?", String.class, newCalibration);
        assertThat(configVersion).startsWith("cfg-");
        assertThat(jdbc.queryForObject("SELECT content FROM config_bundle_file WHERE config_version = ? AND name = 'calibration'",
                byte[].class, configVersion)).isEqualTo(calDoc);
        assertThat(jdbc.queryForObject("SELECT config_version FROM setup_session WHERE setup_session_id = ?::uuid", String.class,
                s.sessionId())).isEqualTo(configVersion);
        // 굽기 도중 관리자가 묶음을 발행해도(Tier 0 가 아직 새 보정을 알리기 전) 새 보정을 싣는다
        JsonNode during = configs.publish(null, userId);
        assertThat(during.get("calibration_version").asString()).isEqualTo(newCalibration);
        // 세팅 화면·뷰어가 보는 현재 Scene 이 이 세션의 미리보기(PREVIEW 표시 유지)
        assertThat(currentScene()).isEqualTo(preview);
        assertThat(jdbc.queryForObject("SELECT quality::text FROM scene_version WHERE scene_version = ?", String.class, preview))
                .contains("scene_tier=PREVIEW").contains("safety_use=false");

        // 3) 결과 SUCCEEDED: 정식 GLB 등록, 세션 COMPLETED, 새 보정을 실은 구성 묶음 발행·연결
        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode sceneRef = put(prefix + "scene.glb", "glb-baked-texture".getBytes(StandardCharsets.UTF_8));
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview).put("scene_version", baked);
        ObjectNode resultArtifacts = result.putObject("artifacts");
        resultArtifacts.set("calibration_document", calRef);
        resultArtifacts.set("preview_scene", previewRef);
        resultArtifacts.set("scene", sceneRef);
        mvc.perform(callback(s, succeeded(s, result)))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        // 현재 Scene 이 이 세션의 미리보기였으므로 정식 Scene 으로 교체
        assertThat(currentScene()).isEqualTo(baked);
        assertThat(jdbc.queryForObject("SELECT state FROM scene_version WHERE scene_version = ?", String.class, baked))
                .isEqualTo("COMMITTED");
        // 결과 성공 때는 이미 그 보정의 묶음이 있으니 다시 만들지 않는다(굽기 중 발행분까지 2개)
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class,
                newCalibration)).isEqualTo(2);
        String latestConfig = during.get("config_version").asString();
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.calibration_version").value(newCalibration))
                .andExpect(jsonPath("$.preview_scene_version").value(preview))
                .andExpect(jsonPath("$.scene_version").value(baked))
                .andExpect(jsonPath("$.config_version").value(latestConfig));
        assertThat(jdbc.queryForList("SELECT kind FROM setup_session_artifact WHERE setup_session_id = ?::uuid ORDER BY kind",
                String.class, s.sessionId())).containsExactly("CALIBRATION", "PREVIEW_SCENE", "SCENE");
    }

    @Test
    void 해시가_다르거나_다른_작업_경로의_보정_문서는_거부하고_등록하지_않는다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        byte[] calDoc = calibrationDocument(newCalibration, s.sessionId());

        // 해시 불일치(올린 바이트와 알린 sha256 이 다름)
        ObjectNode wrongHash = put(prefix + "calibration.json", calDoc).put("sha256", "0".repeat(64));
        ObjectNode snap = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        snap.putObject("artifacts").set("calibration_document", wrongHash);
        mvc.perform(heartbeat(s, 0.65, snap))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // 다른 작업의 결과 경로(키 앞부분이 이 작업·이 대여가 아님)
        ObjectNode otherJob = put("job-objects/site_setup/job-other/" + s.leaseId() + "/calibration.json", calDoc);
        ObjectNode snap2 = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        snap2.putObject("artifacts").set("calibration_document", otherJob);
        mvc.perform(heartbeat(s, 0.65, snap2))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // 문서의 calibration_version 이 스냅샷과 다름
        ObjectNode mismatched = put(prefix + "calibration-x.json", calibrationDocument(otherCalibration, s.sessionId()));
        ObjectNode snap3 = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        snap3.putObject("artifacts").set("calibration_document", mismatched);
        mvc.perform(heartbeat(s, 0.65, snap3))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // 문서의 세팅 출처(setup_session_id)가 이 세션이 아님
        ObjectNode foreign = put(prefix + "calibration-y.json", calibrationDocument(newCalibration, UUID.randomUUID().toString()));
        ObjectNode snap4 = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        snap4.putObject("artifacts").set("calibration_document", foreign);
        mvc.perform(heartbeat(s, 0.65, snap4))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version IN (?, ?)",
                Integer.class, newCalibration, otherCalibration)).isZero();
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_artifact", Integer.class)).isZero();
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COLLECTING_BACKGROUND"))
                .andExpect(jsonPath("$.calibration_version").isEmpty());
    }

    @Test
    void 실패한_세팅은_구성_묶음을_만들지_않고_그_세션의_보정은_다른_세션이_쓸_수_없다() throws Exception {
        Session a = startSession();
        String prefixA = "job-objects/site_setup/" + a.jobId() + "/" + a.leaseId() + "/";
        byte[] calDoc = calibrationDocument(newCalibration, a.sessionId());
        ObjectNode calRef = put(prefixA + "calibration-" + newCalibration + ".json", calDoc);
        ObjectNode snap = snapshot(a.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        snap.putObject("artifacts").set("calibration_document", calRef);
        mvc.perform(heartbeat(a, 0.65, snap)).andExpect(status().isOk());

        // 미리보기 단계에서 실패 → 세션 FAILED, 새 보정으로 구성 묶음을 만들지 않는다(Tier 0 는 이전 보정 그대로)
        String latestBefore = latestConfigVersion();
        mvc.perform(callback(a, failed(a, "STATIC_PREVIEW_TOOL_FAILED", "미리보기 단계 실패")))
                .andExpect(status().isOk());
        mvc.perform(get("/api/setup-sessions/{id}", a.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("FAILED"))
                .andExpect(jsonPath("$.config_version").isEmpty());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class,
                newCalibration)).isZero();
        assertThat(latestConfigVersion()).isEqualTo(latestBefore);
        mvc.perform(get("/api/calibrations/current").with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.calibration_version").value(baseCalibration));

        // 재시도 세션 B 가 A 의 보정 버전을 artifacts 없이 참조하면 거부(다른 세션 귀속)
        JsonNode retried = body(mvc.perform(post("/api/setup-sessions/{id}/retry", a.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isAccepted()).andReturn());
        Session b = lease(retried.get("setup_session_id").asString(), retried.get("job_id").asString());
        ObjectNode borrowed = snapshot(b.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        mvc.perform(heartbeat(b, 0.65, borrowed))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // A 작업 경로의 객체를 B 이름으로 알려도 거부(이 작업·이 대여 경로가 아님)
        ObjectNode borrowedRef = snapshot(b.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        borrowedRef.putObject("artifacts").set("calibration_document", calRef);
        mvc.perform(heartbeat(b, 0.65, borrowedRef))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        // B 가 자기 작업 경로에 같은 보정 내용(결정론적 재시도, 출처만 B)을 올려 알리면 받는다(원문은 처음 것을 둔다)
        ObjectNode ownRef = put("job-objects/site_setup/" + b.jobId() + "/" + b.leaseId() + "/calibration-" + newCalibration
                + ".json", calibrationDocument(newCalibration, b.sessionId()));
        ObjectNode own = snapshot(b.sessionId(), "JOINT_BA_AND_VERIFY", 65.0).put("calibration_version", newCalibration);
        own.putObject("artifacts").set("calibration_document", ownRef);
        mvc.perform(heartbeat(b, 0.65, own)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_artifact WHERE version = ?", Integer.class,
                newCalibration)).isEqualTo(2);
        assertThat(jdbc.queryForObject("SELECT sha256 FROM calibration_document WHERE calibration_version = ?", String.class,
                newCalibration)).isEqualTo("sha256:" + sha256Hex(calDoc));
    }

    @Test
    void 진행_중_작업이_있으면_세팅을_시작하지_않고_세팅_도중_작업이_시작되면_새_보정_묶음을_발행하지_않는다() throws Exception {
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam1", 2.42).put("cam2", 2.48).put("cam3", 2.45).put("cam4", 2.51);
        mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(req.toString()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_BLOCKED_BY_LIVE_WORK"));
        jdbc.update("UPDATE work_unit SET status = 'PLANNED', actual_started_at = NULL WHERE work_id = ?", workUnitId);

        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        // 세팅 도중(직접 DB 로) 작업이 시작됐다고 두면 READY 에서도 묶음을 만들지 않는다
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", "scene-preview-live-" + suffix);
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef);
        artifacts.set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version = ?", Integer.class,
                newCalibration)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class,
                newCalibration)).isZero();
        assertThat(currentScene()).isNotEqualTo("scene-preview-live-" + suffix);
        jdbc.update("UPDATE work_unit SET status = 'PLANNED', actual_started_at = NULL WHERE work_id = ?", workUnitId);
    }

    @Test
    void 관리자가_그_사이_다른_Scene_을_고르면_정식_Scene_으로_바꾸지_않는다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
        String preview = "scene-preview-adm-" + suffix;
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview);
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef);
        artifacts.set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(preview);

        // 관리자가 다른 후보 Scene 을 현재로 고른다
        String chosen = "scene-chosen-" + suffix;
        put(prefix + "chosen.glb", "glb-chosen".getBytes(StandardCharsets.UTF_8));
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound, requested_by)
                VALUES (?, ?, ?, 'CANDIDATE', ?, false, ?)
                """, chosen, site, newCalibration, prefix + "chosen.glb", userId);
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256, size_bytes,
                    registered_at)
                VALUES (?::uuid, 'PREVIEW_SCENE', ?, ?, ?::uuid, ?, ?, ?, now())
                """, s.sessionId(), chosen, s.jobId(), s.leaseId(), BUCKET + prefix + "chosen.glb",
                sha256Hex("glb-chosen".getBytes(StandardCharsets.UTF_8)), "glb-chosen".length());
        sceneActivations.activate(site, chosen, "user:" + userId, "관리자 선택");
        // 이어지는 하트비트가 관리자 선택을 되돌리지 않는다
        ObjectNode bakingSnap = readySnap.deepCopy().put("status", "BAKING_TEXTURE").put("progress_pct", 95.0);
        mvc.perform(heartbeat(s, 0.95, bakingSnap)).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(chosen);

        String baked = "scene-baked-adm-" + suffix;
        ObjectNode sceneRef = put(prefix + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8));
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview).put("scene_version", baked);
        ObjectNode resultArtifacts = result.putObject("artifacts");
        resultArtifacts.set("calibration_document", calRef);
        resultArtifacts.set("preview_scene", previewRef);
        resultArtifacts.set("scene", sceneRef);
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(chosen);
        assertThat(jdbc.queryForObject("SELECT state FROM scene_version WHERE scene_version = ?", String.class, baked))
                .isEqualTo("CANDIDATE");
    }

    @Test
    void 미리보기_스냅샷의_카메라_프레임을_세션에_등록하고_구역_초안_입력_frames_에_싣는다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ArrayNode frames = mapper.createArrayNode();
        for (String cam : List.of("cam1", "cam2", "cam3", "cam4")) {
            frames.add(frameRef(cam, put(prefix + "frame-" + cam + ".jpg", jpeg(cam)), "BLURRED"));
        }
        // 이 세션의 예전 시도(다른 대여)가 남긴 더 최근 프레임: 미리보기를 등록한 그 시도의 프레임만 입력에 싣는다
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256, size_bytes,
                    registered_at, camera_id, face_policy)
                VALUES (?::uuid, 'CAMERA_FRAME', 'cam1@eeeeeeeeeeee', 'job-old', ?::uuid, ?, ?, 10, now() + interval '1 hour', 'cam1',
                    'EXCLUDED')
                """, s.sessionId(), UUID.randomUUID().toString(), BUCKET + "job-objects/site_setup/job-old/lease-old/frame-cam1.jpg",
                "e".repeat(64));

        String preview = "scene-preview-" + s.jobId() + "-" + s.leaseId();
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "PREVIEW_AND_VLM_DRAFT", 80.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview);
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef);
        artifacts.set("preview_scene", previewRef);
        artifacts.set("camera_frames", frames);
        mvc.perform(heartbeat(s, 0.8, readySnap)).andExpect(status().isOk());

        assertThat(jdbc.queryForList("""
                SELECT camera_id || ':' || face_policy || ':' || version FROM setup_session_artifact
                WHERE setup_session_id = ?::uuid AND kind = 'CAMERA_FRAME' AND job_id = ? ORDER BY camera_id
                """, String.class, s.sessionId(), s.jobId()))
                .containsExactly("cam1:BLURRED:cam1@" + sha256Hex(jpeg("cam1")).substring(0, 12),
                        "cam2:BLURRED:cam2@" + sha256Hex(jpeg("cam2")).substring(0, 12),
                        "cam3:BLURRED:cam3@" + sha256Hex(jpeg("cam3")).substring(0, 12),
                        "cam4:BLURRED:cam4@" + sha256Hex(jpeg("cam4")).substring(0, 12));

        JsonNode draftInput = mapper.readTree(jdbc.queryForObject(
                "SELECT input::text FROM analysis_job WHERE job_type = 'vlm_zone_draft'", String.class));
        JsonNode inputFrames = draftInput.get("frames");
        assertThat(inputFrames).isNotNull();
        assertThat(inputFrames.size()).isEqualTo(4);
        for (int i = 0; i < 4; i++) {
            String cam = "cam" + (i + 1);
            assertThat(inputFrames.get(i).get("camera_id").asString()).isEqualTo(cam);
            assertThat(inputFrames.get(i).get("frame_uri").asString()).isEqualTo(BUCKET + prefix + "frame-" + cam + ".jpg");
            assertThat(inputFrames.get(i).get("face_policy").asString()).isEqualTo("BLURRED");
            assertThat(inputFrames.get(i).size()).isEqualTo(3);  // 계약 frame_input: 이 세 필드만 싣는다
        }
        assertThat(draftInput.get("preview_scene_uri").asString()).isEqualTo(BUCKET + prefix + "preview_scene.glb");

        // 이어지는 하트비트가 같은 프레임을 다시 알려도 다시 읽지 않고 중복 등록하지 않는다
        long before = jdbc.queryForObject("SELECT count(*) FROM setup_session_artifact WHERE kind = 'CAMERA_FRAME'", Long.class);
        ObjectNode again = readySnap.deepCopy().put("status", "READY_FOR_ZONES").put("progress_pct", 85.0);
        mvc.perform(heartbeat(s, 0.85, again)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_artifact WHERE kind = 'CAMERA_FRAME'", Long.class))
                .isEqualTo(before);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE job_type = 'vlm_zone_draft'", Integer.class)).isEqualTo(1);
    }

    @Test
    void 프레임이_없으면_초안_입력에_frames_를_싣지_않는다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", "scene-preview-nf-" + suffix);
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef);
        artifacts.set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());
        JsonNode draftInput = mapper.readTree(jdbc.queryForObject(
                "SELECT input::text FROM analysis_job WHERE job_type = 'vlm_zone_draft'", String.class));
        assertThat(draftInput.has("frames")).isFalse();
    }

    @Test
    void 해시_JPEG_경로_카메라_집합이_어긋난_프레임은_거부하고_등록하지_않는다() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        byte[] ok = jpeg("cam1");

        // 알린 sha256 이 올린 바이트와 다름
        assertFrameRejected(s, frameRef("cam1", put(prefix + "frame-a.jpg", ok).put("sha256", "0".repeat(64)), "BLURRED"));
        // JPEG 가 아님(PNG 바이트)
        assertFrameRejected(s, frameRef("cam1", put(prefix + "frame-b.jpg", new byte[] {(byte) 0x89, 'P', 'N', 'G', 1, 2}), "BLURRED"));
        // 이 작업이 아닌 다른 작업의 결과 경로
        assertFrameRejected(s, frameRef("cam1", put("job-objects/site_setup/job-other/" + s.leaseId() + "/frame-c.jpg", ok), "BLURRED"));
        // 세션에 없는 카메라
        assertFrameRejected(s, frameRef("camX", put(prefix + "frame-d.jpg", ok), "BLURRED"));
        // 올리지 않은 객체
        assertFrameRejected(s, frameRef("cam1", mapper.createObjectNode().put("uri", BUCKET + prefix + "frame-missing.jpg")
                .put("sha256", sha256Hex(ok)).put("size_bytes", ok.length), "BLURRED"));
        // 같은 스냅샷에 카메라 중복
        ObjectNode dup = put(prefix + "frame-e.jpg", ok);
        ObjectNode dupSnap = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0);
        dupSnap.putObject("artifacts").putArray("camera_frames").add(frameRef("cam1", dup, "BLURRED")).add(frameRef("cam1", dup, "EXCLUDED"));
        mvc.perform(heartbeat(s, 0.65, dupSnap)).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session_artifact", Integer.class)).isZero();
        // 올바른 프레임은 받는다(거부가 세션을 망가뜨리지 않았다)
        ObjectNode fine = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0);
        fine.putObject("artifacts").putArray("camera_frames").add(frameRef("cam2", put(prefix + "frame-ok.jpg", jpeg("cam2")), "EXCLUDED"));
        mvc.perform(heartbeat(s, 0.65, fine)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT face_policy FROM setup_session_artifact WHERE kind = 'CAMERA_FRAME'", String.class))
                .isEqualTo("EXCLUDED");
    }

    private void assertFrameRejected(Session s, ObjectNode frame) throws Exception {
        ObjectNode snap = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0);
        snap.putObject("artifacts").putArray("camera_frames").add(frame);
        mvc.perform(heartbeat(s, 0.65, snap)).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));
    }

    private ObjectNode frameRef(String cameraId, ObjectNode ref, String facePolicy) {
        ObjectNode out = mapper.createObjectNode().put("camera_id", cameraId);
        out.setAll(ref);
        return out.put("face_policy", facePolicy);
    }

    /** JPEG 시그니처(FF D8 FF)로 시작하는 카메라별로 다른 바이트 */
    private static byte[] jpeg(String tag) {
        byte[] body = ("jpeg-frame-" + tag).getBytes(StandardCharsets.UTF_8);
        byte[] out = new byte[body.length + 4];
        out[0] = (byte) 0xFF;
        out[1] = (byte) 0xD8;
        out[2] = (byte) 0xFF;
        out[3] = (byte) 0xE0;
        System.arraycopy(body, 0, out, 4, body.length);
        return out;
    }

    private String currentScene() {
        return jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s WHERE site_id = ?",
                String.class, site).stream().findFirst().orElse(null);
    }

    private record Session(String sessionId, String jobId, String leaseId) {
    }

    private Session startSession() throws Exception {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam1", 2.42).put("cam2", 2.48).put("cam3", 2.45).put("cam4", 2.51);
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(req.toString()))
                .andExpect(status().isAccepted()).andReturn());
        return lease(created.get("setup_session_id").asString(), created.get("job_id").asString());
    }

    private Session lease(String sessionId, String jobId) throws Exception {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add("site_setup");
        p.put("wait_ms", 0);
        JsonNode leased = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", p).toString()))
                .andExpect(status().isOk()).andReturn());
        assertThat(leased.at("/payload/job_id").asString()).isEqualTo(jobId);
        return new Session(sessionId, jobId, leased.at("/payload/lease_id").asString());
    }

    /** 가짜 저장소에 올리고 계약 object_ref 를 돌려준다 */
    private ObjectNode put(String key, byte[] content) {
        objects.put(key, content);
        return mapper.createObjectNode().put("uri", BUCKET + key).put("sha256", sha256Hex(content))
                .put("size_bytes", content.length);
    }

    /** 계약 예시 보정 문서를 세션 카메라 4대·이 현장·지정 버전·세팅 출처(2.2.0)로 바꾼 원문 바이트 */
    private byte[] calibrationDocument(String version, String sessionId) throws Exception {
        ObjectNode doc = (ObjectNode) mapper.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        doc.put("schema_version", "2.2.0");
        doc.put("calibration_version", version);
        doc.put("setup_session_id", sessionId);
        doc.put("previous_calibration_version", baseCalibration);
        ((ObjectNode) doc.get("coordinate_frame")).put("site_id", site);
        ArrayNode cameras = (ArrayNode) doc.get("cameras");
        JsonNode template = cameras.get(0);
        cameras.removeAll();
        for (String cam : List.of("cam1", "cam2", "cam3", "cam4")) {
            cameras.add(((ObjectNode) template.deepCopy()).put("camera_id", cam));
        }
        doc.remove("content_hash");
        byte[] content = mapper.writeValueAsBytes(doc);
        assertThat(schemas.violations("providers/calibration", new String(content, StandardCharsets.UTF_8))).isEmpty();
        return content;
    }

    private ObjectNode snapshot(String sessionId, String stage, double pct) {
        return mapper.createObjectNode().put("setup_session_id", sessionId).put("status", stage).put("progress_pct", pct);
    }

    private ObjectNode withChecks(ObjectNode node) {
        ObjectNode checks = node.putObject("height_checks");
        double[] heights = {2.42, 2.48, 2.45, 2.51};
        for (int i = 0; i < 4; i++) {
            checks.putObject("cam" + (i + 1)).put("input_height_m", heights[i]).put("restored_height_m", heights[i])
                    .put("difference_cm", 0.0).put("within_tolerance", true);
        }
        return node;
    }

    private RequestBuilder heartbeat(Session s, double progress, ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId()).put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.3.0", p);
        assertThat(schemas.violations("messages/job_heartbeat", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + s.jobId() + "/heartbeat")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(env.toString());
    }

    private ObjectNode succeeded(Session s, ObjectNode result) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "SUCCEEDED").put("started_at", now - 60_000).put("finished_at", now);
        p.set("result", result);
        return p;
    }

    private ObjectNode failed(Session s, String code, String message) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "FAILED").put("started_at", now - 60_000).put("finished_at", now);
        p.putObject("error").put("code", code).put("message", message).put("retryable", false);
        return p;
    }

    private RequestBuilder callback(Session s, ObjectNode payload) {
        ObjectNode env = envelope("job_callback", "2.10.0", payload);
        assertThat(schemas.violations("messages/job_callback", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + s.jobId() + "/result")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(env.toString());
    }

    private String latestConfigVersion() {
        return jdbc.queryForList("SELECT config_version FROM config_bundle WHERE site_id = ? ORDER BY issued_at DESC LIMIT 1",
                String.class, site).stream().findFirst().orElse(null);
    }

    private ObjectNode envelope(String msgType, String version, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode()
                .put("schema_version", version)
                .put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString())
                .put("sent_at", Instant.now().toEpochMilli())
                .put("source_id", worker);
        env.set("payload", payload);
        return env;
    }

    private RequestPostProcessor adminJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId))).authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
    }

    private RequestPostProcessor viewerJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId))).authorities(new SimpleGrantedAuthority("ROLE_VIEWER"));
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }

    private static String sha256Hex(byte[] content) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
