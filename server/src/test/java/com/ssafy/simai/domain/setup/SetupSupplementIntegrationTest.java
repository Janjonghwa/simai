package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.isNull;
import static org.mockito.ArgumentMatchers.nullable;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.delete;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.scene.SceneCameraImageService;
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
 * 세팅 맵 보강 촬영·다시 굽기(S15P31A101-607): 보강 촬영 업로드 서명·확인·상한, 상태 가드(미리보기 전·정식 굽기 중·관제 중·진행 중
 * 다시 굽기), SUPPLEMENT_REBAKE job_request 조립(계약 검사), 하트비트, APPLIED(새 Scene 등록·현재화)·FALLBACK(원래 맵 유지)·취소.
 * 객체 저장소는 가짜(키 → 바이트)다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupSupplementIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String BUCKET = "s3://simai-test-bucket/";
    private static final String BUNDLE = "bundle-" + "0123456789abcdef0123456789abcdef";
    private static final List<String> CAMERAS = List.of("cam_A", "cam_B", "cam_C", "cam_D");

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
    SceneCameraImageService sceneCameraImages;

    @MockitoBean
    ObjectStorage storage;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String worker = "worker-setup-sup-" + suffix;
    private final String baseCalibration = "cal-sup-base-" + suffix;
    private final String newCalibration = "cal-sup-" + suffix + "-jba2-0001";
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
        when(storage.signPut(anyString(), anyString(), anyLong(), nullable(String.class))).thenAnswer(i ->
                new ObjectStorage.SignedRequest("https://storage.test/put/" + i.getArgument(0),
                        Map.of("content-type", (String) i.getArgument(1)), Instant.now().plusSeconds(300)));
        when(storage.signGet(anyString())).thenAnswer(i -> new ObjectStorage.SignedRequest(
                "https://storage.test/" + i.getArgument(0), Map.of(), Instant.now().plusSeconds(300)));

        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        userId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '보강촬영관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "setup-sup-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "보강촬영작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "보강촬영작업-" + suffix);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'bootstrap', now(), 'file:///cal/base.json', ?, now())
                """, baseCalibration, site, "sha256:" + "a".repeat(64));
        token = machines.registerWorker(worker, List.of("site_setup")).token();
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
        for (String cal : List.of(baseCalibration, newCalibration)) {
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
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", newCalibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", baseCalibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM work_unit WHERE name LIKE ?", "보강촬영작업-" + suffix + "%");
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "setup-sup-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void 보강_촬영_업로드_서명을_내고_올린_뒤_확인하면_세션_조회에_실리고_상한과_형식을_지킨다() throws Exception {
        Done s = completedSession();

        // 해시 없는 큰 동영상: 키는 서버가 정하고 해시는 서명에 묶지 않는다
        JsonNode issued = issue(s.sessionId(), mapper.createObjectNode().put("content_type", "video/mp4").put("size_bytes", 12)
                .put("source", "PHONE_UPLOAD").put("file_name", "C:\\DCIM\\VID_0001.mp4"));
        String mediaId = issued.get("media_id").asString();
        assertThat(mediaId).matches("^sup-[0-9a-f]{16}$");
        assertThat(issued.get("kind").asString()).isEqualTo("VIDEO");
        assertThat(issued.get("status").asString()).isEqualTo("PENDING_UPLOAD");
        assertThat(issued.get("file_name").asString()).isEqualTo("VID_0001.mp4");
        assertThat(issued.at("/upload/method").asString()).isEqualTo("PUT");
        String key = "setup/" + s.sessionId() + "/supplement/" + mediaId + ".mp4";
        assertThat(issued.at("/upload/url").asString()).isEqualTo("https://storage.test/put/" + key);
        verify(storage).signPut(eq(key), eq("video/mp4"), eq(12L), isNull());

        // 아직 저장소에 없으면 확인을 거부한다
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media/{m}/complete", s.sessionId(), mediaId).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_UPLOAD_INCOMPLETE"));
        objects.put(key, "0123456789ab".getBytes(StandardCharsets.UTF_8));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media/{m}/complete", s.sessionId(), mediaId).with(adminJwt()))
                .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("UPLOADED"));

        // 해시를 알린 사진은 해시를 서명에 묶고, 저장소 체크섬이 다르면 확인을 거부한다
        JsonNode photo = issue(s.sessionId(), mapper.createObjectNode().put("content_type", "IMAGE/JPEG").put("size_bytes", 5)
                .put("sha256", sha256Hex("right".getBytes(StandardCharsets.UTF_8))));
        String photoKey = "setup/" + s.sessionId() + "/supplement/" + photo.get("media_id").asString() + ".jpg";
        objects.put(photoKey, "wrong".getBytes(StandardCharsets.UTF_8));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media/{m}/complete", s.sessionId(),
                        photo.get("media_id").asString()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_UPLOAD_INCOMPLETE"));

        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.supplement.media.length()").value(2))
                .andExpect(jsonPath("$.supplement.media[0].media_id").value(mediaId))
                .andExpect(jsonPath("$.supplement.media[0].status").value("UPLOADED"))
                .andExpect(jsonPath("$.supplement.media[0].source").value("PHONE_UPLOAD"))
                .andExpect(jsonPath("$.supplement.media[1].status").value("PENDING_UPLOAD"))
                .andExpect(jsonPath("$.supplement.limits.max_items").value(32))
                .andExpect(jsonPath("$.supplement.limits.max_video_bytes").value(1L << 30))
                .andExpect(jsonPath("$.supplement.can_upload").value(true))
                .andExpect(jsonPath("$.supplement.can_rebake").value(true))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").isEmpty())
                .andExpect(jsonPath("$.supplement.rebake").isEmpty());

        // 형식·크기: 받지 않는 형식, 동영상 1 GiB 초과, 사진 50 MiB 초과, 해시 형식
        for (ObjectNode bad : List.of(
                mapper.createObjectNode().put("content_type", "image/heic").put("size_bytes", 10),
                mapper.createObjectNode().put("content_type", "video/mp4").put("size_bytes", (1L << 30) + 1),
                mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", (50L << 20) + 1),
                mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", 10).put("sha256", "xyz"),
                mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", 10).put("source", "CAMERA"))) {
            mvc.perform(post("/api/setup-sessions/{id}/supplement-media", s.sessionId()).with(adminJwt())
                            .contentType(MediaType.APPLICATION_JSON).content(bad.toString()))
                    .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_SUPPLEMENT_MEDIA"));
        }
        // 세션 전체 4 GiB 상한
        for (int i = 0; i < 3; i++) {
            issue(s.sessionId(), mapper.createObjectNode().put("content_type", "video/quicktime").put("size_bytes", 1L << 30));
        }
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.createObjectNode().put("content_type", "video/quicktime").put("size_bytes", 1L << 30)
                                .toString()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_LIMIT_EXCEEDED"));
        // 열람자는 올리지 못한다
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media", s.sessionId()).with(viewerJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", 1).toString()))
                .andExpect(status().isForbidden());

        // 목록에서 빼면 조회·상한에서 빠진다(행은 removed_at 만 남긴다)
        mvc.perform(delete("/api/setup-sessions/{id}/supplement-media/{m}", s.sessionId(), photo.get("media_id").asString())
                .with(adminJwt())).andExpect(status().isNoContent());
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.supplement.media.length()").value(4));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_supplement_media WHERE setup_session_id = ?::uuid",
                Integer.class, s.sessionId())).isEqualTo(5);
        // 등록 카메라에는 들어가지 않는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM camera WHERE camera_id LIKE 'sup-%'", Integer.class)).isZero();
    }

    @Test
    void 미리보기_전에는_올릴_수_없고_정식_맵_굽기_중에는_다시_굽기를_막는다() throws Exception {
        Session s = startSession();
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", 3).toString()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_SESSION_NOT_READY"));
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.supplement.can_upload").value(false))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("SESSION_NOT_READY"));

        ready(s);
        // 미리보기가 나오면 정식 굽기 도중에도 올릴 수 있다
        uploadedMedia(s.sessionId(), "image/png", "png-bytes".getBytes(StandardCharsets.UTF_8), null);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_SETUP_JOB_RUNNING"));
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.supplement.can_upload").value(true))
                .andExpect(jsonPath("$.supplement.can_rebake").value(false))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("SETUP_JOB_RUNNING"));
    }

    @Test
    void 관제_중이거나_올린_파일이_없으면_다시_굽기를_막는다() throws Exception {
        Done s = completedSession();
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_NO_MEDIA"));
        uploadedMedia(s.sessionId(), "video/mp4", "mp4-bytes".getBytes(StandardCharsets.UTF_8), null);
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_BLOCKED_BY_LIVE_WORK"));
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("LIVE_WORK"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_supplement_rebake", Integer.class)).isZero();
    }

    @Test
    void 다시_굽기는_계약에_맞는_입력을_만들고_APPLIED_결과는_새_Scene_을_세션_귀속으로_등록해_현재_맵으로_올린다() throws Exception {
        Done s = completedSession();
        String video = uploadedMedia(s.sessionId(), "video/mp4", "phone-video".getBytes(StandardCharsets.UTF_8), "PHONE_UPLOAD");
        String photo = uploadedMedia(s.sessionId(), "image/jpeg", jpeg("dslr"), "FILE_UPLOAD");

        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.supplement.rebake.status").value("QUEUED"))
                .andExpect(jsonPath("$.supplement.rebake.base_scene_version").value(s.scene()))
                .andExpect(jsonPath("$.supplement.rebake.media_ids[0]").value(video))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("REBAKE_RUNNING"));
        // 세션당 하나, 진행 중이면 같은 현장의 새 세팅도 시작하지 않는다
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_REBAKE_IN_PROGRESS"));
        ObjectNode create = mapper.createObjectNode().put("work_unit_id", workUnitId);
        create.putObject("camera_heights_m").put("cam_A", 2.42).put("cam_B", 2.48).put("cam_C", 2.45).put("cam_D", 2.51);
        mvc.perform(post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(create.toString()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ALREADY_IN_PROGRESS"))
                .andExpect(jsonPath("$.setup_session_id").value(s.sessionId()));
        // 진행 중 다시 굽기가 쓰는 파일은 뺄 수 없다
        mvc.perform(delete("/api/setup-sessions/{id}/supplement-media/{m}", s.sessionId(), photo).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_MEDIA_IN_USE"));

        // 워커가 받는 job_request: 처음 세팅 입력 + mode·supplement_media·rebake_base, 계약 통과
        JsonNode leased = leaseRaw();
        assertThat(schemas.violations("messages/job_request", leased.toString())).isEmpty();
        JsonNode input = leased.at("/payload/input");
        assertThat(input.get("mode").asString()).isEqualTo("SUPPLEMENT_REBAKE");
        assertThat(input.get("setup_session_id").asString()).isEqualTo(s.sessionId());
        assertThat(input.at("/calibration_target/camera_ids")).hasSize(4);
        assertThat(input.get("supplement_media")).hasSize(2);
        assertThat(input.at("/supplement_media/0/media_id").asString()).isEqualTo(video);
        assertThat(input.at("/supplement_media/0/kind").asString()).isEqualTo("VIDEO");
        assertThat(input.at("/supplement_media/0/source").asString()).isEqualTo("PHONE_UPLOAD");
        assertThat(input.at("/supplement_media/0/uri").asString())
                .isEqualTo(BUCKET + "setup/" + s.sessionId() + "/supplement/" + video + ".mp4");
        assertThat(input.at("/supplement_media/0/camera_id").isMissingNode()).isTrue();
        assertThat(input.at("/supplement_media/1/kind").asString()).isEqualTo("IMAGE");
        assertThat(input.at("/supplement_media/1/device/make").asString()).isEqualTo("Canon");
        assertThat(input.at("/supplement_media/1/sha256").asString()).isEqualTo(sha256Hex(jpeg("dslr")));
        JsonNode base = input.get("rebake_base");
        assertThat(base.get("calibration_version").asString()).isEqualTo(newCalibration);
        assertThat(base.at("/calibration_document/uri").asString()).isEqualTo(s.calibrationUri());
        assertThat(base.get("preview_scene_version").asString()).isEqualTo(s.preview());
        assertThat(base.get("scene_version").asString()).isEqualTo(s.scene());
        assertThat(base.at("/bake_images/bundle_id").asString()).isEqualTo(BUNDLE);
        assertThat(base.at("/bake_images/images")).hasSize(4);
        assertThat(base.at("/bake_images/images/0/uri").asString()).startsWith(BUCKET + s.prefix());
        Session rebake = new Session(s.sessionId(), leased.at("/payload/job_id").asString(),
                leased.at("/payload/lease_id").asString());

        // 하트비트는 BAKING_TEXTURE + mode. mode 가 없거나 기준 보정과 다르면 거부
        mvc.perform(heartbeat(rebake, 0.5, rebakeSnapshot(s, "BAKING_TEXTURE", 50.0))).andExpect(status().isOk());
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.supplement.rebake.status").value("RUNNING"))
                .andExpect(jsonPath("$.supplement.rebake.stage").value("BAKING_TEXTURE"))
                .andExpect(jsonPath("$.supplement.rebake.progress_pct").value(50.0))
                // 세션 본체는 그대로(COMPLETED, 진행률 100)
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.progress_pct").value(100.0));
        ObjectNode noMode = rebakeSnapshot(s, "BAKING_TEXTURE", 60.0);
        noMode.remove("mode");
        mvc.perform(heartbeat(rebake, 0.6, noMode)).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_SETUP_PROGRESS"));
        mvc.perform(heartbeat(rebake, 0.4, rebakeSnapshot(s, "BAKING_TEXTURE", 40.0))).andExpect(status().isBadRequest());

        // 다시 굽기 도중 세팅을 완료한다(-615): 완료가 이 세션의 정식 맵을 켠다
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(s.scene());

        // 결과 APPLIED: 다시 굽기 대여 경로의 새 GLB → 세션 귀속 SCENE 후보 → 완료 뒤이고 현재 Scene 이 기준 맵이었으므로 현재화
        String rebakePrefix = "job-objects/site_setup/" + rebake.jobId() + "/" + rebake.leaseId() + "/";
        String newScene = "scene-" + rebake.jobId() + "-" + rebake.leaseId();
        ObjectNode result = rebakeSnapshot(s, "COMPLETED", 100.0).put("scene_version", newScene);
        result.putObject("artifacts").set("scene", put(rebakePrefix + "scene_final_wm2.glb",
                "glb-rebaked".getBytes(StandardCharsets.UTF_8)));
        result.set("supplement", supplementSummary("APPLIED", null, video, "USED", photo, "WIDE_FOV_REJECTED"));
        mvc.perform(callback(rebake, succeeded(rebake, result))).andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        assertThat(currentScene()).isEqualTo(newScene);
        Map<String, Object> sceneRow = jdbc.queryForMap(
                "SELECT calibration_version, gltf_key, quality::text AS quality FROM scene_version WHERE scene_version = ?",
                newScene);
        assertThat(sceneRow.get("calibration_version")).isEqualTo(newCalibration);
        assertThat(sceneRow.get("gltf_key")).isEqualTo(rebakePrefix + "scene_final_wm2.glb");
        assertThat((String) sceneRow.get("quality")).contains("mode=SUPPLEMENT_REBAKE").contains("safety_use=false")
                .contains("rebake_base_scene=" + s.scene());
        assertThat(jdbc.queryForList("""
                SELECT version FROM setup_session_artifact WHERE setup_session_id = ?::uuid AND kind = 'SCENE' ORDER BY registered_at
                """, String.class, s.sessionId())).containsExactly(s.scene(), newScene);
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.scene_version").value(newScene))
                .andExpect(jsonPath("$.calibration_version").value(newCalibration))
                .andExpect(jsonPath("$.supplement.rebake.status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.supplement.rebake.outcome").value("APPLIED"))
                .andExpect(jsonPath("$.supplement.rebake.reason").isEmpty())
                .andExpect(jsonPath("$.supplement.rebake.scene_version").value(newScene))
                .andExpect(jsonPath("$.supplement.rebake.scene_activated").value(true))
                .andExpect(jsonPath("$.supplement.rebake.result.media[1].status").value("WIDE_FOV_REJECTED"))
                // 완료 뒤에는 더 다시 굽지 않는다(-615)
                .andExpect(jsonPath("$.supplement.can_rebake").value(false))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("SETUP_COMPLETED"));
        // 다시 구운 맵도 원래 세팅의 고정 카메라 사진으로 카메라 시점 VDT 를 준다
        ObjectNode images = sceneCameraImages.images(newScene);
        assertThat(images.path("bundle_id").asString()).isEqualTo(BUNDLE);
        assertThat(images.path("images")).hasSize(4);
        assertThat(schemas.violations("messages/scene_camera_images", images.toString())).isEmpty();
    }

    @Test
    void FALLBACK_결과는_새_Scene_을_등록하지_않고_원래_맵을_유지한다() throws Exception {
        Done s = completedSession();
        String currentBefore = currentScene();
        String video = uploadedMedia(s.sessionId(), "video/mp4", "short".getBytes(StandardCharsets.UTF_8), null);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted());
        JsonNode leased = leaseRaw();
        Session rebake = new Session(s.sessionId(), leased.at("/payload/job_id").asString(),
                leased.at("/payload/lease_id").asString());
        long scenesBefore = jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE site_id = ?", Long.class, site);

        // FALLBACK 인데 기준과 다른 scene_version 이면 거부(제출 되돌림)
        ObjectNode wrong = rebakeSnapshot(s, "COMPLETED", 100.0).put("scene_version", "scene-other-" + suffix);
        wrong.set("supplement", supplementSummary("FALLBACK", "TOO_FEW_REGISTERED", video, "NOT_REGISTERED", null, null));
        mvc.perform(callback(rebake, succeeded(rebake, wrong))).andExpect(status().isBadRequest());

        ObjectNode result = rebakeSnapshot(s, "COMPLETED", 100.0).put("scene_version", s.scene());
        result.set("supplement", supplementSummary("FALLBACK", "TOO_FEW_REGISTERED", video, "NOT_REGISTERED", null, null));
        mvc.perform(callback(rebake, succeeded(rebake, result))).andExpect(status().isOk());

        assertThat(currentScene()).isEqualTo(currentBefore);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE site_id = ?", Long.class, site))
                .isEqualTo(scenesBefore);
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, rebake.jobId()))
                .isEqualTo("SUCCEEDED");
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.scene_version").value(s.scene()))
                .andExpect(jsonPath("$.supplement.rebake.status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.supplement.rebake.outcome").value("FALLBACK"))
                .andExpect(jsonPath("$.supplement.rebake.reason").value("TOO_FEW_REGISTERED"))
                .andExpect(jsonPath("$.supplement.rebake.scene_version").isEmpty())
                .andExpect(jsonPath("$.supplement.rebake.scene_activated").isEmpty())
                .andExpect(jsonPath("$.supplement.rebake.result.media[0].status").value("NOT_REGISTERED"));
    }

    @Test
    void 진행_중인_다시_굽기를_취소하면_원래_맵을_두고_다시_요청할_수_있다() throws Exception {
        Done s = completedSession();
        String currentBefore = currentScene();
        uploadedMedia(s.sessionId(), "image/png", "png".getBytes(StandardCharsets.UTF_8), null);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake/cancel", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_REBAKE_NOT_RUNNING"));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted());
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake/cancel", s.sessionId()).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.supplement.rebake.status").value("CANCELLED"))
                .andExpect(jsonPath("$.supplement.can_rebake").value(true));
        assertThat(currentScene()).isEqualTo(currentBefore);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted()).andExpect(jsonPath("$.supplement.rebake.status").value("QUEUED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_supplement_rebake", Integer.class)).isEqualTo(2);
    }

    @Test
    void 굽기를_회수한_세션도_다시_굽기_APPLIED_결과를_받아_세션_맵을_옮긴다() throws Exception {
        // -597: 정식 굽기를 작업 시작으로 회수한 세션(bake_skipped, scene_version 없음)은 미리보기를 기준으로 다시 굽는다.
        // V50 CHECK(bake_skipped 면 scene_version null)에 막혀 결과 제출이 통째로 되돌아가던 것을 V58 이 푼다(-621)
        Done s = reclaimedSession();
        assertThat(jdbc.queryForObject("SELECT scene_version FROM setup_session WHERE setup_session_id = ?::uuid",
                String.class, s.sessionId())).isNull();
        String video = uploadedMedia(s.sessionId(), "video/mp4", "phone-video".getBytes(StandardCharsets.UTF_8), "PHONE_UPLOAD");
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.supplement.rebake.base_scene_version").value(s.preview()));
        JsonNode leased = leaseRaw();
        assertThat(leased.at("/payload/input/rebake_base/scene_version").asString()).isEqualTo(s.preview());
        Session rebake = new Session(s.sessionId(), leased.at("/payload/job_id").asString(),
                leased.at("/payload/lease_id").asString());

        String rebakePrefix = "job-objects/site_setup/" + rebake.jobId() + "/" + rebake.leaseId() + "/";
        String newScene = "scene-" + rebake.jobId() + "-" + rebake.leaseId();
        ObjectNode result = rebakeSnapshot(s, "COMPLETED", 100.0).put("scene_version", newScene);
        result.putObject("artifacts").set("scene", put(rebakePrefix + "scene_final.glb",
                "glb-rebaked".getBytes(StandardCharsets.UTF_8)));
        result.set("supplement", supplementSummary("APPLIED", null, video, "USED", null, null));
        mvc.perform(callback(rebake, succeeded(rebake, result))).andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, rebake.jobId()))
                .isEqualTo("SUCCEEDED");
        // 세팅 완료(C17, -615) 전이라 세션 맵만 옮기고 현재 Scene 은 그대로다. 완료가 이 맵을 켠다
        assertThat(currentScene()).isNotEqualTo(newScene);
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(newScene);
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(jsonPath("$.scene_version").value(newScene))
                // 회수 이력은 그대로 남는다(덮어쓰지 않음)
                .andExpect(jsonPath("$.bake_skipped").value("LIVE_WORK_STARTED"))
                .andExpect(jsonPath("$.supplement.rebake.status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.supplement.rebake.outcome").value("APPLIED"))
                .andExpect(jsonPath("$.supplement.rebake.base_scene_version").value(s.preview()));
    }

    @Test
    void 대기_중_다시_굽기는_같은_현장_작업이_진행_중이면_대여하지_않는다() throws Exception {
        // 요청 때는 관제 중이 아니었는데 대기 중에 작업이 시작됐다: 워커에 내주지 않고 QUEUED 로 둔다(명세 3.2 5항, -621)
        Done s = completedSession();
        uploadedMedia(s.sessionId(), "image/png", "png".getBytes(StandardCharsets.UTF_8), null);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted());
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        leaseExpect(status().isNoContent());
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_type = 'site_setup' AND input ->> 'mode' = ?",
                String.class, "SUPPLEMENT_REBAKE")).isEqualTo("QUEUED");

        // 작업이 끝나면 대여된다
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
        JsonNode leased = leaseRaw();
        assertThat(leased.at("/payload/input/mode").asString()).isEqualTo("SUPPLEMENT_REBAKE");
    }

    @Test
    void 재시도로_대기_중인_세팅은_모드와_상관없이_같은_현장_작업이_진행_중이면_대여하지_않는다() throws Exception {
        // 대여가 끝나(재시도 가능한 실패·만료) 다시 대기 중인 세팅을, 그사이 작업이 시작됐으면 내주지 않는다(명세 3.2 5항, -624).
        // 새 모델은 입력에 mode 가 없다(NULL). -621 은 mode 를 비교해 새 모델은 NULL 로 우연히 막고 REUSE 는 내줬다
        Session s = startSession();
        long now = Instant.now().toEpochMilli();
        ObjectNode failed = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "FAILED").put("started_at", now - 5_000).put("finished_at", now);
        failed.putObject("error").put("code", "STATIC_MAPANYTHING_TIMEOUT").put("message", "시험: 재시도").put("retryable", true);
        mvc.perform(callback(s, failed)).andExpect(status().isOk());
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, s.jobId()))
                .isEqualTo("QUEUED");
        assertThat(jdbc.queryForObject("SELECT input ->> 'mode' FROM analysis_job WHERE job_id = ?", String.class, s.jobId()))
                .isNull();

        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        leaseExpect(status().isNoContent()); // 새 모델(mode 없음)
        jdbc.update("UPDATE analysis_job SET input = jsonb_set(input, '{mode}', to_jsonb('FULL_SETUP'::text)) WHERE job_id = ?",
                s.jobId());
        leaseExpect(status().isNoContent()); // mode 를 FULL_SETUP 으로 적어도
        jdbc.update("UPDATE analysis_job SET input = jsonb_set(input, '{mode}', to_jsonb('REUSE_SCENE'::text)) WHERE job_id = ?",
                s.jobId());
        leaseExpect(status().isNoContent()); // 이전 모델 재사용(REUSE)도
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, s.jobId()))
                .isEqualTo("QUEUED");

        // 작업이 끝나면 같은 작업이 다음 시도로 대여된다
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
        JsonNode leased = leaseRaw();
        assertThat(leased.at("/payload/job_id").asString()).isEqualTo(s.jobId());
        assertThat(leased.at("/payload/attempt").asInt()).isEqualTo(2);
    }

    @Test
    void 세팅을_완료해야_작업을_시작하고_미리보기_맵만_나온_세팅으로는_시작하지_않는다() throws Exception {
        // -642: 작업 시작 기준은 이 작업의 세팅 완료(C17)다. 새 맵·보정·구역은 완료 때 관제에 들어가므로 READY 만으로는 이전 맵으로
        // 관제한다. 완료는 READY_FOR_ZONES 뒤에만 되므로 -624 M5(READY 전 단계가 LIVE 중 GPU 를 씀)도 이 기준이 막는다
        seedCurrentCalibrationDocument();
        Session s = startSession(); // COLLECTING_BACKGROUND
        mvc.perform(post("/api/work-units/{id}/start", workUnitId).with(adminJwt()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_SETUP_NOT_COMPLETED"));
        assertThat(jdbc.queryForObject("SELECT status FROM work_unit WHERE work_id = ?", String.class, workUnitId))
                .isEqualTo("PLANNED");

        Map<String, ObjectNode> r = ready(s); // READY_FOR_ZONES: 미리보기 맵이 나왔지만 아직 완료 전이다
        mvc.perform(post("/api/work-units/{id}/start", workUnitId).with(adminJwt()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_SETUP_NOT_COMPLETED"));
        // 새 모델은 미리보기로 완료하지 않는다(S15P31A101-688). 그래서 미리보기만 나온 세팅으로는 작업도 시작하지 않는다
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_FINAL_MAP_NOT_READY"));
        mvc.perform(post("/api/work-units/{id}/start", workUnitId).with(adminJwt()))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_SETUP_NOT_COMPLETED"));

        // 정밀 맵까지 끝난 뒤 완료하면 시작한다. 남은 굽기가 없으니 작업 시작이 회수할 것도 없다
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview(s)).put("scene_version", baked);
        ObjectNode artifacts = result.putObject("artifacts");
        artifacts.set("calibration_document", r.get("calibration"));
        artifacts.set("preview_scene", r.get("preview"));
        artifacts.set("scene", put(prefix + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(baked);
        mvc.perform(post("/api/work-units/{id}/start", workUnitId).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("IN_PROGRESS"));
        assertThat(jdbc.queryForObject("SELECT bake_skipped FROM setup_session WHERE setup_session_id = ?::uuid",
                String.class, s.sessionId())).isNull();
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
    }

    @Test
    void 미리보기_맵_뒤_재시도_대기_굽기는_작업_시작_때_회수_완료로_닫고_작업_뒤_다시_돌지_않는다() throws Exception {
        // -625 S8: 굽기 중 재시도 가능한 실패(Pod 정지·대여 만료) 뒤 작업을 시작하면, 대기열 굽기를 회수할 주체가 없어 작업 뒤 처음부터
        // 다시 돌고 세션이 READY 전으로 돌아가 다음 작업 시작이 409 였다(감사 재현)
        seedCurrentCalibrationDocument();
        Session s = bakingSession();
        mvc.perform(callback(s, retryableFailure(s))).andExpect(status().isOk());
        // 작업이 없으면 지금처럼 다음 시도를 기다린다(회수하지 않는다)
        assertThat(jobStatus(s)).isEqualTo("QUEUED");
        assertThat(sessionStatus(s)).isEqualTo("BAKING_TEXTURE");

        // 같은 현장에서 세팅을 마친 다른 작업을 시작한다(-688: 이 세션은 정밀 맵 전이라 아직 완료할 수 없다)
        long live = plannedWork("-live");
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", live);
        mvc.perform(post("/api/work-units/{id}/start", live).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("IN_PROGRESS"));
        assertReclaimed(s);
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("COMPLETED"))
                .andExpect(jsonPath("$.bake_skipped").value("LIVE_WORK_STARTED"))
                // 정밀 맵이 없어 이 세션으로는 세팅을 완료할 수 없다(작업이 끝난 뒤 다시 세팅한다)
                .andExpect(jsonPath("$.complete_blocked_reason").value("SETUP_FINAL_MAP_SKIPPED"));

        // 작업이 끝나도 세팅 작업은 다시 대여되지 않고, 다음 작업은 바로 시작된다
        mvc.perform(post("/api/work-units/{id}/end", live).with(adminJwt())).andExpect(status().is2xxSuccessful());
        leaseExpect(status().isNoContent());
        long next = plannedWork("-next");
        // 다음 작업도 세팅을 마친 것으로 둔다(작업 시작은 그 작업의 세팅 완료 뒤다, -642)
        jdbc.update("UPDATE work_unit SET setup_completed_at = now() WHERE work_id = ?", next);
        mvc.perform(post("/api/work-units/{id}/start", next).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("IN_PROGRESS"));
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", next);
    }

    @Test
    void 작업_진행_중_굽기가_재시도_실패로_대기열에_오면_회수_완료로_닫는다() throws Exception {
        // -625: 대여 중 굽기가 회수 신호를 받고도 결과를 내기 전에 재시도 가능한 실패를 내면 LIVE 중 다시 대기열에 온다
        Session s = bakingSession();
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        mvc.perform(callback(s, retryableFailure(s))).andExpect(status().isOk());
        assertReclaimed(s);
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
        leaseExpect(status().isNoContent());
    }

    @Test
    void 작업_진행_중_굽기_대여가_만료돼_대기열에_오면_회수_완료로_닫는다() throws Exception {
        // -625: 하트비트가 끊겨 대여가 만료되면(대여 요청마다 만료 처리) 같은 시도로 다시 대기한다. LIVE 중이면 그 자리에서 회수한다
        Session s = bakingSession();
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        jdbc.update("UPDATE job_lease SET lease_expires_at = now() - interval '1 second' WHERE lease_id = ?::uuid", s.leaseId());
        leaseExpect(status().isNoContent());
        assertReclaimed(s);
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE work_id = ?", workUnitId);
        leaseExpect(status().isNoContent());
    }

    // ---------------------------------------------------------------- 도우미

    /**
     * 미리보기 맵(READY_FOR_ZONES) 뒤 정식 굽기 중(BAKING_TEXTURE)인 세션. 대여는 아직 유효하다. 새 모델은 정밀 맵 전에 완료하지 않으므로
     * (S15P31A101-688) 이 세션으로는 완료하지 않는다. 회수는 같은 현장의 다른 작업(세팅을 마친 작업)이 시작될 때 일어난다
     */
    private Session bakingSession() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        Map<String, ObjectNode> r = ready(s);
        ObjectNode bakingSnap = r.get("snapshot").deepCopy().put("status", "BAKING_TEXTURE").put("progress_pct", 95.0);
        ObjectNode bake = ((ObjectNode) bakingSnap.get("artifacts")).putObject("bake_images").put("bundle_id", BUNDLE);
        ArrayNode bakeImages = bake.putArray("images");
        for (String cam : CAMERAS) {
            ObjectNode image = mapper.createObjectNode().put("camera_id", cam);
            image.setAll(put(prefix + "bake-" + cam + ".jpg", jpeg("bake-" + cam)));
            bakeImages.add(image.put("face_policy", "OBSERVED"));
        }
        mvc.perform(heartbeat(s, 0.95, bakingSnap)).andExpect(status().isOk());
        return s;
    }

    private ObjectNode retryableFailure(Session s) {
        long now = Instant.now().toEpochMilli();
        ObjectNode failed = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId())
                .put("job_type", "site_setup").put("status", "FAILED").put("started_at", now - 5_000).put("finished_at", now);
        failed.putObject("error").put("code", "STATIC_MAPANYTHING_TIMEOUT").put("message", "시험: 재시도").put("retryable", true);
        return failed;
    }

    /** 워커 회수(-597)와 같은 끝 상태: 세션 COMPLETED·bake_skipped·정식 Scene 없음, 세팅 작업 CANCELLED */
    private void assertReclaimed(Session s) {
        assertThat(sessionStatus(s)).isEqualTo("COMPLETED");
        assertThat(jdbc.queryForObject("SELECT bake_skipped FROM setup_session WHERE setup_session_id = ?::uuid",
                String.class, s.sessionId())).isEqualTo("LIVE_WORK_STARTED");
        assertThat(jdbc.queryForObject("SELECT scene_version FROM setup_session WHERE setup_session_id = ?::uuid",
                String.class, s.sessionId())).isNull();
        assertThat(jobStatus(s)).isEqualTo("CANCELLED");
        assertThat(currentScene()).isNotEqualTo(preview(s)); // 미리보기는 관제 맵이 되지 않는다(-688, 완료하지 않은 세션)
    }

    private String jobStatus(Session s) {
        return jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, s.jobId());
    }

    private String sessionStatus(Session s) {
        return jdbc.queryForObject("SELECT status FROM setup_session WHERE setup_session_id = ?::uuid", String.class,
                s.sessionId());
    }

    /** 같은 현장·오늘의 PLANNED 작업 하나 더(정리는 cleanUpTables 가 이름 앞부분으로 한다) */
    private long plannedWork(String nameSuffix) {
        String name = "보강촬영작업-" + suffix + nameSuffix;
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), name, userId);
        return jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, name);
    }

    /** 작업 시작은 현재 보정 문서를 실은 구성 묶음을 만든다. 시험 기준 보정에 문서를 둔다(SetupZoneLayoutIntegrationTest 와 같은 방식) */
    private void seedCurrentCalibrationDocument() throws Exception {
        byte[] doc = java.nio.file.Files.readAllBytes(CONTRACTS.resolve("examples/calibration.json"));
        jdbc.update("INSERT INTO calibration_document(calibration_version,content,sha256,uploaded_by,uploaded_at) VALUES (?,?,?,?,now())",
                baseCalibration, doc, "sha256:" + "b".repeat(64), userId);
    }

    private record Session(String sessionId, String jobId, String leaseId) {
    }

    private record Done(String sessionId, String prefix, String calibrationUri, String preview, String scene) {
    }

    /** 미리보기(READY_FOR_ZONES)까지: 새 보정 문서·미리보기 GLB 등록 */
    private Map<String, ObjectNode> ready(Session s) throws Exception {
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        ObjectNode calRef = put(prefix + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
        ObjectNode previewRef = put(prefix + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview(s));
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef);
        artifacts.set("preview_scene", previewRef);
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());
        return Map.of("calibration", calRef, "preview", previewRef, "snapshot", readySnap);
    }

    private static String preview(Session s) {
        return "scene-preview-" + s.jobId() + "-" + s.leaseId();
    }

    /** 정식 맵까지 끝난 세션: 굽기 입력 사진 4장(BAKING_TEXTURE)과 정식 GLB(결과) */
    private Done completedSession() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        Map<String, ObjectNode> r = ready(s);
        ObjectNode bakingSnap = r.get("snapshot").deepCopy().put("status", "BAKING_TEXTURE").put("progress_pct", 95.0);
        ObjectNode bake = ((ObjectNode) bakingSnap.get("artifacts")).putObject("bake_images").put("bundle_id", BUNDLE);
        ArrayNode bakeImages = bake.putArray("images");
        for (String cam : CAMERAS) {
            ObjectNode image = mapper.createObjectNode().put("camera_id", cam);
            image.setAll(put(prefix + "bake-" + cam + ".jpg", jpeg("bake-" + cam)));
            bakeImages.add(image.put("face_policy", "OBSERVED"));
        }
        mvc.perform(heartbeat(s, 0.95, bakingSnap)).andExpect(status().isOk());

        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview(s)).put("scene_version", baked);
        ObjectNode artifacts = result.putObject("artifacts");
        artifacts.set("calibration_document", r.get("calibration"));
        artifacts.set("preview_scene", r.get("preview"));
        artifacts.set("scene", put(prefix + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        // 세팅 완료(C17, -615) 전이라 정식 맵을 현재 Scene 으로 올리지 않는다
        assertThat(currentScene()).isNotEqualTo(baked);
        return new Done(s.sessionId(), prefix, r.get("calibration").get("uri").asString(), preview(s), baked);
    }

    /** 정식 굽기를 작업 시작으로 회수한 세션(-597): 굽기 입력 사진은 알렸고 결과는 bake_skipped·scene_version null */
    private Done reclaimedSession() throws Exception {
        Session s = startSession();
        String prefix = "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
        Map<String, ObjectNode> r = ready(s);
        ObjectNode bakingSnap = r.get("snapshot").deepCopy().put("status", "BAKING_TEXTURE").put("progress_pct", 95.0);
        ObjectNode bake = ((ObjectNode) bakingSnap.get("artifacts")).putObject("bake_images").put("bundle_id", BUNDLE);
        ArrayNode bakeImages = bake.putArray("images");
        for (String cam : CAMERAS) {
            ObjectNode image = mapper.createObjectNode().put("camera_id", cam);
            image.setAll(put(prefix + "bake-" + cam + ".jpg", jpeg("bake-" + cam)));
            bakeImages.add(image.put("face_policy", "OBSERVED"));
        }
        mvc.perform(heartbeat(s, 0.95, bakingSnap)).andExpect(status().isOk());

        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview(s)).putNull("scene_version")
                .put("bake_skipped", "LIVE_WORK_STARTED");
        ObjectNode artifacts = result.putObject("artifacts");
        artifacts.set("calibration_document", r.get("calibration"));
        artifacts.set("preview_scene", r.get("preview"));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        return new Done(s.sessionId(), prefix, r.get("calibration").get("uri").asString(), preview(s), null);
    }

    private void leaseExpect(org.springframework.test.web.servlet.ResultMatcher expected) throws Exception {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add("site_setup");
        p.put("wait_ms", 0);
        mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", p).toString()))
                .andExpect(expected);
    }

    private ObjectNode rebakeSnapshot(Done s, String stage, double pct) {
        return withChecks(snapshot(s.sessionId(), stage, pct)).put("calibration_version", newCalibration)
                .put("preview_scene_version", s.preview()).put("mode", "SUPPLEMENT_REBAKE");
    }

    private ObjectNode supplementSummary(String status, String reason, String m1, String s1, String m2, String s2) {
        ObjectNode sup = mapper.createObjectNode().put("status", status);
        if (reason == null) {
            sup.putNull("reason");
        } else {
            sup.put("reason", reason);
        }
        sup.put("input_role", "SETUP_SUPPLEMENT_CAPTURE").put("media_count", m2 == null ? 1 : 2)
                .put("frames_selected", 30).put("frames_registered", "APPLIED".equals(status) ? 28 : 2);
        ArrayNode media = sup.putArray("media");
        media.addObject().put("media_id", m1).put("status", s1).put("frames_selected", 29)
                .put("frames_registered", "USED".equals(s1) ? 28 : 2);
        if (m2 != null) {
            media.addObject().put("media_id", m2).put("status", s2).put("frames_selected", 1).put("frames_registered", 0);
        }
        return sup;
    }

    private JsonNode issue(String sessionId, ObjectNode body) throws Exception {
        return body(mvc.perform(post("/api/setup-sessions/{id}/supplement-media", sessionId).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(body.toString()))
                .andExpect(status().isCreated()).andReturn());
    }

    /** 올리고 확인까지 마친 파일의 media_id */
    private String uploadedMedia(String sessionId, String contentType, byte[] content, String source) throws Exception {
        ObjectNode body = mapper.createObjectNode().put("content_type", contentType).put("size_bytes", content.length);
        if (source != null) {
            body.put("source", source);
        }
        if (contentType.equals("image/jpeg")) {
            body.put("sha256", sha256Hex(content));
            body.putObject("device").put("make", "Canon").put("model", "EOS R6");
        }
        JsonNode issued = issue(sessionId, body);
        String mediaId = issued.get("media_id").asString();
        String key = issued.at("/upload/url").asString().substring("https://storage.test/put/".length());
        objects.put(key, content);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media/{m}/complete", sessionId, mediaId).with(adminJwt()))
                .andExpect(status().isOk());
        return mediaId;
    }

    private Session startSession() throws Exception {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam_A", 2.42).put("cam_B", 2.48).put("cam_C", 2.45).put("cam_D", 2.51);
        JsonNode created = body(mvc.perform(post("/api/setup-sessions").with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(req.toString()))
                .andExpect(status().isAccepted()).andReturn());
        JsonNode leased = leaseRaw();
        assertThat(leased.at("/payload/job_id").asString()).isEqualTo(created.get("job_id").asString());
        return new Session(created.get("setup_session_id").asString(), created.get("job_id").asString(),
                leased.at("/payload/lease_id").asString());
    }

    private JsonNode leaseRaw() throws Exception {
        ObjectNode p = mapper.createObjectNode();
        p.putArray("job_types").add("site_setup");
        p.put("wait_ms", 0);
        return body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", p).toString()))
                .andExpect(status().isOk()).andReturn());
    }

    private ObjectNode put(String key, byte[] content) {
        objects.put(key, content);
        return mapper.createObjectNode().put("uri", BUCKET + key).put("sha256", sha256Hex(content))
                .put("size_bytes", content.length);
    }

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
        for (String cam : CAMERAS) {
            cameras.add(((ObjectNode) template.deepCopy()).put("camera_id", cam));
        }
        doc.remove("content_hash");
        return mapper.writeValueAsBytes(doc);
    }

    private ObjectNode snapshot(String sessionId, String stage, double pct) {
        return mapper.createObjectNode().put("setup_session_id", sessionId).put("status", stage).put("progress_pct", pct);
    }

    private ObjectNode withChecks(ObjectNode node) {
        ObjectNode checks = node.putObject("height_checks");
        double[] heights = {2.42, 2.48, 2.45, 2.51};
        for (int i = 0; i < 4; i++) {
            checks.putObject(CAMERAS.get(i)).put("input_height_m", heights[i]).put("restored_height_m", heights[i])
                    .put("difference_cm", 0.0).put("within_tolerance", true);
        }
        // 카메라 확인 통과(S15P31A101-646): 구역 단계·세팅 완료 관문을 지나야 하는 흐름 시험이다
        node.set("camera_check", SetupCameraCheckFixtures.passed(mapper, newCalibration));
        return node;
    }

    private RequestBuilder heartbeat(Session s, double progress, ObjectNode siteSetup) {
        ObjectNode p = mapper.createObjectNode().put("job_id", s.jobId()).put("lease_id", s.leaseId()).put("progress", progress);
        p.set("site_setup", siteSetup);
        ObjectNode env = envelope("job_heartbeat", "2.9.0", p);
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

    private RequestBuilder callback(Session s, ObjectNode payload) {
        ObjectNode env = envelope("job_callback", "2.17.0", payload);
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

    private RequestBuilder complete(String sessionId) {
        return post("/api/setup-sessions/{id}/complete", sessionId).with(adminJwt());
    }

    private String currentScene() {
        return jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s WHERE site_id = ?",
                String.class, site).stream().findFirst().orElse(null);
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

    private static String sha256Hex(byte[] content) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(content));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }
}
