package com.ssafy.simai.domain.gpu;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/** 켜기 패널의 VRAM 등급·추천 규칙(S15P31A101-705, 명세 3.12 최소 사양) */
class GpuTiersTest {

    @Test
    void VRAM_등급은_80_48_24GB급이고_MiB_환산값도_같은_칸이며_모르면_비운다() {
        assertThat(GpuTiers.tier(141)).isEqualTo(GpuTiers.RECOMMENDED);
        assertThat(GpuTiers.tier(80)).isEqualTo(GpuTiers.RECOMMENDED);
        assertThat(GpuTiers.tier(74)).isEqualTo(GpuTiers.RECOMMENDED);
        assertThat(GpuTiers.tier(73)).isEqualTo(GpuTiers.BELOW_RECOMMENDED);
        assertThat(GpuTiers.tier(48)).isEqualTo(GpuTiers.BELOW_RECOMMENDED);
        assertThat(GpuTiers.tier(45)).isEqualTo(GpuTiers.BELOW_RECOMMENDED); // AWS L40S 45,776 MiB
        assertThat(GpuTiers.tier(43)).isEqualTo(GpuTiers.LIMITED);
        assertThat(GpuTiers.tier(40)).isEqualTo(GpuTiers.LIMITED); // A100 40GB
        assertThat(GpuTiers.tier(24)).isEqualTo(GpuTiers.LIMITED);
        assertThat(GpuTiers.tier(22)).isEqualTo(GpuTiers.LIMITED); // AWS A10G 22,888 MiB
        assertThat(GpuTiers.tier(21)).isEqualTo(GpuTiers.RISKY);
        assertThat(GpuTiers.tier(16)).isEqualTo(GpuTiers.RISKY);
        assertThat(GpuTiers.tier(null)).isNull();
        assertThat(GpuTiers.rank(null)).isGreaterThan(GpuTiers.rank(GpuTiers.RISKY));
    }

    @Test
    void 추천은_H100_H200_계열이고_80GB_이상이다() {
        assertThat(GpuTiers.recommended("NVIDIA H100 80GB HBM3", "H100 SXM", 80)).isTrue();
        assertThat(GpuTiers.recommended("NVIDIA H100 NVL", "H100 NVL", 94)).isTrue();
        assertThat(GpuTiers.recommended("NVIDIA H200", "H200 SXM", 141)).isTrue();
        assertThat(GpuTiers.recommended("p5en.48xlarge", "p5en.48xlarge · NVIDIA H200 ×8", 141)).isTrue();
        assertThat(GpuTiers.recommended("p5.48xlarge", "p5.48xlarge · H100 ×8", null)).isTrue();
        assertThat(GpuTiers.recommended("NVIDIA GH200 480GB", "GH200", 96)).isFalse();
        assertThat(GpuTiers.recommended("NVIDIA A100 80GB PCIe", "A100 PCIe", 80)).isFalse();
        assertThat(GpuTiers.recommended("NVIDIA B200", "B200", 180)).isFalse();
        assertThat(GpuTiers.recommended("NVIDIA H100 (MIG 40GB)", "H100 MIG", 40)).isFalse();
    }

    @Test
    void 재고_등급은_켤_수_있음_없음_모름으로_줄인다() {
        assertThat(GpuTiers.available("HIGH")).isTrue();
        assertThat(GpuTiers.available("MEDIUM")).isTrue();
        assertThat(GpuTiers.available("LOW")).isTrue();
        assertThat(GpuTiers.available("NONE")).isFalse();
        assertThat(GpuTiers.available(null)).isFalse();
        assertThat(GpuTiers.available("UNKNOWN")).isNull();
    }

    @Test
    void 이름이나_VRAM_을_모르는_카탈로그_항목은_행이_되지_않고_0GB_는_모름이다() {
        // S15P31A101-713: 16차 세션의 「unknown · 0 GB · 위험」 행
        assertThat(GpuTiers.listable("unknown", "unknown", 0)).isFalse();
        assertThat(GpuTiers.listable("unknown", "H100 SXM", 80)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", "Unknown", 48)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", " ", 48)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", null, 48)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", "N/A", 48)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", "A40", 0)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", "A40", null)).isFalse();
        assertThat(GpuTiers.listable("NVIDIA A40", "A40", 48)).isTrue();
        assertThat(GpuTiers.listable("g6e.xlarge", "g6e.xlarge · NVIDIA L40S ×1", 45)).isTrue();
        assertThat(GpuTiers.listable("NVIDIA RTX A4000", "RTX A4000", 16)).isTrue(); // 작아도 이름·VRAM 을 알면 행(위험)
        assertThat(GpuTiers.memory(0)).isNull();
        assertThat(GpuTiers.memory(-1)).isNull();
        assertThat(GpuTiers.memory(24)).isEqualTo(24);
    }
}
