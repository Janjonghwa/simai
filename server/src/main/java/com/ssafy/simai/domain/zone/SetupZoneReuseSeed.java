package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.domain.setup.SetupReuseCandidates;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.function.Function;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

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
 * <p>AI 초안에서 온 구역은 싣지 않는다(S15P31A101-844·-864, docs/design/S15P31A101-844-reuse-show-ai-drafts.md,
 * S15P31A101-864-reuse-adjusted-ai-zone.md). 재사용 세션의 구역 단계는 그 맵을 만든 세팅의 AI 초안을 다시 보이고(SetupService.getZoneDrafts)
 * 사람이 수락하게 하므로, 같은 개구부가 초안과 채운 구역으로 둘 생기지 않게 한다. 구역에는 출처 칸이 없어 수락 기록과 모양으로 초안과 짝짓는다
 * ({@link #pair}). 짝지은 구역의 지금 모양·이름·심각도는 그 초안의 조정 정보로 초안 조회에 붙는다({@link #draftAdjustments}).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupZoneReuseSeed {

    private static final Logger log = LoggerFactory.getLogger(SetupZoneReuseSeed.class);

    /** 구역이 수락 때 본문 그대로인지 볼 좌표·높이 차이(m). 서버 float4 높이(1.4 → 1.3999999)를 같다고 본다. */
    private static final double SAME_EPS_M = 1e-3;

    /** 그 맵의 마지막 묶음 구역 한 줄(레이아웃 구역 제외). currentVersion 은 지금 관제 버전(관제 중이 아니면 null) */
    record BundleZone(String zoneId, int zoneVersion, Integer currentVersion, UUID createdBy, ZoneRequest zone,
            StaticObjectRequest surface) {
    }

    /** 이 재사용 세션 구역 단계에 다시 보일 AI 초안(목록 순서 = draft_index) */
    record ShownDraft(String candidateId, SetupZoneOverlap.Footprint footprint) {
    }

    /** 구역과 짝지은 초안. 수락 기록(-844)으로 짝지었으면 그 수락 본문, 품음(-864)으로 짝지었으면 null */
    record DraftMatch(String candidateId, ZoneRequest acceptedBody) {
    }

    private record Loaded(List<BundleZone> zones, List<ShownDraft> drafts) {
    }

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
        Loaded loaded = load(siteId, sourceSessionId, sourceCalibration);
        if (loaded == null) {
            return List.of();
        }
        Map<String, DraftMatch> matches = match(loaded);
        List<String> skipped = new ArrayList<>();
        List<SetupZonePlanService.Item> items = new ArrayList<>();
        for (BundleZone zone : loaded.zones()) {
            if (matches.containsKey(zone.zoneId())) {
                skipped.add(zone.zoneId());
                continue;
            }
            boolean active = zone.currentVersion() != null;
            if (active && zone.currentVersion() == zone.zoneVersion()) {
                items.add(new SetupZonePlanService.Item(zone.zoneId(), "MANUAL", null, null, null, false));
                continue;
            }
            items.add(new SetupZonePlanService.Item(active ? zone.zoneId() : null, "MANUAL", null, zone.zone(), zone.surface(), false));
        }
        if (!skipped.isEmpty()) {
            log.info("이전 맵(세팅 {}) 구역 채우기에서 AI 초안에서 온 구역 {} 은 빼고 초안으로 다시 보인다", sourceSessionId, skipped);
        }
        return items;
    }

    /**
     * 이 재사용 세션에 다시 보일 초안 중 짝지은 구역이 있는 것의 조정 정보(candidate_id → 구역의 그 맵 때 모양·이름·심각도 등, S15P31A101-864).
     * 채우기({@link #items})가 빼는 구역과 같은 짝이다. 수락 기록으로 짝지었고 구역이 수락 때 본문 그대로면(사람이 고치지 않음) 붙이지 않는다:
     * 초안을 다시 수락하면 같은 구역이 나온다. 품음으로 짝지었으면(수락 기록 없음) 화면이 초안에서 만든 본문을 알 수 없어 늘 붙인다.
     * 응답 칸: zone_id, zone_version, name, zone_kind, eval_type, polygon_xy, z_min_m, z_max_m, severity, has_guardrail, requirements,
     * surface(이은 작업면 본문, 없으면 null). 묶음이 없거나 다시 보일 초안이 없으면 빈 맵이다.
     */
    public Map<String, ObjectNode> draftAdjustments(String siteId, UUID sourceSessionId, String sourceCalibration) {
        Map<String, ObjectNode> out = new LinkedHashMap<>();
        Loaded loaded = load(siteId, sourceSessionId, sourceCalibration);
        if (loaded == null) {
            return out;
        }
        Map<String, DraftMatch> matches = match(loaded);
        for (BundleZone zone : loaded.zones()) {
            DraftMatch m = matches.get(zone.zoneId());
            if (m == null || (m.acceptedBody() != null && sameZone(zone.zone(), m.acceptedBody()))) {
                continue;
            }
            out.put(m.candidateId(), adjustment(zone));
        }
        return out;
    }

    private Loaded load(String siteId, UUID sourceSessionId, String sourceCalibration) {
        String bundle = latestBundle(siteId, sourceSessionId, sourceCalibration);
        if (bundle == null) {
            return null;
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
        List<BundleZone> zones = new ArrayList<>();
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
            int current = rs.getInt("current_version");
            Integer currentVersion = rs.wasNull() ? null : current;
            boolean guardrail = rs.getBoolean("has_guardrail");
            Boolean hasGuardrail = rs.wasNull() ? null : guardrail;
            String requirements = rs.getString("requirements");
            ZoneRequest zone = new ZoneRequest(rs.getString("name"), rs.getString("zone_kind"), rs.getString("eval_type"),
                    polygonXy, Versioned.real(rs, "z_min_m"), Versioned.real(rs, "z_max_m"), rs.getString("severity"),
                    hasGuardrail, requirements == null ? null : mapper.readTree(requirements));
            zones.add(new BundleZone(zoneId, rs.getInt("zone_version"), currentVersion,
                    rs.getObject("created_by_session", UUID.class), zone, surfaces.get(zoneId)));
        });
        return new Loaded(zones, shownDrafts(sourceSessionId));
    }

    private Map<String, DraftMatch> match(Loaded loaded) {
        if (loaded.drafts().isEmpty()) {
            // 다시 보일 초안이 없으면 아무것도 빼지 않는다(빼면 그 구역을 되살릴 길이 없다)
            return Map.of();
        }
        Map<UUID, List<SetupZonePlanService.Item>> accepted = new HashMap<>();
        return pair(loaded.zones(), loaded.drafts(), session -> accepted.computeIfAbsent(session, this::acceptedDrafts));
    }

    /**
     * 그 맵을 만든 세팅의 마지막 초안 작업의 초안(draft_index 순). 조회(SetupService.getZoneDrafts)와 같은 작업을 고른다
     * (세션의 가장 최근 작업). 작업이 없거나 초안이 없으면 빈 목록이다. 같은 candidate_id 는 앞의 것만 쓴다.
     */
    List<ShownDraft> shownDrafts(UUID sourceSessionId) {
        Map<String, SetupZoneOverlap.Footprint> out = new LinkedHashMap<>();
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
        List<ShownDraft> drafts = new ArrayList<>();
        out.forEach((id, fp) -> drafts.add(new ShownDraft(id, fp)));
        return drafts;
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
     * 묶음 구역과 다시 보일 초안을 짝짓는다(구역 ID → 짝, 한 구역은 한 초안에만, 한 초안은 한 구역에만). 결정적이다.
     * <ol>
     *   <li><b>수락 기록(-844)</b>: 구역을 만든 세팅의 목록에 그 초안(candidate_id)을 수락한 항목이 있고, 그 항목 본문이 초안과 같은 자리이고,
     *       구역의 그 맵 때 모양도 그 본문과 같은 자리({@link SetupZoneOverlap#sameSpot})다. 구역 ID 순으로 보고, 이미 짝지은 초안은 건너뛴다.
     *   <li><b>품음(-864)</b>: 남은 구역·초안 쌍 중 높이 구간이 겹치고 초안 넓이의 {@value SetupZoneOverlap#MIN_CONTAINED_SHARE} 이상이
     *       구역 안에 든 쌍({@link SetupZoneOverlap#containedShare}). 품은 몫이 큰 쌍 → 구역이 작은 쌍(초안에 더 꼭 맞음) → 초안 순서
     *       (draft_index) → 구역 ID 순으로 줄 세워 앞에서부터 짝짓는다(이미 짝지은 구역·초안은 건너뛴다). 사람이 크기를 키운 AI 구역처럼 수락
     *       기록이 없거나 크기가 달라 1 에 안 걸리는 구역도 초안을 대부분 품으면 그 초안에서 온 구역으로 본다(2026-11-24 사용자 결정 1안).
     * </ol>
     */
    static Map<String, DraftMatch> pair(List<BundleZone> zones, List<ShownDraft> drafts,
            Function<UUID, List<SetupZonePlanService.Item>> accepted) {
        Map<String, DraftMatch> out = new LinkedHashMap<>();
        if (drafts.isEmpty()) {
            return out;
        }
        Map<String, Integer> order = new HashMap<>();
        Map<String, SetupZoneOverlap.Footprint> byId = new HashMap<>();
        for (int i = 0; i < drafts.size(); i++) {
            order.putIfAbsent(drafts.get(i).candidateId(), i);
            byId.putIfAbsent(drafts.get(i).candidateId(), drafts.get(i).footprint());
        }
        List<BundleZone> sorted = new ArrayList<>(zones);
        sorted.sort(Comparator.comparing(BundleZone::zoneId));
        Set<String> taken = new HashSet<>();
        // 1. 수락 기록
        for (BundleZone zone : sorted) {
            if (zone.createdBy() == null) {
                continue;
            }
            SetupZoneOverlap.Footprint footprint = SetupZoneOverlap.Footprint.of(zone.zone());
            for (SetupZonePlanService.Item item : accepted.apply(zone.createdBy())) {
                String candidate = item.candidateId().strip();
                SetupZoneOverlap.Footprint draft = byId.get(candidate);
                if (draft == null || taken.contains(candidate)) {
                    continue;
                }
                SetupZoneOverlap.Footprint body = SetupZoneOverlap.Footprint.of(item.zone());
                if (body.polygon().size() >= 3 && SetupZoneOverlap.sameSpot(body, draft) && SetupZoneOverlap.sameSpot(footprint, body)) {
                    out.put(zone.zoneId(), new DraftMatch(candidate, item.zone()));
                    taken.add(candidate);
                    break;
                }
            }
        }
        // 2. 품음
        record Candidate(BundleZone zone, String candidateId, long share, double zoneArea, int draftOrder) {
        }
        List<Candidate> pairs = new ArrayList<>();
        for (BundleZone zone : sorted) {
            if (out.containsKey(zone.zoneId())) {
                continue;
            }
            SetupZoneOverlap.Footprint footprint = SetupZoneOverlap.Footprint.of(zone.zone());
            double zoneArea = SetupZoneOverlap.area(footprint.polygon());
            for (ShownDraft draft : drafts) {
                if (taken.contains(draft.candidateId()) || order.get(draft.candidateId()) == null) {
                    continue;
                }
                double share = SetupZoneOverlap.containedShare(footprint, draft.footprint());
                if (share >= SetupZoneOverlap.MIN_CONTAINED_SHARE) {
                    // 부동소수 오차로 순서가 흔들리지 않게 몫은 1e-6 단위로 견준다
                    pairs.add(new Candidate(zone, draft.candidateId(), Math.round(share * 1e6), zoneArea,
                            order.get(draft.candidateId())));
                }
            }
        }
        pairs.sort(Comparator.comparingLong(Candidate::share).reversed()
                .thenComparingDouble(Candidate::zoneArea)
                .thenComparingInt(Candidate::draftOrder)
                .thenComparing(c -> c.zone().zoneId()));
        for (Candidate c : pairs) {
            if (out.containsKey(c.zone().zoneId()) || taken.contains(c.candidateId())) {
                continue;
            }
            out.put(c.zone().zoneId(), new DraftMatch(c.candidateId(), null));
            taken.add(c.candidateId());
        }
        return out;
    }

    /** 구역이 수락 때 본문 그대로인지(이름·종류·판정·심각도가 같고, 높이·꼭짓점이 {@value #SAME_EPS_M} m 안) */
    static boolean sameZone(ZoneRequest zone, ZoneRequest body) {
        if (!Objects.equals(strip(zone.name()), strip(body.name())) || !Objects.equals(zone.zoneKind(), body.zoneKind())
                || !Objects.equals(zone.evalType(), body.evalType()) || !Objects.equals(zone.severity(), body.severity())
                || !near(zone.zMinM(), body.zMinM()) || !near(zone.zMaxM(), body.zMaxM())) {
            return false;
        }
        List<double[]> a = SetupZoneOverlap.Footprint.of(zone).polygon();
        List<double[]> b = SetupZoneOverlap.Footprint.of(body).polygon();
        if (a.size() != b.size()) {
            return false;
        }
        for (double[] p : a) {
            if (b.stream().noneMatch(q -> Math.abs(p[0] - q[0]) <= SAME_EPS_M && Math.abs(p[1] - q[1]) <= SAME_EPS_M)) {
                return false;
            }
        }
        return true;
    }

    /** 초안 조회에 붙이는 조정 정보(구역 생성 본문과 같은 칸 + zone_id·zone_version·surface) */
    private ObjectNode adjustment(BundleZone bundleZone) {
        ZoneRequest zone = bundleZone.zone();
        ObjectNode out = mapper.createObjectNode();
        out.put("zone_id", bundleZone.zoneId());
        out.put("zone_version", bundleZone.zoneVersion());
        out.put("name", zone.name());
        out.put("zone_kind", zone.zoneKind());
        out.put("eval_type", zone.evalType());
        ArrayNode polygon = out.putArray("polygon_xy");
        zone.polygonXy().forEach(point -> polygon.addArray().add(point.get(0)).add(point.get(1)));
        putMeters(out, "z_min_m", zone.zMinM());
        putMeters(out, "z_max_m", zone.zMaxM());
        out.put("severity", zone.severity());
        if (zone.hasGuardrail() == null) {
            out.putNull("has_guardrail");
        } else {
            out.put("has_guardrail", zone.hasGuardrail());
        }
        if (zone.requirements() == null || zone.requirements().isNull()) {
            out.putNull("requirements");
        } else {
            out.set("requirements", zone.requirements().deepCopy());
        }
        StaticObjectRequest surface = bundleZone.surface();
        if (surface == null) {
            out.putNull("surface");
        } else {
            ObjectNode s = out.putObject("surface");
            s.put("kind", surface.kind());
            s.put("name", surface.name());
            ObjectNode obb = s.putObject("obb");
            ArrayNode center = obb.putArray("center");
            surface.obb().center().forEach(center::add);
            ArrayNode size = obb.putArray("size");
            surface.obb().size().forEach(size::add);
            obb.put("yaw_rad", surface.obb().yawRad() == null ? 0.0 : surface.obb().yawRad());
            putMeters(s, "top_height_m", surface.topHeightM());
            if (surface.topHeightSigmaM() == null) {
                s.putNull("top_height_sigma_m");
            } else {
                s.put("top_height_sigma_m", surface.topHeightSigmaM());
            }
        }
        return out;
    }

    /** 높이(m). DB float4 값(1.3999999)을 1e-6 m 로 맞춰 싣는다. */
    private static void putMeters(ObjectNode node, String field, Double value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, Math.round(value * 1e6) / 1e6);
        }
    }

    private static boolean near(Double a, Double b) {
        return a == null ? b == null : b != null && Math.abs(a - b) <= SAME_EPS_M;
    }

    private static String strip(String s) {
        return s == null ? null : s.strip();
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
