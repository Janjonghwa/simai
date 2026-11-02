package com.ssafy.simai.domain.zone;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;

/**
 * 구역 편집 한 번 저장(POST /api/zones/commit, S15P31A101-397). 구역 하나와 그 구역의 작업면 하나를 바꾸고 구성 묶음을 만든다.
 * <ul>
 *   <li>zone.op: CREATE(body) · UPDATE(zone_id, body) · ARCHIVE(zone_id, 이은 작업면도 보관) · KEEP(zone_id, 작업면만 바꿀 때)
 *   <li>surface.op(선택): CREATE(body) · UPDATE(object_id, body) · REPLACE(object_id 옛 작업면 보관 뒤 body 로 새로 만듦) ·
 *       ARCHIVE(object_id). 작업면은 언제나 이 구역에 잇는다(body 의 zone_id 는 쓰지 않는다)
 *   <li>publish: 구성 묶음을 만들어 Tier 0 에 보낼지(기본 true)
 * </ul>
 */
public record ZoneCommitRequest(
        @JsonProperty("zone") ZonePart zone,
        @JsonProperty("surface") SurfacePart surface,
        @JsonProperty("publish") Boolean publish) {

    public record ZonePart(
            @JsonProperty("op") String op,
            @JsonProperty("zone_id") String zoneId,
            @JsonProperty("body") ZoneRequest body) {
    }

    public record SurfacePart(
            @JsonProperty("op") String op,
            @JsonProperty("object_id") String objectId,
            @JsonProperty("body") StaticObjectRequest body) {
    }
}
