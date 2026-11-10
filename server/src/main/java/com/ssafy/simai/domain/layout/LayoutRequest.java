package com.ssafy.simai.domain.layout;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * 가상 레이아웃 저장 본문(명세 8.3, 12.6 /api/layouts, V23 layout_plan). 좌표는 World(m, +Z 위).
 * items 는 관제 앱 PLANNED 배치(features/viewer3d/planned/plannedLayout.ts)와 같은 OBB 다.
 * revision 은 PUT 에서만 쓰는 낙관적 잠금 값(고치기 시작한 revision). 다르면 409 다. 없으면 검사하지 않는다.
 */
public record LayoutRequest(
        @JsonProperty("name") String name,
        @JsonProperty("items") List<Item> items,
        @JsonProperty("revision") Integer revision) {

    /** kind 는 OPENING·SCAFFOLD·EQUIPMENT. center 는 OBB 가운데 [x, y, z], size 는 전체 길이(> 0), yaw_rad 는 [-π, π] */
    public record Item(
            @JsonProperty("item_id") String itemId,
            @JsonProperty("kind") String kind,
            @JsonProperty("center") List<Double> center,
            @JsonProperty("size") List<Double> size,
            @JsonProperty("yaw_rad") Double yawRad,
            @JsonProperty("label") String label,
            @JsonProperty("source_zone_id") String sourceZoneId,
            @JsonProperty("source_object_id") String sourceObjectId) {
    }
}
