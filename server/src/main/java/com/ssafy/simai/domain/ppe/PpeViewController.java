package com.ssafy.simai.domain.ppe;

import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * PPE 처음 상태(GET /api/ppe/current, 열람 이상, S15P31A101-531, 명세 12.4). 관제 앱이 STOMP 연결·재연결 때 한 번 부른다.
 * Tier 0 마다 /topic/ppe 와 같은 모양(ppe_view payload)을 하나씩 담은 배열이다. 촬영 시각이 60초 안인 상태가 없으면 빈 배열.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class PpeViewController {

    static final String API = "/api/ppe/current";

    private final PpeViewRepository views;

    PpeViewController(PpeViewRepository views) {
        this.views = views;
    }

    @GetMapping(API)
    @PreAuthorize("hasAnyRole('VIEWER', 'FOREMAN', 'ADMIN')")
    List<ObjectNode> current() {
        return views.snapshots();
    }
}
