package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobStateChanged;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Types;
import java.time.Clock;
import java.time.Duration;
import java.time.OffsetDateTime;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.event.EventListener;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 3D 배경(Scene) 재생성 요청·상태 전이 서비스(S15P31A101-428).
 *
 * <p>관리자가 {@code POST /api/scenes/regenerate} 를 호출하면 {@code scene_regenerate_request} 행을
 * {@code REQUESTED} 로 만들고 {@code analysis_job(job_type = "scene_regenerate", priority = BACKGROUND)} 을 큐에 넣는다.
 * 이후 워커 대여({@code CAPTURING}) → 정적 재구성 제출 또는 콜백 성공({@code RECONSTRUCTING}) →
 * 후보 Scene 생성({@code CANDIDATE_READY}) 또는 실패({@code FAILED})로 전이한다.
 * 워커가 10분 안에 대여하지 않으면 {@code FAILED(TIMEOUT)} 처리한다.
 */
@Service
public class SceneRegenerateService {

    public static final String JOB_TYPE = AnalysisJobService.SCENE_REGENERATE_JOB_TYPE;
    public static final int MAX_REASON_LENGTH = 120;
    public static final Set<String> VALID_FAILURE_REASONS = Set.of(
            "NO_WORKER",
            "TIER0_NOT_RUNNING",
            "BACKGROUND_NOT_READY",
            "UPLOAD_FAILED",
            "RECONSTRUCT_FAILED",
            "TIMEOUT");

    private final NamedParameterJdbcTemplate jdbc;
    private final AnalysisJobService jobs;
    private final ObjectMapper mapper;
    private final Clock clock;
    private final int leaseTimeoutMs;

    public SceneRegenerateService(
            NamedParameterJdbcTemplate jdbc,
            AnalysisJobService jobs,
            ObjectMapper mapper,
            Clock clock,
            @Value("${simai.scene.regenerate-timeout-ms:600000}") int leaseTimeoutMs) {
        this.jdbc = jdbc;
        this.jobs = jobs;
        this.mapper = mapper;
        this.clock = clock;
        this.leaseTimeoutMs = leaseTimeoutMs;
    }

    @Transactional
    public ObjectNode requestRegenerate(long userId, JsonNode body) {
        String reason = parseReason(body);
        expireTimedOutRequests();

        List<Long> active = jdbc.queryForList("""
                SELECT id FROM scene_regenerate_request
                WHERE status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                ORDER BY id DESC LIMIT 1
                """, Map.of(), Long.class);
        if (!active.isEmpty()) {
            throw new SceneRegenerateInProgressException(active.getFirst());
        }

        Boolean liveWork = jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM work_unit WHERE status = 'IN_PROGRESS')", Map.of(), Boolean.class);
        if (Boolean.TRUE.equals(liveWork)) {
            throw new BusinessException(ErrorCode.Scene.REGENERATE_WORK_IN_PROGRESS);
        }

        OffsetDateTime now = OffsetDateTime.now(clock);
        // 동시 요청은 진행 중 유일 색인에서 갈린다. 위반을 catch 하면 PostgreSQL 트랜잭션이 중단돼 뒤 조회가 실패하므로(500)
        // 충돌은 ON CONFLICT 로 받아 빈 결과로 보고, 먼저 들어간 요청을 새 문장으로 읽어 409 로 답한다
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("status", "REQUESTED")
                .addValue("reason", reason, Types.VARCHAR)
                .addValue("requestedBy", userId)
                .addValue("now", now);
        List<Long> inserted = List.of();
        // 충돌 뒤 조회 사이에 먼저 들어간 요청이 끝나면 진행 중 요청이 안 보인다. 그때는 한 번 더 넣어 본다
        for (int attempt = 0; attempt < 2 && inserted.isEmpty(); attempt++) {
            inserted = jdbc.queryForList("""
                    INSERT INTO scene_regenerate_request (status, reason, requested_by, created_at, updated_at)
                    VALUES (:status, :reason, :requestedBy, :now, :now)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """, params, Long.class);
            if (inserted.isEmpty()) {
                List<Long> current = jdbc.queryForList("""
                        SELECT id FROM scene_regenerate_request
                        WHERE status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                        ORDER BY id DESC LIMIT 1
                        """, Map.of(), Long.class);
                if (!current.isEmpty()) {
                    throw new SceneRegenerateInProgressException(current.getFirst());
                }
            }
        }
        if (inserted.isEmpty()) {
            // 두 번 다 다른 요청과 엇갈렸다(극히 드묾). 진행 중 요청 id 는 모르지만 같은 409 로 다시 누르게 한다
            throw new SceneRegenerateInProgressException(0L);
        }
        long requestId = inserted.getFirst();

        ObjectNode input = mapper.createObjectNode()
                .put("request_id", requestId)
                .put("requested_at_ms", now.toInstant().toEpochMilli());
        if (reason != null) {
            input.put("reason", reason);
        }
        String jobId = jobs.enqueue(new JobSpec(JOB_TYPE, input, 1, leaseTimeoutMs, null, userId));
        jdbc.update("""
                UPDATE scene_regenerate_request
                SET job_id = :jobId
                WHERE id = :id
                """, Map.of("jobId", jobId, "id", requestId));

        return mapper.createObjectNode()
                .put("request_id", requestId)
                .put("status", "REQUESTED");
    }

    @Transactional
    public ObjectNode getById(long requestId) {
        if (requestId < 1) {
            throw new BusinessException(ErrorCode.Scene.REGENERATE_NOT_FOUND);
        }
        expireTimedOutRequests();
        List<Row> rows = jdbc.query("""
                SELECT id, status, failure_reason, reason, scene_version, created_at, updated_at
                FROM scene_regenerate_request
                WHERE id = :id
                """, Map.of("id", requestId), (rs, i) -> mapRow(
                rs.getLong("id"),
                rs.getString("status"),
                rs.getString("failure_reason"),
                rs.getString("reason"),
                rs.getString("scene_version"),
                rs.getObject("created_at", OffsetDateTime.class),
                rs.getObject("updated_at", OffsetDateTime.class)));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Scene.REGENERATE_NOT_FOUND);
        }
        return toJson(rows.getFirst());
    }

    @Transactional
    public Optional<ObjectNode> findLatest() {
        expireTimedOutRequests();
        List<Row> rows = jdbc.query("""
                SELECT id, status, failure_reason, reason, scene_version, created_at, updated_at
                FROM scene_regenerate_request
                ORDER BY id DESC
                LIMIT 1
                """, Map.of(), (rs, i) -> mapRow(
                rs.getLong("id"),
                rs.getString("status"),
                rs.getString("failure_reason"),
                rs.getString("reason"),
                rs.getString("scene_version"),
                rs.getObject("created_at", OffsetDateTime.class),
                rs.getObject("updated_at", OffsetDateTime.class)));
        return rows.isEmpty() ? Optional.empty() : Optional.of(toJson(rows.getFirst()));
    }

    /**
     * 워커가 하나도 등록·온라인이 아니어도 처음에는 REQUESTED 로 두되,
     * 10분(leaseTimeoutMs) 안에 대여되지 않으면 FAILED(TIMEOUT) 으로 전이한다.
     */
    @Transactional
    public void expireTimedOutRequests() {
        OffsetDateTime now = OffsetDateTime.now(clock);
        OffsetDateTime cutoff = now.minus(Duration.ofMillis(leaseTimeoutMs));
        List<String> jobIds = jdbc.queryForList("""
                UPDATE scene_regenerate_request
                SET status = 'FAILED', failure_reason = 'TIMEOUT', updated_at = :now
                WHERE status = 'REQUESTED' AND created_at <= :cutoff
                RETURNING job_id
                """, Map.of("now", now, "cutoff", cutoff), String.class);
        for (String jobId : jobIds) {
            if (jobId != null) {
                jdbc.update("""
                        UPDATE analysis_job
                        SET status = 'TIMED_OUT', finished_at = :now
                        WHERE job_id = :jobId AND status = 'QUEUED'
                        """, Map.of("now", now, "jobId", jobId));
            }
        }
    }

    @EventListener
    @Transactional
    public void onJobStateChanged(JobStateChanged event) {
        if (!JOB_TYPE.equals(event.jobType()) || !"LEASED".equals(event.status())) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(clock);
        jdbc.update("""
                UPDATE scene_regenerate_request
                SET status = 'CAPTURING', updated_at = :now
                WHERE job_id = :jobId AND status = 'REQUESTED'
                """, Map.of("now", now, "jobId", event.jobId()));
    }

    @Transactional
    public void onStaticReconstructSubmitted(long requestId, String staticReconstructJobId) {
        if (requestId < 1 || staticReconstructJobId == null || staticReconstructJobId.isBlank()) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(clock);
        int updated = jdbc.update("""
                UPDATE scene_regenerate_request
                SET static_reconstruct_job_id = :reconstructJobId,
                    status = CASE WHEN status IN ('REQUESTED', 'CAPTURING') THEN 'RECONSTRUCTING' ELSE status END,
                    updated_at = :now
                WHERE id = :id AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                """, Map.of("reconstructJobId", staticReconstructJobId, "now", now, "id", requestId));
        if (updated > 0) {
            syncExistingCandidateIfReady(staticReconstructJobId);
        }
    }

    @EventListener
    @Transactional
    public void onJobFinished(JobFinished event) {
        if (!JOB_TYPE.equals(event.jobType())) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(clock);
        if ("SUCCEEDED".equals(event.status())) {
            String reconstructJobId = event.result().path("static_reconstruct_job_id").asString();
            String bundleId = event.result().path("bundle_id").asString();
            MapSqlParameterSource params = new MapSqlParameterSource()
                    .addValue("reconstructJobId", reconstructJobId)
                    .addValue("bundleId", bundleId)
                    .addValue("now", now)
                    .addValue("jobId", event.jobId());
            // 제출된 작업과 묶음이 콜백에 일치해야 다른 재구성 결과를 연결하지 않는다.
            Integer matches = jdbc.queryForObject("""
                    SELECT count(*) FROM analysis_job
                    WHERE job_id = :reconstructJobId AND job_type = 'static_reconstruct'
                      AND input->>'clean_background_bundle_id' = :bundleId
                    """, params, Integer.class);
            if (matches == null || matches == 0) {
                throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
            }
            int updated = jdbc.update("""
                    UPDATE scene_regenerate_request
                    SET static_reconstruct_job_id = :reconstructJobId,
                        status = CASE WHEN status IN ('REQUESTED', 'CAPTURING') THEN 'RECONSTRUCTING' ELSE status END,
                        updated_at = :now
                    WHERE job_id = :jobId AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                      AND (static_reconstruct_job_id IS NULL OR static_reconstruct_job_id = :reconstructJobId)
                    """, params);
            if (updated > 0) {
                syncExistingCandidateIfReady(reconstructJobId);
            } else if (!jdbc.queryForList("""
                    SELECT id FROM scene_regenerate_request
                    WHERE job_id = :jobId AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                    """, params, Long.class).isEmpty()) {
                throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
            }
            return;
        }

        String failureReason = "TIMED_OUT".equals(event.status())
                ? "TIMEOUT"
                : resolveFailureReason(event.error(), "UPLOAD_FAILED");
        jdbc.update("""
                UPDATE scene_regenerate_request
                SET status = 'FAILED', failure_reason = :failureReason, updated_at = :now
                WHERE job_id = :jobId AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                """, Map.of("failureReason", failureReason, "now", now, "jobId", event.jobId()));
    }

    @Transactional
    public void onStaticReconstructCandidateReady(String staticReconstructJobId, String sceneVersion) {
        if (staticReconstructJobId == null || sceneVersion == null) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(clock);
        jdbc.update("""
                UPDATE scene_regenerate_request
                SET status = 'CANDIDATE_READY', scene_version = :sceneVersion, failure_reason = NULL, updated_at = :now
                WHERE static_reconstruct_job_id = :jobId AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                """, Map.of("sceneVersion", sceneVersion, "now", now, "jobId", staticReconstructJobId));
    }

    @Transactional
    public void onStaticReconstructFailed(String staticReconstructJobId, String status, JsonNode error) {
        if (staticReconstructJobId == null) {
            return;
        }
        OffsetDateTime now = OffsetDateTime.now(clock);
        String failureReason = "TIMED_OUT".equals(status)
                ? "TIMEOUT"
                : resolveFailureReason(error, "RECONSTRUCT_FAILED");
        jdbc.update("""
                UPDATE scene_regenerate_request
                SET status = 'FAILED', failure_reason = :failureReason, updated_at = :now
                WHERE static_reconstruct_job_id = :jobId AND status IN ('REQUESTED', 'CAPTURING', 'RECONSTRUCTING')
                """, Map.of("failureReason", failureReason, "now", now, "jobId", staticReconstructJobId));
    }

    private void syncExistingCandidateIfReady(String staticReconstructJobId) {
        List<String> versions = jdbc.queryForList("""
                SELECT scene_version FROM scene_version WHERE job_id = :jobId LIMIT 1
                """, Map.of("jobId", staticReconstructJobId), String.class);
        if (!versions.isEmpty()) {
            onStaticReconstructCandidateReady(staticReconstructJobId, versions.getFirst());
        }
    }

    private String resolveFailureReason(JsonNode error, String fallback) {
        if (error != null && error.isObject()) {
            String code = error.path("code").asString(null);
            if (code != null && VALID_FAILURE_REASONS.contains(code)) {
                return code;
            }
        }
        return fallback;
    }

    private String parseReason(JsonNode body) {
        if (body == null || body.isNull() || body.isMissingNode()) {
            return null;
        }
        if (!body.isObject()) {
            throw new BusinessException(ErrorCode.Scene.INVALID_REGENERATE_REQUEST);
        }
        Iterator<String> names = body.propertyNames().iterator();
        while (names.hasNext()) {
            String name = names.next();
            if (!"reason".equals(name)) {
                throw new BusinessException(ErrorCode.Scene.INVALID_REGENERATE_REQUEST);
            }
        }
        JsonNode reasonNode = body.get("reason");
        if (reasonNode == null || reasonNode.isNull()) {
            return null;
        }
        if (!reasonNode.isString()) {
            throw new BusinessException(ErrorCode.Scene.INVALID_REGENERATE_REQUEST);
        }
        String stripped = reasonNode.asString().strip();
        if (stripped.length() > MAX_REASON_LENGTH) {
            throw new BusinessException(ErrorCode.Scene.INVALID_REGENERATE_REQUEST);
        }
        return stripped.isEmpty() ? null : stripped;
    }

    private Row mapRow(
            long id,
            String status,
            String failureReason,
            String reason,
            String sceneVersion,
            OffsetDateTime createdAt,
            OffsetDateTime updatedAt) {
        return new Row(id, status, failureReason, reason, sceneVersion, createdAt, updatedAt);
    }

    private ObjectNode toJson(Row row) {
        ObjectNode node = mapper.createObjectNode();
        node.put("request_id", row.id());
        node.put("status", row.status());
        if (row.failureReason() != null) {
            node.put("failure_reason", row.failureReason());
        } else {
            node.putNull("failure_reason");
        }
        if (row.sceneVersion() != null) {
            node.put("scene_version", row.sceneVersion());
        } else {
            node.putNull("scene_version");
        }
        if (row.reason() != null) {
            node.put("reason", row.reason());
        } else {
            node.putNull("reason");
        }
        node.put("requested_at_ms", row.createdAt().toInstant().toEpochMilli());
        node.put("updated_at_ms", row.updatedAt().toInstant().toEpochMilli());
        return node;
    }

    private record Row(
            long id,
            String status,
            String failureReason,
            String reason,
            String sceneVersion,
            OffsetDateTime createdAt,
            OffsetDateTime updatedAt) {
    }
}
