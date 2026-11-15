package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.nullable;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.report.TbmFormAiService;
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
 * 녹화 재생 시험 스위치를 켠 EC2(S15P31A101-736, docs/design/S15P31A101-736-replay-test-switch.md). 노트북 카메라 입력이 모두 녹화 파일(FILE)이면
 * 세팅 대여 하트비트 응답이 카메라 확인 건너뛰기(job_lease_status 2.4.0 camera_check_skip)를 싣고, 워커가 남긴 SKIPPED_REPLAY 를 완료 관문이
 * 통과로 본다. 실제 카메라(RTSP)면 스위치가 켜져 있어도 싣지 않고 SKIPPED_REPLAY 로는 완료할 수 없다. viewer_health 는 알림 스위치를 싣는다.
 * 스위치를 끈 기본 동작은 {@link SetupCompleteIntegrationTest}·{@code ReplayTestSwitchTest} 가 본다.
 */
@SpringBootTest(properties = {"simai.replay-test.alerts=true", "simai.replay-test.skip-camera-check=true"})
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupReplayTestSwitchIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String BUCKET = "s3://simai-test-bucket/";
    /** 이 시험 문맥의 노트북 하나. HealthState 는 노트북마다 마지막 하트비트를 60초 들고 있어 시험마다 같은 ID 로 덮어쓴다 */
    private static final String LAPTOP = "laptop-replay-test-736";
    private static final List<String> CAMERAS = List.of("cam_A", "cam_B", "cam_C", "cam_D");

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper mapper;
    @Autowired MachineTokenService machines;
    @Autowired ContractSchemas schemas;
    @Autowired ViewerHealthService viewerHealth;

    @MockitoBean ObjectStorage storage;
    @MockitoBean TbmFormAiService tbmFormAi;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String worker = "worker-replay-test-" + suffix;
    private final String baseCalibration = "cal-replay-base-" + suffix;
    private final String newCalibration = "cal-replay-" + suffix + "-jba2-0001";
    private final Map<String, byte[]> objects = new ConcurrentHashMap<>();

    private String site;
    private long userId;
    private long workUnitId;
    private String token;
    private String laptopToken;

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
        when(storage.signPut(anyString(), anyString(), anyLong(), nullable(String.class))).thenAnswer(i ->
                new ObjectStorage.SignedRequest("https://storage.test/put/" + i.getArgument(0), Map.of(), Instant.now().plusSeconds(300)));

        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        userId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '재생시험관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "replay-test-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "재생시험작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "재생시험작업-" + suffix);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'bootstrap', now(), 'file:///cal/base.json', ?, now())
                """, baseCalibration, site, "sha256:" + "a".repeat(64));
        token = machines.registerWorker(worker, List.of("site_setup")).token();
        laptopToken = machines.registerDevice(LAPTOP, "LAPTOP").token();
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
        jdbc.update("DELETE FROM setup_session_zone_plan");
        jdbc.update("DELETE FROM setup_session_zone");
        jdbc.update("DELETE FROM setup_supplement_media");
        jdbc.update("DELETE FROM setup_session");
        jdbc.update("DELETE FROM static_object WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id = ?)",
                "replay-test-admin-" + suffix);
        jdbc.update("DELETE FROM zone WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id = ?)",
                "replay-test-admin-" + suffix);
        for (String cal : List.of(baseCalibration, newCalibration)) {
            for (String v : jdbc.queryForList("SELECT config_version FROM config_bundle WHERE calibration_version = ?",
                    String.class, cal)) {
                jdbc.update("UPDATE config_bundle SET previous_config_version = NULL WHERE previous_config_version = ?", v);
                jdbc.update("DELETE FROM config_ack WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_file WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle_item WHERE config_version = ?", v);
                jdbc.update("DELETE FROM config_bundle WHERE config_version = ?", v);
            }
            jdbc.update("DELETE FROM scene_activation WHERE scene_version IN (SELECT scene_version FROM scene_version "
                    + "WHERE calibration_version = ?)", cal);
            jdbc.update("DELETE FROM scene_version WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_update_message WHERE calibration_version = ?", cal);
            jdbc.update("DELETE FROM calibration_document WHERE calibration_version = ?", cal);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", newCalibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", baseCalibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM work_unit WHERE name LIKE ?", "재생시험%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "replay-test-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
        jdbc.update("DELETE FROM device_heartbeat WHERE device_id = ?", LAPTOP);
        jdbc.update("DELETE FROM device WHERE device_id = ?", LAPTOP);
    }

    @Test
    void 스위치가_켜져_있고_입력이_녹화_파일이면_걷기_확인을_건너뛰라고_알리고_SKIPPED_REPLAY_로_완료한다() throws Exception {
        laptopHeartbeat("FILE");
        Session s = startSession();

        JsonNode lease = body(mvc.perform(heartbeat(s, 0.65, skippedSnapshot(s, "READY_FOR_ZONES", 85.0)))
                .andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_lease_status", lease.toString())).isEmpty();
        assertThat(lease.get("schema_version").asString()).isEqualTo("2.4.0");
        assertThat(lease.at("/payload/camera_check_skip/reason").asString()).isEqualTo("REPLAY_TEST");

        JsonNode view = sessionView(s);
        assertThat(view.path("camera_check").path("status").asString()).isEqualTo("SKIPPED_REPLAY");
        assertThat(view.path("camera_check_phase").asString()).isEqualTo("SKIPPED_REPLAY");
        assertThat(view.path("camera_check_passed").asBoolean()).as("관문 통과").isTrue();
        assertThat(view.path("paused_reason").isNull()).as("걷기 창을 띄우지 않는다").isTrue();
        // 카메라 확인 관문을 지나 정밀 맵만 남는다
        assertThat(view.path("complete_blocked_reason").asString()).isEqualTo("SETUP_FINAL_MAP_NOT_READY");

        baked(s);
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT setup_completed_at IS NOT NULL FROM work_unit WHERE work_id = ?", Boolean.class,
                workUnitId)).isTrue();
    }

    @Test
    void 스위치가_켜져_있어도_실제_카메라_입력이면_걷기_확인_그대로다() throws Exception {
        laptopHeartbeat("RTSP");
        Session s = startSession();

        JsonNode lease = body(mvc.perform(heartbeat(s, 0.65, skippedSnapshot(s, "READY_FOR_ZONES", 85.0)))
                .andExpect(status().isOk()).andReturn());
        assertThat(lease.at("/payload/camera_check_skip").isMissingNode()).as("RTSP 면 건너뛰기를 싣지 않는다").isTrue();

        JsonNode view = sessionView(s);
        assertThat(view.path("camera_check_passed").asBoolean()).isFalse();
        assertThat(view.path("complete_blocked_reason").asString()).isEqualTo("SETUP_CAMERA_CHECK_NOT_PASSED");
        baked(s);
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_CAMERA_CHECK_NOT_PASSED"));
    }

    @Test
    void 녹화_파일과_실제_카메라가_섞이면_건너뛰지_않는다() throws Exception {
        laptopHeartbeat("FILE", "FILE", "FILE", "RTSP");
        Session s = startSession();
        JsonNode lease = body(mvc.perform(heartbeat(s, 0.65, skippedSnapshot(s, "READY_FOR_ZONES", 85.0)))
                .andExpect(status().isOk()).andReturn());
        assertThat(lease.at("/payload/camera_check_skip").isMissingNode()).isTrue();
        assertThat(sessionView(s).path("complete_blocked_reason").asString()).isEqualTo("SETUP_CAMERA_CHECK_NOT_PASSED");
    }

    @Test
    void 관제_화면_상태에_재생_시험_알림_스위치를_싣는다() throws Exception {
        laptopHeartbeat("FILE");
        ObjectNode message = viewerHealth.message();
        assertThat(schemas.violations("messages/viewer_health", message.toString())).isEmpty();
        assertThat(message.at("/payload/laptop/test_input").booleanValue()).isTrue();
        assertThat(message.at("/payload/laptop/replay_test_alerts").booleanValue()).isTrue();
    }

    // ---- 도움 ------------------------------------------------------------------------------------------------

    private record Session(String sessionId, String jobId, String leaseId) {
    }

    private void laptopHeartbeat(String... sourceKinds) throws Exception {
        String[] kinds = sourceKinds.length == 1 ? new String[] {sourceKinds[0], sourceKinds[0], sourceKinds[0], sourceKinds[0]}
                : sourceKinds;
        ObjectNode p = mapper.createObjectNode().put("heartbeat_seq", 0);
        ObjectNode tier0 = p.putObject("tier0").put("control", "CONNECTED");
        tier0.putNull("last_message_at");
        ArrayNode video = tier0.putArray("video_cameras");
        p.put("site_display", "NORMAL");
        ArrayNode cameras = p.putArray("cameras");
        for (int i = 0; i < kinds.length; i++) {
            video.add(CAMERAS.get(i));
            cameras.addObject().put("camera_id", CAMERAS.get(i)).put("rtsp", "CONNECTED").put("source_kind", kinds[i]);
        }
        ObjectNode env = mapper.createObjectNode().put("schema_version", "1.3.0").put("msg_type", "laptop_heartbeat")
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", Instant.now().toEpochMilli()).put("source_id", LAPTOP);
        env.set("payload", p);
        assertThat(schemas.violations("messages/laptop_heartbeat", env.toString())).isEmpty();
        mvc.perform(post("/api/devices/heartbeat").header(HttpHeaders.AUTHORIZATION, "Bearer " + laptopToken)
                .contentType(MediaType.APPLICATION_JSON).content(env.toString())).andExpect(status().isNoContent());
    }

    private Session startSession() throws Exception {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam_A", 2.42).put("cam_B", 2.48).put("cam_C", 2.45).put("cam_D", 2.51);
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON)
                .content(req.toString())).andExpect(status().isAccepted()).andReturn());
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add("site_setup");
        p.put("wait_ms", 0);
        JsonNode leased = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", p).toString()))
                .andExpect(status().isOk()).andReturn());
        return new Session(created.get("setup_session_id").asString(), created.get("job_id").asString(),
                leased.at("/payload/lease_id").asString());
    }

    /** 워커가 걷기 확인을 건너뛴 스냅샷(camera_check SKIPPED_REPLAY)과 새 보정 문서·미리보기 등록 */
    private ObjectNode skippedSnapshot(Session s, String stage, double pct) throws Exception {
        ObjectNode snap = mapper.createObjectNode().put("setup_session_id", s.sessionId()).put("status", stage)
                .put("progress_pct", pct).put("calibration_version", newCalibration).put("preview_scene_version", preview(s));
        ObjectNode checks = snap.putObject("height_checks");
        double[] heights = {2.42, 2.48, 2.45, 2.51}; // 세션을 만들 때 넣은 높이(startSession)와 같아야 한다
        for (int i = 0; i < CAMERAS.size(); i++) {
            checks.putObject(CAMERAS.get(i)).put("input_height_m", heights[i]).put("restored_height_m", heights[i])
                    .put("difference_cm", 0.0).put("within_tolerance", true);
        }
        snap.set("camera_check", SetupCameraCheckFixtures.check(mapper, newCalibration, "SKIPPED_REPLAY"));
        ObjectNode artifacts = snap.putObject("artifacts");
        artifacts.set("calibration_document", put(prefix(s) + "calibration-" + newCalibration + ".json",
                calibrationDocument(s.sessionId())));
        artifacts.set("preview_scene", put(prefix(s) + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8)));
        return snap;
    }

    private static String preview(Session s) {
        return "scene-preview-" + s.jobId() + "-" + s.leaseId();
    }

    /** 정밀 맵 굽기 끝(COMPLETED 결과, 카메라 확인 SKIPPED_REPLAY 그대로) */
    private void baked(Session s) throws Exception {
        ObjectNode result = skippedSnapshot(s, "COMPLETED", 100.0).put("scene_version", "scene-" + s.jobId() + "-" + s.leaseId());
        ((ObjectNode) result.get("artifacts")).set("scene", put(prefix(s) + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        long now = Instant.now().toEpochMilli();
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "SUCCEEDED").put("started_at", now - 60_000).put("finished_at", now);
        p.set("result", result);
        ObjectNode env = envelope("job_callback", "2.26.0", p);
        assertThat(schemas.violations("messages/job_callback", env.toString())).isEmpty();
        mvc.perform(post("/api/worker/jobs/" + s.jobId() + "/result").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content(env.toString())).andExpect(status().isOk());
    }

    private RequestBuilder heartbeat(Session s, double progress, ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId()).put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.19.0", p);
        assertThat(schemas.violations("messages/job_heartbeat", env.toString())).isEmpty();
        return post("/api/worker/jobs/" + s.jobId() + "/heartbeat").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content(env.toString());
    }

    private RequestBuilder complete(String sessionId) {
        return post("/api/setup-sessions/{id}/complete", sessionId).with(adminJwt());
    }

    private JsonNode sessionView(Session s) throws Exception {
        return body(mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(adminJwt())).andExpect(status().isOk())
                .andReturn());
    }

    private String prefix(Session s) {
        return "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
    }

    private ObjectNode put(String key, byte[] content) {
        objects.put(key, content);
        return mapper.createObjectNode().put("uri", BUCKET + key).put("sha256", sha256Hex(content)).put("size_bytes", content.length);
    }

    private byte[] calibrationDocument(String sessionId) throws Exception {
        ObjectNode doc = (ObjectNode) mapper.readTree(Files.readString(CONTRACTS.resolve("examples/calibration.json")));
        doc.put("schema_version", "2.2.0");
        doc.put("calibration_version", newCalibration);
        doc.put("setup_session_id", sessionId);
        doc.put("previous_calibration_version", baseCalibration);
        ((ObjectNode) doc.get("coordinate_frame")).put("site_id", site);
        ArrayNode cameras = (ArrayNode) doc.get("cameras");
        JsonNode template = cameras.get(0);
        cameras.removeAll();
        for (String cam : CAMERAS) {
            cameras.add(((ObjectNode) template.deepCopy()).put("camera_id", cam));
        }
        doc.remove("content_hash");
        return mapper.writeValueAsBytes(doc);
    }

    private ObjectNode envelope(String msgType, String version, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode().put("schema_version", version).put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", Instant.now().toEpochMilli()).put("source_id", worker);
        env.set("payload", payload);
        return env;
    }

    private RequestPostProcessor adminJwt() {
        return jwt().jwt(j -> j.subject(String.valueOf(userId))).authorities(new SimpleGrantedAuthority("ROLE_ADMIN"));
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
