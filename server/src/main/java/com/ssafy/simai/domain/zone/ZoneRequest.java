package com.ssafy.simai.domain.zone;

import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import tools.jackson.databind.JsonNode;

/**
 * 구역 생성·새 버전 본문(명세 8.2·11.1 zone, V1 zone 표). 좌표는 World(m, +Z 위), 다각형은 바닥 XY 꼭짓점(닫는 점은 생략 가능).
 * eval_type 은 GROUND_FOOTPRINT·VOLUME_3D, VOLUME_3D 는 z_min_m < z_max_m 가 있어야 한다. severity 는 CRITICAL·HIGH·CAUTION·INFO.
 */
public record ZoneRequest(
        @JsonProperty("name") String name,
        @JsonProperty("zone_kind") String zoneKind,
        @JsonProperty("eval_type") String evalType,
        @JsonProperty("polygon_xy") List<List<Double>> polygonXy,
        @JsonProperty("z_min_m") Double zMinM,
        @JsonProperty("z_max_m") Double zMaxM,
        @JsonProperty("severity") String severity,
        @JsonProperty("has_guardrail") Boolean hasGuardrail,
        @JsonProperty("requirements") JsonNode requirements) {
}
