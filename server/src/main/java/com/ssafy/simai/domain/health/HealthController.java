package com.ssafy.simai.domain.health;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 지금 System Health(GET /api/health, 열람, 명세 12.6, FR-5). 관제 앱이 처음 열 때·재연결 때 /topic/health 를 기다리지 않고 받는다.
 * 응답은 /topic/health 와 같은 viewer_health 메시지(봉투 포함)다(관제 앱 features/layout/api.ts).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HealthController {

    private final ViewerHealthService health;

    HealthController(ViewerHealthService health) {
        this.health = health;
    }

    @GetMapping("/api/health")
    ObjectNode current() {
        return health.message();
    }
}
