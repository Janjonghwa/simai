package com.ssafy.simai.domain.scene;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
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
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.RequestBuilder;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 배경 묶음 static_reconstruct 자동 등록(S15P31A101-403): 같은 묶음은 작업 하나(멱등·동시 요청), 워커 토큰만, 계약·보정 검사.
 * 응답은 contracts/ static_reconstruct_submit_result 로 검사한다. 시험 트랜잭션 없이 커밋하고 이 시험이 만든 행만 지운다.
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class WorkerStaticReconstructSubmitIntegrationTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final String PATH = "/api/worker/scene-jobs/static-reconstruct";

    @Autowired
    MockMvc mvc;

    @Autowired
    MachineTokenService machines;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    private final String worker = "worker-s-" + UUID.randomUUID().toString().substring(0, 8);
    private final String calibrated = "cal-403-" + UUID.randomUUID();
    private final String drifted = "cal-403-drift-" + UUID.randomUUID();
    private final List<String> bundles = new ArrayList<>();
    private String token;

    @BeforeEach
    void setUp() {
        String site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        for (String[] row : new String[][] {{calibrated, "CALIBRATED"}, {drifted, "DRIFTED"}}) {
            jdbc.update("""
                    INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                        calibration_ref, calibration_hash, created_at)
                    VALUES (?, ?, ?, 'G1_AUTO', 'submit test', now(), 's3://test/cal.json', ?, now())
                    """, row[0], site, row[1], "sha256:" + "b".repeat(64));
        }
        token = machines.registerWorker(worker, List.of("static_reconstruct")).token();
    }

    @AfterEach
    void tearDown() {
        for (String bundle : bundles) {
            jdbc.update("DELETE FROM analysis_job WHERE input->>'clean_background_bundle_id' = ?", bundle);
        }
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version IN (?, ?)", calibrated, drifted);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void 새_묶음은_작업을_하나_만들고_같은_묶음은_같은_작업을_돌려준다() throws Exception {
        String bundle = newBundle();
        JsonNode first = body(mvc.perform(submit(token, worker, bundle, input(calibrated))).andExpect(status().isCreated()).andReturn());
        assertThat(schemas.violations("messages/static_reconstruct_submit_result", first.toString())).isEmpty();
        JsonNode p = first.get("payload");
        assertThat(p.get("created").booleanValue()).isTrue();
        assertThat(p.get("job_status").stringValue()).isEqualTo("QUEUED");
        assertThat(p.get("bundle_id").stringValue()).isEqualTo(bundle);
        assertThat(p.has("job_error_code")).isFalse();
        String jobId = p.get("job_id").stringValue();

        // 등록한 작업은 BACKGROUND·세션 도구와 같은 상한이고 input 에 묶음 ID 가 실린다
        JsonNode stored = mapper.readTree(jdbc.queryForObject("SELECT input::text FROM analysis_job WHERE job_id = ?", String.class, jobId));
        assertThat(stored.get(StaticReconstructSubmitService.BUNDLE_KEY).stringValue()).isEqualTo(bundle);
        assertThat(stored.get("calibration_version").stringValue()).isEqualTo(calibrated);
        assertThat(jdbc.queryForObject("SELECT priority || '/' || max_attempts || '/' || timeout_ms FROM analysis_job WHERE job_id = ?",
                String.class, jobId)).isEqualTo("BACKGROUND/2/4200000");

        // 응답을 잃은 재전송·재시작 뒤 다시 올린 사진(다른 주소)도 같은 작업을 돌려준다(input 을 보지 않는다)
        ObjectNode reuploaded = input(calibrated);
        ((ObjectNode) reuploaded.get("images").get(0)).put("uri", "s3://simai-test-bucket/avatars/other.jpg");
        JsonNode again = body(mvc.perform(submit(token, worker, bundle, reuploaded)).andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/static_reconstruct_submit_result", again.toString())).isEmpty();
        assertThat(again.at("/payload/job_id").stringValue()).isEqualTo(jobId);
        assertThat(again.at("/payload/created").booleanValue()).isFalse();
        assertThat(count(bundle)).isEqualTo(1);

        // 끝난 작업이면 그 상태와 실패 코드를 돌려준다(새 작업을 만들지 않는다)
        jdbc.update("UPDATE analysis_job SET status = 'FAILED', error = '{\"code\":\"STATIC_BAKE_EMPTY\"}'::jsonb, finished_at = now()"
                + " WHERE job_id = ?", jobId);
        JsonNode failed = body(mvc.perform(submit(token, worker, bundle, input(calibrated))).andExpect(status().isOk()).andReturn());
        assertThat(failed.at("/payload/job_status").stringValue()).isEqualTo("FAILED");
        assertThat(failed.at("/payload/job_error_code").stringValue()).isEqualTo("STATIC_BAKE_EMPTY");
        assertThat(schemas.violations("messages/static_reconstruct_submit_result", failed.toString())).isEmpty();
        assertThat(count(bundle)).isEqualTo(1);
    }

    @Test
    void 같은_묶음을_동시에_보내도_작업은_하나다() throws Exception {
        String bundle = newBundle();
        ExecutorService pool = Executors.newFixedThreadPool(4);
        Set<String> jobIds = new HashSet<>();
        int created = 0;
        try {
            List<Future<MvcResult>> futures = new ArrayList<>();
            for (int i = 0; i < 4; i++) {
                futures.add(pool.submit(() -> mvc.perform(submit(token, worker, bundle, input(calibrated))).andReturn()));
            }
            for (Future<MvcResult> f : futures) {
                JsonNode p = body(f.get(30, TimeUnit.SECONDS)).get("payload");
                jobIds.add(p.get("job_id").stringValue());
                created += p.get("created").booleanValue() ? 1 : 0;
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(jobIds).hasSize(1);
        assertThat(created).isEqualTo(1);
        assertThat(count(bundle)).isEqualTo(1);
    }

    @Test
    void 워커_토큰만_받고_봉투_워커_ID_가_다르면_401() throws Exception {
        String bundle = newBundle();
        mvc.perform(submit("not-a-token", worker, bundle, input(calibrated))).andExpect(status().isUnauthorized());
        mvc.perform(post(PATH).contentType(MediaType.APPLICATION_JSON)
                .content(envelope(worker, payload(bundle, input(calibrated))).toString())).andExpect(status().isUnauthorized());
        mvc.perform(submit(token, "worker-other", bundle, input(calibrated)))
                .andExpect(status().isUnauthorized()).andExpect(jsonPath("$.code").value("WORKER_SOURCE_MISMATCH"));
        assertThat(count(bundle)).isZero();
    }

    @Test
    void 계약_위반은_400_등록되지_않았거나_DRIFTED_보정은_409() throws Exception {
        String bundle = newBundle();
        // 묶음 ID 형식·이미지 2장 미만·input 의 묶음 ID 불일치·다른 메이저
        mvc.perform(submit(token, worker, "../bundle-x", input(calibrated)))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_WORKER_MESSAGE"));
        ObjectNode oneImage = input(calibrated);
        ArrayNode images = (ArrayNode) oneImage.get("images");
        while (images.size() > 1) {
            images.remove(images.size() - 1);
        }
        mvc.perform(submit(token, worker, bundle, oneImage)).andExpect(status().isBadRequest());
        ObjectNode otherBundle = input(calibrated).put(StaticReconstructSubmitService.BUNDLE_KEY, newBundle());
        mvc.perform(submit(token, worker, bundle, otherBundle)).andExpect(status().isBadRequest());
        ObjectNode v2 = envelope(worker, payload(bundle, input(calibrated))).put("schema_version", "2.0.0");
        mvc.perform(post(PATH).header(HttpHeaders.AUTHORIZATION, "Bearer " + token).contentType(MediaType.APPLICATION_JSON)
                .content(v2.toString())).andExpect(status().isBadRequest());

        // 보정: 등록되지 않은 버전, DRIFTED (Scene 등록 규칙과 같은 PROVISIONAL·CALIBRATED 만)
        mvc.perform(submit(token, worker, bundle, input("cal-403-unknown-" + UUID.randomUUID())))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SCENE_CALIBRATION_INVALID"));
        mvc.perform(submit(token, worker, bundle, input(drifted)))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.code").value("SCENE_CALIBRATION_INVALID"));
        assertThat(count(bundle)).isZero();

        // 거부 뒤 고친 요청은 새 작업으로 받는다(거부는 묶음을 막지 않는다)
        mvc.perform(submit(token, worker, bundle, input(calibrated))).andExpect(status().isCreated());
        assertThat(count(bundle)).isEqualTo(1);
    }

    private String newBundle() {
        String bundle = "bundle-" + UUID.randomUUID().toString().replace("-", "");
        bundles.add(bundle);
        return bundle;
    }

    private int count(String bundle) {
        return jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE input->>'clean_background_bundle_id' = ?",
                Integer.class, bundle);
    }

    private ObjectNode input(String calibration) throws Exception {
        ObjectNode in = (ObjectNode) mapper.readTree(Files.readString(EXAMPLES.resolve("static_reconstruct_submit.json")))
                .at("/payload/input");
        in.put("calibration_version", calibration);
        ((ObjectNode) in.get("input_provenance").get("calibration")).put("calibration_version", calibration);
        return in;
    }

    private ObjectNode payload(String bundle, JsonNode input) {
        ObjectNode p = mapper.createObjectNode().put("bundle_id", bundle);
        p.set("input", input);
        return p;
    }

    private RequestBuilder submit(String bearer, String source, String bundle, JsonNode input) {
        return post(PATH).header(HttpHeaders.AUTHORIZATION, "Bearer " + bearer).contentType(MediaType.APPLICATION_JSON)
                .content(envelope(source, payload(bundle, input)).toString());
    }

    private ObjectNode envelope(String source, JsonNode payload) {
        ObjectNode envelope = mapper.createObjectNode().put("schema_version", "1.0.0").put("msg_type", "static_reconstruct_submit")
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", System.currentTimeMillis()).put("source_id", source);
        envelope.set("payload", payload);
        return envelope;
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }
}
