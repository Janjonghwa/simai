package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.domain.setup.SetupReuseCandidates;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;

/**
 * 이전 맵 재사용 세팅의 구역 목록 채우기(S15P31A101-714, docs/design/S15P31A101-714-reuse-chosen-map.md).
 * <p>고른 맵의 구역 = 그 맵의 보정(그 맵을 만든 보정과, 그 맵을 재사용하며 같은 내용으로 다시 낸 보정)으로 마지막에 발행한 구성 묶음의 구역·작업면
 * 버전(config_bundle_item)이다. 지금 관제 구역이 다른 맵(다른 World)의 것이어도 고른 맵의 좌표를 쓴다. 항목 규칙(적용은 {@link SetupZoneApplier}):
 * <ul>
 *   <li>지금도 같은 버전으로 관제 중인 구역: zone_id 만(그대로).
 *   <li>지금 다른 버전으로 관제 중인 구역: zone_id + 그 맵 때 본문(새 버전으로 되돌림) + 이은 작업면 본문.
 *   <li>지금 관제 중이 아닌 구역: 그 맵 때 본문으로 새 구역(zone_id 는 새로 난다) + 이은 작업면 본문.
 *   <li>레이아웃이 만든 구역은 레이아웃이 관리하므로 싣지 않는다. 목록에 없는 지금 관제 구역은 완료 때 보관된다(기존 적용 규칙).
 * </ul>
 * 그 맵의 묶음이 없으면 빈 목록이다(다른 맵의 구역을 지어 넣지 않는다). 확인 표시(needs_review)는 달지 않는다(같은 보정·같은 World).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupZoneReuseSeed {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SetupReuseCandidates candidates;

    SetupZoneReuseSeed(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SetupReuseCandidates candidates) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.candidates = candidates;
    }

    /** 이 맵의 마지막 묶음(없으면 null) */
    String latestBundle(String siteId, UUID sourceSessionId, String sourceCalibration) {
        List<String> versions = candidates.calibrationVersionsOf(sourceSessionId, sourceCalibration);
        return jdbc.queryForList("""
                SELECT config_version FROM config_bundle WHERE site_id = :site AND calibration_version IN (:versions)
                ORDER BY issued_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId).addValue("versions", versions), String.class)
                .stream().findFirst().orElse(null);
    }

    List<SetupZonePlanService.Item> items(String siteId, UUID sourceSessionId, String sourceCalibration) {
        String bundle = latestBundle(siteId, sourceSessionId, sourceCalibration);
        if (bundle == null) {
            return List.of();
        }
        MapSqlParameterSource p = new MapSqlParameterSource("bundle", bundle).addValue("site", siteId);
        // 그 묶음의 작업면 중 구역에 이은 것: 구역 ID → 그 버전 본문
        Map<String, StaticObjectRequest> surfaces = new HashMap<>();
        jdbc.query("""
                SELECT o.zone_id, o.kind, o.name, o.obb::text AS obb, o.top_height_m
                FROM config_bundle_item i
                JOIN static_object o ON o.object_id = i.item_id AND o.object_version = i.item_version
                WHERE i.config_version = :bundle AND i.item_type = 'STATIC_OBJECT' AND o.zone_id IS NOT NULL
                ORDER BY o.object_id
                """, p, rs -> {
            JsonNode obb = mapper.readTree(rs.getString("obb"));
            surfaces.putIfAbsent(rs.getString("zone_id"), new StaticObjectRequest(rs.getString("kind"), rs.getString("name"),
                    new StaticObjectRequest.Obb(doubles(obb.path("center")), doubles(obb.path("size")),
                            obb.path("yaw_rad").isNumber() ? obb.path("yaw_rad").doubleValue() : null),
                    Versioned.real(rs, "top_height_m"),
                    obb.path("top_height_sigma_m").isNumber() ? obb.path("top_height_sigma_m").doubleValue() : null, null));
        });
        List<SetupZonePlanService.Item> items = new ArrayList<>();
        jdbc.query("""
                SELECT z.zone_id, z.zone_version, z.name, z.zone_kind, z.eval_type, ST_AsGeoJSON(z.polygon) AS polygon,
                       z.z_min_m, z.z_max_m, z.severity, z.has_guardrail, z.requirements::text AS requirements,
                       z.layout_item_id IS NOT NULL AS layout,
                       (SELECT c.zone_version FROM zone c WHERE c.zone_id = z.zone_id AND c.site_id = :site AND c.valid_to IS NULL
                        LIMIT 1) AS current_version
                FROM config_bundle_item i
                JOIN zone z ON z.zone_id = i.item_id AND z.zone_version = i.item_version
                WHERE i.config_version = :bundle AND i.item_type = 'ZONE'
                ORDER BY z.zone_id
                """, p, rs -> {
            if (rs.getBoolean("layout")) {
                return;
            }
            String zoneId = rs.getString("zone_id");
            int current = rs.getInt("current_version");
            boolean active = !rs.wasNull();
            if (active && current == rs.getInt("zone_version")) {
                items.add(new SetupZonePlanService.Item(zoneId, "MANUAL", null, null, null, false));
                return;
            }
            ArrayNode polygon = mapper.createArrayNode();
            Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), polygon);
            List<List<Double>> polygonXy = new ArrayList<>();
            polygon.forEach(point -> polygonXy.add(doubles(point)));
            boolean guardrail = rs.getBoolean("has_guardrail");
            Boolean hasGuardrail = rs.wasNull() ? null : guardrail;
            String requirements = rs.getString("requirements");
            ZoneRequest zone = new ZoneRequest(rs.getString("name"), rs.getString("zone_kind"), rs.getString("eval_type"),
                    polygonXy, Versioned.real(rs, "z_min_m"), Versioned.real(rs, "z_max_m"), rs.getString("severity"),
                    hasGuardrail, requirements == null ? null : mapper.readTree(requirements));
            items.add(new SetupZonePlanService.Item(active ? zoneId : null, "MANUAL", null, zone, surfaces.get(zoneId), false));
        });
        return items;
    }

    private static List<Double> doubles(JsonNode array) {
        List<Double> out = new ArrayList<>();
        array.forEach(v -> out.add(v.doubleValue()));
        return out;
    }
}
