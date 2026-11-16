package com.ssafy.simai.domain.zone;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import java.util.List;
import org.junit.jupiter.api.Test;

/** 이어 온 구역과 AI 초안 구역의 같은 자리 기준(S15P31A101-758). 화면 zoneOverlap.test.ts 와 같은 경우를 본다 */
class SetupZoneOverlapTest {

    private static SetupZoneOverlap.Footprint box(double x0, double y0, double x1, double y1, Double zMin, Double zMax) {
        return new SetupZoneOverlap.Footprint(List.of(new double[] {x0, y0}, new double[] {x1, y0}, new double[] {x1, y1},
                new double[] {x0, y1}), zMin, zMax);
    }

    @Test
    void 같은_개구부를_조금_넓혀_다시_잡은_초안은_같은_자리다() {
        // 지난 세팅에서 손으로 그린 1.2 x 1.0 개구부와, 이번 AI 초안을 0.3 m 민 구역
        assertThat(SetupZoneOverlap.sameSpot(box(0, 0, 1.2, 1, 0.0, 0.6), box(-0.3, -0.3, 1.5, 1.3, 0.0, 0.65))).isTrue();
        // 초안을 10 cm 옮겨 다시 잡아도 같은 자리
        assertThat(SetupZoneOverlap.sameSpot(box(0.1, 0.1, 1.3, 1.1, null, null), box(0, 0, 1.2, 1, null, null))).isTrue();
    }

    @Test
    void 떨어진_구역_한쪽만_스친_구역_넓은_구역_다른_높이는_다른_자리다() {
        SetupZoneOverlap.Footprint draft = box(0, 0, 1, 1, 0.0, 0.6);
        assertThat(SetupZoneOverlap.sameSpot(box(5, 5, 6, 6, null, null), draft)).isFalse();
        // 교집합이 작은 쪽의 절반에 못 미친다(0.3 / 1.0)
        assertThat(SetupZoneOverlap.sameSpot(box(0.7, 0, 1.7, 1, null, null), draft)).isFalse();
        // 개구부를 품은 넓은 구역(36 m²)은 크기가 4배 넘게 달라 다른 구역이다
        assertThat(SetupZoneOverlap.sameSpot(box(-2, -2, 4, 4, null, null), draft)).isFalse();
        // 높이 범위가 겹치지 않는다(단상 위 작업면 구역)
        assertThat(SetupZoneOverlap.sameSpot(box(0, 0, 1, 1, 0.8, 2.8), draft)).isFalse();
    }

    @Test
    void 오목한_이어_온_구역도_볼록한_초안으로_잘라_면적을_잰다() {
        // ㄱ자(3칸, 3 m²)를 1 x 1 상자로 자르면 1 m²
        List<double[]> l = List.of(new double[] {0, 0}, new double[] {2, 0}, new double[] {2, 1}, new double[] {1, 1},
                new double[] {1, 2}, new double[] {0, 2});
        List<double[]> hull = SetupZoneOverlap.convexHull(List.of(new double[] {0, 0}, new double[] {1, 0},
                new double[] {1, 1}, new double[] {0, 1}));
        assertThat(SetupZoneOverlap.area(l)).isCloseTo(3.0, within(1e-9));
        assertThat(SetupZoneOverlap.area(SetupZoneOverlap.clip(l, hull))).isCloseTo(1.0, within(1e-9));
    }
}
