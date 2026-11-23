package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.domain.setup.SetupReuseCandidates;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
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
 * <p>AI 초안에서 온 구역은 싣지 않는다(S15P31A101-844, docs/design/S15P31A101-844-reuse-show-ai-drafts.md). 재사용 세션의 구역 단계는 그 맵을
 * 만든 세팅의 AI 초안을 다시 보이고(SetupService.getZoneDrafts) 사람이 수락하게 하므로, 같은 개구부가 초안과 채운 구역으로 둘 생기지 않게 한다.
 * 구역에는 출처 칸이 없어 수락 기록으로 판정한다({@link #fromShownDraft}). 판정 근거가 하나라도 없으면 사람 구역으로 보고 싣는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupZoneReuseSeed {

    private static final Logger log = LoggerFactory.getLogger(SetupZoneReuseSeed.class);

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
        // 이 재사용 세션 구역 단계에 다시 보일 AI 초안(그 맵을 만든 세팅의 마지막 초안 작업): candidate_id → 바닥 모양
        Map<String, SetupZoneOverlap.Footprint> shownDrafts = shownDrafts(sourceSessionId);
        Map<UUID, List<SetupZonePlanService.Item>> acceptedBySession = new HashMap<>();
        List<String> skipped = new ArrayList<>();
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
                        LIMIT 1) AS current_version,
                       (SELECT ssz.setup_session_id FROM setup_session_zone ssz WHERE ssz.zone_id = z.zone_id
                        ORDER BY ssz.created_at LIMIT 1) AS created_by_session
                FROM config_bundle_item i
                JOIN zone z ON z.zone_id = i.item_id AND z.zone_version = i.item_version
                WHERE i.config_version = :bundle AND i.item_type = 'ZONE'
                ORDER BY z.zone_id
                """, p, rs -> {
            if (rs.getBoolean("layout")) {
                return;
            }
            String zoneId = rs.getString("zone_id");
            ArrayNode polygon = mapper.createArrayNode();
            Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), polygon);
            List<List<Double>> polygonXy = new ArrayList<>();
            polygon.forEach(point -> polygonXy.add(doubles(point)));
            UUID createdBy = rs.getObject("created_by_session", UUID.class);
            SetupZoneOverlap.Footprint footprint = footprint(polygonXy, Versioned.real(rs, "z_min_m"), Versioned.real(rs, "z_max_m"));
            if (createdBy != null && !shownDrafts.isEmpty() && fromShownDraft(footprint,
                    acceptedBySession.computeIfAbsent(createdBy, this::acceptedDrafts), shownDrafts)) {
                skipped.add(zoneId);
                return;
            }
            int current = rs.getInt("current_version");
            boolean active = !rs.wasNull();
            if (active && current == rs.getInt("zone_version")) {
                items.add(new SetupZonePlanService.Item(zoneId, "MANUAL", null, null, null, false));
                return;
            }
            boolean guardrail = rs.getBoolean("has_guardrail");
            Boolean hasGuardrail = rs.wasNull() ? null : guardrail;
            String requirements = rs.getString("requirements");
            ZoneRequest zone = new ZoneRequest(rs.getString("name"), rs.getString("zone_kind"), rs.getString("eval_type"),
                    polygonXy, Versioned.real(rs, "z_min_m"), Versioned.real(rs, "z_max_m"), rs.getString("severity"),
                    hasGuardrail, requirements == null ? null : mapper.readTree(requirements));
            items.add(new SetupZonePlanService.Item(active ? zoneId : null, "MANUAL", null, zone, surfaces.get(zoneId), false));
        });
        if (!skipped.isEmpty()) {
            log.info("이전 맵(세팅 {}) 구역 채우기에서 AI 초안을 수락해 만든 구역 {} 은 빼고 초안으로 다시 보인다", sourceSessionId, skipped);
        }
        return items;
    }

    /**
     * 그 맵을 만든 세팅의 마지막 초안 작업의 초안(candidate_id → 바닥 모양). 조회(SetupService.getZoneDrafts)와 같은 작업을 고른다
     * (세션의 가장 최근 작업). 작업이 없거나 초안이 없으면 빈 맵이다.
     */
    Map<String, SetupZoneOverlap.Footprint> shownDrafts(UUID sourceSessionId) {
        Map<String, SetupZoneOverlap.Footprint> out = new HashMap<>();
        jdbc.query("""
                SELECT d.candidate_id, d.polygon_xy::text AS polygon_xy, d.z_min_m, d.z_max_m
                FROM setup_zone_draft d
                WHERE d.draft_run_id = (SELECT r.draft_run_id FROM setup_zone_draft_run r WHERE r.setup_session_id = :source
                                        ORDER BY r.created_at DESC LIMIT 1)
                ORDER BY d.draft_index
                """, new MapSqlParameterSource("source", sourceSessionId), rs -> {
            List<List<Double>> polygon = new ArrayList<>();
            mapper.readTree(rs.getString("polygon_xy")).forEach(point -> polygon.add(doubles(point)));
            out.putIfAbsent(rs.getString("candidate_id"),
                    footprint(polygon, Versioned.real(rs, "z_min_m"), Versioned.real(rs, "z_max_m")));
        });
        return out;
    }

    /**
     * 그 세팅 세션 구역 목록에서 AI 초안을 수락해 새로 만든 항목(source AI, candidate_id 있음, zone_id 없음, 본문 있음).
     * 세팅 완료가 이 항목마다 새 구역을 만들고 그 세션에 잇는다(setup_session_zone). 목록 행은 완료 뒤에도 남는다(지우지 않는다).
     */
    private List<SetupZonePlanService.Item> acceptedDrafts(UUID setupSessionId) {
        List<String> rows = jdbc.queryForList("SELECT items::text FROM setup_session_zone_plan WHERE setup_session_id = :id",
                new MapSqlParameterSource("id", setupSessionId), String.class);
        List<SetupZonePlanService.Item> out = new ArrayList<>();
        if (rows.isEmpty()) {
            return out;
        }
        JsonNode items = mapper.readTree(rows.getFirst());
        if (!items.isArray()) {
            return out;
        }
        for (JsonNode node : items) {
            SetupZonePlanService.Item item;
            try {
                item = mapper.treeToValue(node, SetupZonePlanService.Item.class);
            } catch (RuntimeException e) {
                continue;
            }
            if ("AI".equals(item.source()) && item.candidateId() != null && !item.candidateId().isBlank()
                    && item.zoneId() == null && item.zone() != null) {
                out.add(item);
            }
        }
        return out;
    }

    /**
     * 이 구역(묶음 버전의 바닥 모양)이 지금 다시 보일 AI 초안을 수락해 만든 구역인지. 구역을 만든 세팅의 목록에 그 초안(candidate_id)을
     * 수락한 항목이 있고, 그 항목 본문이 초안과 같은 자리이고, 구역의 지금(묶음) 모양도 그 본문과 같은 자리일 때만 그렇다고 본다
     * (같은 자리 기준은 {@link SetupZoneOverlap#sameSpot}, -758 과 같다). 사람이 다른 자리로 옮긴 구역은 사람 구역으로 남는다.
     */
    static boolean fromShownDraft(SetupZoneOverlap.Footprint zone, List<SetupZonePlanService.Item> accepted,
            Map<String, SetupZoneOverlap.Footprint> shownDrafts) {
        for (SetupZonePlanService.Item item : accepted) {
            SetupZoneOverlap.Footprint draft = shownDrafts.get(item.candidateId().strip());
            if (draft == null) {
                continue;
            }
            SetupZoneOverlap.Footprint body = SetupZoneOverlap.Footprint.of(item.zone());
            if (body.polygon().size() >= 3 && SetupZoneOverlap.sameSpot(body, draft) && SetupZoneOverlap.sameSpot(zone, body)) {
                return true;
            }
        }
        return false;
    }

    private static SetupZoneOverlap.Footprint footprint(List<List<Double>> polygon, Double zMin, Double zMax) {
        List<double[]> points = new ArrayList<>();
        for (List<Double> p : polygon) {
            if (p.size() >= 2 && p.get(0) != null && p.get(1) != null) {
                points.add(new double[] {p.get(0), p.get(1)});
            }
        }
        return new SetupZoneOverlap.Footprint(points, zMin, zMax);
    }

    private static List<Double> doubles(JsonNode array) {
        List<Double> out = new ArrayList<>();
        array.forEach(v -> out.add(v.doubleValue()));
        return out;
    }
}
