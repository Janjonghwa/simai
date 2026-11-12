package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.layout.LayoutZoneService;
import com.ssafy.simai.domain.report.TbmFormAiService;
import com.ssafy.simai.domain.scene.SceneActivationService;
import com.ssafy.simai.domain.work.WorkUnitRepository;
import com.ssafy.simai.domain.zone.SetupZoneApplier;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.Set;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.support.TransactionSynchronization;
import org.springframework.transaction.support.TransactionSynchronizationManager;
import org.springframework.transaction.support.TransactionTemplate;

/**
 * 세팅 완료 POST /api/setup-sessions/{id}/complete (C17, S15P31A101-615, docs/design/S15P31A101-615-setup-complete.md).
 * <p>한 트랜잭션에서 이 순서로 한다(-612 약속):
 * <ol>
 *   <li>세션 구역 목록을 관제 구역으로 적용한다({@link SetupZoneApplier#apply}, 묶음 발행 없음).
 *   <li>{@code work_unit.setup_completed_at} 을 기록한다(달력 SETUP_DONE, 하루 1회).
 *   <li>새 모델(NEW)이면 이 세션의 최종 맵(다시 굽기 반영분 &gt; 정식 맵 &gt; 미리보기)을 현재 Scene 으로 켠다. 이전 모델 재사용(REUSE)은 지금 맵을 둔다.
 *   <li>구성 묶음을 이 세션 보정과 함께 한 번 발행한다(Tier 0 전송은 커밋 뒤).
 * </ol>
 * 커밋 뒤에는 그 작업의 TBM AI 양식 초안을 시작한다(결과를 기다리지 않고, 실패해도 완료는 성공).
 * 잠금 순서는 작업 시작과 같다: 레이아웃 구역 잠금 → 현장 행 → 세션 행 → 작업 행.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupCompletionService {

    private static final Logger log = LoggerFactory.getLogger(SetupCompletionService.class);
    private static final Set<String> READY_OR_LATER = Set.of("READY_FOR_ZONES", "BAKING_TEXTURE", "COMPLETED");

    /** 완료 결과: 적용한 구역과 만든 구성 묶음 */
    public record Completed(long workUnitId, SetupZoneApplier.ApplyResult zones, String configVersion, String sceneVersion) {
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final SiteLookup sites;
    private final Clock clock;
    private final WorkUnitRepository workUnits;
    private final LayoutZoneService layoutZones;
    private final SetupZoneApplier zoneApplier;
    private final SceneActivationService scenes;
    private final ConfigBundleService configs;
    private final SetupArtifactRegistrar artifacts;
    private final TbmFormAiService tbmFormAi;

    public SetupCompletionService(NamedParameterJdbcTemplate jdbc, PlatformTransactionManager transactionManager, SiteLookup sites,
            Clock clock, WorkUnitRepository workUnits, LayoutZoneService layoutZones, SetupZoneApplier zoneApplier,
            SceneActivationService scenes, ConfigBundleService configs, SetupArtifactRegistrar artifacts,
            TbmFormAiService tbmFormAi) {
        this.jdbc = jdbc;
        this.transaction = new TransactionTemplate(transactionManager);
        this.sites = sites;
        this.clock = clock;
        this.workUnits = workUnits;
        this.layoutZones = layoutZones;
        this.zoneApplier = zoneApplier;
        this.scenes = scenes;
        this.configs = configs;
        this.artifacts = artifacts;
        this.tbmFormAi = tbmFormAi;
    }

    private record SessionRow(long workUnitId, String status, String mode, boolean anchorAccepted, String calibrationVersion,
            String previewSceneVersion, String sceneVersion) {
    }

    private record WorkRow(String status, UUID activeSetupSessionId, boolean completed) {
    }

    /**
     * 세팅을 완료한다. 거부(모두 409, 없으면 404): 세션 없음 SETUP_SESSION_NOT_FOUND, 이미 완료 SETUP_ALREADY_COMPLETED,
     * 미리보기 전·실패 SETUP_NOT_READY_TO_COMPLETE, 작업에 걸린 세션 아님 SETUP_SESSION_NOT_ACTIVE, 오늘 아님 WORK_NOT_TODAY,
     * 작업 PLANNED 아님 WORK_INVALID_STATE, 같은 현장 관제 중 SETUP_COMPLETE_BLOCKED_BY_LIVE_WORK, 구역 적용 오류(SETUP_ZONE_*)는 그대로.
     */
    public Completed complete(UUID setupSessionId, long actorUserId) {
        String siteId = sites.siteId();
        Completed done = transaction.execute(status -> {
            // 작업 시작(WorkSessionService.start)과 같은 잠금 순서. 적용 서비스도 이 잠금을 다시 잡는다(같은 트랜잭션이라 바로 통과)
            layoutZones.lock();
            jdbc.queryForList("SELECT site_id FROM site WHERE site_id = :site FOR UPDATE",
                    new MapSqlParameterSource("site", siteId), String.class);
            SessionRow session = lockSession(siteId, setupSessionId);
            WorkRow work = lockWork(siteId, session.workUnitId());
            if (work.completed()) {
                throw new BusinessException(ErrorCode.Setup.SETUP_ALREADY_COMPLETED);
            }
            if (!READY_OR_LATER.contains(session.status())) {
                throw new BusinessException(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
            }
            if (!setupSessionId.equals(work.activeSetupSessionId())) {
                throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_ACTIVE);
            }
            Instant now = clock.instant();
            workUnits.requireToday(siteId, session.workUnitId(), now);
            if (!"PLANNED".equals(work.status())) {
                throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
            }
            // 관제 중에는 맵·보정을 바꾸지 않는다(세팅 시작·Scene 자동 현재화와 같은 가드)
            if (Boolean.TRUE.equals(jdbc.queryForObject(
                    "SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS')",
                    new MapSqlParameterSource("site", siteId), Boolean.class))) {
                throw new BusinessException(ErrorCode.Setup.SETUP_COMPLETE_BLOCKED_BY_LIVE_WORK);
            }
            boolean reuse = SetupReuseMode.REUSE.equals(session.mode());

            // 1) 구역 적용. 같은 World(재사용 또는 World 다시 맞추기 채택)가 아닌데 기존 zone_id 가 있으면 409 SETUP_ZONE_WORLD_MISMATCH
            SetupZoneApplier.ApplyResult zones = zoneApplier.apply(setupSessionId, reuse || session.anchorAccepted(), actorUserId);

            // 2) 완료 기록
            OffsetDateTime utcNow = OffsetDateTime.ofInstant(now, ZoneOffset.UTC);
            jdbc.update("""
                    UPDATE work_unit SET setup_completed_at = :now, updated_at = :now
                    WHERE site_id = :site AND work_id = :work
                    """, new MapSqlParameterSource("now", utcNow).addValue("site", siteId).addValue("work", session.workUnitId()));

            // 3) Scene: 새 모델은 이 세션의 최종 맵을 켠다. 재사용은 지금 관제 맵을 그대로 둔다
            String sceneVersion = reuse ? null : finalScene(setupSessionId, session);
            if (sceneVersion != null) {
                scenes.activate(siteId, sceneVersion, "setup:" + setupSessionId,
                        "현장 세팅 완료(C17): 이 세션의 최종 맵 적용(위험 판정 미사용)");
            }

            // 4) 구성 묶음 한 번: 이 세션이 등록한 새 보정을 싣는다(등록한 보정이 없으면 지금 보정)
            String calibration = session.calibrationVersion() != null
                    && artifacts.registeredBySession(setupSessionId, SetupArtifactRegistrar.CALIBRATION, session.calibrationVersion())
                    ? session.calibrationVersion() : null;
            String configVersion = configs.publishInTransaction(calibration, actorUserId);

            long workId = session.workUnitId();
            TransactionSynchronizationManager.registerSynchronization(new TransactionSynchronization() {
                @Override
                public void afterCommit() {
                    startTbmDraft(workId);
                }
            });
            return new Completed(workId, zones, configVersion, sceneVersion);
        });
        log.info("세팅 세션 {} 완료: 작업 {}, 구성 묶음 {}, Scene {}", setupSessionId, done.workUnitId(), done.configVersion(),
                done.sceneVersion());
        return done;
    }

    /**
     * 이 세션의 최종 맵: 다시 굽기 APPLIED 가 옮긴 맵 또는 정식 맵(setup_session.scene_version), 없으면(굽기 중·회수) 미리보기.
     * 이 세션이 등록한 Scene 만 켠다. 없으면 완료할 맵이 없다(409)
     */
    private String finalScene(UUID setupSessionId, SessionRow session) {
        if (session.sceneVersion() != null
                && artifacts.registeredBySession(setupSessionId, SetupArtifactRegistrar.SCENE, session.sceneVersion())) {
            return session.sceneVersion();
        }
        if (session.previewSceneVersion() != null
                && artifacts.registeredBySession(setupSessionId, SetupArtifactRegistrar.PREVIEW_SCENE, session.previewSceneVersion())) {
            return session.previewSceneVersion();
        }
        throw new BusinessException(ErrorCode.Setup.SETUP_NOT_READY_TO_COMPLETE);
    }

    /** TBM 양식 초안은 부가 작업이다. 커밋된 구역을 읽도록 다른 스레드에서 새 트랜잭션으로 시작하고 기다리지 않는다 */
    private void startTbmDraft(long workId) {
        try {
            Thread.ofVirtual().name("setup-complete-tbm-" + workId).start(() -> {
                try {
                    tbmFormAi.tryStartDraftForWork(workId);
                } catch (RuntimeException e) {
                    log.warn("세팅 완료 뒤 작업 {} 의 TBM 초안 시작 실패(완료는 유지): {}", workId, e.getMessage());
                }
            });
        } catch (RuntimeException e) {
            log.warn("세팅 완료 뒤 작업 {} 의 TBM 초안 시작 실패(완료는 유지): {}", workId, e.getMessage());
        }
    }

    private SessionRow lockSession(String siteId, UUID setupSessionId) {
        List<SessionRow> rows = jdbc.query("""
                SELECT work_unit_id, status, mode, COALESCE((world_anchor ->> 'accepted')::boolean, false) AS anchor_accepted,
                       calibration_version, preview_scene_version, scene_version
                FROM setup_session
                WHERE site_id = :site AND setup_session_id = :id
                FOR UPDATE
                """, new MapSqlParameterSource("site", siteId).addValue("id", setupSessionId),
                (rs, i) -> new SessionRow(rs.getLong("work_unit_id"), rs.getString("status"), rs.getString("mode"),
                        rs.getBoolean("anchor_accepted"), rs.getString("calibration_version"),
                        rs.getString("preview_scene_version"), rs.getString("scene_version")));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        return rows.getFirst();
    }

    private WorkRow lockWork(String siteId, long workId) {
        List<WorkRow> rows = jdbc.query("""
                SELECT status, active_setup_session_id, setup_completed_at IS NOT NULL AS completed
                FROM work_unit WHERE site_id = :site AND work_id = :id
                FOR UPDATE
                """, new MapSqlParameterSource("site", siteId).addValue("id", workId),
                (rs, i) -> new WorkRow(rs.getString("status"), rs.getObject("active_setup_session_id", UUID.class),
                        rs.getBoolean("completed")));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        return rows.getFirst();
    }
}
