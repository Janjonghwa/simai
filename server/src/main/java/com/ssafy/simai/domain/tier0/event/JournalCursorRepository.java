package com.ssafy.simai.domain.tier0.event;

import java.time.OffsetDateTime;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * journal_cursor(Tier 0 인스턴스별 저널 수신 상태, V1). acked_offset 은 줄어들지 않는다(GREATEST).
 * 아직 빈틈없이 받은 offset 이 없으면 acked_offset 을 -1 로 둔다. 계약 값이 아니라 이 테이블 안의 표시이고 Tier 0 에 보내지 않는다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class JournalCursorRepository {

    static final long NONE = -1;

    private static final String UPSERT = """
            INSERT INTO journal_cursor (gateway_id, acked_offset, sync_mode, updated_at)
            VALUES (:gatewayId, :acked, :syncMode, :now)
            ON CONFLICT (gateway_id) DO UPDATE SET
                acked_offset = GREATEST(journal_cursor.acked_offset, EXCLUDED.acked_offset),
                sync_mode = COALESCE(:setSyncMode, journal_cursor.sync_mode),
                updated_at = EXCLUDED.updated_at
            """;

    private final NamedParameterJdbcTemplate jdbc;

    public JournalCursorRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** 저장된 acked_offset. 행이 없거나 아직 받은 것이 없으면 null */
    public Long loadAcked(String gatewayId) {
        List<Long> rows = jdbc.queryForList("SELECT acked_offset FROM journal_cursor WHERE gateway_id = :g",
                new MapSqlParameterSource("g", gatewayId), Long.class);
        return rows.isEmpty() || rows.getFirst() < 0 ? null : rows.getFirst();
    }

    public void saveSyncMode(String gatewayId, boolean syncMode, OffsetDateTime now) {
        upsert(gatewayId, NONE, syncMode, syncMode, now);
    }

    public void saveAcked(String gatewayId, long acked, OffsetDateTime now) {
        upsert(gatewayId, acked, true, null, now);
    }

    public void saveSnapshot(String gatewayId, OffsetDateTime snapshotAt, Long snapshotOffset, Long resendFrom) {
        jdbc.update("""
                UPDATE journal_cursor SET last_snapshot_at = :at, last_snapshot_offset = :offset, resend_from_offset = :resend
                WHERE gateway_id = :g
                """, new MapSqlParameterSource()
                .addValue("at", snapshotAt).addValue("offset", snapshotOffset).addValue("resend", resendFrom)
                .addValue("g", gatewayId));
    }

    private void upsert(String gatewayId, long acked, boolean insertSyncMode, Boolean setSyncMode, OffsetDateTime now) {
        jdbc.update(UPSERT, new MapSqlParameterSource()
                .addValue("gatewayId", gatewayId)
                .addValue("acked", acked)
                .addValue("syncMode", insertSyncMode)
                .addValue("setSyncMode", setSyncMode, java.sql.Types.BOOLEAN)
                .addValue("now", now));
    }
}
