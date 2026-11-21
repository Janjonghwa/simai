package com.ssafy.simai.domain.watch;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;

/**
 * 사람·워치 표시 이름 조회·변경 REST API(S15P31A101-658, S15P31A101-835).
 * <ul>
 *   <li>GET /api/people/display-names?date=YYYY-MM-DD: 지난 기록용 시각별 사람 표시 이름 조회(읽기 전용, 로그인한 모든 역할)</li>
 *   <li>PUT /api/watches/{deviceId}/display-name {"display_name"}: 워치 기기 표시 이름 지정(현장 단위 지속, 권장 경로, 관리자)</li>
 *   <li>DELETE /api/watches/{deviceId}/display-name: 워치 기기 표시 이름 비움(이력 행 CLEAR 추가, 관리자)</li>
 *   <li>PUT /api/people/{worldId}/display-name {"display_name"}: 현재 World ID 에 당일 표시 이름 지정(병합 따라감, 그날 한정, 관리자)</li>
 *   <li>DELETE /api/people/{worldId}/display-name: 현재 World ID 당일 표시 이름 비움(이력 행 CLEAR 추가, 관리자)</li>
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

    record HistoricalDisplayNameItem(
            @JsonProperty("world_id") String worldId,
            @JsonProperty("display_name") String displayName,
            @JsonProperty("name_source") String nameSource,
            @JsonProperty("from_ms") long fromMs,
            @JsonProperty("to_ms") long toMs) {
    }

    record HistoricalDisplayNamesResponse(
            @JsonProperty("site_id") String siteId,
            @JsonProperty("date") String date,
            @JsonProperty("items") List<HistoricalDisplayNameItem> items) {
    }

    private final PersonDisplayNames displayNames;

    PersonDisplayNameController(PersonDisplayNames displayNames) {
        this.displayNames = displayNames;
    }

    @GetMapping("/api/people/display-names")
    HistoricalDisplayNamesResponse listForDate(@RequestParam(name = "date", required = false) String dateRaw) {
        LocalDate date = parseDate(dateRaw);
        PersonDisplayNames.HistoricalSnapshot snapshot = displayNames.historicalForCurrentSite(date);
        List<HistoricalDisplayNameItem> items = snapshot.items().stream()
                .map(i -> new HistoricalDisplayNameItem(
                        i.worldId(), i.displayName(), i.nameSource(), i.fromMs(), i.toMs()))
                .toList();
        return new HistoricalDisplayNamesResponse(snapshot.siteId(), snapshot.date().toString(), items);
    }

    private static LocalDate parseDate(String raw) {
        if (raw == null || !raw.matches("\\d{4}-\\d{2}-\\d{2}")) {
            throw new BusinessException(ErrorCode.Watch.INVALID_DISPLAY_NAME_DATE);
        }
        try {
            return LocalDate.parse(raw);
        } catch (DateTimeParseException e) {
            throw new BusinessException(ErrorCode.Watch.INVALID_DISPLAY_NAME_DATE);
        }
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
