package com.ssafy.simai.domain.video;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.temporal.ChronoUnit;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 상시 영상 조각 목록·재생 주소(S15P31A101-330, 관리자). 이벤트 클립(ClipService)과 같은 규칙이다:
 * 목록에는 주소를 싣지 않고, 재생 주소는 조각마다 짧게 사는 GET 서명 주소를 내며 그때마다 video_access_log 에 남긴다(14장 조회 기록).
 * EC2 는 영상 바이트를 다루지 않는다: 원본(h264-annexb)을 그대로 서명하고 형식(content_type·container)을 함께 알려 화면이 감싸 재생한다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class VideoService {

    /** 한 번에 볼 구간. 1분 조각이면 카메라 4대 하루 5,760행이다 */
    static final Duration MAX_RANGE = Duration.ofHours(24);
    /** 조각 하나의 최대 길이로 보는 값. 구간 앞에서 시작한 조각을 찾을 때 시작 시각 색인을 이만큼만 거슬러 본다 */
    static final Duration MAX_SEGMENT = Duration.ofHours(1);

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;
    private final Clock clock;

    VideoService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper, Clock clock) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
        this.clock = clock;
    }

    /**
     * 촬영 구간이 [from, to] 와 겹치는 조각(카메라·시작 시각 순). cameraId 가 없으면 모든 카메라. 지운 조각도 기록은 보인다(deleted_at_ms).
     * 조각 길이는 Tier 0 가 정하므로 시작이 from 보다 이른 조각도 끝이 from 뒤면 넣는다(수신이 MAX_SEGMENT 보다 긴 조각을 받지 않는다).
     * 카메라 조건은 있을 때만 붙인다(OR 로 묶으면 일반 실행 계획이 카메라 색인을 못 쓴다)
     */
    ObjectNode segments(Instant from, Instant to, String cameraId) {
        if (from.isAfter(to) || Duration.between(from, to).compareTo(MAX_RANGE) > 0) {
            throw new BusinessException(ErrorCode.Video.INVALID_VIDEO_QUERY);
        }
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("from", utc(from)).addValue("to", utc(to)).addValue("lookback", utc(from.minus(MAX_SEGMENT)))
                .addValue("camera", cameraId);
        List<ObjectNode> rows = jdbc.query("""
                SELECT segment_id, gateway_id, camera_id, segment_start_capture_at, segment_end_capture_at, frame_count,
                    starts_with_keyframe, has_gaps, content_type, container, size_bytes, delete_after, deleted_at
                FROM video_segment
                WHERE segment_start_capture_at <= :to AND segment_end_capture_at >= :from
                  AND segment_start_capture_at >= :lookback%s
                ORDER BY camera_id, segment_start_capture_at, segment_id
                """.formatted(cameraId == null ? "" : " AND camera_id = :camera"), params, (rs, i) -> view(rs));
        ObjectNode body = mapper.createObjectNode();
        body.put("from", from.toEpochMilli());
        body.put("to", to.toEpochMilli());
        body.putArray("segments").addAll(rows);
        return body;
    }

    /** 재생 주소. 서명에 성공했을 때만 조회 기록을 남긴다(주소를 받지 못했으면 열람이 아니다) */
    ObjectNode playback(UUID segmentId, long userId) {
        List<Stored> found = jdbc.query("""
                SELECT object_key, content_type, container, deleted_at FROM video_segment WHERE segment_id = :s
                """, new MapSqlParameterSource("s", segmentId), (rs, i) -> new Stored(rs.getString("object_key"),
                rs.getString("content_type"), rs.getString("container"), rs.getObject("deleted_at") != null));
        if (found.isEmpty()) {
            throw new BusinessException(ErrorCode.Video.SEGMENT_NOT_FOUND);
        }
        Stored segment = found.getFirst();
        if (segment.deleted()) {
            throw new BusinessException(ErrorCode.Video.SEGMENT_DELETED);
        }
        SignedRequest signed = storage.signGet(segment.objectKey());
        jdbc.update("""
                INSERT INTO video_access_log (segment_id, user_id, accessed_at, signed_url_expires_at)
                VALUES (:s, :u, :now, :expires)
                """, new MapSqlParameterSource().addValue("s", segmentId).addValue("u", userId)
                .addValue("now", utc(clock.instant().truncatedTo(ChronoUnit.MILLIS)))
                .addValue("expires", utc(signed.expiresAt())));
        ObjectNode node = mapper.createObjectNode();
        node.put("segment_id", segmentId.toString());
        node.put("url", signed.url());
        node.put("expires_at_ms", signed.expiresAt().toEpochMilli());
        node.put("content_type", segment.contentType());
        node.put("container", segment.container());
        return node;
    }

    /** 재생 주소에 쓰는 칸. object_key 는 응답·로그에 싣지 않는다 */
    private record Stored(String objectKey, String contentType, String container, boolean deleted) {
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode n = mapper.createObjectNode();
        n.put("segment_id", rs.getString("segment_id"));
        n.put("gateway_id", rs.getString("gateway_id"));
        n.put("camera_id", rs.getString("camera_id"));
        n.put("segment_start_capture_ms", rs.getTimestamp("segment_start_capture_at").getTime());
        n.put("segment_end_capture_ms", rs.getTimestamp("segment_end_capture_at").getTime());
        n.put("frame_count", rs.getInt("frame_count"));
        n.put("starts_with_keyframe", rs.getBoolean("starts_with_keyframe"));
        n.put("has_gaps", rs.getBoolean("has_gaps"));
        n.put("content_type", rs.getString("content_type"));
        n.put("container", rs.getString("container"));
        n.put("size_bytes", rs.getLong("size_bytes"));
        n.put("delete_after_ms", rs.getTimestamp("delete_after").getTime());
        Timestamp deletedAt = rs.getTimestamp("deleted_at");
        if (deletedAt == null) {
            n.putNull("deleted_at_ms");
        } else {
            n.put("deleted_at_ms", deletedAt.getTime());
        }
        return n;
    }

    private static OffsetDateTime utc(Instant instant) {
        return instant.atOffset(ZoneOffset.UTC);
    }
}
