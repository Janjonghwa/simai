package com.ssafy.simai.domain.tier0.event;

import java.time.OffsetDateTime;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * journal_cursor(Tier 0 인스턴스별 저널 수신 상태, V1·V4). acked_offset 은 같은 세대 안에서 줄어들지 않는다(GREATEST).
 * 아직 빈틈없이 받은 offset 이 없으면 acked_offset 을 -1 로 둔다. 계약 값이 아니라 이 테이블 안의 표시이고 Tier 0 에 보내지 않는다.
 * journal_epoch 는 Tier 0 저널이 새로 시작될 때마다 1 씩 오른다(V4).
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

    /** acked 는 아직 받은 것이 없으면 null */
    public record Cursor(Long acked, int epoch) {
    }

    public Cursor load(String gatewayId) {
        List<Cursor> rows = jdbc.query("SELECT acked_offset, journal_epoch FROM journal_cursor WHERE gateway_id = :g",
                new MapSqlParameterSource("g", gatewayId),
                (rs, i) -> new Cursor(rs.getLong(1) < 0 ? null : rs.getLong(1), rs.getInt(2)));
        return rows.isEmpty() ? new Cursor(null, 0) : rows.getFirst();
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

    /** Tier 0 저널이 새로 시작됐다. 세대를 올리고 받은 위치를 비운다. @return 새 세대 */
    public int startNewEpoch(String gatewayId, OffsetDateTime now) {
        return jdbc.queryForObject("""
                UPDATE journal_cursor SET journal_epoch = journal_epoch + 1, acked_offset = -1, resend_from_offset = NULL,
                    updated_at = :now
                WHERE gateway_id = :g
                RETURNING journal_epoch
                """, new MapSqlParameterSource().addValue("g", gatewayId).addValue("now", now), Integer.class);
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
