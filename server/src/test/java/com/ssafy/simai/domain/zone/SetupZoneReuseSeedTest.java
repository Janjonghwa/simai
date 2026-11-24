package com.ssafy.simai.domain.zone;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.within;

import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.junit.jupiter.api.Test;

/** 재사용 채우기의 구역 ↔ AI 초안 짝짓기(S15P31A101-864, docs/design/S15P31A101-864-reuse-adjusted-ai-zone.md) */
class SetupZoneReuseSeedTest {

    private static List<List<Double>> rect(double x0, double y0, double x1, double y1) {
        return List.of(List.of(x0, y0), List.of(x1, y0), List.of(x1, y1), List.of(x0, y1));
    }

    private static ZoneRequest body(String name, List<List<Double>> polygon, double zMin, double zMax) {
        return new ZoneRequest(name, "OPENING", "VOLUME_3D", polygon, zMin, zMax, "CRITICAL", null, null);
    }

    private static SetupZoneReuseSeed.BundleZone zone(String id, UUID createdBy, ZoneRequest body) {
        return new SetupZoneReuseSeed.BundleZone(id, 1, 1, createdBy, body, null);
    }

    private static SetupZoneReuseSeed.ShownDraft draft(String id, double x0, double y0, double x1, double y1, double zMax) {
        return new SetupZoneReuseSeed.ShownDraft(id, new SetupZoneOverlap.Footprint(List.of(new double[] {x0, y0},
                new double[] {x1, y0}, new double[] {x1, y1}, new double[] {x0, y1}), 0.0, zMax));
    }

    @Test
    void 운영_값_사람이_키운_개구부는_수락_기록이_없어도_초안을_품어_짝이_된다() {
        // 11-24 운영: 초안 0.3 x 0.7(z 0~0.6205), 구역 2.4 x 2.3(z 0~1.4), 원 세션에 AI 수락 기록 없음
        SetupZoneReuseSeed.ShownDraft opening = draft("cand-platform-opening-07", 6.3336, 2.99, 6.6336, 3.69, 0.6205);
        SetupZoneReuseSeed.BundleZone grown = zone("z-c3066908", UUID.randomUUID(),
                body("단상 개구부 1", rect(5.2, 2.25, 7.6, 4.55), 0, 1.4));
        SetupZoneReuseSeed.BundleZone human = zone("z-human", UUID.randomUUID(), body("사람 구역", rect(0, 0, 1, 1), 0, 1.4));
        // -844 기준(같은 자리)으로는 크기 비가 0.25 에 못 미쳐 걸리지 않는다
        assertThat(SetupZoneOverlap.sameSpot(SetupZoneOverlap.Footprint.of(grown.zone()), opening.footprint())).isFalse();
        assertThat(SetupZoneOverlap.containedShare(SetupZoneOverlap.Footprint.of(grown.zone()), opening.footprint()))
                .isCloseTo(1.0, within(1e-9));

        Map<String, SetupZoneReuseSeed.DraftMatch> pairs = SetupZoneReuseSeed.pair(List.of(human, grown), List.of(opening),
                session -> List.of());
        assertThat(pairs).containsOnlyKeys("z-c3066908");
        assertThat(pairs.get("z-c3066908").candidateId()).isEqualTo("cand-platform-opening-07");
        assertThat(pairs.get("z-c3066908").acceptedBody()).isNull();
    }

    @Test
    void 초안을_절반_넘게_품지_않거나_높이가_안_겹치면_짝이_아니다() {
        SetupZoneReuseSeed.ShownDraft d = draft("d", 0, 0, 1, 1, 0.6);
        // 0.4 만 품음
        assertThat(SetupZoneReuseSeed.pair(List.of(zone("a", null, body("a", rect(0.6, 0, 3, 1), 0, 1.4))), List.of(d),
                s -> List.of())).isEmpty();
        // 절반을 넘게(0.55) 품으면 짝이다
        assertThat(SetupZoneReuseSeed.pair(List.of(zone("a", null, body("a", rect(0.45, 0, 3, 1), 0, 1.4))), List.of(d),
                s -> List.of())).containsOnlyKeys("a");
        // 단상 위(0.8 m 위) 구역
        assertThat(SetupZoneReuseSeed.pair(List.of(zone("a", null, body("a", rect(-1, -1, 2, 2), 0.8, 2.8))), List.of(d),
                s -> List.of())).isEmpty();
        // 다시 보일 초안이 없으면 아무것도 짝짓지 않는다
        assertThat(SetupZoneReuseSeed.pair(List.of(zone("a", null, body("a", rect(-1, -1, 2, 2), 0, 1.4))), List.of(),
                s -> List.of())).isEmpty();
    }

    @Test
    void 한_초안에_여러_구역이_걸리면_많이_품은_쪽_같으면_작은_쪽_하나만_한_구역은_한_초안만() {
        SetupZoneReuseSeed.ShownDraft d1 = draft("d1", 0, 0, 1, 1, 0.6);
        SetupZoneReuseSeed.ShownDraft d2 = draft("d2", 0.2, 0.2, 0.8, 0.8, 0.6);
        SetupZoneReuseSeed.BundleZone big = zone("z-big", null, body("넓은", rect(-3, -3, 4, 4), 0, 1.4));
        SetupZoneReuseSeed.BundleZone fit = zone("z-fit", null, body("꼭 맞음", rect(-0.3, -0.3, 1.3, 1.3), 0, 1.4));
        SetupZoneReuseSeed.BundleZone half = zone("z-half", null, body("반쯤", rect(0.4, 0, 2, 1), 0, 1.4));
        // d1: big·fit 둘 다 다 품는다 → 작은 fit. d2: 남은 big(다 품음)이 half(0.67)보다 먼저 → big. half 는 남는다
        Map<String, SetupZoneReuseSeed.DraftMatch> pairs = SetupZoneReuseSeed.pair(List.of(half, big, fit), List.of(d1, d2),
                s -> List.of());
        assertThat(pairs).hasSize(2);
        assertThat(pairs.get("z-fit").candidateId()).isEqualTo("d1");
        assertThat(pairs.get("z-big").candidateId()).isEqualTo("d2");
        // 입력 순서가 바뀌어도 같다
        assertThat(SetupZoneReuseSeed.pair(List.of(fit, big, half), List.of(d1, d2), s -> List.of())).isEqualTo(pairs);
    }

    @Test
    void 수락_기록_짝이_품음보다_먼저이고_그대로인_구역은_조정_정보가_필요_없다() {
        UUID session = UUID.randomUUID();
        SetupZoneReuseSeed.ShownDraft d = draft("cand-1", 1, 1, 3, 3, 1.4);
        ZoneRequest accepted = body("단상 개구부 1", rect(0.9, 0.9, 3.1, 3.1), 0, 1.4);
        // 수락 기록 구역(같은 자리)과, 초안을 더 꼭 맞게 품는 사람 구역
        SetupZoneReuseSeed.BundleZone fromRecord = zone("z-b", session, accepted);
        SetupZoneReuseSeed.BundleZone other = zone("z-a", null, body("사람", rect(0.95, 0.95, 3.05, 3.05), 0, 1.4));
        Map<String, SetupZoneReuseSeed.DraftMatch> pairs = SetupZoneReuseSeed.pair(List.of(other, fromRecord), List.of(d),
                s -> s.equals(session) ? List.of(new SetupZonePlanService.Item(null, "AI", "cand-1", accepted, null, false)) : List.of());
        assertThat(pairs).containsOnlyKeys("z-b");
        assertThat(pairs.get("z-b").acceptedBody()).isEqualTo(accepted);

        // DB float4 높이(1.4 → 1.3999999)는 같다고 보고, 이름·심각도·꼭짓점이 바뀌면 다르다
        ZoneRequest stored = new ZoneRequest("단상 개구부 1", "OPENING", "VOLUME_3D", List.of(List.of(3.1, 0.9), List.of(3.1, 3.1),
                List.of(0.9, 3.1), List.of(0.9, 0.9)), 0.0, (double) 1.4f, "CRITICAL", null, null);
        assertThat(SetupZoneReuseSeed.sameZone(stored, accepted)).isTrue();
        assertThat(SetupZoneReuseSeed.sameZone(body("단상 개구부(고침)", rect(0.9, 0.9, 3.1, 3.1), 0, 1.4), accepted)).isFalse();
        assertThat(SetupZoneReuseSeed.sameZone(new ZoneRequest("단상 개구부 1", "OPENING", "VOLUME_3D", rect(0.9, 0.9, 3.1, 3.1), 0.0,
                1.4, "HIGH", null, null), accepted)).isFalse();
        assertThat(SetupZoneReuseSeed.sameZone(body("단상 개구부 1", rect(0.9, 0.9, 3.5, 3.1), 0, 1.4), accepted)).isFalse();
    }
}
