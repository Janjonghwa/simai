package com.ssafy.simai.domain.floorplan;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Pattern;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * BEV 평면도(명세 4.1, 12.5, 12.6, FR-22, S15P31A101-240). 평면도는 보조 화면이다. 좌표·스케일의 기준이 아니고 안전 판단에 쓰지 않는다.
 * <ul>
 * <li>프레임: 관리자가 카메라별 사람 없는 프레임(s3://) 묶음을 관리 명령으로 등록한다(bev_frame_set, 가장 늦은 묶음을 쓴다).</li>
 * <li>등록: 현재 보정(가장 늦은 calibration_update)이 CALIBRATED 이고 (보정 버전, 최신 프레임 묶음)의 평면도가 아직 없으면
 * bev_floorplan 작업을 한 번 넣고 floor_plan 을 PENDING 으로 만든다. 대조기(FloorPlanScheduler)와 관리 명령이 부른다.</li>
 * <li>결과: 작업이 끝나면(JobFinished) READY(이미지 키·결과 원문) 또는 FAILED 로 바꾼다.</li>
 * </ul>
 * 같은 (보정 버전, 묶음)은 FAILED 여도 다시 넣지 않는다(실패를 되풀이하지 않게). 다시 만들려면 프레임을 새로 등록한다.
 */
@Service
public class FloorPlanService {

    static final String JOB_TYPE = "bev_floorplan";
    private static final Logger log = LoggerFactory.getLogger(FloorPlanService.class);
    private static final Set<String> FRAME_SOURCES = Set.of("LIVE", "FILE", "SIM", "DATASET");
    private static final Pattern CAMERA_ID = Pattern.compile("^[A-Za-z0-9_-]+$");
    private static final Pattern OBJECT_URI = Pattern.compile("^s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/\\S+$");
    private static final int MAX_URI_LENGTH = 1093;
    /** CPU 로 수십 초. 시도 둘이면 일시 저장소 오류 한 번을 넘긴다 */
    private static final int MAX_ATTEMPTS = 2;
    private static final int TIMEOUT_MS = 600_000;

    private final NamedParameterJdbcTemplate jdbc;
    private final AnalysisJobService jobs;
    private final ObjectStorage storage;
    private final SiteLookup sites;
    private final ObjectMapper mapper;
    private final Clock clock;

    FloorPlanService(NamedParameterJdbcTemplate jdbc, AnalysisJobService jobs, ObjectStorage storage, SiteLookup sites,
            ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.jobs = jobs;
        this.storage = storage;
        this.sites = sites;
        this.mapper = mapper;
        this.clock = clock;
    }

    /** 카메라별 프레임 묶음을 등록하고 바로 대조한다. @return 새 묶음 ID 와 넣은 작업(없으면 빈 값) */
    @Transactional
    public Registered registerFrames(String frameSource, List<Frame> frames) {
        if (!FRAME_SOURCES.contains(frameSource) || frames.isEmpty()) {
            throw new BusinessException(ErrorCode.FloorPlan.INVALID_FRAMES);
        }
        String bucket = storage.uri("");
        Set<String> cameras = new HashSet<>();
        ArrayNode array = mapper.createArrayNode();
        for (Frame frame : frames) {
            // 워커는 같은 카메라 두 장을 거부한다. 워커 서명 API 는 우리 버킷 주소만 내려 준다
            // 계약(object_uri 1093자, 촬영 시각 0 이상)에 맞지 않으면 나중에 작업 등록에서야 실패하므로 지금 거부한다
            if (frame.cameraId() == null || !CAMERA_ID.matcher(frame.cameraId()).matches() || !cameras.add(frame.cameraId())
                    || frame.frameUri() == null || frame.frameUri().length() > MAX_URI_LENGTH
                    || !OBJECT_URI.matcher(frame.frameUri()).matches() || !frame.frameUri().startsWith(bucket)
                    || frame.capturedAtMs() < 0 || frame.uncertaintyMs() < 0) {
                throw new BusinessException(ErrorCode.FloorPlan.INVALID_FRAMES);
            }
            ObjectNode node = array.addObject();
            node.put("camera_id", frame.cameraId());
            node.put("frame_uri", frame.frameUri());
            node.put("estimated_capture_time_ms", frame.capturedAtMs());
            node.put("timestamp_source", frame.timestampSource());
            node.put("timestamp_uncertainty_ms", frame.uncertaintyMs());
        }
        Long setId = jdbc.queryForObject("""
                INSERT INTO bev_frame_set (site_id, frame_source, frames, created_at)
                VALUES (:site, :source, CAST(:frames AS jsonb), :now) RETURNING frame_set_id
                """, new MapSqlParameterSource().addValue("site", sites.siteId()).addValue("source", frameSource)
                .addValue("frames", array.toString()).addValue("now", now()), Long.class);
        return new Registered(setId, reconcile());
    }

    /**
     * 현재 보정이 CALIBRATED 이고 최신 프레임 묶음과의 평면도가 없으면 작업을 넣는다. 여러 번 불러도 한 번만 넣는다
     * (트랜잭션 잠금 + 고유 색인). @return 넣은 작업 ID
     */
    @Transactional
    public Optional<String> reconcile() {
        // 대조기와 관리 명령이 동시에 들어와도 차례로 본다
        jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('floor_plan_reconcile'))", Map.of());
        Optional<Calibration> calibration = currentCalibration();
        if (calibration.isEmpty() || !"CALIBRATED".equals(calibration.get().state())) {
            return Optional.empty();
        }
        Optional<FrameSet> frameSet = latestFrameSet();
        if (frameSet.isEmpty()) {
            return Optional.empty();
        }
        Calibration cal = calibration.get();
        FrameSet set = frameSet.get();
        // 작업을 넣은 적이 있으면(성공·실패 무관) 다시 넣지 않는다. 넣지 못한 실패(job_id 없음)는 설정을 고치면 다시 본다
        Integer existing = jdbc.queryForObject("""
                SELECT count(*) FROM floor_plan WHERE calibration_version = :v AND frame_set_id = :set AND job_id IS NOT NULL
                """, new MapSqlParameterSource().addValue("v", cal.version()).addValue("set", set.id()), Integer.class);
        if (existing != null && existing > 0) {
            return Optional.empty();
        }
        // 워커는 우리 버킷 주소만 서명받는다. 다른 곳의 보정 문서면 작업이 반드시 실패하므로 넣지 않는다
        if (cal.ref() == null || !cal.ref().startsWith(storage.uri(""))) {
            notEnqueued(cal.version(), set.id(), "CALIBRATION_REF_NOT_IN_BUCKET");
            return Optional.empty();
        }
        ObjectNode input = mapper.createObjectNode();
        input.put("calibration_version", cal.version());
        input.put("calibration_ref", cal.ref());
        input.put("calibration_hash", cal.hash());
        input.put("frame_source", set.source());
        input.set("frames", set.frames());
        String jobId;
        try {
            jobId = jobs.enqueue(new JobSpec(JOB_TYPE, input, MAX_ATTEMPTS, TIMEOUT_MS, null, null));
        } catch (BusinessException e) {
            // 계약 검사에 걸린 입력(예: 보정 해시 형식)
            notEnqueued(cal.version(), set.id(), e.getErrorCode().name());
            return Optional.empty();
        }
        // 이전 보정 버전으로 아직 도는 평면도 작업은 쓸모가 없다. 워커 시간을 쓰지 않게 취소한다(JobFinished 로 FAILED·CANCELLED)
        for (String stale : jdbc.queryForList("""
                SELECT job_id FROM floor_plan WHERE status = 'PENDING' AND calibration_version <> :v AND job_id IS NOT NULL
                """, new MapSqlParameterSource("v", cal.version()), String.class)) {
            jobs.cancel(stale);
        }
        jdbc.update("""
                DELETE FROM floor_plan WHERE calibration_version = :v AND frame_set_id = :set AND job_id IS NULL AND status = 'FAILED'
                """, new MapSqlParameterSource().addValue("v", cal.version()).addValue("set", set.id()));
        insertPlan(cal.version(), set.id(), jobId, "PENDING", null);
        log.info("BEV 평면도 작업 {} 을 넣었다(보정 {}, 프레임 묶음 {})", jobId, cal.version(), set.id());
        return Optional.of(jobId);
    }

    /** 작업 결과를 floor_plan 으로 옮긴다. 결과는 계약 검사를 마친 job_callback result 다 */
    @EventListener
    public void onJobFinished(JobFinished event) {
        if (!JOB_TYPE.equals(event.jobType())) {
            return;
        }
        String status = "FAILED";
        String imageKey = null;
        // 워커 실패 코드(예: CALIBRATION_HASH_MISMATCH)가 있으면 그것, 없으면 작업 상태(TIMED_OUT·CANCELLED)
        String error = event.error() != null && event.error().path("code").isString()
                ? event.error().get("code").stringValue() : event.status();
        if ("SUCCEEDED".equals(event.status()) && event.result() != null) {
            String imageUri = event.result().path("image_uri").stringValue("");
            // 이 작업의 출력 자리(워커 서명 API 가 정한 floor-plans/{job_id}/…)만 받는다. 조회는 열람 권한으로 이 키를 서명하므로
            // 다른 객체(클립·보정 문서)를 가리키는 결과를 받으면 버킷의 아무 객체나 내주게 된다
            String outputs = storage.uri("floor-plans/" + event.jobId() + "/");
            if (imageUri.startsWith(outputs) && imageUri.length() > outputs.length()) {
                status = "READY";
                imageKey = imageUri.substring(storage.uri("").length());
                error = null;
            } else {
                error = "IMAGE_NOT_IN_JOB_OUTPUT";
            }
        }
        List<Long> updated = jdbc.queryForList("""
                UPDATE floor_plan SET status = :status, image_key = :key, result = CAST(:result AS jsonb), error = :error,
                    finished_at = :now
                WHERE job_id = :jobId AND status = 'PENDING' RETURNING floor_plan_id
                """, new MapSqlParameterSource().addValue("jobId", event.jobId()).addValue("status", status)
                .addValue("key", imageKey).addValue("error", error).addValue("now", now())
                .addValue("result", event.result() == null ? null : event.result().toString()), Long.class);
        if (updated.isEmpty()) {
            return;
        }
        // 현재 보정 버전의 평면도일 때만 현장의 현재 평면도로 둔다(늦게 끝난 옛 버전 작업이 덮어쓰지 않게)
        String current = currentCalibration().map(Calibration::version).orElse(null);
        if ("READY".equals(status) && current != null) {
            jdbc.update("""
                    UPDATE site SET current_floor_plan_id = f.floor_plan_id FROM floor_plan f
                    WHERE f.floor_plan_id = :id AND site.site_id = f.site_id AND f.calibration_version = :v
                    """, new MapSqlParameterSource().addValue("id", updated.getFirst()).addValue("v", current));
        }
    }

    /** 작업을 넣지 못했다. 같은 이유가 이어지면 다시 남기지 않는다(대조가 10초마다 돈다). 설정을 고치면 다음 대조가 넣는다 */
    private void notEnqueued(String version, long setId, String error) {
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("v", version).addValue("set", setId)
                .addValue("error", error);
        List<String> previous = jdbc.queryForList("""
                SELECT error FROM floor_plan WHERE calibration_version = :v AND frame_set_id = :set AND job_id IS NULL AND status = 'FAILED'
                """, params, String.class);
        if (previous.isEmpty()) {
            insertPlan(version, setId, null, "FAILED", error);
        } else if (!error.equals(previous.getFirst())) {
            jdbc.update("""
                    UPDATE floor_plan SET error = :error WHERE calibration_version = :v AND frame_set_id = :set AND job_id IS NULL
                    """, params);
        } else {
            return;
        }
        log.warn("보정 {}·프레임 묶음 {} 의 BEV 평면도 작업을 넣지 못했다: {}", version, setId, error);
    }

    /**
     * GET /api/floor-plans/current. 현재 보정 버전의 READY 평면도가 있으면 그것, 없으면 가장 최근 READY 평면도를 stale=true 로 준다
     * (보정이 바뀌어 새로 만드는 중에도 화면이 비지 않게). READY 가 하나도 없으면 404.
     */
    @Transactional(readOnly = true)
    public ObjectNode current() {
        String version = currentCalibration().map(Calibration::version).orElse(null);
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT floor_plan_id, calibration_version, image_key, result::text AS result, finished_at FROM floor_plan
                WHERE status = 'READY'
                ORDER BY (calibration_version IS NOT DISTINCT FROM :v) DESC, finished_at DESC, floor_plan_id DESC LIMIT 1
                """, new MapSqlParameterSource("v", version));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.FloorPlan.FLOOR_PLAN_NOT_READY);
        }
        Map<String, Object> row = rows.getFirst();
        JsonNode result = mapper.readTree((String) row.get("result"));
        ObjectStorage.SignedRequest image = storage.signGet((String) row.get("image_key"));
        String planVersion = (String) row.get("calibration_version");
        ObjectNode node = mapper.createObjectNode();
        node.put("floor_plan_id", ((Number) row.get("floor_plan_id")).longValue());
        node.put("calibration_version", planVersion);
        node.put("current_calibration_version", version);
        // 이 평면도가 현재 보정 버전으로 만든 것이 아니다(새로 만드는 중이거나 프레임이 없다)
        node.put("stale", !planVersion.equals(version));
        node.put("pending", version != null && jdbc.queryForObject(
                "SELECT count(*) FROM floor_plan WHERE calibration_version = :v AND status = 'PENDING'",
                new MapSqlParameterSource("v", version), Integer.class) > 0);
        node.put("image_url", image.url());
        node.put("image_url_expires_at_ms", image.expiresAt().toEpochMilli());
        for (String field : new String[] {"content_type", "image_sha256", "scene_version", "created_at", "purpose", "safety_use",
                "coordinate_frame", "raster", "cameras_used", "cameras_skipped", "coverage_ratio", "room_outline"}) {
            node.set(field, result.path(field).isMissingNode() ? mapper.nullNode() : result.get(field));
        }
        node.put("generated_at", ((Timestamp) row.get("finished_at")).toInstant().toEpochMilli());
        return node;
    }

    private void insertPlan(String version, long setId, String jobId, String status, String error) {
        jdbc.update("""
                INSERT INTO floor_plan (site_id, calibration_version, frame_set_id, job_id, status, error, created_at)
                VALUES (:site, :v, :set, :job, :status, :error, :now)
                """, new MapSqlParameterSource().addValue("site", sites.siteId()).addValue("v", version).addValue("set", setId)
                .addValue("job", jobId).addValue("status", status).addValue("error", error).addValue("now", now()));
    }

    /** 현재 보정 = Tier 0 가 가장 늦게 알린 calibration_update(CalibrationViews 와 같은 순서) */
    private Optional<Calibration> currentCalibration() {
        return jdbc.query("""
                SELECT calibration_version, calibration_state, payload->>'calibration_ref' AS ref, payload->>'calibration_hash' AS hash
                FROM calibration_update_message
                ORDER BY LEAST(sent_at, received_at) DESC, received_at DESC LIMIT 1
                """, Map.of(), (rs, i) -> new Calibration(rs.getString("calibration_version"), rs.getString("calibration_state"),
                rs.getString("ref"), rs.getString("hash"))).stream().findFirst();
    }

    private Optional<FrameSet> latestFrameSet() {
        return jdbc.query("""
                SELECT frame_set_id, frame_source, frames::text AS frames FROM bev_frame_set
                ORDER BY created_at DESC, frame_set_id DESC LIMIT 1
                """, Map.of(), (rs, i) -> new FrameSet(rs.getLong("frame_set_id"), rs.getString("frame_source"),
                mapper.readTree(rs.getString("frames")))).stream().findFirst();
    }

    private Timestamp now() {
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        return Timestamp.from(now);
    }

    /** 카메라 하나의 사람 없는 프레임. 촬영 시각 세 값은 계약 capture_time 이다 */
    public record Frame(String cameraId, String frameUri, long capturedAtMs, String timestampSource, long uncertaintyMs) {
    }

    public record Registered(long frameSetId, Optional<String> jobId) {
    }

    private record Calibration(String version, String state, String ref, String hash) {
    }

    private record FrameSet(long id, String source, JsonNode frames) {
    }
}
