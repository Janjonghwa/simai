package com.ssafy.simai.domain.job;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import java.time.Instant;
import java.util.UUID;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * EC2 → 분석 워커 I5 메시지(명세 12.5, FR-101). 봉투는 I1·I2 와 같다(source_id ec2, 새 msg_id).
 * job_request 는 3.x(attempt = 반납을 세지 않는 시도 번호)이고 워커는 3.x 가 아니면 받지 않는다.
 */
final class JobMessages {

    /** job_request 3.5.0(vlm_rationale 입력 프레임 local_copy, -408). 워커 클라이언트는 메이저 3 만 본다 */
    static final String JOB_REQUEST_VERSION = "3.5.0";
    static final String LEASE_STATUS_VERSION = "2.0.0";
    /** 대여 유지 시간과 하트비트 주기(계약 기본안: 유지 30000ms, 주기 10000ms = 유지의 1/3) */
    static final long LEASE_HOLD_MS = 30_000;
    static final int HEARTBEAT_INTERVAL_MS = 10_000;

    private JobMessages() {
    }

    /** 새 대여(새 lease_id, 유지 시간·하트비트 주기). 대여와 등록 때 검사가 같은 모양을 쓴다 */
    static LeasedJob newLease(String jobId, int attempt, int maxAttempts, String jobType, String priority, long timeoutMs,
            Instant now, JsonNode input, Instant deadlineAt) {
        return new LeasedJob(jobId, UUID.randomUUID(), attempt, maxAttempts, jobType, priority, timeoutMs, now,
                now.plusMillis(LEASE_HOLD_MS), HEARTBEAT_INTERVAL_MS, input, deadlineAt);
    }

    static ObjectNode jobRequest(ObjectMapper mapper, Instant now, LeasedJob job) {
        ObjectNode p = mapper.createObjectNode();
        p.put("job_id", job.jobId());
        p.put("lease_id", job.leaseId().toString());
        p.put("attempt", job.attempt());
        p.put("max_attempts", job.maxAttempts());
        p.put("job_type", job.jobType());
        p.put("priority", job.priority());
        p.put("timeout_ms", job.timeoutMs());
        p.put("leased_at", job.leasedAt().toEpochMilli());
        p.put("lease_expires_at", job.leaseExpiresAt().toEpochMilli());
        p.put("heartbeat_interval_ms", job.heartbeatIntervalMs());
        // 이벤트 마감(analysis_job.deadline_at, vlm_rationale 은 관측 + 5 s). 워커가 남은 예산을 이 시각까지로 묶는다(-408)
        if (job.deadlineAt() != null) {
            p.put("deadline_at", job.deadlineAt().toEpochMilli());
        }
        p.set("input", job.input());
        String version = AnalysisJobService.SCENE_REGENERATE_JOB_TYPE.equals(job.jobType()) ? "3.6.0" : JOB_REQUEST_VERSION;
        return I1Envelope.outbound(mapper, now.toEpochMilli(), "job_request", version, p);
    }

    static ObjectNode leaseStatus(ObjectMapper mapper, Instant now, String jobId, UUID leaseId, LeaseStatus status) {
        ObjectNode p = mapper.createObjectNode();
        p.put("job_id", jobId);
        p.put("lease_id", leaseId.toString());
        p.put("status", status.status().name());
        // 계약: 만료 시각은 ACTIVE 에만, 이유는 REJECTED 에만 싣는다
        if (status.leaseExpiresAt() != null) {
            p.put("lease_expires_at", status.leaseExpiresAt().toEpochMilli());
        }
        if (status.reason() != null) {
            p.put("reason", status.reason().name());
        }
        return I1Envelope.outbound(mapper, now.toEpochMilli(), "job_lease_status", LEASE_STATUS_VERSION, p);
    }

    /** 대여 하나(job_request 의 값). deadlineAt 은 이벤트 마감이 없으면 null */
    record LeasedJob(String jobId, UUID leaseId, int attempt, int maxAttempts, String jobType, String priority,
            long timeoutMs, Instant leasedAt, Instant leaseExpiresAt, int heartbeatIntervalMs, JsonNode input,
            Instant deadlineAt) {
    }
}
