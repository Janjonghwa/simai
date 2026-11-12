package com.ssafy.simai.domain.zone;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 중 구역 목록 GET·PUT /api/setup-sessions/{id}/zones (ADMIN, S15P31A101-612).
 * 목록을 통째로 저장하고 관제 구역에는 쓰지 않는다. 세팅 완료 때 SetupZoneApplier 가 적용한다.
 */
@RestController
@PreAuthorize("hasRole('ADMIN')")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupZonePlanController {

    private final SetupZonePlanService plans;

    SetupZonePlanController(SetupZonePlanService plans) {
        this.plans = plans;
    }

    /** 저장 본문: items 는 통째로, revision 은 마지막으로 읽은 값 */
    record PutRequest(JsonNode items, Integer revision) {
    }

    @GetMapping("/api/setup-sessions/{id}/zones")
    ObjectNode get(@PathVariable String id) {
        return plans.get(id);
    }

    @PutMapping("/api/setup-sessions/{id}/zones")
    ObjectNode put(@PathVariable String id, @RequestBody(required = false) PutRequest request, @AuthenticationPrincipal Jwt jwt) {
        return plans.put(id, request == null ? null : request.items(), request == null ? null : request.revision(),
                Long.parseLong(jwt.getSubject()));
    }

    /**
     * 구역 단계에 처음 들어올 때 목록 채우기(S15P31A101-629): 저장한 적 없는 목록만 지금 관제 구역으로 채운다(재사용은 그대로,
     * 새 모델 + World 이어짐은 needs_review=true). 이미 저장한 목록·World 끊김은 그대로 돌려준다. 응답은 GET 과 같다
     */
    @PostMapping("/api/setup-sessions/{id}/zones/seed")
    ObjectNode seed(@PathVariable String id, @AuthenticationPrincipal Jwt jwt) {
        return plans.seed(id, Long.parseLong(jwt.getSubject()));
    }
}
