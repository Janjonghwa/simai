package com.ssafy.simai.domain.watch;

import com.fasterxml.jackson.annotation.JsonProperty;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

/**
 * 사람·워치 표시 이름 변경 REST API(관리자 전용, S15P31A101-658).
 * <ul>
 *   <li>PUT /api/watches/{deviceId}/display-name {"display_name"}: 워치 기기 표시 이름 지정(현장 단위 지속, 권장 경로)</li>
 *   <li>DELETE /api/watches/{deviceId}/display-name: 워치 기기 표시 이름 비움(이력 행 CLEAR 추가)</li>
 *   <li>PUT /api/people/{worldId}/display-name {"display_name"}: 현재 World ID 에 당일 표시 이름 지정(병합 따라감, 그날 한정)</li>
 *   <li>DELETE /api/people/{worldId}/display-name: 현재 World ID 당일 표시 이름 비움(이력 행 CLEAR 추가)</li>
 * </ul>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class PersonDisplayNameController {

    record DisplayNameRequest(@JsonProperty("display_name") String displayName) {
    }

    record WatchDisplayNameResponse(
            @JsonProperty("device_id") String deviceId,
            @JsonProperty("site_id") String siteId,
            @JsonProperty("display_name") String displayName,
            @JsonProperty("changed_by_user_id") long changedByUserId,
            @JsonProperty("updated_at") long updatedAt) {
    }

    record WorldDisplayNameResponse(
            @JsonProperty("world_id") String worldId,
            @JsonProperty("site_id") String siteId,
            @JsonProperty("site_date") String siteDate,
            @JsonProperty("display_name") String displayName,
            @JsonProperty("name_source") String nameSource,
            @JsonProperty("changed_by_user_id") long changedByUserId,
            @JsonProperty("updated_at") long updatedAt) {
    }

    private final PersonDisplayNames displayNames;

    PersonDisplayNameController(PersonDisplayNames displayNames) {
        this.displayNames = displayNames;
    }

    @PutMapping("/api/watches/{deviceId}/display-name")
    @PreAuthorize("hasRole('ADMIN')")
    WatchDisplayNameResponse setWatchName(@PathVariable String deviceId,
            @RequestBody(required = false) DisplayNameRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        var saved = displayNames.setWatchDisplayName(deviceId,
                request == null ? null : request.displayName(),
                Long.parseLong(jwt.getSubject()));
        return new WatchDisplayNameResponse(saved.deviceId(), saved.siteId(), saved.displayName(),
                saved.changedByUserId(), saved.createdAt().toEpochMilli());
    }

    @DeleteMapping("/api/watches/{deviceId}/display-name")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void clearWatchName(@PathVariable String deviceId, @AuthenticationPrincipal Jwt jwt) {
        displayNames.clearWatchDisplayName(deviceId, Long.parseLong(jwt.getSubject()));
    }

    @PutMapping("/api/people/{worldId}/display-name")
    @PreAuthorize("hasRole('ADMIN')")
    WorldDisplayNameResponse setWorldName(@PathVariable String worldId,
            @RequestBody(required = false) DisplayNameRequest request,
            @AuthenticationPrincipal Jwt jwt) {
        var saved = displayNames.setPersonWorldDisplayName(worldId,
                request == null ? null : request.displayName(),
                Long.parseLong(jwt.getSubject()));
        return new WorldDisplayNameResponse(saved.worldId(), saved.siteId(), saved.siteDate().toString(),
                saved.displayName(), PersonDisplayNames.SOURCE_WORLD_MANUAL,
                saved.changedByUserId(), saved.createdAt().toEpochMilli());
    }

    @DeleteMapping("/api/people/{worldId}/display-name")
    @PreAuthorize("hasRole('ADMIN')")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void clearWorldName(@PathVariable String worldId, @AuthenticationPrincipal Jwt jwt) {
        displayNames.clearPersonWorldDisplayName(worldId, Long.parseLong(jwt.getSubject()));
    }
}
