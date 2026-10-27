package com.ssafy.simai.domain.job;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.ResultActions;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 분석 워커 객체 서명(S15P31A101-248). 대여 묶음(만료·남의 대여·다른 작업·끝난 대여·취소), GET 은 input 안의 주소만,
 * PUT 키는 EC2 가 정한 작업 전용 앞부분. 서명은 계산만 하므로 저장소 없이 주소 모양을 본다(application-test.yaml 버킷).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
@Transactional
class WorkerObjectSignIntegrationTest {

    private static final String BUCKET = "s3://simai-test-bucket/";
    private static final String CALIBRATION = BUCKET + "calibrations/v7/calibration.json";
    private static final String FRAME = BUCKET + "event-frames/e1/cam_A.jpg";
    private static final String SHA = "d".repeat(64);

    @Autowired
    MockMvc mvc;

    @Autowired
    MachineTokenService machines;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    private final String tag = UUID.randomUUID().toString().substring(0, 8);
    private final String workerA = "obj-a-" + tag;
    private final String workerB = "obj-b-" + tag;
    private String tokenA;
    private String tokenB;

    @BeforeEach
    void workers() {
        tokenA = machines.registerWorker(workerA, List.of("bev_floorplan", "vlm_rationale")).token();
        tokenB = machines.registerWorker(workerB, List.of("bev_floorplan", "vlm_rationale")).token();
    }

    @Test
    void 유효한_대여면_input_안의_주소를_GET_으로_서명하고_결과는_작업_전용_키로_PUT_서명한다() throws Exception {
        String jobId = job("bev_floorplan", "LEASED");
        UUID lease = lease(jobId, workerA, "ACTIVE", 30);

        JsonNode get = ok(sign(tokenA, jobId, Map.of("lease_id", lease, "op", "GET", "uri", CALIBRATION)));
        assertThat(get.get("method").stringValue()).isEqualTo("GET");
        assertThat(get.get("uri").stringValue()).isEqualTo(CALIBRATION);
        assertThat(get.get("url").stringValue())
                .startsWith("http://127.0.0.1:9000/simai-test-bucket/calibrations/v7/calibration.json?")
                .contains("X-Amz-Expires=300", "X-Amz-Signature=");
        // 배열 안 깊은 값(frames[].frame_uri)도 input 에 있는 주소다
        ok(sign(tokenA, jobId, Map.of("lease_id", lease, "op", "get", "uri", FRAME)));

        JsonNode put = ok(sign(tokenA, jobId, Map.of("lease_id", lease, "op", "PUT", "name", "bev.png",
                "content_type", "image/png", "size_bytes", 2048, "sha256", SHA)));
        // 대여마다 다른 키(옛 대여의 늦은 업로드가 새 시도 결과를 덮어쓰지 않게)
        String key = "floor-plans/" + jobId + "/" + lease + "/bev.png";
        assertThat(put.get("uri").stringValue()).isEqualTo(BUCKET + key);
        assertThat(put.get("url").stringValue()).startsWith("http://127.0.0.1:9000/simai-test-bucket/" + key + "?");
        Map<String, String> headers = mapper.convertValue(put.get("headers"), Map.class);
        assertThat(headers).containsEntry("content-type", "image/png").containsEntry("content-length", "2048")
                .containsKey("x-amz-checksum-sha256");

        // 앞부분을 정하지 않은 유형은 job-objects/{job_type}
        String vlm = job("vlm_rationale", "LEASED");
        UUID vlmLease = lease(vlm, workerA, "ACTIVE", 30);
        assertThat(ok(sign(tokenA, vlm, Map.of("lease_id", vlmLease, "op", "PUT", "name", "notes.json",
                "content_type", "application/json", "size_bytes", 10, "sha256", SHA))).get("uri").stringValue())
                .isEqualTo(BUCKET + "job-objects/vlm_rationale/" + vlm + "/" + vlmLease + "/notes.json");
    }

    @Test
    void 만료_남의_대여_다른_작업_끝난_대여_취소된_작업이면_409() throws Exception {
        String jobId = job("bev_floorplan", "LEASED");
        UUID active = lease(jobId, workerA, "ACTIVE", 30);
        Map<String, Object> get = Map.of("lease_id", active, "op", "GET", "uri", CALIBRATION);

        // 다른 워커가 같은 lease_id 를 써도
        conflict(sign(tokenB, jobId, get));
        // 다른 작업 경로로
        String other = job("bev_floorplan", "LEASED");
        lease(other, workerA, "ACTIVE", 30);
        conflict(sign(tokenA, other, get));
        // 없는 대여
        conflict(sign(tokenA, jobId, Map.of("lease_id", UUID.randomUUID(), "op", "GET", "uri", CALIBRATION)));

        // 만료 시각이 지난 대여(스케줄러가 아직 EXPIRED 로 바꾸기 전)
        String expired = job("bev_floorplan", "LEASED");
        UUID old = lease(expired, workerA, "ACTIVE", -1);
        conflict(sign(tokenA, expired, Map.of("lease_id", old, "op", "GET", "uri", CALIBRATION)));

        // 제출을 마친 대여
        String done = job("bev_floorplan", "SUCCEEDED");
        UUID submitted = lease(done, workerA, "SUBMITTED", 30);
        conflict(sign(tokenA, done, Map.of("lease_id", submitted, "op", "GET", "uri", CALIBRATION)));

        // 대여 중 취소된 작업
        jdbc.update("UPDATE analysis_job SET status = 'CANCELLED' WHERE job_id = ?", jobId);
        conflict(sign(tokenA, jobId, get));
    }

    @Test
    void input_에_없는_주소_GET_과_잘못된_PUT_은_거부한다() throws Exception {
        String jobId = job("bev_floorplan", "LEASED");
        UUID lease = lease(jobId, workerA, "ACTIVE", 30);
        for (String uri : List.of(BUCKET + "calibrations/v8/calibration.json", "s3://other-bucket/calibrations/v7/calibration.json",
                BUCKET, CALIBRATION + "?x=1", "calibrations/v7/calibration.json")) {
            sign(tokenA, jobId, Map.of("lease_id", lease, "op", "GET", "uri", uri)).andExpect(status().isForbidden())
                    .andExpect(jsonPath("$.code").value("OBJECT_NOT_IN_INPUT"));
        }

        Map<String, Object> put = new HashMap<>(Map.of("lease_id", lease, "op", "PUT", "name", "bev.png",
                "content_type", "image/png", "size_bytes", 10, "sha256", SHA));
        for (Map.Entry<String, Object> bad : List.<Map.Entry<String, Object>>of(Map.entry("name", "../bev.png"),
                Map.entry("name", "plans/bev.png"), Map.entry("name", ".hidden"), Map.entry("name", "a..b"),
                Map.entry("content_type", "png"), Map.entry("size_bytes", 0), Map.entry("size_bytes", (512L << 20) + 1),
                Map.entry("sha256", "xyz"), Map.entry("op", "DELETE"))) {
            Map<String, Object> request = new HashMap<>(put);
            request.put(bad.getKey(), bad.getValue());
            sign(tokenA, jobId, request).andExpect(status().isBadRequest())
                    .andExpect(jsonPath("$.code").value("INVALID_OBJECT_REQUEST"));
        }
    }

    @Test
    void 워커_토큰이_아니면_401() throws Exception {
        String jobId = job("bev_floorplan", "LEASED");
        UUID lease = lease(jobId, workerA, "ACTIVE", 30);
        Map<String, Object> get = Map.of("lease_id", lease, "op", "GET", "uri", CALIBRATION);
        sign(null, jobId, get).andExpect(status().isUnauthorized());
        sign("not-a-token", jobId, get).andExpect(status().isUnauthorized());
        String device = machines.registerDevice("obj-dev-" + tag, "LAPTOP").token();
        sign(device, jobId, get).andExpect(status().isUnauthorized());
    }

    private String job(String type, String status) {
        String jobId = UUID.randomUUID().toString();
        Map<String, Object> input = Map.of("calibration_version", "v7", "calibration_ref", CALIBRATION,
                "calibration_hash", SHA, "frame_source", "LIVE",
                "frames", List.of(Map.of("camera_id", "cam_A", "frame_uri", FRAME)));
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, input, created_at)
                VALUES (?, ?, 'BACKGROUND', ?, 1, 2, 600000, CAST(? AS jsonb), now())
                """, jobId, type, status, mapper.writeValueAsString(input));
        return jobId;
    }

    private UUID lease(String jobId, String workerId, String status, int expiresInSeconds) {
        UUID leaseId = UUID.randomUUID();
        jdbc.update("""
                INSERT INTO job_lease (lease_id, job_id, worker_id, attempt, leased_at, lease_expires_at, heartbeat_interval_ms, status)
                VALUES (?, ?, ?, 1, now() - interval '5 seconds', now() + make_interval(secs => ?), 10000, ?)
                """, leaseId, jobId, workerId, expiresInSeconds, status);
        return leaseId;
    }

    private ResultActions sign(String token, String jobId, Map<String, Object> body) throws Exception {
        var request = post("/api/worker/jobs/{jobId}/objects/sign", jobId).contentType(MediaType.APPLICATION_JSON)
                .content(mapper.writeValueAsString(body));
        if (token != null) {
            request.header(HttpHeaders.AUTHORIZATION, "Bearer " + token);
        }
        return mvc.perform(request);
    }

    private JsonNode ok(ResultActions result) throws Exception {
        return mapper.readTree(result.andExpect(status().isOk()).andReturn().getResponse()
                .getContentAsString(StandardCharsets.UTF_8));
    }

    private static void conflict(ResultActions result) throws Exception {
        result.andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("LEASE_NOT_ACTIVE"));
    }
}
