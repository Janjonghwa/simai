package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.job.AnalysisJobService;
import com.ssafy.simai.domain.job.AnalysisJobService.JobSpec;
import com.ssafy.simai.domain.job.JobFinished;
import com.ssafy.simai.domain.job.JobStateChanged;
import com.ssafy.simai.domain.tier0.event.RiskEventsDelivered;
import com.ssafy.simai.domain.tier0.i1.I1Json;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import org.springframework.context.ApplicationEventPublisher;
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
 * 사고 구간 VLM 사후 요약 요청·상태 전이·콜백 반영(명세 5.2·5.6·12.5·12.6, S15P31A101-369).
 * <ul>
 *   <li>요청: LIVE 이벤트이고 삭제되지 않은 클립이 하나 이상 있을 때 REPORT 우선순위 incident_summary 작업을 만든다.
 *       이미 진행 중인 요약(QUEUED·RUNNING)이 있으면 새 작업을 만들지 않고 그 항목을 그대로 돌려준다.</li>
 *   <li>상태·결과: 작업 상태가 바뀌거나(QUEUED ↔ RUNNING → SUCCEEDED / FAILED / TIMED_OUT) 결과가 저장되면
 *       incident_summary 행을 갱신하고 이벤트를 다시 발행한다(RiskEventsDelivered).</li>
 *   <li>불변 조건: 사후 요약은 이벤트 등급(severity)이나 상향 의견(proposed_severity)을 바꾸지 않으며(AGENTS.md 4),
 *       완료된 결과는 덮어쓰지 않고 요청마다 새 행을 추가한다(AGENTS.md 6).</li>
 * </ul>
 */
@Service
public class IncidentSummaryService {

    static final String JOB_TYPE = "incident_summary";
    static final int TIMEOUT_MS = 120_000;
    static final int MAX_ATTEMPTS = 2;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final ObjectStorage storage;
    private final AnalysisJobService jobs;
    private final ApplicationEventPublisher publisher;
    private final Clock clock;

    public IncidentSummaryService(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, ObjectStorage storage,
            AnalysisJobService jobs, ApplicationEventPublisher publisher, Clock clock) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.storage = storage;
        this.jobs = jobs;
        this.publisher = publisher;
        this.clock = clock;
    }

    public record SummaryResponse(boolean created, ObjectNode body) {
    }

    @Transactional
    public SummaryResponse requestSummary(UUID eventId, Long userId) {
        MapSqlParameterSource idParam = new MapSqlParameterSource("id", eventId);
        EventRow event = jdbc.query("""
                SELECT event_id, source, gateway_id, rule_id, severity, zone_id,
                    to_json(world_ids)::text AS world_ids, metrics::text AS metrics,
                    event_sequence, raised_at, updated_at
                FROM risk_event
                WHERE event_id = :id
                FOR UPDATE
                """, idParam, (rs, i) -> new EventRow(
                rs.getString("source"),
                rs.getString("gateway_id"),
                rs.getString("rule_id"),
                rs.getString("severity"),
                rs.getString("zone_id"),
                rs.getString("world_ids"),
                rs.getString("metrics"),
                (Integer) rs.getObject("event_sequence"),
                instantOrNull(rs.getTimestamp("raised_at")),
                instantOrNull(rs.getTimestamp("updated_at"))))
                .stream().findFirst()
                .orElseThrow(() -> new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND));

        if (!"LIVE".equals(event.source())) {
            throw new BusinessException(ErrorCode.Event.EVENT_NOT_LIVE);
        }

        List<ObjectNode> active = jdbc.query("""
                SELECT summary_id, event_id, job_id, status, requested_at, finished_at, summary,
                    evidence_timestamps_ms, window_start_ms, window_end_ms, error_code
                FROM incident_summary
                WHERE event_id = :id AND status IN ('QUEUED', 'RUNNING')
                ORDER BY requested_at DESC, summary_id DESC
                LIMIT 1
                """, idParam, (rs, i) -> {
            ObjectNode node = mapper.createObjectNode();
            mapSummaryRow(rs, node);
            return node;
        });
        if (!active.isEmpty()) {
            return new SummaryResponse(false, active.getFirst());
        }

        List<ClipRow> clips = jdbc.query("""
                SELECT clip_id, camera_id, object_key, clip_start_capture_at, clip_end_capture_at,
                    anchor_capture_at, frame_count, container, event_sequence
                FROM media_clip
                WHERE event_id = :id AND deleted_at IS NULL
                ORDER BY resume_count, clip_start_capture_at, camera_id, clip_id
                """, idParam, (rs, i) -> new ClipRow(
                rs.getString("clip_id"),
                rs.getString("camera_id"),
                rs.getString("object_key"),
                rs.getTimestamp("clip_start_capture_at").getTime(),
                rs.getTimestamp("clip_end_capture_at").getTime(),
                rs.getTimestamp("anchor_capture_at").getTime(),
                rs.getInt("frame_count"),
                rs.getString("container"),
                rs.getInt("event_sequence")));
        if (clips.isEmpty()) {
            throw new BusinessException(ErrorCode.Event.CLIP_NOT_READY);
        }

        Instant now = clock.instant();
        int sequence = event.eventSequence() != null && event.eventSequence() >= 1
                ? event.eventSequence()
                : Math.max(1, clips.getLast().eventSequence());
        long windowStartMs = clips.stream().mapToLong(ClipRow::startMs).min().orElseThrow();
        long windowEndMs = clips.stream().mapToLong(ClipRow::endMs).max().orElseThrow();
        long anchorCaptureMs = clips.getLast().anchorMs();

        ObjectNode input = buildInput(eventId, sequence, windowStartMs, windowEndMs, anchorCaptureMs, clips, event, now);
        String jobId = jobs.enqueue(new JobSpec(JOB_TYPE, input, MAX_ATTEMPTS, TIMEOUT_MS, eventId, userId));

        Long summaryId = jdbc.queryForObject("""
                INSERT INTO incident_summary (
                    event_id, event_sequence, job_id, status, requested_by, requested_at, window_start_ms, window_end_ms
                ) VALUES (
                    :eventId, :sequence, :jobId, 'QUEUED', :requestedBy, :requestedAt, :windowStartMs, :windowEndMs
                )
                RETURNING summary_id
                """, new MapSqlParameterSource()
                .addValue("eventId", eventId)
                .addValue("sequence", sequence)
                .addValue("jobId", jobId)
                .addValue("requestedBy", userId)
                .addValue("requestedAt", I1Json.utc(now))
                .addValue("windowStartMs", windowStartMs)
                .addValue("windowEndMs", windowEndMs), Long.class);

        publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
        ObjectNode body = toSummaryNode(summaryId, jobId, "QUEUED", now.toEpochMilli(), null, null,
                List.of(), windowStartMs, windowEndMs, null);
        return new SummaryResponse(true, body);
    }

    @EventListener
    public void stateChanged(JobStateChanged event) {
        if (!JOB_TYPE.equals(event.jobType())) {
            return;
        }
        String sql;
        if ("LEASED".equals(event.status())) {
            sql = "UPDATE incident_summary SET status = 'RUNNING' WHERE job_id = :jobId AND status = 'QUEUED' RETURNING event_id";
        } else if ("QUEUED".equals(event.status())) {
            sql = "UPDATE incident_summary SET status = 'QUEUED' WHERE job_id = :jobId AND status = 'RUNNING' RETURNING event_id";
        } else {
            return;
        }
        List<UUID> eventIds = jdbc.queryForList(sql, new MapSqlParameterSource("jobId", event.jobId()), UUID.class);
        if (!eventIds.isEmpty()) {
            publisher.publishEvent(new RiskEventsDelivered(eventIds));
        }
    }

    @EventListener
    public void finished(JobFinished event) {
        if (!JOB_TYPE.equals(event.jobType())) {
            return;
        }
        List<JobRow> jobsFound = jdbc.query("""
                SELECT input::text AS input, finished_at
                FROM analysis_job
                WHERE job_id = :jobId AND job_type = 'incident_summary'
                """, new MapSqlParameterSource("jobId", event.jobId()),
                (rs, i) -> new JobRow(rs.getString("input"), rs.getObject("finished_at", OffsetDateTime.class)));
        if (jobsFound.isEmpty()) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        JobRow job = jobsFound.getFirst();
        JsonNode input = mapper.readTree(job.input());
        UUID eventId;
        try {
            eventId = UUID.fromString(input.path("event_id").asString());
        } catch (IllegalArgumentException e) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        OffsetDateTime finishedAt = job.finishedAt() != null ? job.finishedAt() : I1Json.utc(clock.instant());

        switch (event.status()) {
            case "SUCCEEDED" -> handleSucceeded(event.jobId(), eventId, input, event.result(), finishedAt);
            case "TIMED_OUT" -> handleTerminated(event.jobId(), eventId, "TIMED_OUT",
                    errorCodeOf(event.error(), "TIMED_OUT"), finishedAt);
            default -> handleTerminated(event.jobId(), eventId, "FAILED",
                    errorCodeOf(event.error(), "CANCELLED".equals(event.status()) ? "JOB_CANCELLED" : "FAILED"), finishedAt);
        }
    }

    private void handleSucceeded(String jobId, UUID eventId, JsonNode input, JsonNode result, OffsetDateTime finishedAt) {
        if (result == null || !result.isObject()
                || result.has("severity") || result.has("proposed_severity")
                || !input.path("event_id").asString().equals(result.path("event_id").asString(""))
                || input.path("event_sequence").asInt(-1) != result.path("event_sequence").asInt(-2)) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        long winStart = result.path("window_start_ms").asLong(-1);
        long winEnd = result.path("window_end_ms").asLong(-1);
        if (winStart <= 0 || winEnd < winStart
                || input.path("window_start_ms").asLong(-2) != winStart
                || input.path("window_end_ms").asLong(-2) != winEnd) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        JsonNode evidenceNode = result.get("evidence_timestamps_ms");
        if (evidenceNode == null || !evidenceNode.isArray() || evidenceNode.isEmpty()) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }
        List<Long> evidence = new ArrayList<>(evidenceNode.size());
        for (JsonNode item : evidenceNode) {
            if (!item.isIntegralNumber()) {
                throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
            }
            long ts = item.longValue();
            if (ts < winStart || ts > winEnd) {
                throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
            }
            evidence.add(ts);
        }
        String summaryText = result.path("summary").asString("");
        int framesUsed = result.path("frames_used").asInt(-1);
        String modelVersion = result.path("model_version").asString("");
        int latencyMs = result.path("latency_ms").asInt(-1);
        if (summaryText.isBlank() || summaryText.length() > 1000
                || framesUsed < 1 || modelVersion.isBlank() || latencyMs < 0) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }

        List<String> sources = jdbc.queryForList(
                "SELECT source FROM risk_event WHERE event_id = :id FOR UPDATE",
                new MapSqlParameterSource("id", eventId), String.class);
        if (sources.isEmpty() || !"LIVE".equals(sources.getFirst())) {
            throw new BusinessException(ErrorCode.Job.INVALID_WORKER_MESSAGE);
        }

        int updated = jdbc.update("""
                UPDATE incident_summary
                SET status = 'SUCCEEDED',
                    finished_at = :finishedAt,
                    window_start_ms = :windowStartMs,
                    window_end_ms = :windowEndMs,
                    summary = :summary,
                    evidence_timestamps_ms = CAST(:evidence AS bigint[]),
                    frames_used = :framesUsed,
                    model_version = :modelVersion,
                    latency_ms = :latencyMs,
                    error_code = NULL
                WHERE job_id = :jobId AND status IN ('QUEUED', 'RUNNING')
                """, new MapSqlParameterSource()
                .addValue("jobId", jobId)
                .addValue("finishedAt", finishedAt)
                .addValue("windowStartMs", winStart)
                .addValue("windowEndMs", winEnd)
                .addValue("summary", summaryText)
                .addValue("evidence", longArrayLiteral(evidence))
                .addValue("framesUsed", framesUsed)
                .addValue("modelVersion", modelVersion)
                .addValue("latencyMs", latencyMs));
        if (updated > 0) {
            publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
        }
    }

    private void handleTerminated(String jobId, UUID eventId, String status, String errorCode, OffsetDateTime finishedAt) {
        int updated = jdbc.update("""
                UPDATE incident_summary
                SET status = :status,
                    finished_at = :finishedAt,
                    error_code = :errorCode
                WHERE job_id = :jobId AND status IN ('QUEUED', 'RUNNING')
                """, new MapSqlParameterSource()
                .addValue("jobId", jobId)
                .addValue("status", status)
                .addValue("finishedAt", finishedAt)
                .addValue("errorCode", errorCode));
        if (updated > 0) {
            publisher.publishEvent(new RiskEventsDelivered(List.of(eventId)));
        }
    }

    private ObjectNode buildInput(UUID eventId, int sequence, long windowStartMs, long windowEndMs, long anchorCaptureMs,
            List<ClipRow> clips, EventRow event, Instant now) {
        ObjectNode input = mapper.createObjectNode();
        input.put("event_id", eventId.toString());
        input.put("event_sequence", sequence);
        input.put("window_start_ms", windowStartMs);
        input.put("window_end_ms", windowEndMs);
        input.put("anchor_capture_ms", anchorCaptureMs);
        ArrayNode clipNodes = input.putArray("clips");
        for (ClipRow c : clips) {
            ObjectNode clip = clipNodes.addObject();
            clip.put("clip_id", c.clipId());
            clip.put("camera_id", c.cameraId());
            clip.put("object_key", c.objectKey());
            clip.put("clip_uri", storage.uri(c.objectKey()));
            clip.put("clip_start_capture_ms", c.startMs());
            clip.put("clip_end_capture_ms", c.endMs());
            clip.put("frame_count", c.frameCount());
            clip.put("container", c.container());
        }
        input.put("face_policy", "BLURRED");
        input.set("structured_state", structuredState(eventId, event, now));
        return input;
    }

    private ObjectNode structuredState(UUID eventId, EventRow event, Instant now) {
        ObjectNode state = mapper.createObjectNode();
        state.put("rule_id", event.ruleId());
        state.put("severity", event.severity());
        if (event.zoneId() != null) {
            state.put("zone_id", event.zoneId());
        }
        state.set("metrics", readObject(event.metrics()));

        ArrayNode transitions = state.putArray("transitions");
        jdbc.query("""
                SELECT transition FROM risk_event_update
                WHERE event_id = :id
                ORDER BY event_sequence, occurred_at
                """, new MapSqlParameterSource("id", eventId), (rs, i) -> rs.getString("transition"))
                .forEach(transitions::add);

        Instant observedAt = event.raisedAt() != null ? event.raisedAt()
                : (event.updatedAt() != null ? event.updatedAt() : now);
        OffsetDateTime observedUtc = I1Json.utc(observedAt);

        ArrayNode tracks = state.putArray("tracks");
        if (event.gatewayId() != null) {
            for (JsonNode worldId : readArray(event.worldIds())) {
                jdbc.query("""
                        SELECT world_id, ST_X(position) AS x, ST_Y(position) AS y, head_z, head_z_validity,
                            observation_state, input_provenance::text AS input_provenance
                        FROM world_track_state
                        WHERE gateway_id = :gateway AND world_id = :world AND observation_at <= :observedAt
                        ORDER BY observation_at DESC
                        LIMIT 1
                        """, new MapSqlParameterSource()
                        .addValue("gateway", event.gatewayId())
                        .addValue("world", worldId.asString())
                        .addValue("observedAt", observedUtc),
                        (rs, i) -> trackNode(rs)).forEach(tracks::add);
            }
        }
        if (event.zoneId() == null) {
            state.putNull("zone");
        } else {
            state.set("zone", zoneNode(event.zoneId(), observedUtc));
        }
        return state;
    }

    private ObjectNode trackNode(ResultSet rs) throws SQLException {
        ObjectNode track = mapper.createObjectNode().put("world_id", rs.getString("world_id"));
        track.putArray("position_xy").add(rs.getDouble("x")).add(rs.getDouble("y"));
        double headZ = rs.getDouble("head_z");
        if (rs.wasNull()) {
            track.putNull("head_z");
        } else {
            track.put("head_z", headZ);
        }
        track.put("head_z_validity", rs.getString("head_z_validity"));
        track.put("observation_state", rs.getString("observation_state"));
        track.set("input_provenance", readObject(rs.getString("input_provenance")));
        return track;
    }

    private ObjectNode zoneNode(String zoneId, OffsetDateTime observedAt) {
        return jdbc.query("""
                SELECT zone_id, eval_type AS judge_type FROM zone
                WHERE zone_id = :zone AND valid_from <= :observedAt AND (valid_to IS NULL OR :observedAt < valid_to)
                ORDER BY zone_version DESC
                LIMIT 1
                """, new MapSqlParameterSource().addValue("zone", zoneId).addValue("observedAt", observedAt),
                (rs, i) -> mapper.createObjectNode()
                        .put("zone_id", rs.getString("zone_id"))
                        .put("judge_type", rs.getString("judge_type")))
                .stream().findFirst()
                .orElseGet(() -> mapper.createObjectNode().put("zone_id", zoneId).putNull("judge_type"));
    }

    private ObjectNode readObject(String json) {
        if (json == null || json.isBlank()) {
            return mapper.createObjectNode();
        }
        JsonNode node = mapper.readTree(json);
        return node != null && node.isObject() ? (ObjectNode) node : mapper.createObjectNode();
    }

    private JsonNode readArray(String json) {
        if (json == null || json.isBlank()) {
            return mapper.createArrayNode();
        }
        JsonNode node = mapper.readTree(json);
        return node != null && node.isArray() ? node : mapper.createArrayNode();
    }

    static void mapSummaryRow(ResultSet rs, ObjectNode s) throws SQLException {
        s.put("summary_id", rs.getLong("summary_id"));
        s.put("job_id", rs.getString("job_id"));
        s.put("status", rs.getString("status"));
        s.put("requested_at", rs.getTimestamp("requested_at").getTime());
        Timestamp finishedAt = rs.getTimestamp("finished_at");
        if (finishedAt == null) {
            s.putNull("finished_at");
        } else {
            s.put("finished_at", finishedAt.getTime());
        }
        s.put("summary", rs.getString("summary"));
        ArrayNode ev = s.putArray("evidence_timestamps_ms");
        Array sqlArr = rs.getArray("evidence_timestamps_ms");
        if (sqlArr != null && sqlArr.getArray() instanceof Object[] values) {
            for (Object v : values) {
                if (v instanceof Number n) {
                    ev.add(n.longValue());
                }
            }
        }
        long winStart = rs.getLong("window_start_ms");
        if (rs.wasNull()) {
            s.putNull("window_start_ms");
        } else {
            s.put("window_start_ms", winStart);
        }
        long winEnd = rs.getLong("window_end_ms");
        if (rs.wasNull()) {
            s.putNull("window_end_ms");
        } else {
            s.put("window_end_ms", winEnd);
        }
        s.put("error_code", rs.getString("error_code"));
    }

    private ObjectNode toSummaryNode(Long summaryId, String jobId, String status, long requestedAtMs, Long finishedAtMs,
            String summary, List<Long> evidenceTimestampsMs, Long windowStartMs, Long windowEndMs, String errorCode) {
        ObjectNode s = mapper.createObjectNode();
        s.put("summary_id", summaryId);
        s.put("job_id", jobId);
        s.put("status", status);
        s.put("requested_at", requestedAtMs);
        if (finishedAtMs == null) {
            s.putNull("finished_at");
        } else {
            s.put("finished_at", finishedAtMs);
        }
        if (summary == null) {
            s.putNull("summary");
        } else {
            s.put("summary", summary);
        }
        ArrayNode ev = s.putArray("evidence_timestamps_ms");
        for (Long ts : evidenceTimestampsMs) {
            ev.add(ts);
        }
        if (windowStartMs == null) {
            s.putNull("window_start_ms");
        } else {
            s.put("window_start_ms", windowStartMs);
        }
        if (windowEndMs == null) {
            s.putNull("window_end_ms");
        } else {
            s.put("window_end_ms", windowEndMs);
        }
        if (errorCode == null) {
            s.putNull("error_code");
        } else {
            s.put("error_code", errorCode);
        }
        return s;
    }

    private static Instant instantOrNull(Timestamp ts) {
        return ts == null ? null : ts.toInstant();
    }

    private static String errorCodeOf(JsonNode error, String fallback) {
        if (error == null || error.isNull()) {
            return fallback;
        }
        String code = error.path("code").asString("");
        return code.isBlank() ? fallback : code;
    }

    private static String longArrayLiteral(List<Long> values) {
        StringBuilder sb = new StringBuilder("{");
        for (int i = 0; i < values.size(); i++) {
            if (i > 0) {
                sb.append(',');
            }
            sb.append(values.get(i));
        }
        return sb.append('}').toString();
    }

    private record EventRow(String source, String gatewayId, String ruleId, String severity, String zoneId,
            String worldIds, String metrics, Integer eventSequence, Instant raisedAt, Instant updatedAt) {
    }

    private record ClipRow(String clipId, String cameraId, String objectKey, long startMs, long endMs, long anchorMs,
            int frameCount, String container, int eventSequence) {
    }

    private record JobRow(String input, OffsetDateTime finishedAt) {
    }
}
