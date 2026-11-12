package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import java.util.Map;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/** 세팅 구역 상면 높이 차이 판정 검증·이어 쓴 구역 매핑 단위 시험(S15P31A101-630) */
class SetupZoneHeightChangesTest {

    private final JsonMapper mapper = JsonMapper.builder().build();

    private ObjectNode validSnapshot() {
        ObjectNode zh = mapper.createObjectNode();
        zh.put("previous_scene_version", "scene-lobby-prev");
        zh.put("compared_scene_version", "scene-preview-1");
        zh.put("cell_m", 0.10);
        zh.put("height_diff_threshold_m", 0.15);
        zh.put("changed_area_ratio_threshold", 0.10);
        ArrayNode zones = zh.putArray("zones");

        ObjectNode changed = zones.addObject();
        changed.put("zone_id", "z-changed").put("status", "CHANGE_SUSPECTED").putNull("reason")
                .put("changed_area_ratio", 0.36).put("max_height_diff_m", 0.35)
                .put("total_cells", 100).put("compared_cells", 100).put("changed_cells", 36);
        changed.putArray("max_diff_xy").add(1.5).add(1.5);

        ObjectNode unchanged = zones.addObject();
        unchanged.put("zone_id", "z-unchanged").put("status", "UNCHANGED").putNull("reason")
                .put("changed_area_ratio", 0.0).put("max_height_diff_m", 0.02)
                .put("total_cells", 100).put("compared_cells", 100).put("changed_cells", 0);
        unchanged.putArray("max_diff_xy").add(2.4).add(2.4);

        ObjectNode unknown = zones.addObject();
        unknown.put("zone_id", "z-unknown").put("status", "UNKNOWN").put("reason", "INSUFFICIENT_COVERAGE")
                .putNull("changed_area_ratio").putNull("max_height_diff_m").putNull("max_diff_xy")
                .put("total_cells", 100).put("compared_cells", 10).putNull("changed_cells");

        ObjectNode snap = mapper.createObjectNode();
        snap.set("zone_height_changes", zh);
        return snap;
    }

    @Test
    void 계약_불변식을_지킨_스냅샷만_저장하고_어긋난_값은_버린다() {
        ObjectNode snap = validSnapshot();
        String saved = SetupZoneHeightChanges.fromSnapshot(mapper, snap);
        assertThat(saved).isNotNull().contains("CHANGE_SUSPECTED", "UNCHANGED", "INSUFFICIENT_COVERAGE");

        // UNKNOWN 인데 수치 필드가 null 이 아니면 거부
        ObjectNode unknownWithRatio = snap.deepCopy();
        ((ObjectNode) unknownWithRatio.at("/zone_height_changes/zones/2")).put("changed_area_ratio", 0.0);
        assertThat(SetupZoneHeightChanges.fromSnapshot(mapper, unknownWithRatio)).isNull();

        // CHANGE_SUSPECTED 인데 max_diff_xy 가 null 이면 거부
        ObjectNode changedWithoutXy = snap.deepCopy();
        ((ObjectNode) changedWithoutXy.at("/zone_height_changes/zones/0")).putNull("max_diff_xy");
        assertThat(SetupZoneHeightChanges.fromSnapshot(mapper, changedWithoutXy)).isNull();

        // 중복 zone_id 는 거부
        ObjectNode dupId = snap.deepCopy();
        ((ObjectNode) dupId.at("/zone_height_changes/zones/1")).put("zone_id", "z-changed");
        assertThat(SetupZoneHeightChanges.fromSnapshot(mapper, dupId)).isNull();
    }

    @Test
    void 이어_쓰는_구역은_워커_판정을_붙이고_없으면_세션_상태에_맞는_UNKNOWN_을_채운다() {
        String raw = SetupZoneHeightChanges.fromSnapshot(mapper, validSnapshot());
        Map<String, JsonNode> indexed = SetupZoneHeightChanges.indexByZoneId(mapper, raw);

        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-changed", indexed, false, true, true)
                .path("status").asString()).isEqualTo("CHANGE_SUSPECTED");
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-unchanged", indexed, false, true, true)
                .path("status").asString()).isEqualTo("UNCHANGED");

        // 워커 목록에 없는 이어 쓴 구역: 세션 조건별 UNKNOWN 사유
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-missing", indexed, true, true, true)
                .path("reason").asString()).isEqualTo("REUSE_SCENE");
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-missing", indexed, false, false, true)
                .path("reason").asString()).isEqualTo("WORLD_NOT_ANCHORED");
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-missing", indexed, false, true, false)
                .path("reason").asString()).isEqualTo("NO_PREVIOUS_SCENE");
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, "z-missing", indexed, false, true, true)
                .path("reason").asString()).isEqualTo("INSUFFICIENT_COVERAGE");

        // 새로 그린 구역(zone_id null)은 null
        assertThat(SetupZoneHeightChanges.resolveForZone(mapper, null, indexed, false, true, true).isNull()).isTrue();
    }
}
