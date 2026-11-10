package com.ssafy.simai.domain.layout;

import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.zone.ZoneRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.OffsetDateTime;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 호출자의 구성 묶음 트랜잭션 안에서만 실행한다. */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class LayoutZoneService {
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final LayoutRepository layouts;
    private final ZoneRepository zones;
    private final StaticObjectRepository objects;
    public LayoutZoneService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, LayoutRepository layouts,
            ZoneRepository zones, StaticObjectRepository objects) {
        this.jdbc = jdbc; this.mapper = mapper; this.layouts = layouts; this.zones = zones; this.objects = objects;
    }
    public void denyZone(String id) { deny("zone", "zone_id", id); }
    public void denyObject(String id) { deny("static_object", "object_id", id); }
    public void lock() {
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.layout_zone'))", new MapSqlParameterSource());
    }
    private void deny(String table, String column, String id) {
        if (id != null && !jdbc.queryForList("SELECT 1 FROM " + table + " WHERE " + column
                + " = :id AND layout_item_id IS NOT NULL LIMIT 1", new MapSqlParameterSource("id", id), Integer.class).isEmpty())
            throw new BusinessException(ErrorCode.Config.LAYOUT_ZONE_LOCKED);
    }
    public Long activeWork(String site, long layout) {
        return jdbc.queryForList("SELECT work_id FROM work_unit WHERE site_id = :site AND layout_id = :layout AND status = 'IN_PROGRESS'",
                new MapSqlParameterSource("site", site).addValue("layout", layout), Long.class).stream().findFirst().orElse(null);
    }
    public void sync(String site, long work, Long layout, long user, OffsetDateTime now) {
        if (layout == null) return;
        ObjectNode l = layouts.findOne(site, layout).orElseThrow(() -> new BusinessException(ErrorCode.Layout.LAYOUT_NOT_FOUND));
        // 원본이 사라진 참조를 가상의 형상으로 대체하지 않는다. 작업 시작도 같은 트랜잭션에서 되돌린다.
        for (var item : l.path("items")) {
            String zone = item.path("source_zone_id").asString("");
            String object = item.path("source_object_id").asString("");
            if (zone.isEmpty()) continue;
            boolean exists = !jdbc.queryForList("""
                    SELECT 1 FROM zone z WHERE z.site_id = :site AND z.zone_id = :zone AND z.valid_to IS NULL
                      AND (:kind <> 'OPENING' OR z.zone_kind = 'OPENING')
                      AND (:object = '' OR EXISTS (SELECT 1 FROM static_object o WHERE o.site_id = :site
                        AND o.object_id = :object AND o.zone_id = z.zone_id AND o.kind = 'WORK_SURFACE' AND o.valid_to IS NULL))
                    """, new MapSqlParameterSource("site", site).addValue("zone", zone).addValue("object", object)
                    .addValue("kind", item.path("kind").asString("")), Integer.class).isEmpty();
            if (!exists) throw new BusinessException(ErrorCode.Layout.SETUP_ZONE_INCOMPLETE);
        }
        var plans = LayoutZonePlans.from(l.path("items"));
        Set<String> wanted = new HashSet<>();
        plans.forEach(p -> wanted.add(p.itemId()));
        for (var z : zones.findCurrent(site)) {
            if (z.path("work_id").asLong(-1) == work && z.hasNonNull("layout_item_id")
                    && !wanted.contains(z.path("layout_item_id").asString())) closeZone(z.path("zone_id").asString(), now);
        }
        for (var p : plans) {
            String zid = id("zone", "zone_id", work, p.itemId());
            String signature = mapper.writeValueAsString(p.zone());
            if (zid == null) {
                zid = zones.create(site, p.zone(), user, now).path("zone_id").asString();
                mark("zone", "zone_id", zid, work, layout, p.itemId(), signature);
            } else if (!same("zone", "zone_id", zid, layout, signature)) {
                zones.newVersion(zid, p.zone(), user, now);
                mark("zone", "zone_id", zid, work, layout, p.itemId(), signature);
            }
            String oid = objects.linkedTo(zid);
            if (p.surface() == null) {
                if (oid != null) objects.archive(oid, now);
            } else {
                String sig = mapper.writeValueAsString(p.surface());
                if (oid == null) {
                    oid = objects.create(site, p.surface(), zid, user, now).path("object_id").asString();
                    mark("static_object", "object_id", oid, work, layout, p.itemId(), sig);
                } else if (!same("static_object", "object_id", oid, layout, sig)) {
                    objects.newVersion(oid, p.surface(), zid, user, now);
                    mark("static_object", "object_id", oid, work, layout, p.itemId(), sig);
                }
            }
        }
    }
    public void end(String site, long work, OffsetDateTime now) {
        for (var z : zones.findCurrent(site)) if (z.path("work_id").asLong(-1) == work && z.hasNonNull("layout_item_id"))
            closeZone(z.path("zone_id").asString(), now);
        for (var o : objects.findCurrent(site)) if (o.path("work_id").asLong(-1) == work && o.hasNonNull("layout_item_id"))
            objects.archive(o.path("object_id").asString(), now);
    }
    private void closeZone(String id, OffsetDateTime now) {
        zones.lock(id);
        String oid = objects.linkedTo(id);
        if (oid != null) objects.archive(oid, now);
        zones.archive(id, now);
    }
    private String id(String table, String col, long work, String item) {
        return jdbc.queryForList("SELECT " + col + " FROM " + table + " WHERE work_id = :work AND layout_item_id = :item AND valid_to IS NULL",
                new MapSqlParameterSource("work", work).addValue("item", item), String.class).stream().findFirst().orElse(null);
    }
    private boolean same(String table, String col, String id, long layout, String signature) {
        return Boolean.TRUE.equals(jdbc.queryForObject("SELECT layout_id = :layout AND layout_signature = CAST(:sig AS jsonb) FROM "
                + table + " WHERE " + col + " = :id AND valid_to IS NULL",
                new MapSqlParameterSource("id", id).addValue("layout", layout).addValue("sig", signature), Boolean.class));
    }
    private void mark(String table, String col, String id, long work, long layout, String item, String signature) {
        jdbc.update("UPDATE " + table + " SET work_id = :work, layout_id = :layout, layout_item_id = :item, layout_signature = CAST(:sig AS jsonb) WHERE "
                + col + " = :id AND valid_to IS NULL", new MapSqlParameterSource("id", id).addValue("work", work)
                .addValue("layout", layout).addValue("item", item).addValue("sig", signature));
    }
}
