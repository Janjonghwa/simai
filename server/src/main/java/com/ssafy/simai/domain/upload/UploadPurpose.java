package com.ssafy.simai.domain.upload;

import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * 업로드 목적(upload_grant.purpose, 명세 12.6). 목적마다 받는 형식·최대 크기·키 앞부분·요청할 수 있는 주체가 정해져 있다.
 * 이벤트 프레임·클립은 Tier 0 가 올리고(계약 event_frame_ready·media_clip), 조치 사진은 반장이 올리며(FR-41),
 * 아바타 자산(glTF/GLB·텍스처·표면 출처 지도)은 분석 워커가 올린다(I6, S15P31A101-325).
 */
public enum UploadPurpose {
    // VLM 근거 입력 프레임 한 장(event_frame_ready.frames[].frame_uri)
    EVENT_FRAME("event-frames", Map.of("image/jpeg", ".jpg", "image/png", ".png"), 16L << 20,
            Set.of(Requester.Kind.TIER0, Requester.Kind.DEVICE)),
    // 이벤트 클립(형식은 ClipFormat, 클립 기록 수신과 같은 표). 수십 초 분량
    EVENT_CLIP("event-clips", ClipFormat.extensions(), 256L << 20,
            Set.of(Requester.Kind.TIER0, Requester.Kind.DEVICE)),
    // 상시 영상 조각(video_segment, S15P31A101-330). 카메라 하나 × 1분 남짓이라 클립보다 작게 잡는다. Tier 0 만 올린다
    VIDEO_SEGMENT("video-segments", ClipFormat.extensions(), 128L << 20, Set.of(Requester.Kind.TIER0)),
    // 조치 완료 사진(반장 PWA, FR-41)
    TICKET_PHOTO("ticket-photos", Map.of("image/jpeg", ".jpg", "image/png", ".png", "image/webp", ".webp"), 20L << 20,
            Set.of(Requester.Kind.USER)),
    // 관리자가 올리는 정적 배경 Scene glTF(GLB 한 파일, S15P31A101-322). 교실 메시는 수십~수백 MB 라 클립보다 크게 잡는다
    SCENE_GLTF("scenes", Map.of("model/gltf-binary", ".glb"), 512L << 20, Set.of(Requester.Kind.ADMIN)),
    // EXP-02 아바타 메시(GLB·glTF)·텍스처·표면 출처 지도(워커 토큰, S15P31A101-325)
    AVATAR_ASSET(
            "avatars",
            Map.of(
                    "model/gltf-binary", ".glb",
                    "model/gltf+json", ".gltf",
                    "application/octet-stream", ".glb",
                    "image/png", ".png",
                    "image/jpeg", ".jpg",
                    "image/webp", ".webp"),
            64L << 20,
            Set.of(Requester.Kind.WORKER));

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
