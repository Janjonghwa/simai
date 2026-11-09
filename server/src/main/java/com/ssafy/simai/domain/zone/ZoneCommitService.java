package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.geometry.StaticObjectRequest;
import com.ssafy.simai.domain.layout.LayoutZoneService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Service;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 구역·작업면 저장과 구성 묶음 만들기를 한 트랜잭션으로 한다(S15P31A101-397, docs/design/server-zone-commit.md).
 * 관제 앱이 요청 3~4개(구역 → 작업면 → 발행)로 나눠 보내 중간에 실패하면 일부만 저장되던 것을 막는다.
 * 어느 단계든 실패하면 구역·작업면·묶음이 모두 그대로이고, Tier 0 송신은 커밋 뒤에만 한다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ZoneCommitService {

    private final ZoneRepository zones;
    private final StaticObjectRepository surfaces;
    private final ConfigBundleService bundles;
    private final SiteLookup sites;
    private final Clock clock;
    private final LayoutZoneService layoutZones;

    public ZoneCommitService(ZoneRepository zones, StaticObjectRepository surfaces, ConfigBundleService bundles,
            SiteLookup sites, Clock clock, LayoutZoneService layoutZones) {
        this.zones = zones;
        this.surfaces = surfaces;
        this.bundles = bundles;
        this.sites = sites;
        this.clock = clock;
        this.layoutZones = layoutZones;
    }

    private record Changes(ObjectNode zone, ObjectNode surface, String zoneId, String archivedSurfaceId) {
    }

    private record Plan(String zoneOp, String zoneId, ZoneRequest zoneBody, String surfaceOp, String objectId,
            StaticObjectRequest surfaceBody, boolean publish) {
    }

    /** 응답: {zone_id, zone(새 버전 또는 null), surface(새 버전 또는 null), archived_surface_id, bundle(만든 묶음 또는 null)} */
    public ObjectNode commit(ZoneCommitRequest request, long userId) {
        Plan plan = plan(request);
        String siteId = sites.siteId();
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        ConfigBundleService.Published<Changes> published = bundles.publishWith(
                () -> apply(plan, siteId, userId, now), plan.publish(), userId);

        Changes changes = published.changes();
        ObjectNode response = JsonNodeFactory.instance.objectNode();
        response.put("zone_id", changes.zoneId());
        response.set("zone", changes.zone());
        response.set("surface", changes.surface());
        response.put("archived_surface_id", changes.archivedSurfaceId());
        response.set("bundle", published.bundle());
        return response;
    }

    /** 트랜잭션 전에 모양만 검사한다(값 검사는 저장소가 트랜잭션 안에서 한다) */
    private static Plan plan(ZoneCommitRequest r) {
        if (r == null || r.zone() == null || r.zone().op() == null) {
            throw invalid();
        }
        String zoneOp = r.zone().op();
        String zoneId = blank(r.zone().zoneId()) ? null : r.zone().zoneId().strip();
        boolean zoneOk = switch (zoneOp) {
            case "CREATE" -> zoneId == null && r.zone().body() != null;
            case "UPDATE" -> zoneId != null && r.zone().body() != null;
            case "ARCHIVE", "KEEP" -> zoneId != null && r.zone().body() == null;
            default -> false;
        };
        if (!zoneOk) {
            throw invalid();
        }
        ZoneCommitRequest.SurfacePart s = r.surface();
        String surfaceOp = s == null ? null : s.op();
        String objectId = s == null || blank(s.objectId()) ? null : s.objectId().strip();
        if (s != null) {
            boolean surfaceOk = surfaceOp != null && switch (surfaceOp) {
                case "CREATE" -> objectId == null && s.body() != null;
                case "UPDATE", "REPLACE" -> objectId != null && s.body() != null;
                case "ARCHIVE" -> objectId != null && s.body() == null;
                default -> false;
            };
            // 구역을 보관하면 이은 작업면도 함께 보관한다. 따로 바꿀 작업면이 없다
            if (!surfaceOk || "ARCHIVE".equals(zoneOp)) {
                throw invalid();
            }
        } else if ("KEEP".equals(zoneOp)) {
            // 아무것도 바꾸지 않는 요청
            throw invalid();
        }
        boolean publish = r.publish() == null || r.publish();
        return new Plan(zoneOp, zoneId, r.zone().body(), surfaceOp, objectId, s == null ? null : s.body(), publish);
    }

    /** 트랜잭션 안: 구역 → 작업면. 저장소가 값·버전 충돌·연결을 검사하고 실패하면 던져 모두 되돌린다 */
    private Changes apply(Plan p, String siteId, long userId, OffsetDateTime now) {
        layoutZones.lock();
        if (!"CREATE".equals(p.zoneOp())) layoutZones.denyZone(p.zoneId());
        if (p.surfaceOp() != null && !"CREATE".equals(p.surfaceOp())) layoutZones.denyObject(p.objectId());
        ObjectNode zone = null;
        String zoneId = p.zoneId();
        String archivedSurface = null;
        switch (p.zoneOp()) {
            case "CREATE" -> {
                zone = zones.create(siteId, p.zoneBody(), userId, now);
                zoneId = zone.get("zone_id").asString();
            }
            case "UPDATE" -> zone = zones.newVersion(zoneId, p.zoneBody(), userId, now);
            case "ARCHIVE" -> {
                // 다른 경로와 같은 순서(구역 → 작업면)로 잠근다(작업면을 먼저 잠그면 KEEP·UPDATE 요청과 교착할 수 있다).
                // 이미 보관한 구역은 잠글 행이 없고 zones.archive 가 멱등으로 처리한다
                zones.lock(zoneId);
                // 작업면을 먼저 닫는다(보관한 구역에 지금 작업면이 남지 않게)
                archivedSurface = surfaces.linkedTo(zoneId);
                if (archivedSurface != null) {
                    surfaces.archive(archivedSurface, now);
                }
                zones.archive(zoneId, now);
            }
            default -> zones.requireCurrent(zoneId);
        }

        ObjectNode surface = null;
        if (p.surfaceOp() != null) {
            // 다른 구역에 이어진 작업면은 이 구역 저장으로 바꾸거나 가져오지 않는다
            String owner = p.objectId() == null ? null : surfaces.zoneOf(p.objectId());
            if (owner != null && !owner.equals(zoneId)) {
                throw new BusinessException(ErrorCode.Config.SURFACE_ZONE_TAKEN);
            }
            switch (p.surfaceOp()) {
                case "CREATE" -> surface = surfaces.create(siteId, p.surfaceBody(), zoneId, userId, now);
                case "UPDATE" -> surface = surfaces.newVersion(p.objectId(), p.surfaceBody(), zoneId, userId, now);
                case "REPLACE" -> {
                    surfaces.archive(p.objectId(), now);
                    archivedSurface = p.objectId();
                    surface = surfaces.create(siteId, p.surfaceBody(), zoneId, userId, now);
                }
                default -> {
                    surfaces.archive(p.objectId(), now);
                    archivedSurface = p.objectId();
                }
            }
        }
        return new Changes(zone, surface, zoneId, archivedSurface);
    }

    private static boolean blank(String value) {
        return value == null || value.isBlank();
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Config.INVALID_ZONE_COMMIT);
    }
}
