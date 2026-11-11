package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** site_setup metrics.joint_ba → 세션 pair_holdout(S15P31A101-602) */
class SetupPairHoldoutTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    /** 계약 필수 칸만 채운 metrics.joint_ba */
    private static ObjectNode jointBa() {
        return MAPPER.createObjectNode()
                .put("anchor_camera_id", "cam1").put("converged", true).put("stage_seconds", 3.0).put("valid_points", 10);
    }

    private static ObjectNode root(ObjectNode jointBa) {
        ObjectNode snapshot = MAPPER.createObjectNode();
        snapshot.putObject("metrics").set("joint_ba", jointBa);
        return snapshot;
    }

    private static ObjectNode stats(double median) {
        return MAPPER.createObjectNode().put("n", 30).put("invalid", 2).put("median", median).put("p90", median * 2)
                .put("p95", median * 3);
    }

    @Test
    void 전후_통계와_중앙값을_쌍별로_합치고_허용_오차를_싣는다() {
        ObjectNode jointBa = jointBa().put("holdout_worsening_tolerance_px", 1.0);
        ObjectNode pair = jointBa.putObject("pair_holdout").putObject("cam_a-cam-b");
        pair.set("before", stats(2.0));
        pair.set("after", stats(1.0).putNull("p95"));
        jointBa.putObject("pair_holdout_median_px").put("cam_a-cam-b", 1.0).putNull("cam3-cam4");

        JsonNode out = MAPPER.readTree(SetupPairHoldout.fromSnapshot(MAPPER, root(jointBa)));
        assertThat(out.get("holdout_worsening_tolerance_px").asDouble()).isEqualTo(1.0);
        // 카메라 ID 에 '-' 가 있어도 키를 나누지 않는다
        assertThat(out.at("/pairs/cam_a-cam-b/before/median").asDouble()).isEqualTo(2.0);
        assertThat(out.at("/pairs/cam_a-cam-b/before/n").asInt()).isEqualTo(30);
        assertThat(out.at("/pairs/cam_a-cam-b/after/p95").isNull()).isTrue();
        assertThat(out.at("/pairs/cam_a-cam-b/median_px").asDouble()).isEqualTo(1.0);
        assertThat(out.at("/pairs/cam3-cam4/before").isNull()).isTrue();
        assertThat(out.at("/pairs/cam3-cam4/after").isNull()).isTrue();
        assertThat(out.at("/pairs/cam3-cam4/median_px").isNull()).isTrue();
    }

    @Test
    void 중앙값만_온_옛_스냅샷도_받고_허용_오차가_없으면_null_이다() {
        ObjectNode jointBa = jointBa();
        jointBa.putObject("pair_holdout_median_px").put("cam1-cam2", 0.8);
        JsonNode out = MAPPER.readTree(SetupPairHoldout.fromSnapshot(MAPPER, root(jointBa)));
        assertThat(out.get("holdout_worsening_tolerance_px").isNull()).isTrue();
        assertThat(out.at("/pairs/cam1-cam2/median_px").asDouble()).isEqualTo(0.8);
        assertThat(out.at("/pairs/cam1-cam2/before").isNull()).isTrue();
    }

    @Test
    void 값이_없거나_형식이_어긋나면_null_이라_이전_값을_유지한다() {
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, MAPPER.createObjectNode())).isNull();
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(jointBa()))).isNull();

        ObjectNode emptyPairs = jointBa();
        emptyPairs.putObject("pair_holdout");
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(emptyPairs))).isNull();

        ObjectNode negative = jointBa();
        ObjectNode pair = negative.putObject("pair_holdout").putObject("cam1-cam2");
        pair.set("before", stats(1.0));
        pair.set("after", stats(1.0).put("median", -1.0));
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(negative))).isNull();

        ObjectNode missingAfter = jointBa();
        missingAfter.putObject("pair_holdout").putObject("cam1-cam2").set("before", stats(1.0));
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(missingAfter))).isNull();

        ObjectNode fractionalCount = jointBa();
        ObjectNode p2 = fractionalCount.putObject("pair_holdout").putObject("cam1-cam2");
        p2.set("before", stats(1.0).put("n", 1.5));
        p2.set("after", stats(1.0));
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(fractionalCount))).isNull();

        ObjectNode badMedian = jointBa();
        badMedian.putObject("pair_holdout_median_px").put("cam1-cam2", "1.0");
        assertThat(SetupPairHoldout.fromSnapshot(MAPPER, root(badMedian))).isNull();
    }
}
