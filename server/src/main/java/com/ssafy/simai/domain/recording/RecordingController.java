package com.ssafy.simai.domain.recording;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Instant;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 게이트웨이 녹화 작업 구간 업로드(S15P31A101-668). 기기 토큰(/api/devices/** 기기 체인), 현장 노트북만.
 * <pre>
 * GET  /api/devices/recordings/windows?from=ms&amp;to=ms   → recording_windows 1.0.0 (범위 24시간 이내)
 * POST /api/devices/recordings/segments                → {segment_id, status: CREATED|DUPLICATE} (본문 recording_segment 1.0.0)
 * </pre>
 * 업로드 주소는 기존 POST /api/uploads/sign(purpose RECORDING_SEGMENT, segment_id)으로 받는다. 재생은 기존 GET /api/videos(관리자).
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RecordingController {

    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private final RecordingService recordings;

    RecordingController(RecordingService recordings) {
        this.recordings = recordings;
    }

    @GetMapping("/api/devices/recordings/windows")
    ObjectNode windows(@RequestParam(name = "from", required = false) String fromMs,
            @RequestParam(name = "to", required = false) String toMs, @AuthenticationPrincipal String deviceId) {
        return recordings.windows(deviceId, epochMs(fromMs), epochMs(toMs));
    }

    @PostMapping("/api/devices/recordings/progress")
    ObjectNode progress(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal String deviceId) {
        return recordings.progress(deviceId, body);
    }

    @PostMapping("/api/devices/recordings/segments")
    ObjectNode register(@RequestBody(required = false) JsonNode body, @AuthenticationPrincipal String deviceId) {
        return recordings.register(deviceId, body);
    }

    /** 없거나 숫자가 아니거나 DB 가 받지 못하는 시각이면 400(INVALID_RECORDING_QUERY) */
    private static Instant epochMs(String value) {
        try {
            long ms = Long.parseLong(value == null ? "" : value.strip());
            if (ms < 0 || ms > MAX_EPOCH_MS) {
                throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_QUERY);
            }
            return Instant.ofEpochMilli(ms);
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_QUERY);
        }
    }
}
