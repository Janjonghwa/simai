package com.ssafy.simai.domain.recording;

import com.ssafy.simai.global.contract.ContractSchemas;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 게이트웨이 녹화 작업 구간 업로드(S15P31A101-668, 기기 토큰). 설계 docs/design/S15P31A101-668-recording-s3-playback.md.
 * <ul>
 * <li>작업 구간 조회(recording_windows 1.1.0): 범위와 겹치는 끝난 실행의 구간 = 실행 시작 → 실행 종료(작업 시작 = 관제 시작,
 *     S15P31A101-679 에서 -668 의 세팅 완료부터 넓히기를 되돌렸다). 작업 하나에 실행이 여럿이면 실행마다 한 구간이다
 *     (S15P31A101-696, 실행 사이는 관제·경보가 꺼져 있어 올리지 않는다). 진행 중 실행이 겹치면 open_work 로 알려 게이트웨이가 그 녹화를
 *     미룬다(사용자 결정 2026-11-13). 끝난 실행은 다음 실행이 진행 중이어도 바로 싣는다(리허설 뒤 본 시연 전에 다시 볼 수 있게).</li>
 * <li>조각 등록(recording_segment 1.0.0): 계약 검사 → 그 작업의 끝난 실행 구간 하나 안 → 이 기기가 받은 RECORDING_SEGMENT 발급과 키·형식·크기·해시가 같음 →
 *     video_segment 한 행(origin GATEWAY_RECORDING, delete_after = 첫 프레임 촬영 시각 + 14일). 삭제·재생 주소·조회 기록은 domain/video 그대로다.</li>
 * </ul>
 * 영상 바이트는 다루지 않는다(EC2 디스크에 두지 않는다).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class RecordingService {

    private static final String SCHEMA = "messages/recording_segment";
    /** 한 번에 묻는 범위. 게이트웨이는 녹화 하나(하루 관제)마다 묻는다 */
    static final Duration MAX_RANGE = Duration.ofHours(24);
    /** 보관 기간(사용자 결정, 명세 11.5). 첫 프레임 촬영 시각 기준 */
    static final Duration RETENTION = Duration.ofDays(14);
    /** 조각은 구간 시작 직전 키프레임부터 자른다. GOP 하나(C110 수 초)를 넉넉히 봐준다 */
    static final Duration LEAD_SLACK = Duration.ofSeconds(30);
    /** 조각 끝 = 마지막 프레임 + 그 프레임 길이(게이트웨이가 1 s 로 묶는다) */
    static final Duration TAIL_SLACK = Duration.ofSeconds(1);
    /** 게이트웨이 조각은 5분. 목록 질의(VideoService, 조각 길이 1시간까지 거슬러 봄)보다 충분히 짧게 받는다 */
    static final Duration MAX_SEGMENT = Duration.ofMinutes(10);
    /** PostgreSQL timestamptz 가 받는 범위 안(9999-12-31) */
    private static final long MAX_EPOCH_MS = 253_402_300_799_999L;
    private static final String PREFIX = "recording-segments/";

    /** 응답 판. 1.1.0 은 구간마다 실행 번호(run_no)를 싣는다(S15P31A101-696) */
    static final String WINDOWS_VERSION = "1.1.0";

    private final NamedParameterJdbcTemplate jdbc;
    private final ContractSchemas schemas;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final Clock clock;

    RecordingService(NamedParameterJdbcTemplate jdbc, ContractSchemas schemas, ObjectMapper mapper, TransactionTemplate transaction,
            Clock clock) {
        this.jdbc = jdbc;
        this.schemas = schemas;
        this.mapper = mapper;
        this.transaction = transaction;
        this.clock = clock;
    }

    /**
     * recording_windows 1.1.0. 구간은 실행 시작 → 실행 종료(작업 시작 = 관제 시작, S15P31A101-679). 실행 기록은 work_run_window
     * (실행 표, 실행 기록이 없는 시작한 작업은 work_unit 시각을 실행 1로 본다)에서 읽는다
     */
    ObjectNode windows(String deviceId, Instant from, Instant to) {
        requireLaptop(deviceId);
        if (from.isAfter(to) || Duration.between(from, to).compareTo(MAX_RANGE) > 0) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_QUERY);
        }
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("from", utc(from)).addValue("to", utc(to));
        List<ObjectNode> rows = jdbc.query("""
                SELECT work_id, run_no, started_at, ended_at FROM work_run_window
                WHERE ended_at IS NOT NULL AND ended_at >= :from AND started_at <= :to AND ended_at > started_at
                ORDER BY started_at, work_id, run_no
                """, params, (rs, i) -> {
                    ObjectNode w = mapper.createObjectNode();
                    w.put("work_id", rs.getLong("work_id"));
                    w.put("run_no", rs.getInt("run_no"));
                    w.put("start_ms", rs.getTimestamp("started_at").getTime());
                    w.put("end_ms", rs.getTimestamp("ended_at").getTime());
                    return w;
                });
        // 진행 중 실행(작업이 IN_PROGRESS 인 것만: 실행 표 밖에서 끝낸 작업의 열린 실행은 보지 않는다)
        Boolean open = jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM work_run_window r JOIN work_unit w ON w.work_id = r.work_id
                    WHERE r.ended_at IS NULL AND w.status = 'IN_PROGRESS' AND r.started_at <= :to)
                """, params, Boolean.class);
        ObjectNode body = mapper.createObjectNode();
        body.put("schema_version", WINDOWS_VERSION);
        body.put("from_ms", from.toEpochMilli());
        body.put("to_ms", to.toEpochMilli());
        ArrayNode windows = body.putArray("windows");
        windows.addAll(rows);
        body.put("open_work", Boolean.TRUE.equals(open));
        body.put("retention_days", RETENTION.toDays());
        return body;
    }

    /**
     * 끝난 실행이 있는 작업에 대한 녹화별 계획(작업 하나의 끝난 실행 전부를 합친 수). 영상 등록 수는 기기의 주장을 받지 않고 DB에서 센다.
     * 다음 실행이 진행 중이어도 끝난 실행의 진행은 받는다(S15P31A101-696).
     */
    ObjectNode progress(String deviceId, JsonNode body) {
        requireLaptop(deviceId);
        if (body == null || !schemas.violations("messages/recording_upload_progress", mapper.writeValueAsString(body)).isEmpty()
                || !body.path("work_id").canConvertToLong()
                || body.path("failed_segments").asInt() > body.path("expected_segments").asInt()) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_SEGMENT);
        }
        MapSqlParameterSource params = new MapSqlParameterSource("w", body.get("work_id").asLong());
        Boolean ended = jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM work_run_window WHERE work_id = :w AND ended_at > started_at)
                """, params, Boolean.class);
        if (!Boolean.TRUE.equals(ended)) {
            throw new BusinessException(ErrorCode.Recording.OUTSIDE_WORK_WINDOW);
        }
        params.addValue("g", deviceId).addValue("r", body.get("recording_id").asString())
                .addValue("expected", body.get("expected_segments").asInt())
                .addValue("failed", body.get("failed_segments").asInt())
                .addValue("sealed", body.get("sealed").asBoolean()).addValue("now", utc(clock.instant()));
        jdbc.update("""
                INSERT INTO recording_upload_progress
                    (gateway_id, recording_id, work_id, expected_segments, failed_segments, sealed, received_at)
                VALUES (:g, :r, :w, :expected, :failed, :sealed, :now)
                ON CONFLICT (gateway_id, recording_id, work_id) DO UPDATE SET
                    expected_segments = EXCLUDED.expected_segments, failed_segments = EXCLUDED.failed_segments,
                    sealed = EXCLUDED.sealed, received_at = EXCLUDED.received_at
                """, params);
        ObjectNode result = mapper.createObjectNode();
        result.put("status", "ACCEPTED");
        return result;
    }

    /** recording_segment 1.0.0 → video_segment. 같은 조각 재등록은 같은 내용일 때만 DUPLICATE */
    ObjectNode register(String deviceId, JsonNode body) {
        requireLaptop(deviceId);
        if (body == null || !body.isObject() || !schemas.violations(SCHEMA, mapper.writeValueAsString(body)).isEmpty()) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_SEGMENT);
        }
        for (String field : List.of("segment_start_capture_ms", "segment_end_capture_ms", "created_at_ms", "uploaded_at_ms")) {
            if (body.get(field).asLong() > MAX_EPOCH_MS) {
                throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_SEGMENT);
            }
        }
        if (!body.get("frame_count").canConvertToInt() || !body.get("stream_epochs").get(0).canConvertToLong()
                || (body.has("upload_attempts") && !body.get("upload_attempts").canConvertToInt())) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_SEGMENT);
        }
        long start = body.get("segment_start_capture_ms").asLong();
        long end = body.get("segment_end_capture_ms").asLong();
        if (end <= start || end - start > MAX_SEGMENT.toMillis()) {
            throw new BusinessException(ErrorCode.Recording.INVALID_RECORDING_SEGMENT);
        }
        UUID segmentId = UUID.fromString(body.get("segment_id").asString());
        String key = body.get("object_key").asString();
        if (!key.equals(PREFIX + segmentId + ".mp4")) {
            throw new BusinessException(ErrorCode.Recording.RECORDING_UPLOAD_MISMATCH);
        }
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        Instant deleteAfter = Instant.ofEpochMilli(start).plus(RETENTION);
        if (!deleteAfter.isAfter(now)) {
            throw new BusinessException(ErrorCode.Recording.RECORDING_EXPIRED);
        }
        long workId = body.get("work_id").asLong();
        requireInsideWindow(workId, start, end);
        String sha256 = body.get("sha256").asString();
        String camera = body.get("camera_id").asString();
        MapSqlParameterSource row = new MapSqlParameterSource()
                .addValue("segmentId", segmentId)
                .addValue("gatewayId", deviceId)
                .addValue("cameraId", camera)
                .addValue("streamEpochs", "{" + body.get("stream_epochs").get(0).asLong() + "}")
                .addValue("start", utc(Instant.ofEpochMilli(start)))
                .addValue("end", utc(Instant.ofEpochMilli(end)))
                .addValue("frameCount", body.get("frame_count").asInt())
                .addValue("hasGaps", body.get("has_gaps").asBoolean())
                .addValue("contentType", body.get("content_type").asString())
                .addValue("container", body.get("container").asString())
                .addValue("sizeBytes", body.get("size_bytes").asLong())
                .addValue("sha256", sha256)
                .addValue("createdAt", utc(Instant.ofEpochMilli(body.get("created_at_ms").asLong())))
                .addValue("deleteAfter", utc(deleteAfter))
                .addValue("objectKey", key)
                .addValue("uploadedAt", utc(Instant.ofEpochMilli(body.get("uploaded_at_ms").asLong())))
                .addValue("uploadAttempts", body.has("upload_attempts") ? body.get("upload_attempts").asInt() : null)
                .addValue("receivedAt", utc(now))
                .addValue("workId", workId)
                .addValue("recordingId", body.get("recording_id").asString());
        String status = transaction.execute(tx -> {
            // 아직 행이 없는 조각은 FOR UPDATE 로 잠기지 않는다. 같은 조각을 잠가 재시도로 겹쳐 온 같은 등록을 한 줄로 세운다
            jdbc.queryForList("SELECT pg_advisory_xact_lock(hashtext('simai.video:' || CAST(:segmentId AS text)))", row);
            List<Map<String, Object>> existing = jdbc.queryForList(
                    "SELECT sha256, gateway_id, camera_id FROM video_segment WHERE segment_id = :segmentId", row);
            if (!existing.isEmpty()) {
                Map<String, Object> stored = existing.getFirst();
                if (!Objects.equals(stored.get("sha256"), sha256) || !Objects.equals(stored.get("gateway_id"), deviceId)
                        || !Objects.equals(stored.get("camera_id"), camera)) {
                    throw new BusinessException(ErrorCode.Recording.RECORDING_SEGMENT_CONFLICT);
                }
                return "DUPLICATE";
            }
            claimUpload(deviceId, body, key, now);
            jdbc.update("""
                    INSERT INTO video_segment (segment_id, gateway_id, camera_id, stream_epochs, segment_start_capture_at,
                        segment_end_capture_at, frame_count, starts_with_keyframe, has_gaps, content_type, container, size_bytes, sha256,
                        face_redaction, segment_created_at, delete_after, object_key, uploaded_at, upload_attempts, received_at,
                        origin, work_id, recording_id)
                    VALUES (:segmentId, :gatewayId, :cameraId, CAST(:streamEpochs AS bigint[]), :start, :end, :frameCount, true,
                        :hasGaps, :contentType, :container, :sizeBytes, :sha256, 'NONE', :createdAt, :deleteAfter, :objectKey,
                        :uploadedAt, :uploadAttempts, :receivedAt, 'GATEWAY_RECORDING', :workId, :recordingId)
                    """, row);
            return "CREATED";
        });
        ObjectNode response = mapper.createObjectNode();
        response.put("segment_id", segmentId.toString());
        response.put("status", status);
        return response;
    }

    /**
     * 조각이 그 작업의 끝난 실행 구간 하나 안이어야 한다(S15P31A101-696, 실행 사이 구간은 받지 않는다).
     * 앞은 키프레임 여유(LEAD_SLACK), 끝은 마지막 프레임 길이 여유(TAIL_SLACK)
     */
    private void requireInsideWindow(long workId, long start, long end) {
        List<long[]> found = jdbc.query("""
                SELECT started_at, ended_at FROM work_run_window WHERE work_id = :w AND ended_at IS NOT NULL
                """, new MapSqlParameterSource("w", workId),
                (rs, i) -> new long[] {rs.getTimestamp("started_at").getTime(), rs.getTimestamp("ended_at").getTime()});
        boolean inside = found.stream().anyMatch(w -> start >= w[0] - LEAD_SLACK.toMillis() && end <= w[1] + TAIL_SLACK.toMillis()
                && end > w[0] && start < w[1]);
        if (!inside) {
            throw new BusinessException(ErrorCode.Recording.OUTSIDE_WORK_WINDOW);
        }
    }

    /**
     * 객체 키는 EC2 가 이 기기에 이 조각으로 발급한 RECORDING_SEGMENT 여야 하고, 발급에 묶은 형식·크기·해시가 기록과 같아야 한다.
     * 저장소는 서명에 묶은 내용만 받으므로 같으면 올라온 객체가 기록이 말하는 그 내용이다. 확인되면 발급을 쓰인 것으로 표시한다.
     */
    private void claimUpload(String deviceId, JsonNode body, String key, Instant now) {
        List<Map<String, Object>> grants = jdbc.queryForList("""
                SELECT purpose, requested_by_device, content_type, size_bytes, sha256 FROM upload_grant WHERE object_key = :key FOR UPDATE
                """, new MapSqlParameterSource("key", key));
        if (grants.isEmpty()) {
            throw new BusinessException(ErrorCode.Recording.RECORDING_UPLOAD_MISMATCH);
        }
        Map<String, Object> grant = grants.getFirst();
        boolean same = "RECORDING_SEGMENT".equals(grant.get("purpose"))
                && deviceId.equals(grant.get("requested_by_device"))
                && body.get("content_type").asString().equals(grant.get("content_type"))
                && body.get("size_bytes").asLong() == ((Number) grant.get("size_bytes")).longValue()
                && body.get("sha256").asString().equals("sha256:" + grant.get("sha256"));
        if (!same) {
            throw new BusinessException(ErrorCode.Recording.RECORDING_UPLOAD_MISMATCH);
        }
        jdbc.update("UPDATE upload_grant SET used_at = :usedAt WHERE object_key = :key AND used_at IS NULL",
                new MapSqlParameterSource("key", key).addValue("usedAt", utc(now)));
    }

    /** 녹화는 현장 노트북만 올린다(워치 등 다른 기기 거부) */
    private void requireLaptop(String deviceId) {
        List<String> kind = jdbc.queryForList("SELECT kind FROM device WHERE device_id = :id AND revoked_at IS NULL",
                new MapSqlParameterSource("id", deviceId), String.class);
        if (kind.isEmpty() || !"LAPTOP".equals(kind.getFirst())) {
            throw new BusinessException(ErrorCode.Recording.RECORDING_NOT_A_LAPTOP);
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
    }
}
