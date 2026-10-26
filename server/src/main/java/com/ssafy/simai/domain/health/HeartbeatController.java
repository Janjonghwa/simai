package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.tier0.i1.I1Envelope;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.domain.tier0.i1.InvalidI1Message;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;

/**
 * 노트북 하트비트 수신(POST /api/devices/heartbeat, 기기 토큰, 1초, 명세 3.2·6.5·12.6, FR-134). 본문은 laptop_heartbeat 봉투 그대로이고
 * 봉투 source_id 는 토큰의 기기 ID 와 같아야 한다. 노트북은 2xx 만 성공으로 본다(gateway/site_alert heartbeat.py). 응답 본문은 없다.
 * 형식이 틀리면 400 이다. 노트북이 고칠 때까지 EC2 는 하트비트가 없는 것으로 본다(끊김).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HeartbeatController {

    private static final Set<String> LINK_STATES = Set.of("CONNECTED", "DISCONNECTED");
    private static final Set<String> SITE_DISPLAYS = Set.of("NORMAL", "CONTROL_HALTED", "REMOTE_FAULT", "AI_DELAY");

    private final HeartbeatRepository heartbeats;
    private final HealthState state;
    private final Clock clock;

    HeartbeatController(HeartbeatRepository heartbeats, HealthState state, Clock clock) {
        this.heartbeats = heartbeats;
        this.state = state;
        this.clock = clock;
    }

    @PostMapping("/api/devices/heartbeat")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    void heartbeat(@RequestBody JsonNode body, @AuthenticationPrincipal String deviceId) {
        Instant receivedAt = clock.instant();
        I1Envelope envelope;
        try {
            envelope = I1Envelope.parse(body);
            check(envelope.payload());
        } catch (InvalidI1Message e) {
            throw new BusinessException(ErrorCode.Health.INVALID_HEARTBEAT);
        }
        if (!"laptop_heartbeat".equals(envelope.msgType()) || envelope.major() != 1) {
            throw new BusinessException(ErrorCode.Health.INVALID_HEARTBEAT);
        }
        if (!deviceId.equals(envelope.sourceId())) {
            throw new BusinessException(ErrorCode.Health.HEARTBEAT_SOURCE_MISMATCH);
        }
        if (!"LAPTOP".equals(heartbeats.kindOf(deviceId))) {
            throw new BusinessException(ErrorCode.Health.NOT_A_LAPTOP);
        }
        heartbeats.insert(deviceId, envelope.sentAt(), envelope.payload(), receivedAt);
        state.recordHeartbeat(deviceId, envelope.payload(), receivedAt);
    }

    /** 계약 laptop_heartbeat 의 필수 값(EC2 가 읽는 것) */
    private static void check(JsonNode p) {
        JsonNode seq = p.get("heartbeat_seq");
        JsonNode tier0 = p.get("tier0");
        JsonNode display = p.get("site_display");
        if (seq == null || !seq.isIntegralNumber() || seq.longValue() < 0 || tier0 == null || !tier0.isObject()
                || display == null || !display.isString() || !SITE_DISPLAYS.contains(display.stringValue())) {
            throw new InvalidI1Message("heartbeat_seq·tier0·site_display");
        }
        JsonNode control = tier0.get("control");
        JsonNode cameras = tier0.get("video_cameras");
        if (control == null || !control.isString() || !LINK_STATES.contains(control.stringValue())
                || !tier0.has("last_message_at") || cameras == null || !cameras.isArray()
                || !cameras.valueStream().allMatch(JsonNode::isString)) {
            throw new InvalidI1Message("tier0");
        }
        I1Json.time(tier0, "last_message_at"); // 형식만 본다(null 허용)
    }
}
