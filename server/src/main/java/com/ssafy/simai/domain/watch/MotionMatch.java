package com.ssafy.simai.domain.watch;

import java.util.Arrays;

/**
 * 워치 ↔ World 트랙 움직임 대조의 계산(S15P31A101-340, 확장 C.6 강화층). 상태 없는 함수만 둔다.
 *
 * <p>워치의 {@code band_magnitude} 는 손목 가속도 크기 |g + a| 를 0.5~3 Hz 로 거른 값이다. 걸을 때 a 가 g 보다 작으면
 * |g + a| − g ≈ a·ĝ(연직 성분)이라, World 쪽은 손목 관절 높이(Z)의 2차 차분(연직 가속도)을 같은 대역으로 걸러 비교한다.
 * 상관계수는 크기에 무관해 워치 m/s² 와 삼각측량 m/s² 의 축척 차이를 따지지 않는다.
 */
final class MotionMatch {

    /** 비교 신호 간격(워치 band_magnitude 기본 rate_hz 10, world_state 10 Hz) */
    static final double RATE_HZ = 10.0;
    /** 상관을 낼 최소 겹침 표본(2초) */
    static final int MIN_OVERLAP = 20;

    private MotionMatch() {
    }

    /**
     * 0.5~3 Hz 대역 통과(근사). 2초 이동 평균을 빼 0.5 Hz 아래(자세·중력 변화)를 없애고 [1/4, 1/2, 1/4] 로 3 Hz 위 잡음을 줄인다.
     * NaN(관측 없음)은 그대로 NaN 으로 둔다.
     */
    static double[] bandpass(double[] x) {
        int n = x.length;
        int half = (int) Math.round(RATE_HZ); // 앞뒤 1초
        double[] hp = new double[n];
        for (int i = 0; i < n; i++) {
            if (Double.isNaN(x[i])) {
                hp[i] = Double.NaN;
                continue;
            }
            double sum = 0;
            int count = 0;
            for (int j = Math.max(0, i - half); j <= Math.min(n - 1, i + half); j++) {
                if (!Double.isNaN(x[j])) {
                    sum += x[j];
                    count++;
                }
            }
            hp[i] = x[i] - sum / count;
        }
        double[] out = new double[n];
        for (int i = 0; i < n; i++) {
            double a = i > 0 ? hp[i - 1] : hp[i];
            double c = i < n - 1 ? hp[i + 1] : hp[i];
            out[i] = Double.isNaN(a) || Double.isNaN(hp[i]) || Double.isNaN(c) ? hp[i] : 0.25 * a + 0.5 * hp[i] + 0.25 * c;
        }
        return out;
    }

    /** 위치(m, RATE_HZ 간격)의 2차 차분 → 가속도(m/s²). 양 끝과 이웃에 NaN 이 있으면 NaN */
    static double[] secondDifference(double[] z) {
        int n = z.length;
        double[] a = new double[n];
        Arrays.fill(a, Double.NaN);
        double dt2 = 1.0 / (RATE_HZ * RATE_HZ);
        for (int i = 1; i < n - 1; i++) {
            if (!Double.isNaN(z[i - 1]) && !Double.isNaN(z[i]) && !Double.isNaN(z[i + 1])) {
                a[i] = (z[i - 1] - 2 * z[i] + z[i + 1]) / dt2;
            }
        }
        return a;
    }

    /** 중력 가속도(m/s²). 워치 |g + a| 를 World 관절로 다시 만들 때 +Z 로 더한다 */
    static final double GRAVITY = 9.80665;

    /**
     * 손목 3D 위치(m, RATE_HZ 간격)에서 워치가 재는 가속도 크기 |g + a| 를 다시 만든다(S15P31A101-508 손 흔들기). a 는 위치의
     * 2차 차분이고 g 는 +Z 로 더한다. 크기는 센서 방향에 무관해 워치 자세를 몰라도 되고, 걸을 때의 연직 근사와 달리 흔들기처럼
     * a 가 g 만큼 크고 수평 성분이 큰 움직임에도 맞는다. 어느 축이든 NaN 이면 그 표본은 NaN. 대역 통과는 호출자가 한다.
     */
    static double[] specificForceMagnitude(double[] x, double[] y, double[] z) {
        double[] ax = secondDifference(x);
        double[] ay = secondDifference(y);
        double[] az = secondDifference(z);
        double[] m = new double[ax.length];
        for (int i = 0; i < m.length; i++) {
            m[i] = Double.isNaN(ax[i]) || Double.isNaN(ay[i]) || Double.isNaN(az[i]) ? Double.NaN
                    : Math.sqrt(ax[i] * ax[i] + ay[i] * ay[i] + (az[i] + GRAVITY) * (az[i] + GRAVITY));
        }
        return m;
    }

    /** 유효 표본의 RMS(평균은 빼지 않는다). 유효 표본이 없으면 NaN */
    static double rms(double[] x) {
        double sum = 0;
        int n = 0;
        for (double v : x) {
            if (!Double.isNaN(v)) {
                sum += v * v;
                n++;
            }
        }
        return n == 0 ? Double.NaN : Math.sqrt(sum / n);
    }

    /** 유효(NaN 아님) 표본 비율 */
    static double coverage(double[] x) {
        if (x.length == 0) {
            return 0;
        }
        long valid = Arrays.stream(x).filter(v -> !Double.isNaN(v)).count();
        return (double) valid / x.length;
    }

    /**
     * a 와 b 를 b 쪽으로 −maxLag ~ +maxLag 표본 밀어 가며 겹친 유효 표본의 피어슨 상관 최댓값. 시계 차이·관측 지연을 흡수한다.
     * 어느 밀기에서도 겹침이 {@link #MIN_OVERLAP} 보다 적거나 한쪽이 평평하면 NaN.
     */
    static double bestLaggedCorrelation(double[] a, double[] b, int maxLag) {
        double best = Double.NaN;
        for (int lag = -maxLag; lag <= maxLag; lag++) {
            double r = correlation(a, b, lag);
            if (!Double.isNaN(r) && (Double.isNaN(best) || r > best)) {
                best = r;
            }
        }
        return best;
    }

    static double correlation(double[] a, double[] b, int lag) {
        double sa = 0, sb = 0, saa = 0, sbb = 0, sab = 0;
        int n = 0;
        for (int i = 0; i < a.length; i++) {
            int j = i + lag;
            if (j < 0 || j >= b.length || Double.isNaN(a[i]) || Double.isNaN(b[j])) {
                continue;
            }
            sa += a[i];
            sb += b[j];
            saa += a[i] * a[i];
            sbb += b[j] * b[j];
            sab += a[i] * b[j];
            n++;
        }
        if (n < MIN_OVERLAP) {
            return Double.NaN;
        }
        double va = saa - sa * sa / n;
        double vb = sbb - sb * sb / n;
        if (va <= 1e-12 || vb <= 1e-12) {
            return Double.NaN;
        }
        return (sab - sa * sb / n) / Math.sqrt(va * vb);
    }

    /**
     * 헝가리안(최소 비용 배정, Kuhn–Munkres O(n³)). 직사각형이면 큰 쪽에 맞춰 0 비용으로 채운다.
     *
     * @return 행 i 에 배정된 열(없으면 −1)
     */
    static int[] hungarian(double[][] cost) {
        int rows = cost.length;
        int cols = rows == 0 ? 0 : cost[0].length;
        int n = Math.max(rows, cols);
        if (n == 0) {
            return new int[0];
        }
        double[][] c = new double[n + 1][n + 1];
        for (int i = 0; i < rows; i++) {
            for (int j = 0; j < cols; j++) {
                c[i + 1][j + 1] = cost[i][j];
            }
        }
        double[] u = new double[n + 1];
        double[] v = new double[n + 1];
        int[] p = new int[n + 1];
        int[] way = new int[n + 1];
        for (int i = 1; i <= n; i++) {
            p[0] = i;
            int j0 = 0;
            double[] minv = new double[n + 1];
            Arrays.fill(minv, Double.POSITIVE_INFINITY);
            boolean[] used = new boolean[n + 1];
            do {
                used[j0] = true;
                int i0 = p[j0];
                double delta = Double.POSITIVE_INFINITY;
                int j1 = 0;
                for (int j = 1; j <= n; j++) {
                    if (!used[j]) {
                        double cur = c[i0][j] - u[i0] - v[j];
                        if (cur < minv[j]) {
                            minv[j] = cur;
                            way[j] = j0;
                        }
                        if (minv[j] < delta) {
                            delta = minv[j];
                            j1 = j;
                        }
                    }
                }
                for (int j = 0; j <= n; j++) {
                    if (used[j]) {
                        u[p[j]] += delta;
                        v[j] -= delta;
                    } else {
                        minv[j] -= delta;
                    }
                }
                j0 = j1;
            } while (p[j0] != 0);
            do {
                int j1 = way[j0];
                p[j0] = p[j1];
                j0 = j1;
            } while (j0 != 0);
        }
        int[] assignment = new int[rows];
        Arrays.fill(assignment, -1);
        for (int j = 1; j <= n; j++) {
            if (p[j] >= 1 && p[j] <= rows && j <= cols) {
                assignment[p[j] - 1] = j - 1;
            }
        }
        return assignment;
    }
}
