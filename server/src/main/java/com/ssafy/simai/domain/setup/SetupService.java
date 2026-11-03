package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobProgress;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 현장 세팅 세션 REST 및 I5 {@code site_setup} 진행·완료 처리(S15P31A101-452, docs/design/site-setup-pipeline.md).
 * <ul>
 *   <li>줄자 카메라 높이 입력을 현장 기본값({@code site.default_camera_heights_m})으로 저장한다. 세팅이나 시작 조건 검사가 실패해도
 *       마지막으로 제출한 유효한 높이는 다음 기본값으로 유지되고, 한 번도 제출된 적 없는 카메라는 기본값을 꾸며내지 않고 {@code null} 로 둔다.
 *   <li>I5 {@code site_setup} 작업을 대기열에 넣고, 유효 대여 하트비트({@link JobProgress})와 완료 콜백({@link JobFinished})으로
 *       단계·진행률·카메라별 배경 수집률·높이 검사(±5 cm)·교차 확인·등록된 버전 참조를 갱신한다.
 *   <li>{@code READY_FOR_ZONES} 단계부터 {@code work_unit.active_setup_session_id} 와 {@code ready_at} 을 보장하며,
 *       보정·Scene·구성 묶음 참조는 먼저 등록된 버전의 현장·보정 계보를 검증하되 자동 활성화하지 않는다.
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupService {

    static final double DEFAULT_HEIGHT_TOLERANCE_CM = 5.0;
    private static final double MAX_CAMERA_HEIGHT_M = 20.0;
    private static final int SETUP_MAX_ATTEMPTS = 2;
    private static final int SETUP_TIMEOUT_MS = 900_000;

    private static final List<String> ORDERED_STAGES = List.of(
            "COLLECTING_BACKGROUND",
            "INITIAL_POSE_AND_WORLD",
            "JOINT_BA_AND_VERIFY",
            "PREVIEW_AND_VLM_DRAFT",
            "READY_FOR_ZONES",
            "BAKING_TEXTURE",
            "COMPLETED");

    private static final Set<String> READY_OR_LATER_STAGES = Set.of(
            "READY_FOR_ZONES",
            "BAKING_TEXTURE",
            "COMPLETED");

    private static final Set<String> CROSS_CHECK_KINDS = Set.of(
            "TRAFFIC_CONE",
            "OPENING_SIGN");

    private final NamedParameterJdbcTemplate jdbc;
    private final TransactionTemplate transaction;
    private final TransactionTemplate requiresNew;
    private final ObjectMapper mapper;
    private final SiteLookup sites;
    private final AnalysisJobService jobs;
    private final Clock clock;

    public SetupService(NamedParameterJdbcTemplate jdbc, PlatformTransactionManager transactionManager,
            ObjectMapper mapper, SiteLookup sites, AnalysisJobService jobs, Clock clock) {
        this.jdbc = jdbc;
        this.transaction = new TransactionTemplate(transactionManager);
        this.requiresNew = new TransactionTemplate(transactionManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.mapper = mapper;
        this.sites = sites;
        this.jobs = jobs;
        this.clock = clock;
    }

    /** 현재 현장의 활성 카메라별 저장된 높이 기본값. 한 번도 제출된 적 없는 카메라는 null 로 내려준다. */
    public ObjectNode defaults() {
        String siteId = sites.siteId();
        List<String> cameras = activeCameras(siteId);
        Map<String, Double> stored = loadStoredDefaults(siteId);
        ObjectNode out = mapper.createObjectNode();
        out.put("site_id", siteId);
        out.put("height_tolerance_cm", DEFAULT_HEIGHT_TOLERANCE_CM);
        ObjectNode heights = out.putObject("camera_heights_m");
        boolean allPresent = !cameras.isEmpty();
        for (String cam : cameras) {
            Double value = stored.get(cam);
            if (value != null && isValidHeight(value)) {
                heights.put(cam, value);
            } else {
                heights.putNull(cam);
                allPresent = false;
            }
        }
        out.put("all_present", allPresent);
        return out;
    }

    /** 세팅 세션을 만들고 I5 {@code site_setup} 작업을 대기열에 넣는다. */
    public ObjectNode create(Long workUnitId, Map<String, Double> requestedHeights, long userId, UUID retryOf) {
        if (workUnitId == null || workUnitId < 1) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_REQUEST);
        }
        String siteId = sites.siteId();
        checkWorkUnitCanSetup(siteId, workUnitId);
        List<String> cameras = activeCameras(siteId);
        if (cameras.size() < 2) {
            throw new BusinessException(ErrorCode.Setup.SETUP_CAMERAS_INSUFFICIENT);
        }
        Map<String, Double> validatedSubmitted = validateSubmittedHeights(cameras, requestedHeights);
        if (!validatedSubmitted.isEmpty()) {
            // 세팅 세션 생성이나 이후 워커 작업이 실패해도 마지막 제출 높이는 다음 기본값으로 남긴다
            ObjectNode patch = mapper.createObjectNode();
            validatedSubmitted.forEach(patch::put);
            String patchJson = mapper.writeValueAsString(patch);
            requiresNew.executeWithoutResult(status -> jdbc.update("""
                    UPDATE site
                    SET default_camera_heights_m = COALESCE(default_camera_heights_m, '{}'::jsonb) || CAST(:patch AS jsonb)
                    WHERE site_id = :site
                    """, new MapSqlParameterSource().addValue("site", siteId).addValue("patch", patchJson)));
        }

        return transaction.execute(status -> {
            jdbc.queryForList("SELECT site_id FROM site WHERE site_id = :site FOR UPDATE",
                    new MapSqlParameterSource("site", siteId), String.class);
            checkWorkUnitCanSetupForUpdate(siteId, workUnitId);
            Map<String, Double> stored = loadStoredDefaults(siteId);
            Map<String, Double> effectiveHeights = new LinkedHashMap<>();
            for (String cam : cameras) {
                Double height = validatedSubmitted.containsKey(cam) ? validatedSubmitted.get(cam) : stored.get(cam);
                if (height == null || !isValidHeight(height)) {
                    throw new BusinessException(ErrorCode.Setup.CAMERA_HEIGHT_REQUIRED);
                }
                effectiveHeights.put(cam, height);
            }

            List<UUID> running = jdbc.queryForList("""
                    SELECT setup_session_id FROM setup_session
                    WHERE site_id = :site AND status NOT IN ('COMPLETED', 'FAILED')
                    LIMIT 1
                    """, new MapSqlParameterSource("site", siteId), UUID.class);
            if (!running.isEmpty()) {
                throw new BusinessException(ErrorCode.Setup.SETUP_ALREADY_IN_PROGRESS);
            }
            if (!hasActiveSetupWorker()) {
                throw new BusinessException(ErrorCode.Setup.SETUP_WORKER_UNAVAILABLE);
            }

            UUID sessionId = UUID.randomUUID();
            Instant now = clock.instant();
            ObjectNode heightsNode = mapper.createObjectNode();
            ObjectNode collectionNode = mapper.createObjectNode();
            for (Map.Entry<String, Double> e : effectiveHeights.entrySet()) {
                heightsNode.put(e.getKey(), e.getValue());
                collectionNode.put(e.getKey(), 0.0);
            }

            ObjectNode input = mapper.createObjectNode();
            input.put("setup_session_id", sessionId.toString());
            input.put("work_unit_id", workUnitId);
            input.put("site_id", siteId);
            input.set("camera_heights_m", heightsNode);
            ObjectNode target = input.putObject("calibration_target");
            ArrayNode camIds = target.putArray("camera_ids");
            cameras.forEach(camIds::add);
            target.put("method", "BACKGROUND_JOINT_BA_V2");
            target.put("world_frame", "AUTO_FLOOR_Z_UP_METERS");
            input.put("height_tolerance_cm", DEFAULT_HEIGHT_TOLERANCE_CM);

            String jobId = jobs.enqueue(new JobSpec("site_setup", input, SETUP_MAX_ATTEMPTS, SETUP_TIMEOUT_MS, null, null));

            OffsetDateTime utcNow = utc(now);
            jdbc.update("""
                    INSERT INTO setup_session (
                        setup_session_id, site_id, work_unit_id, retry_of, job_id, status, progress_pct,
                        camera_heights_m, height_tolerance_cm, background_collection_pct,
                        requested_by, started_at, updated_at
                    ) VALUES (
                        :id, :site, :workId, :retryOf, :jobId, 'COLLECTING_BACKGROUND', 0,
                        CAST(:heights AS jsonb), :tolerance, CAST(:collection AS jsonb),
                        :userId, :now, :now
                    )
                    """, new MapSqlParameterSource()
                    .addValue("id", sessionId)
                    .addValue("site", siteId)
                    .addValue("workId", workUnitId)
                    .addValue("retryOf", retryOf)
                    .addValue("jobId", jobId)
                    .addValue("heights", mapper.writeValueAsString(heightsNode))
                    .addValue("tolerance", DEFAULT_HEIGHT_TOLERANCE_CM)
                    .addValue("collection", mapper.writeValueAsString(collectionNode))
                    .addValue("userId", userId)
                    .addValue("now", utcNow));

            jdbc.update("""
                    UPDATE work_unit SET active_setup_session_id = :id, updated_at = :now
                    WHERE site_id = :site AND work_id = :workId
                    """, new MapSqlParameterSource()
                    .addValue("id", sessionId)
                    .addValue("site", siteId)
                    .addValue("workId", workUnitId)
                    .addValue("now", utcNow));

            return getInSite(siteId, sessionId);
        });
    }

    /** 세팅 세션 단건 조회 */
    public ObjectNode get(UUID setupSessionId) {
        return getInSite(sites.siteId(), setupSessionId);
    }

    /** 실패한 세팅 세션을 재시도해 새 세션을 만든다(새 카메라 높이가 있으면 기본값과 함께 갱신). */
    public ObjectNode retry(UUID setupSessionId, Map<String, Double> cameraHeightsM, long userId) {
        String siteId = sites.siteId();
        SessionRow existing = findForUpdateOrView(siteId, setupSessionId, false);
        if (!"FAILED".equals(existing.status())) {
            throw new BusinessException(ErrorCode.Setup.SETUP_RETRY_NOT_ALLOWED);
        }
        return create(existing.workUnitId(), cameraHeightsM, userId, setupSessionId);
    }

    /** 유효 대여의 하트비트에 실린 {@code site_setup} 진행 스냅샷을 검증·반영한다. */
    @EventListener
    public void onProgress(JobProgress event) {
        SessionRow session = findByJobForUpdate(event.jobId());
        if (session == null || "COMPLETED".equals(session.status()) || "FAILED".equals(session.status())) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        applySnapshot(session, event.leaseId(), event.setup(), false);
    }

    /** {@code site_setup} 작업 종료 콜백을 반영한다. */
    @EventListener
    public void onFinished(JobFinished event) {
        if (!"site_setup".equals(event.jobType())) {
            return;
        }
        SessionRow session = findByJobForUpdate(event.jobId());
        if (session == null || "COMPLETED".equals(session.status()) || "FAILED".equals(session.status())) {
            return;
        }
        if ("SUCCEEDED".equals(event.status())) {
            applySnapshot(session, session.progressLeaseId(), event.result(), true);
            return;
        }
        String code = event.status();
        String message = "현장 세팅 작업이 중단되었습니다";
        JsonNode err = event.error();
        if (err != null && err.isObject()) {
            String errCode = err.path("code").asString(null);
            String errMsg = err.path("message").asString(null);
            if (errCode != null && !errCode.isBlank()) {
                code = errCode;
            }
            if (errMsg != null && !errMsg.isBlank()) {
                message = errMsg;
            }
        }
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                UPDATE setup_session
                SET status = 'FAILED', failure_code = :code, failure_message = :msg,
                    completed_at = :now, updated_at = :now
                WHERE setup_session_id = :id
                """, new MapSqlParameterSource()
                .addValue("id", session.setupSessionId())
                .addValue("code", code)
                .addValue("msg", message)
                .addValue("now", now));
    }

    private void applySnapshot(SessionRow session, UUID leaseId, JsonNode snapshot, boolean terminalResult) {
        if (snapshot == null || !snapshot.isObject()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        String reportedSessionId = snapshot.path("setup_session_id").asString(null);
        if (!session.setupSessionId().toString().equals(reportedSessionId)) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        String stage = snapshot.path("status").asString(null);
        int nextStageIdx = stageIndex(stage);
        if (nextStageIdx < 0) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        if (terminalResult && !"COMPLETED".equals(stage)) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        if (!terminalResult && "COMPLETED".equals(stage)) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }

        JsonNode pctNode = snapshot.get("progress_pct");
        if (pctNode == null || !pctNode.isNumber()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        double progressPct = pctNode.doubleValue();
        if (!Double.isFinite(progressPct) || progressPct < 0.0 || progressPct > 100.0) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        if (terminalResult && Math.abs(progressPct - 100.0) > 1e-6) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }

        // 같은 대여 안에서는 단계와 진행률이 감소할 수 없다. 재할당된 새 대여는 첫 스냅샷부터 새 기준선을 시작한다
        boolean sameLease = session.progressLeaseId() != null && leaseId != null && session.progressLeaseId().equals(leaseId);
        if (sameLease) {
            int currentStageIdx = stageIndex(session.status());
            if (nextStageIdx < currentStageIdx || progressPct < session.progressPct() - 1e-6) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }

        Map<String, Double> expectedHeights = parseHeightsMap(session.cameraHeightsJson());
        String backgroundCollectionJson = session.backgroundCollectionJson();
        JsonNode bgNode = snapshot.get("background_collection_pct");
        if (bgNode != null && !bgNode.isNull()) {
            if (!bgNode.isObject() || bgNode.properties().isEmpty()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            ObjectNode mergedBg = mapper.createObjectNode();
            for (String cam : expectedHeights.keySet()) {
                JsonNode v = bgNode.get(cam);
                if (v == null || !v.isNumber()) {
                    throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
                }
                double pct = v.doubleValue();
                if (!Double.isFinite(pct) || pct < 0.0 || pct > 100.0) {
                    throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
                }
                mergedBg.put(cam, pct);
            }
            if (bgNode.properties().size() != expectedHeights.size()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            backgroundCollectionJson = mapper.writeValueAsString(mergedBg);
        }

        boolean readyOrLater = READY_OR_LATER_STAGES.contains(stage);
        String calibrationVersion = optionalText(snapshot, "calibration_version", session.calibrationVersion());
        String previewSceneVersion = optionalText(snapshot, "preview_scene_version", session.previewSceneVersion());
        String sceneVersion = optionalText(snapshot, "scene_version", session.sceneVersion());
        String configVersion = optionalText(snapshot, "config_version", session.configVersion());
        String zoneDraftsRef = optionalText(snapshot, "zone_drafts_ref", session.zoneDraftsRef());

        JsonNode heightChecksNode = snapshot.get("height_checks");
        String heightChecksJson = session.heightChecksJson();
        if (heightChecksNode != null && !heightChecksNode.isNull()) {
            heightChecksJson = validateAndEnrichHeightChecks(heightChecksNode, expectedHeights, session.heightToleranceCm());
        }

        if (readyOrLater) {
            if (calibrationVersion == null || previewSceneVersion == null || heightChecksJson == null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        if (terminalResult && sceneVersion == null) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }

        validateVersionLineage(session.siteId(), calibrationVersion, previewSceneVersion, sceneVersion, configVersion);

        String objectCrossChecksJson = session.objectCrossChecksJson();
        JsonNode crossNode = snapshot.get("object_cross_checks");
        if (crossNode != null && !crossNode.isNull()) {
            objectCrossChecksJson = validateObjectCrossChecks(crossNode);
        }

        OffsetDateTime now = utc(clock.instant());
        OffsetDateTime readyAt = session.readyAt() != null ? session.readyAt() : (readyOrLater ? now : null);
        OffsetDateTime completedAt = terminalResult ? now : null;
        UUID nextLeaseId = leaseId != null ? leaseId : session.progressLeaseId();

        jdbc.update("""
                UPDATE setup_session
                SET progress_lease_id = :leaseId,
                    status = :status,
                    progress_pct = :pct,
                    background_collection_pct = CAST(:bg AS jsonb),
                    height_checks = CAST(:heightChecks AS jsonb),
                    object_cross_checks = CAST(:crossChecks AS jsonb),
                    calibration_version = :cal,
                    preview_scene_version = :previewScene,
                    scene_version = :scene,
                    config_version = :config,
                    zone_drafts_ref = :zoneDrafts,
                    ready_at = COALESCE(ready_at, :readyAt),
                    completed_at = COALESCE(completed_at, :completedAt),
                    updated_at = :now
                WHERE setup_session_id = :id
                """, new MapSqlParameterSource()
                .addValue("id", session.setupSessionId())
                .addValue("leaseId", nextLeaseId)
                .addValue("status", stage)
                .addValue("pct", progressPct)
                .addValue("bg", backgroundCollectionJson)
                .addValue("heightChecks", heightChecksJson)
                .addValue("crossChecks", objectCrossChecksJson)
                .addValue("cal", calibrationVersion)
                .addValue("previewScene", previewSceneVersion)
                .addValue("scene", sceneVersion)
                .addValue("config", configVersion)
                .addValue("zoneDrafts", zoneDraftsRef)
                .addValue("readyAt", readyAt)
                .addValue("completedAt", completedAt)
                .addValue("now", now));

        if (readyOrLater) {
            jdbc.update("""
                    UPDATE work_unit SET active_setup_session_id = :id, updated_at = :now
                    WHERE site_id = :site AND work_id = :workId
                    """, new MapSqlParameterSource()
                    .addValue("id", session.setupSessionId())
                    .addValue("site", session.siteId())
                    .addValue("workId", session.workUnitId())
                    .addValue("now", now));
        }
    }

    private String validateAndEnrichHeightChecks(JsonNode node, Map<String, Double> expectedHeights, double toleranceCm) {
        if (!node.isObject() || node.properties().size() != expectedHeights.size()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        ObjectNode out = mapper.createObjectNode();
        for (Map.Entry<String, Double> entry : expectedHeights.entrySet()) {
            String camId = entry.getKey();
            double expectedInputM = entry.getValue();
            JsonNode check = node.get(camId);
            if (check == null || !check.isObject()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            JsonNode inputNode = check.get("input_height_m");
            JsonNode restoredNode = check.get("restored_height_m");
            JsonNode diffNode = check.get("difference_cm");
            JsonNode withinNode = check.get("within_tolerance");
            if (inputNode == null || !inputNode.isNumber()
                    || restoredNode == null || !restoredNode.isNumber()
                    || diffNode == null || !diffNode.isNumber()
                    || withinNode == null || !withinNode.isBoolean()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            double inputM = inputNode.doubleValue();
            double restoredM = restoredNode.doubleValue();
            double diffCm = diffNode.doubleValue();
            boolean within = withinNode.booleanValue();
            if (!Double.isFinite(inputM) || !Double.isFinite(restoredM) || !Double.isFinite(diffCm) || restoredM <= 0.0) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            if (Math.abs(inputM - expectedInputM) > 1e-4) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            double expectedDiffCm = (restoredM - inputM) * 100.0;
            if (Math.abs(diffCm - expectedDiffCm) > 0.15) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            boolean expectedWithin = Math.abs(diffCm) <= toleranceCm + 1e-6;
            if (within != expectedWithin) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            ObjectNode enriched = out.putObject(camId);
            enriched.put("input_height_m", inputM);
            enriched.put("restored_height_m", restoredM);
            enriched.put("difference_cm", diffCm);
            enriched.put("within_tolerance", within);
            if (within) {
                enriched.putNull("guidance");
            } else {
                enriched.put("guidance", String.format(Locale.ROOT,
                        "카메라 %s 높이가 %.1f cm 안 맞음 — 거치대 확인", camId, Math.abs(diffCm)));
            }
        }
        return mapper.writeValueAsString(out);
    }

    private String validateObjectCrossChecks(JsonNode crossNode) {
        if (!crossNode.isArray()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        for (JsonNode item : crossNode) {
            if (item == null || !item.isObject()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            String kind = item.path("kind").asString(null);
            JsonNode passed = item.get("passed");
            if (kind == null || !CROSS_CHECK_KINDS.contains(kind) || passed == null || !passed.isBoolean()) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        return mapper.writeValueAsString(crossNode);
    }

    private void validateVersionLineage(String siteId, String calibrationVersion, String previewSceneVersion,
            String sceneVersion, String configVersion) {
        if (calibrationVersion != null) {
            Boolean validCal = jdbc.queryForObject("""
                    SELECT EXISTS (
                        SELECT 1 FROM calibration_version
                        WHERE calibration_version = :cal AND site_id = :site
                          AND state IN ('PROVISIONAL', 'CALIBRATED')
                    )
                    """, new MapSqlParameterSource().addValue("cal", calibrationVersion).addValue("site", siteId),
                    Boolean.class);
            if (!Boolean.TRUE.equals(validCal)) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        for (String sv : List.of(previewSceneVersion != null ? previewSceneVersion : "",
                sceneVersion != null ? sceneVersion : "")) {
            if (sv.isEmpty()) {
                continue;
            }
            if (calibrationVersion == null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            Boolean validScene = jdbc.queryForObject("""
                    SELECT EXISTS (
                        SELECT 1 FROM scene_version
                        WHERE scene_version = :scene AND site_id = :site AND calibration_version = :cal
                    )
                    """, new MapSqlParameterSource().addValue("scene", sv).addValue("site", siteId)
                    .addValue("cal", calibrationVersion), Boolean.class);
            if (!Boolean.TRUE.equals(validScene)) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        if (configVersion != null) {
            if (calibrationVersion == null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            Boolean validConfig = jdbc.queryForObject("""
                    SELECT EXISTS (
                        SELECT 1 FROM config_bundle
                        WHERE config_version = :cfg AND site_id = :site AND calibration_version = :cal
                    )
                    """, new MapSqlParameterSource().addValue("cfg", configVersion).addValue("site", siteId)
                    .addValue("cal", calibrationVersion), Boolean.class);
            if (!Boolean.TRUE.equals(validConfig)) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
    }

    private void checkWorkUnitCanSetup(String siteId, long workUnitId) {
        List<String> rows = jdbc.queryForList(
                "SELECT status FROM work_unit WHERE site_id = :site AND work_id = :id",
                new MapSqlParameterSource().addValue("site", siteId).addValue("id", workUnitId), String.class);
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        if ("DONE".equals(rows.getFirst())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
    }

    private void checkWorkUnitCanSetupForUpdate(String siteId, long workUnitId) {
        List<String> rows = jdbc.queryForList(
                "SELECT status FROM work_unit WHERE site_id = :site AND work_id = :id FOR UPDATE",
                new MapSqlParameterSource().addValue("site", siteId).addValue("id", workUnitId), String.class);
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        if ("DONE".equals(rows.getFirst())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
    }

    private List<String> activeCameras(String siteId) {
        return jdbc.queryForList("""
                SELECT camera_id FROM camera
                WHERE site_id = :site AND removed_at IS NULL
                ORDER BY camera_id
                """, new MapSqlParameterSource("site", siteId), String.class);
    }

    private Map<String, Double> loadStoredDefaults(String siteId) {
        String raw = jdbc.queryForObject(
                "SELECT default_camera_heights_m::text FROM site WHERE site_id = :site",
                new MapSqlParameterSource("site", siteId), String.class);
        return parseHeightsMap(raw);
    }

    private Map<String, Double> parseHeightsMap(String rawJson) {
        Map<String, Double> out = new LinkedHashMap<>();
        if (rawJson == null || rawJson.isBlank()) {
            return out;
        }
        JsonNode node = mapper.readTree(rawJson);
        if (!node.isObject()) {
            return out;
        }
        for (Map.Entry<String, JsonNode> e : node.properties()) {
            if (e.getValue() != null && e.getValue().isNumber()) {
                double val = e.getValue().doubleValue();
                if (isValidHeight(val)) {
                    out.put(e.getKey(), val);
                }
            }
        }
        return out;
    }

    private Map<String, Double> validateSubmittedHeights(List<String> cameras, Map<String, Double> requestedHeights) {
        Map<String, Double> out = new LinkedHashMap<>();
        if (requestedHeights == null || requestedHeights.isEmpty()) {
            return out;
        }
        for (Map.Entry<String, Double> e : requestedHeights.entrySet()) {
            String cam = e.getKey();
            Double value = e.getValue();
            if (cam == null || !cameras.contains(cam) || value == null || !isValidHeight(value)) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_REQUEST);
            }
            out.put(cam, value);
        }
        return out;
    }

    private static boolean isValidHeight(double value) {
        return Double.isFinite(value) && value > 0.0 && value <= MAX_CAMERA_HEIGHT_M;
    }

    private boolean hasActiveSetupWorker() {
        Boolean exists = jdbc.queryForObject("""
                SELECT EXISTS (
                    SELECT 1 FROM worker
                    WHERE revoked_at IS NULL AND 'site_setup' = ANY(job_types)
                )
                """, Map.of(), Boolean.class);
        return Boolean.TRUE.equals(exists);
    }

    private static int stageIndex(String stage) {
        return stage == null ? -1 : ORDERED_STAGES.indexOf(stage);
    }

    private static String optionalText(JsonNode node, String field, String current) {
        if (!node.has(field)) {
            return current;
        }
        JsonNode child = node.get(field);
        if (child == null || child.isNull()) {
            return current;
        }
        if (!child.isString() || child.asString().isBlank()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        return child.asString();
    }

    private ObjectNode getInSite(String siteId, UUID setupSessionId) {
        List<ObjectNode> list = jdbc.query("""
                SELECT setup_session_id, site_id, work_unit_id, retry_of, job_id, status, progress_pct,
                       camera_heights_m::text AS camera_heights_m, height_tolerance_cm,
                       background_collection_pct::text AS background_collection_pct,
                       height_checks::text AS height_checks,
                       object_cross_checks::text AS object_cross_checks,
                       calibration_version, preview_scene_version, scene_version, config_version, zone_drafts_ref,
                       failure_code, failure_message, requested_by, started_at, ready_at, completed_at, updated_at
                FROM setup_session
                WHERE site_id = :site AND setup_session_id = :id
                """, new MapSqlParameterSource().addValue("site", siteId).addValue("id", setupSessionId),
                (rs, i) -> view(rs));
        if (list.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        return list.getFirst();
    }

    private SessionRow findForUpdateOrView(String siteId, UUID setupSessionId, boolean forUpdate) {
        String sql = """
                SELECT setup_session_id, site_id, work_unit_id, progress_lease_id, status, progress_pct,
                       camera_heights_m::text AS camera_heights_m, height_tolerance_cm,
                       background_collection_pct::text AS background_collection_pct,
                       height_checks::text AS height_checks,
                       object_cross_checks::text AS object_cross_checks,
                       calibration_version, preview_scene_version, scene_version, config_version, zone_drafts_ref,
                       ready_at
                FROM setup_session
                WHERE site_id = :site AND setup_session_id = :id
                """ + (forUpdate ? " FOR UPDATE" : "");
        List<SessionRow> list = jdbc.query(sql,
                new MapSqlParameterSource().addValue("site", siteId).addValue("id", setupSessionId),
                (rs, i) -> mapSessionRow(rs));
        if (list.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        return list.getFirst();
    }

    private SessionRow findByJobForUpdate(String jobId) {
        List<SessionRow> list = jdbc.query("""
                SELECT setup_session_id, site_id, work_unit_id, progress_lease_id, status, progress_pct,
                       camera_heights_m::text AS camera_heights_m, height_tolerance_cm,
                       background_collection_pct::text AS background_collection_pct,
                       height_checks::text AS height_checks,
                       object_cross_checks::text AS object_cross_checks,
                       calibration_version, preview_scene_version, scene_version, config_version, zone_drafts_ref,
                       ready_at
                FROM setup_session
                WHERE job_id = :jobId
                FOR UPDATE
                """, new MapSqlParameterSource("jobId", jobId), (rs, i) -> mapSessionRow(rs));
        return list.isEmpty() ? null : list.getFirst();
    }

    private static SessionRow mapSessionRow(ResultSet rs) throws SQLException {
        return new SessionRow(
                rs.getObject("setup_session_id", UUID.class),
                rs.getString("site_id"),
                rs.getLong("work_unit_id"),
                rs.getObject("progress_lease_id", UUID.class),
                rs.getString("status"),
                rs.getDouble("progress_pct"),
                rs.getString("camera_heights_m"),
                rs.getDouble("height_tolerance_cm"),
                rs.getString("background_collection_pct"),
                rs.getString("height_checks"),
                rs.getString("object_cross_checks"),
                rs.getString("calibration_version"),
                rs.getString("preview_scene_version"),
                rs.getString("scene_version"),
                rs.getString("config_version"),
                rs.getString("zone_drafts_ref"),
                rs.getObject("ready_at", OffsetDateTime.class));
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode out = mapper.createObjectNode();
        out.put("setup_session_id", rs.getObject("setup_session_id", UUID.class).toString());
        out.put("site_id", rs.getString("site_id"));
        out.put("work_unit_id", rs.getLong("work_unit_id"));
        UUID retryOf = rs.getObject("retry_of", UUID.class);
        if (retryOf == null) {
            out.putNull("retry_of");
        } else {
            out.put("retry_of", retryOf.toString());
        }
        out.put("job_id", rs.getString("job_id"));
        String status = rs.getString("status");
        out.put("status", status);
        out.put("stage", status);
        out.put("progress_pct", rs.getDouble("progress_pct"));
        out.set("camera_heights_m", mapper.readTree(rs.getString("camera_heights_m")));
        out.put("height_tolerance_cm", rs.getDouble("height_tolerance_cm"));
        out.set("background_collection_pct", mapper.readTree(rs.getString("background_collection_pct")));

        String heightChecksRaw = rs.getString("height_checks");
        ArrayNode warnings = mapper.createArrayNode();
        if (heightChecksRaw == null) {
            out.putNull("height_checks");
        } else {
            JsonNode checksNode = mapper.readTree(heightChecksRaw);
            out.set("height_checks", checksNode);
            if (checksNode.isObject()) {
                for (Map.Entry<String, JsonNode> e : checksNode.properties()) {
                    String guidance = e.getValue().path("guidance").asString(null);
                    if (guidance != null) {
                        warnings.add(guidance);
                    }
                }
            }
        }
        out.set("height_warnings", warnings);

        String crossRaw = rs.getString("object_cross_checks");
        if (crossRaw == null) {
            out.putNull("object_cross_checks");
        } else {
            out.set("object_cross_checks", mapper.readTree(crossRaw));
        }

        putText(out, "calibration_version", rs.getString("calibration_version"));
        putText(out, "preview_scene_version", rs.getString("preview_scene_version"));
        putText(out, "scene_version", rs.getString("scene_version"));
        putText(out, "config_version", rs.getString("config_version"));
        putText(out, "zone_drafts_ref", rs.getString("zone_drafts_ref"));
        putText(out, "failure_code", rs.getString("failure_code"));
        putText(out, "failure_message", rs.getString("failure_message"));

        long requestedBy = rs.getLong("requested_by");
        if (rs.wasNull()) {
            out.putNull("requested_by");
        } else {
            out.put("requested_by", requestedBy);
        }
        out.put("started_at_ms", rs.getTimestamp("started_at").getTime());
        var readyAt = rs.getTimestamp("ready_at");
        if (readyAt == null) {
            out.putNull("ready_at_ms");
        } else {
            out.put("ready_at_ms", readyAt.getTime());
        }
        var completedAt = rs.getTimestamp("completed_at");
        if (completedAt == null) {
            out.putNull("completed_at_ms");
        } else {
            out.put("completed_at_ms", completedAt.getTime());
        }
        out.put("updated_at_ms", rs.getTimestamp("updated_at").getTime());
        return out;
    }

    private static void putText(ObjectNode node, String field, String value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private record SessionRow(
            UUID setupSessionId,
            String siteId,
            long workUnitId,
            UUID progressLeaseId,
            String status,
            double progressPct,
            String cameraHeightsJson,
            double heightToleranceCm,
            String backgroundCollectionJson,
            String heightChecksJson,
            String objectCrossChecksJson,
            String calibrationVersion,
            String previewSceneVersion,
            String sceneVersion,
            String configVersion,
            String zoneDraftsRef,
            OffsetDateTime readyAt) {
    }
}
