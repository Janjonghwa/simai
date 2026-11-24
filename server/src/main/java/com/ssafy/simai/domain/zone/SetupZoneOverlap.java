package com.ssafy.simai.domain.zone;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * 이어 온 구역과 AI 초안 구역이 "같은 자리"인지(S15P31A101-758, docs/design/S15P31A101-758-new-map-opening-only.md).
 * 화면(front features/setup/zoneOverlap.ts)과 같은 기준이다:
 * <ul>
 *   <li>높이 범위가 겹치고(한쪽이라도 높이가 없으면 겹친다고 본다),
 *   <li>바닥 다각형 교집합이 작은 쪽 면적의 {@value #MIN_SHARED_RATIO} 이상이고,
 *   <li>작은 쪽 면적이 큰 쪽의 {@value #MIN_SIZE_RATIO} 이상(넓은 구역 안에 든 작은 구역은 다른 구역이다).
 * </ul>
 * 교집합은 AI 다각형의 볼록 껍질로 다른 다각형을 자른다(Sutherland–Hodgman). AI 초안 구역은 볼록 다각형을 바깥으로 민 볼록 다각형이다.
 */
final class SetupZoneOverlap {

    static final double MIN_SHARED_RATIO = 0.5;
    static final double MIN_SIZE_RATIO = 0.25;
    /** 재사용 구역이 AI 초안을 "품었다"고 볼 초안 넓이 몫(S15P31A101-864, {@link #containedShare}) */
    static final double MIN_CONTAINED_SHARE = 0.5;
    private static final double EPS = 1e-9;

    /** 바닥 다각형(x, y)과 높이 범위(없으면 null) */
    record Footprint(List<double[]> polygon, Double zMin, Double zMax) {
        static Footprint of(ZoneRequest zone) {
            List<double[]> points = new ArrayList<>();
            if (zone.polygonXy() != null) {
                for (List<Double> p : zone.polygonXy()) {
                    if (p != null && p.size() >= 2 && p.get(0) != null && p.get(1) != null) {
                        points.add(new double[] {p.get(0), p.get(1)});
                    }
                }
            }
            return new Footprint(points, zone.zMinM(), zone.zMaxM());
        }
    }

    private SetupZoneOverlap() {
    }

    /** carried(이어 온 구역)와 draft(AI 초안 구역)가 같은 자리인지 */
    static boolean sameSpot(Footprint carried, Footprint draft) {
        if (!heightsOverlap(carried, draft)) {
            return false;
        }
        double a = area(carried.polygon());
        double b = area(draft.polygon());
        double small = Math.min(a, b);
        if (small <= EPS || small < MIN_SIZE_RATIO * Math.max(a, b)) {
            return false;
        }
        List<double[]> hull = convexHull(draft.polygon());
        if (hull.size() < 3) {
            return false;
        }
        return area(clip(carried.polygon(), hull)) >= MIN_SHARED_RATIO * small;
    }

    /**
     * inner(AI 초안) 바닥 넓이 중 outer(구역) 안에 든 몫(0~1, S15P31A101-864). 높이 범위가 겹치지 않으면 0 이다.
     * 초안의 볼록 껍질로 구역 다각형을 잘라 그 넓이를 껍질 넓이로 나눈다(AI 초안은 볼록이라 껍질이 곧 초안이다). 구역은 오목해도 된다.
     */
    static double containedShare(Footprint outer, Footprint inner) {
        if (!heightsOverlap(outer, inner) || outer.polygon().size() < 3) {
            return 0;
        }
        List<double[]> hull = convexHull(inner.polygon());
        if (hull.size() < 3) {
            return 0;
        }
        double hullArea = area(hull);
        if (hullArea <= EPS) {
            return 0;
        }
        return Math.min(1, area(clip(outer.polygon(), hull)) / hullArea);
    }

    private static boolean heightsOverlap(Footprint a, Footprint b) {
        double aLow = a.zMin() == null ? Double.NEGATIVE_INFINITY : a.zMin();
        double aHigh = a.zMax() == null ? Double.POSITIVE_INFINITY : a.zMax();
        double bLow = b.zMin() == null ? Double.NEGATIVE_INFINITY : b.zMin();
        double bHigh = b.zMax() == null ? Double.POSITIVE_INFINITY : b.zMax();
        return Math.min(aHigh, bHigh) > Math.max(aLow, bLow);
    }

    /** 단순 다각형 면적(신발끈 공식, 방향 무관) */
    static double area(List<double[]> polygon) {
        double sum = 0;
        for (int i = 0; i < polygon.size(); i++) {
            double[] p = polygon.get(i);
            double[] q = polygon.get((i + 1) % polygon.size());
            sum += p[0] * q[1] - q[0] * p[1];
        }
        return Math.abs(sum) / 2;
    }

    private static double cross(double[] o, double[] a, double[] b) {
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
    }

    /** 볼록 껍질(반시계, Andrew 단조 사슬) */
    static List<double[]> convexHull(List<double[]> points) {
        List<double[]> sorted = new ArrayList<>(points);
        sorted.sort(Comparator.<double[]>comparingDouble(p -> p[0]).thenComparingDouble(p -> p[1]));
        if (sorted.size() < 3) {
            return sorted;
        }
        List<double[]> lower = chain(sorted);
        List<double[]> reversed = new ArrayList<>(sorted);
        java.util.Collections.reverse(reversed);
        List<double[]> upper = chain(reversed);
        lower.addAll(upper);
        return lower;
    }

    private static List<double[]> chain(List<double[]> points) {
        List<double[]> out = new ArrayList<>();
        for (double[] p : points) {
            while (out.size() >= 2 && cross(out.get(out.size() - 2), out.get(out.size() - 1), p) <= 0) {
                out.removeLast();
            }
            out.add(p);
        }
        out.removeLast();
        return out;
    }

    /** subject 를 볼록 다각형 clip(반시계) 안으로 자른다 */
    static List<double[]> clip(List<double[]> subject, List<double[]> clip) {
        List<double[]> output = new ArrayList<>(subject);
        for (int i = 0; i < clip.size() && !output.isEmpty(); i++) {
            double[] a = clip.get(i);
            double[] b = clip.get((i + 1) % clip.size());
            List<double[]> input = output;
            output = new ArrayList<>();
            for (int j = 0; j < input.size(); j++) {
                double[] current = input.get(j);
                double[] previous = input.get((j + input.size() - 1) % input.size());
                boolean currentIn = cross(a, b, current) >= 0;
                boolean previousIn = cross(a, b, previous) >= 0;
                if (currentIn) {
                    if (!previousIn) output.add(intersection(previous, current, a, b));
                    output.add(current);
                } else if (previousIn) {
                    output.add(intersection(previous, current, a, b));
                }
            }
        }
        return output;
    }

    /** 선분 p→q 와 직선 a→b 의 교점 */
    private static double[] intersection(double[] p, double[] q, double[] a, double[] b) {
        double dp = cross(a, b, p);
        double dq = cross(a, b, q);
        double t = dp / (dp - dq);
        return new double[] {p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])};
    }
}
