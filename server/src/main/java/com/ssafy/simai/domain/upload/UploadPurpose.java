package com.ssafy.simai.domain.upload;

import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * 업로드 목적(upload_grant.purpose, 명세 12.6). 목적마다 받는 형식·최대 크기·키 앞부분·요청할 수 있는 주체가 정해져 있다.
 * 이벤트 프레임·클립은 Tier 0 가 올리고(계약 event_frame_ready·media_clip), 조치 사진은 반장이 올린다(FR-41).
 */
enum UploadPurpose {
    // VLM 근거 입력 프레임 한 장(event_frame_ready.frames[].frame_uri)
    EVENT_FRAME("event-frames", Map.of("image/jpeg", ".jpg", "image/png", ".png"), 16L << 20,
            Set.of(Requester.Kind.TIER0, Requester.Kind.DEVICE)),
    // 이벤트 클립(재인코딩 없는 H.264 기본 스트림, ai/services/tier0/clips/media.py). 수십 초 분량
    EVENT_CLIP("event-clips", Map.of("video/h264", ".h264", "video/mp4", ".mp4"), 256L << 20,
            Set.of(Requester.Kind.TIER0, Requester.Kind.DEVICE)),
    // 조치 완료 사진(반장 PWA, FR-41)
    TICKET_PHOTO("ticket-photos", Map.of("image/jpeg", ".jpg", "image/png", ".png", "image/webp", ".webp"), 20L << 20,
            Set.of(Requester.Kind.USER));

    private final String prefix;
    private final Map<String, String> extensions;
    private final long maxBytes;
    private final Set<Requester.Kind> requesters;

    UploadPurpose(String prefix, Map<String, String> extensions, long maxBytes, Set<Requester.Kind> requesters) {
        this.prefix = prefix;
        this.extensions = extensions;
        this.maxBytes = maxBytes;
        this.requesters = requesters;
    }

    static Optional<UploadPurpose> parse(String value) {
        for (UploadPurpose purpose : values()) {
            if (purpose.name().equals(value)) {
                return Optional.of(purpose);
            }
        }
        return Optional.empty();
    }

    String prefix() {
        return prefix;
    }

    /** 받는 형식이면 키 확장자, 아니면 빈 값 */
    Optional<String> extension(String contentType) {
        return Optional.ofNullable(extensions.get(contentType));
    }

    long maxBytes() {
        return maxBytes;
    }

    boolean allows(Requester.Kind kind) {
        return requesters.contains(kind);
    }
}
