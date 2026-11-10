package com.ssafy.simai.domain.layout;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;
import java.util.List;

@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupZoneLayoutController {
    private final SetupZoneLayoutService service;
    SetupZoneLayoutController(SetupZoneLayoutService service) { this.service = service; }

    @GetMapping("/api/work-units/{workId}/layout/zones")
    List<String> list(@PathVariable long workId) { return service.list(workId); }

    @PutMapping("/api/work-units/{workId}/layout/zones/{zoneId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode add(@PathVariable long workId, @PathVariable String zoneId, @AuthenticationPrincipal Jwt jwt) {
        return service.add(workId, zoneId, Long.parseLong(jwt.getSubject()));
    }
}
