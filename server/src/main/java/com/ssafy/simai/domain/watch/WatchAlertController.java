package com.ssafy.simai.domain.watch;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.watch.WatchAlertService.AlertRow;
import com.ssafy.simai.domain.watch.WatchAlertService.ManualRequest;
import java.time.Instant;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 워치 수동 알림·알림 조회 API(S15P31A101-475, 확장 부록 C.6, FR-45·46). 명세 밖 운영 입구라 계약(contracts/)에 두지 않는다(수동 배정과 같다).
 * <ul>
 *   <li>POST /api/watch-alerts(관리자·반장) {"device_id","severity","word","direction"?,"ack_within_ms"?,"needs_check"?}: 워치 하나에 바로 보내고
 *       남긴 행을 돌려준다. 등록 안 된 워치 404, 연결 안 됨 409(WATCH_NOT_CONNECTED), 값 오류 400(INVALID_ALERT)
 *   <li>GET /api/watch-alerts?device_id=&limit=(열람 이상): 최근 알림과 진동·확인·격상 시각. 시연·시험에서 ACK 가 돌아왔는지 본다
 * </ul>
 * 사람 이름은 싣지 않는다(기기 ID·익명 World ID·보낸 사용자 ID, 11.4-5).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WatchAlertController {

    static final String API = "/api/watch-alerts";
    static final int DEFAULT_LIMIT = 20;

    record SendRequest(@JsonProperty("device_id") String deviceId, String severity, String word, String direction,
            @JsonProperty("ack_within_ms") Integer ackWithinMs, @JsonProperty("needs_check") Boolean needsCheck) {
    }

    record AlertResponse(@JsonProperty("alert_id") long alertId, @JsonProperty("alert_msg_id") String alertMsgId, String origin,
            @JsonProperty("event_id") String eventId, @JsonProperty("device_id") String deviceId, @JsonProperty("world_id") String worldId,
            String severity, @JsonProperty("vibration_pattern") String vibrationPattern, String direction, String word,
            @JsonProperty("needs_check") boolean needsCheck, @JsonProperty("ack_within_ms") int ackWithinMs,
            @JsonProperty("sent_at") long sentAt, @JsonProperty("acked_at") Long ackedAt, @JsonProperty("ack_received_at") Long ackReceivedAt,
            @JsonProperty("confirmed_at") Long confirmedAt, @JsonProperty("confirm_received_at") Long confirmReceivedAt,
            @JsonProperty("escalated_at") Long escalatedAt, @JsonProperty("escalation_reason") String escalationReason,
            @JsonProperty("requested_by_user_id") Long requestedByUserId) {
    }

    private final WatchAlertService alerts;

    WatchAlertController(WatchAlertService alerts) {
        this.alerts = alerts;
    }

    @PostMapping(API)
    @PreAuthorize("hasAnyRole('FOREMAN', 'ADMIN')")
    AlertResponse send(@RequestBody SendRequest request, @AuthenticationPrincipal Jwt jwt) {
        ManualRequest manual = request == null ? new ManualRequest(null, null, null, null, null, null)
                : new ManualRequest(request.deviceId(), request.severity(), request.word(), request.direction(), request.ackWithinMs(),
                        request.needsCheck());
        return response(alerts.sendManual(manual, Long.parseLong(jwt.getSubject())));
    }

    @GetMapping(API)
    @PreAuthorize("hasAnyRole('VIEWER', 'FOREMAN', 'ADMIN')")
    List<AlertResponse> list(@RequestParam(name = "device_id", required = false) String deviceId,
            @RequestParam(defaultValue = "" + DEFAULT_LIMIT) int limit) {
        return alerts.list(deviceId == null || deviceId.isBlank() ? null : deviceId, limit).stream().map(this::response).toList();
    }

    private AlertResponse response(AlertRow r) {
        return new AlertResponse(r.alertId(), r.alertMsgId().toString(), r.origin(), r.eventId().toString(), r.deviceId(), r.worldId(),
                r.severity(), r.vibrationPattern(), r.direction(), r.word(), r.needsCheck(), r.ackWithinMs(), r.sentAt().toEpochMilli(),
                millis(r.ackedAt()), millis(r.ackReceivedAt()), millis(r.confirmedAt()), millis(r.confirmReceivedAt()),
                millis(r.escalatedAt()), r.escalationReason(), r.requestedByUserId());
    }

    private static Long millis(Instant instant) {
        return instant == null ? null : instant.toEpochMilli();
    }
}
