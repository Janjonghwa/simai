package com.ssafy.simai.domain.layout;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.work.WorkUnitRepository;
import com.ssafy.simai.domain.zone.ZoneRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.util.ArrayList;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 세팅 구역을 작업 레이아웃에 참조로 추가한다. 구역과 작업면은 변경하지 않는다. */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupZoneLayoutService {
    private static final Logger log = LoggerFactory.getLogger(SetupZoneLayoutService.class);
    private final SiteLookup sites;
    private final WorkUnitRepository works;
    private final ZoneRepository zones;
    private final StaticObjectRepository objects;
    private final LayoutRepository layouts;
    private final LayoutZoneService layoutZones;
    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final ObjectMapper mapper;
    private final Clock clock;

    public SetupZoneLayoutService(SiteLookup sites, WorkUnitRepository works, ZoneRepository zones,
            StaticObjectRepository objects, LayoutRepository layouts, LayoutZoneService layoutZones,
            NamedParameterJdbcTemplate jdbc, TransactionTemplate transaction, ObjectMapper mapper, Clock clock) {
        this.sites = sites; this.works = works; this.zones = zones; this.objects = objects;
        this.layouts = layouts; this.layoutZones = layoutZones; this.jdbc = jdbc;
        this.transaction = transaction; this.mapper = mapper; this.clock = clock;
    }

    public ObjectNode add(long workId, String zoneId, long userId) {
        String site = sites.siteId();
        return transaction.execute(status -> {
            layoutZones.lock();
            ObjectNode work = works.lockSession(site, workId);
            if (!"PLANNED".equals(work.path("status").asString()))
                throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
            var args = new MapSqlParameterSource("work", workId).addValue("site", site).addValue("zone", zoneId);
            boolean linked = !jdbc.queryForList("""
                    SELECT 1 FROM setup_session_zone sz JOIN setup_session s ON s.setup_session_id = sz.setup_session_id
                    JOIN work_unit w ON w.work_id = s.work_unit_id
                    WHERE sz.zone_id = :zone AND w.work_id = :work AND s.site_id = :site
                      AND w.site_id = :site AND w.active_setup_session_id = s.setup_session_id
                    """, args, Integer.class).isEmpty();
            if (!linked) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_NOT_LINKED);
            ObjectNode zone = zones.findCurrent(site).stream().filter(z -> zoneId.equals(z.path("zone_id").asString()))
                    .findFirst().orElseThrow(() -> new BusinessException(ErrorCode.Config.ZONE_NOT_FOUND));
            int version = zone.path("zone_version").asInt();
            boolean published = !jdbc.queryForList("""
                    SELECT 1 FROM config_bundle_item i JOIN config_bundle b ON b.config_version = i.config_version
                    WHERE b.site_id = :site AND i.item_type = 'ZONE' AND i.item_id = :zone AND i.item_version = :version
                    LIMIT 1
                    """, args.addValue("version", version), Integer.class).isEmpty();
            if (!published) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_NOT_PUBLISHED);

            ObjectNode surface = objects.findCurrent(site).stream()
                    .filter(o -> "WORK_SURFACE".equals(o.path("kind").asString()) && zoneId.equals(o.path("zone_id").asString()))
                    .findFirst().orElse(null);
            if (surface != null) {
                boolean surfacePublished = !jdbc.queryForList("""
                        SELECT 1 FROM config_bundle_item z JOIN config_bundle_item o ON o.config_version = z.config_version
                        JOIN config_bundle b ON b.config_version = z.config_version
                        WHERE b.site_id = :site AND z.item_type = 'ZONE' AND z.item_id = :zone
                          AND z.item_version = :version AND o.item_type = 'STATIC_OBJECT'
                          AND o.item_id = :object AND o.item_version = :objectVersion LIMIT 1
                        """, args.addValue("object", surface.path("object_id").asString())
                        .addValue("objectVersion", surface.path("object_version").asInt()), Integer.class).isEmpty();
                if (!surfacePublished) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_NOT_PUBLISHED);
            }
            if (surface == null && !"OPENING".equals(zone.path("zone_kind").asString())) {
                if (work.path("layout_id").isNumber()) {
                    ObjectNode current = layouts.findOne(site, work.path("layout_id").asLong())
                            .orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
                    for (JsonNode entry : current.path("items")) {
                        if (("auto-" + zoneId).equals(entry.path("item_id").asString()))
                            throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
                    }
                }
                return result(workId, zoneId, null, null, "DISPLAY_ONLY");
            }
            LayoutRequest.Item item = surface != null ? surfaceItem(zone, surface) : openingItem(site, zone);
            Long id = work.path("layout_id").isNumber() ? work.path("layout_id").asLong() : null;
            ObjectNode old = id == null ? null : layouts.findOne(site, id)
                    .orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
            List<LayoutRequest.Item> items = new ArrayList<>();
            boolean updated = false;
            if (old != null) for (JsonNode entry : old.path("items")) {
                if (zoneId.equals(entry.path("source_zone_id").asString())) {
                    if (!item.itemId().equals(entry.path("item_id").asString())) {
                        boolean sameSource = item.kind().equals(entry.path("kind").asString())
                                && java.util.Objects.equals(item.sourceObjectId(), entry.path("source_object_id").isTextual()
                                        ? entry.path("source_object_id").asString() : null);
                        if (sameSource) return result(workId, zoneId, id, old.path("revision").asInt(), "ALREADY_LINKED");
                        items.add(readItem(entry));
                        continue;
                    }
                    if (readItem(entry).equals(item))
                        return result(workId, zoneId, id, old.path("revision").asInt(), "ALREADY_LINKED");
                    items.add(item);
                    updated = true;
                    continue;
                }
                items.add(readItem(entry));
            }
            if (!updated) items.add(item);
            String name = old == null ? work.path("name").asString() : old.path("name").asString();
            LayoutRequest request = new LayoutRequest(name, items, old == null ? null : old.path("revision").asInt());
            if (id == null) {
                id = layouts.create(site, request, userId, clock.instant());
                jdbc.update("UPDATE work_unit SET layout_id = :layout, updated_at = :now WHERE site_id = :site AND work_id = :work",
                        args.addValue("layout", id).addValue("now", java.time.OffsetDateTime.now(clock)));
            } else layouts.replaceSetupReferences(site, id, request, userId, clock.instant());
            return result(workId, zoneId, id, layouts.findOne(site, id).orElseThrow().path("revision").asInt(), "LINKED");
        });
    }

    /** 현재 세팅에서 만든 구역만 돌려준다. 새로고침 후 미반영 구역 재시도에 쓴다. */
    public List<String> list(long workId) {
        String site = sites.siteId();
        ObjectNode work = works.findOne(site, workId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND));
        if (!"PLANNED".equals(work.path("status").asString())) return List.of();
        return jdbc.queryForList("""
                SELECT sz.zone_id FROM setup_session_zone sz
                JOIN setup_session s ON s.setup_session_id = sz.setup_session_id
                JOIN work_unit w ON w.active_setup_session_id = s.setup_session_id AND w.work_id = s.work_unit_id
                JOIN zone z ON z.zone_id = sz.zone_id AND z.site_id = s.site_id AND z.valid_to IS NULL
                WHERE w.work_id = :work AND w.site_id = :site AND s.site_id = :site
                  AND EXISTS (SELECT 1 FROM config_bundle_item i JOIN config_bundle b ON b.config_version = i.config_version
                    WHERE b.site_id = :site AND i.item_type = 'ZONE' AND i.item_id = z.zone_id AND i.item_version = z.zone_version)
                ORDER BY sz.created_at, sz.zone_id
                """, new MapSqlParameterSource("work", workId).addValue("site", site), String.class);
    }

    /** 다른 화면에서 원본을 고친 경우에도 해당 계획 작업의 참조를 다시 맞춘다. */
    public boolean refreshForZone(String zoneId, long userId) {
        String site = sites.siteId();
        List<Long> workIds = jdbc.queryForList("""
                SELECT w.work_id FROM setup_session_zone sz
                JOIN setup_session s ON s.setup_session_id = sz.setup_session_id
                JOIN work_unit w ON w.active_setup_session_id = s.setup_session_id AND w.work_id = s.work_unit_id
                WHERE sz.zone_id = :zone AND w.site_id = :site AND s.site_id = :site AND w.status = 'PLANNED'
                """, new MapSqlParameterSource("site", site).addValue("zone", zoneId), Long.class);
        boolean ok = true;
        for (Long workId : workIds) {
            try {
                add(workId, zoneId, userId);
            } catch (RuntimeException e) {
                log.warn("Failed to refresh layout reference for work {} and zone {}", workId, zoneId, e);
                ok = false;
            }
        }
        return ok;
    }

    private ObjectNode result(long work, String zone, Long layout, Integer revision, String state) {
        ObjectNode result = mapper.createObjectNode();
        result.put("work_id", work); result.put("zone_id", zone); result.put("layout_id", layout);
        result.put("revision", revision); result.put("result", state);
        return result;
    }

    private LayoutRequest.Item surfaceItem(ObjectNode zone, ObjectNode surface) {
        JsonNode obb = surface.path("obb");
        if (!obb.path("yaw_rad").isNumber() || !Double.isFinite(obb.path("yaw_rad").asDouble()))
            throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        return new LayoutRequest.Item("auto-" + zone.path("zone_id").asString(), "WORK_SURFACE",
                vector(obb.path("center")), vector(obb.path("size")), obb.path("yaw_rad").asDouble(),
                zone.path("name").asString(), zone.path("zone_id").asString(), surface.path("object_id").asString());
    }

    private LayoutRequest.Item openingItem(String site, ObjectNode zone) {
        if (!zone.path("z_min_m").isNumber() || !zone.path("z_max_m").isNumber())
            throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        String geom = jdbc.queryForObject("""
                SELECT CASE WHEN ST_Equals(polygon, ST_OrientedEnvelope(polygon))
                    THEN ST_AsGeoJSON(ST_OrientedEnvelope(polygon)) ELSE NULL END FROM zone
                WHERE site_id = :site AND zone_id = :zone AND valid_to IS NULL
                """, new MapSqlParameterSource("site", site).addValue("zone", zone.path("zone_id").asString()), String.class);
        if (geom == null) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        JsonNode points = mapper.readTree(geom).path("coordinates").get(0);
        if (points == null || points.size() < 4) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        double x0 = points.get(0).get(0).asDouble(), y0 = points.get(0).get(1).asDouble();
        double x1 = points.get(1).get(0).asDouble(), y1 = points.get(1).get(1).asDouble();
        double x2 = points.get(2).get(0).asDouble(), y2 = points.get(2).get(1).asDouble();
        double min = zone.path("z_min_m").asDouble(), max = zone.path("z_max_m").asDouble();
        double sx = Math.hypot(x1-x0, y1-y0), sy = Math.hypot(x2-x1, y2-y1);
        if (!(sx > 0 && sy > 0 && max > min)) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        return new LayoutRequest.Item("auto-" + zone.path("zone_id").asString(), "OPENING",
                List.of((x0+x2)/2, (y0+y2)/2, (min+max)/2), List.of(sx, sy, max-min), Math.atan2(y1-y0, x1-x0),
                zone.path("name").asString(), zone.path("zone_id").asString(), null);
    }

    private LayoutRequest.Item readItem(JsonNode node) {
        return new LayoutRequest.Item(node.path("item_id").asString(), node.path("kind").asString(),
                vector(node.path("center")), vector(node.path("size")), node.path("yaw_rad").asDouble(),
                node.path("label").isTextual() ? node.path("label").asString() : null,
                node.path("source_zone_id").isTextual() ? node.path("source_zone_id").asString() : null,
                node.path("source_object_id").isTextual() ? node.path("source_object_id").asString() : null);
    }

    private List<Double> vector(JsonNode node) {
        if (!node.isArray() || node.size() != 3) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        for (JsonNode value : node) if (!value.isNumber() || !Double.isFinite(value.asDouble()))
            throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        return List.of(node.get(0).asDouble(), node.get(1).asDouble(), node.get(2).asDouble());
    }
}
