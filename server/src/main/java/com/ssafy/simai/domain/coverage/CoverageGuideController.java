package com.ssafy.simai.domain.coverage;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 1단계 카메라 각도 헬퍼 중계 컨트롤러(S15P31A101-447, 명세 4.3·4.5·12.2·12.6·13.4·18.1).
 *
 * <ul>
 *   <li>{@code POST /api/devices/coverage-guides}(기기 토큰, {@code LAPTOP}): 게이트웨이 헬퍼 결과를 검증하고
 *       메모리에 최신 1건만 보관하며, 응답에 {@code active_demand} 를 실어 보낸다.</li>
 *   <li>{@code GET /api/devices/coverage-guides}(기기 토큰): 수요({@code active_demand})만 돌려준다(보관 없음).</li>
 *   <li>{@code GET /api/cameras/coverage-guides}(관리자): 최신 결과를 돌려주고(10 s 초과 시 {@code STALE}),
 *       마지막 관리자 조회 시각을 기록한다.</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CoverageGuideController {

    private final CoverageGuideService coverageGuides;

    public CoverageGuideController(CoverageGuideService coverageGuides) {
        this.coverageGuides = coverageGuides;
    }

    @PostMapping("/api/devices/coverage-guides")
    ObjectNode ingest(
            @RequestBody(required = false) JsonNode body,
            @AuthenticationPrincipal String deviceId) {
        return coverageGuides.ingest(body, deviceId);
    }

    /** 기기 토큰 수요 조회: {@code active_demand} 만 돌려주고 아무것도 보관하지 않는다. */
    @GetMapping("/api/devices/coverage-guides")
    ObjectNode demand(@AuthenticationPrincipal String deviceId) {
        return coverageGuides.demandFor(deviceId);
    }

    @GetMapping("/api/cameras/coverage-guides")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode currentGuides() {
        return coverageGuides.queryForAdmin();
    }
}
