package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.domain.gpu.GpuSessionService.Registered;
import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.util.List;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.CacheControl;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * GPU 세션 API(S15P31A101-699, 계약 messages/gpu_session_register·_result·report).
 * <ul>
 * <li>POST /api/gpu/sessions (부팅 비밀값): gpu_session_register → 201 gpu_session_register_result(토큰 원문 한 번만, Cache-Control no-store).</li>
 * <li>POST /api/gpu/sessions/{id}/reports (부팅 비밀값): gpu_session_report → 204. 끝난 세션의 진행 보고는 409 GPU_SESSION_ENDED.</li>
 * <li>GET /api/gpu/sessions/current (관리자): 가장 최근 세션의 단계·연결·경과·GPU·지역(계약 밖 조회 응답, 없으면 session null)과
 * 게이트웨이 자동 전환의 마지막 상태(gateway, S15P31A101-701).</li>
 * </ul>
 * 본문은 계약 봉투 그대로이고 스키마로 검사한다(틀리면 400). 봉투 source_id 는 부팅 비밀값 ID 와 같아야 한다.
 * 요청·응답 본문은 로그에 남기지 않는다(응답에 토큰 원문이 있다).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class GpuSessionController {

    private static final Logger log = LoggerFactory.getLogger(GpuSessionController.class);
    private static final int MESSAGE_MAJOR = 1;
    static final String RESULT_SCHEMA_VERSION = "1.0.0";

    private final GpuSessionService sessions;
    private final GpuStartConfig startConfig;
    private final GpuSessionSweeper sweeper;
    private final GatewaySwitchStates gatewayStates;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;

    GpuSessionController(GpuSessionService sessions, GpuStartConfig startConfig, GpuSessionSweeper sweeper,
            GatewaySwitchStates gatewayStates, ContractSchemas schemas, ObjectMapper mapper, Clock clock) {
        this.sessions = sessions;
        this.startConfig = startConfig;
        this.sweeper = sweeper;
        this.gatewayStates = gatewayStates;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
    }

    @PostMapping(GpuPaths.SESSIONS)
    ResponseEntity<JsonNode> register(@RequestBody JsonNode body, @AuthenticationPrincipal String bootId) {
        I1Envelope envelope = read(body, "gpu_session_register", bootId);
        // 기동 설정을 먼저 만든다. 여기서 실패하면 이전 세션·토큰을 건드리지 않는다
        ObjectNode start = startConfig.build();
        Registered registered = sessions.register(bootId, envelope.msgId(), envelope.payload());
        log.info("GPU 세션 {} 등록(부팅 비밀값 {}, 워커 {}, 밀린 세션 {})", registered.sessionId(), bootId, registered.workerId(),
                registered.superseded());
        ObjectNode payload = mapper.createObjectNode();
        payload.put("session_id", registered.sessionId().toString());
        payload.put("registered_at", registered.registeredAt().toEpochMilli());
        ArrayNode superseded = payload.putArray("superseded_session_ids");
        registered.superseded().forEach(id -> superseded.add(id.toString()));
        ObjectNode worker = payload.putObject("worker");
        worker.put("worker_id", registered.workerId());
        worker.put("token", registered.workerToken());
        ObjectNode tier0 = payload.putObject("tier0");
        tier0.put("gateway_id", registered.gatewayId());
        tier0.put("token", registered.tier0Token());
        ObjectNode report = payload.putObject("report");
        report.put("interval_ms", sessions.reportInterval().toMillis());
        report.put("stale_after_ms", sessions.staleAfter().toMillis());
        payload.set("start", start);
        ObjectNode result = I1Envelope.outbound(mapper, registered.registeredAt().toEpochMilli(), "gpu_session_register_result",
                RESULT_SCHEMA_VERSION, payload);
        return ResponseEntity.status(HttpStatus.CREATED).cacheControl(CacheControl.noStore()).body(result);
    }

    @PostMapping("/api/gpu/sessions/{sessionId}/reports")
    ResponseEntity<Void> report(@PathVariable String sessionId, @RequestBody JsonNode body, @AuthenticationPrincipal String bootId) {
        UUID id = parseId(sessionId);
        I1Envelope envelope = read(body, "gpu_session_report", bootId);
        if (!id.toString().equals(envelope.payload().get("session_id").stringValue())) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_MESSAGE);
        }
        sessions.report(bootId, id, envelope.payload());
        return ResponseEntity.noContent().build();
    }

    @GetMapping(GpuPaths.CURRENT)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode current() {
        ObjectNode body = mapper.createObjectNode();
        body.set("session", sessions.latest(sweeper::tier0Connected).orElse(null));
        // 게이트웨이 자동 전환의 마지막 상태(S15P31A101-701, 메모리, 받은 적 없으면 null): {device_id, received_at_ms, state: gateway_switch_state}
        body.set("gateway", gatewayStates.latestView().orElse(null));
        body.put("server_time_ms", clock.millis());
        return body;
    }

    /** 계약 검사·메이저·source_id 를 보고 봉투를 돌려준다. 검사 문구는 경로·규칙만 담는다(값 없음) */
    private I1Envelope read(JsonNode body, String msgType, String bootId) {
        List<String> violations = schemas.violations("messages/" + msgType, mapper.writeValueAsString(body));
        if (!violations.isEmpty()) {
            log.warn("부팅 비밀값 {} 의 {} 가 계약에 맞지 않는다: {}", bootId, msgType, violations);
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_MESSAGE);
        }
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(body);
        } catch (InvalidI1Message e) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_MESSAGE);
        }
        if (envelope.major() != MESSAGE_MAJOR) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GPU_MESSAGE);
        }
        if (!bootId.equals(envelope.sourceId())) {
            throw new BusinessException(ErrorCode.Gpu.GPU_SOURCE_MISMATCH);
        }
        return envelope;
    }

    private static UUID parseId(String raw) {
        try {
            UUID id = UUID.fromString(raw);
            if (!id.toString().equals(raw)) {
                throw new IllegalArgumentException();
            }
            return id;
        } catch (IllegalArgumentException e) {
            throw new BusinessException(ErrorCode.Gpu.GPU_SESSION_NOT_FOUND);
        }
    }
}
