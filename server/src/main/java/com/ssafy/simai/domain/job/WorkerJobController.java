package com.ssafy.simai.domain.job;

import com.ssafy.simai.domain.job.JobMessages.LeasedJob;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import jakarta.servlet.http.HttpServletResponse;
import java.io.IOException;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Optional;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * I5 분석 워커 API(워커 토큰, 명세 12.5, FR-101, S15P31A101-229). 본문은 계약 봉투 그대로이고 contracts/ 스키마로 검사한다(틀리면 400).
 * 봉투 source_id 는 토큰의 워커 ID 와 같아야 한다(틀리면 401, 계약 job_lease_request).
 * <ul>
 * <li>POST /api/worker/jobs/lease: long-poll. 작업이 생길 때까지 최대 wait_ms 기다리고, 있으면 200 job_request, 없으면 204.</li>
 * <li>POST /api/worker/jobs/{job_id}/heartbeat: job_heartbeat → job_lease_status.</li>
 * <li>POST /api/worker/jobs/{job_id}/result: job_callback → job_lease_status.</li>
 * </ul>
 * REJECTED 는 409 와 함께 본문 job_lease_status 로 알린다. 워커는 HTTP 상태보다 본문 status 를 따른다.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WorkerJobController {

    private static final Logger log = LoggerFactory.getLogger(WorkerJobController.class);
    /**
     * 다른 프로세스(관리 명령)가 넣은 작업·만료 재할당을 놓치지 않게 기다리는 동안 이 주기로 DB 를 다시 본다.
     * 기다리는 동안 요청 스레드 하나를 쓴다(워커 수가 적은 MVP 기준. 늘면 DeferredResult·LISTEN/NOTIFY 로 바꾼다)
     */
    private static final long POLL_SLICE_MS = 1000;
    /** 워커가 보내는 I5 메시지의 메이저(job_lease_request·job_heartbeat·job_callback 2.x) */
    private static final int WORKER_MESSAGE_MAJOR = 2;

    private final JobLeaseService leases;
    private final JobSignal signal;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;

    WorkerJobController(JobLeaseService leases, JobSignal signal, ContractSchemas schemas, ObjectMapper mapper, Clock clock) {
        this.leases = leases;
        this.signal = signal;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
    }

    /**
     * 응답은 직접 쓴다. 기다리는 동안 워커가 끊겨 job_request 를 쓰지 못하면 대여를 되돌린다(워커가 받지 못한 대여로 시도를 쓰지 않게).
     * 끊긴 연결을 쓰기 전에 알 수는 없어서, 쓰기가 실패한 경우만 잡는다. 나머지는 만료 뒤 재할당된다.
     */
    @PostMapping("/api/worker/jobs/lease")
    void lease(@RequestBody JsonNode body, @AuthenticationPrincipal String workerId, HttpServletResponse response)
            throws IOException {
        JsonNode p = read(body, "job_lease_request", workerId);
        List<String> jobTypes = p.get("job_types").valueStream().map(JsonNode::stringValue).toList();
        long deadline = System.nanoTime() + p.get("wait_ms").longValue() * 1_000_000;
        leases.touchWorker(workerId, jobTypes, now());
        // 만료 재할당은 스케줄러가 1초마다 한다. 요청 처음에 한 번 더 보아 막 만료된 작업을 바로 내준다
        leases.expireDue(now());
        try {
            while (true) {
                long seen = signal.generation();
                Instant now = now();
                Optional<LeasedJob> job = leases.tryLease(workerId, jobTypes, now);
                if (job.isPresent()) {
                    // sent_at 은 대여 시각과 같다. 워커는 lease_expires_at − sent_at 으로 남은 시간을 센다(시계 차이)
                    write(response, JobMessages.jobRequest(mapper, now, job.get()), job.get());
                    return;
                }
                long remainingMs = (deadline - System.nanoTime()) / 1_000_000;
                if (remainingMs <= 0) {
                    break;
                }
                signal.await(seen, Math.min(remainingMs, POLL_SLICE_MS));
            }
        } catch (InterruptedException e) {
            // 서버 종료 중. 워커는 204 를 받고 다시 대여를 요청한다
            Thread.currentThread().interrupt();
        }
        response.setStatus(HttpStatus.NO_CONTENT.value());
    }

    private void write(HttpServletResponse response, JsonNode message, LeasedJob job) throws IOException {
        byte[] bytes = mapper.writeValueAsBytes(message);
        response.setStatus(HttpStatus.OK.value());
        response.setContentType(MediaType.APPLICATION_JSON_VALUE);
        response.setContentLength(bytes.length);
        try {
            response.getOutputStream().write(bytes);
            response.flushBuffer();
        } catch (IOException e) {
            log.warn("작업 {} 대여 {} 를 워커에 보내지 못해 되돌린다: {}", job.jobId(), job.leaseId(), e.getMessage());
            leases.unlease(job.leaseId());
            throw e;
        }
    }

    @PostMapping("/api/worker/jobs/{jobId}/heartbeat")
    ResponseEntity<JsonNode> heartbeat(@PathVariable String jobId, @RequestBody JsonNode body,
            @AuthenticationPrincipal String workerId) {
        JsonNode p = read(body, "job_heartbeat", workerId);
        samePath(p, jobId);
        UUID leaseId = UUID.fromString(p.get("lease_id").stringValue());
        JsonNode progress = p.get("progress");
        Instant now = now();
        LeaseStatus status = leases.heartbeat(workerId, jobId, leaseId,
                progress == null || progress.isNull() ? null : progress.doubleValue(), now);
        return respond(jobId, leaseId, status, now);
    }

    @PostMapping("/api/worker/jobs/{jobId}/result")
    ResponseEntity<JsonNode> result(@PathVariable String jobId, @RequestBody JsonNode body,
            @AuthenticationPrincipal String workerId) {
        JsonNode p = read(body, "job_callback", workerId);
        samePath(p, jobId);
        Instant now = now();
        LeaseStatus status = leases.submit(workerId, jobId, p, now);
        return respond(jobId, UUID.fromString(p.get("lease_id").stringValue()), status, now);
    }

    /** 계약 검사·메이저·source_id 를 보고 payload 를 돌려준다 */
    private JsonNode read(JsonNode body, String msgType, String workerId) {
        List<String> violations = schemas.violations("messages/" + msgType, mapper.writeValueAsString(body));
        if (!violations.isEmpty()) {
            log.warn("워커 {} 의 {} 가 계약에 맞지 않는다: {}", workerId, msgType, violations);
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(body);
        } catch (InvalidI1Message e) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        if (envelope.major() != WORKER_MESSAGE_MAJOR) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        if (!workerId.equals(envelope.sourceId())) {
            throw new BusinessException(ErrorCode.Job.WORKER_SOURCE_MISMATCH);
        }
        return envelope.payload();
    }

    private static void samePath(JsonNode p, String jobId) {
        if (!jobId.equals(p.get("job_id").stringValue())) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
    }

    private ResponseEntity<JsonNode> respond(String jobId, UUID leaseId, LeaseStatus status, Instant now) {
        HttpStatus http = status.status() == LeaseStatus.Status.REJECTED ? HttpStatus.CONFLICT : HttpStatus.OK;
        return ResponseEntity.status(http).body(JobMessages.leaseStatus(mapper, now, jobId, leaseId, status));
    }

    /** 계약 시각은 UTC ms 라 DB 에도 ms 로 맞춰 둔다(job_request 의 leased_at 과 DB 값이 같게) */
    private Instant now() {
        return clock.instant().truncatedTo(ChronoUnit.MILLIS);
    }
}
