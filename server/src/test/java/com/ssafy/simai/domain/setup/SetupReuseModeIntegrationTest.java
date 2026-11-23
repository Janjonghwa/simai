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

import com.ssafy.simai.domain.calibration.CalibrationService;
import com.ssafy.simai.domain.coverage.CoverageGuideService;
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
import java.sql.Timestamp;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
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
 * 세팅 모드 새 맵 / 이전 맵 재사용(S15P31A101-614·-689·-712·-714). POST /api/setup-sessions 의 mode 검사, 날짜 작업 오늘만 검사,
 * 재사용 후보 맵 목록(최근 순·DB 사실로 가린 맵·시점 확인 순위·추천), 고른 맵의 묶음(보정·높이·구역·맵)을 그대로 쓰는 재사용(워커 작업 없음,
 * 완료 때 그 맵 켜기·같은 내용의 보정 다시 내기·그 맵의 구역 채우기)을 본다. 객체 저장소는 가짜(키 → 바이트)다.
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

    @Autowired
    CalibrationService calibrations;

    @MockitoBean
    ObjectStorage storage;

    /** Tier 0 회전 감시(-540) 재세팅 카메라만 바꿔 본다(S15P31A101-689). 기본은 Tier 0 끊김(모름) */
    @MockitoSpyBean
    ViewerHealthService health;

    /** 세팅 1단계 헬퍼의 재사용 시점 확인(S15P31A101-712)만 바꿔 본다. 기본은 결과 없음(모름) */
    @MockitoSpyBean
    CoverageGuideService coverageGuides;

    /** 시연 계정 판정만 바꿔 본다. 기본은 시연 계정 아님(시험 설정 demo-user-ids 비움) */
    @MockitoSpyBean
    SetupDemoAccounts demoAccounts;

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
        doReturn(Optional.empty()).when(coverageGuides).reuseViewCheck(anyString());
    }

    @AfterEach
    void tearDown() {
        cleanUpTables();
        jdbc.update("UPDATE site SET default_camera_heights_m = '{}'::jsonb WHERE site_id = ?", site);
    }

    private void cleanUpTables() {
        List<String> cals = new ArrayList<>(jdbc.queryForList(
                "SELECT calibration_version FROM calibration_version WHERE calibration_version LIKE ?",
                String.class, "cal-reuse-map-%" + suffix + "%"));
        cals.addAll(List.of(baseCalibration, newCalibration));
        jdbc.update("UPDATE work_unit SET active_setup_session_id = NULL WHERE active_setup_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_zone_draft");
        jdbc.update("DELETE FROM setup_zone_draft_run");
        // 완료가 만든 구역(세션에 이은 새 구역)과 그 작업면, 이 시험이 넣은 구역·작업면
        List<String> createdZones = jdbc.queryForList("SELECT zone_id FROM setup_session_zone", String.class);
        for (String z : createdZones) {
            jdbc.update("DELETE FROM static_object WHERE zone_id = ?", z);
        }
        jdbc.update("DELETE FROM setup_session_zone");
        jdbc.update("DELETE FROM setup_session_zone_plan");
        for (String z : createdZones) {
            jdbc.update("DELETE FROM zone WHERE zone_id = ?", z);
        }
        jdbc.update("DELETE FROM static_object WHERE object_id LIKE ?", "%" + suffix);
        jdbc.update("DELETE FROM zone WHERE zone_id LIKE ?", "%" + suffix);
        jdbc.update("DELETE FROM setup_session WHERE reuse_source_session_id IS NOT NULL");
        jdbc.update("DELETE FROM setup_session");
        jdbc.update("DELETE FROM scene_activation WHERE scene_version LIKE ?", "%" + suffix);
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
        // 다시 낸 보정이 원래 보정을 이전 버전으로 가리키므로 나중 것부터 지운다
        for (String cal : cals) {
            if (cal.contains("-reuse-") && cal.startsWith("cal-reuse-map-")) {
                jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", cal);
            }
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version LIKE ?", "cal-reuse-map-%" + suffix);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", newCalibration);
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", baseCalibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
        jdbc.update("DELETE FROM work_unit WHERE name = ? OR name LIKE ?", "재사용세팅작업-" + suffix, "재사용지난작업-%-" + suffix);
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
    void 후보_맵은_최근_순으로_보이고_카메라_확인_실패와_세팅_미완료_맵은_이유와_함께_가린다() throws Exception {
        // 오래된 완료 맵(고를 수 있음), 최근이지만 카메라 확인 INCOMPLETE 인 맵(15차 같은 경우), 완료 기록 없는 맵
        MapFixture old = mapSession("old", Instant.now().minusSeconds(3 * 86400), true, "PASSED");
        MapFixture incomplete = mapSession("inc", Instant.now().minusSeconds(86400), true, "INCOMPLETE");
        MapFixture notDone = mapSession("nd", Instant.now().minusSeconds(3600), false, "PASSED");

        JsonNode check = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(check.get("available").asBoolean()).isFalse();
        // 고를 수 있는 맵은 아직 시점 확인 결과가 없어(헬퍼 결과 없음) 모른다. 모르면 그대로 쓰지 않는다
        assertThat(check.get("blocked_reason").asString()).isEqualTo("VIEW_UNCONFIRMED");
        JsonNode list = check.get("candidates");
        assertThat(list).hasSize(3);
        assertThat(list.at("/0/setup_session_id").asString()).isEqualTo(old.session().toString());
        assertThat(list.at("/0/status").asString()).isEqualTo("UNCONFIRMED");
        assertThat(list.at("/0/reason").asString()).isEqualTo("NO_RESULT");
        assertThat(list.at("/0/camera_heights_m/cam_A").asDouble()).isEqualTo(2.41);
        // 가린 맵은 최근 순으로 뒤에, 이유와 함께
        assertThat(list.at("/1/setup_session_id").asString()).isEqualTo(notDone.session().toString());
        assertThat(list.at("/1/status").asString()).isEqualTo("EXCLUDED");
        assertThat(list.at("/1/reason").asString()).isEqualTo("NOT_COMPLETED");
        assertThat(list.at("/2/setup_session_id").asString()).isEqualTo(incomplete.session().toString());
        assertThat(list.at("/2/reason").asString()).isEqualTo("CAMERA_CHECK_NOT_PASSED");
        assertThat(list.at("/2/selectable").asBoolean()).isFalse();

        mvc.perform(createReuse(null)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_VIEW_UNCONFIRMED"));
        mvc.perform(createReuse(incomplete.session())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_MAP_UNAVAILABLE"));
        mvc.perform(createReuse(UUID.randomUUID())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_MAP_UNAVAILABLE"));
        mvc.perform(get("/api/setup-sessions/reuse-check").with(viewerJwt())).andExpect(status().isForbidden());
    }

    @Test
    void 지금_카메라와_가장_잘_맞는_맵을_추천하고_어긋나거나_조금_돌아간_맵은_고를_수_없다() throws Exception {
        MapFixture older = mapSession("a", Instant.now().minusSeconds(2 * 86400), true, "PASSED");
        MapFixture newer = mapSession("b", Instant.now().minusSeconds(86400), true, null);  // 카메라 확인 도입 전 세팅(기록 없음)
        MapFixture third = mapSession("c", Instant.now().minusSeconds(3 * 86400), true, "PASSED");
        // 두 맵 모두 4대 같은 시점이지만 오래된 맵이 지금 화면과 더 많이 맞는다 → 기본은 오래된 맵(가장 최근이 아니다)
        view(older.scene(), 400, "SAME", 0.0);
        view(newer.scene(), 150, "SAME", 0.0);
        // 세 번째 맵은 카메라 하나가 1.7° 돌아가 있다(-712 기준으로는 같은 시점이지만 보정을 그대로 쓸 수는 없다)
        view(third.scene(), 500, "SAME", 0.03);

        JsonNode check = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(check.get("available").asBoolean()).isTrue();
        assertThat(check.get("recommended_setup_session_id").asString()).isEqualTo(older.session().toString());
        assertThat(check.get("scene_version").asString()).isEqualTo(older.scene());
        assertThat(check.get("camera_status").asString()).isEqualTo("NOT_DETECTED");
        JsonNode list = check.get("candidates");
        assertThat(list.at("/0/setup_session_id").asString()).isEqualTo(older.session().toString());
        assertThat(list.at("/0/recommended").asBoolean()).isTrue();
        assertThat(list.at("/1/setup_session_id").asString()).isEqualTo(newer.session().toString());
        assertThat(list.at("/1/status").asString()).isEqualTo("MATCH");
        assertThat(list.at("/1/recommended").asBoolean()).isFalse();
        assertThat(list.at("/2/status").asString()).isEqualTo("SHIFTED");
        assertThat(list.at("/2/shifted_cameras/0").asString()).isEqualTo("cam_D");
        assertThat(list.at("/2/cameras").size()).isEqualTo(4);
        mvc.perform(createReuse(third.session())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_CAMERA_CHANGED"));

        // 카메라가 그때와 달라진 맵(2회 연속 확인)은 이유(카메라)와 함께 막는다. 맞는 맵이 하나도 없으면 재사용은 꺼진다
        view(older.scene(), 400, "CHANGED", null);
        view(newer.scene(), 150, "CHANGED", null);
        JsonNode changed = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(changed.get("available").asBoolean()).isFalse();
        assertThat(changed.get("blocked_reason").asString()).isEqualTo("CAMERA_CHANGED");
        assertThat(changed.get("camera_source").asString()).isEqualTo("VIEW_MATCH");
        assertThat(changed.at("/candidates/2/status").asString()).isEqualTo("CHANGED");
        assertThat(changed.at("/candidates/2/changed_cameras/0").asString()).isEqualTo("cam_D");
        mvc.perform(createReuse(null)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_CAMERA_CHANGED"));

        // Tier 0 가 지금 그 맵의 보정을 쓰고 배경 회전 감시가 재세팅을 요구하면 그 맵은 바뀐 것이다
        view(older.scene(), 400, "SAME", 0.0);
        tier0Uses(older.calibration());
        doReturn(List.of("cam_C")).when(health).reSetupCameraIds();
        JsonNode drift = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(drift.get("available").asBoolean()).isFalse();
        JsonNode olderItem = find(drift.get("candidates"), older.session());
        assertThat(olderItem.get("status").asString()).isEqualTo("CHANGED");
        assertThat(olderItem.get("camera_source").asString()).isEqualTo("TIER0_DRIFT");
    }

    @Test
    void 시연_계정은_시점_확인과_추천_없이_완료된_맵을_모두_고른다() throws Exception {
        doReturn(true).when(demoAccounts).isDemo(userId);
        MapFixture older = mapSession("da", Instant.now().minusSeconds(2 * 86400), true, "PASSED");
        MapFixture newer = mapSession("db", Instant.now().minusSeconds(86400), true, "PASSED");
        MapFixture notDone = mapSession("dn", Instant.now().minusSeconds(3600), false, "PASSED");
        // 일반 계정이면 older 가 추천(MATCH)이고 newer 는 카메라가 달라져 막힌다
        view(older.scene(), 400, "SAME", 0.0);
        view(newer.scene(), 150, "CHANGED", null);

        JsonNode check = body(mvc.perform(reuseCheck()).andExpect(status().isOk()).andReturn());
        assertThat(check.get("available").asBoolean()).isTrue();
        assertThat(check.get("blocked_reason").isNull()).isTrue();
        assertThat(check.get("recommended_setup_session_id").isNull()).isTrue();
        JsonNode list = check.get("candidates");
        assertThat(find(list, older.session()).get("selectable").asBoolean()).isTrue();
        assertThat(find(list, older.session()).get("recommended").asBoolean()).isFalse();
        assertThat(find(list, newer.session()).get("status").asString()).isEqualTo("CHANGED");
        assertThat(find(list, newer.session()).get("selectable").asBoolean()).isTrue();
        // 완료 전 맵(EXCLUDED)은 기술적으로 쓸 수 없어 시연 계정도 고를 수 없다
        assertThat(find(list, notDone.session()).get("selectable").asBoolean()).isFalse();
        mvc.perform(createReuse(notDone.session())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_REUSE_MAP_UNAVAILABLE"));

        // 카메라가 달라진 맵도 시작한다
        JsonNode created = body(mvc.perform(createReuse(newer.session())).andExpect(status().isAccepted()).andReturn());
        assertThat(created.get("mode").asString()).isEqualTo("REUSE");
        assertThat(created.at("/reuse_source/setup_session_id").asString()).isEqualTo(newer.session().toString());
    }

    @Test
    void 시연_계정이_맵을_고르지_않으면_가장_최근에_완료된_맵을_쓴다() throws Exception {
        doReturn(true).when(demoAccounts).isDemo(userId);
        mapSession("do", Instant.now().minusSeconds(2 * 86400), true, "PASSED");
        MapFixture newest = mapSession("dw", Instant.now().minusSeconds(86400), true, "PASSED");
        // 둘 다 시점 확인 결과가 없다(일반 계정이면 VIEW_UNCONFIRMED 로 막힌다)
        JsonNode created = body(mvc.perform(createReuse(null)).andExpect(status().isAccepted()).andReturn());
        assertThat(created.at("/reuse_source/setup_session_id").asString()).isEqualTo(newest.session().toString());
    }

    @Test
    void 재사용은_고른_맵의_보정_높이_구역_맵을_그대로_쓰고_워커_작업_없이_바로_구역_단계로_간다() throws Exception {
        // 고른 맵(이틀 전): 그 보정으로 발행한 묶음에 구역 Z1(v1)·이은 작업면
        MapFixture chosen = mapSession("pick", Instant.now().minusSeconds(2 * 86400), true, "PASSED");
        String z1 = "zone-reuse-1-" + suffix;
        zone(z1, 1, "POLYGON((0 0, 2 0, 2 2, 0 2, 0 0))", true);
        surface("surface-reuse-1-" + suffix, 1, z1, true);
        bundle(chosen.calibration(), List.of(z1 + ":1"), List.of("surface-reuse-1-" + suffix + ":1"));
        // 그 뒤 다른 날 새 맵(다른 World)이 지금 관제 맵이 되었고 Z1 은 보관, Z2 가 관제 중
        jdbc.update("UPDATE zone SET valid_to = now() WHERE zone_id = ?", z1);
        jdbc.update("UPDATE static_object SET valid_to = now() WHERE object_id = ?", "surface-reuse-1-" + suffix);
        String z2 = "zone-reuse-2-" + suffix;
        zone(z2, 1, "POLYGON((5 5, 6 5, 6 6, 5 6, 5 5))", true);
        commitBaseScene();
        view(chosen.scene(), 300, "SAME", 0.0);

        JsonNode created = body(mvc.perform(createReuse(chosen.session())).andExpect(status().isAccepted()).andReturn());
        String id = created.get("setup_session_id").asString();
        assertThat(created.get("status").asString()).isEqualTo("COMPLETED");
        assertThat(created.get("progress_pct").asDouble()).isEqualTo(100.0);
        assertThat(created.get("job_id").isNull()).isTrue();
        assertThat(created.get("mode").asString()).isEqualTo("REUSE");
        assertThat(created.get("calibration_version").asString()).isEqualTo(chosen.calibration());
        assertThat(created.at("/camera_heights_m/cam_A").asDouble()).isEqualTo(2.41);
        assertThat(created.at("/reuse_source/setup_session_id").asString()).isEqualTo(chosen.session().toString());
        assertThat(created.at("/reuse_source/scene_version").asString()).isEqualTo(chosen.scene());
        assertThat(created.at("/reuse_source/view_check/cameras").size()).isEqualTo(4);
        assertThat(created.get("complete_blocked_reason").isNull()).isTrue();
        assertThat(created.get("paused_reason").isNull()).isTrue();
        assertThat(created.at("/supplement/rebake_blocked_reason").asString()).isEqualTo("REUSED_SCENE");
        // 워커 작업을 만들지 않는다(배경·Stage A 재계산 없음), 그 맵을 만들 때의 높이가 현장 기본값이 된다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE job_type = 'site_setup' AND job_id NOT LIKE 'job-reuse-map-%'",
                Integer.class)).isZero();
        assertThat(jdbc.queryForObject("SELECT default_camera_heights_m ->> 'cam_A' FROM site WHERE site_id = ?", String.class, site))
                .isEqualTo("2.41");

        // 구역 단계: 지금 관제 구역(다른 맵의 Z2)이 아니라 그 맵의 구역(Z1 본문·작업면)으로 채운다
        JsonNode plan = body(mvc.perform(post("/api/setup-sessions/{id}/zones/seed", id).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(plan.get("items")).hasSize(1);
        assertThat(plan.at("/items/0/zone_id").isNull() || !plan.at("/items/0").has("zone_id")).isTrue();
        assertThat(plan.at("/items/0/zone/name").asString()).isEqualTo("재사용 구역 " + z1);
        assertThat(plan.at("/items/0/zone/polygon_xy").size()).isGreaterThanOrEqualTo(4);
        assertThat(plan.at("/items/0/surface/top_height_m").asDouble()).isCloseTo(0.9, org.assertj.core.data.Offset.offset(1e-6));
        assertThat(plan.at("/items/0/needs_review").asBoolean()).isFalse();

        // 완료: 그 맵을 켜고, Tier 0 가 다른 보정을 쓰므로 그 맵의 보정을 계보 키만 바꾼 새 버전으로 내 묶음에 싣는다
        JsonNode done = body(mvc.perform(complete(id)).andExpect(status().isOk()).andReturn());
        assertThat(currentScene()).isEqualTo(chosen.scene());
        String reissued = done.get("calibration_version").asString();
        assertThat(reissued).startsWith(chosen.calibration() + "-reuse-");
        assertThat(jdbc.queryForObject("SELECT previous_calibration_version FROM calibration_version WHERE calibration_version = ?",
                String.class, reissued)).isEqualTo(baseCalibration);
        assertThat(jdbc.queryForObject("SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?",
                String.class, reissued)).isEqualTo(jdbc.queryForObject(
                "SELECT calibration_hash FROM calibration_version WHERE calibration_version = ?", String.class, chosen.calibration()));
        JsonNode reissuedDoc = mapper.readTree(jdbc.queryForObject(
                "SELECT content FROM calibration_document WHERE calibration_version = ?", byte[].class, reissued));
        assertThat(reissuedDoc.get("setup_session_id").asString()).isEqualTo(id);
        assertThat(reissuedDoc.get("previous_calibration_version").asString()).isEqualTo(baseCalibration);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM config_bundle WHERE calibration_version = ?", Integer.class, reissued))
                .isEqualTo(1);
        // 구역: 다른 맵의 Z2 는 보관, 그 맵의 Z1 본문으로 새 구역
        assertThat(done.at("/applied_zones/archived").toString()).contains(z2);
        assertThat(done.at("/applied_zones/created").size()).isEqualTo(1);
        // 같은 내용의 보정이라 맵은 흐리지 않는다
        JsonNode compat = compatibility.compare(chosen.scene(), chosen.calibration(), reissued);
        assertThat(compat.get("verification").asString()).isEqualTo("VERIFIED");
        assertThat(compat.get("reason").asString()).isEqualTo("SAME_CALIBRATION");
        // 다음 날 재사용 목록에서 그 맵의 보정 버전 묶음에 새로 낸 버전도 들어간다(구역은 그 묶음에서 다시 채운다)
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_session WHERE reuse_source_session_id = ?", Integer.class,
                chosen.session())).isEqualTo(1);
    }

    @Test
    void Tier0_가_이미_그_맵의_보정을_쓰면_새_버전을_내지_않고_그대로_싣는다() throws Exception {
        MapFixture chosen = mapSession("same", Instant.now().minusSeconds(86400), true, "PASSED");
        bundle(chosen.calibration(), List.of(), List.of());
        tier0Uses(chosen.calibration());
        view(chosen.scene(), 300, "SAME", 0.004);
        JsonNode created = body(mvc.perform(createReuse(null)).andExpect(status().isAccepted()).andReturn());
        String id = created.get("setup_session_id").asString();
        mvc.perform(post("/api/setup-sessions/{id}/zones/seed", id).with(adminJwt())).andExpect(status().isOk());
        JsonNode done = body(mvc.perform(complete(id)).andExpect(status().isOk()).andReturn());
        assertThat(done.get("calibration_version").asString()).isEqualTo(chosen.calibration());
        assertThat(jdbc.queryForObject("SELECT count(*) FROM calibration_version WHERE calibration_version LIKE ?", Integer.class,
                chosen.calibration() + "-reuse-%")).isZero();
        assertThat(currentScene()).isEqualTo(chosen.scene());
        // 같은 작업에 두 번째 세팅은 만들지 않는다(하루 1회)
        mvc.perform(createReuse(null)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("SETUP_ALREADY_COMPLETED"));
    }

    @Test
    void 재사용_세션은_그_맵을_만든_세팅의_AI_초안을_보이고_채우기에서는_그_초안으로_만든_구역만_뺀다() throws Exception {
        // 그 맵을 만든 NEW 세팅: AI 초안(개구부 하나) → 수락해 만든 구역 zAi, 사람이 그린 구역 zHuman,
        // 지금 다시 보이지 않는 초안(시연 화면의 가짜 초안처럼 DB 초안에 없는 후보)으로 만든 구역 zOther
        MapFixture chosen = mapSession("ai", Instant.now().minusSeconds(86400), true, "PASSED");
        draftRun(chosen.session(), chosen.calibration(), "cand-open-1", "[[1, 1], [3, 1], [3, 3], [1, 3]]");
        String zAi = "zone-reuse-ai-" + suffix;
        String zHuman = "zone-reuse-human-" + suffix;
        String zOther = "zone-reuse-other-" + suffix;
        zone(zAi, 1, "POLYGON((0.9 0.9, 3.1 0.9, 3.1 3.1, 0.9 3.1, 0.9 0.9))", true);
        zone(zHuman, 1, "POLYGON((5 5, 6 5, 6 6, 5 6, 5 5))", true);
        zone(zOther, 1, "POLYGON((8 8, 9 8, 9 9, 8 9, 8 8))", true);
        for (String z : List.of(zAi, zHuman, zOther)) {
            jdbc.update("INSERT INTO setup_session_zone (setup_session_id, zone_id, zone_version, created_at) VALUES (?, ?, 1, now())",
                    chosen.session(), z);
        }
        ArrayNode sourceItems = mapper.createArrayNode();
        sourceItems.addObject().putNull("zone_id").put("source", "AI").put("candidate_id", "cand-open-1")
                .set("zone", zoneBody("단상 개구부 1", "[[0.9, 0.9], [3.1, 0.9], [3.1, 3.1], [0.9, 3.1]]"));
        sourceItems.addObject().putNull("zone_id").put("source", "MANUAL").putNull("candidate_id")
                .set("zone", zoneBody("사람 구역", "[[5, 5], [6, 5], [6, 6], [5, 6]]"));
        sourceItems.addObject().putNull("zone_id").put("source", "AI").put("candidate_id", "demo-opening-stage")
                .set("zone", zoneBody("다른 초안 구역", "[[8, 8], [9, 8], [9, 9], [8, 9]]"));
        jdbc.update("""
                INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                VALUES (?, ?::jsonb, 4, ?, now())
                """, chosen.session(), sourceItems.toString(), userId);
        bundle(chosen.calibration(), List.of(zAi + ":1", zHuman + ":1", zOther + ":1"), List.of());
        tier0Uses(chosen.calibration());
        view(chosen.scene(), 300, "SAME", 0.0);
        String sourcePlanBefore = sourcePlan(chosen.session());

        JsonNode created = body(mvc.perform(createReuse(chosen.session())).andExpect(status().isAccepted()).andReturn());
        String id = created.get("setup_session_id").asString();

        // 초안: 재사용 세션은 초안 작업이 없어도 그 맵을 만든 세팅의 초안을 그대로(같은 보정·같은 World 좌표) 받는다
        JsonNode drafts = body(mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", id).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(drafts.get("setup_session_id").asString()).isEqualTo(id);
        assertThat(drafts.get("draft_source_setup_session_id").asString()).isEqualTo(chosen.session().toString());
        assertThat(drafts.get("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(drafts.get("calibration_version").asString()).isEqualTo(chosen.calibration());
        assertThat(created.get("calibration_version").asString()).isEqualTo(chosen.calibration());
        assertThat(drafts.get("drafts")).hasSize(1);
        assertThat(drafts.at("/drafts/0/candidate_id").asString()).isEqualTo("cand-open-1");
        assertThat(drafts.at("/drafts/0/polygon_xy/1/0").asDouble()).isEqualTo(3.0);
        // 재사용 세션에는 초안 작업을 새로 만들지 않는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_zone_draft_run WHERE setup_session_id = ?::uuid", Integer.class, id))
                .isZero();

        // 채우기: 그 초안을 수락해 만든 zAi 는 빼고(초안으로 다시 보인다), 사람 구역과 다시 보이지 않는 초안의 구역은 그대로
        JsonNode plan = body(mvc.perform(post("/api/setup-sessions/{id}/zones/seed", id).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        List<String> seeded = new ArrayList<>();
        plan.get("items").forEach(item -> seeded.add(item.get("zone_id").asString()));
        assertThat(seeded).containsExactlyInAnyOrder(zHuman, zOther);

        // 수락: 그 초안 ID 로 이 세션 구역 목록에 AI 항목을 더한다(화면 useAcceptZoneDraft 와 같은 저장)
        ArrayNode items = ((ArrayNode) plan.get("items")).deepCopy();
        items.addObject().putNull("zone_id").put("source", "AI").put("candidate_id", "cand-open-1")
                .set("zone", zoneBody("단상 개구부 1", "[[0.9, 0.9], [3.1, 0.9], [3.1, 3.1], [0.9, 3.1]]"));
        ObjectNode put = mapper.createObjectNode().put("revision", plan.get("revision").asInt());
        put.set("items", items);
        JsonNode accepted = body(mvc.perform(org.springframework.test.web.servlet.request.MockMvcRequestBuilders
                        .put("/api/setup-sessions/{id}/zones", id).with(adminJwt())
                        .contentType(MediaType.APPLICATION_JSON).content(put.toString()))
                .andExpect(status().isOk()).andReturn());
        assertThat(accepted.get("items")).hasSize(3);
        assertThat(accepted.at("/items/2/candidate_id").asString()).isEqualTo("cand-open-1");

        // 완료: 수락한 초안이 이 세션의 새 구역이 되고, 같은 자리의 옛 zAi 는 보관된다(같은 개구부가 둘 남지 않는다)
        JsonNode done = body(mvc.perform(complete(id)).andExpect(status().isOk()).andReturn());
        assertThat(done.at("/applied_zones/created")).hasSize(1);
        assertThat(done.at("/applied_zones/archived").toString()).contains(zAi);
        assertThat(done.at("/applied_zones/kept").toString()).contains(zHuman).contains(zOther);
        String newZone = done.at("/applied_zones/created/0").asString();
        assertThat(jdbc.queryForObject("SELECT setup_session_id::text FROM setup_session_zone WHERE zone_id = ?", String.class, newZone))
                .isEqualTo(id);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM zone WHERE site_id = ? AND valid_to IS NULL AND name = '단상 개구부 1'",
                Integer.class, site)).isEqualTo(1);

        // 원 세션의 초안·구역 목록 기록은 바꾸지 않는다
        assertThat(jdbc.queryForObject("SELECT count(*) FROM setup_zone_draft WHERE setup_session_id = ?", Integer.class,
                chosen.session())).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT status FROM setup_zone_draft_run WHERE setup_session_id = ?", String.class,
                chosen.session())).isEqualTo("SUCCEEDED");
        assertThat(sourcePlan(chosen.session())).isEqualTo(sourcePlanBefore);
    }

    @Test
    void 그_맵을_만든_세팅에도_초안이_없으면_재사용_초안은_NOT_REQUESTED_이고_구역은_모두_채운다() throws Exception {
        MapFixture chosen = mapSession("nodraft", Instant.now().minusSeconds(86400), true, "PASSED");
        String zAi = "zone-reuse-nodraft-" + suffix;
        zone(zAi, 1, "POLYGON((0.9 0.9, 3.1 0.9, 3.1 3.1, 0.9 3.1, 0.9 0.9))", true);
        jdbc.update("INSERT INTO setup_session_zone (setup_session_id, zone_id, zone_version, created_at) VALUES (?, ?, 1, now())",
                chosen.session(), zAi);
        ArrayNode sourceItems = mapper.createArrayNode();
        sourceItems.addObject().putNull("zone_id").put("source", "AI").put("candidate_id", "cand-open-1")
                .set("zone", zoneBody("단상 개구부 1", "[[0.9, 0.9], [3.1, 0.9], [3.1, 3.1], [0.9, 3.1]]"));
        jdbc.update("""
                INSERT INTO setup_session_zone_plan (setup_session_id, items, revision, updated_by, updated_at)
                VALUES (?, ?::jsonb, 1, ?, now())
                """, chosen.session(), sourceItems.toString(), userId);
        bundle(chosen.calibration(), List.of(zAi + ":1"), List.of());
        tier0Uses(chosen.calibration());
        view(chosen.scene(), 300, "SAME", 0.0);

        // 새 세션 자신의 조회는 초안 출처가 자기 자신이다
        JsonNode own = body(mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", chosen.session()).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(own.get("draft_source_setup_session_id").asString()).isEqualTo(chosen.session().toString());

        JsonNode created = body(mvc.perform(createReuse(chosen.session())).andExpect(status().isAccepted()).andReturn());
        String id = created.get("setup_session_id").asString();
        JsonNode drafts = body(mvc.perform(get("/api/setup-sessions/{id}/zone-drafts", id).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(drafts.get("status").asString()).isEqualTo("NOT_REQUESTED");
        assertThat(drafts.get("drafts")).isEmpty();
        assertThat(drafts.get("setup_session_id").asString()).isEqualTo(id);
        assertThat(drafts.get("draft_source_setup_session_id").asString()).isEqualTo(chosen.session().toString());
        assertThat(drafts.get("calibration_version").asString()).isEqualTo(chosen.calibration());
        // 다시 보일 초안이 없으니 빼지 않는다(빼면 그 구역을 되살릴 길이 없다)
        JsonNode plan = body(mvc.perform(post("/api/setup-sessions/{id}/zones/seed", id).with(adminJwt()))
                .andExpect(status().isOk()).andReturn());
        assertThat(plan.get("items")).hasSize(1);
        assertThat(plan.at("/items/0/zone_id").asString()).isEqualTo(zAi);
    }

    // ------------------------------------------------------------------ 도우미

    /** 그 세팅의 끝난 초안 작업 하나와 초안 하나(높이 0~1.4 m 개구부) */
    private void draftRun(UUID session, String calibration, String candidateId, String polygonXy) {
        String job = "job-reuse-draft-" + session.toString().substring(0, 8) + "-" + suffix;
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at)"
                + " VALUES (?, 'vlm_zone_draft','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb, now())", job);
        UUID run = UUID.randomUUID();
        jdbc.update("""
                INSERT INTO setup_zone_draft_run (draft_run_id, setup_session_id, job_id, site_id, calibration_version,
                    preview_scene_version, status, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, 'SUCCEEDED', now(), now())
                """, run, session, job, site, calibration, "preview-" + suffix);
        jdbc.update("""
                INSERT INTO setup_zone_draft (draft_id, draft_run_id, setup_session_id, job_id, draft_index, candidate_id,
                    candidate_kind, zone_kind, eval_type, polygon_xy, z_min_m, z_max_m, recommended_name, recommended_severity,
                    rationale, vlm_status)
                VALUES (?, ?, ?, ?, 0, ?, 'OPENING', 'OPENING', 'VOLUME_3D', ?::jsonb, 0, 1.4, '단상 개구부', 'CRITICAL',
                    '단상 둘레가 트여 있어요.', 'VLM')
                """, UUID.randomUUID(), run, session, job, candidateId, polygonXy);
    }

    /** 세팅 구역 목록 항목의 구역 본문(입체 판정 0~1.4 m) */
    private ObjectNode zoneBody(String name, String polygonXy) throws Exception {
        ObjectNode zone = mapper.createObjectNode().put("name", name).put("zone_kind", "OPENING").put("eval_type", "VOLUME_3D")
                .put("z_min_m", 0.0).put("z_max_m", 1.4).put("severity", "CRITICAL");
        zone.set("polygon_xy", mapper.readTree(polygonXy));
        return zone;
    }

    private String sourcePlan(UUID session) {
        return jdbc.queryForObject("SELECT items::text || ':' || revision FROM setup_session_zone_plan WHERE setup_session_id = ?",
                String.class, session);
    }

    /** 지난 새 맵 세팅 하나: 그 세션·맵·보정 */
    private record MapFixture(UUID session, String scene, String calibration) {
    }

    /**
     * 지난 날 새 맵(NEW) 세팅을 만든다: 보정(원문 등록), COMMITTED 맵, 끝난 세팅 세션(높이·카메라 확인), 그 세팅을 건 작업(c17 이면 세팅 완료 기록).
     * checkStatus 가 null 이면 카메라 확인 기록이 없는 옛 세팅이다.
     */
    private MapFixture mapSession(String tag, Instant completedAt, boolean c17, String checkStatus) throws Exception {
        UUID session = UUID.randomUUID();
        String calibration = "cal-reuse-map-" + tag + "-" + suffix;
        String scene = "scene-reuse-map-" + tag + "-" + suffix;
        calibrations.registerFromSetup(site, calibrationDocument(calibration, session.toString(), true), "s3://test/" + calibration,
                null, "재사용 시험 지난 맵", baseCalibration);
        Timestamp at = Timestamp.from(completedAt);
        jdbc.update("""
                INSERT INTO scene_version (scene_version, site_id, calibration_version, state, gltf_key, is_upper_bound,
                    committed_at, valid_from)
                VALUES (?, ?, ?, 'COMMITTED', ?, false, ?, ?)
                """, scene, site, calibration, "scenes/" + scene + ".glb", at, at);
        String job = "job-reuse-map-" + tag + "-" + suffix;
        jdbc.update("INSERT INTO analysis_job(job_id,job_type,priority,status,attempt,max_attempts,timeout_ms,input,created_at)"
                + " VALUES (?, 'site_setup','BACKGROUND','SUCCEEDED',1,1,1000,'{}'::jsonb,?)", job, at);
        String workName = "재사용지난작업-" + tag + "-" + suffix;
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, now(), now() + interval '4 hours', 4, 'PLANNED', ?, now(), now())
                """, site, LocalDate.now(ZoneId.of("Asia/Seoul")).minusDays(2), workName, userId);
        long work = jdbc.queryForObject("SELECT work_id FROM work_unit WHERE name = ?", Long.class, workName);
        String check = checkStatus == null ? null : SetupCameraCheckFixtures.check(mapper, calibration, checkStatus).toString();
        jdbc.update("""
                INSERT INTO setup_session (setup_session_id, site_id, work_unit_id, job_id, status, progress_pct, camera_heights_m,
                    height_tolerance_cm, requested_by, started_at, ready_at, completed_at, updated_at, calibration_version, scene_version,
                    camera_check)
                VALUES (?, ?, ?, ?, 'COMPLETED', 100, '{"cam_A": 2.41, "cam_B": 2.42, "cam_C": 2.47, "cam_D": 2.48}'::jsonb, 5, ?,
                    ?, ?, ?, ?, ?, ?, ?::jsonb)
                """, session, site, work, job, userId, Timestamp.from(completedAt.minusSeconds(900)),
                Timestamp.from(completedAt.minusSeconds(300)), at, at, calibration, scene, check);
        jdbc.update("UPDATE work_unit SET active_setup_session_id = ?, setup_completed_at = ? WHERE work_id = ?", session,
                c17 ? at : null, work);
        // 그 세팅이 등록한 정밀 맵 GLB(맵 켜기가 저장소에 있는지 본다)
        byte[] glb = ("glb-" + scene).getBytes(StandardCharsets.UTF_8);
        objects.put("scenes/" + scene + ".glb", glb);
        jdbc.update("""
                INSERT INTO setup_session_artifact (setup_session_id, kind, version, job_id, lease_id, object_uri, sha256, size_bytes,
                    registered_at)
                VALUES (?, 'SCENE', ?, ?, ?, ?, ?, ?, ?)
                """, session, scene, job, UUID.randomUUID(), BUCKET + "scenes/" + scene + ".glb", sha256Hex(glb), glb.length, at);
        return new MapFixture(session, scene, calibration);
    }

    /** 헬퍼가 이 맵 기준으로 비교한 결과: cam_A~C 는 같은 시점(회전 0), cam_D 는 주어진 상태·회전. 대응 수는 카메라마다 matches/4 */
    private void view(String scene, int matches, String statusD, Double rotationD) {
        ArrayNode cameras = mapper.createArrayNode();
        for (String cam : List.of("cam_A", "cam_B", "cam_C", "cam_D")) {
            boolean d = cam.equals("cam_D");
            String status = d ? statusD : "SAME";
            ObjectNode item = cameras.addObject().put("camera_id", cam).put("status", status)
                    .put("reason", "CHANGED".equals(status) ? "MOVED" : null).put("matches", matches / 4);
            item.putNull("inlier_ratio");
            if (d && rotationD == null) {
                item.putNull("rotation_rad");
            } else {
                item.put("rotation_rad", d ? rotationD : 0.0);
            }
        }
        List<String> changed = "CHANGED".equals(statusD) ? List.of("cam_D") : List.of();
        doReturn(Optional.of(new CoverageGuideService.ViewCheck(changed, "SAME".equals(statusD), cameras, Instant.now())))
                .when(coverageGuides).reuseViewCheck(scene);
    }

    /** Tier 0 가 지금 이 보정을 쓴다고 알렸다(가장 최근 calibration_update) */
    private void tier0Uses(String version) {
        ObjectNode payload = mapper.createObjectNode().put("calibration_version", version).put("calibration_state", "CALIBRATED");
        jdbc.update("""
                INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason,
                    sent_at, received_at, payload)
                VALUES (?, ?, ?, 'CALIBRATED', 'RECALIBRATED', now() + interval '3 hours', now() + interval '3 hours', ?::jsonb)
                """, UUID.randomUUID(), "tier0-reuse-" + suffix, version, payload.toString());
    }

    private void zone(String id, int version, String wkt, boolean active) {
        jdbc.update("""
                INSERT INTO zone (zone_id, zone_version, site_id, name, zone_kind, eval_type, polygon, severity, valid_from, valid_to)
                VALUES (?, ?, ?, ?, 'HAZARD', 'GROUND_FOOTPRINT', ST_GeomFromText(?, 0), 'HIGH', now() - interval '3 days',
                    CASE WHEN ? THEN NULL ELSE now() END)
                """, id, version, site, "재사용 구역 " + id, wkt, active);
    }

    private void surface(String id, int version, String zoneId, boolean active) {
        jdbc.update("""
                INSERT INTO static_object (object_id, object_version, site_id, kind, obb, top_height_m, name, zone_id, valid_from, valid_to)
                VALUES (?, ?, ?, 'WORK_SURFACE', '{"center": [1, 1, 0.45], "size": [1, 1, 0.9], "yaw_rad": 0}'::jsonb, 0.9, '작업대',
                    ?, now() - interval '3 days', CASE WHEN ? THEN NULL ELSE now() END)
                """, id, version, site, zoneId, active);
    }

    /** 이 보정으로 발행한 묶음(구역·작업면 버전 "id:버전") */
    private void bundle(String calibration, List<String> zones, List<String> objects) {
        String version = "cfg-reuse-" + calibration;
        jdbc.update("""
                INSERT INTO config_bundle (config_version, site_id, issued_at, bundle_uri, bundle_hash, components, calibration_version,
                    rule_profile)
                VALUES (?, ?, now() - interval '2 days', 'https://test/bundle', 'sha256:test', '{}'::jsonb, ?, 'OPERATION')
                """, version, site, calibration);
        for (String z : zones) {
            String[] parts = z.split(":");
            jdbc.update("INSERT INTO config_bundle_item (config_version, item_type, item_id, item_version) VALUES (?, 'ZONE', ?, ?)",
                    version, parts[0], Integer.parseInt(parts[1]));
        }
        for (String o : objects) {
            String[] parts = o.split(":");
            jdbc.update("INSERT INTO config_bundle_item (config_version, item_type, item_id, item_version) VALUES (?, 'STATIC_OBJECT', ?, ?)",
                    version, parts[0], Integer.parseInt(parts[1]));
        }
    }

    private static JsonNode find(JsonNode candidates, UUID session) {
        for (JsonNode c : candidates) {
            if (session.toString().equals(c.get("setup_session_id").asString())) {
                return c;
            }
        }
        throw new AssertionError("후보 없음: " + session);
    }

    private RequestBuilder createReuse(UUID chosen) {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId).put("mode", "REUSE");
        if (chosen != null) {
            req.put("reuse_setup_session_id", chosen.toString());
        }
        return post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(req.toString());
    }

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

    private RequestBuilder create(String mode) {
        ObjectNode req = mapper.createObjectNode().put("work_unit_id", workUnitId);
        req.putObject("camera_heights_m").put("cam_A", 2.44).put("cam_B", 2.44).put("cam_C", 2.48).put("cam_D", 2.48);
        if (mode != null) {
            req.put("mode", mode);
        }
        return post("/api/setup-sessions").with(adminJwt()).contentType(MediaType.APPLICATION_JSON).content(req.toString());
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
