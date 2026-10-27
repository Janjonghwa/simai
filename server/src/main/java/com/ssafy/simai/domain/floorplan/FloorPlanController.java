package com.ssafy.simai.domain.floorplan;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/** BEV 평면도 조회(열람, 명세 12.6, FR-22, S15P31A101-240). 이미지는 짧게 사는 서명 주소로 준다(14장) */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class FloorPlanController {

    private final FloorPlanService floorPlans;

    FloorPlanController(FloorPlanService floorPlans) {
        this.floorPlans = floorPlans;
    }

    @GetMapping("/api/floor-plans/current")
    ObjectNode current() {
        return floorPlans.current();
    }
}
