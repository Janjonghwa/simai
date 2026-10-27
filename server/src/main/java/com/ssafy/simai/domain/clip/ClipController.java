package com.ssafy.simai.domain.clip;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 클립(명세 12.6 GET /api/clips/{id}·PUT /api/clips/{id}/retention, 관리자). 이벤트별 목록 GET /api/events/{id}/clips 는
 * 12.6 에 없어 더했다(관제 앱 features/clips 가 이 경로를 쓴다). 필드는 계약 media_clip 과 같은 snake_case, 시각은 UTC ms.
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ClipController {

    private final ClipService clips;

    ClipController(ClipService clips) {
        this.clips = clips;
    }

    record RetentionRequest(@JsonProperty("retention_hold") Boolean retentionHold) {
    }

    @GetMapping("/api/events/{eventId}/clips")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode eventClips(@PathVariable UUID eventId) {
        return clips.eventClips(eventId);
    }

    @GetMapping("/api/clips/{clipId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode playback(@PathVariable UUID clipId, @AuthenticationPrincipal Jwt jwt) {
        return clips.playback(clipId, Long.parseLong(jwt.getSubject()));
    }

    @PutMapping("/api/clips/{clipId}/retention")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode retention(@PathVariable UUID clipId, @RequestBody(required = false) RetentionRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        return clips.setRetention(clipId, request == null ? null : request.retentionHold(), Long.parseLong(jwt.getSubject()));
    }
}
