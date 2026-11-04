package com.ssafy.simai.domain.scene;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 3D 배경(Scene) 재생성 REST 컨트롤러(S15P31A101-428).
 *
 * <ul>
 *   <li>{@code POST /api/scenes/regenerate} (ADMIN 전용) → 202 {@code {"request_id", "status"}} 또는 409</li>
 *   <li>{@code GET /api/scenes/regenerate/{requestId}} (Viewer 이상) → 진행 상태 조회</li>
 *   <li>{@code GET /api/scenes/regenerate?latest=1} (Viewer 이상) → 가장 최근 요청 조회(없으면 204)</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneRegenerateController {

    private final SceneRegenerateService service;

    public SceneRegenerateController(SceneRegenerateService service) {
        this.service = service;
    }

    @PostMapping("/api/scenes/regenerate")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.ACCEPTED)
    public ObjectNode requestRegenerate(
            @RequestBody(required = false) JsonNode body,
            @AuthenticationPrincipal Jwt jwt) {
        long userId = Long.parseLong(jwt.getSubject());
        return service.requestRegenerate(userId, body);
    }

    @GetMapping("/api/scenes/regenerate/{requestId}")
    public ObjectNode getById(@PathVariable long requestId) {
        return service.getById(requestId);
    }

    @GetMapping("/api/scenes/regenerate")
    public ResponseEntity<ObjectNode> getLatest(
            @RequestParam(name = "latest", required = false, defaultValue = "1") String latest) {
        if (!"1".equals(latest) && !"true".equalsIgnoreCase(latest)) {
            throw new BusinessException(ErrorCode.Scene.INVALID_REGENERATE_REQUEST);
        }
        return service.findLatest()
                .map(ResponseEntity::ok)
                .orElseGet(() -> ResponseEntity.noContent().build());
    }
}
