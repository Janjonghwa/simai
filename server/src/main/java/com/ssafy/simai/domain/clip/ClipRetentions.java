package com.ssafy.simai.domain.clip;

import java.time.OffsetDateTime;
import java.util.UUID;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;

/** Tier 0 가 정한 보존 표시 이력(media_clip_retention, LIVE). 클립 기록의 CRITICAL 자동 보존과 보존 기록 메시지가 같은 모양으로 남긴다 */
final class ClipRetentions {

    private ClipRetentions() {
    }

    /** 같은 msg_id 는 한 행(V11). 새로 남겼으면 true */
    static boolean recordTier0(NamedParameterJdbcTemplate jdbc, UUID clipId, UUID eventId, String gatewayId, int resumeCount,
            String severity, String reason, OffsetDateTime issuedAt, UUID msgId) {
        return jdbc.update("""
                INSERT INTO media_clip_retention (clip_id, event_id, gateway_id, resume_count, severity, retention_hold, reason,
                    source, issued_at, msg_id)
                VALUES (:clipId, :eventId, :gatewayId, :resumeCount, :severity, true, :reason, 'LIVE', :issuedAt, :msgId)
                ON CONFLICT (msg_id) DO NOTHING
                """, new MapSqlParameterSource().addValue("clipId", clipId).addValue("eventId", eventId)
                .addValue("gatewayId", gatewayId).addValue("resumeCount", resumeCount).addValue("severity", severity)
                .addValue("reason", reason).addValue("issuedAt", issuedAt).addValue("msgId", msgId)) > 0;
    }
}
