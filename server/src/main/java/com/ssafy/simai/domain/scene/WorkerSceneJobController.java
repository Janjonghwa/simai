package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.scene.StaticReconstructSubmitService.Submitted;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 분석 워커의 사람 지운 배경 묶음 static_reconstruct 자동 등록(워커 토큰 `/api/worker/**`, S15P31A101-403).
 * 본문은 계약 static_reconstruct_submit 1.x 봉투, 응답은 static_reconstruct_submit_result(새 작업 201, 이미 있는 작업 200).
 * 형식 오류 400, 봉투 source_id 가 토큰의 워커 ID 와 다르면 401, 등록되지 않았거나 PROVISIONAL·CALIBRATED 가 아닌 보정이면 409.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WorkerSceneJobController {

    private static final Logger log = LoggerFactory.getLogger(WorkerSceneJobController.class);
    static final String RESULT_VERSION = "1.0.0";
    private static final int SUBMIT_MAJOR = 1;

    private final StaticReconstructSubmitService submits;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;

    WorkerSceneJobController(StaticReconstructSubmitService submits, ContractSchemas schemas, ObjectMapper mapper, Clock clock) {
        this.submits = submits;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
    }

    @PostMapping("/api/worker/scene-jobs/static-reconstruct")
    ResponseEntity<ObjectNode> submit(@RequestBody JsonNode body, @AuthenticationPrincipal String workerId) {
        List<String> violations = schemas.violations("messages/static_reconstruct_submit", mapper.writeValueAsString(body));
        if (!violations.isEmpty()) {
            log.warn("워커 {} 의 static_reconstruct_submit 가 계약에 맞지 않는다: {}", workerId, violations);
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(body);
        } catch (InvalidI1Message e) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        if (envelope.major() != SUBMIT_MAJOR) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        if (!workerId.equals(envelope.sourceId())) {
            throw new BusinessException(ErrorCode.Job.WORKER_SOURCE_MISMATCH);
        }
        JsonNode p = envelope.payload();
        Submitted done = submits.submit(workerId, p.get("bundle_id").stringValue(), p.get("input"));
        ObjectNode out = mapper.createObjectNode();
        out.put("bundle_id", done.bundleId());
        out.put("job_id", done.jobId());
        out.put("created", done.created());
        out.put("job_status", done.jobStatus());
        // 계약: 실패 코드가 없으면 싣지 않는다(생성 클래스는 없음과 null 을 가르지 못한다)
        if (done.errorCode() != null) {
            out.put("job_error_code", done.errorCode());
        }
        ObjectNode message = I1Envelope.outbound(mapper, clock.millis(), "static_reconstruct_submit_result", RESULT_VERSION, out);
        return ResponseEntity.status(done.created() ? HttpStatus.CREATED : HttpStatus.OK).body(message);
    }
}
