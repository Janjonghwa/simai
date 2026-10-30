package com.ssafy.simai.domain.job;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.scene.SceneActivationService;
import com.ssafy.simai.domain.scene.SceneJobListener;
import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.global.storage.ObjectStorage;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.command.AdminCommands;
import com.ssafy.simai.global.command.CommandIo;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
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
import tools.jackson.databind.node.ObjectNode;

/**
 * I5 작업 대여·연장·제출·재할당(S15P31A101-229). 대여의 행 잠금을 실제로 보려고 시험 트랜잭션 없이 커밋하고 매번 지운다.
 * 서버가 내는 job_request·job_lease_status 는 contracts/ 스키마로 검사한다.
 */
@SpringBootTest(properties = "simai.scene.default-gltf-key=scenes/demo.glb")
@AutoConfigureMockMvc
@ActiveProfiles("test")
class WorkerJobIntegrationTest {

    private static final Path EXAMPLES = Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples");
    private static final List<String> ALL_TYPES = List.of("vlm_rationale", "static_reconstruct", "tbm_report");
    private static final Pattern ENQUEUED = Pattern.compile("작업을 넣었다: (\\S+)");

    @Autowired
    MockMvc mvc;

    @Autowired
    MachineTokenService machines;

    @Autowired
    AnalysisJobService jobs;

    @Autowired
    JobLeaseService leases;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    AdminCommands commands;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    private final String workerA = "worker-a-" + UUID.randomUUID().toString().substring(0, 8);
    private final String workerB = "worker-b-" + UUID.randomUUID().toString().substring(0, 8);
    private final String calibration = "cal-scene-" + UUID.randomUUID();
    private final String scene = "scene-" + UUID.randomUUID();
    private String site;

    @MockitoBean
    ObjectStorage storage;

    @Autowired
    SceneActivationService activations;

    @Autowired
    SceneJobListener sceneJobs;

    @Autowired
    ViewerHealthService health;

    private String tokenA;
    private String tokenB;

    @BeforeEach
    void setUp() {
        clearJobs();
        site = jdbc.queryForObject("SELECT site_id FROM site LIMIT 1", String.class);
        jdbc.update("""
                INSERT INTO calibration_version (calibration_version, site_id, state, provider_source, reason, effective_at,
                    calibration_ref, calibration_hash, created_at)
                VALUES (?, ?, 'CALIBRATED', 'G1_AUTO', 'scene test', now(), 's3://test/cal.json', ?, now())
                """, calibration, site, "sha256:" + "a".repeat(64));
        when(storage.uri(anyString())).thenAnswer(call -> "s3://simai-test-bucket/" + call.getArgument(0));
        when(storage.configured()).thenReturn(true);
        when(storage.size(anyString())).thenReturn(java.util.OptionalLong.of(1000));
        when(storage.signGet(anyString())).thenAnswer(call -> new ObjectStorage.SignedRequest(
                "https://objects.test/" + call.getArgument(0), java.util.Map.of(), Instant.now().plusSeconds(300)));
        tokenA = machines.registerWorker(workerA, ALL_TYPES).token();
        tokenB = machines.registerWorker(workerB, ALL_TYPES).token();
    }

    @AfterEach
    void tearDown() {
        clearJobs();
        jdbc.update("DELETE FROM calibration_version WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM worker WHERE worker_id IN (?, ?)", workerA, workerB);
    }

    private void clearJobs() {
        jdbc.update("DELETE FROM scene_version WHERE calibration_version = ?", calibration);
        jdbc.update("DELETE FROM job_lease");
        jdbc.update("DELETE FROM analysis_job");
    }

    @Test
    void 우선순위대로_대여하고_job_request_는_계약에_맞으며_없으면_204() throws Exception {
        String background = enqueue("static_reconstruct", 2);
        String vlm = jobs.enqueue(new JobSpec("vlm_rationale", exampleInput("job_request.vlm_rationale.json"), 2, 5000, null, null));

        // 처리할 수 없는 유형만 대기 중이면 내주지 않는다
        mvc.perform(lease(tokenA, workerA, List.of("tbm_report"), 0)).andExpect(status().isNoContent());

        JsonNode first = body(mvc.perform(lease(tokenA, workerA, ALL_TYPES, 0)).andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_request", first.toString())).isEmpty();
        assertThat(first.get("schema_version").stringValue()).startsWith("3.");
        JsonNode p = first.get("payload");
        assertThat(p.get("job_id").stringValue()).isEqualTo(vlm);
        assertThat(p.get("priority").stringValue()).isEqualTo("VLM_FAST");
        assertThat(p.get("attempt").intValue()).isEqualTo(1);
        assertThat(p.get("leased_at").longValue()).isEqualTo(first.get("sent_at").longValue());
        assertThat(p.get("lease_expires_at").longValue() - p.get("leased_at").longValue()).isEqualTo(JobMessages.LEASE_HOLD_MS);

        JsonNode second = body(mvc.perform(lease(tokenA, workerA, ALL_TYPES, 0)).andExpect(status().isOk()).andReturn());
        assertThat(second.at("/payload/job_id").stringValue()).isEqualTo(background);
        mvc.perform(lease(tokenA, workerA, ALL_TYPES, 0)).andExpect(status().isNoContent());

        assertThat(jdbc.queryForObject("SELECT last_seen_at IS NOT NULL FROM worker WHERE worker_id = ?", Boolean.class, workerA))
                .isTrue();
    }

    @Test
    void 두_워커가_동시에_대여해도_같은_작업은_한_번만_나간다() throws Exception {
        for (int i = 0; i < 30; i++) {
            enqueue("static_reconstruct", 1);
        }
        ExecutorService pool = Executors.newFixedThreadPool(8);
        List<String> all = new ArrayList<>();
        try {
            List<Future<List<String>>> futures = new ArrayList<>();
            for (int t = 0; t < 8; t++) {
                String worker = t % 2 == 0 ? workerA : workerB;
                futures.add(pool.submit(() -> {
                    List<String> got = new ArrayList<>();
                    Optional<JobMessages.LeasedJob> job;
                    while ((job = leases.tryLease(worker, ALL_TYPES, Instant.now())).isPresent()) {
                        got.add(job.get().jobId());
                    }
                    return got;
                }));
            }
            for (Future<List<String>> f : futures) {
                all.addAll(f.get(30, TimeUnit.SECONDS));
            }
        } finally {
            pool.shutdownNow();
        }
        assertThat(all).hasSize(30);
        assertThat(new HashSet<>(all)).hasSize(30);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM job_lease", Integer.class)).isEqualTo(30);
    }

    @Test
    void 하트비트는_대여를_늘리고_다른_워커나_틀린_봉투는_거부한다() throws Exception {
        String jobId = enqueue("static_reconstruct", 2);
        String leaseId = leasePayload(tokenA, workerA).get("lease_id").stringValue();

        JsonNode active = body(mvc.perform(heartbeat(tokenA, workerA, jobId, leaseId)).andExpect(status().isOk()).andReturn());
        assertThat(schemas.violations("messages/job_lease_status", active.toString())).isEmpty();
        assertThat(active.at("/payload/status").stringValue()).isEqualTo("ACTIVE");
        assertThat(active.at("/payload/lease_expires_at").longValue() - active.get("sent_at").longValue())
                .isEqualTo(JobMessages.LEASE_HOLD_MS);
        assertThat(jdbc.queryForObject("SELECT progress FROM job_lease WHERE lease_id = ?::uuid", Double.class, leaseId))
                .isEqualTo(0.5);

        // 다른 워커가 같은 대여로 보내면 UNKNOWN_LEASE
        JsonNode unknown = body(mvc.perform(heartbeat(tokenB, workerB, jobId, leaseId)).andExpect(status().isConflict())
                .andReturn());
        assertThat(unknown.at("/payload/reason").stringValue()).isEqualTo("UNKNOWN_LEASE");
        assertThat(schemas.violations("messages/job_lease_status", unknown.toString())).isEmpty();
        // 봉투 source_id 가 토큰의 워커가 아니면 401, 경로와 본문 job_id 가 다르면 400
        mvc.perform(heartbeat(tokenA, workerB, jobId, leaseId)).andExpect(status().isUnauthorized())
                .andExpect(jsonPath("$.code").value("WORKER_SOURCE_MISMATCH"));
        mvc.perform(post("/api/worker/jobs/other-job/heartbeat").header(HttpHeaders.AUTHORIZATION, "Bearer " + tokenA)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_heartbeat", "2.0.0", workerA, heartbeatPayload(jobId, leaseId)).toString()))
                .andExpect(status().isBadRequest());
        // 계약에 맞지 않는 본문은 400, 토큰이 틀리면 401
        mvc.perform(post("/api/worker/jobs/lease").header(HttpHeaders.AUTHORIZATION, "Bearer " + tokenA)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"msg_type\":\"job_lease_request\"}"))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.code").value("INVALID_WORKER_MESSAGE"));
        mvc.perform(heartbeat("nope", workerA, jobId, leaseId)).andExpect(status().isUnauthorized());
    }

    @Test
    void 만료된_대여는_새_대여로_재할당되고_늦은_제출은_거부되며_재전송은_DUPLICATE() throws Exception {
        String jobId = enqueue("static_reconstruct", 2);
        String oldLease = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        expireLease(oldLease);

        JsonNode second = leasePayload(tokenB, workerB);
        assertThat(second.get("job_id").stringValue()).isEqualTo(jobId);
        assertThat(second.get("attempt").intValue()).isEqualTo(2);
        String newLease = second.get("lease_id").stringValue();
        assertThat(newLease).isNotEqualTo(oldLease);

        JsonNode late = body(mvc.perform(submit(tokenA, workerA, succeeded(jobId, oldLease))).andExpect(status().isConflict())
                .andReturn());
        assertThat(late.at("/payload/reason").stringValue()).isEqualTo("LEASE_EXPIRED");

        JsonNode accepted = body(mvc.perform(submit(tokenB, workerB, succeeded(jobId, newLease))).andExpect(status().isOk())
                .andReturn());
        assertThat(accepted.at("/payload/status").stringValue()).isEqualTo("ACCEPTED");
        assertThat(schemas.violations("messages/job_lease_status", accepted.toString())).isEmpty();
        assertThat(jobStatus(jobId)).isEqualTo("SUCCEEDED");
        assertThat(jdbc.queryForObject("SELECT result->>'scene_version' FROM analysis_job WHERE job_id = ?", String.class, jobId))
                .isEqualTo(scene);

        // 응답을 못 받아 다시 보낸 제출은 대여가 끝났어도 DUPLICATE
        mvc.perform(submit(tokenB, workerB, succeeded(jobId, newLease))).andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("DUPLICATE"));
    }

    @Test
    void 남은_시도가_없으면_만료된_작업은_TIMED_OUT_으로_끝난다() throws Exception {
        String jobId = enqueue("static_reconstruct", 1);
        String leaseId = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        expireLease(leaseId);
        leases.expireDue(Instant.now());

        assertThat(jobStatus(jobId)).isEqualTo("TIMED_OUT");
        assertThat(jdbc.queryForObject("SELECT status FROM job_lease WHERE lease_id = ?::uuid", String.class, leaseId))
                .isEqualTo("EXPIRED");
        mvc.perform(lease(tokenA, workerA, ALL_TYPES, 0)).andExpect(status().isNoContent());
        mvc.perform(heartbeat(tokenA, workerA, jobId, leaseId)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("LEASE_EXPIRED"));
    }

    @Test
    void 재시도_가능한_실패는_다음_시도로_반납은_같은_시도로_다시_내주고_그_밖의_실패는_끝낸다() throws Exception {
        String jobId = enqueue("static_reconstruct", 2);
        String first = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        mvc.perform(submit(tokenA, workerA, failed(jobId, first, "INPUT_DOWNLOAD_FAILED", true, null)))
                .andExpect(status().isOk()).andExpect(jsonPath("$.payload.status").value("ACCEPTED"));
        assertThat(jobStatus(jobId)).isEqualTo("QUEUED");

        JsonNode second = leasePayload(tokenA, workerA);
        assertThat(second.get("attempt").intValue()).isEqualTo(2);
        String secondLease = second.get("lease_id").stringValue();
        // 반납: 마지막 시도여도 시도를 쓰지 않고 같은 attempt 로 다시 대기
        mvc.perform(submit(tokenA, workerA, failed(jobId, secondLease, "TIER0_PRIORITY", true, false)))
                .andExpect(status().isOk()).andExpect(jsonPath("$.payload.status").value("ACCEPTED"));
        assertThat(jdbc.queryForObject("SELECT status FROM job_lease WHERE lease_id = ?::uuid", String.class, secondLease))
                .isEqualTo("RELEASED");
        mvc.perform(submit(tokenA, workerA, failed(jobId, secondLease, "TIER0_PRIORITY", true, false)))
                .andExpect(status().isOk()).andExpect(jsonPath("$.payload.status").value("DUPLICATE"));

        JsonNode third = leasePayload(tokenA, workerA);
        assertThat(third.get("attempt").intValue()).isEqualTo(2);
        mvc.perform(submit(tokenA, workerA, failed(jobId, third.get("lease_id").stringValue(), "HANDLER_ERROR", false, null)))
                .andExpect(status().isOk());
        assertThat(jobStatus(jobId)).isEqualTo("FAILED");
        assertThat(jdbc.queryForObject("SELECT error->>'code' FROM analysis_job WHERE job_id = ?", String.class, jobId))
                .isEqualTo("HANDLER_ERROR");
    }

    @Test
    void 실행_상한이_지난_하트비트는_JOB_TIMED_OUT_이고_취소된_작업은_JOB_CANCELLED() throws Exception {
        String timedOut = enqueue("static_reconstruct", 2);
        String lease = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        jdbc.update("UPDATE job_lease SET leased_at = leased_at - interval '20 minutes' WHERE lease_id = ?::uuid", lease);
        mvc.perform(heartbeat(tokenA, workerA, timedOut, lease)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("JOB_TIMED_OUT"));
        assertThat(jobStatus(timedOut)).isEqualTo("TIMED_OUT");

        String cancelled = enqueue("static_reconstruct", 2);
        String cancelledLease = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        assertThat(jobs.cancel(cancelled)).isTrue();
        mvc.perform(heartbeat(tokenA, workerA, cancelled, cancelledLease)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("JOB_CANCELLED"));
        mvc.perform(submit(tokenA, workerA, succeeded(cancelled, cancelledLease))).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("JOB_CANCELLED"));
        assertThat(jobStatus(cancelled)).isEqualTo("CANCELLED");
        assertThat(jobs.cancel(cancelled)).isFalse();
    }

    @Test
    void 취소된_작업은_대여가_만료된_뒤에도_JOB_CANCELLED_로_알린다() throws Exception {
        String jobId = enqueue("static_reconstruct", 2);
        String leaseId = leasePayload(tokenA, workerA).get("lease_id").stringValue();
        assertThat(jobs.cancel(jobId)).isTrue();
        expireLease(leaseId);
        leases.expireDue(Instant.now());

        mvc.perform(heartbeat(tokenA, workerA, jobId, leaseId)).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("JOB_CANCELLED"));
        assertThat(jobStatus(jobId)).isEqualTo("CANCELLED");
    }

    @Test
    void 워커에_보내지_못한_대여는_시도를_쓰지_않고_되돌린다() throws Exception {
        String jobId = enqueue("static_reconstruct", 1);
        JobMessages.LeasedJob lost = leases.tryLease(workerA, ALL_TYPES, Instant.now()).orElseThrow();
        leases.unlease(lost.leaseId());

        assertThat(jobStatus(jobId)).isEqualTo("QUEUED");
        JsonNode again = leasePayload(tokenA, workerA);
        assertThat(again.get("attempt").intValue()).isEqualTo(1);
        mvc.perform(heartbeat(tokenA, workerA, jobId, lost.leaseId().toString())).andExpect(status().isConflict())
                .andExpect(jsonPath("$.payload.reason").value("LEASE_EXPIRED"));
    }

    @Test
    void long_poll_은_기다리는_동안_들어온_작업을_바로_내준다() throws Exception {
        ExecutorService pool = Executors.newSingleThreadExecutor();
        try {
            pool.submit(() -> {
                Thread.sleep(300);
                return enqueue("static_reconstruct", 1);
            });
            long started = System.nanoTime();
            mvc.perform(lease(tokenA, workerA, ALL_TYPES, 10_000)).andExpect(status().isOk());
            assertThat((System.nanoTime() - started) / 1_000_000).isLessThan(5_000);
        } finally {
            pool.shutdownNow();
        }
    }

    @Test
    void 계약에_맞지_않는_작업은_등록하지_않고_관리_명령으로_넣고_취소할_수_있다() {
        assertThatThrownBy(() -> jobs.enqueue(new JobSpec("vlm_rationale", mapper.createObjectNode(), 2, 5000, null, null)))
                .isInstanceOf(BusinessException.class);
        assertThatThrownBy(() -> jobs.enqueue(new JobSpec("unknown", mapper.createObjectNode(), 2, 5000, null, null)))
                .isInstanceOf(BusinessException.class);

        ByteArrayOutputStream out = new ByteArrayOutputStream();
        assertThat(commands.execute(List.of("job", "enqueue", "--type", "static_reconstruct", "--input",
                "{\"calibration_version\":\"cal-7\"}"), io(out))).isZero();
        Matcher matcher = ENQUEUED.matcher(out.toString(StandardCharsets.UTF_8));
        assertThat(matcher.find()).isTrue();
        String jobId = matcher.group(1);
        assertThat(jobStatus(jobId)).isEqualTo("QUEUED");
        assertThat(commands.execute(List.of("job", "cancel", "--id", jobId), io(new ByteArrayOutputStream()))).isZero();
        assertThat(jobStatus(jobId)).isEqualTo("CANCELLED");
        assertThat(commands.execute(List.of("job", "enqueue", "--type", "vlm_rationale", "--input", "{}"),
                io(new ByteArrayOutputStream()))).isNotZero();
        assertThat(commands.execute(List.of("job", "cancel", "--id", "job-none"), io(new ByteArrayOutputStream())))
                .isNotZero();
    }

    @Test
    void 콜백은_후보와_근거를_한번만_추가하고_활성전환과_되돌리기를_즉시_조회한다() throws Exception {
        String job = enqueue("static_reconstruct", 2);
        String lease = leasePayload(tokenA, workerA).get("lease_id").asString();
        ObjectNode callback = succeeded(job, lease);
        mvc.perform(submit(tokenA, workerA, callback)).andExpect(status().isOk());
        mvc.perform(submit(tokenA, workerA, callback)).andExpect(jsonPath("$.payload.status").value("DUPLICATE"));
        // 이벤트를 다시 받아도 처음 결과만 남는다.
        sceneJobs.finished(new JobFinished(job, "static_reconstruct", "SUCCEEDED", callback.get("result"), null));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_version WHERE job_id = ?", Integer.class, job)).isOne();
        assertThat(jdbc.queryForObject("SELECT state FROM scene_version WHERE job_id = ?", String.class, job)).isEqualTo("CANDIDATE");
        JsonNode saved = mapper.readTree(jdbc.queryForObject("SELECT scene_output::text FROM scene_version WHERE job_id = ?",
                String.class, job));
        assertThat(saved).isEqualTo(callback.get("result"));
        assertThat(current().get("default").asBoolean()).isTrue();
        assertThat(current().has("surface_source_map_urls")).isFalse();
        activations.activate(site, scene, "test-admin", "첫 배경");
        JsonNode first = current();
        assertThat(first.get("scene_version").asString()).isEqualTo(scene);
        assertThat(first.get("surface_source_map_urls")).hasSize(1);
        assertThat(first.at("/surface_source_map_urls/0").asString()).contains("/" + lease + "/asset-1");
        assertThat(first.get("gltf_url").asString()).contains("/" + lease + "/asset-0");
        String secondJob = enqueue("static_reconstruct", 2);
        String secondLease = leasePayload(tokenA, workerA).get("lease_id").asString();
        ObjectNode second = succeeded(secondJob, secondLease);
        ((ObjectNode) second.get("result")).put("scene_version", scene + "-b");
        // Native 복원은 보정 입력 근거를 생략할 수 있다. 선택 필드가 없어도 보정 FK의 현장을 쓴다.
        ((ObjectNode) second.at("/result/input_provenance")).remove("calibration");
        ((ObjectNode) second.at("/result/coordinate_frame")).remove("site_id");
        mvc.perform(submit(tokenA, workerA, second)).andExpect(status().isOk());
        assertThat(current().get("scene_version").asString()).isEqualTo(scene);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        assertThat(commands.execute(List.of("scene", "activate", "--site", site, "--version", scene + "-b",
                "--actor", "test-admin", "--reason", "두번째"), io(out))).isZero();
        assertThat(current().get("scene_version").asString()).isEqualTo(scene + "-b");
        assertThat(health.message().at("/payload/scene/committed_version").asString()).isEqualTo(scene + "-b");
        activations.activate(site, scene, "test-admin", "되돌림");
        assertThat(current().get("scene_version").asString()).isEqualTo(scene);
        assertThat(health.message().at("/payload/scene/committed_version").asString()).isEqualTo(scene);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_activation WHERE site_id = ?", Integer.class, site)).isEqualTo(3);
        assertThat(mapper.readTree(jdbc.queryForObject("SELECT scene_output::text FROM scene_version WHERE job_id = ?",
                String.class, job))).isEqualTo(saved);
        activations.activate(site, scene, "test-admin", "같은 대상 재시도");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_activation WHERE site_id = ?", Integer.class, site)).isEqualTo(3);
        assertThat(commands.execute(List.of("scene", "list", "--site", site), io(out))).isZero();
        assertThat(out.toString(StandardCharsets.UTF_8)).contains(job, "되돌림");
    }

    @Test
    void 잘못된_결과는_작업과_대여까지_되돌려_수정한_콜백을_받는다() throws Exception {
        String job = enqueue("static_reconstruct", 2);
        String lease = leasePayload(tokenA, workerA).get("lease_id").asString();
        ObjectNode callback = succeeded(job, lease);
        ObjectNode output = (ObjectNode) callback.get("result");
        ObjectNode asset = (ObjectNode) output.at("/assets/0");
        String validUri = asset.get("uri").asString();
        for (String bad : List.of("s3://other-bucket/static-3d/asset.glb",
                "s3://simai-test-bucket/static-3d/other-job/" + lease + "/asset.glb",
                "s3://simai-test-bucket/static-3d/" + job + "/other-lease/asset.glb")) {
            asset.put("uri", bad);
            mvc.perform(submit(tokenA, workerA, callback)).andExpect(status().isBadRequest());
            assertThat(jobStatus(job)).isEqualTo("LEASED");
            assertThat(jdbc.queryForObject("SELECT status FROM job_lease WHERE lease_id = ?::uuid", String.class, lease)).isEqualTo("ACTIVE");
        }
        asset.put("uri", validUri);
        ((ObjectNode) output.get("coordinate_frame")).put("site_id", "wrong-site");
        mvc.perform(submit(tokenA, workerA, callback)).andExpect(status().isBadRequest());
        ((ObjectNode) output.get("coordinate_frame")).put("site_id", site);
        output.put("calibration_version", "wrong-calibration");
        mvc.perform(submit(tokenA, workerA, callback)).andExpect(status().isBadRequest());
        output.put("calibration_version", calibration);
        output.remove("quality");
        mvc.perform(submit(tokenA, workerA, callback)).andExpect(status().isBadRequest());
        mvc.perform(submit(tokenA, workerA, succeeded(job, lease))).andExpect(status().isOk());
        String otherJob = enqueue("static_reconstruct", 2);
        String otherLease = leasePayload(tokenA, workerA).get("lease_id").asString();
        // 다른 작업이 같은 Scene ID를 주장해도 처음 Scene을 보존한다.
        mvc.perform(submit(tokenA, workerA, succeeded(otherJob, otherLease))).andExpect(status().isConflict());
        assertThat(jobStatus(otherJob)).isEqualTo("LEASED");
        assertThat(jdbc.queryForObject("SELECT job_id FROM scene_version WHERE scene_version = ?", String.class, scene)).isEqualTo(job);
    }

    @Test
    void 상한입력과_보정오류와_누락자산과_다른현장과_거절된_후보는_활성화하지_않는다() throws Exception {
        String job = enqueue("static_reconstruct", 2);
        String lease = leasePayload(tokenA, workerA).get("lease_id").asString();
        mvc.perform(submit(tokenA, workerA, succeeded(job, lease))).andExpect(status().isOk());
        assertThatThrownBy(() -> activations.activate("other-site", scene, "test", "현장 오류")).isInstanceOf(BusinessException.class);
        jdbc.update("UPDATE scene_version SET is_upper_bound = true WHERE scene_version = ?", scene);
        assertThatThrownBy(() -> activations.activate(site, scene, "test", "상한")).isInstanceOf(BusinessException.class);
        jdbc.update("UPDATE scene_version SET is_upper_bound = false WHERE scene_version = ?", scene);
        jdbc.update("UPDATE calibration_version SET state = 'DRIFTED' WHERE calibration_version = ?", calibration);
        assertThatThrownBy(() -> activations.activate(site, scene, "test", "보정 오류")).isInstanceOf(BusinessException.class);
        jdbc.update("UPDATE calibration_version SET state = 'CALIBRATED' WHERE calibration_version = ?", calibration);
        when(storage.size(anyString())).thenReturn(java.util.OptionalLong.empty());
        assertThatThrownBy(() -> activations.activate(site, scene, "test", "자산 누락")).isInstanceOf(BusinessException.class);
        assertThat(current().get("default").asBoolean()).isTrue();
        when(storage.size(anyString())).thenReturn(java.util.OptionalLong.of(1000));
        jdbc.update("UPDATE scene_version SET state = 'REJECTED' WHERE scene_version = ?", scene);
        assertThatThrownBy(() -> activations.activate(site, scene, "test", "거절 후보")).isInstanceOf(BusinessException.class);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM scene_activation WHERE site_id = ?", Integer.class, site)).isZero();
    }

    private JsonNode current() throws Exception {
        return body(mvc.perform(get("/api/scenes/current").with(jwt().authorities(
                new org.springframework.security.core.authority.SimpleGrantedAuthority("ROLE_VIEWER"))))
                .andExpect(status().isOk()).andReturn());
    }

    // ---- 도우미

    private String enqueue(String type, int maxAttempts) {
        return jobs.enqueue(new JobSpec(type, mapper.createObjectNode().put("calibration_version", calibration), maxAttempts,
                600_000, null, null));
    }

    private JsonNode exampleInput(String file) throws Exception {
        return mapper.readTree(Files.readString(EXAMPLES.resolve(file))).at("/payload/input");
    }

    private JsonNode leasePayload(String token, String worker) throws Exception {
        return body(mvc.perform(lease(token, worker, ALL_TYPES, 0)).andExpect(status().isOk()).andReturn()).get("payload");
    }

    private void expireLease(String leaseId) {
        jdbc.update("UPDATE job_lease SET lease_expires_at = now() - interval '1 second' WHERE lease_id = ?::uuid", leaseId);
    }

    private String jobStatus(String jobId) {
        return jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, jobId);
    }

    private RequestBuilder lease(String token, String worker, List<String> types, int waitMs) {
        ObjectNode p = mapper.createObjectNode();
        types.forEach(p.putArray("job_types")::add);
        p.put("wait_ms", waitMs);
        return post("/api/worker/jobs/lease").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON).content(envelope("job_lease_request", "2.0.0", worker, p).toString());
    }

    private RequestBuilder heartbeat(String token, String worker, String jobId, String leaseId) {
        return post("/api/worker/jobs/" + jobId + "/heartbeat").header(HttpHeaders.AUTHORIZATION, "Bearer " + token)
                .contentType(MediaType.APPLICATION_JSON)
                .content(envelope("job_heartbeat", "2.0.0", worker, heartbeatPayload(jobId, leaseId)).toString());
    }

    private ObjectNode heartbeatPayload(String jobId, String leaseId) {
        return mapper.createObjectNode().put("job_id", jobId).put("lease_id", leaseId).put("progress", 0.5);
    }

    private RequestBuilder submit(String token, String worker, ObjectNode payload) {
        return post("/api/worker/jobs/" + payload.get("job_id").stringValue() + "/result")
                .header(HttpHeaders.AUTHORIZATION, "Bearer " + token).contentType(MediaType.APPLICATION_JSON)
                .content(envelope("job_callback", "2.2.0", worker, payload).toString());
    }

    private ObjectNode succeeded(String jobId, String leaseId) {
        ObjectNode p = callback(jobId, leaseId, "SUCCEEDED");
        ObjectNode output;
        try {
            output = (ObjectNode) mapper.readTree(Files.readString(EXAMPLES.resolve("scene_output.json")));
        } catch (java.io.IOException e) {
            throw new java.io.UncheckedIOException(e);
        }
        output.put("scene_version", scene).put("calibration_version", calibration);
        ((ObjectNode) output.get("coordinate_frame")).put("site_id", site);
        ((ObjectNode) output.at("/input_provenance/calibration")).put("calibration_version", calibration);
        int i = 0;
        for (JsonNode asset : output.get("assets")) {
            ((ObjectNode) asset).put("uri", "s3://simai-test-bucket/static-3d/" + jobId + "/" + leaseId + "/asset-" + i++)
                    .put("bytes", 1000);
        }
        p.set("result", output);
        return p;
    }

    private ObjectNode failed(String jobId, String leaseId, String code, boolean retryable, Boolean consumeAttempt) {
        ObjectNode p = callback(jobId, leaseId, "FAILED");
        ObjectNode error = p.putObject("error").put("code", code).put("message", "시험").put("retryable", retryable);
        if (consumeAttempt != null) {
            error.put("consume_attempt", consumeAttempt);
        }
        return p;
    }

    private ObjectNode callback(String jobId, String leaseId, String status) {
        long now = System.currentTimeMillis();
        return mapper.createObjectNode().put("job_id", jobId).put("lease_id", leaseId).put("job_type", "static_reconstruct")
                .put("status", status).put("started_at", now - 1000).put("finished_at", now);
    }

    private ObjectNode envelope(String msgType, String version, String source, JsonNode payload) {
        ObjectNode envelope = mapper.createObjectNode().put("schema_version", version).put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString()).put("sent_at", System.currentTimeMillis()).put("source_id", source);
        envelope.set("payload", payload);
        return envelope;
    }

    private JsonNode body(MvcResult result) throws Exception {
        return mapper.readTree(result.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }

    private static CommandIo io(ByteArrayOutputStream out) {
        PrintStream print = new PrintStream(out, true, StandardCharsets.UTF_8);
        return new CommandIo(print, print, prompt -> null);
    }
}
