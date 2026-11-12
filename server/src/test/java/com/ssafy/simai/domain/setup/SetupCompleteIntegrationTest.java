package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyInt;
import static org.mockito.ArgumentMatchers.anyLong;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.ArgumentMatchers.nullable;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.never;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.report.TbmFormAiService;
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
import java.util.concurrent.CopyOnWriteArrayList;
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
 * 세팅 완료 POST /api/setup-sessions/{id}/complete (C17, S15P31A101-615). 조건별 거부, 새 모델 적용(구역 적용 → 완료 기록 →
 * 이 세션 맵 켜기 → 구성 묶음 한 번), 완료 뒤 새 세션·구역 목록 저장·보강 거부, 커밋 뒤 TBM 초안 시작을 실제 DB 로 본다.
 * 객체 저장소는 가짜(키 → 바이트), TBM AI 초안은 호출만 본다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class SetupCompleteIntegrationTest {

    private static final Path CONTRACTS = Path.of(System.getProperty("simai.contracts.dir", "../contracts"));
    private static final String BUCKET = "s3://simai-test-bucket/";

    @Autowired MockMvc mvc;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper mapper;
    @Autowired MachineTokenService machines;
    @Autowired ContractSchemas schemas;

    @MockitoBean ObjectStorage storage;
    @MockitoBean TbmFormAiService tbmFormAi;
    /** 실제 동작. 발행 시점의 완료 기록·구역을 보고 발행 횟수를 센다 */
    @MockitoSpyBean ConfigBundleService configs;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String worker = "worker-setup-done-" + suffix;
    private final String baseCalibration = "cal-done-base-" + suffix;
    private final String newCalibration = "cal-done-" + suffix + "-jba2-0001";
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
                new ObjectStorage.SignedRequest("https://storage.test/put/" + i.getArgument(0), Map.of(), Instant.now().plusSeconds(300)));

        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
        userId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, display_label, role, created_at)
                VALUES (?, 'hash', '세팅완료관리자', 'ADMIN', now()) RETURNING user_id
                """, Long.class, "setup-done-admin-" + suffix);
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(), "세팅완료작업-" + suffix, userId);
        workUnitId = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, "세팅완료작업-" + suffix);
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
        jdbc.update("DELETE FROM setup_session_zone_plan");
        jdbc.update("DELETE FROM setup_session_zone");
        jdbc.update("DELETE FROM setup_supplement_media");
        jdbc.update("DELETE FROM setup_session");
        // 이 시험 관리자가 만든 구역·작업면(완료가 적용한 것)
        jdbc.update("""
                DELETE FROM static_object WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id = ?)
                """, "setup-done-admin-" + suffix);
        jdbc.update("DELETE FROM zone WHERE created_by IN (SELECT user_id FROM app_user WHERE login_id = ?)",
                "setup-done-admin-" + suffix);
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
        jdbc.update("DELETE FROM work_unit WHERE name LIKE ?", "세팅완료%-" + suffix);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "setup-done-admin-" + suffix);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void 완료는_구역을_적용하고_완료를_기록한_뒤_이_세션_맵을_켜고_묶음을_한_번_발행하며_커밋_뒤_TBM_초안을_시작한다() throws Exception {
        Session s = startSession();
        String preview = ready(s);
        // READY 는 관제 맵·묶음을 바꾸지 않는다
        assertThat(currentScene()).isNotEqualTo(preview);
        assertThat(bundleCount()).isZero();

        // 세팅 중 구역 목록: 새 구역 하나(-612)
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("source", "MANUAL").set("zone", zoneBody("세팅 완료 새 구역"));
        putPlan(s.sessionId(), items, 0).andExpect(status().isOk());

        // 발행 시점: 같은 트랜잭션에서 완료 기록과 적용한 구역이 이미 보여야 한다(적용 → 완료 기록 → 발행)
        List<Boolean> completedAtPublish = new CopyOnWriteArrayList<>();
        List<Integer> createdZonesAtPublish = new CopyOnWriteArrayList<>();
        doAnswer(inv -> {
            completedAtPublish.add(jdbc.queryForObject("SELECT setup_completed_at IS NOT NULL FROM work_unit WHERE work_id = ?",
                    Boolean.class, workUnitId));
            createdZonesAtPublish.add(jdbc.queryForObject(
                    "SELECT count(*) FROM setup_session_zone WHERE setup_session_id = ?::uuid", Integer.class, s.sessionId()));
            return inv.callRealMethod();
        }).when(configs).publishInTransaction(eq(newCalibration), eq(userId));
        // TBM 초안 시작은 커밋 뒤(다른 스레드에서 커밋된 완료 기록이 보인다)
        List<Boolean> committedAtTbm = new CopyOnWriteArrayList<>();
        when(tbmFormAi.tryStartDraftForWork(workUnitId)).thenAnswer(inv -> {
            committedAtTbm.add(jdbc.queryForObject("SELECT setup_completed_at IS NOT NULL FROM work_unit WHERE work_id = ?",
                    Boolean.class, workUnitId));
            return Optional.empty();
        });

        JsonNode done = body(mvc.perform(complete(s.sessionId())).andExpect(status().isOk()).andReturn());
        assertThat(done.get("setup_session_id").asString()).isEqualTo(s.sessionId());
        assertThat(done.get("setup_completed_at_ms").isNumber()).isTrue();
        assertThat(done.at("/applied_zones/created")).hasSize(1);
        assertThat(done.at("/applied_zones/archived").isArray()).isTrue();
        String createdZone = done.at("/applied_zones/created/0").asString();

        assertThat(completedAtPublish).containsExactly(true);
        assertThat(createdZonesAtPublish).containsExactly(1);
        verify(configs, never()).publishForSetupAfterCommit(anyString(), anyLong());
        // 묶음 하나: 새 보정 + 적용한 구역
        assertThat(bundleCount()).isEqualTo(1);
        String configVersion = jdbc.queryForObject("SELECT config_version FROM config_bundle WHERE calibration_version = ?",
                String.class, newCalibration);
        assertThat(done.get("config_version").asString()).isEqualTo(configVersion);
        assertThat(jdbc.queryForObject("""
                SELECT count(*) FROM config_bundle_item WHERE config_version = ? AND item_type = 'ZONE' AND item_id = ?
                """, Integer.class, configVersion, createdZone)).isEqualTo(1);
        // 새 모델: 이 세션 맵(굽기 전이라 미리보기)을 켠다
        assertThat(currentScene()).isEqualTo(preview);
        assertThat(jdbc.queryForObject("SELECT actor FROM scene_activation WHERE scene_version = ? ORDER BY activated_at DESC LIMIT 1",
                String.class, preview)).isEqualTo("setup:" + s.sessionId());
        verify(tbmFormAi, timeout(5_000)).tryStartDraftForWork(workUnitId);
        assertThat(committedAtTbm).containsExactly(true);

        // 하루 1회: 다시 완료·새 세션·재시도·구역 목록 저장·보강 촬영을 거부한다
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ALREADY_COMPLETED"));
        mvc.perform(create()).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ALREADY_COMPLETED"));
        putPlan(s.sessionId(), items, 1).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ZONES_CLOSED"));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-media", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.createObjectNode().put("content_type", "image/png").put("size_bytes", 3).toString()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_SETUP_COMPLETED"));
        mvc.perform(post("/api/setup-sessions/{id}/supplement-rebake", s.sessionId()).with(adminJwt()))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SUPPLEMENT_SETUP_COMPLETED"));
        mvc.perform(get("/api/setup-sessions/{id}", s.sessionId()).with(viewerJwt()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.setup_completed_at_ms").isNumber())
                .andExpect(jsonPath("$.supplement.can_upload").value(false))
                .andExpect(jsonPath("$.supplement.can_rebake").value(false))
                .andExpect(jsonPath("$.supplement.rebake_blocked_reason").value("SETUP_COMPLETED"));
        // 세팅 실패 뒤 재시도도 같은 작업이라 막힌다
        jdbc.update("UPDATE setup_session SET status = 'FAILED', completed_at = now() WHERE setup_session_id = ?::uuid", s.sessionId());
        mvc.perform(post("/api/setup-sessions/{id}/retry", s.sessionId()).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content("{}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SETUP_ALREADY_COMPLETED"));
        assertThat(bundleCount()).isEqualTo(1);
    }

    @Test
    void 완료_뒤_도착한_정식_맵은_미리보기를_교체하고_묶음을_다시_만들지_않는다() throws Exception {
        Session s = startSession();
        String preview = ready(s);
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(preview);

        String prefix = prefix(s);
        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview).put("scene_version", baked);
        ObjectNode artifacts = result.putObject("artifacts");
        artifacts.set("calibration_document", calRef(s));
        artifacts.set("preview_scene", previewRef(s));
        artifacts.set("scene", put(prefix + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(baked);
        assertThat(bundleCount()).isEqualTo(1);
    }

    @Test
    void 완료_전에_끝난_정식_맵은_올리지_않고_완료가_그_맵을_켠다() throws Exception {
        Session s = startSession();
        String preview = ready(s);
        String baked = "scene-" + s.jobId() + "-" + s.leaseId();
        ObjectNode result = withChecks(snapshot(s.sessionId(), "COMPLETED", 100.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview).put("scene_version", baked);
        ObjectNode artifacts = result.putObject("artifacts");
        artifacts.set("calibration_document", calRef(s));
        artifacts.set("preview_scene", previewRef(s));
        artifacts.set("scene", put(prefix(s) + "scene.glb", "glb-baked".getBytes(StandardCharsets.UTF_8)));
        mvc.perform(callback(s, succeeded(s, result))).andExpect(status().isOk());
        assertThat(currentScene()).isNotEqualTo(baked);
        assertThat(bundleCount()).isZero();

        mvc.perform(complete(s.sessionId())).andExpect(status().isOk());
        assertThat(currentScene()).isEqualTo(baked);
        assertThat(bundleCount()).isEqualTo(1);
    }

    @Test
    void 조건이_안_맞으면_원인별로_거부하고_아무것도_바꾸지_않는다() throws Exception {
        mvc.perform(complete(UUID.randomUUID().toString())).andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("SETUP_SESSION_NOT_FOUND"));

        Session s = startSession();
        // 미리보기 전(COLLECTING_BACKGROUND)
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_NOT_READY_TO_COMPLETE"));
        String preview = ready(s);
        // 열람자는 못 한다
        mvc.perform(post("/api/setup-sessions/{id}/complete", s.sessionId()).with(viewerJwt())).andExpect(status().isForbidden());

        // 날짜 작업인데 오늘이 아니다(유일 색인과 겹치지 않을 먼 과거 날짜)
        jdbc.update("UPDATE work_unit SET daily = true, work_date = ? WHERE work_id = ?", LocalDate.now().minusDays(4100), workUnitId);
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("WORK_NOT_TODAY"));
        jdbc.update("UPDATE work_unit SET daily = false, work_date = ? WHERE work_id = ?", LocalDate.now(), workUnitId);

        // 같은 현장의 다른 작업이 관제 중
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, actual_started_at,
                    created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '1 hour', 1, 'IN_PROGRESS', now(), ?, now(), now())
                """, site, LocalDate.now(), "세팅완료관제-" + suffix, userId);
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_COMPLETE_BLOCKED_BY_LIVE_WORK"));
        jdbc.update("UPDATE work_unit SET status = 'DONE', actual_ended_at = now() WHERE name = ?", "세팅완료관제-" + suffix);

        // 작업이 PLANNED 가 아니다(이 작업이 이미 시작됨)
        jdbc.update("UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = now() WHERE work_id = ?", workUnitId);
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("WORK_INVALID_STATE"));
        jdbc.update("UPDATE work_unit SET status = 'PLANNED', actual_started_at = NULL WHERE work_id = ?", workUnitId);

        // 작업에 지금 걸린 세션이 아니다
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE work_id = ?", workUnitId);
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_SESSION_NOT_ACTIVE"));
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ?::uuid WHERE work_id = ?", s.sessionId(), workUnitId);

        // World 가 바뀐 세팅(world_anchor 채택 아님)에 기존 zone_id 를 이으면 적용 서비스의 409 를 그대로 내고 모두 되돌린다
        String existing = "z-done-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from,created_by)
                VALUES (?,1,?,'기존','OPENING','GROUND_FOOTPRINT',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),'HIGH',now(),?)
                """, existing, site, userId);
        ArrayNode items = mapper.createArrayNode();
        items.addObject().put("zone_id", existing).put("source", "AI");
        putPlan(s.sessionId(), items, 0).andExpect(status().isOk());
        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ZONE_WORLD_MISMATCH"));

        assertThat(jdbc.queryForObject("SELECT setup_completed_at IS NULL FROM work_unit WHERE work_id = ?", Boolean.class,
                workUnitId)).isTrue();
        assertThat(bundleCount()).isZero();
        assertThat(currentScene()).isNotEqualTo(preview);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE zone_id = ? AND valid_to IS NULL", Integer.class, existing))
                .isEqualTo(1);
        verify(tbmFormAi, never()).tryStartDraftForWork(anyLong());

        // World 다시 맞추기가 채택된 새 모델 세팅은 같은 World 라 기존 구역을 잇는다
        jdbc.update("""
                UPDATE setup_session SET world_anchor = '{"accepted": true, "reason": "ANCHORED"}'::jsonb
                WHERE setup_session_id = ?::uuid
                """, s.sessionId());
        mvc.perform(complete(s.sessionId())).andExpect(status().isOk())
                .andExpect(jsonPath("$.applied_zones.kept[0]").value(existing));
        assertThat(currentScene()).isEqualTo(preview);
    }

    @Test
    void 확인_필요_구역이_남으면_완료를_거부하고_모두_확인하면_완료한다() throws Exception {
        Session s = startSession();
        String preview = ready(s);
        String existing = "z-review-" + suffix;
        jdbc.update("""
                INSERT INTO zone(zone_id,zone_version,site_id,name,zone_kind,eval_type,polygon,severity,valid_from,created_by)
                VALUES (?,1,?,'이어 쓸 구역','OPENING','GROUND_FOOTPRINT',ST_GeomFromText('POLYGON((0 0,2 0,2 1,0 1,0 0))',0),'HIGH',now(),?)
                """, existing, site, userId);
        jdbc.update("""
                UPDATE setup_session SET world_anchor = '{"accepted": true, "reason": "ANCHORED"}'::jsonb
                WHERE setup_session_id = ?::uuid
                """, s.sessionId());
        // 새 모델 + World 이어짐: 지금 관제 구역을 확인 필요로 채운다(S15P31A101-629)
        JsonNode seeded = body(mvc.perform(post("/api/setup-sessions/{id}/zones/seed", s.sessionId()).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        int pending = 0;
        for (JsonNode item : seeded.path("items")) {
            if (item.path("needs_review").asBoolean(false)) pending++;
        }
        assertThat(pending).isPositive();

        mvc.perform(complete(s.sessionId())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ZONES_UNCONFIRMED"))
                .andExpect(jsonPath("$.unconfirmed_count").value(pending));
        assertThat(jdbc.queryForObject("SELECT setup_completed_at IS NULL FROM work_unit WHERE work_id = ?", Boolean.class,
                workUnitId)).isTrue();
        assertThat(bundleCount()).isZero();

        ArrayNode confirmed = (ArrayNode) seeded.path("items").deepCopy();
        confirmed.forEach(item -> ((ObjectNode) item).put("needs_review", false));
        putPlan(s.sessionId(), confirmed, 1).andExpect(status().isOk());
        JsonNode done = body(mvc.perform(complete(s.sessionId())).andExpect(status().isOk()).andReturn());
        assertThat(done.at("/applied_zones/kept").toString()).contains(existing);
        assertThat(currentScene()).isEqualTo(preview);
    }

    // ---------------------------------------------------------------- 도우미

    private record Session(String sessionId, String jobId, String leaseId) {
    }

    private String prefix(Session s) {
        return "job-objects/site_setup/" + s.jobId() + "/" + s.leaseId() + "/";
    }

    private ObjectNode calRef(Session s) throws Exception {
        return put(prefix(s) + "calibration-" + newCalibration + ".json", calibrationDocument(newCalibration, s.sessionId()));
    }

    private ObjectNode previewRef(Session s) {
        return put(prefix(s) + "preview_scene.glb", "glb-preview".getBytes(StandardCharsets.UTF_8));
    }

    /** 미리보기(READY_FOR_ZONES)까지: 새 보정 문서·미리보기 GLB 등록. 미리보기 버전을 돌려준다 */
    private String ready(Session s) throws Exception {
        String preview = "scene-preview-" + s.jobId() + "-" + s.leaseId();
        ObjectNode readySnap = withChecks(snapshot(s.sessionId(), "READY_FOR_ZONES", 85.0))
                .put("calibration_version", newCalibration).put("preview_scene_version", preview);
        ObjectNode artifacts = readySnap.putObject("artifacts");
        artifacts.set("calibration_document", calRef(s));
        artifacts.set("preview_scene", previewRef(s));
        mvc.perform(heartbeat(s, 0.85, readySnap)).andExpect(status().isOk());
        return preview;
    }

    private int bundleCount() {
        return jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class, newCalibration);
    }

    private RequestBuilder complete(String sessionId) {
        return post("/api/setup-sessions/{id}/complete", sessionId).with(adminJwt());
    }

    private RequestBuilder create() {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam1", 2.42).put("cam2", 2.48).put("cam3", 2.45).put("cam4", 2.51);
        return post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(req.toString());
    }

    private org.springframework.test.web.servlet.ResultActions putPlan(String sessionId, JsonNode items, int revision)
            throws Exception {
        ObjectNode b = mapper.createObjectNode();
        b.set("items", items);
        b.put("revision", revision);
        return mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders.put("/api/setup-sessions/{id}/zones", sessionId).with(adminJwt())
                .contentType(MediaType.APPLICATION_JSON).content(b.toString()));
    }

    private ObjectNode zoneBody(String name) {
        ObjectNode z = mapper.createObjectNode();
        z.put("name", name).put("zone_kind", "OPENING").put("eval_type", "GROUND_FOOTPRINT").put("severity", "CRITICAL");
        ArrayNode poly = z.putArray("polygon_xy");
        poly.addArray().add(0.0).add(0.0);
        poly.addArray().add(1.0).add(0.0);
        poly.addArray().add(1.0).add(1.0);
        poly.addArray().add(0.0).add(1.0);
        return z;
    }

    private String currentScene() {
        return jdbc.queryForList("SELECT scene_version FROM (" + SceneService.CURRENT_COMMITTED + ") s WHERE site_id = ?",
                String.class, site).stream().findFirst().orElse(null);
    }

    private Session startSession() throws Exception {
        JsonNode created = body(mvc.perform(create()).andExpect(status().isAccepted()).andReturn());
        String sessionId = created.get("setup_session_id").asString();
        String jobId = created.get("job_id").asString();
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

    private RequestBuilder callback(Session s, ObjectNode payload) {
        ObjectNode env = envelope("job_callback", "2.10.0", payload);
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
