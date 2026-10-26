package com.ssafy.simai.domain.tier0.endpoint;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 노트북의 Tier 0 접속 주소 조회(GET /api/endpoints/tier0, 기기 토큰, 명세 3.12·12.6, FR-138).
 * 본문은 현재 엔드포인트인 tier0_endpoint 봉투 그대로다(값을 고치지 않는다). 등록이 없으면 404(계약 설명, gateway/site_alert endpoint.py).
 * 쿼리 파라미터는 없다. 노트북은 gateway_id 를 고르지 않고, MVP 는 GPU 인스턴스가 하나라 Tier 0 도 하나다(명세 3.12).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class Tier0EndpointController {

    private final Tier0EndpointRepository endpoints;

    Tier0EndpointController(Tier0EndpointRepository endpoints) {
        this.endpoints = endpoints;
    }

    @GetMapping("/api/endpoints/tier0")
    ObjectNode current() {
        return endpoints.findCurrent().orElseThrow(() -> new BusinessException(ErrorCode.Endpoint.TIER0_ENDPOINT_NOT_REGISTERED));
    }
}
