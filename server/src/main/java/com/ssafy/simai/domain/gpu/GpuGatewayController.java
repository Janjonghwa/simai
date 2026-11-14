package com.ssafy.simai.domain.gpu;

import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.util.List;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.ResponseEntity;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 게이트웨이 자동 전환(S15P31A101-701, 설계 docs/design/S15P31A101-701-gateway-auto-switch.md). 기기 토큰(/api/devices/** 기기 체인),
 * 현장 노트북(LAPTOP)만.
 * <pre>
 * GET  /api/devices/gpu-session                → gpu_session_status 1.0.0 (가장 최근 세션, 없으면 session null)
 * POST /api/devices/gpu-session/gateway-state  → 204 (본문 gateway_switch_state 1.0.0, 기기별 마지막 값만 메모리에)
 * </pre>
 * 노트북이 I0 로 접속할 주소·지문의 정본은 여전히 GET /api/endpoints/tier0 다. 이 조회는 세션이 살아 있는지와 EC2 터널이 붙을 실제 주소를 준다.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class GpuGatewayController {

    private static final Logger log = LoggerFactory.getLogger(GpuGatewayController.class);
    static final String STATUS_SCHEMA_VERSION = "1.0.0";

    private final GpuSessionService sessions;
    private final GpuSessionSweeper sweeper;
    private final GatewaySwitchStates states;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final Clock clock;

    GpuGatewayController(GpuSessionService sessions, GpuSessionSweeper sweeper, GatewaySwitchStates states, ContractSchemas schemas,
            ObjectMapper mapper, Clock clock) {
        this.sessions = sessions;
        this.sweeper = sweeper;
        this.states = states;
        this.schemas = schemas;
        this.mapper = mapper;
        this.clock = clock;
    }

    @GetMapping(GpuPaths.DEVICE_SESSION)
    ObjectNode session(@AuthenticationPrincipal String deviceId) {
        requireLaptop(deviceId);
        ObjectNode body = mapper.createObjectNode();
        body.put("schema_version", STATUS_SCHEMA_VERSION);
        body.put("server_time_ms", clock.millis());
        body.set("session", sessions.latestForGateway(sweeper::tier0Connected).orElse(null));
        return body;
    }

    @PostMapping(GpuPaths.DEVICE_GATEWAY_STATE)
    ResponseEntity<Void> gatewayState(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal String deviceId) {
        requireLaptop(deviceId);
        if (body == null) {
            throw new BusinessException(ErrorCode.Gpu.INVALID_GATEWAY_STATE);
        }
        List<String> violations = schemas.violations("messages/gateway_switch_state", mapper.writeValueAsString(body));
        if (!violations.isEmpty()) {
            // 검사 문구는 경로·규칙만 담는다(값 없음)
            log.warn("기기 {} 의 gateway_switch_state 가 계약에 맞지 않는다: {}", deviceId, violations);
            throw new BusinessException(ErrorCode.Gpu.INVALID_GATEWAY_STATE);
        }
        if (states.record(deviceId, body, clock.instant())) {
            JsonNode target = body.get("target");
            log.info("게이트웨이 {} 자동 전환: 결정 {} 결과 {} 까닭 {} 모드 {} 주인 {} 자동 {} 세션 {} 대상 {}", deviceId,
                    body.get("decision").stringValue(), body.get("switch_ok"), body.get("reason_code").stringValue(),
                    body.get("mode").stringValue(), body.get("owner").stringValue(), body.get("auto_enabled"), body.get("session_id"),
                    target == null || target.isNull() ? "-" : target.get("public_host").stringValue() + ":"
                            + target.get("control_port") + "/" + target.get("video_port"));
        }
        return ResponseEntity.noContent().build();
    }

    private void requireLaptop(String deviceId) {
        if (deviceId == null || !sessions.isLaptop(deviceId)) {
            throw new BusinessException(ErrorCode.Gpu.GPU_NOT_A_LAPTOP);
        }
    }
}
