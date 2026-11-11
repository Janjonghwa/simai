package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** 맵 보강 촬영(S15P31A101-607) 순수 도우미: 받는 형식 → 종류, 표시용 파일 이름 다듬기. */
class SetupSupplementServiceTest {

    @Test
    void 받는_형식만_동영상_사진으로_가른다() {
        assertThat(SetupSupplementService.kindOf("video/mp4")).contains("VIDEO");
        assertThat(SetupSupplementService.kindOf(" Video/QuickTime ")).contains("VIDEO");
        assertThat(SetupSupplementService.kindOf("image/jpeg")).contains("IMAGE");
        assertThat(SetupSupplementService.kindOf("image/png")).contains("IMAGE");
        assertThat(SetupSupplementService.kindOf("image/heic")).isEmpty();
        assertThat(SetupSupplementService.kindOf("video/webm")).isEmpty();
        assertThat(SetupSupplementService.kindOf(null)).isEmpty();
        assertThat(SetupSupplementService.CONTENT_TYPES).hasSize(4);
    }

    @Test
    void 파일_이름은_경로와_제어_문자를_떼고_255자로_자른다() {
        assertThat(SetupSupplementService.displayName("/sdcard/DCIM/VID_1.mp4")).isEqualTo("VID_1.mp4");
        assertThat(SetupSupplementService.displayName("C:\\GoPro\\GX01.MP4")).isEqualTo("GX01.MP4");
        assertThat(SetupSupplementService.displayName("a\u0000b\nc.jpg")).isEqualTo("abc.jpg");
        assertThat(SetupSupplementService.displayName("   ")).isNull();
        assertThat(SetupSupplementService.displayName(null)).isNull();
        assertThat(SetupSupplementService.displayName("x".repeat(300))).hasSize(255);
    }

    @Test
    void 상한은_계약_개수와_설계_값을_따른다() {
        assertThat(SetupSupplementService.MAX_ITEMS).isEqualTo(32);
        assertThat(SetupSupplementService.MAX_VIDEO_BYTES).isEqualTo(1L << 30);
        assertThat(SetupSupplementService.MAX_IMAGE_BYTES).isEqualTo(50L << 20);
        assertThat(SetupSupplementService.MAX_TOTAL_BYTES).isEqualTo(4L << 30);
    }
}
