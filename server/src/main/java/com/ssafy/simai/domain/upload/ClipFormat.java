package com.ssafy.simai.domain.upload;

import java.util.Arrays;
import java.util.Map;
import java.util.Optional;
import java.util.function.Function;
import java.util.stream.Collectors;

/**
 * 이벤트 클립 형식. 업로드 서명(키 확장자, UploadPurpose.EVENT_CLIP)과 클립 기록 수신(media_clip.container 검사)이 이 표 하나를 쓴다.
 * 화면은 container 를 보고 감싸 재생하므로 형식과 container 가 어긋나면 재생하지 못한다.
 */
public enum ClipFormat {
    // 재인코딩 없는 H.264 Annex B 기본 스트림(ai/services/tier0/clips/media.py)
    H264("video/h264", ".h264", "h264-annexb"),
    MP4("video/mp4", ".mp4", "mp4");

    private static final Map<String, ClipFormat> BY_CONTENT_TYPE = Arrays.stream(values())
            .collect(Collectors.toUnmodifiableMap(ClipFormat::contentType, Function.identity()));

    private final String contentType;
    private final String extension;
    private final String container;

    ClipFormat(String contentType, String extension, String container) {
        this.contentType = contentType;
        this.extension = extension;
        this.container = container;
    }

    /** contentType 은 소문자(서명이 소문자로 바꿔 발급한다) */
    public static Optional<ClipFormat> of(String contentType) {
        return Optional.ofNullable(BY_CONTENT_TYPE.get(contentType));
    }

    static Map<String, String> extensions() {
        return Arrays.stream(values()).collect(Collectors.toUnmodifiableMap(ClipFormat::contentType, f -> f.extension));
    }

    public String contentType() {
        return contentType;
    }

    public String container() {
        return container;
    }
}
