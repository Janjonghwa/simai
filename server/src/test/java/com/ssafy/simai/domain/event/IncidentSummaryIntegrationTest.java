package com.ssafy.simai.domain.event;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.atLeastOnce;
import static org.mockito.Mockito.clearInvocations;
import static org.mockito.Mockito.timeout;
import static org.mockito.Mockito.verify;
import static org.mockito.Mockito.when;
import static org.springframework.security.test.web.servlet.request.SecurityMockMvcRequestPostProcessors.jwt;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import com.ssafy.simai.domain.machine.MachineTokenService;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.nio.charset.StandardCharsets;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.mockito.ArgumentCaptor;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.webmvc.test.autoconfigure.AutoConfigureMockMvc;
import org.springframework.http.HttpHeaders;
import org.springframework.http.MediaType;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.messaging.Message;
import org.springframework.messaging.simp.SimpMessagingTemplate;
import org.springframework.security.core.authority.SimpleGrantedAuthority;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.context.bean.override.mockito.MockitoSpyBean;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.RequestPostProcessor;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 사고 구간 VLM 사후 요약 요청·작업 생성·대여·콜백 반영·STOMP 재발행 통합 시험(S15P31A101-369).
 */
@SpringBootTest
@AutoConfigureMockMvc
@ActiveProfiles("test")
class IncidentSummaryIntegrationTest {

    @Autowired
    MockMvc mvc;

    @Autowired
    JdbcTemplate jdbc;

    @Autowired
    ObjectMapper mapper;

    @Autowired
    ContractSchemas schemas;

    @Autowired
    MachineTokenService machines;

    @MockitoBean
    ObjectStorage storage;

    @MockitoSpyBean
    SimpMessagingTemplate broker;

    private final String suffix = UUID.randomUUID().toString().substring(0, 8);
    private final String gatewayId = "tier0-sum-" + suffix;
    private final String workerId = "worker-sum-" + suffix;
    private final UUID liveEventId = uuid7();
    private final UUID replayEventId = uuid7();
    private final UUID noClipEventId = uuid7();
    private final Instant anchor = Instant.parse("2026-10-13T08:00:00.100Z");
    private final long windowStartMs = anchor.minusSeconds(20).toEpochMilli();
    private final long windowEndMs = anchor.plusSeconds(20).toEpochMilli();
    private String workerToken;
    private String clipKey;
    private long adminUserId;

    @BeforeEach
    void setUp() {
        cleanUp();
        when(storage.configured()).thenReturn(true);
        when(storage.uri(anyString())).thenAnswer(call -> "s3://simai-test-bucket/" + call.getArgument(0));
        when(storage.signGet(anyString())).thenAnswer(call -> new ObjectStorage.SignedRequest(
                "https://objects.test/" + call.getArgument(0), Map.of(), Instant.now().plusSeconds(300)));

        workerToken = machines.registerWorker(workerId, List.of("incident_summary")).token();
        adminUserId = jdbc.queryForObject("""
                INSERT INTO app_user (login_id, password_hash, role, created_at)
                VALUES (?, 'hash', 'ADMIN', now())
                RETURNING user_id
                """, Long.class, "admin-sum-" + suffix);

        seedEvent(liveEventId, "LIVE", "HIGH", 2);
        seedEvent(replayEventId, "REPLAY", "HIGH", 1);
        seedEvent(noClipEventId, "LIVE", "HIGH", 1);

        clipKey = "clips/" + gatewayId + "/" + liveEventId + "/0/cam-01/clip-" + uuid7() + ".mp4";
        seedClip(liveEventId, uuid7(), "cam-01", clipKey,
                Instant.ofEpochMilli(windowStartMs), Instant.ofEpochMilli(windowEndMs), anchor, null);
        clearInvocations(broker);
    }

    @AfterEach
    void cleanUp() {
        jdbc.update("DELETE FROM incident_summary WHERE event_id IN (?, ?, ?)", liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM job_lease WHERE job_id IN (SELECT job_id FROM analysis_job WHERE event_id IN (?, ?, ?))",
                liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM analysis_job WHERE event_id IN (?, ?, ?)", liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM media_clip WHERE event_id IN (?, ?, ?)", liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM risk_event_update WHERE event_id IN (?, ?, ?)", liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM risk_event WHERE event_id IN (?, ?, ?)", liveEventId, replayEventId, noClipEventId);
        jdbc.update("DELETE FROM worker WHERE worker_id = ?", workerId);
        jdbc.update("DELETE FROM app_user WHERE login_id = ?", "admin-sum-" + suffix);
    }

    @Test
    void 요약_요청부터_중복요청_대여_객체서명_콜백_성공까지_정본저장_투영_STOMP_발행을_검증한다() throws Exception {
        // 1. 요약 요청 -> 202 Accepted, QUEUED, STOMP /topic/events 발행
        JsonNode created = body(mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.status").value("QUEUED"))
                .andExpect(jsonPath("$.window_start_ms").value(windowStartMs))
                .andExpect(jsonPath("$.window_end_ms").value(windowEndMs))
                .andExpect(jsonPath("$.evidence_timestamps_ms").isEmpty())
                .andReturn());
        long summaryId = created.get("summary_id").asLong();
        String jobId = created.get("job_id").asString();
        assertThat(jobId).startsWith("job-");

        ObjectNode topicAfterQueue = lastTopicEvent(liveEventId);
        assertThat(topicAfterQueue.at("/summaries/0/summary_id").asLong()).isEqualTo(summaryId);
        assertThat(topicAfterQueue.at("/summaries/0/status").asString()).isEqualTo("QUEUED");
        clearInvocations(broker);

        // 2. QUEUED 상태에서 중복 요청 -> 200 OK, 기존 항목 그대로 반환, 작업 수 여전히 1개
        mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.summary_id").value(summaryId))
                .andExpect(jsonPath("$.job_id").value(jobId))
                .andExpect(jsonPath("$.status").value("QUEUED"));
        assertThat(jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE event_id = ?", Integer.class, liveEventId))
                .isOne();

        // 3. 워커 대여 -> job_request 3.7.0 계약 통과, 상태 RUNNING 전환 및 STOMP 발행
        JsonNode leaseEnvelope = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", workerId,
                                mapper.createObjectNode().put("wait_ms", 0)
                                        .set("job_types", mapper.createArrayNode().add("incident_summary"))).toString()))
                .andExpect(status().isOk())
                .andReturn());
        assertThat(schemas.violations("messages/job_request", leaseEnvelope.toString())).isEmpty();
        assertThat(leaseEnvelope.get("schema_version").asString()).isEqualTo("3.7.0");
        JsonNode leasePayload = leaseEnvelope.get("payload");
        assertThat(leasePayload.get("job_id").asString()).isEqualTo(jobId);
        assertThat(leasePayload.get("priority").asString()).isEqualTo("REPORT");
        assertThat(leasePayload.get("timeout_ms").asInt()).isEqualTo(120_000);
        assertThat(leasePayload.at("/input/face_policy").asString()).isEqualTo("BLURRED");
        String leaseId = leasePayload.get("lease_id").asString();

        ObjectNode topicAfterLease = lastTopicEvent(liveEventId);
        assertThat(topicAfterLease.at("/summaries/0/status").asString()).isEqualTo("RUNNING");
        clearInvocations(broker);

        // 4. RUNNING 상태에서도 중복 요청 -> 200 OK, status RUNNING 그대로 반환
        mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.summary_id").value(summaryId))
                .andExpect(jsonPath("$.job_id").value(jobId))
                .andExpect(jsonPath("$.status").value("RUNNING"));

        // 5. 워커가 클립 GET 서명 주소 발급 가능 확인(JobObjectSignService 연동)
        mvc.perform(post("/api/worker/jobs/{jobId}/objects/sign", jobId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(mapper.createObjectNode()
                                .put("lease_id", leaseId)
                                .put("op", "GET")
                                .put("uri", "s3://simai-test-bucket/" + clipKey)
                                .toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.url").value("https://objects.test/" + clipKey));

        // 6. 워커 콜백(SUCCEEDED) -> 정본 저장 + summaries 투영 + STOMP 발행 + 등급 불변
        long evidenceTs = anchor.minusSeconds(5).toEpochMilli();
        ObjectNode callbackPayload = callbackPayload(jobId, leaseId, "SUCCEEDED");
        ObjectNode result = callbackPayload.putObject("result")
                .put("event_id", liveEventId.toString())
                .put("event_sequence", 2)
                .put("window_start_ms", windowStartMs)
                .put("window_end_ms", windowEndMs)
                .put("summary", "비계 2단 작업 중 안전대 미체결 상태로 몸을 내밀어 추락 위험이 발생한 뒤 복귀했습니다.")
                .put("frames_used", 8)
                .put("model_version", "qwen2.5-vl-7b-awq")
                .put("latency_ms", 11200);
        result.putArray("evidence_timestamps_ms").add(evidenceTs);
        ObjectNode callbackEnvelope = envelope("job_callback", "2.4.0", workerId, callbackPayload);
        assertThat(schemas.violations("messages/job_callback", callbackEnvelope.toString())).isEmpty();

        mvc.perform(post("/api/worker/jobs/{jobId}/result", jobId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(callbackEnvelope.toString()))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.payload.status").value("ACCEPTED"));

        ObjectNode topicAfterSuccess = lastTopicEvent(liveEventId);
        assertThat(topicAfterSuccess.get("severity").asString()).isEqualTo("HIGH");
        assertThat(topicAfterSuccess.get("proposed_severity").isNull()).isTrue();
        JsonNode projected = topicAfterSuccess.at("/summaries/0");
        assertThat(projected.get("summary_id").asLong()).isEqualTo(summaryId);
        assertThat(projected.get("job_id").asString()).isEqualTo(jobId);
        assertThat(projected.get("status").asString()).isEqualTo("SUCCEEDED");
        assertThat(projected.get("summary").asString()).contains("비계 2단 작업 중");
        assertThat(projected.at("/evidence_timestamps_ms/0").asLong()).isEqualTo(evidenceTs);
        assertThat(projected.get("window_start_ms").asLong()).isEqualTo(windowStartMs);
        assertThat(projected.get("window_end_ms").asLong()).isEqualTo(windowEndMs);
        assertThat(projected.get("finished_at").isNumber()).isTrue();
        assertThat(projected.get("error_code").isNull()).isTrue();

        mvc.perform(get("/api/events/{id}", liveEventId).with(role("FOREMAN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.severity").value("HIGH"))
                .andExpect(jsonPath("$.summaries.length()").value(1))
                .andExpect(jsonPath("$.summaries[0].summary_id").value(summaryId))
                .andExpect(jsonPath("$.summaries[0].status").value("SUCCEEDED"))
                .andExpect(jsonPath("$.summaries[0].evidence_timestamps_ms[0]").value(evidenceTs));
    }

    @Test
    void 실패와_타임아웃_상태를_반영하고_재요청하면_기존_기록을_유지한_채_새_요약을_추가한다() throws Exception {
        // 1. 첫 번째 요청 -> FAILED(retryable=false)
        JsonNode first = body(mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isAccepted())
                .andReturn());
        long firstSummaryId = first.get("summary_id").asLong();
        String firstJobId = first.get("job_id").asString();
        String firstLeaseId = leaseOne(firstJobId);

        ObjectNode failedPayload = callbackPayload(firstJobId, firstLeaseId, "FAILED");
        failedPayload.putObject("error")
                .put("code", "CLIP_DECODE_FAILED")
                .put("message", "클립 디코딩 실패")
                .put("retryable", false);
        mvc.perform(post("/api/worker/jobs/{jobId}/result", firstJobId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_callback", "2.4.0", workerId, failedPayload).toString()))
                .andExpect(status().isOk());

        mvc.perform(get("/api/events/{id}", liveEventId).with(role("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.summaries[0].summary_id").value(firstSummaryId))
                .andExpect(jsonPath("$.summaries[0].status").value("FAILED"))
                .andExpect(jsonPath("$.summaries[0].error_code").value("CLIP_DECODE_FAILED"));

        // 2. 두 번째 요청 -> TIMED_OUT
        JsonNode second = body(mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isAccepted())
                .andReturn());
        long secondSummaryId = second.get("summary_id").asLong();
        String secondJobId = second.get("job_id").asString();
        assertThat(secondSummaryId).isGreaterThan(firstSummaryId);
        assertThat(secondJobId).isNotEqualTo(firstJobId);
        String secondLeaseId = leaseOne(secondJobId);

        ObjectNode timedOutPayload = callbackPayload(secondJobId, secondLeaseId, "TIMED_OUT");
        timedOutPayload.putObject("error")
                .put("code", "VLM_TIMEOUT")
                .put("message", "120초 예산 초과")
                .put("retryable", false);
        mvc.perform(post("/api/worker/jobs/{jobId}/result", secondJobId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_callback", "2.4.0", workerId, timedOutPayload).toString()))
                .andExpect(status().isOk());

        // 두 기록이 모두 보존되고 최신 요청이 앞에 온다(AGENTS.md 6 추가 전용)
        mvc.perform(get("/api/events/{id}", liveEventId).with(role("ADMIN")))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.summaries.length()").value(2))
                .andExpect(jsonPath("$.summaries[0].summary_id").value(secondSummaryId))
                .andExpect(jsonPath("$.summaries[0].status").value("TIMED_OUT"))
                .andExpect(jsonPath("$.summaries[0].error_code").value("VLM_TIMEOUT"))
                .andExpect(jsonPath("$.summaries[1].summary_id").value(firstSummaryId))
                .andExpect(jsonPath("$.summaries[1].status").value("FAILED"))
                .andExpect(jsonPath("$.summaries[1].error_code").value("CLIP_DECODE_FAILED"));
    }

    @Test
    void 없는_이벤트_비LIVE_클립없음_삭제된클립_비관리자_잘못된_콜백을_모두_거부한다() throws Exception {
        // 1. 없는 이벤트 -> 404 EVENT_NOT_FOUND
        mvc.perform(post("/api/events/{id}/summary", uuid7()).with(role("ADMIN")))
                .andExpect(status().isNotFound())
                .andExpect(jsonPath("$.code").value("EVENT_NOT_FOUND"));

        // 2. 비-LIVE(REPLAY) -> 409 EVENT_NOT_LIVE
        mvc.perform(post("/api/events/{id}/summary", replayEventId).with(role("ADMIN")))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("EVENT_NOT_LIVE"));

        // 3. 클립 없음 -> 409 CLIP_NOT_READY
        mvc.perform(post("/api/events/{id}/summary", noClipEventId).with(role("ADMIN")))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("CLIP_NOT_READY"));

        // 삭제된 클립(deleted_at IS NOT NULL)만 있어도 409 CLIP_NOT_READY
        seedClip(noClipEventId, uuid7(), "cam-01", "clips/deleted.mp4",
                Instant.ofEpochMilli(windowStartMs), Instant.ofEpochMilli(windowEndMs), anchor, Instant.now());
        mvc.perform(post("/api/events/{id}/summary", noClipEventId).with(role("ADMIN")))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.code").value("CLIP_NOT_READY"));

        // 4. 권한 검사: 반장(FOREMAN)·뷰어(VIEWER)·미인증 거부
        mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("FOREMAN")))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("VIEWER")))
                .andExpect(status().isForbidden());
        mvc.perform(post("/api/events/{id}/summary", liveEventId))
                .andExpect(status().isUnauthorized());

        // 5. 잘못된 워커 콜백(구간 밖 evidence_timestamps_ms) -> 400 INVALID_WORKER_MESSAGE 및 대여 유지(롤백)
        JsonNode created = body(mvc.perform(post("/api/events/{id}/summary", liveEventId).with(role("ADMIN")))
                .andExpect(status().isAccepted())
                .andReturn());
        String jobId = created.get("job_id").asString();
        String leaseId = leaseOne(jobId);

        ObjectNode badCallback = callbackPayload(jobId, leaseId, "SUCCEEDED");
        ObjectNode badResult = badCallback.putObject("result")
                .put("event_id", liveEventId.toString())
                .put("event_sequence", 2)
                .put("window_start_ms", windowStartMs)
                .put("window_end_ms", windowEndMs)
                .put("summary", "구간 밖 타임스탬프 시험")
                .put("frames_used", 4)
                .put("model_version", "qwen2.5-vl-7b-awq")
                .put("latency_ms", 5000);
        badResult.putArray("evidence_timestamps_ms").add(windowEndMs + 10_000L);

        mvc.perform(post("/api/worker/jobs/{jobId}/result", jobId)
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_callback", "2.4.0", workerId, badCallback).toString()))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.code").value("INVALID_WORKER_MESSAGE"));

        assertThat(jdbc.queryForObject("SELECT status FROM incident_summary WHERE job_id = ?", String.class, jobId))
                .isEqualTo("RUNNING");
        assertThat(jdbc.queryForObject("SELECT status FROM analysis_job WHERE job_id = ?", String.class, jobId))
                .isEqualTo("LEASED");
    }

    private String leaseOne(String expectedJobId) throws Exception {
        JsonNode env = body(mvc.perform(post("/api/worker/jobs/lease")
                        .header(HttpHeaders.AUTHORIZATION, "Bearer " + workerToken)
                        .contentType(MediaType.APPLICATION_JSON)
                        .content(envelope("job_lease_request", "2.0.0", workerId,
                                mapper.createObjectNode().put("wait_ms", 0)
                                        .set("job_types", mapper.createArrayNode().add("incident_summary"))).toString()))
                .andExpect(status().isOk())
                .andReturn());
        assertThat(env.at("/payload/job_id").asString()).isEqualTo(expectedJobId);
        return env.at("/payload/lease_id").asString();
    }

    @SuppressWarnings({"unchecked", "rawtypes"})
    private ObjectNode lastTopicEvent(UUID eventId) {
        ArgumentCaptor<Message<?>> captor = (ArgumentCaptor) ArgumentCaptor.forClass(Message.class);
        verify(broker, timeout(2000).atLeastOnce()).send(eq("/topic/events"), captor.capture());
        return captor.getAllValues().stream()
                .map(Message::getPayload)
                .filter(byte[].class::isInstance)
                .map(bytes -> mapper.readTree((byte[]) bytes))
                .filter(env -> eventId.toString().equals(env.at("/payload/event_id").asString()))
                .reduce((first, second) -> second)
                .map(env -> {
                    assertThat(schemas.violations("messages/risk_event", env.toString())).isEmpty();
                    return (ObjectNode) env.get("payload");
                })
                .orElseThrow(() -> new AssertionError("/topic/events 에 이벤트가 발행되지 않았다: " + eventId));
    }

    private void seedEvent(UUID eventId, String source, String severity, int sequence) {
        Timestamp t = Timestamp.from(anchor);
        jdbc.update("""
                INSERT INTO risk_event (event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity,
                    confidence, needs_check, world_ids, zone_id, metrics, resume_count, event_sequence,
                    raised_at, updated_at, delivery, retention_hold)
                VALUES (?, ?, ?, 'FALL_FROM_HEIGHT', 1, 'UPDATED', 'VERIFIED', ?, 'HIGH', false, '{w-12}', NULL,
                    '{"height_m":2.4}'::jsonb, 0, ?, ?, ?, ?, false)
                """, eventId, source, "LIVE".equals(source) ? gatewayId : null, severity, sequence, t, t,
                "LIVE".equals(source) ? "REALTIME" : null);
    }

    private void seedClip(UUID eventId, UUID clipId, String cameraId, String objectKey, Instant start, Instant end,
            Instant anchorAt, Instant deletedAt) {
        Timestamp startTs = Timestamp.from(start);
        Timestamp endTs = Timestamp.from(end);
        Timestamp anchorTs = Timestamp.from(anchorAt);
        jdbc.update("""
                INSERT INTO media_clip (clip_id, event_id, gateway_id, event_sequence, resume_count, rule_id, severity, source,
                    camera_id, stream_epochs, anchor_capture_at, requested_start_at, requested_end_at, clip_start_capture_at,
                    clip_end_capture_at, frame_count, starts_with_keyframe, truncated_start, truncated_end, has_gaps,
                    content_type, container, size_bytes, sha256, face_redaction, clip_created_at, delete_after, retention_hold,
                    object_key, uploaded_at, received_at, deleted_at)
                VALUES (?, ?, ?, 2, 0, 'FALL_FROM_HEIGHT', 'HIGH', 'LIVE', ?, '{1}', ?, ?, ?, ?, ?, 400, true, false, false,
                    false, 'video/mp4', 'MP4_H264', 123456, ?, 'NONE', ?, ?, false, ?, ?, ?, ?)
                """, clipId, eventId, gatewayId, cameraId, anchorTs, startTs, endTs, startTs, endTs,
                "a".repeat(64), anchorTs, endTs, objectKey, anchorTs, anchorTs,
                deletedAt == null ? null : Timestamp.from(deletedAt));
    }

    private ObjectNode callbackPayload(String jobId, String leaseId, String status) {
        long nowMs = anchor.plusSeconds(12).toEpochMilli();
        return mapper.createObjectNode()
                .put("job_id", jobId)
                .put("lease_id", leaseId)
                .put("job_type", "incident_summary")
                .put("status", status)
                .put("started_at", nowMs - 5000)
                .put("finished_at", nowMs);
    }

    private ObjectNode envelope(String msgType, String version, String source, JsonNode payload) {
        ObjectNode env = mapper.createObjectNode()
                .put("schema_version", version)
                .put("msg_type", msgType)
                .put("msg_id", UUID.randomUUID().toString())
                .put("sent_at", System.currentTimeMillis())
                .put("source_id", source);
        env.set("payload", payload);
        return env;
    }

    private RequestPostProcessor role(String role) {
        return jwt().jwt(j -> j.subject(String.valueOf(adminUserId)).claim("role", role))
                .authorities(new SimpleGrantedAuthority("ROLE_" + role));
    }

    private JsonNode body(MvcResult res) throws Exception {
        return mapper.readTree(res.getResponse().getContentAsString(StandardCharsets.UTF_8));
    }

    private static UUID uuid7() {
        UUID random = UUID.randomUUID();
        long high = (System.currentTimeMillis() << 16) | 0x7000L | (random.getMostSignificantBits() & 0x0FFFL);
        long low = (random.getLeastSignificantBits() & 0x3FFFFFFFFFFFFFFFL) | 0x8000000000000000L;
        return new UUID(high, low);
    }
}
