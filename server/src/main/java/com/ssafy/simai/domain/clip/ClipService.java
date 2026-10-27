package com.ssafy.simai.domain.clip;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.storage.ObjectStorage;
import com.ssafy.simai.global.storage.ObjectStorage.SignedRequest;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Clock;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.support.TransactionTemplate;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 이벤트 클립 목록·재생 주소·보존 표시(S15P31A101-261, 명세 11.5·12.6·14, FR-59·99). 모두 관리자 전용이다(12.6).
 * 재생 주소는 요청마다 짧게 사는 GET 서명 주소(ObjectStorage)를 내고 그때마다 clip_access_log 에 남긴다(14장 조회 기록).
 * EC2 는 영상 바이트를 다루지 않는다: 원본(h264-annexb)을 그대로 서명하고 형식(content_type·container)을 함께 알려 화면이 변환해 재생한다.
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ClipService {

    private static final String SELECT = """
            SELECT c.clip_id, c.event_id, c.camera_id, c.resume_count, c.event_sequence, c.rule_id, c.severity,
                c.anchor_capture_at, c.clip_start_capture_at, c.clip_end_capture_at, c.frame_count, c.starts_with_keyframe,
                c.truncated_start, c.truncated_end, c.has_gaps, c.content_type, c.container, c.size_bytes, c.face_redaction,
                c.retention_hold, c.delete_after, c.deleted_at, c.uploaded_at, c.received_at,
                (c.retention_hold AND (c.severity = 'CRITICAL' OR EXISTS (SELECT 1 FROM media_clip_retention r
                    WHERE r.clip_id = c.clip_id AND r.reason = 'CRITICAL_AUTO'))) AS auto_retained
            FROM media_clip c
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectStorage storage;
    private final ObjectMapper mapper;
    private final TransactionTemplate transaction;
    private final Clock clock;

    public ClipService(NamedParameterJdbcTemplate jdbc, ObjectStorage storage, ObjectMapper mapper, TransactionTemplate transaction,
            Clock clock) {
        this.jdbc = jdbc;
        this.storage = storage;
        this.mapper = mapper;
        this.transaction = transaction;
        this.clock = clock;
    }

    /** 이벤트의 클립. 위험 구간(resume_count)·카메라 순. 지운 클립도 기록은 보인다(deleted_at_ms) */
    public ObjectNode eventClips(UUID eventId) {
        if (jdbc.queryForList("SELECT 1 FROM risk_event WHERE event_id = :e", new MapSqlParameterSource("e", eventId)).isEmpty()) {
            throw new BusinessException(ErrorCode.Event.EVENT_NOT_FOUND);
        }
        ObjectNode node = mapper.createObjectNode();
        node.put("event_id", eventId.toString());
        node.putArray("clips").addAll(jdbc.query(SELECT + " WHERE c.event_id = :e ORDER BY c.resume_count, c.camera_id",
                new MapSqlParameterSource("e", eventId), (rs, i) -> view(rs)));
        return node;
    }

    /** 재생 주소. 서명에 성공했을 때만 조회 기록을 남긴다(주소를 받지 못했으면 열람이 아니다) */
    public ObjectNode playback(UUID clipId, long userId) {
        List<Stored> found = jdbc.query("SELECT object_key, content_type, container, deleted_at FROM media_clip WHERE clip_id = :c",
                new MapSqlParameterSource("c", clipId), (rs, i) -> new Stored(rs.getString("object_key"),
                        rs.getString("content_type"), rs.getString("container"), rs.getObject("deleted_at") != null));
        if (found.isEmpty()) {
            throw new BusinessException(ErrorCode.Clip.CLIP_NOT_FOUND);
        }
        Stored clip = found.getFirst();
        if (clip.deleted()) {
            throw new BusinessException(ErrorCode.Clip.CLIP_DELETED);
        }
        SignedRequest signed = storage.signGet(clip.objectKey());
        jdbc.update("""
                INSERT INTO clip_access_log (clip_id, user_id, accessed_at, signed_url_expires_at) VALUES (:c, :u, :now, :expires)
                """, new MapSqlParameterSource().addValue("c", clipId).addValue("u", userId).addValue("now", now())
                .addValue("expires", OffsetDateTime.ofInstant(signed.expiresAt(), ZoneOffset.UTC)));
        ObjectNode node = mapper.createObjectNode();
        node.put("clip_id", clipId.toString());
        node.put("url", signed.url());
        node.put("expires_at_ms", signed.expiresAt().toEpochMilli());
        node.put("content_type", clip.contentType());
        node.put("container", clip.container());
        return node;
    }

    /** 재생 주소에 쓰는 칸. object_key 는 응답·로그에 싣지 않는다 */
    private record Stored(String objectKey, String contentType, String container, boolean deleted) {
    }

    /**
     * 보존 표시 붙이기·떼기(관리자, 11.5 아차사고). 바뀔 때만 이력(ADMIN_HOLD·ADMIN_RELEASE)을 남긴다.
     * CRITICAL 이벤트 연결분은 자동 보존이라 떼지 못한다. 지운 클립은 되살릴 수 없어 붙이지 않는다.
     */
    public ObjectNode setRetention(UUID clipId, Boolean hold, long userId) {
        if (hold == null) {
            throw new BusinessException(ErrorCode.Clip.INVALID_RETENTION);
        }
        MapSqlParameterSource params = new MapSqlParameterSource("c", clipId);
        return transaction.execute(status -> {
            List<ObjectNode> found = jdbc.query(SELECT + " WHERE c.clip_id = :c FOR UPDATE OF c", params, (rs, i) -> view(rs));
            if (found.isEmpty()) {
                throw new BusinessException(ErrorCode.Clip.CLIP_NOT_FOUND);
            }
            ObjectNode clip = found.getFirst();
            if (clip.get("retention_hold").asBoolean() == hold) {
                return clip;
            }
            if (!hold && clip.get("auto_retained").asBoolean()) {
                throw new BusinessException(ErrorCode.Clip.CLIP_AUTO_RETAINED);
            }
            if (hold && !clip.get("deleted_at_ms").isNull()) {
                throw new BusinessException(ErrorCode.Clip.CLIP_DELETED);
            }
            jdbc.update("UPDATE media_clip SET retention_hold = :hold WHERE clip_id = :c", params.addValue("hold", hold));
            jdbc.update("""
                    INSERT INTO media_clip_retention (clip_id, event_id, retention_hold, reason, actor_user_id, issued_at)
                    VALUES (:c, :e, :hold, :reason, :user, :now)
                    """, params.addValue("e", UUID.fromString(clip.get("event_id").asString()))
                    .addValue("reason", hold ? "ADMIN_HOLD" : "ADMIN_RELEASE").addValue("user", userId).addValue("now", now()));
            // auto_retained 는 보존 표시에 따라 바뀌므로 다시 읽어 돌려준다
            return jdbc.query(SELECT + " WHERE c.clip_id = :c", params, (rs, i) -> view(rs)).getFirst();
        });
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode n = mapper.createObjectNode();
        n.put("clip_id", rs.getString("clip_id"));
        n.put("event_id", rs.getString("event_id"));
        n.put("camera_id", rs.getString("camera_id"));
        n.put("resume_count", rs.getInt("resume_count"));
        n.put("event_sequence", rs.getInt("event_sequence"));
        n.put("rule_id", rs.getString("rule_id"));
        n.put("severity", rs.getString("severity"));
        n.put("anchor_capture_ms", millis(rs, "anchor_capture_at"));
        n.put("clip_start_capture_ms", millis(rs, "clip_start_capture_at"));
        n.put("clip_end_capture_ms", millis(rs, "clip_end_capture_at"));
        n.put("frame_count", rs.getInt("frame_count"));
        n.put("starts_with_keyframe", rs.getBoolean("starts_with_keyframe"));
        n.put("truncated_start", rs.getBoolean("truncated_start"));
        n.put("truncated_end", rs.getBoolean("truncated_end"));
        n.put("has_gaps", rs.getBoolean("has_gaps"));
        n.put("content_type", rs.getString("content_type"));
        n.put("container", rs.getString("container"));
        n.put("size_bytes", rs.getLong("size_bytes"));
        n.put("face_redaction", rs.getString("face_redaction"));
        n.put("retention_hold", rs.getBoolean("retention_hold"));
        // CRITICAL 이벤트 연결분: 화면이 '떼기' 를 막는다
        n.put("auto_retained", rs.getBoolean("auto_retained"));
        n.put("delete_after_ms", millis(rs, "delete_after"));
        n.put("deleted_at_ms", millis(rs, "deleted_at"));
        n.put("uploaded_at_ms", millis(rs, "uploaded_at"));
        n.put("received_at_ms", millis(rs, "received_at"));
        return n;
    }

    private static Long millis(ResultSet rs, String column) throws SQLException {
        OffsetDateTime t = rs.getObject(column, OffsetDateTime.class);
        return t == null ? null : t.toInstant().toEpochMilli();
    }

    private OffsetDateTime now() {
        return OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
    }
}
