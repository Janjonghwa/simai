package com.ssafy.simai.domain.setup;

import java.util.Map;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import com.fasterxml.jackson.annotation.JsonProperty;
import tools.jackson.databind.node.ObjectNode;

/** 현장 세팅 REST. 조회는 인증된 열람자, 높이 조회·생성·재시도는 ADMIN(명세 12.6). */
@RestController
@RequestMapping("/api/setup-sessions")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupController {
    private final SetupService setups;

    SetupController(SetupService setups) { this.setups = setups; }

    record Create(@JsonProperty("work_unit_id") Long workUnitId,
                  @JsonProperty("camera_heights_m") Map<String, Double> cameraHeightsM) { }
    record Retry(@JsonProperty("camera_heights_m") Map<String, Double> cameraHeightsM) { }

    @GetMapping("/camera-height-defaults")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode defaults() { return setups.defaults(); }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody Create request, @AuthenticationPrincipal Jwt jwt) {
        return setups.create(request.workUnitId(), request.cameraHeightsM(), Long.parseLong(jwt.getSubject()), null);
    }

    @GetMapping("/{id}")
    ObjectNode get(@PathVariable UUID id) { return setups.get(id); }

    @PostMapping("/{id}/retry")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode retry(@PathVariable UUID id, @RequestBody(required = false) Retry request,
            @AuthenticationPrincipal Jwt jwt) {
        return setups.retry(id, request == null ? null : request.cameraHeightsM(), Long.parseLong(jwt.getSubject()));
    }
}
