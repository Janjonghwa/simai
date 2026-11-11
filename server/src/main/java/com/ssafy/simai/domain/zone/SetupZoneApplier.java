package com.ssafy.simai.domain.zone;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.geometry.StaticObjectRepository;
import com.ssafy.simai.domain.layout.LayoutZoneService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Propagation;
import org.springframework.transaction.annotation.Transactional;

/**
 * 세팅 완료(C17) 때 세션의 구역 목록을 관제 구역으로 한 번에 적용한다(S15P31A101-612, docs/design/setup-session-zones.md).
 *
 * <ul>
 *   <li>항목에 zone_id 가 있으면 그 구역을 잇는다: zone 본문이 있으면 UPDATE(새 버전), 없으면 KEEP. zone_id 가 이어져 구역별 사건 기록이 끊기지 않는다.
 *   <li>zone_id 가 없으면 CREATE(세션에 연결, setup_session_zone).
 *   <li>목록에 없는 지금 관제 구역은 ARCHIVE(이은 작업면도). 레이아웃이 만든 구역은 레이아웃이 관리하므로 건드리지 않는다.
 *   <li>surface 본문이 있으면 그 구역의 작업면을 UPDATE(이어진 작업면이 있을 때)하거나 CREATE 한다.
 * </ul>
 * 부르는 쪽(세팅 완료) 트랜잭션 안에서만 돈다. 구성 묶음은 만들지 않는다(세팅 완료가 Scene·보정과 함께 한 번 발행한다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupZoneApplier {

    public record ApplyResult(List<String> created, List<String> updated, List<String> kept, List<String> archived) {
    }

    private final SetupZonePlanService plans;
    private final ZoneCommitService commits;
    private final StaticObjectRepository surfaces;
    private final NamedParameterJdbcTemplate jdbc;
    private final SiteLookup sites;
    private final Clock clock;
    private final LayoutZoneService layoutZones;

    public SetupZoneApplier(SetupZonePlanService plans, ZoneCommitService commits, StaticObjectRepository surfaces,
            NamedParameterJdbcTemplate jdbc, SiteLookup sites, Clock clock, LayoutZoneService layoutZones) {
        this.layoutZones = layoutZones;
        this.plans = plans;
        this.commits = commits;
        this.surfaces = surfaces;
        this.jdbc = jdbc;
        this.sites = sites;
        this.clock = clock;
    }

    /**
     * sameWorld: 이 세팅이 지금 관제 구역과 같은 World 인지(이전 모델 쓰기, 또는 새로 만들기에서 world_anchor.accepted).
     * false 인데 목록에 기존 zone_id 가 있으면 409 SETUP_ZONE_WORLD_MISMATCH(좌표가 새 3D 와 맞지 않는다).
     * 목록의 zone_id 가 지금 관제 구역이 아니면 409 SETUP_ZONE_STALE.
     */
    @Transactional(propagation = Propagation.MANDATORY)
    public ApplyResult apply(UUID setupSessionId, boolean sameWorld, long actorUserId) {
        // 구역 저장(zones/commit)과 같은 잠금을 먼저 잡아, 지금 관제 구역 목록을 읽는 사이에 구역이 바뀌지 않게 한다
        layoutZones.lock();
        requireActiveSession(setupSessionId);
        List<SetupZonePlanService.Item> items = plans.lockItems(setupSessionId);
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        MapSqlParameterSource site = new MapSqlParameterSource("site", sites.siteId());
        Set<String> current = new LinkedHashSet<>(jdbc.queryForList("""
                SELECT DISTINCT zone_id FROM zone
                WHERE site_id = :site AND valid_to IS NULL AND layout_item_id IS NULL ORDER BY zone_id
                """, site, String.class));
        // 레이아웃이 만든 구역은 레이아웃이 관리한다. 목록에 들어 있어도(GET /api/zones 로 채운 목록) 그대로 둔다
        Set<String> layoutMade = new LinkedHashSet<>(jdbc.queryForList("""
                SELECT DISTINCT zone_id FROM zone WHERE site_id = :site AND valid_to IS NULL AND layout_item_id IS NOT NULL
                """, site, String.class));
        Set<String> listed = new LinkedHashSet<>();
        for (SetupZonePlanService.Item item : items) {
            if (item.zoneId() == null) continue;
            if (!sameWorld) throw new BusinessException(ErrorCode.Config.SETUP_ZONE_WORLD_MISMATCH);
            if (!current.contains(item.zoneId()) && !layoutMade.contains(item.zoneId())) {
                throw new BusinessException(ErrorCode.Config.SETUP_ZONE_STALE);
            }
            listed.add(item.zoneId());
        }

        List<String> archived = new ArrayList<>();
        for (String zoneId : current) {
            if (listed.contains(zoneId)) continue;
            commits.applyInTransaction(new ZoneCommitRequest(
                    new ZoneCommitRequest.ZonePart("ARCHIVE", zoneId, null), null, false, null), actorUserId, now);
            archived.add(zoneId);
        }

        List<String> created = new ArrayList<>();
        List<String> updated = new ArrayList<>();
        List<String> kept = new ArrayList<>();
        for (SetupZonePlanService.Item item : items) {
            if (item.zoneId() == null) {
                ZoneCommitRequest.SurfacePart surface = item.surface() == null ? null
                        : new ZoneCommitRequest.SurfacePart("CREATE", null, item.surface());
                created.add(commits.applyInTransaction(new ZoneCommitRequest(
                        new ZoneCommitRequest.ZonePart("CREATE", null, item.zone()), surface, false,
                        setupSessionId.toString()), actorUserId, now));
                continue;
            }
            if (layoutMade.contains(item.zoneId()) && item.zone() == null && item.surface() == null) {
                kept.add(item.zoneId());
                continue;
            }
            ZoneCommitRequest.SurfacePart surface = null;
            if (item.surface() != null) {
                String linked = surfaces.linkedTo(item.zoneId());
                surface = linked == null
                        ? new ZoneCommitRequest.SurfacePart("CREATE", null, item.surface())
                        : new ZoneCommitRequest.SurfacePart("UPDATE", linked, item.surface());
            }
            if (item.zone() != null) {
                commits.applyInTransaction(new ZoneCommitRequest(
                        new ZoneCommitRequest.ZonePart("UPDATE", item.zoneId(), item.zone()), surface, false, null),
                        actorUserId, now);
                updated.add(item.zoneId());
            } else {
                if (surface != null) {
                    commits.applyInTransaction(new ZoneCommitRequest(
                            new ZoneCommitRequest.ZonePart("KEEP", item.zoneId(), null), surface, false, null),
                            actorUserId, now);
                }
                kept.add(item.zoneId());
            }
        }
        return new ApplyResult(created, updated, kept, archived);
    }

    /** 이 현장의 작업에 지금 걸린 세션이고, 작업이 PLANNED 이고, 구역을 만들 수 있는 단계인지 본다. 아니면 409 SETUP_ZONES_CLOSED */
    private void requireActiveSession(UUID setupSessionId) {
        List<Long> found = jdbc.queryForList("""
                SELECT w.work_id FROM setup_session s JOIN work_unit w ON w.work_id = s.work_unit_id
                WHERE s.setup_session_id = :session AND s.site_id = :site AND w.status = 'PLANNED'
                  AND w.active_setup_session_id = s.setup_session_id
                  AND s.status IN ('READY_FOR_ZONES', 'BAKING_TEXTURE', 'COMPLETED')
                FOR UPDATE OF s, w
                """, new MapSqlParameterSource("session", setupSessionId).addValue("site", sites.siteId()), Long.class);
        if (found.isEmpty()) {
            throw new BusinessException(ErrorCode.Config.SETUP_ZONES_CLOSED);
        }
    }
}
