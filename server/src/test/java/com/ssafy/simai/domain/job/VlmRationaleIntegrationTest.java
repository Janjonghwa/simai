package com.ssafy.simai.domain.job;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.mockito.ArgumentMatchers.*;
import static org.mockito.Mockito.*;

import com.ssafy.simai.domain.event.VlmRationaleIngestor;
import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.domain.realtime.RealtimePublisher;
import com.ssafy.simai.domain.tier0.event.RiskEventRepository;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1MessageRouter;
import com.ssafy.simai.domain.tier0.i1.Tier0TelemetryRepository;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 실제 DB 잠금·멱등성·제출 롤백·커밋 뒤 I2 릴레이 연결. DB는 Lead 로컬 환경에서 실행한다. */
@SpringBootTest
@ActiveProfiles("test")
class VlmRationaleIntegrationTest {

    @Autowired VlmRationaleIngestor ingestor;
    @Autowired I1MessageRouter router;
    @Autowired RiskEventRepository events;
    @Autowired Tier0TelemetryRepository telemetry;
    @Autowired TransactionTemplate transaction;
    @Autowired JobLeaseService leases;
    @Autowired JdbcTemplate jdbc;
    @Autowired ObjectMapper mapper;
    @Autowired ContractSchemas schemas;
    @Autowired MachineTokenService machines;
    @MockitoBean RealtimePublisher publisher;

    private final UUID eventId = v7();
    private final String gateway = "vlm-329-" + UUID.randomUUID();
    private final String worker = "vlm-worker-" + UUID.randomUUID();
    private final String world = "w-" + UUID.randomUUID();
    private Instant observed;
    private ObjectNode frame;

    @BeforeEach
    void setUp() throws Exception {
        observed = Instant.ofEpochMilli(System.currentTimeMillis());
        machines.registerWorker(worker, List.of("vlm_rationale"));
        ObjectNode transition = example("risk_event_transition.json");
        transition.put("source_id", gateway).put("msg_id", UUID.randomUUID().toString());
        ObjectNode p = (ObjectNode) transition.get("payload");
        p.put("event_id", eventId.toString()).put("gateway_id", gateway).put("event_sequence", 1)
                .put("transition", "RAISED").put("occurred_at", observed.toEpochMilli())
                .put("observation_time", observed.toEpochMilli()).put("evaluated_at", observed.toEpochMilli());
        p.putArray("world_ids").add(world);
        ObjectNode worldState = example("world_state.json");
        worldState.put("source_id", gateway).put("msg_id", UUID.randomUUID().toString());
        ((ObjectNode) worldState.get("payload")).put("t_latest_global", observed.toEpochMilli());
        ObjectNode track = (ObjectNode) worldState.at("/payload/tracks/0");
        track.put("world_id", world).put("t_last_valid", observed.toEpochMilli());
        ((ObjectNode) worldState.get("payload")).putArray("tracks").add(track);
        p.put("evidence_ref", worldState.path("msg_id").asString());
        transaction.executeWithoutResult(s -> {
            telemetry.upsertWorldState(I1Envelope.parse(worldState), observed);
            events.recordTransition(I1Envelope.parse(transition), "REALTIME", observed, 0);
        });
        frame = example("event_frame_ready.json");
        frame.put("source_id", gateway).put("msg_id", UUID.randomUUID().toString());
        ((ObjectNode) frame.get("payload")).put("event_id", eventId.toString());
        for (JsonNode f : frame.at("/payload/frames")) {
            ((ObjectNode) f).put("estimated_capture_time_ms", observed.toEpochMilli());
        }
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM vlm_assessment WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM job_lease WHERE job_id IN (SELECT job_id FROM analysis_job WHERE event_id = ?)", eventId);
        jdbc.update("DELETE FROM analysis_job WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM risk_event_update WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM risk_event WHERE event_id = ?", eventId);
        jdbc.update("DELETE FROM skeleton_3d WHERE world_id = ?", world);
        jdbc.update("DELETE FROM world_track_state WHERE gateway_id = ?", gateway);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", worker);
    }

    @Test
    void I1_수신은_CRITICAL_작업을_하나_만들고_동시_재통지도_중복된다() throws Exception {
        try (var pool = Executors.newFixedThreadPool(2)) {
            var first = pool.submit(() -> router.route(I1Envelope.parse(frame), observed.plusMillis(100), null));
            ObjectNode secondFrame = frame.deepCopy().put("msg_id", UUID.randomUUID().toString());
            var second = pool.submit(() -> router.route(I1Envelope.parse(secondFrame), observed.plusMillis(100), null));
            first.get(10, TimeUnit.SECONDS);
            second.get(10, TimeUnit.SECONDS);
        }
        assertThat(jobCount()).isEqualTo(1);
        JsonNode job = leases.tryLease(worker, List.of("vlm_rationale"), observed.plusMillis(200))
                .map(j -> JobMessages.jobRequest(mapper, observed.plusMillis(200), j)).orElseThrow();
        assertThat(schemas.violations("messages/job_request", job.toString())).isEmpty();
        assertThat(job.at("/payload/priority").asString()).isEqualTo("VLM_FAST");
        assertThat(job.at("/payload/timeout_ms").asInt()).isEqualTo(5000);
        // -408: 워커 남은 예산의 기준인 이벤트 마감(관측 + 5 s)을 싣는다
        assertThat(job.at("/payload/deadline_at").asLong()).isEqualTo(job.at("/payload/input/observation_time").asLong() + 5000);
        assertThat(job.at("/payload/input/structured_state/tracks/0/position_xy/0").asDouble()).isEqualTo(3.4);
        assertThat(job.at("/payload/input/structured_state/tracks/0/head_z").asDouble()).isCloseTo(1.68, org.assertj.core.data.Offset.offset(0.001));
        assertThat(job.at("/payload/input/frames/0/face_policy").asString()).isEqualTo("BLURRED");
    }

    @Test
    void HIGH_REPLAY_감사전용은_작업이_없다() {
        jdbc.update("UPDATE risk_event SET severity = 'HIGH' WHERE event_id = ?", eventId);
        ingest();
        jdbc.update("UPDATE risk_event SET severity = 'CRITICAL', source = 'REPLAY' WHERE event_id = ?", eventId);
        ingest();
        jdbc.update("UPDATE risk_event SET source = 'LIVE', delivery = 'AUDIT_ONLY' WHERE event_id = ?", eventId);
        ingest();
        assertThat(jobCount()).isZero();
    }

    @Test
    void 콜백은_근거_한건과_G7을_저장하고_등급을_유지하며_화면_형식으로_발행한다() {
        var job = lease();
        ObjectNode callback = succeeded(job);
        assertThat(leases.submit(worker, job.jobId(), callback, observed.plusMillis(2000)).status()).isEqualTo(LeaseStatus.Status.ACCEPTED);
        assertThat(leases.submit(worker, job.jobId(), callback, observed.plusSeconds(60)).status()).isEqualTo(LeaseStatus.Status.DUPLICATE);
        assertThat(jdbc.queryForObject("SELECT count(*) FROM vlm_assessment WHERE event_id = ?", Integer.class, eventId)).isEqualTo(1);
        assertThat(jdbc.queryForObject("SELECT severity FROM risk_event WHERE event_id = ?", String.class, eventId)).isEqualTo("CRITICAL");
        assertThat(jdbc.queryForObject("SELECT event_to_rationale_ms = (extract(epoch FROM created_at) * 1000)::bigint"
                + " - (extract(epoch FROM event_observed_at) * 1000)::bigint FROM vlm_assessment WHERE event_id = ?", Boolean.class, eventId)).isTrue();
        assertThat(jdbc.queryForObject("SELECT within_deadline FROM vlm_assessment WHERE event_id = ?", Boolean.class, eventId)).isTrue();
        var payload = ArgumentCaptor.forClass(JsonNode.class);
        verify(publisher, timeout(3000)).publish(eq("/topic/events"), eq("risk_event"), eq("2.3.0"), payload.capture());
        assertThat(payload.getValue().path("rationale").asString()).isEqualTo(callback.at("/result/rationale").asString());
        assertThat(payload.getValue().path("proposed_severity").isNull()).isTrue();
        JsonNode message = I1Envelope.outbound(mapper, observed.toEpochMilli(), "risk_event", "2.3.0", payload.getValue());
        assertThat(schemas.violations("messages/risk_event", message.toString())).isEmpty();
    }

    /** S15P31A101-408: 관측 + 5 s 를 넘긴 성공도 근거는 저장하되(SUCCEEDED) 마감 판정은 false 로 따로 남는다 */
    @Test
    void 마감을_넘긴_성공은_저장하되_within_deadline_false() {
        var job = lease();
        // 10차 세션의 7.12 s 건: 대여 뒤 처리가 늦어 관측부터 7.12 s 에 저장된 경우를 관측 시각을 당겨 재현한다
        jdbc.update("UPDATE analysis_job SET input = jsonb_set(input, '{observation_time}', to_jsonb(?::bigint)) WHERE job_id = ?",
                observed.toEpochMilli() - 7120, job.jobId());
        assertThat(leases.submit(worker, job.jobId(), succeeded(job), observed.plusMillis(100)).status())
                .isEqualTo(LeaseStatus.Status.ACCEPTED);
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, job.jobId()))
                .isEqualTo("SUCCEEDED");
        assertThat(jdbc.queryForObject("SELECT event_to_rationale_ms >= 7120 AND within_deadline = false FROM vlm_assessment"
                + " WHERE event_id = ?", Boolean.class, eventId)).isTrue();
    }

    @Test
    void 다른_이벤트_결과는_전체_제출이_롤백된다() {
        var job = lease();
        ObjectNode bad = succeeded(job);
        ((ObjectNode) bad.get("result")).put("event_id", v7().toString());
        assertThatThrownBy(() -> leases.submit(worker, job.jobId(), bad, observed.plusMillis(1000))).isInstanceOf(BusinessException.class);
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, job.jobId())).isEqualTo("LEASED");
        assertThat(jdbc.queryForObject("SELECT status FROM job_lease WHERE lease_id = ?", String.class, job.leaseId())).isEqualTo("ACTIVE");
        assertThat(jdbc.queryForObject("SELECT count(*) FROM vlm_assessment WHERE event_id = ?", Integer.class, eventId)).isZero();
        verify(publisher, never()).publish(eq("/topic/events"), anyString(), anyString(), any());
    }

    @Test
    void 재시도는_새_대여와_시도2로_나가고_이벤트_5초부터는_끝난다() {
        var first = lease();
        leases.submit(worker, first.jobId(), failed(first), observed.plusMillis(1000));
        var second = leases.tryLease(worker, List.of("vlm_rationale"), observed.plusMillis(2000)).orElseThrow();
        assertThat(second.jobId()).isEqualTo(first.jobId());
        assertThat(second.leaseId()).isNotEqualTo(first.leaseId());
        assertThat(second.attempt()).isEqualTo(2);
        leases.submit(worker, second.jobId(), failed(second), observed.plusMillis(5000));
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, first.jobId())).isEqualTo("TIMED_OUT");
        assertThat(leases.tryLease(worker, List.of("vlm_rationale"), observed.plusMillis(5001))).isEmpty();
        assertThat(jdbc.queryForObject("SELECT rationale FROM risk_event WHERE event_id = ?", String.class, eventId)).isNull();
    }

    @Test
    void 만료된_대기작업은_내주지_않고_시간초과로_끝난다() {
        ingest();
        assertThat(leases.tryLease(worker, List.of("vlm_rationale"), observed.plusMillis(5000))).isEmpty();
        leases.expireDue(observed.plusMillis(5000));
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE event_id = ?", String.class, eventId)).isEqualTo("TIMED_OUT");
    }

    private JobMessages.LeasedJob lease() {
        ingest();
        return leases.tryLease(worker, List.of("vlm_rationale"), observed.plusMillis(100)).orElseThrow();
    }

    private void ingest() {
        ingestor.ingest(I1Envelope.parse(frame), observed.plusMillis(100), null);
    }

    private int jobCount() {
        return jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE event_id = ?", Integer.class, eventId);
    }

    private ObjectNode succeeded(JobMessages.LeasedJob job) {
        ObjectNode p = callback(job, "SUCCEEDED");
        p.putObject("result").put("event_id", eventId.toString()).put("event_sequence", 1).put("severity", "CRITICAL")
                .put("rationale", "개구부 가까이에 사람이 있다.").putNull("proposed_severity").put("model_version", "test-vlm")
                .put("latency_ms", 1900).putArray("clause_ids");
        return p;
    }

    private ObjectNode failed(JobMessages.LeasedJob job) {
        ObjectNode p = callback(job, "FAILED");
        p.putObject("error").put("code", "VLM_UNAVAILABLE").put("message", "시험").put("retryable", true);
        return p;
    }

    private ObjectNode callback(JobMessages.LeasedJob job, String status) {
        return mapper.createObjectNode().put("job_id", job.jobId()).put("lease_id", job.leaseId().toString())
                .put("job_type", "vlm_rationale").put("status", status).put("finished_at", observed.plusMillis(1900).toEpochMilli());
    }

    private ObjectNode example(String file) throws java.io.IOException {
        return (ObjectNode) mapper.readTree(Files.readString(Path.of(System.getProperty("simai.contracts.dir", "../contracts"), "examples", file)));
    }

    private static UUID v7() {
        UUID random = UUID.randomUUID();
        return new UUID((random.getMostSignificantBits() & ~0xf000L) | 0x7000L, random.getLeastSignificantBits());
    }
}
