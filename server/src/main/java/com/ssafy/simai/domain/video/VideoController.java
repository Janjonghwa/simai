package com.ssafy.simai.domain.video;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Instant;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 상시 영상 조회(S15P31A101-330, 관리자). 얼굴 흐림 없는 원본이라 이벤트 클립처럼 관리자만 본다(11.5·14장).
 * <pre>
 * GET /api/videos?from=ms&amp;to=ms&amp;camera_id=cam1   → {from, to, segments[]}  (주소 없음, 24시간 이내)
 * GET /api/videos/{segmentId}                     → {segment_id, url, expires_at_ms, content_type, container} (조회 기록)
 * </pre>
 */
@RestController
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class VideoController {

    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private final VideoService videos;

    VideoController(VideoService videos) {
        this.videos = videos;
    }

    @GetMapping("/api/videos")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode segments(@RequestParam(name = "from", required = false) String fromMs,
            @RequestParam(name = "to", required = false) String toMs,
            @RequestParam(name = "camera_id", required = false) String cameraId) {
        String camera = cameraId == null || cameraId.isBlank() ? null : cameraId.strip();
        return videos.segments(epochMs(fromMs), epochMs(toMs), camera);
    }

    @GetMapping("/api/videos/{segmentId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode playback(@PathVariable UUID segmentId, @AuthenticationPrincipal Jwt jwt) {
        return videos.playback(segmentId, Long.parseLong(jwt.getSubject()));
    }

    /** 없거나 숫자가 아니거나 DB 가 받지 못하는 시각이면 400(코드 INVALID_VIDEO_QUERY) */
    private static Instant epochMs(String value) {
        try {
            long ms = Long.parseLong(value == null ? "" : value.strip());
            if (ms < 0 || ms > MAX_EPOCH_MS) {
                throw new BusinessException(ErrorCode.Video.INVALID_VIDEO_QUERY);
            }
            return Instant.ofEpochMilli(ms);
        } catch (NumberFormatException e) {
            throw new BusinessException(ErrorCode.Video.INVALID_VIDEO_QUERY);
        }
    }
}
