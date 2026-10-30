package com.ssafy.simai.domain.job;

import com.ssafy.simai.contracts.JobCallbackPayload.JobType;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * I5 분석 작업 등록·취소(명세 12.5, 3.2, S15P31A101-229). 등록한 작업은 analysis_job 에 QUEUED 로 두고 워커가 대여해 간다.
 * 우선순위는 작업 유형으로 정한다(계약 job_request: vlm_rationale 는 VLM_FAST, 보고서·사후 요약은 REPORT, 그 밖은 BACKGROUND).
 * 입력은 등록할 때 계약으로 검사한다. 대여 때 나갈 job_request 를 그대로 만들어 보므로 워커가 입력 오류로 실패하지 않게 한다.
 * 작업을 등록하는 기능(TBM·VLM 근거·평면도 등)이 이 서비스를 부른다. 관리 명령 admin job 으로도 넣을 수 있다.
 */
@Service
public class AnalysisJobService {

    private static final Logger log = LoggerFactory.getLogger(AnalysisJobService.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ContractSchemas schemas;
    private final JobSignal signal;
    private final Clock clock;
    private final ApplicationEventPublisher events;

    public AnalysisJobService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, ContractSchemas schemas, JobSignal signal,
            Clock clock, ApplicationEventPublisher events) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.schemas = schemas;
        this.signal = signal;
        this.clock = clock;
        this.events = events;
    }

    /**
     * @param maxAttempts 최대 시도 수(반납은 세지 않는다). 재할당하지 않는 작업은 1
     * @param timeoutMs 시도 하나의 실행 시간 상한(대여 시각부터)
     * @param eventId 이벤트 관련 작업이면 그 이벤트, 아니면 null
     * @param requestedBy 요청한 사용자, 없으면 null
     */
    public record JobSpec(String jobType, JsonNode input, int maxAttempts, int timeoutMs, UUID eventId, Long requestedBy) {
    }

    /** @return 새 job_id */
    public String enqueue(JobSpec spec) {
        String priority = priorityOf(spec.jobType());
        if (spec.maxAttempts() < 1 || spec.timeoutMs() < 1 || spec.input() == null || !spec.input().isObject()) {
            throw new BusinessException(ErrorCode.Job.INVALID_JOB);
        }
        String jobId = "job-" + UUID.randomUUID();
        Instant now = clock.instant();
        // 첫 대여 때 나갈 job_request 를 계약으로 검사한다(유형별 입력 형식·우선순위 조건 포함)
        JobMessages.LeasedJob sample = JobMessages.newLease(jobId, 1, spec.maxAttempts(), spec.jobType(), priority,
                spec.timeoutMs(), now, spec.input());
        List<String> violations = schemas.violations("messages/job_request",
                mapper.writeValueAsString(JobMessages.jobRequest(mapper, now, sample)));
        if (!violations.isEmpty()) {
            log.warn("작업 입력이 계약에 맞지 않아 등록하지 않는다: job_type={} {}", spec.jobType(), violations);
            throw new BusinessException(ErrorCode.Job.INVALID_JOB);
        }
        jdbc.update("""
                INSERT INTO analysis_job (job_id, job_type, priority, status, attempt, max_attempts, timeout_ms, input, event_id,
                    requested_by, created_at, deadline_at)
                VALUES (:jobId, :jobType, :priority, 'QUEUED', 1, :maxAttempts, :timeoutMs, CAST(:input AS jsonb), :eventId,
                    :requestedBy, :now, :deadline)
                """, new MapSqlParameterSource().addValue("jobId", jobId).addValue("jobType", spec.jobType())
                .addValue("priority", priority).addValue("maxAttempts", spec.maxAttempts()).addValue("timeoutMs", spec.timeoutMs())
                .addValue("input", mapper.writeValueAsString(spec.input())).addValue("eventId", spec.eventId())
                .addValue("requestedBy", spec.requestedBy()).addValue("now", I1Json.utc(now))
                .addValue("deadline", "vlm_rationale".equals(spec.jobType()) && spec.eventId() != null
                        ? I1Json.utc(Instant.ofEpochMilli(spec.input().path("observation_time").longValue()).plusMillis(5000)) : null));
        signal.wake();
        return jobId;
    }

    /**
     * 끝나지 않은 작업을 취소한다. 대여 중이면 워커의 다음 하트비트·제출이 REJECTED(JOB_CANCELLED)를 받고 멈춘다.
     * @return 이번에 취소했으면 true, 이미 끝난 작업이면 false
     */
    /** 취소와 그 알림(JobFinished)을 한 트랜잭션으로 묶는다. 듣는 쪽이 실패하면 취소도 되돌아간다 */
    @Transactional
    public boolean cancel(String jobId) {
        List<String> cancelled = jdbc.queryForList("""
                UPDATE analysis_job SET status = 'CANCELLED', finished_at = :now WHERE job_id = :jobId AND status IN ('QUEUED', 'LEASED')
                RETURNING job_type
                """, new MapSqlParameterSource().addValue("jobId", jobId)
                .addValue("now", I1Json.utc(clock.instant())), String.class);
        cancelled.forEach(type -> events.publishEvent(new JobFinished(jobId, type, "CANCELLED", null, null)));
        int rows = cancelled.size();
        if (rows == 0 && jdbc.queryForObject("SELECT count(*) FROM analysis_job WHERE job_id = :jobId",
                new MapSqlParameterSource("jobId", jobId), Integer.class) == 0) {
            throw new BusinessException(ErrorCode.Job.JOB_NOT_FOUND);
        }
        return rows > 0;
    }

    static String priorityOf(String jobType) {
        JobType type;
        try {
            type = JobType.fromValue(jobType);
        } catch (IllegalArgumentException | NullPointerException e) {
            throw new BusinessException(ErrorCode.Job.INVALID_JOB);
        }
        return switch (type) {
            case VLM_RATIONALE -> "VLM_FAST";
            case TBM_REPORT, INCIDENT_SUMMARY -> "REPORT";
            default -> "BACKGROUND";
        };
    }
}
