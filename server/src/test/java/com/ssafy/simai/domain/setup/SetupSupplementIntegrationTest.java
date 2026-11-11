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
    private static final List<String> CAMERAS = List.of("cam1", "cam2", "cam3", "cam4");

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
        jdbc.update("DELETE FROM work_unit WHERE name = ?", "보강촬영작업-" + suffix);
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
        create.putObject("camera_heights_m").put("cam1", 2.42).put("cam2", 2.48).put("cam3", 2.45).put("cam4", 2.51);
        mvc.perform(post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(create.toString()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ALREADY_IN_PROGRESS"));
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

        // 결과 APPLIED: 다시 굽기 대여 경로의 새 GLB → 세션 귀속 SCENE 후보 → 현재 Scene 이 기준 맵이었으므로 현재화
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
                .andExpect(jsonPath("$.supplement.can_rebake").value(true));
        // 다시 구운 맵도 원래 세팅의 고정 카메라 사진으로 카메라 시점 VDT 를 준다
        ObjectNode images = sceneCameraImages.images(newScene);
        assertThat(images.path("bundle_id").asString()).isEqualTo(BUNDLE);
        assertThat(images.path("images")).hasSize(4);
        assertThat(schemas.violations("messages/scene_camera_images", images.toString())).isEmpty();
    }

    @Test
    void FALLBACK_결과는_새_Scene_을_등록하지_않고_원래_맵을_유지한다() throws Exception {
        Done s = completedSession();
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

        assertThat(currentScene()).isEqualTo(s.scene());
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
        uploadedMedia(s.sessionId(), "image/png", "png".getBytes(StandardCharsets.UTF_8), null);
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake/cancel", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_REBAKE_NOT_RUNNING"));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted());
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake/cancel", s.sessionId()).with(adminJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.supplement.rebake.status").value("CANCELLED"))
                .andExpect(jsonPath("$.supplement.can_rebake").value(true));
        assertThat(currentScene()).isEqualTo(s.scene());
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isAccepted()).andExpect(jsonPath("$.supplement.rebake.status").value("QUEUED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_supplement_rebake", Integer.class)).isEqualTo(2);
    }

    // ---------------------------------------------------------------- 도우미

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
        assertThat(currentScene()).isEqualTo(baked);
        return new Done(s.sessionId(), prefix, r.get("calibration").get("uri").asString(), preview(s), baked);
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
        req.putObject("camera_heights_m").put("cam1", 2.42).put("cam2", 2.48).put("cam3", 2.45).put("cam4", 2.51);
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
            checks.putObject("cam" + (i + 1)).put("input_height_m", heights[i]).put("restored_height_m", heights[i])
                    .put("difference_cm", 0.0).put("within_tolerance", true);
        }
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
