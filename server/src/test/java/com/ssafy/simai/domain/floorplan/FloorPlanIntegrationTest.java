package com.ssafy.simai.domain.floorplan;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.hamcrest.Matchers.startsWith;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.floorplan.FloorPlanService.Frame;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.config.JwtConfig;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.security.AccessTokenIssuer;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.Arrays;
import java.util.List;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.http.HttpHeaders;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.security.oauth2.jose.jws.MacAlgorithm;
import org.springframework.security.oauth2.jwt.JwsHeader;
import org.springframework.security.oauth2.jwt.JwtClaimsSet;
import org.springframework.security.oauth2.jwt.JwtEncoder;
import org.springframework.security.oauth2.jwt.JwtEncoderParameters;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * BEV 평면도 작업 등록·결과 저장·조회(S15P31A101-240). 주기 대조는 시험 설정에서 1시간이라 대조를 직접 부른다.
 * 작업 등록은 AnalysisJobService 가 계약(job_request + bev_floorplan input)으로 검사하므로, 등록되면 입력이 계약에 맞는 것이다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class FloorPlanIntegrationTest {

    private static final String BUCKET = "s3://simai-test-bucket/";
    /** 계약 sha256 형식(sha256:16진수) */
    private static final String HASH = "sha256:" + "e".repeat(64);

    @Autowired
    MockMvc mvc;

    @Autowired
    FloorPlanService floorPlans;

    @Autowired
    AnalysisJobService jobs;

    @Autowired
    ApplicationEventPublisher events;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    JwtEncoder jwtEncoder;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private String site;
    /** 다른 시험이 남긴 보정 알림보다 늦게 보이도록 미래 시각으로 넣는다(현재 보정 = 가장 늦은 알림) */
    private int minutesAhead = 60;

    @BeforeEach
    void site() {
        site = jdbc.queryForObject("SELECT site_id FROM site ORDER BY site_id LIMIT 1", String.class);
    }

    @Test
    void 프레임과_CALIBRATED_보정이_모두_있을_때_작업을_한_번_넣고_결과가_오면_READY_로_조회된다() throws Exception {
        String version = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + ".json");
        mvc.perform(get("/api/floor-plans/current").header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER")))
                .andExpect(status().isNotFound()).andExpect(jsonPath("$.code").value("FLOOR_PLAN_NOT_READY"));
        // 프레임이 없으면 넣지 않는다
        assertThat(floorPlans.reconcile()).isEmpty();

        FloorPlanService.Registered registered = floorPlans.registerFrames("LIVE", frames("cam_A", "cam_B"));
        String jobId = registered.jobId().orElseThrow();
        JsonNode input = mapper.readTree(jdbc.queryForObject("SELECT input::text FROM analysis_job WHERE job_id = ?", String.class, jobId));
        assertThat(input.get("calibration_version").stringValue()).isEqualTo(version);
        assertThat(input.get("calibration_ref").stringValue()).isEqualTo(BUCKET + "calibrations/" + tag + ".json");
        assertThat(input.get("calibration_hash").stringValue()).isEqualTo(HASH);
        assertThat(input.get("frame_source").stringValue()).isEqualTo("LIVE");
        assertThat(input.get("frames")).hasSize(2);
        assertThat(jdbc.queryForObject("SELECT job_type || '/' || priority FROM analysis_job WHERE job_id = ?", String.class, jobId))
                .isEqualTo("bev_floorplan/BACKGROUND");
        // 같은 보정·같은 묶음은 다시 넣지 않는다
        assertThat(floorPlans.reconcile()).isEmpty();
        assertThat(plans(version)).containsExactly("PENDING");

        events.publishEvent(new JobFinished(jobId, "bev_floorplan", "SUCCEEDED", result(version, "floor-plans/" + jobId + "/l/bev.png"),
                null));
        assertThat(plans(version)).containsExactly("READY");
        assertThat(jdbc.queryForObject("SELECT current_floor_plan_id FROM site WHERE site_id = ?", Long.class, site))
                .isEqualTo(jdbc.queryForObject("SELECT floor_plan_id FROM floor_plan WHERE job_id = ?", Long.class, jobId));

        mvc.perform(get("/api/floor-plans/current").header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("VIEWER")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.calibration_version").value(version))
                .andExpect(jsonPath("$.stale").value(false))
                .andExpect(jsonPath("$.pending").value(false))
                .andExpect(jsonPath("$.purpose").value("OVERVIEW_DISPLAY_ONLY"))
                .andExpect(jsonPath("$.safety_use").value(false))
                .andExpect(jsonPath("$.raster.meters_per_pixel").value(0.02))
                .andExpect(jsonPath("$.image_url").value(startsWith(
                        "http://127.0.0.1:9000/simai-test-bucket/floor-plans/" + jobId + "/l/bev.png?")));
        mvc.perform(get("/api/floor-plans/current")).andExpect(status().isUnauthorized());
    }

    @Test
    void 보정_버전이_바뀌면_새_작업을_넣고_그동안은_이전_평면도를_stale_로_준다() throws Exception {
        String first = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + "-1.json");
        String firstJob = floorPlans.registerFrames("LIVE", frames("cam_A")).jobId().orElseThrow();
        events.publishEvent(new JobFinished(firstJob, "bev_floorplan", "SUCCEEDED", result(first, "floor-plans/" + firstJob + "/l/bev.png"),
                null));

        // 아직 CALIBRATED 가 아닌 새 버전에서는 넣지 않는다
        String second = calibration("PROVISIONAL", BUCKET + "calibrations/" + tag + "-2.json");
        assertThat(floorPlans.reconcile()).isEmpty();
        String third = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + "-3.json");
        String thirdJob = floorPlans.reconcile().orElseThrow();
        assertThat(plans(second)).isEmpty();
        assertThat(plans(third)).containsExactly("PENDING");

        mvc.perform(get("/api/floor-plans/current").header(HttpHeaders.AUTHORIZATION, "Bearer " + jwt("FOREMAN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.calibration_version").value(first))
                .andExpect(jsonPath("$.current_calibration_version").value(third))
                .andExpect(jsonPath("$.stale").value(true))
                .andExpect(jsonPath("$.pending").value(true));

        // 실패·취소는 FAILED 로 남고 같은 조합은 다시 넣지 않는다
        jobs.cancel(thirdJob);
        assertThat(plans(third)).containsExactly("FAILED");
        assertThat(jdbc.queryForObject("SELECT error FROM floor_plan WHERE job_id = ?", String.class, thirdJob)).isEqualTo("CANCELLED");
        assertThat(floorPlans.reconcile()).isEmpty();
        // 프레임을 새로 등록하면 다시 넣는다
        assertThat(floorPlans.registerFrames("LIVE", frames("cam_A")).jobId()).isPresent();
    }

    @Test
    void 보정_문서가_버킷_밖이면_넣지_않고_고치면_다음_대조에서_넣는다() {
        String version = calibration("CALIBRATED", "s3://other-bucket/calibrations/" + tag + ".json");
        assertThat(floorPlans.registerFrames("LIVE", frames("cam_A")).jobId()).isEmpty();
        assertThat(jdbc.queryForObject("SELECT status || '/' || error FROM floor_plan WHERE calibration_version = ?", String.class, version))
                .isEqualTo("FAILED/CALIBRATION_REF_NOT_IN_BUCKET");
        // 같은 이유면 행을 늘리지 않는다
        assertThat(floorPlans.reconcile()).isEmpty();
        assertThat(plans(version)).containsExactly("FAILED");

        // 설정(주소)을 고치면 다음 대조가 넣는다(넣지 못한 실패는 막지 않는다)
        jdbc.update("""
                UPDATE calibration_update_message SET payload = jsonb_set(payload, '{calibration_ref}', to_jsonb(CAST(? AS text)))
                WHERE calibration_version = ?
                """, BUCKET + "calibrations/" + tag + ".json", version);
        assertThat(floorPlans.reconcile()).isPresent();
        assertThat(plans(version)).containsExactly("PENDING");
    }

    @Test
    void 결과_이미지가_작업_출력_자리_밖이면_FAILED_이고_워커_실패_코드를_남긴다() {
        String version = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + ".json");
        String jobId = floorPlans.registerFrames("LIVE", frames("cam_A")).jobId().orElseThrow();
        // 버킷 안이라도 이 작업의 출력(floor-plans/{job_id}/…)이 아니면 받지 않는다(조회가 열람 권한으로 서명하므로)
        events.publishEvent(new JobFinished(jobId, "bev_floorplan", "SUCCEEDED", result(version, "event-clips/e/c.h264"), null));
        assertThat(jdbc.queryForObject("SELECT status || '/' || error FROM floor_plan WHERE job_id = ?", String.class, jobId))
                .isEqualTo("FAILED/IMAGE_NOT_IN_JOB_OUTPUT");
        // 다른 유형의 작업 종료는 건드리지 않는다
        events.publishEvent(new JobFinished(jobId, "tbm_report", "SUCCEEDED", null, null));
        assertThat(jdbc.queryForObject("SELECT status FROM floor_plan WHERE job_id = ?", String.class, jobId)).isEqualTo("FAILED");

        String other = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + "-2.json");
        String failed = floorPlans.reconcile().orElseThrow();
        events.publishEvent(new JobFinished(failed, "bev_floorplan", "FAILED", null,
                mapper.createObjectNode().put("code", "CALIBRATION_HASH_MISMATCH").put("message", "x")));
        assertThat(jdbc.queryForObject("SELECT error FROM floor_plan WHERE job_id = ?", String.class, failed))
                .isEqualTo("CALIBRATION_HASH_MISMATCH");
        assertThat(plans(other)).containsExactly("FAILED");
    }

    @Test
    void 새_보정_버전_작업을_넣으면_옛_버전_작업은_취소하고_늦게_끝난_옛_결과는_현재_평면도를_덮지_않는다() {
        String oldVersion = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + "-old.json");
        String oldJob = floorPlans.registerFrames("LIVE", frames("cam_A")).jobId().orElseThrow();
        String newVersion = calibration("CALIBRATED", BUCKET + "calibrations/" + tag + "-new.json");
        String newJob = floorPlans.reconcile().orElseThrow();
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, oldJob)).isEqualTo("CANCELLED");
        assertThat(jdbc.queryForObject("SELECT status || '/' || error FROM floor_plan WHERE job_id = ?", String.class, oldJob))
                .isEqualTo("FAILED/CANCELLED");

        events.publishEvent(new JobFinished(newJob, "bev_floorplan", "SUCCEEDED",
                result(newVersion, "floor-plans/" + newJob + "/l/bev.png"), null));
        long current = jdbc.queryForObject("SELECT current_floor_plan_id FROM site WHERE site_id = ?", Long.class, site);
        // 옛 버전 평면도가 어떤 길로 늦게 READY 가 돼도 현재 평면도로 두지 않는다
        jdbc.update("UPDATE floor_plan SET status = 'PENDING', error = NULL WHERE job_id = ?", oldJob);
        events.publishEvent(new JobFinished(oldJob, "bev_floorplan", "SUCCEEDED",
                result(oldVersion, "floor-plans/" + oldJob + "/l/bev.png"), null));
        assertThat(jdbc.queryForObject("SELECT current_floor_plan_id FROM site WHERE site_id = ?", Long.class, site)).isEqualTo(current);
    }

    @Test
    void 프레임은_버킷_안_주소와_카메라마다_한_장만_받고_관리_명령으로_등록한다() {
        for (List<Frame> bad : List.of(frames(), List.of(frame("cam_A", "s3://other-bucket/a.jpg")),
                List.of(frame("cam_A", BUCKET + "a.jpg"), frame("cam_A", BUCKET + "b.jpg")),
                List.of(frame("cam A", BUCKET + "a.jpg")), List.of(frame("cam_A", "https://x/a.jpg")),
                List.of(frame("cam_A", BUCKET + "k".repeat(1100))),
                List.of(new Frame("cam_A", BUCKET + "a.jpg", -1, "RECEIVE_ESTIMATED", 1000)))) {
            assertThatThrownBy(() -> floorPlans.registerFrames("LIVE", bad)).isInstanceOf(BusinessException.class);
        }
        assertThatThrownBy(() -> floorPlans.registerFrames("CAMERA", frames("cam_A"))).isInstanceOf(BusinessException.class);

        calibration("CALIBRATED", BUCKET + "calibrations/" + tag + ".json");
        String out = run("floorplan", "frames", "--frames", "cam_A=" + BUCKET + "bev/" + tag + "/cam_A.jpg,cam_B=" + BUCKET
                + "bev/" + tag + "/cam_B.jpg", "--source", "FILE", "--captured-at", "1760342400000");
        assertThat(out).contains("프레임 묶음을 등록했다", "카메라 2대", "평면도 작업을 넣었다: job-");
        JsonNode frames = mapper.readTree(jdbc.queryForObject(
                "SELECT frames::text FROM bev_frame_set ORDER BY frame_set_id DESC LIMIT 1", String.class));
        assertThat(frames.get(0).get("timestamp_source").stringValue()).isEqualTo("FILE");
        assertThat(frames.get(0).get("estimated_capture_time_ms").longValue()).isEqualTo(1760342400000L);
        assertThat(run("floorplan", "reconcile")).contains("넣을 평면도 작업이 없다");
        assertThat(run("floorplan", "frames", "--frames", "cam_A=s3://other-bucket/a.jpg")).contains("프레임을 등록하지 못했다");
    }

    /** 보정 버전 행과 그 버전의 calibration_update 알림 하나. @return 보정 버전 */
    private String calibration(String state, String ref) {
        String version = "cal-" + tag + "-" + minutesAhead;
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, ?, 'G1_AUTO', 'INITIAL', now(), ?, ?, now())
                """, version, site, state, ref, HASH);
        ObjectNode payload = mapper.createObjectNode().put("calibration_version", version).put("calibration_state", state)
                .put("calibration_ref", ref).put("calibration_hash", HASH);
        jdbc.update("""
                INSERT INTO calibration_update_message (msg_id, gateway_id, calibration_version, calibration_state, reason, sent_at,
                    received_at, payload)
                VALUES (?, 'tier0-test', ?, ?, 'INITIAL', now() + make_interval(mins => ?), now() + make_interval(mins => ?),
                    CAST(? AS jsonb))
                """, UUID.randomUUID(), version, state, minutesAhead, minutesAhead, payload.toString());
        minutesAhead++;
        return version;
    }

    private List<String> plans(String version) {
        return jdbc.queryForList("SELECT status FROM floor_plan WHERE calibration_version = ? ORDER BY floor_plan_id", String.class,
                version);
    }

    private List<Frame> frames(String... cameras) {
        return Arrays.stream(cameras).map(c -> frame(c, BUCKET + "bev/" + tag + "/" + c + ".jpg")).toList();
    }

    private static Frame frame(String camera, String uri) {
        return new Frame(camera, uri, 1760342400000L, "RECEIVE_ESTIMATED", 1000);
    }

    /** 계약 bev_floorplan result 의 조회에 쓰는 칸 */
    private ObjectNode result(String version, String imageKey) {
        ObjectNode result = mapper.createObjectNode().put("image_uri", BUCKET + imageKey).put("image_sha256", HASH)
                .put("content_type", "image/png").put("metadata_uri", BUCKET + imageKey + ".json")
                .put("calibration_version", version).putNull("scene_version").put("created_at", 1760342400000L)
                .put("purpose", "OVERVIEW_DISPLAY_ONLY").put("safety_use", false).put("coverage_ratio", 0.6);
        result.putObject("raster").put("width", 100).put("height", 80).put("meters_per_pixel", 0.02);
        result.putArray("cameras_used").add("cam_A");
        result.putObject("cameras_skipped");
        result.putNull("room_outline");
        return result;
    }

    private String jwt(String role) {
        Instant now = Instant.now();
        JwtClaimsSet claims = JwtClaimsSet.builder().issuer(JwtConfig.ISSUER).subject("1").issuedAt(now)
                .expiresAt(now.plusSeconds(300)).claim(AccessTokenIssuer.ROLE_CLAIM, role).build();
        return jwtEncoder.encode(JwtEncoderParameters.from(JwsHeader.with(MacAlgorithm.HS256).build(), claims)).getTokenValue();
    }

    private String run(String... args) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        commands.execute(List.of(args), new CommandIo(print, print, prompt -> null));
        return out.toString(StandardCharsets.UTF_8);
    }
}
