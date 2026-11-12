package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.config.ConfigBundleService;
import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.config.Versioned;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobProgress;
import com.ssafy.simai.domain.job.JobStateChanged;
import com.ssafy.simai.domain.scene.SceneActivationService;
import com.ssafy.simai.domain.scene.SceneCompatibility;
import com.ssafy.simai.domain.scene.SceneService;
import com.ssafy.simai.domain.work.WorkStarted;
import com.ssafy.simai.domain.work.WorkUnitRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.PlatformTransactionManager;
import org.springframework.transaction.TransactionDefinition;
import org.springframework.transaction.support.TransactionSynchronizationManager;
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
 *       보정·Scene·구성 묶음 참조는 현장·보정 계보를 검증하되 자동 활성화하지 않는다.
 *   <li>스냅샷의 {@code artifacts}(site_setup 1.2.0)로 알린 새 보정 문서·미리보기/정식 GLB 는 {@link SetupArtifactRegistrar} 가
 *       새 calibration_version·scene_version(CANDIDATE)으로 등록해 이 세션에 귀속시킨다. 버전 참조는 이미 있던 버전이거나 이 세션이
 *       등록한 버전만 받고, 다른 세팅 세션에 귀속된 버전은 거부한다(S15P31A101-467).
 *   <li>같은 스냅샷의 {@code artifacts.camera_frames}(1.3.0)로 알린 카메라별 대표 프레임도 이 세션 귀속으로 보관하고(CAMERA_FRAME),
 *       AI 구역 초안({@code vlm_zone_draft}) 입력 {@code frames} 에 카메라·주소·얼굴 정책으로 싣는다(S15P31A101-479).
 *   <li>새 보정을 실은 구성 묶음 발행과 이 세션 맵의 현재 Scene 올리기는 세팅 완료(C17, {@link SetupCompletionService},
 *       S15P31A101-615)가 한다. READY_FOR_ZONES 하트비트는 더 이상 묶음을 만들거나 Scene 을 올리지 않는다. 완료 뒤에 도착한
 *       정식 맵·다시 굽기 맵은 지금 현재 Scene 이 이 세션의 이전 맵일 때만 교체한다(관제 중이면 하지 않음).
 *   <li>세팅을 완료한 작업(work_unit.setup_completed_at)에는 새 세팅 세션을 만들지 않는다(하루 1회, 409 SETUP_ALREADY_COMPLETED).
 * </ul>
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupService {

    private static final Logger log = LoggerFactory.getLogger(SetupService.class);
    static final double DEFAULT_HEIGHT_TOLERANCE_CM = 5.0;
    private static final int DRAFT_MAX_ATTEMPTS = 2;
    /** vlm_zone_draft 입력 frames maxItems(계약) */
    private static final int MAX_DRAFT_FRAMES = 8;
    private static final int DRAFT_TIMEOUT_MS = 60_000;
    /**
     * 변경 의심 구역 의견(S15P31A101-631)을 함께 낼 때의 상한. 초안 60 s + 구역 최대 8개 × 약 10 s(얼굴 처리 + VLM 한 번).
     * 워커는 남은 시간이 모자라면 그 구역을 NO_OPINION(TIME_BUDGET)으로 낸다.
     */
    private static final int DRAFT_WITH_OPINIONS_TIMEOUT_MS = 150_000;
    private static final java.util.regex.Pattern S3_URI_PATTERN =
            java.util.regex.Pattern.compile("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/\\S+$");
    private static final java.util.regex.Pattern SHA256_PATTERN =
            java.util.regex.Pattern.compile("^sha256:[0-9a-f]{64}$");
    private static final java.util.regex.Pattern SHA256_HEX_PATTERN =
            java.util.regex.Pattern.compile("^[0-9a-f]{64}$");
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

    /** site_setup 1.7.0 bake_skipped 의 유일한 값: 작업 시작(LIVE)으로 정식 굽기를 회수했다(S15P31A101-597) */
    static final String BAKE_SKIPPED_LIVE_WORK = "LIVE_WORK_STARTED";
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
    private final SetupArtifactRegistrar artifacts;
    private final ConfigBundleService configs;
    private final SceneActivationService scenes;
    private final SetupWaitReasonReader waitReasons;
    private final SetupSupplementService supplements;
    private final SceneCompatibility compatibility;
    private final WorkUnitRepository workUnits;
    private final SetupZoneOpinions opinions;

    public SetupService(NamedParameterJdbcTemplate jdbc, PlatformTransactionManager transactionManager,
            ObjectMapper mapper, SiteLookup sites, AnalysisJobService jobs, Clock clock, SetupArtifactRegistrar artifacts,
            ConfigBundleService configs, SceneActivationService scenes, SetupWaitReasonReader waitReasons,
            SetupSupplementService supplements, SceneCompatibility compatibility, WorkUnitRepository workUnits,
            SetupZoneOpinions opinions) {
        this.jdbc = jdbc;
        this.transaction = new TransactionTemplate(transactionManager);
        this.requiresNew = new TransactionTemplate(transactionManager);
        this.requiresNew.setPropagationBehavior(TransactionDefinition.PROPAGATION_REQUIRES_NEW);
        this.mapper = mapper;
        this.sites = sites;
        this.jobs = jobs;
        this.clock = clock;
        this.artifacts = artifacts;
        this.configs = configs;
        this.scenes = scenes;
        this.waitReasons = waitReasons;
        this.supplements = supplements;
        this.compatibility = compatibility;
        this.workUnits = workUnits;
        this.opinions = opinions;
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

    /** 세팅 세션을 만들고 I5 {@code site_setup} 작업을 대기열에 넣는다(새 모델). */
    public ObjectNode create(Long workUnitId, Map<String, Double> requestedHeights, long userId, UUID retryOf) {
        return create(workUnitId, requestedHeights, userId, retryOf, SetupReuseMode.NEW);
    }

    /**
     * 세팅 세션을 만들고 I5 {@code site_setup} 작업을 대기열에 넣는다.
     *
     * @param mode REST mode: NEW(기본, 새 모델) | REUSE(이전 모델 재사용, S15P31A101-614). REUSE 는 현재 관제 맵과 그 보정이 있어야 한다
     */
    public ObjectNode create(Long workUnitId, Map<String, Double> requestedHeights, long userId, UUID retryOf, String mode) {
        if (workUnitId == null || workUnitId < 1) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_REQUEST);
        }
        String requestedMode = SetupReuseMode.parseRequested(mode);
        String siteId = sites.siteId();
        checkWorkUnitCanSetup(siteId, workUnitId);
        // 날짜 작업은 작업 날짜가 오늘(Asia/Seoul)일 때만 세팅을 시작한다(-610 정본, 409 WORK_NOT_TODAY, -614 팀 작업 관리 흐름)
        workUnits.requireToday(siteId, workUnitId, clock.instant());
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

            // 세팅은 작업 시작 전 절차다. 관제 중(IN_PROGRESS) 작업이 있으면 보정·World 를 바꾸는 세팅을 시작하지 않는다(S15P31A101-467)
            Boolean live = jdbc.queryForObject("""
                    SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS')
                    """, new MapSqlParameterSource("site", siteId), Boolean.class);
            if (Boolean.TRUE.equals(live)) {
                throw new BusinessException(ErrorCode.Setup.SETUP_BLOCKED_BY_LIVE_WORK);
            }
            // 맵 보강 다시 굽기(S15P31A101-607)도 같은 GPU 의 site_setup 작업이다. 끝나기 전에는 새 세팅을 시작하지 않는다.
            // 막고 있는 세션(다시 굽기면 그 세션)과 작업을 응답에 싣는다(S15P31A101-641)
            List<Map<String, Object>> running = jdbc.queryForList("""
                    SELECT setup_session_id, work_unit_id FROM setup_session s
                    WHERE site_id = :site AND (status NOT IN ('COMPLETED', 'FAILED')
                        OR EXISTS (SELECT 1 FROM setup_supplement_rebake r
                                   WHERE r.setup_session_id = s.setup_session_id AND r.status IN ('QUEUED', 'RUNNING')))
                    ORDER BY (status NOT IN ('COMPLETED', 'FAILED')) DESC, started_at DESC LIMIT 1
                    """, new MapSqlParameterSource("site", siteId));
            if (!running.isEmpty()) {
                throw new SetupSessionBlockingException(ErrorCode.Setup.SETUP_ALREADY_IN_PROGRESS,
                        (UUID) running.getFirst().get("setup_session_id"),
                        ((Number) running.getFirst().get("work_unit_id")).longValue());
            }
            if (!hasActiveSetupWorker()) {
                throw new BusinessException(ErrorCode.Setup.SETUP_WORKER_UNAVAILABLE);
            }
            if (SetupReuseMode.REUSE.equals(requestedMode)) {
                requireReusableScene(siteId);
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
            if (SetupReuseMode.REUSE.equals(requestedMode)) {
                input.put("mode", SetupReuseMode.REUSE_SCENE);  // site_setup 1.11.0. 새 모델은 지금처럼 싣지 않는다(FULL_SETUP)
            }
            populatePreviousSceneAndZones(siteId, input);

            String jobId = jobs.enqueue(new JobSpec("site_setup", input, SETUP_MAX_ATTEMPTS, SETUP_TIMEOUT_MS, null, null));

            OffsetDateTime utcNow = utc(now);
            jdbc.update("""
                    INSERT INTO setup_session (
                        setup_session_id, site_id, work_unit_id, retry_of, job_id, status, progress_pct,
                        camera_heights_m, height_tolerance_cm, background_collection_pct,
                        requested_by, started_at, updated_at, requested_mode, mode
                    ) VALUES (
                        :id, :site, :workId, :retryOf, :jobId, 'COLLECTING_BACKGROUND', 0,
                        CAST(:heights AS jsonb), :tolerance, CAST(:collection AS jsonb),
                        :userId, :now, :now, :mode, :mode
                    )
                    """, new MapSqlParameterSource()
                    .addValue("mode", requestedMode)
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

    /** 세팅 세션에 귀속된 VLM 위험구역 초안 목록 조회(GET /api/setup-sessions/{id}/zone-drafts, S15P31A101-455). */
    public ObjectNode getZoneDrafts(UUID setupSessionId) {
        String siteId = sites.siteId();
        SessionRow session = findForUpdateOrView(siteId, setupSessionId, false);
        List<ObjectNode> runs = jdbc.query("""
                SELECT draft_run_id, job_id, calibration_version, preview_scene_version, status,
                       model_version, prompt_version, latency_ms, error_code, error_message,
                       created_at, completed_at
                FROM setup_zone_draft_run
                WHERE setup_session_id = :id
                ORDER BY created_at DESC
                LIMIT 1
                """, new MapSqlParameterSource("id", setupSessionId), (rs, i) -> {
            ObjectNode run = mapper.createObjectNode();
            UUID runId = rs.getObject("draft_run_id", UUID.class);
            run.put("draft_run_id", runId.toString());
            run.put("setup_session_id", setupSessionId.toString());
            run.put("site_id", siteId);
            run.put("calibration_version", rs.getString("calibration_version"));
            run.put("preview_scene_version", rs.getString("preview_scene_version"));
            run.put("safety_use", false);
            run.put("job_id", rs.getString("job_id"));
            run.put("status", rs.getString("status"));
            putText(run, "model_version", rs.getString("model_version"));
            putText(run, "prompt_version", rs.getString("prompt_version"));
            double latency = rs.getDouble("latency_ms");
            if (rs.wasNull()) {
                run.putNull("latency_ms");
            } else {
                run.put("latency_ms", latency);
            }
            putText(run, "error_code", rs.getString("error_code"));
            putText(run, "error_message", rs.getString("error_message"));
            run.put("created_at_ms", rs.getTimestamp("created_at").getTime());
            var completed = rs.getTimestamp("completed_at");
            if (completed == null) {
                run.putNull("completed_at_ms");
            } else {
                run.put("completed_at_ms", completed.getTime());
            }
            ArrayNode draftsNode = run.putArray("drafts");
            jdbc.query("""
                    SELECT draft_id, draft_index, candidate_id, candidate_kind, zone_kind, eval_type,
                           polygon_xy::text AS polygon_xy, z_min_m, z_max_m, top_height_m, obb::text AS obb,
                           recommended_name, recommended_severity, rationale, vlm_status,
                           evidence_cameras::text AS evidence_cameras, confidence
                    FROM setup_zone_draft
                    WHERE draft_run_id = :runId
                    ORDER BY draft_index ASC
                    """, new MapSqlParameterSource("runId", runId), dRs -> {
                ObjectNode d = draftsNode.addObject();
                d.put("draft_id", dRs.getObject("draft_id", UUID.class).toString());
                d.put("draft_index", dRs.getInt("draft_index"));
                d.put("candidate_id", dRs.getString("candidate_id"));
                d.put("candidate_kind", dRs.getString("candidate_kind"));
                d.put("zone_kind", dRs.getString("zone_kind"));
                d.put("eval_type", dRs.getString("eval_type"));
                d.set("polygon_xy", mapper.readTree(dRs.getString("polygon_xy")));
                d.put("z_min_m", (double) dRs.getFloat("z_min_m"));
                d.put("z_max_m", (double) dRs.getFloat("z_max_m"));
                float topH = dRs.getFloat("top_height_m");
                if (dRs.wasNull()) {
                    d.putNull("top_height_m");
                } else {
                    d.put("top_height_m", (double) topH);
                }
                String obbRaw = dRs.getString("obb");
                if (obbRaw == null) {
                    d.putNull("obb");
                } else {
                    d.set("obb", mapper.readTree(obbRaw));
                }
                d.put("recommended_name", dRs.getString("recommended_name"));
                d.put("recommended_severity", dRs.getString("recommended_severity"));
                d.put("rationale", dRs.getString("rationale"));
                d.put("vlm_status", dRs.getString("vlm_status"));
                d.set("evidence_cameras", mapper.readTree(dRs.getString("evidence_cameras")));
                float conf = dRs.getFloat("confidence");
                if (dRs.wasNull()) {
                    d.putNull("confidence");
                } else {
                    d.put("confidence", (double) conf);
                }
            });
            return run;
        });
        if (!runs.isEmpty()) {
            return runs.getFirst();
        }
        ObjectNode empty = mapper.createObjectNode();
        empty.putNull("draft_run_id");
        empty.put("setup_session_id", setupSessionId.toString());
        empty.put("site_id", siteId);
        putText(empty, "calibration_version", session.calibrationVersion());
        putText(empty, "preview_scene_version", session.previewSceneVersion());
        empty.put("safety_use", false);
        empty.putNull("job_id");
        empty.put("status", "NOT_REQUESTED");
        empty.putNull("model_version");
        empty.putNull("prompt_version");
        empty.putNull("latency_ms");
        empty.putNull("error_code");
        empty.putNull("error_message");
        empty.putNull("created_at_ms");
        empty.putNull("completed_at_ms");
        empty.putArray("drafts");
        return empty;
    }

    /** 실패한 세팅 세션을 재시도해 새 세션을 만든다(새 카메라 높이가 있으면 기본값과 함께 갱신). */
    public ObjectNode retry(UUID setupSessionId, Map<String, Double> cameraHeightsM, long userId) {
        String siteId = sites.siteId();
        SessionRow existing = findForUpdateOrView(siteId, setupSessionId, false);
        if (!"FAILED".equals(existing.status())) {
            throw new BusinessException(ErrorCode.Setup.SETUP_RETRY_NOT_ALLOWED);
        }
        // 재시도는 처음 요청한 모드 그대로(-614)
        return create(existing.workUnitId(), cameraHeightsM, userId, setupSessionId, existing.requestedMode());
    }

    /**
     * 진행 중인 세팅 세션을 관리자가 취소한다(워커가 죽거나 작업이 멈춰 같은 현장의 새 세팅이 막힐 때).
     * 세션은 FAILED + {@code CANCELLED_BY_ADMIN} 이 되고(DB 에 CANCELLED 상태 값이 없다) 연결된 site_setup·미완료 vlm_zone_draft 작업은
     * 기존 작업 취소 경로로 끝낸다(워커의 다음 하트비트는 REJECTED/JOB_CANCELLED). 이미 끝난 세션은 409.
     */
    @Transactional
    public ObjectNode cancel(UUID setupSessionId) {
        String siteId = sites.siteId();
        SessionRow session = findForUpdateOrView(siteId, setupSessionId, true);
        if ("COMPLETED".equals(session.status()) || "FAILED".equals(session.status())) {
            throw new BusinessException(ErrorCode.Setup.SETUP_CANCEL_NOT_ALLOWED);
        }
        closeAndCancelJobs(setupSessionId, "CANCELLED_BY_ADMIN", "관리자가 세팅을 취소했습니다");
        return getInSite(siteId, setupSessionId);
    }

    /** 세팅을 마치기 전에 세션을 FAILED 로 닫고 세팅·구역 초안 대기 작업을 취소한다(관리자 취소, 대기 시간 초과) */
    private void closeAndCancelJobs(UUID setupSessionId, String code, String message) {
        OffsetDateTime now = utc(clock.instant());
        // 먼저 세션을 FAILED 로 닫아 둔다. 작업 취소 알림(JobFinished)이 와도 닫힌 세션은 그대로 둔다
        jdbc.update("""
                UPDATE setup_session
                SET status = 'FAILED', failure_code = :code, failure_message = :msg,
                    completed_at = :now, updated_at = :now
                WHERE setup_session_id = :id
                """, new MapSqlParameterSource().addValue("id", setupSessionId).addValue("code", code)
                .addValue("msg", message).addValue("now", now));
        List<String> draftJobs = jdbc.queryForList("""
                SELECT job_id FROM setup_zone_draft_run WHERE setup_session_id = :id AND status = 'QUEUED'
                """, new MapSqlParameterSource("id", setupSessionId), String.class);
        jobs.cancel(jobIdOf(setupSessionId));
        draftJobs.forEach(jobs::cancel);
    }

    /** 미리보기 맵(READY_FOR_ZONES) 전 단계. 같은 현장의 작업 시작을 막는다(WorkUnitRepository.start) */
    public static final List<String> PRE_READY_STATUSES =
            List.of("COLLECTING_BACKGROUND", "INITIAL_POSE_AND_WORLD", "JOINT_BA_AND_VERIFY", "PREVIEW_AND_VLM_DRAFT");

    /**
     * 세팅 작업을 받을 수 있는 워커가 cutoff 뒤로 보였는가. 대여 요청 시각(worker.last_seen_at)만 보면 다른 작업을 오래 돌리는
     * 바쁜 워커를 없는 것으로 보므로, 그 워커가 쥔 진행 중 대여의 하트비트도 함께 본다.
     */
    private static final String SETUP_WORKER_SEEN_SINCE = """
            EXISTS (SELECT 1 FROM worker w
                    WHERE w.revoked_at IS NULL AND 'site_setup' = ANY(w.job_types)
                      AND (w.last_seen_at > :cutoff
                           OR EXISTS (SELECT 1 FROM job_lease l WHERE l.worker_id = w.worker_id AND l.status = 'ACTIVE'
                                        AND l.last_heartbeat_at > :cutoff)))
            """;

    /**
     * 미리보기 맵 전 세션의 세팅 작업이 cutoff 전에 들어와 지금 대기 중이고, 세팅을 받을 워커가 cutoff 뒤로 한 번도 보이지 않았으면
     * 세션을 SETUP_QUEUE_TIMEOUT 으로 닫고 작업을 취소한다(S15P31A101-641). GPU 인스턴스가 꺼져 있으면 작업이 대기열에 끝없이 남아
     * 같은 현장의 새 세팅과 작업 시작을 모두 막기 때문이다. 워커가 보였으면(다른 작업으로 바쁨 등) 기다린다. 대여됐다가 워커가 죽어
     * 다시 대기열로 돌아온 작업도 같은 규칙으로 닫힌다.
     * 잠금은 대여와 같은 순서(작업 → 세션)로 잡고, 작업을 지금 대여 중이면(SKIP LOCKED) 건너뛴다.
     * @return 닫았으면 true
     */
    @Transactional
    public boolean failUnclaimed(UUID setupSessionId, Instant cutoff) {
        MapSqlParameterSource params = new MapSqlParameterSource("id", setupSessionId).addValue("cutoff", utc(cutoff));
        List<String> job = jdbc.queryForList("""
                SELECT j.job_id FROM analysis_job j JOIN setup_session s ON s.job_id = j.job_id
                WHERE s.setup_session_id = :id AND j.status = 'QUEUED' AND j.created_at <= :cutoff
                  AND NOT\s""" + SETUP_WORKER_SEEN_SINCE + """
                FOR UPDATE OF j SKIP LOCKED
                """, params, String.class);
        if (job.isEmpty()) {
            return false;
        }
        Boolean preReady = jdbc.queryForObject("""
                SELECT status IN (:pre) FROM setup_session WHERE setup_session_id = :id FOR UPDATE
                """, params.addValue("pre", PRE_READY_STATUSES), Boolean.class);
        if (!Boolean.TRUE.equals(preReady)) {
            return false;
        }
        closeAndCancelJobs(setupSessionId, "SETUP_QUEUE_TIMEOUT",
                "GPU 서버가 세팅 작업을 가져가지 않아 세팅을 닫았습니다. GPU 서버를 켠 뒤 다시 세팅하세요");
        log.warn("세팅 워커가 보이지 않아 대기 중인 세팅을 시간 초과로 닫았다: session={} job={}", setupSessionId, job.getFirst());
        return true;
    }

    /** 대기 시간 초과 후보: 미리보기 맵 전 세션 중 작업이 cutoff 전에 들어와 대기 중이고 세팅 워커가 cutoff 뒤로 보이지 않은 것 */
    public List<UUID> unclaimedBefore(Instant cutoff) {
        return jdbc.queryForList("""
                SELECT s.setup_session_id FROM setup_session s JOIN analysis_job j ON j.job_id = s.job_id
                WHERE s.status IN (:pre) AND j.status = 'QUEUED' AND j.created_at <= :cutoff
                  AND NOT\s""" + SETUP_WORKER_SEEN_SINCE,
                new MapSqlParameterSource("pre", PRE_READY_STATUSES).addValue("cutoff", utc(cutoff)), UUID.class);
    }

    private String jobIdOf(UUID setupSessionId) {
        return jdbc.queryForObject("SELECT job_id FROM setup_session WHERE setup_session_id = :id",
                new MapSqlParameterSource("id", setupSessionId), String.class);
    }

    /**
     * 작업이 시작되면(LIVE) 같은 현장에서 미리보기 맵 뒤(READY_FOR_ZONES·BAKING_TEXTURE) 재시도를 기다리는 세팅 굽기를 회수 완료로 닫는다
     * (명세 3.2 5항, S15P31A101-625). 대여 중인 굽기는 하트비트 신호로 워커가 회수한다(-597). 대기열 작업은 회수할 주체가 없어
     * 작업이 끝난 뒤 처음부터 다시 돌았고(재개 없음), 세션이 READY 전으로 돌아가 다음 작업 시작까지 막았다.
     * 작업 시작 트랜잭션 안에서 돈다. 여기서 실패하면 작업 시작도 되돌아간다.
     */
    @EventListener
    public void onWorkStarted(WorkStarted event) {
        List<String> queued = jdbc.queryForList("""
                SELECT s.job_id FROM setup_session s JOIN analysis_job j ON j.job_id = s.job_id
                WHERE s.site_id = :site AND s.status IN ('READY_FOR_ZONES', 'BAKING_TEXTURE') AND j.status = 'QUEUED'
                """, new MapSqlParameterSource("site", event.siteId()), String.class);
        queued.forEach(this::reclaimQueuedBake);
    }

    /**
     * 같은 현장 작업이 진행 중(LIVE)인데 세팅 작업이 재시도·대여 만료·반납으로 다시 대기열에 오면, 작업 뒤 처음부터 다시 돌지 않게
     * 회수 완료로 닫는다(S15P31A101-625). 작업이 없으면 지금처럼 다시 대여된다. 대기열로 바꾼 트랜잭션 안에서 돈다.
     */
    @EventListener
    public void onJobStateChanged(JobStateChanged event) {
        if (!"site_setup".equals(event.jobType()) || !"QUEUED".equals(event.status())
                || supplements.isRebakeJob(event.jobId())) {
            return;
        }
        List<String> site = jdbc.queryForList("SELECT site_id FROM setup_session WHERE job_id = :job",
                new MapSqlParameterSource("job", event.jobId()), String.class);
        if (!site.isEmpty() && liveWorkInProgress(site.getFirst())) {
            reclaimQueuedBake(event.jobId());
        }
    }

    /**
     * 대기 중인 세팅 작업의 세션이 미리보기 맵 뒤면 세션을 COMPLETED 로 닫고(새 모델은 bake_skipped=LIVE_WORK_STARTED, 정식 Scene 없음)
     * 작업을 취소한다. 워커 회수(-597) 결과와 같은 상태다. 관제는 세팅 완료(C17, -615)가 켠 미리보기로 하고, 정식 맵은 작업 뒤 관리자가
     * 다시 굽기(-607)로 얻는다(자동으로 다시 굽지 않는다). 세션을 먼저 닫아 취소 알림(JobFinished)이 세션을 FAILED 로 바꾸지 않게 한다.
     */
    private void reclaimQueuedBake(String jobId) {
        // 하트비트·제출 경로와 같은 순서(작업 행 → 세션 행)로 잠근다
        List<String> jobStatus = jdbc.queryForList("SELECT status FROM analysis_job WHERE job_id = :job FOR UPDATE",
                new MapSqlParameterSource("job", jobId), String.class);
        if (jobStatus.isEmpty() || !"QUEUED".equals(jobStatus.getFirst())) {
            return;
        }
        SessionRow session = findByJobForUpdate(jobId);
        if (session == null || !Set.of("READY_FOR_ZONES", "BAKING_TEXTURE").contains(session.status())
                || session.sceneVersion() != null) {
            return;
        }
        // 이전 모델 재사용(-614)은 굽기가 없어 bake_skipped 없이 끝난다(applySnapshotCore 와 같은 규칙)
        String bakeSkipped = SetupReuseMode.REUSE.equals(session.mode()) ? null : BAKE_SKIPPED_LIVE_WORK;
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                UPDATE setup_session
                SET status = 'COMPLETED', bake_skipped = :bakeSkipped, completed_at = :now, updated_at = :now
                WHERE setup_session_id = :id
                """, new MapSqlParameterSource().addValue("id", session.setupSessionId())
                .addValue("bakeSkipped", bakeSkipped).addValue("now", now));
        jobs.cancel(jobId);
        log.info("세팅 세션 {}: 작업 진행 중이라 대기 중인 정식 굽기(작업 {})를 회수했다(bake_skipped={})",
                session.setupSessionId(), jobId, bakeSkipped);
    }

    /** 유효 대여의 하트비트에 실린 {@code site_setup} 진행 스냅샷을 검증·반영한다. */
    @EventListener
    public void onProgress(JobProgress event) {
        // 맵 보강 다시 굽기 작업(S15P31A101-607)은 세션의 원래 작업이 아니라 다시 굽기 행만 바꾼다
        if (supplements.isRebakeJob(event.jobId())) {
            supplements.onRebakeProgress(event);
            return;
        }
        SessionRow session = findByJobForUpdate(event.jobId());
        if (session == null || "COMPLETED".equals(session.status()) || "FAILED".equals(session.status())) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        // READY_FOR_ZONES 에서 하던 구성 묶음 발행·미리보기 현재화는 세팅 완료(C17)로 옮겼다(S15P31A101-615).
        // 세팅 화면은 미리보기를 버전 지정 보기(GET /api/scenes/{v}/view)로 그리고, 관제 맵은 완료 전까지 그대로다
        applySnapshot(session, event.jobId(), event.leaseId(), event.leaseId(), event.setup(), false);
    }

    /** {@code site_setup} 작업 종료 콜백을 반영한다. */
    @EventListener
    public void onFinished(JobFinished event) {
        if ("vlm_zone_draft".equals(event.jobType())) {
            handleZoneDraftFinishedSafe(event);
            return;
        }
        if (!"site_setup".equals(event.jobType())) {
            return;
        }
        if (supplements.isRebakeJob(event.jobId())) {
            SetupSupplementService.Applied applied = supplements.onRebakeFinished(event);
            // 완료 전 다시 굽기는 세션 맵만 옮긴다(완료가 그 맵을 켠다). 완료 뒤 도착하면 세팅 정식 Scene 과 같은 절차:
            // 지금 현재 Scene 이 기준 맵일 때만, 관제 중이면 하지 않는다
            if (applied != null && setupCompleted(applied.setupSessionId())) {
                promoteSetupSceneAfterCommit(applied.setupSessionId(), applied.siteId(), applied.calibrationVersion(),
                        applied.sceneVersion(), SetupArtifactRegistrar.SCENE, applied.baseSceneVersion(),
                        "현장 세팅 맵 보강 다시 굽기 반영(보조 촬영 입력, 위험 판정 미사용)");
            }
            return;
        }
        SessionRow session = findByJobForUpdate(event.jobId());
        if (session == null || "COMPLETED".equals(session.status()) || "FAILED".equals(session.status())) {
            return;
        }
        if ("SUCCEEDED".equals(event.status())) {
            // 결과 산출물 키는 결과를 낸 대여(SUBMITTED)의 경로다. 단계·진행률 기준선은 기존대로 마지막 하트비트 대여
            UUID submittedLease = jdbc.queryForList("""
                    SELECT l.lease_id FROM job_lease l JOIN analysis_job j ON j.job_id = l.job_id AND j.attempt = l.attempt
                    WHERE l.job_id = :job AND l.status = 'SUBMITTED'
                    """, new MapSqlParameterSource("job", event.jobId()), UUID.class).stream().findFirst().orElse(null);
            applySnapshot(session, event.jobId(), session.progressLeaseId(), submittedLease, event.result(), true);
            // 완료(C17) 전이면 묶음·Scene 은 완료가 한다(완료가 이 정식 맵을 켠다). 완료 뒤 도착한 결과만 아래로 잇는다
            if (!setupCompleted(session.setupSessionId())) {
                return;
            }
            String calibration = optionalText(event.result(), "calibration_version", session.calibrationVersion());
            // 완료가 이 보정으로 이미 발행했으면 만들지 않는다(보정이 바뀐 결과만 새 묶음)
            publishSetupConfig(session.setupSessionId(), calibration, session.requestedBy());
            // 정식 Scene 은 지금 현재 Scene 이 이 세션의 미리보기일 때만 교체한다(관리자가 고른 다른 Scene 은 건드리지 않음).
            // 이전 모델 재사용(REUSE_SCENE, -614)은 새 Scene 이 없어 현재 관제 맵이 그대로다
            if (!SetupReuseMode.reuseScene(event.result())) {
                promoteSetupSceneAfterCommit(session.setupSessionId(), session.siteId(), calibration,
                        optionalText(event.result(), "scene_version", session.sceneVersion()), SetupArtifactRegistrar.SCENE,
                        optionalText(event.result(), "preview_scene_version", session.previewSceneVersion()));
            }
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

    private void applySnapshot(SessionRow session, String jobId, UUID leaseId, UUID artifactLeaseId, JsonNode snapshot,
            boolean terminalResult) {
        applySnapshotCore(session, jobId, leaseId, artifactLeaseId, snapshot, terminalResult);
        if (SetupReuseMode.reuseScene(snapshot)) {
            return;  // 이전 모델 재사용(-614): AI 구역 초안이 필요 없다(화면이 현재 관제 구역을 채운다)
        }
        String cal = optionalText(snapshot, "calibration_version", session.calibrationVersion());
        String preview = optionalText(snapshot, "preview_scene_version", session.previewSceneVersion());
        if (cal != null && preview != null) {
            enqueueZoneDraftIfReadySafe(session.setupSessionId(), session.siteId(), cal, preview);
        }
    }

    private void applySnapshotCore(SessionRow session, String jobId, UUID leaseId, UUID artifactLeaseId, JsonNode snapshot,
            boolean terminalResult) {
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
        // 새 모델 / 이전 모델 재사용(-614). 재사용은 미리보기·정식 Scene·구역 초안이 없다(현재 관제 맵을 그대로 쓴다)
        SetupReuseMode.Applied mode = SetupReuseMode.read(mapper, session.requestedMode(), session.mode(), sameLease, snapshot);
        String calibrationVersion = optionalText(snapshot, "calibration_version", session.calibrationVersion());
        String previewSceneVersion = optionalText(snapshot, "preview_scene_version", session.previewSceneVersion());
        String sceneVersion = optionalText(snapshot, "scene_version", session.sceneVersion());
        String configVersion = optionalText(snapshot, "config_version", session.configVersion());
        String zoneDraftsRef = optionalText(snapshot, "zone_drafts_ref", session.zoneDraftsRef());
        if (mode.reuse()) {
            if (optionalText(snapshot, "preview_scene_version", null) != null || optionalText(snapshot, "scene_version", null) != null
                    || optionalText(snapshot, "zone_drafts_ref", null) != null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            // 앞선 대여가 새 모델로 이어 가며 남긴 미리보기는 이 결과의 것이 아니다
            previewSceneVersion = null;
            sceneVersion = null;
            zoneDraftsRef = null;
        }

        JsonNode heightChecksNode = snapshot.get("height_checks");
        String heightChecksJson = session.heightChecksJson();
        if (heightChecksNode != null && !heightChecksNode.isNull()) {
            heightChecksJson = validateAndEnrichHeightChecks(heightChecksNode, expectedHeights, session.heightToleranceCm());
        }

        if (readyOrLater) {
            if (calibrationVersion == null || (!mode.reuse() && previewSceneVersion == null) || heightChecksJson == null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            // 재사용은 World 다시 맞추기가 채택된 뒤에만 READY 다(계약 site_setup 1.11.0 reuse_rules)
            if (mode.reuse() && mode.worldAnchorJson() == null) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        // 정식 굽기를 작업 시작(LIVE)으로 회수한 결과만 정식 Scene 없이 끝난다(site_setup 1.7.0, 명세 3.2 5항, S15P31A101-597).
        // 관제는 READY_FOR_ZONES 때 현재 Scene 이 된 미리보기로 한다. 계약이 bake_skipped ↔ scene_version null 을 묶는다.
        // 이전 모델 재사용(-614)은 굽기가 없어 정식 Scene 없이 끝난다(bake_skipped 없음)
        String bakeSkipped = terminalResult ? optionalText(snapshot, "bake_skipped", null) : null;
        if (terminalResult && !mode.reuse() && sceneVersion == null && !BAKE_SKIPPED_LIVE_WORK.equals(bakeSkipped)) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        if (bakeSkipped != null && (mode.reuse() || !BAKE_SKIPPED_LIVE_WORK.equals(bakeSkipped) || sceneVersion != null)) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        if (bakeSkipped != null && !liveWorkInProgress(session.siteId())) {
            // 워커가 신호를 받은 뒤 작업이 이미 끝났을 수 있다. 결과는 받되(미리보기로 충분) 흔적을 남긴다
            log.warn("세팅 세션 {}: 진행 중 작업이 없는데 정식 굽기 건너뜀({}) 결과가 왔다", session.setupSessionId(), bakeSkipped);
        }

        // 이 스냅샷이 알린 새 산출물을 먼저 등록한다(이 작업·이 대여 경로, 해시 대조). 등록 뒤 계보 검사가 이 세션 귀속을 본다
        artifacts.register(new SetupArtifactRegistrar.Target(session.setupSessionId(), session.siteId(), jobId, artifactLeaseId,
                        new TreeSet<>(expectedHeights.keySet()), session.requestedBy()),
                snapshot, calibrationVersion, previewSceneVersion, sceneVersion);
        validateVersionLineage(session.setupSessionId(), session.siteId(), calibrationVersion, previewSceneVersion, sceneVersion,
                configVersion);

        String objectCrossChecksJson = session.objectCrossChecksJson();
        JsonNode crossNode = snapshot.get("object_cross_checks");
        if (crossNode != null && !crossNode.isNull()) {
            objectCrossChecksJson = validateObjectCrossChecks(crossNode);
        }
        // 쌍별 홀드아웃 오차(S15P31A101-602). 표시용이라 이 스냅샷에 없거나 어긋나면 null 이고 아래 COALESCE 로 이전 값을 둔다
        String pairHoldoutJson = SetupPairHoldout.fromSnapshot(mapper, snapshot);
        // 보정 품질 게이트·카메라 확인(S15P31A101-646). 없거나 어긋나면 null 이고 아래 COALESCE 로 이전 값을 둔다
        String qualityGateJson = SetupCameraCheck.qualityGate(mapper, snapshot);
        String cameraCheckJson = SetupCameraCheck.cameraCheck(mapper, snapshot);
        // 구역별 상면 높이 차이 판정(S15P31A101-630). 없거나 어긋나면 null 이고 같은 대여 안에서는 이전 값을 둔다
        String zoneHeightChangesJson = SetupZoneHeightChanges.fromSnapshot(mapper, snapshot);

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
                    pair_holdout = COALESCE(CAST(:pairHoldout AS jsonb), pair_holdout),
                    quality_gate = COALESCE(CAST(:qualityGate AS jsonb), quality_gate),
                    camera_check = COALESCE(CAST(:cameraCheck AS jsonb), camera_check),
                    calibration_version = :cal,
                    preview_scene_version = :previewScene,
                    scene_version = :scene,
                    config_version = :config,
                    zone_drafts_ref = :zoneDrafts,
                    bake_skipped = COALESCE(:bakeSkipped, bake_skipped),
                    ready_at = COALESCE(ready_at, :readyAt),
                    completed_at = COALESCE(completed_at, :completedAt),
                    mode = :mode,
                    reuse_fallback_reason = :fallback,
                    world_anchor = COALESCE(CAST(:worldAnchor AS jsonb), CASE WHEN :keepAnchor THEN world_anchor END),
                    zone_height_changes = COALESCE(CAST(:zoneHeightChanges AS jsonb), CASE WHEN :keepAnchor THEN zone_height_changes END),
                    updated_at = :now
                WHERE setup_session_id = :id
                """, new MapSqlParameterSource()
                .addValue("id", session.setupSessionId())
                .addValue("mode", mode.mode())
                .addValue("fallback", mode.fallbackReason())
                .addValue("worldAnchor", mode.worldAnchorJson())
                .addValue("zoneHeightChanges", zoneHeightChangesJson)
                // 재대여는 처음부터 다시 하므로 앞선 대여의 World 다시 맞추기 결과를 두지 않는다
                .addValue("keepAnchor", sameLease)
                .addValue("leaseId", nextLeaseId)
                .addValue("status", stage)
                .addValue("pct", progressPct)
                .addValue("bg", backgroundCollectionJson)
                .addValue("heightChecks", heightChecksJson)
                .addValue("crossChecks", objectCrossChecksJson)
                .addValue("pairHoldout", pairHoldoutJson)
                .addValue("qualityGate", qualityGateJson)
                .addValue("cameraCheck", cameraCheckJson)
                .addValue("cal", calibrationVersion)
                .addValue("previewScene", previewSceneVersion)
                .addValue("scene", sceneVersion)
                .addValue("config", configVersion)
                .addValue("zoneDrafts", zoneDraftsRef)
                .addValue("bakeSkipped", bakeSkipped)
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

    /**
     * READY_FOR_ZONES 이후(그리고 결과 성공 때) 이 세션이 등록한 새 보정을 Tier 0 에 적용하는 기존 경로(구성 묶음 발행)를 잇는다
     * (S15P31A101-467). 묶음은 하트비트·제출이 커밋된 뒤 별도 트랜잭션에서 만들고, 그 보정의 묶음이 이미 있으면 만들지 않는다
     * (ConfigBundleService.publishForSetupAfterCommit). 이 세션이 artifacts 로 등록한 보정이 아니면(이미 있던 보정을 참조만 함)
     * 새 묶음을 만들지 않는다.
     */
    private void publishSetupConfig(UUID setupSessionId, String calibrationVersion, Long requestedBy) {
        if (calibrationVersion == null || requestedBy == null
                || !artifacts.registeredBySession(setupSessionId, SetupArtifactRegistrar.CALIBRATION, calibrationVersion)) {
            return;
        }
        configs.publishForSetupAfterCommit(calibrationVersion, requestedBy);
    }

    /**
     * 세팅 Scene 자동 현재화(S15P31A101-467). 하트비트·제출이 커밋된 뒤 별도 트랜잭션에서 기존 활성화 경로
     * (SceneActivationService, 이력 추가·덮어쓰지 않음)로 이 세션이 등록한 Scene 을 현재 Scene 으로 올린다. Scene 은 표시용이고
     * 미리보기는 quality.notes 의 scene_tier=PREVIEW·safety_use=false 를 그대로 둔다(위험 판정 미사용).
     * <ul>
     *   <li>이 세션이 artifacts 로 등록한 보정·Scene 일 때만. 같은 현장에 관제 중(IN_PROGRESS) 작업이 있으면 하지 않는다.
     *   <li>미리보기(expectedCurrent=null): 이 세션 이름으로 그 미리보기를 이미 한 번 올렸으면 다시 올리지 않는다(이어지는 하트비트가
     *       관리자 선택을 되돌리지 않게).
     *   <li>정식(expectedCurrent=미리보기): 지금 현재 Scene 이 이 세션의 미리보기일 때만 교체한다.
     * </ul>
     * 실패해도 세팅은 되돌리지 않고 경고만 남긴다.
     */
    private void promoteSetupSceneAfterCommit(UUID setupSessionId, String siteId, String calibrationVersion, String sceneVersion,
            String kind, String expectedCurrent) {
        promoteSetupSceneAfterCommit(setupSessionId, siteId, calibrationVersion, sceneVersion, kind, expectedCurrent,
                SetupArtifactRegistrar.PREVIEW_SCENE.equals(kind)
                        ? "현장 세팅 미리보기 자동 현재화(PREVIEW, 위험 판정 미사용)"
                        : "현장 세팅 정식 Scene 이 미리보기를 교체");
    }

    private void promoteSetupSceneAfterCommit(UUID setupSessionId, String siteId, String calibrationVersion, String sceneVersion,
            String kind, String expectedCurrent, String reason) {
        if (sceneVersion == null || calibrationVersion == null
                || !artifacts.registeredBySession(setupSessionId, SetupArtifactRegistrar.CALIBRATION, calibrationVersion)
                || !artifacts.registeredBySession(setupSessionId, kind, sceneVersion)) {
            return;
        }
        String actor = "setup:" + setupSessionId;
        Runnable task = () -> {
            try {
                requiresNew.executeWithoutResult(status -> {
                    MapSqlParameterSource params = new MapSqlParameterSource("site", siteId).addValue("v", sceneVersion)
                            .addValue("actor", actor);
                    if (Boolean.TRUE.equals(jdbc.queryForObject("""
                            SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS')
                            """, params, Boolean.class))) {
                        log.warn("세팅 세션 {} 의 Scene {} 을 현재로 올리지 않는다: 같은 현장에 진행 중(IN_PROGRESS) 작업이 있다",
                                setupSessionId, sceneVersion);
                        return;
                    }
                    if (Boolean.TRUE.equals(jdbc.queryForObject("""
                            SELECT EXISTS (SELECT 1 FROM scene_activation
                                           WHERE site_id = :site AND scene_version = :v AND actor = :actor)
                            """, params, Boolean.class))) {
                        return;  // 이 세션이 이미 한 번 올렸다
                    }
                    boolean done = scenes.activateIfCurrent(siteId, sceneVersion, expectedCurrent, actor, reason);
                    if (done) {
                        log.info("세팅 세션 {} 의 Scene {} 을 현재 Scene 으로 올림", setupSessionId, sceneVersion);
                    }
                });
            } catch (RuntimeException e) {
                log.warn("세팅 세션 {} 의 Scene {} 자동 현재화 실패(세팅은 유지): {}", setupSessionId, sceneVersion, e.getMessage());
            }
        };
        if (org.springframework.transaction.support.TransactionSynchronizationManager.isSynchronizationActive()) {
            org.springframework.transaction.support.TransactionSynchronizationManager.registerSynchronization(
                    new org.springframework.transaction.support.TransactionSynchronization() {
                        @Override
                        public void afterCommit() {
                            task.run();
                        }
                    });
            return;
        }
        task.run();
    }

    /**
     * 바깥 트랜잭션이 있으면 SQL SAVEPOINT 로 감싸 실패한 부분만 되돌린다. JpaTransactionManager 의 PROPAGATION_NESTED 는
     * HibernateJpaDialect 가 savepoint 를 지원하지 않아 쓸 수 없다. JdbcTemplate 은 JPA 트랜잭션과 같은 연결을 쓴다.
     */
    private void withSavepoint(Runnable action) {
        if (!TransactionSynchronizationManager.isActualTransactionActive()) {
            action.run();
            return;
        }
        JdbcTemplate plain = jdbc.getJdbcTemplate();
        plain.execute("SAVEPOINT setup_side_effect");
        try {
            action.run();
            plain.execute("RELEASE SAVEPOINT setup_side_effect");
        } catch (RuntimeException | Error e) {
            try {
                plain.execute("ROLLBACK TO SAVEPOINT setup_side_effect");
            } catch (RuntimeException rollback) {
                e.addSuppressed(rollback);
            }
            throw e;
        }
    }

    /** 이 세션으로 세팅을 완료했는지(C17): 작업에 걸린 세션이고 작업의 setup_completed_at 이 있다 */
    private boolean setupCompleted(UUID setupSessionId) {
        return Boolean.TRUE.equals(jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM work_unit
                               WHERE active_setup_session_id = :id AND setup_completed_at IS NOT NULL)
                """, new MapSqlParameterSource("id", setupSessionId), Boolean.class));
    }

    private void enqueueZoneDraftIfReadySafe(UUID setupSessionId, String siteId, String calibrationVersion,
            String previewSceneVersion) {
        try {
            // 하트비트·제출 트랜잭션 안에서 돈다. SQL 오류를 삼키기만 하면 PostgreSQL 트랜잭션이 중단돼 바깥 커밋까지 롤백되므로
            // savepoint 로 이 부분만 되돌린다
            withSavepoint(() ->
                    enqueueZoneDraftIfReady(setupSessionId, siteId, calibrationVersion, previewSceneVersion));
        } catch (RuntimeException e) {
            log.warn("세팅 세션 {} 의 vlm_zone_draft 작업 등록 실패(세팅 진행에는 영향 없음): {}", setupSessionId, e.getMessage());
        }
    }

    private void enqueueZoneDraftIfReady(UUID setupSessionId, String siteId, String calibrationVersion,
            String previewSceneVersion) {
        Boolean exists = jdbc.queryForObject("""
                SELECT EXISTS (
                    SELECT 1 FROM setup_zone_draft_run
                    WHERE setup_session_id = :sessionId AND preview_scene_version = :previewScene
                )
                """, new MapSqlParameterSource()
                .addValue("sessionId", setupSessionId)
                .addValue("previewScene", previewSceneVersion), Boolean.class);
        if (Boolean.TRUE.equals(exists)) {
            return;
        }

        ObjectNode input = mapper.createObjectNode();
        input.put("site_id", siteId);
        input.put("setup_session_id", setupSessionId.toString());
        input.put("calibration_version", calibrationVersion);
        input.put("preview_scene_version", previewSceneVersion);

        jdbc.query("""
                SELECT calibration_ref, calibration_hash
                FROM calibration_version
                WHERE calibration_version = :cal AND site_id = :site
                """, new MapSqlParameterSource().addValue("cal", calibrationVersion).addValue("site", siteId), rs -> {
            String ref = rs.getString("calibration_ref");
            String hash = rs.getString("calibration_hash");
            if (ref != null && S3_URI_PATTERN.matcher(ref).matches()) {
                input.put("calibration_ref", ref);
            }
            if (hash != null && SHA256_PATTERN.matcher(hash).matches()) {
                input.put("calibration_hash", hash);
            }
        });

        // 세팅 세션이 등록한 미리보기 Scene(GLB) 주소. 없으면 워커가 후보 0개를 낸다
        jdbc.query("""
                SELECT object_uri FROM setup_session_artifact
                WHERE setup_session_id = :sessionId AND kind = 'PREVIEW_SCENE' AND version = :previewScene
                """, new MapSqlParameterSource()
                .addValue("sessionId", setupSessionId)
                .addValue("previewScene", previewSceneVersion), rs -> {
            String uri = rs.getString("object_uri");
            if (uri != null && S3_URI_PATTERN.matcher(uri).matches() && !input.has("preview_scene_uri")) {
                input.put("preview_scene_uri", uri);
            }
        });

        // 세팅 워커가 올려 이 세션에 등록한 카메라별 대표 프레임(S15P31A101-479). 워커가 후보별 크롭을 만들어 VLM 에 넣는다.
        // 미리보기를 등록한 그 시도(대여)의 프레임을 쓰고(재시도가 다른 배경을 썼을 수 있다), 없으면 가장 최근 등록분. 카메라당 하나
        List<Map<String, Object>> frameRows = jdbc.queryForList("""
                SELECT f.camera_id, f.object_uri, f.face_policy
                FROM setup_session_artifact f
                WHERE f.setup_session_id = :sessionId AND f.kind = 'CAMERA_FRAME'
                  AND f.lease_id = COALESCE((SELECT p.lease_id FROM setup_session_artifact p
                                             WHERE p.setup_session_id = f.setup_session_id AND p.kind = 'PREVIEW_SCENE'
                                               AND p.version = :previewScene), f.lease_id)
                ORDER BY f.camera_id, f.registered_at DESC
                """, new MapSqlParameterSource().addValue("sessionId", setupSessionId).addValue("previewScene", previewSceneVersion));
        ArrayNode frames = mapper.createArrayNode();
        Set<String> framedCameras = new HashSet<>();
        for (Map<String, Object> row : frameRows) {
            String cameraId = (String) row.get("camera_id");
            String uri = (String) row.get("object_uri");
            String facePolicy = (String) row.get("face_policy");
            if (frames.size() >= MAX_DRAFT_FRAMES || cameraId == null || uri == null || !S3_URI_PATTERN.matcher(uri).matches()
                    || facePolicy == null || !framedCameras.add(cameraId)) {
                continue;
            }
            ObjectNode frame = frames.addObject();
            frame.put("camera_id", cameraId);
            frame.put("frame_uri", uri);
            frame.put("face_policy", facePolicy);
        }
        if (!frames.isEmpty()) {
            input.set("frames", frames);
        }
        // 높이 비교가 변경 의심으로 판정한 구역과 이전 세팅 사진·보정(S15P31A101-631). 없으면 기존 초안 작업 그대로
        boolean withOpinions = addOpinionInputSafe(setupSessionId, siteId, input);

        String jobId = jobs.enqueue(new JobSpec("vlm_zone_draft", input, DRAFT_MAX_ATTEMPTS,
                withOpinions ? DRAFT_WITH_OPINIONS_TIMEOUT_MS : DRAFT_TIMEOUT_MS, null, null));
        UUID runId = UUID.randomUUID();
        OffsetDateTime now = utc(clock.instant());
        jdbc.update("""
                INSERT INTO setup_zone_draft_run (
                    draft_run_id, setup_session_id, job_id, site_id, calibration_version, preview_scene_version,
                    status, created_at
                ) VALUES (
                    :runId, :sessionId, :jobId, :site, :cal, :previewScene, 'QUEUED', :now
                )
                ON CONFLICT (setup_session_id, preview_scene_version) DO NOTHING
                """, new MapSqlParameterSource()
                .addValue("runId", runId)
                .addValue("sessionId", setupSessionId)
                .addValue("jobId", jobId)
                .addValue("site", siteId)
                .addValue("cal", calibrationVersion)
                .addValue("previewScene", previewSceneVersion)
                .addValue("now", now));
    }

    /** 의견 입력을 싣다가 실패하면 그 필드만 빼고 기존 초안 작업으로 넣는다(초안 작업 등록을 막지 않는다) */
    private boolean addOpinionInputSafe(UUID setupSessionId, String siteId, ObjectNode input) {
        boolean[] added = {false};
        try {
            withSavepoint(() -> added[0] = opinions.addOpinionInput(setupSessionId, siteId, input));
            return added[0];
        } catch (RuntimeException e) {
            input.remove(List.of("suspected_zones", "previous_setup_session_id", "previous_calibration_version",
                    "previous_calibration_ref", "previous_calibration_hash", "previous_frames"));
            log.warn("세팅 세션 {} 의 구역 의견 입력을 싣지 못해 초안만 요청한다: {}", setupSessionId, e.getMessage());
            return false;
        }
    }

    private void handleZoneDraftFinishedSafe(JobFinished event) {
        try {
            // 작업 제출 트랜잭션 안의 리스너다. 위와 같은 까닭으로 savepoint 로 가둔다
            withSavepoint(() -> handleZoneDraftFinished(event));
        } catch (RuntimeException e) {
            log.warn("vlm_zone_draft 작업 {} 결과 저장 실패(세팅 세션에는 영향 없음): {}", event.jobId(), e.getMessage());
        }
    }

    private static final Set<String> DRAFT_CANDIDATE_KINDS = Set.of("ELEVATED_WORK_SURFACE", "OPENING", "FLOOR_EDGE");
    private static final Set<String> DRAFT_EVAL_TYPES = Set.of("GROUND_FOOTPRINT", "VOLUME_3D");
    private static final Set<String> DRAFT_SEVERITIES = Set.of("CRITICAL", "HIGH");
    private static final Set<String> DRAFT_VLM_STATUSES = Set.of("VLM", "NO_OPINION");

    /** setup_zone_draft(V34) 제약을 먼저 본다. 어기면 이유, 맞으면 null */
    static String draftProblem(JsonNode d) {
        if (d == null || !d.isObject()) {
            return "객체가 아님";
        }
        if (!textWithin(d, "candidate_id", 128) || !textWithin(d, "zone_kind", 64)) {
            return "candidate_id·zone_kind 누락 또는 너무 김";
        }
        if (!textWithin(d, "recommended_name", 160) || !textWithin(d, "rationale", 500)) {
            return "recommended_name·rationale 누락 또는 너무 김";
        }
        if (!DRAFT_CANDIDATE_KINDS.contains(d.path("candidate_kind").asString(""))
                || !DRAFT_EVAL_TYPES.contains(d.path("eval_type").asString(""))
                || !DRAFT_SEVERITIES.contains(d.path("recommended_severity").asString(""))
                || !DRAFT_VLM_STATUSES.contains(d.path("vlm_status").asString(""))) {
            return "candidate_kind·eval_type·recommended_severity·vlm_status 값이 목록 밖";
        }
        if (!d.path("polygon_xy").isArray() || d.get("polygon_xy").isEmpty()) {
            return "polygon_xy 가 비었음";
        }
        if (!finiteFloat(d.get("z_min_m")) || !finiteFloat(d.get("z_max_m"))) {
            return "z_min_m·z_max_m 이 유한한 수가 아님";
        }
        for (String optional : List.of("top_height_m", "confidence")) {
            JsonNode n = d.get(optional);
            if (n != null && !n.isNull() && !finiteFloat(n)) {
                return optional + " 이 유한한 수가 아님";
            }
        }
        return null;
    }

    private static boolean textWithin(JsonNode d, String field, int max) {
        JsonNode n = d.get(field);
        return n != null && n.isString() && n.asString().length() <= max;
    }

    /** real 열에 넣을 수(float 로 바꿔도 유한) */
    private static boolean finiteFloat(JsonNode n) {
        return n != null && n.isNumber() && Float.isFinite((float) n.doubleValue());
    }

    private void handleZoneDraftFinished(JobFinished event) {
        List<DraftRunRow> runs = jdbc.query("""
                SELECT draft_run_id, setup_session_id, status
                FROM setup_zone_draft_run
                WHERE job_id = :jobId
                FOR UPDATE
                """, new MapSqlParameterSource("jobId", event.jobId()),
                (rs, i) -> new DraftRunRow(
                        rs.getObject("draft_run_id", UUID.class),
                        rs.getObject("setup_session_id", UUID.class),
                        rs.getString("status")));
        if (runs.isEmpty() || !"QUEUED".equals(runs.getFirst().status())) {
            return;
        }
        DraftRunRow run = runs.getFirst();
        OffsetDateTime now = utc(clock.instant());

        if ("SUCCEEDED".equals(event.status()) && event.result() != null && event.result().isObject()) {
            JsonNode res = event.result();
            String resStatus = "NO_OPINION".equals(res.path("status").asString(null)) ? "NO_OPINION" : "SUCCEEDED";
            String modelVer = res.path("model_version").isString() ? res.get("model_version").asString() : null;
            String promptVer = res.path("prompt_version").isString() ? res.get("prompt_version").asString() : null;
            Double latencyMs = res.path("latency_ms").isNumber() ? res.get("latency_ms").doubleValue() : null;

            jdbc.update("""
                    UPDATE setup_zone_draft_run
                    SET status = :status, model_version = :model, prompt_version = :prompt,
                        latency_ms = :latency, completed_at = :now
                    WHERE draft_run_id = :runId
                    """, new MapSqlParameterSource()
                    .addValue("runId", run.draftRunId())
                    .addValue("status", resStatus)
                    .addValue("model", modelVer)
                    .addValue("prompt", promptVer)
                    .addValue("latency", latencyMs)
                    .addValue("now", now));

            JsonNode drafts = res.get("drafts");
            if (drafts != null && drafts.isArray()) {
                for (int i = 0; i < drafts.size(); i++) {
                    JsonNode d = drafts.get(i);
                    String problem = draftProblem(d);
                    if (problem != null) {
                        // 한 항목이 표 제약을 어기면 나머지 초안까지 잃지 않게 그 항목만 뺀다
                        log.warn("vlm_zone_draft 작업 {} 의 초안 {} 를 뺀다: {}", event.jobId(), i, problem);
                        continue;
                    }
                    JsonNode topHNode = d.get("top_height_m");
                    Float topH = (topHNode != null && topHNode.isNumber()) ? (float) topHNode.doubleValue() : null;
                    JsonNode obbNode = d.get("obb");
                    String obbJson = (obbNode != null && !obbNode.isNull()) ? mapper.writeValueAsString(obbNode) : null;
                    JsonNode confNode = d.get("confidence");
                    Float conf = (confNode != null && confNode.isNumber()) ? (float) confNode.doubleValue() : null;
                    JsonNode camsNode = d.get("evidence_cameras");
                    String camsJson = (camsNode != null && camsNode.isArray())
                            ? mapper.writeValueAsString(camsNode) : "[]";

                    jdbc.update("""
                            INSERT INTO setup_zone_draft (
                                draft_id, draft_run_id, setup_session_id, job_id, draft_index,
                                candidate_id, candidate_kind, zone_kind, eval_type, polygon_xy,
                                z_min_m, z_max_m, top_height_m, obb, recommended_name,
                                recommended_severity, rationale, vlm_status, evidence_cameras,
                                confidence, created_at
                            ) VALUES (
                                :id, :runId, :sessionId, :jobId, :idx,
                                :candId, :candKind, :zoneKind, :evalType, CAST(:poly AS jsonb),
                                :zMin, :zMax, :topH, CAST(:obb AS jsonb), :name,
                                :severity, :rationale, :vlmStatus, CAST(:cams AS jsonb),
                                :conf, :now
                            )
                            ON CONFLICT (draft_run_id, draft_index) DO NOTHING
                            """, new MapSqlParameterSource()
                            .addValue("id", UUID.randomUUID())
                            .addValue("runId", run.draftRunId())
                            .addValue("sessionId", run.setupSessionId())
                            .addValue("jobId", event.jobId())
                            .addValue("idx", i)
                            .addValue("candId", d.path("candidate_id").asString())
                            .addValue("candKind", d.path("candidate_kind").asString())
                            .addValue("zoneKind", d.path("zone_kind").asString())
                            .addValue("evalType", d.path("eval_type").asString())
                            .addValue("poly", mapper.writeValueAsString(d.get("polygon_xy")))
                            .addValue("zMin", (float) d.path("z_min_m").doubleValue())
                            .addValue("zMax", (float) d.path("z_max_m").doubleValue())
                            .addValue("topH", topH)
                            .addValue("obb", obbJson)
                            .addValue("name", d.path("recommended_name").asString())
                            .addValue("severity", d.path("recommended_severity").asString())
                            .addValue("rationale", d.path("rationale").asString())
                            .addValue("vlmStatus", d.path("vlm_status").asString())
                            .addValue("cams", camsJson)
                            .addValue("conf", conf)
                            .addValue("now", now));
                }
            }

            // 변경 의심 구역 의견(S15P31A101-631). 표시용 제안이라 구역 목록·확인 필요는 건드리지 않는다.
            // 의견 저장이 실패해도 초안은 남긴다(savepoint 로 이 부분만 되돌린다)
            try {
                withSavepoint(() -> opinions.store(run.draftRunId(), run.setupSessionId(), event.jobId(), res, now));
            } catch (RuntimeException e) {
                log.warn("vlm_zone_draft 작업 {} 의 구역 의견 저장 실패(초안은 저장): {}", event.jobId(), e.getMessage());
            }

            jdbc.update("""
                    UPDATE setup_session
                    SET zone_drafts_ref = COALESCE(zone_drafts_ref, :ref), updated_at = :now
                    WHERE setup_session_id = :sessionId
                    """, new MapSqlParameterSource()
                    .addValue("sessionId", run.setupSessionId())
                    .addValue("ref", "zone-draft-run:" + run.draftRunId())
                    .addValue("now", now));
            return;
        }

        String code = event.status();
        String message = "VLM 위험구역 초안 생성이 실패했습니다";
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
        jdbc.update("""
                UPDATE setup_zone_draft_run
                SET status = 'FAILED', error_code = :code, error_message = :msg, completed_at = :now
                WHERE draft_run_id = :runId
                """, new MapSqlParameterSource()
                .addValue("runId", run.draftRunId())
                .addValue("code", code)
                .addValue("msg", message)
                .addValue("now", now));
    }

    private record DraftRunRow(UUID draftRunId, UUID setupSessionId, String status) {
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

    /**
     * 버전 참조의 현장·보정 계보. 버전은 같은 현장에 있어야 하고, 세팅 세션에 귀속된 버전(setup_session_artifact)이면 이 세션 귀속이어야
     * 한다(다른 세팅 세션이 만든 보정·Scene 을 이 세션 결과로 쓰지 못하게). 귀속이 없는 버전은 관리자·Tier 0 가 등록한 기존 버전이다.
     */
    private void validateVersionLineage(UUID setupSessionId, String siteId, String calibrationVersion, String previewSceneVersion,
            String sceneVersion, String configVersion) {
        if (calibrationVersion != null) {
            Boolean validCal = jdbc.queryForObject("""
                    SELECT EXISTS (
                        SELECT 1 FROM calibration_version
                        WHERE calibration_version = :cal AND site_id = :site
                          AND state IN ('PROVISIONAL', 'CALIBRATED')
                    ) AND (
                        NOT EXISTS (SELECT 1 FROM setup_session_artifact a
                                    WHERE a.kind = 'CALIBRATION' AND a.version = :cal)
                        OR EXISTS (SELECT 1 FROM setup_session_artifact a
                                   WHERE a.kind = 'CALIBRATION' AND a.version = :cal AND a.setup_session_id = :session)
                    )
                    """, new MapSqlParameterSource().addValue("cal", calibrationVersion).addValue("site", siteId)
                    .addValue("session", setupSessionId), Boolean.class);
            if (!Boolean.TRUE.equals(validCal)) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
        }
        List<String[]> scenes = List.of(
                new String[] {previewSceneVersion != null ? previewSceneVersion : "", SetupArtifactRegistrar.PREVIEW_SCENE},
                new String[] {sceneVersion != null ? sceneVersion : "", SetupArtifactRegistrar.SCENE});
        for (String[] pair : scenes) {
            String sv = pair[0];
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
                    ) AND (
                        NOT EXISTS (SELECT 1 FROM setup_session_artifact a
                                    WHERE a.kind IN ('PREVIEW_SCENE', 'SCENE') AND a.version = :scene)
                        OR EXISTS (SELECT 1 FROM setup_session_artifact a
                                   WHERE a.kind = :kind AND a.version = :scene AND a.setup_session_id = :session)
                    )
                    """, new MapSqlParameterSource().addValue("scene", sv).addValue("site", siteId)
                    .addValue("cal", calibrationVersion).addValue("kind", pair[1]).addValue("session", setupSessionId),
                    Boolean.class);
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
        checkWorkUnitCanSetup(siteId, workUnitId, "");
    }

    /** 끝난 작업은 409 WORK_INVALID_STATE, 세팅을 완료한 작업은 409 SETUP_ALREADY_COMPLETED(하루 1회, S15P31A101-615) */
    private void checkWorkUnitCanSetup(String siteId, long workUnitId, String lock) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT status, setup_completed_at IS NOT NULL AS completed FROM work_unit WHERE site_id = :site AND work_id = :id"
                        + lock,
                new MapSqlParameterSource().addValue("site", siteId).addValue("id", workUnitId));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        if ("DONE".equals(rows.getFirst().get("status"))) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        if (Boolean.TRUE.equals(rows.getFirst().get("completed"))) {
            throw new BusinessException(ErrorCode.Setup.SETUP_ALREADY_COMPLETED);
        }
    }

    /**
     * 이전 모델 재사용(-614)은 현재 관제 맵(현장당 하나)과 그 보정이 있어야 한다. 현재 맵이 Tier 0 의 지금 보정과 다른 World 로 판정되면
     * 새 보정을 그 World 에 맞춰도 맵과 어긋나므로 막는다(판정을 모르면 막지 않는다. 워커의 World 다시 맞추기가 다시 확인한다).
     */
    private void requireReusableScene(String siteId) {
        List<String[]> current = jdbc.query("SELECT s.scene_version, s.calibration_version FROM ("
                + SceneService.CURRENT_COMMITTED + """
                ) s JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                WHERE s.site_id = :site
                LIMIT 1
                """, new MapSqlParameterSource("site", siteId),
                (rs, i) -> new String[] {rs.getString("scene_version"), rs.getString("calibration_version")});
        if (current.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_REUSE_SCENE_MISSING);
        }
        JsonNode compat = compatibility.current(current.getFirst()[0], current.getFirst()[1]);
        if ("MISMATCH".equals(compat.path("verification").asString(""))) {
            throw new BusinessException(ErrorCode.Setup.SETUP_REUSE_SCENE_MISMATCH);
        }
    }

    /**
     * 지금 관제 Scene(World 일치)과 지금 유효한 구역 다각형 목록을 site_setup 입력에 싣는다(S15P31A101-630).
     * 워커가 미리보기·정식·다시 굽기 단계에서 이전 관제 Scene GLB 와 상면 높이를 비교해 구역별 판정을 내고,
     * JobObjectSignService 는 입력에 실린 previous_scene.uri 에만 GET 서명을 내준다.
     */
    private void populatePreviousSceneAndZones(String siteId, ObjectNode input) {
        record CurrentScene(String sceneVersion, String calibrationVersion, String gltfKey,
                String artifactUri, String artifactSha256) { }
        List<CurrentScene> current = jdbc.query("SELECT s.scene_version, s.calibration_version, s.gltf_key, "
                + "(SELECT a.object_uri FROM setup_session_artifact a WHERE a.kind IN ('SCENE', 'PREVIEW_SCENE') "
                + "AND a.version = s.scene_version ORDER BY (a.kind = 'SCENE') DESC, a.registered_at DESC LIMIT 1) AS artifact_uri, "
                + "(SELECT a.sha256 FROM setup_session_artifact a WHERE a.kind IN ('SCENE', 'PREVIEW_SCENE') "
                + "AND a.version = s.scene_version ORDER BY (a.kind = 'SCENE') DESC, a.registered_at DESC LIMIT 1) AS artifact_sha256 "
                + "FROM (" + SceneService.CURRENT_COMMITTED + ") s "
                + "JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id "
                + "WHERE s.site_id = :site LIMIT 1",
                new MapSqlParameterSource("site", siteId),
                (rs, i) -> new CurrentScene(rs.getString("scene_version"), rs.getString("calibration_version"),
                        rs.getString("gltf_key"), rs.getString("artifact_uri"), rs.getString("artifact_sha256")));
        if (!current.isEmpty()) {
            CurrentScene cs = current.getFirst();
            JsonNode compat = compatibility.current(cs.sceneVersion(), cs.calibrationVersion());
            if (!"MISMATCH".equals(compat.path("verification").asString(""))) {
                String uri = null;
                String sha = null;
                if (cs.artifactUri() != null && S3_URI_PATTERN.matcher(cs.artifactUri()).matches()) {
                    uri = cs.artifactUri();
                    if (cs.artifactSha256() != null && SHA256_HEX_PATTERN.matcher(cs.artifactSha256()).matches()) {
                        sha = cs.artifactSha256();
                    }
                } else if (cs.gltfKey() != null && S3_URI_PATTERN.matcher(cs.gltfKey()).matches()) {
                    uri = cs.gltfKey();
                } else {
                    String storageUri = artifacts.storageUriIfConfigured(cs.gltfKey());
                    if (storageUri != null && S3_URI_PATTERN.matcher(storageUri).matches()) {
                        uri = storageUri;
                    }
                }
                if (uri != null) {
                    ObjectNode prevScene = input.putObject("previous_scene");
                    prevScene.put("scene_version", cs.sceneVersion());
                    prevScene.put("calibration_version", cs.calibrationVersion());
                    prevScene.put("uri", uri);
                    if (sha != null) {
                        prevScene.put("sha256", sha);
                    }
                }
            }
        }
        ArrayNode prevZones = mapper.createArrayNode();
        jdbc.query("""
                SELECT zone_id, ST_AsGeoJSON(polygon) AS polygon
                FROM zone
                WHERE site_id = :site AND valid_to IS NULL
                ORDER BY zone_id
                LIMIT 100
                """, new MapSqlParameterSource("site", siteId), rs -> {
            String zoneId = rs.getString("zone_id");
            if (zoneId == null || zoneId.isBlank() || zoneId.length() > 100) {
                return;
            }
            ObjectNode z = mapper.createObjectNode();
            z.put("zone_id", zoneId);
            ArrayNode poly = z.putArray("polygon_xy");
            Versioned.polygonXy(mapper.readTree(rs.getString("polygon")), poly);
            if (poly.size() >= 3 && poly.size() <= 64) {
                prevZones.add(z);
            }
        });
        if (!prevZones.isEmpty()) {
            input.set("previous_zones", prevZones);
        }
    }

    private void checkWorkUnitCanSetupForUpdate(String siteId, long workUnitId) {
        // 세팅 완료(SetupCompletionService)도 이 행을 잠그고 setup_completed_at 을 쓴다. 잠근 뒤 다시 봐 완료 직후의 새 세션을 막는다
        checkWorkUnitCanSetup(siteId, workUnitId, " FOR UPDATE");
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

    private boolean liveWorkInProgress(String siteId) {
        Boolean exists = jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS')",
                new MapSqlParameterSource("site", siteId), Boolean.class);
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
        List<ViewRow> list = jdbc.query("""
                SELECT setup_session_id, site_id, work_unit_id, retry_of, job_id, status, progress_pct, progress_lease_id,
                       camera_heights_m::text AS camera_heights_m, height_tolerance_cm,
                       background_collection_pct::text AS background_collection_pct,
                       height_checks::text AS height_checks,
                       object_cross_checks::text AS object_cross_checks,
                       pair_holdout::text AS pair_holdout,
                       quality_gate::text AS quality_gate, camera_check::text AS camera_check,
                       calibration_version, preview_scene_version, scene_version, config_version, zone_drafts_ref,
                       failure_code, failure_message, bake_skipped, requested_by, started_at, ready_at, completed_at, updated_at,
                       requested_mode, mode, reuse_fallback_reason, world_anchor::text AS world_anchor,
                       zone_height_changes::text AS zone_height_changes,
                       (SELECT w.setup_completed_at FROM work_unit w
                        WHERE w.work_id = setup_session.work_unit_id
                          AND w.active_setup_session_id = setup_session.setup_session_id) AS setup_completed_at,
                       CASE WHEN setup_session.started_at < COALESCE(
                            (SELECT installed_on FROM flyway_schema_history WHERE version = '47' AND success), now()) THEN
                       (SELECT min(t) FROM (
                            SELECT z.valid_from AS t FROM zone z WHERE z.site_id = setup_session.site_id
                            UNION ALL
                            SELECT z.valid_to FROM zone z WHERE z.site_id = setup_session.site_id
                            UNION ALL
                            SELECT o.valid_from FROM static_object o WHERE o.site_id = setup_session.site_id
                            UNION ALL
                            SELECT o.valid_to FROM static_object o WHERE o.site_id = setup_session.site_id
                        ) changes WHERE t >= setup_session.started_at)
                       ELSE (SELECT min(b.issued_at) FROM setup_session_zone sz
                            JOIN config_bundle_item i ON i.item_type = 'ZONE' AND i.item_id = sz.zone_id
                                AND i.item_version >= sz.zone_version
                            JOIN config_bundle b ON b.config_version = i.config_version AND b.site_id = setup_session.site_id
                            WHERE sz.setup_session_id = setup_session.setup_session_id)
                       END AS zones_committed_at
                FROM setup_session
                WHERE site_id = :site AND setup_session_id = :id
                """, new MapSqlParameterSource().addValue("site", siteId).addValue("id", setupSessionId),
                (rs, i) -> new ViewRow(view(rs), rs.getString("status"), rs.getString("job_id"),
                        rs.getObject("progress_lease_id", UUID.class), rs.getString("background_collection_pct")));
        if (list.isEmpty()) {
            throw new BusinessException(ErrorCode.Setup.SETUP_SESSION_NOT_FOUND);
        }
        ViewRow row = list.getFirst();
        ObjectNode out = row.view();
        // 왜 기다리는지(S15P31A101-583). 표시용이며 진행 처리에 쓰지 않는다
        SetupWaitReason.Reason reason = null;
        Instant now = clock.instant();
        if (SetupWaitReason.mayWait(row.status())) {
            reason = SetupWaitReason.derive(
                    waitReasons.read(row.status(), row.progressLeaseId(), row.backgroundCollectionJson(), row.jobId(), now), now);
        }
        if (reason == null) {
            out.putNull("wait_reason");
        } else {
            out.set("wait_reason", reason.toJson(mapper, now));
        }
        JsonNode calibration = out.path("calibration_version");
        out.set("cameras", cameraSummary(calibration.isString() ? calibration.asString() : null));
        // 맵 보강 촬영 파일·다시 굽기 상태(S15P31A101-607). 계약 스키마 없는 조회 응답이라 -602 cameras·pair_holdout 처럼 최상위 객체로 싣는다
        out.set("supplement", supplements.view(siteId, setupSessionId));
        return out;
    }

    /**
     * 이 세션 보정 문서의 카메라별 요약(S15P31A101-602). 문서는 JOINT_BA_AND_VERIFY(65%) 하트비트의 artifacts 로 등록되고
     * 세션 calibration_version 이 그때 채워진다. 그 전이거나 문서가 없으면 null(지어내지 않음).
     */
    private JsonNode cameraSummary(String calibrationVersion) {
        if (calibrationVersion == null) {
            return mapper.nullNode();
        }
        List<byte[]> docs = jdbc.queryForList("SELECT content FROM calibration_document WHERE calibration_version = :v",
                new MapSqlParameterSource("v", calibrationVersion), byte[].class);
        return docs.isEmpty() ? mapper.nullNode() : SetupCameraSummary.of(mapper, docs.getFirst());
    }

    private record ViewRow(ObjectNode view, String status, String jobId, UUID progressLeaseId,
            String backgroundCollectionJson) {
    }

    private SessionRow findForUpdateOrView(String siteId, UUID setupSessionId, boolean forUpdate) {
        String sql = """
                SELECT setup_session_id, site_id, work_unit_id, progress_lease_id, status, progress_pct,
                       camera_heights_m::text AS camera_heights_m, height_tolerance_cm,
                       background_collection_pct::text AS background_collection_pct,
                       height_checks::text AS height_checks,
                       object_cross_checks::text AS object_cross_checks,
                       calibration_version, preview_scene_version, scene_version, config_version, zone_drafts_ref,
                       ready_at, requested_by, requested_mode, mode
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
                       ready_at, requested_by, requested_mode, mode
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
                rs.getObject("ready_at", OffsetDateTime.class),
                rs.getObject("requested_by", Long.class),
                rs.getString("requested_mode"),
                rs.getString("mode"));
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

        // 쌍별 홀드아웃 오차(S15P31A101-602). 마지막으로 값을 실은 스냅샷 것, 받은 적 없으면 null
        String pairHoldoutRaw = rs.getString("pair_holdout");
        out.set("pair_holdout", pairHoldoutRaw == null ? mapper.nullNode() : mapper.readTree(pairHoldoutRaw));

        // 보정 품질 게이트·카메라 확인(S15P31A101-646). 마지막으로 값을 실은 스냅샷 것, 받은 적 없으면 null.
        // camera_check_passed 는 구역 단계·세팅 완료 관문과 같은 판단(PASSED 이고 그 확인의 보정이 세션 보정과 같다)
        String qualityGateRaw = rs.getString("quality_gate");
        out.set("quality_gate", qualityGateRaw == null ? mapper.nullNode() : mapper.readTree(qualityGateRaw));
        String cameraCheckRaw = rs.getString("camera_check");
        JsonNode cameraCheck = cameraCheckRaw == null ? mapper.nullNode() : mapper.readTree(cameraCheckRaw);
        out.set("camera_check", cameraCheck);
        out.put("camera_check_passed", SetupCameraCheck.passed(cameraCheck.path("status").asString(null),
                cameraCheck.path("calibration_version").asString(null), rs.getString("calibration_version")));

        putText(out, "calibration_version", rs.getString("calibration_version"));
        putText(out, "preview_scene_version", rs.getString("preview_scene_version"));
        putText(out, "scene_version", rs.getString("scene_version"));
        putText(out, "config_version", rs.getString("config_version"));
        putText(out, "zone_drafts_ref", rs.getString("zone_drafts_ref"));
        putText(out, "failure_code", rs.getString("failure_code"));
        putText(out, "failure_message", rs.getString("failure_message"));
        // 작업 시작으로 정식 굽기를 회수한 COMPLETED(site_setup 1.7.0, S15P31A101-597). 그 밖에는 null
        putText(out, "bake_skipped", rs.getString("bake_skipped"));
        // 세팅 모드(-614): mode 는 실제로 따른 모드(NEW|REUSE), requested_mode 는 요청한 모드. REUSE 요청이 새 모델로 이어 가면
        // mode=NEW 이고 reuse_fallback_reason 에 World 다시 맞추기 거부 사유가 온다. world_anchor 는 그 판정 요약(받기 전 null)
        out.put("mode", rs.getString("mode"));
        out.put("requested_mode", rs.getString("requested_mode"));
        putText(out, "reuse_fallback_reason", rs.getString("reuse_fallback_reason"));
        SetupReuseMode.putWorldAnchor(mapper, out, rs.getString("world_anchor"));
        String zoneHeightChangesRaw = rs.getString("zone_height_changes");
        out.set("zone_height_changes",
                zoneHeightChangesRaw == null ? mapper.nullNode() : mapper.readTree(zoneHeightChangesRaw));

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
        // 세팅 완료(C17, S15P31A101-615) 시각. 이 세션으로 완료한 작업이면 그 work_unit.setup_completed_at, 아니면 null
        var setupCompletedAt = rs.getTimestamp("setup_completed_at");
        if (setupCompletedAt == null) {
            out.putNull("setup_completed_at_ms");
        } else {
            out.put("setup_completed_at_ms", setupCompletedAt.getTime());
        }
        // 새 세션은 해당 세션에서 만든 구역이 발행된 시각. 마이그레이션 이전 세션은 기존 표시를 유지한다.
        var zonesCommittedAt = rs.getTimestamp("zones_committed_at");
        if (zonesCommittedAt == null) {
            out.putNull("zones_committed_at");
        } else {
            out.put("zones_committed_at", zonesCommittedAt.getTime());
        }
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
            OffsetDateTime readyAt,
            Long requestedBy,
            String requestedMode,
            String mode) {
    }
}
