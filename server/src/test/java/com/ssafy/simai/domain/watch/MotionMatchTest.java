package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import org.junit.jupiter.api.Test;

/** 움직임 대조 계산(S15P31A101-340): 대역 통과·2차 차분·밀어 본 상관·헝가리안 */
class MotionMatchTest {

    @Test
    void 헝가리안은_합이_가장_작은_1대1을_고르고_직사각형도_다룬다() {
        // 탐욕(행마다 최솟값)이면 0→0(1), 1→0 충돌 뒤 1→2(9) 로 합 10 이지만 최적은 0→1, 1→0, 2→2 = 2+1+1 = 4
        double[][] cost = {{1, 2, 9}, {1, 9, 9}, {9, 9, 1}};
        assertThat(MotionMatch.hungarian(cost)).containsExactly(1, 0, 2);
        // 워치 2 · 트랙 3: 남는 트랙이 하나 있다
        assertThat(MotionMatch.hungarian(new double[][] {{5, 1, 5}, {1, 5, 5}})).containsExactly(1, 0);
        // 워치 3 · 트랙 1: 한 워치만 받는다
        int[] tall = MotionMatch.hungarian(new double[][] {{3}, {1}, {2}});
        assertThat(tall).containsExactly(-1, 0, -1);
        assertThat(MotionMatch.hungarian(new double[0][0])).isEmpty();
    }

    @Test
    void 밀어_본_상관은_시차를_흡수하고_다른_주기와는_낮다() {
        int n = 40;
        double[] a = new double[n];
        double[] shifted = new double[n];
        double[] other = new double[n];
        for (int i = 0; i < n; i++) {
            double t = i / MotionMatch.RATE_HZ;
            a[i] = Math.sin(2 * Math.PI * 1.8 * t);
            shifted[i] = Math.sin(2 * Math.PI * 1.8 * (t - 0.3)); // 0.3초 늦음
            other[i] = Math.sin(2 * Math.PI * 1.1 * t + 1.0);
        }
        assertThat(MotionMatch.correlation(a, shifted, 0)).isLessThan(0.5);
        assertThat(MotionMatch.bestLaggedCorrelation(a, shifted, 5)).isGreaterThan(0.95);
        assertThat(MotionMatch.bestLaggedCorrelation(a, other, 5)).isLessThan(0.6);
        // 평평하거나 겹침이 모자라면 NaN
        assertThat(MotionMatch.bestLaggedCorrelation(a, new double[n], 5)).isNaN();
        double[] sparse = new double[n];
        java.util.Arrays.fill(sparse, Double.NaN);
        sparse[3] = 1;
        assertThat(MotionMatch.bestLaggedCorrelation(a, sparse, 5)).isNaN();
    }

    @Test
    void 대역_통과는_평균을_빼고_2차_차분은_가속도를_낸다() {
        double[] constant = new double[30];
        java.util.Arrays.fill(constant, 1.2);
        for (double v : MotionMatch.bandpass(constant)) {
            assertThat(v).isCloseTo(0, within(1e-9));
        }
        // z = t² 의 가속도는 2
        double[] z = new double[10];
        for (int i = 0; i < z.length; i++) {
            double t = i / MotionMatch.RATE_HZ;
            z[i] = t * t;
        }
        z[5] = Double.NaN;
        double[] a = MotionMatch.secondDifference(z);
        assertThat(a[0]).isNaN();
        assertThat(a[2]).isCloseTo(2, within(1e-9));
        assertThat(a[4]).isNaN();
        assertThat(a[6]).isNaN();
        assertThat(a[7]).isCloseTo(2, within(1e-9));
        assertThat(MotionMatch.coverage(a)).isCloseTo(5 / 10.0, within(1e-9));
    }
}
