package com.ssafy.simai.domain.geometry;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;

/**
 * 등록 작업면·정적 객체 본문(명세 7.5, 5.3 Operational Geometry, V1 static_object 표). 좌표는 World(m, +Z 위).
 * kind 는 WORK_SURFACE(단상·작업대 등 사람이 올라서는 면)·OBSTACLE. OBB 는 보수적으로 잡는다(실제보다 작게 잡지 않는다).
 * 작업면은 상판 높이 top_height_m(> 0)가 있어야 한다. top_height_sigma_m 는 상판 높이 표준편차(없으면 Tier 0 기본 0.02).
 */
public record StaticObjectRequest(
        @JsonProperty("kind") String kind,
        @JsonProperty("name") String name,
        @JsonProperty("obb") Obb obb,
        @JsonProperty("top_height_m") Double topHeightM,
        @JsonProperty("top_height_sigma_m") Double topHeightSigmaM) {

    /** center 는 OBB 가운데 [x, y, z], size 는 가로·세로·높이 전체 길이, yaw_rad 는 +Z 축 회전(라디안) */
    public record Obb(
            @JsonProperty("center") List<Double> center,
            @JsonProperty("size") List<Double> size,
            @JsonProperty("yaw_rad") Double yawRad) {
    }
}
