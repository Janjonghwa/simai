package com.ssafy.simai.domain.job;

import com.ssafy.simai.domain.job.JobMessages.LeasedJob;
import com.ssafy.simai.domain.job.LeaseStatus.Reason;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * I5 작업 대여·연장·제출·만료(명세 12.5, 계약 job_request 3.x·job_callback 2.x·job_lease_status, S15P31A101-229).
 * <ul>
 * <li>대여: QUEUED 작업 하나를 행 잠금(FOR UPDATE SKIP LOCKED)으로 골라 LEASED 로 바꾸고 job_lease 를 만든다.
 * 두 워커가 동시에 대여해도 같은 작업이 두 번 나가지 않는다. 순서는 VLM_FAST > REPORT > BACKGROUND, 같은 우선순위는 먼저 넣은 것.</li>
 * <li>하트비트: 유효한 대여면 만료 시각을 받은 시각 + 유지 시간으로 늘린다. leased_at + timeout_ms 가 지났으면 작업을 TIMED_OUT 으로 끝낸다.</li>
 * <li>제출: 이미 반영한 대여(SUBMITTED·RELEASED)는 만료 검사보다 먼저 DUPLICATE. 유효한 대여만 반영한다.
 * 반납(error.consume_attempt false)은 시도를 쓰지 않고 같은 attempt 로 다시 대기, 재시도 가능한 실패는 시도가 남으면 attempt + 1 로 다시 대기.</li>
 * <li>만료: 하트비트도 제출도 없이 만료 시각이 지난 대여는 EXPIRED. 시도가 남고 실행 상한 전이면 attempt + 1 로 다시 대기, 아니면 TIMED_OUT.</li>
 * </ul>
 * 잠금 순서는 언제나 job_lease → analysis_job 이다(대여는 새 job_lease 를 만들 뿐 기존 대여를 잠그지 않는다).
 */
@Service
class JobLeaseService {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final JobSignal signal;
    private final ApplicationEventPublisher events;

    JobLeaseService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, JobSignal signal, ApplicationEventPublisher events) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.signal = signal;
        this.events = events;
    }

    /** 대여 요청을 보낸 워커의 마지막 접속 시각과 처리 가능 유형(db-tables worker.job_types: 마지막 대여 요청의 값) */
    void touchWorker(String workerId, List<String> jobTypes, Instant now) {
        jdbc.update("UPDATE worker SET last_seen_at = :now, job_types = CAST(:types AS text[]) WHERE worker_id = :workerId",
                new MapSqlParameterSource().addValue("workerId", workerId).addValue("types", textArray(jobTypes))
                        .addValue("now", I1Json.utc(now)));
    }

    @Transactional
    Optional<LeasedJob> tryLease(String workerId, List<String> jobTypes, Instant now) {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                WITH next AS (
                    SELECT job_id FROM analysis_job
                    WHERE status = 'QUEUED' AND job_type = ANY(CAST(:types AS text[]))
                        AND (deadline_at IS NULL OR deadline_at > :now)
                    ORDER BY CASE priority WHEN 'VLM_FAST' THEN 0 WHEN 'REPORT' THEN 1 ELSE 2 END, created_at, job_id
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED)
                UPDATE analysis_job j SET status = 'LEASED' FROM next WHERE j.job_id = next.job_id
                RETURNING j.job_id, j.job_type, j.priority, j.attempt, j.max_attempts, j.timeout_ms, j.input::text AS input,
                    j.deadline_at
                """, new MapSqlParameterSource("types", textArray(jobTypes)).addValue("now", I1Json.utc(now)));
        if (rows.isEmpty()) {
            return Optional.empty();
        }
        Map<String, Object> row = rows.getFirst();
        LeasedJob job = JobMessages.newLease((String) row.get("job_id"), (Integer) row.get("attempt"),
                (Integer) row.get("max_attempts"), (String) row.get("job_type"), (String) row.get("priority"),
                (Integer) row.get("timeout_ms"), now, mapper.readTree((String) row.get("input")),
                instant(row.get("deadline_at")));
        jdbc.update("""
                INSERT INTO job_lease (lease_id, job_id, worker_id, attempt, leased_at, lease_expires_at, heartbeat_interval_ms, status)
                VALUES (:leaseId, :jobId, :workerId, :attempt, :leasedAt, :expiresAt, :interval, 'ACTIVE')
                """, new MapSqlParameterSource().addValue("leaseId", job.leaseId()).addValue("jobId", job.jobId())
                .addValue("workerId", workerId).addValue("attempt", job.attempt()).addValue("leasedAt", I1Json.utc(now))
                .addValue("expiresAt", I1Json.utc(job.leaseExpiresAt())).addValue("interval", job.heartbeatIntervalMs()));
        events.publishEvent(new JobStateChanged(job.jobId(), job.jobType(), "LEASED"));
        return Optional.of(job);
    }

    /**
     * 내준 job_request 를 워커에 쓰지 못한 대여를 되돌린다(long-poll 중 워커가 끊김). 워커가 받지 못한 대여라 시도를 쓰지 않고
     * 같은 attempt 로 다시 대기한다. 이 대여의 하트비트·제출은 LEASE_EXPIRED 로 거부된다.
     */
    @Transactional
    void unlease(UUID leaseId) {
        jdbc.query("""
                UPDATE job_lease SET status = 'REJECTED', reject_reason = 'LEASE_EXPIRED' WHERE lease_id = :leaseId AND status = 'ACTIVE'
                RETURNING job_id
                """, new MapSqlParameterSource("leaseId", leaseId), (rs, i) -> rs.getString("job_id"))
                .forEach(jobId -> jdbc.queryForList("UPDATE analysis_job SET status = 'QUEUED' WHERE job_id = :jobId AND status = 'LEASED' RETURNING job_type",
                        new MapSqlParameterSource("jobId", jobId), String.class)
                        .forEach(type -> events.publishEvent(new JobStateChanged(jobId, type, "QUEUED"))));

        signal.wake();
    }

    @Transactional
    LeaseStatus heartbeat(String workerId, String jobId, UUID leaseId, Double progress, Instant now) {
        return heartbeat(workerId, jobId, leaseId, progress, null, now);
    }

    @Transactional
    LeaseStatus heartbeat(String workerId, String jobId, UUID leaseId, Double progress, JsonNode siteSetup, Instant now) {
        Lease lease = lockLease(leaseId);
        if (lease == null || !lease.owns(workerId, jobId)) {
            return LeaseStatus.rejected(Reason.UNKNOWN_LEASE);
        }
        Job job = lockJob(jobId);
        LeaseStatus closed = closedOrExpire(lease, job, now);
        if (closed != null) {
            return closed;
        }
        if (!now.isBefore(lease.leasedAt().plusMillis(job.timeoutMs()))) {
            rejectLease(leaseId, Reason.JOB_TIMED_OUT);
            finish(jobId, "TIMED_OUT", null, error("JOB_TIMED_OUT", "실행 시간 상한(timeout_ms)이 지난 뒤의 하트비트"), null,
                    I1Json.utc(now));
            return LeaseStatus.rejected(Reason.JOB_TIMED_OUT);
        }
        if (siteSetup != null && !siteSetup.isNull()) {
            if (!"site_setup".equals(job.jobType())) {
                throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
            }
            events.publishEvent(new JobProgress(jobId, leaseId, siteSetup));
        }
        Instant expiresAt = now.plusMillis(JobMessages.LEASE_HOLD_MS);
        jdbc.update("""
                UPDATE job_lease SET lease_expires_at = :expiresAt, last_heartbeat_at = :now, progress = :progress
                WHERE lease_id = :leaseId
                """, new MapSqlParameterSource().addValue("leaseId", leaseId).addValue("expiresAt", I1Json.utc(expiresAt))
                .addValue("now", I1Json.utc(now)).addValue("progress", progress));
        LeaseStatus active = LeaseStatus.active(expiresAt);
        if ("site_setup".equals(job.jobType())) {
            // 같은 현장에 진행 중 작업이 있으면 정식 굽기를 회수하라고 알린다(명세 3.2 5항, job_lease_status 2.1.0, -597).
            // 세팅은 진행 중 작업이 없을 때만 시작하므로(-467 409) true 는 세팅 도중 작업이 시작됐다는 뜻이다
            return active.withSiteSetupSignal(JobMessages.SITE_SETUP_HEARTBEAT_INTERVAL_MS, liveWorkAtJobSite(jobId));
        }
        return active;
    }

    /** site_setup 입력의 현장에 IN_PROGRESS 작업이 있는가 */
    private boolean liveWorkAtJobSite(String jobId) {
        Boolean live = jdbc.queryForObject("""
                SELECT EXISTS (
                    SELECT 1 FROM analysis_job j JOIN work_unit w ON w.site_id = j.input ->> 'site_id'
                    WHERE j.job_id = :jobId AND w.status = 'IN_PROGRESS'
                )
                """, new MapSqlParameterSource("jobId", jobId), Boolean.class);
        return Boolean.TRUE.equals(live);
    }

    /** @param p job_callback payload(계약 검사를 마친 것) */
    @Transactional
    LeaseStatus submit(String workerId, String jobId, JsonNode p, Instant now) {
        UUID leaseId = UUID.fromString(p.get("lease_id").stringValue());
        Lease lease = lockLease(leaseId);
        if (lease == null || !lease.owns(workerId, jobId)) {
            return LeaseStatus.rejected(Reason.UNKNOWN_LEASE);
        }
        // 이미 반영한 대여의 재전송은 대여가 끝난 뒤라도 처음 것을 두고 DUPLICATE(만료 검사보다 먼저)
        if ("SUBMITTED".equals(lease.status()) || "RELEASED".equals(lease.status())) {
            return LeaseStatus.duplicate();
        }
        Job job = lockJob(jobId);
        if (!job.jobType().equals(p.get("job_type").stringValue())) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        LeaseStatus closed = closedOrExpire(lease, job, now);
        if (closed != null) {
            return closed;
        }
        // 실행 상한(leased_at + timeout_ms)이 지났어도 대여가 유효한 동안 온 제출은 받는다(계약 job_request 【시간 초과·재할당】).
        // 상한 뒤의 하트비트만 JOB_TIMED_OUT 이다
        JsonNode error = p.get("error");
        JsonNode consume = error == null ? null : error.get("consume_attempt");
        // 워커 시계 값이다(db-tables analysis_job.started_at·finished_at 의 출처는 job_callback)
        OffsetDateTime startedAt = I1Json.time(p, "started_at");
        OffsetDateTime finishedAt = I1Json.time(p, "finished_at");
        if (consume != null && !consume.booleanValue()) {
            // 반납: Tier 0 우선으로 돌려준 작업. 시도를 쓰지 않고 같은 attempt 로 다시 대기(마지막 시도여도 끝내지 않는다)
            closeLease(leaseId, "RELEASED", false, now);
            if (job.pastDeadline(now)) {
                finish(jobId, "TIMED_OUT", null, error("VLM_DEADLINE", "이벤트 관측부터 5초가 지났다"), startedAt, I1Json.utc(now));
                return LeaseStatus.accepted();
            }
            jdbc.update("UPDATE analysis_job SET status = 'QUEUED', error = CAST(:error AS jsonb) WHERE job_id = :jobId",
                        new MapSqlParameterSource().addValue("jobId", jobId).addValue("error", json(error)));
            events.publishEvent(new JobStateChanged(jobId, job.jobType(), "QUEUED"));
            signal.wake();
            return LeaseStatus.accepted();
        }
        closeLease(leaseId, "SUBMITTED", consume == null ? null : true, now);
        switch (p.get("status").stringValue()) {
            case "SUCCEEDED" -> finish(jobId, "SUCCEEDED", p.get("result"), null, startedAt, finishedAt);
            case "TIMED_OUT" -> finish(jobId, "TIMED_OUT", null, error, startedAt, finishedAt);
            default -> {
                // FAILED: 재시도 가능하고 시도가 남으면 새 대여로 다시 내준다
                if (error.path("retryable").asBoolean(false) && job.pastDeadline(now)) {
                    finish(jobId, "TIMED_OUT", null, error("VLM_DEADLINE", "이벤트 관측부터 5초가 지났다"), startedAt, I1Json.utc(now));
                } else if (error.path("retryable").asBoolean(false) && job.attempt() < job.maxAttempts()) {
                    jdbc.update("""
                            UPDATE analysis_job SET status = 'QUEUED', attempt = attempt + 1, error = CAST(:error AS jsonb)
                            WHERE job_id = :jobId
                            """, new MapSqlParameterSource().addValue("jobId", jobId).addValue("error", json(error)));
                    events.publishEvent(new JobStateChanged(jobId, job.jobType(), "QUEUED"));
                    signal.wake();
                } else {
                    finish(jobId, "FAILED", null, error, startedAt, finishedAt);
                }
            }
        }
        return LeaseStatus.accepted();
    }

    /** 만료 시각이 지난 ACTIVE 대여를 모두 만료 처리한다(스케줄러·대여 요청마다) */
    @Transactional
    void expireDue(Instant now) {
        List<String> queued = jdbc.queryForList("""
                SELECT job_id FROM analysis_job WHERE status = 'QUEUED' AND deadline_at <= :now
                FOR UPDATE SKIP LOCKED
                """, new MapSqlParameterSource("now", I1Json.utc(now)), String.class);
        for (String jobId : queued) {
            finish(jobId, "TIMED_OUT", null, error("VLM_DEADLINE", "이벤트 관측부터 5초가 지났다"), null, I1Json.utc(now));
        }
        List<Lease> expired = jdbc.query("""
                UPDATE job_lease SET status = 'EXPIRED' WHERE status = 'ACTIVE' AND lease_expires_at <= :now
                RETURNING lease_id, job_id, worker_id, status, leased_at, lease_expires_at, reject_reason
                """, new MapSqlParameterSource("now", I1Json.utc(now)), JobLeaseService::lease);
        for (Lease lease : expired) {
            afterExpiry(lease, lockJob(lease.jobId()), now);
        }
    }

    // ---- 내부

    /** timestamptz 열 값(드라이버에 따라 Timestamp·OffsetDateTime) → Instant, 없으면 null */
    private static Instant instant(Object value) {
        if (value instanceof java.sql.Timestamp timestamp) {
            return timestamp.toInstant();
        }
        return value instanceof OffsetDateTime time ? time.toInstant() : null;
    }

    /** 더 쓸 수 없는 대여면 그 답, 쓸 수 있으면 null. ACTIVE 인데 만료 시각이 지났으면 여기서 만료 처리한다(스케줄러보다 먼저 온 경우) */
    private LeaseStatus closedOrExpire(Lease lease, Job job, Instant now) {
        // 취소가 먼저다. 대여가 이미 만료됐어도 워커에는 취소를 알린다
        if ("CANCELLED".equals(job.status())) {
            if ("ACTIVE".equals(lease.status())) {
                rejectLease(lease.leaseId(), Reason.JOB_CANCELLED);
            }
            return LeaseStatus.rejected(Reason.JOB_CANCELLED);
        }
        if ("ACTIVE".equals(lease.status())) {
            if (!now.isBefore(lease.expiresAt())) {
                jdbc.update("UPDATE job_lease SET status = 'EXPIRED' WHERE lease_id = :leaseId",
                        new MapSqlParameterSource("leaseId", lease.leaseId()));
                afterExpiry(lease, job, now);
                return LeaseStatus.rejected(Reason.LEASE_EXPIRED);
            }
            return null;
        }
        if ("REJECTED".equals(lease.status()) && lease.rejectReason() != null) {
            return LeaseStatus.rejected(Reason.valueOf(lease.rejectReason()));
        }
        return LeaseStatus.rejected(Reason.LEASE_EXPIRED);
    }

    /** 만료된 대여의 작업: 시도가 남고 실행 상한 전이면 다음 시도로 다시 대기, 아니면 TIMED_OUT. 취소된 작업은 그대로 둔다 */
    private void afterExpiry(Lease lease, Job job, Instant now) {
        if (job == null || !"LEASED".equals(job.status())) {
            return;
        }
        boolean pastTimeout = !now.isBefore(lease.leasedAt().plusMillis(job.timeoutMs()));
        if (!pastTimeout && !job.pastDeadline(now) && job.attempt() < job.maxAttempts()) {
            jdbc.update("UPDATE analysis_job SET status = 'QUEUED', attempt = attempt + 1 WHERE job_id = :jobId",
                    new MapSqlParameterSource("jobId", lease.jobId()));
            events.publishEvent(new JobStateChanged(lease.jobId(), job.jobType(), "QUEUED"));
            signal.wake();
        } else {
            finish(lease.jobId(), "TIMED_OUT", null,
                    error("LEASE_EXPIRED", pastTimeout ? "실행 시간 상한이 지나 대여가 만료됐다" : "대여가 만료됐고 남은 시도가 없다"),
                    null, I1Json.utc(now));
        }
    }

    private void finish(String jobId, String status, JsonNode result, JsonNode error, OffsetDateTime startedAt,
            OffsetDateTime finishedAt) {
        List<String> jobType = jdbc.queryForList("""
                UPDATE analysis_job SET status = :status, result = CAST(:result AS jsonb), error = CAST(:error AS jsonb),
                    started_at = COALESCE(:startedAt, started_at), finished_at = :finishedAt
                WHERE job_id = :jobId RETURNING job_type
                """, new MapSqlParameterSource().addValue("jobId", jobId).addValue("status", status)
                .addValue("result", json(result)).addValue("error", json(error))
                .addValue("startedAt", startedAt).addValue("finishedAt", finishedAt), String.class);
        // 같은 트랜잭션에서 동기로 알린다. 듣는 쪽(예: floor_plan)이 실패하면 끝내기도 되돌아간다
        jobType.forEach(type -> events.publishEvent(new JobFinished(jobId, type, status, result, error)));
    }

    private void closeLease(UUID leaseId, String status, Boolean consumeAttempt, Instant now) {
        jdbc.update("""
                UPDATE job_lease SET status = :status, consume_attempt = :consume, submitted_at = :now WHERE lease_id = :leaseId
                """, new MapSqlParameterSource().addValue("leaseId", leaseId).addValue("status", status)
                .addValue("consume", consumeAttempt).addValue("now", I1Json.utc(now)));
    }

    private void rejectLease(UUID leaseId, Reason reason) {
        jdbc.update("UPDATE job_lease SET status = 'REJECTED', reject_reason = :reason WHERE lease_id = :leaseId",
                new MapSqlParameterSource().addValue("leaseId", leaseId).addValue("reason", reason.name()));
    }

    private Lease lockLease(UUID leaseId) {
        return jdbc.query("""
                SELECT lease_id, job_id, worker_id, status, leased_at, lease_expires_at, reject_reason FROM job_lease
                WHERE lease_id = :leaseId FOR UPDATE
                """, new MapSqlParameterSource("leaseId", leaseId), JobLeaseService::lease).stream().findFirst().orElse(null);
    }

    private Job lockJob(String jobId) {
        return jdbc.query("""
                SELECT job_type, status, attempt, max_attempts, timeout_ms, deadline_at FROM analysis_job WHERE job_id = :jobId FOR UPDATE
                """, new MapSqlParameterSource("jobId", jobId), (rs, i) -> new Job(rs.getString("job_type"),
                rs.getString("status"), rs.getInt("attempt"), rs.getInt("max_attempts"), rs.getInt("timeout_ms"),
                rs.getObject("deadline_at", OffsetDateTime.class)))
                .stream().findFirst().orElse(null);
    }

    private static Lease lease(ResultSet rs, int rowNum) throws SQLException {
        return new Lease(rs.getObject("lease_id", UUID.class), rs.getString("job_id"), rs.getString("worker_id"),
                rs.getString("status"), rs.getObject("leased_at", OffsetDateTime.class).toInstant(),
                rs.getObject("lease_expires_at", OffsetDateTime.class).toInstant(), rs.getString("reject_reason"));
    }

    private JsonNode error(String code, String message) {
        return mapper.createObjectNode().put("code", code).put("message", message);
    }

    private String json(JsonNode node) {
        return node == null || node.isNull() ? null : mapper.writeValueAsString(node);
    }

    /** 계약 job_type 값(스키마로 검사한 것)만 들어온다 */
    private static String textArray(List<String> values) {
        return "{" + String.join(",", values) + "}";
    }

    private record Lease(UUID leaseId, String jobId, String workerId, String status, Instant leasedAt, Instant expiresAt,
            String rejectReason) {

        boolean owns(String workerId, String jobId) {
            return this.workerId.equals(workerId) && this.jobId.equals(jobId);
        }
    }

    private record Job(String jobType, String status, int attempt, int maxAttempts, int timeoutMs, OffsetDateTime deadlineAt) {

        boolean pastDeadline(Instant now) {
            return deadlineAt != null && !now.isBefore(deadlineAt.toInstant());
        }
    }
}
