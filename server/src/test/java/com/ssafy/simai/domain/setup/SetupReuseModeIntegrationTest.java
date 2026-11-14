package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.doReturn;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.scene.SceneCompatibility;
import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
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
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.RequestBuilder;
import org.springframework.test.web.servlet.request.RequestPostProcessor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 모드 새 모델 / 이전 모델 재사용(S15P31A101-614). POST /api/setup-sessions 의 mode 검사, 날짜 작업 오늘만 검사, REUSE 채택
 * (현재 관제 맵 그대로·새 보정만 등록·구성 묶음 발행·Scene 정합 VERIFIED), REUSE 거부 → 새 모델로 이어 가기, 재사용 세션의 맵 보강 차단을 본다.
 * 객체 저장소는 가짜(키 → 바이트)다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupReuseModeIntegrationTest {

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
    SceneCompatibility compatibility;

    @MockitoBean
    ObjectStorage storage;

    /** Tier 0 회전 감시(-540) 재세팅 카메라만 바꿔 본다(S15P31A101-689). 기본은 Tier 0 끊김(모름) */
    @MockitoSpyBean
    ViewerHealthService health;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String worker = "worker-setup-reuse-" + suffix;
    private final String baseCalibration = "cal-reuse-base-" + suffix;
    private final String baseScene = "scene-reuse-base-" + suffix;
    private final String newCalibration = "cal-reuse-" + suffix + "-jba2-0001";
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
                VALUES (?, 'hash', '재사용세팅관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "setup-reuse-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(ZoneId.of("Asia/Seoul")), "재사용세팅작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "재사용세팅작업-" + suffix);

        // Tier 0 가 지금 쓰는 보정(이전 활성). 새 보정은 이 버전을 previous·world_anchor 기준으로 잇는다
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'bootstrap', now(), 'file:///cal/base.json', ?, now())
                """, baseCalibration, site, "sha256:" + "a".repeat(64));
        jdbc.update("""
                INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                    sent_at, received_at, payload)
                VALUES (?, ?, ?, 'CALIBRATED', 'INITIAL', now() + interval '1 hour', now() + interval '1 hour', ?::jsonb)
                """, UUID.randomUUID(), "tier0-reuse-" + suffix, baseCalibration,
                "{\"calibration_version\": \"" + baseCalibration + "\", \"calibration_state\": \"CALIBRATED\"}");

        token = machines.registerWorker(worker, List.of("site_setup")).token();
        doReturn(null).when(health).reSetupCameraIds();
    }

    @AfterEach
    void tearDown() {
        cleanUpTables();
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
    }

    private void cleanUpTables() {
        List<String> cals = List.of(baseCalibration, newCalibration);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE active_setup_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_zone_draft");
        jdbc.update("DELETE FROM setup_zone_draft_run");
        jdbc.update("DELETE FROM setup_session");
        for (String cal : cals) {
            for (String v : jdbc.queryForList("SELECT config_version FROM config_bundle WHERE calibration_version = ?",
                    String.class, cal)) {
                jdbc.update("UPDATE config_bundle SET previous_config_version = NULL WHERE previous_config_version = ?", v);
                jdbc.update("DELETE FROM config_ack WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", v);
            }
            jdbc.update("DELETE FROM floor_coverage WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM scene_version WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_update_message WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_document WHERE calibration_version = ?", cal);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", newCalibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", baseCalibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "재사용세팅작업-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "setup-reuse-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void 모드_값이_틀리면_400_이고_현재_관제_맵이_없으면_재사용을_409_로_막는다() throws Exception {
        mvc.perform(create("BOGUS")).andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_SETUP_REQUEST"));
        mvc.perform(create("REUSE")).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_SCENE_MISSING"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session", Integer.class)).isZero();
        // 기본(mode 없음)은 지금처럼 새 모델이고 작업 입력에 mode 를 싣지 않는다
        JsonNode created = body(mvc.perform(create(null)).andExpect(status().isAccepted()).andReturn());
        assertThat(created.get("mode").asString()).isEqualTo("NEW");
        assertThat(created.get("requested_mode").asString()).isEqualTo("NEW");
        assertThat(created.get("world_anchor").isNull()).isTrue();
        assertThat(created.get("reuse_fallback_reason").isNull()).isTrue();
        assertThat(jobInput(created.get("job_id").asString()).has("mode")).isFalse();
    }

    @Test
    void 날짜_작업은_작업_날짜가_오늘일_때만_세팅을_시작한다() throws Exception {
        // -610 의 WorkUnitRepository.requireToday(날짜 작업 daily=true 만)를 세팅 시작에서 부른다
        LocalDate today = LocalDate.now(ZoneId.of("Asia/Seoul"));
        // 날짜 작업은 현장·날짜마다 하나라(유일 색인) 다른 날짜 작업과 겹치지 않을 먼 과거 날짜를 쓴다
        jdbc.update("UPDATE work_unit SET daily = true, work_date = ? WHERE work_id = ?", today.minusDays(4000), workUnitId);
        mvc.perform(create(null)).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_NOT_TODAY"));
        // 예전 작업(daily=false)은 날짜를 보지 않는다
        jdbc.update("UPDATE work_unit SET daily = false WHERE work_id = ?", workUnitId);
        mvc.perform(create(null)).andExpect(status().isAccepted());
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE work_id = ?", workUnitId);
        jdbc.update("DELETE FROM setup_session");
        // 오늘 날짜 작업은 받는다(오늘 날짜 작업이 이미 따로 있으면 유일 색인 때문에 이 확인은 건너뛴다)
        Integer todayDaily = jdbc.queryForObject("SELECT count(*) FROM work_unit WHERE site_id = ? AND work_date = ? AND daily",
                Integer.class, site, today);
        if (todayDaily != null && todayDaily == 0) {
            jdbc.update("UPDATE work_unit SET daily = true, work_date = ? WHERE work_id = ?", today, workUnitId);
            mvc.perform(create(null)).andExpect(status().isAccepted());
        }
    }

    @Test
    void 재사용이_채택되면_현재_관제_맵을_그대로_두고_새_보정만_등록해_묶음을_발행하고_맵은_같은_World_로_판정된다() throws Exception {
        commitBaseScene();
        JsonNode created = body(mvc.perform(create("REUSE")).andExpect(status().isAccepted()).andReturn());
        assertThat(created.get("mode").asString()).isEqualTo("REUSE");
        assertThat(created.get("requested_mode").asString()).isEqualTo("REUSE");
        assertThat(jobInput(created.get("job_id").asString()).get("mode").asString()).isEqualTo("REUSE_SCENE");
        Session s = lease(created.get("setup_session_id").asString(), created.get("job_id").asString());
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";

        ObjectNode before = snapshot(s.sessionId(), "INITIAL_POSE_AND_WORLD", 40.0).put("mode", "REUSE_SCENE");
        mvc.perform(heartbeat(s, 0.40, before)).andExpect(status().isOk());

        byte[] calDoc = calibrationDocument(newCalibration, s.sessionId(), true);
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calDoc);
        ObjectNode joint = withChecks(snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0))
                .put("mode", "REUSE_SCENE").put("calibration_version", newCalibration);
        joint.set("world_anchor", anchor(true));
        joint.putObject("artifacts").set("calibration_document", calRef);
        mvc.perform(heartbeat(s, 0.65, joint)).andExpect(status().isOk());

        ObjectNode ready = joint.deepCopy().put("status", "READY_FOR_ZONES").put("progress_pct", 85.0);
        mvc.perform(heartbeat(s, 0.85, ready)).andExpect(status().isOk());
        // READY_FOR_ZONES: 묶음은 아직(완료가 발행, -615), 현재 Scene 은 그대로, AI 구역 초안도 없다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class,
                newCalibration)).isZero();
        assertThat(currentScene()).isEqualTo(baseScene);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE job_type = 'vlm_zone_draft'", Integer.class)).isZero();

        // 세팅 완료(C17): 재사용은 지금 관제 맵을 그대로 두고 새 보정을 실은 구성 묶음만 한 번 발행한다
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class,
                newCalibration)).isEqualTo(1);
        assertThat(currentScene()).isEqualTo(baseScene);

        ObjectNode result = joint.deepCopy().put("status", "COMPLETED").put("progress_pct", 100.0);
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        assertThat(currentScene()).isEqualTo(baseScene);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE calibration_version = ?", Integer.class,
                newCalibration)).isZero();
        assertThat(jdbc.queryForList("SELECT kind FROM setup_session_artifact WHERE setup_session_id = ?::uuid", String.class,
                s.sessionId())).containsExactly("CALIBRATION");
        JsonNode view = body(mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(view.get("status").asString()).isEqualTo("COMPLETED");
        assertThat(view.get("mode").asString()).isEqualTo("REUSE");
        assertThat(view.get("calibration_version").asString()).isEqualTo(newCalibration);
        assertThat(view.get("preview_scene_version").isNull()).isTrue();
        assertThat(view.get("scene_version").isNull()).isTrue();
        assertThat(view.get("ready_at_ms").isNull()).isFalse();
        assertThat(view.at("/world_anchor/accepted").asBoolean()).isTrue();
        assertThat(view.at("/world_anchor/reference_calibration_version").asString()).isEqualTo(baseCalibration);
        // 이 세션이 만든 맵이 없어 맵 보강은 막는다(-607)
        assertThat(view.at("/supplement/can_rebake").asBoolean()).isFalse();
        assertThat(view.at("/supplement/rebake_blocked_reason").asString()).isEqualTo("REUSED_SCENE");
        assertThat(view.at("/supplement/can_upload").asBoolean()).isFalse();

        // Tier 0 가 새 보정을 새 World 계보(world_frame_id = 새 버전)로 채택해도, 보정 문서의 world_anchor 가 맵 보정의 World 로
        // 옮겼으므로 현재 맵은 흐리지 않는다(자세 변화는 채택 한도로 판정)
        tier0Adopted(baseCalibration, baseCalibration);
        tier0Adopted(newCalibration, newCalibration);
        JsonNode compat = compatibility.compare(baseScene, baseCalibration, newCalibration);
        assertThat(compat.get("verification").asString()).isEqualTo("VERIFIED");
        assertThat(compat.get("reason").asString()).isEqualTo("SMALL_CHANGE");
    }

    @Test
    void 옛_워커가_재사용_거부_뒤_같은_작업에서_새_모델로_이어_가면_세션은_NEW_와_사유를_보인다() throws Exception {
        // site_setup 1.17.0 까지의 워커 호환(-614). 1.18.0 워커는 이어 가지 않고 실패로 멈춘다(S15P31A101-689, 아래 시험)
        commitBaseScene();
        JsonNode created = body(mvc.perform(create("REUSE")).andExpect(status().isAccepted()).andReturn());
        Session s = lease(created.get("setup_session_id").asString(), created.get("job_id").asString());
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";

        // REUSE 요청 작업이 판정 전에 FULL_SETUP 을 이유 없이 보내면 거부한다
        ObjectNode wrong = snapshot(s.sessionId(), "INITIAL_POSE_AND_WORLD", 40.0).put("mode", "FULL_SETUP");
        mvc.perform(heartbeat(s, 0.40, wrong)).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));

        byte[] calDoc = calibrationDocument(newCalibration, s.sessionId(), false);
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calDoc);
        ObjectNode joint = withChecks(snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 65.0))
                .put("mode", "FULL_SETUP").put("reuse_fallback_reason", "CENTER_RESIDUAL_EXCEEDED")
                .put("calibration_version", newCalibration);
        joint.set("world_anchor", anchor(false));
        joint.putObject("artifacts").set("calibration_document", calRef);
        mvc.perform(heartbeat(s, 0.65, joint)).andExpect(status().isOk());
        JsonNode view = body(mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt())).andReturn());
        assertThat(view.get("mode").asString()).isEqualTo("NEW");
        assertThat(view.get("requested_mode").asString()).isEqualTo("REUSE");
        assertThat(view.get("reuse_fallback_reason").asString()).isEqualTo("CENTER_RESIDUAL_EXCEEDED");
        assertThat(view.at("/world_anchor/reason").asString()).isEqualTo("CENTER_RESIDUAL_EXCEEDED");

        // 같은 대여에서 다시 REUSE_SCENE 으로 돌아가지 않는다
        ObjectNode back = joint.deepCopy().put("mode", "REUSE_SCENE");
        back.remove("reuse_fallback_reason");
        back.set("world_anchor", anchor(true));
        mvc.perform(heartbeat(s, 0.66, back)).andExpect(status().isBadRequest());

        // 이후는 새 모델과 같다: 미리보기 등록·현재화 → 정식 Scene 교체
        String preview = "scene-preview-" + s.jobId() + "-" + s.leaseId();
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ObjectNode ready = joint.deepCopy().put("status", "READY_FOR_ZONES").put("progress_pct", 85.0)
                .put("preview_scene_version", preview);
        ((ObjectNode) ready.get("artifacts")).set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, ready)).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(baseScene);
        // 새 모델로 이어 간 세션(World 다시 맞추기 거부)의 완료는 이 세션 미리보기를 켠다
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(preview);

        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode result = ready.deepCopy().put("status", "COMPLETED").put("progress_pct", 100.0).put("scene_version", baked);
        ((ObjectNode) result.get("artifacts")).set("scene", put(prefix + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(baked);
        JsonNode done = body(mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt())).andReturn());
        assertThat(done.get("status").asString()).isEqualTo("COMPLETED");
        assertThat(done.get("mode").asString()).isEqualTo("NEW");
        assertThat(done.get("scene_version").asString()).isEqualTo(baked);
        assertThat(done.at("/supplement/rebake_blocked_reason").asString()).isNotEqualTo("REUSED_SCENE");
    }

    @Test
    void 새_모델_세션은_재사용_스냅샷을_거부하고_실패한_재사용_세션의_재시도는_재사용을_유지한다() throws Exception {
        commitBaseScene();
        JsonNode created = body(mvc.perform(create(null)).andExpect(status().isAccepted()).andReturn());
        Session s = lease(created.get("setup_session_id").asString(), created.get("job_id").asString());
        mvc.perform(heartbeat(s, 0.40, snapshot(s.sessionId(), "INITIAL_POSE_AND_WORLD", 40.0).put("mode", "REUSE_SCENE")))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));
        mvc.perform(callback(s, failed(s))).andExpect(status().isOk());
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE work_id = ?", workUnitId);
        jdbc.update("DELETE FROM setup_session");

        JsonNode reuse = body(mvc.perform(create("REUSE")).andExpect(status().isAccepted()).andReturn());
        Session r = lease(reuse.get("setup_session_id").asString(), reuse.get("job_id").asString());
        mvc.perform(callback(r, failed(r))).andExpect(status().isOk());
        JsonNode retried = body(mvc.perform(post("/api/setup-sessions/{id}/retry", r.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isAccepted()).andReturn());
        assertThat(retried.get("requested_mode").asString()).isEqualTo("REUSE");
        assertThat(jobInput(retried.get("job_id").asString()).get("mode").asString()).isEqualTo("REUSE_SCENE");
    }

    @Test
    void 재사용이_거부되면_새_모델로_넘어가지_않고_실패로_멈추고_다음_선택에서_재사용을_막으며_새_맵_재시도를_받는다() throws Exception {
        // site_setup 1.18.0(S15P31A101-689): 워커는 REUSE_SCENE 그대로 거부 요약(달라진 카메라)을 보내고 REUSE_WORLD_REJECTED 로 실패한다
        commitBaseScene();
        JsonNode created = body(mvc.perform(create("REUSE")).andExpect(status().isAccepted()).andReturn());
        Session s = lease(created.get("setup_session_id").asString(), created.get("job_id").asString());
        mvc.perform(heartbeat(s, 0.40, snapshot(s.sessionId(), "INITIAL_POSE_AND_WORLD", 40.0).put("mode", "REUSE_SCENE")))
                .andExpect(status().isOk());
        ObjectNode rejected = snapshot(s.sessionId(), "JOINT_BA_AND_VERIFY", 62.0).put("mode", "REUSE_SCENE");
        ObjectNode anchor = anchor(false);
        anchor.putArray("changed_cameras").add("cam_B");
        rejected.set("world_anchor", anchor);
        mvc.perform(heartbeat(s, 0.62, rejected)).andExpect(status().isOk());
        ObjectNode failure = failed(s);
        ((ObjectNode) failure.get("error")).put("code", "REUSE_WORLD_REJECTED")
                .put("message", "카메라 cam_B 이(가) 직전 세팅과 달라져 이전 맵을 쓸 수 없습니다. 새 맵으로 다시 하세요");
        mvc.perform(callback(s, failure)).andExpect(status().isOk());

        JsonNode view = body(mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt())).andReturn());
        assertThat(view.get("status").asString()).isEqualTo("FAILED");
        assertThat(view.get("failure_code").asString()).isEqualTo("REUSE_WORLD_REJECTED");
        assertThat(view.get("mode").asString()).isEqualTo("REUSE");
        assertThat(view.get("reuse_fallback_reason").isNull()).isTrue();
        assertThat(view.at("/world_anchor/accepted").asBoolean()).isFalse();
        assertThat(view.at("/world_anchor/changed_cameras/0").asString()).isEqualTo("cam_B");
        assertThat(view.get("preview_scene_version").isNull()).isTrue();
        assertThat(currentScene()).isEqualTo(baseScene);

        // 다음 선택: 직전 시도가 카메라 변화로 거부됐으므로 재사용은 꺼지고 이유(카메라)를 보인다. 시작해도 409
        JsonNode check = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(check.get("available").asBoolean()).isFalse();
        assertThat(check.get("blocked_reason").asString()).isEqualTo("CAMERA_CHANGED");
        assertThat(check.get("camera_source").asString()).isEqualTo("LAST_SETUP_REJECTED");
        assertThat(check.at("/changed_cameras/0").asString()).isEqualTo("cam_B");
        mvc.perform(post("/api/setup-sessions/{id}/retry", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\": \"REUSE\"}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_REUSE_CAMERA_CHANGED"));
        mvc.perform(post("/api/setup-sessions/{id}/retry", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\": \"BOGUS\"}"))
                .andExpect(status().isBadRequest());

        // 관리자가 새 맵을 고른 재시도는 새 방식을 새 세션에 저장한다(작업 입력에 mode 없음 = FULL_SETUP)
        JsonNode retried = body(mvc.perform(post("/api/setup-sessions/{id}/retry", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{\"mode\": \"NEW\"}"))
                .andExpect(status().isAccepted()).andReturn());
        assertThat(retried.get("requested_mode").asString()).isEqualTo("NEW");
        assertThat(retried.get("retry_of").asString()).isEqualTo(s.sessionId());
        assertThat(jobInput(retried.get("job_id").asString()).has("mode")).isFalse();
        // 가장 최근 시도가 거부가 아니므로 카메라 근거는 다시 모름(막지 않음)
        JsonNode after = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(after.get("camera_status").asString()).isEqualTo("UNKNOWN");
        assertThat(after.get("available").asBoolean()).isTrue();
    }

    @Test
    void Tier0_회전_감시가_카메라_변화를_알리면_재사용을_막고_근거가_없으면_막지_않는다() throws Exception {
        JsonNode none = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(none.get("blocked_reason").asString()).isEqualTo("NO_SCENE");
        assertThat(none.get("scene_version").isNull()).isTrue();

        commitBaseScene();
        JsonNode unknown = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(unknown.get("available").asBoolean()).isTrue();
        assertThat(unknown.get("camera_status").asString()).isEqualTo("UNKNOWN");
        assertThat(unknown.get("scene_version").asString()).isEqualTo(baseScene);

        doReturn(List.of("cam_C")).when(health).reSetupCameraIds();
        JsonNode drift = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(drift.get("blocked_reason").asString()).isEqualTo("CAMERA_CHANGED");
        assertThat(drift.get("camera_source").asString()).isEqualTo("TIER0_DRIFT");
        assertThat(drift.at("/changed_cameras/0").asString()).isEqualTo("cam_C");
        mvc.perform(create("REUSE")).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_CAMERA_CHANGED"));
        // 새 맵은 막지 않는다
        mvc.perform(create(null)).andExpect(status().isAccepted());

        doReturn(List.of()).when(health).reSetupCameraIds();
        JsonNode quiet = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(quiet.get("camera_status").asString()).isEqualTo("NOT_DETECTED");
        assertThat(quiet.get("available").asBoolean()).isTrue();
        mvc.perform(get("/api/setup-sessions/reuse-check").with(viewerJwt())).andExpect(status().isForbidden());
    }

    // ------------------------------------------------------------------ 도우미

    private RequestBuilder reuseCheck() {
        return get("/api/setup-sessions/reuse-check").with(adminJwt());
    }

    /** 현재 관제 맵: 이전 활성 보정으로 만든 COMMITTED Scene 을 가장 최근 활성화로 둔다 */
    private void commitBaseScene() {
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound,
                    committed_at, valid_from)
                VALUES (?, ?, ?, 'COMMITTED', ?, false, now(), now())
                """, baseScene, site, baseCalibration, "scenes/" + baseScene + ".glb");
        jdbc.update("""
                INSERT INTO scene_activation (site_id, scene_version, previous_scene_version, activated_at, actor, reason)
                VALUES (?, ?, NULL, now(), 'test', '재사용 시험 현재 맵')
                """, site, baseScene);
        assertThat(currentScene()).isEqualTo(baseScene);
    }

    /**
     * Tier 0 가 이 보정을 world_frame_id 계보로 채택했다고 알린 calibration_update. 자세는 카메라 4대가 같은 방향·x 로 늘어선
     * 정규 직교 회전이다(예시 문서 R 은 소수 넷째 자리 반올림이라 직교 검사를 넘지 못한다). 두 보정이 같은 자세 = 방이 그대로
     */
    private void tier0Adopted(String version, String worldFrameId) {
        ObjectNode payload = mapper.createObjectNode().put("calibration_version", version).put("calibration_state", "CALIBRATED")
                .put("world_frame_id", worldFrameId);
        ArrayNode poses = payload.putArray("camera_poses");
        String[] cams = {"cam_A", "cam_B", "cam_C", "cam_D"};
        for (int i = 1; i <= 4; i++) {
            ObjectNode pose = poses.addObject().put("camera_id", cams[i - 1]);
            pose.set("R", mapper.valueToTree(new double[][] {{1, 0, 0}, {0, 1, 0}, {0, 0, 1}}));
            pose.set("t", mapper.valueToTree(new double[] {-i, 0, -2.44}));
        }
        jdbc.update("""
                INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                    sent_at, received_at, payload)
                VALUES (?, ?, ?, 'CALIBRATED', 'RECALIBRATED', now() + interval '2 hours', now() + interval '2 hours', ?::jsonb)
                """, UUID.randomUUID(), "tier0-reuse-" + suffix, version, payload.toString());
    }

    private ObjectNode anchor(boolean accepted) {
        return mapper.createObjectNode().put("accepted", accepted)
                .put("reason", accepted ? "ANCHORED" : "CENTER_RESIDUAL_EXCEEDED")
                .put("reference_calibration_version", baseCalibration)
                .put("max_center_residual_m", accepted ? 0.0437 : 0.41).put("max_orientation_change_rad", 0.0095);
    }

    private RequestBuilder complete(String sessionId) {
        return post("/api/setup-sessions/{id}/complete", sessionId).with(adminJwt());
    }

    private String currentScene() {
        return jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s WHERE site_id = ?",
                String.class, site).stream().findFirst().orElse(null);
    }

    private JsonNode jobInput(String jobId) throws Exception {
        return mapper.readTree(jdbc.queryForObject("SELECT input::text FROM analysis_job WHERE job_id = ?", String.class, jobId));
    }

    private record Session(String sessionId, String jobId, String leaseId) {
    }

    private RequestBuilder create(String mode) {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam_A", 2.44).put("cam_B", 2.44).put("cam_C", 2.48).put("cam_D", 2.48);
        if (mode != null) {
            req.put("mode", mode);
        }
        return post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(req.toString());
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

    private ObjectNode put(String key, byte[] content) {
        objects.put(key, content);
        return mapper.createObjectNode().put("uri", BUCKET + key).put("sha256", sha256Hex(content))
                .put("size_bytes", content.length);
    }

    /** 계약 예시 calibration.world_anchor 를 이 현장·세션·버전으로 바꾼 원문. accepted 면 이전 활성 보정의 World 로 옮긴 문서다 */
    private byte[] calibrationDocument(String version, String sessionId, boolean accepted) throws Exception {
        ObjectNode doc = (ObjectNode) mapper.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.world_anchor.json")));
        doc.put("calibration_version", version);
        doc.put("setup_session_id", sessionId);
        doc.put("previous_calibration_version", baseCalibration);
        ((ObjectNode) doc.get("coordinate_frame")).put("site_id", site);
        ObjectNode worldAnchor = (ObjectNode) doc.get("world_anchor");
        worldAnchor.put("reference_calibration_version", baseCalibration);
        if (!accepted) {
            worldAnchor.put("accepted", false).put("reason", "CENTER_RESIDUAL_EXCEEDED");
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
        String[] cams = {"cam_A", "cam_B", "cam_C", "cam_D"};
        double[] heights = {2.44, 2.44, 2.48, 2.48};
        for (int i = 0; i < 4; i++) {
            checks.putObject(cams[i]).put("input_height_m", heights[i]).put("restored_height_m", heights[i])
                    .put("difference_cm", 0.0).put("within_tolerance", true);
        }
        // 카메라 확인 통과(S15P31A101-646): 구역 단계·세팅 완료 관문을 지나야 하는 흐름 시험이다
        node.set("camera_check", SetupCameraCheckFixtures.passed(mapper, newCalibration));
        return node;
    }

    private RequestBuilder heartbeat(Session s, double progress, ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId()).put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.10.0", p);
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

    private ObjectNode failed(Session s) {
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "FAILED").put("started_at", now - 60_000).put("finished_at", now);
        p.putObject("error").put("code", "FLOOR_FIT_FAILED").put("message", "바닥 맞춤 실패").put("retryable", false);
        return p;
    }

    private RequestBuilder callback(Session s, ObjectNode payload) {
        ObjectNode env = envelope("job_callback", "2.18.0", payload);
        assertThat(schemas.violations("messages/job_callback", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + s.jobId() + "/result")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(env.toString());
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
