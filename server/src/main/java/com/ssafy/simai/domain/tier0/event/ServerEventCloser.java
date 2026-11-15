package com.ssafy.simai.domain.tier0.event;

import com.ssafy.simai.domain.tier0.i1.I1Json;
import java.time.Clock;
import java.time.Instant;
import java.time.temporal.ChronoUnit;
import java.util.Collection;
import java.util.List;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;

/**
 * EC2 마감(S15P31A101-722, 계약 risk_event 2.5.0 server_close_resolution, V79). 그 이벤트를 들고 있던 Tier 0 가 사라졌거나
 * 살아 있는 Tier 0 의 스냅샷에 없어 해소 전이가 다시 오지 않을 LIVE 이벤트를 닫는다.
 * <ul>
 *   <li>관측 해소도 운영자 종료도 아니다. 검증 상태·등급·event_sequence 는 그대로 두고 lifecycle RESOLVED·resolved_at·resolution 만 바꾼다.
 *   <li>기록은 추가만: risk_event_server_close 에 행을 남기고, Tier 0 전이 기록(risk_event_update)에는 아무것도 넣지 않는다
 *       (EC2 는 Tier 0 전이를 만들지 않는다, 명세 3.2).
 *   <li>이미 RESOLVED 인 이벤트는 건드리지 않는다. 같은 행을 Tier 0 전이 반영(RiskEventRepository.APPLY)과 함께 잡아도 잠금 뒤 다시 본다.
 *   <li>같은 Tier 0 가 돌아와 그 이벤트의 전이를 다시 보내거나 스냅샷에 활성으로 실으면 Tier 0 쪽을 따라 현재 상태를 되돌린다
 *       (RiskEventRepository.APPLY·reopenServerClosed). 마감 기록은 지우지 않는다.
 * </ul>
 * 한 트랜잭션 안에서 부른다. 화면 알림(RiskEventsDelivered)은 부른 쪽이 낸다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class ServerEventCloser {

    /** 마감 종류(계약 server_close_resolution.kind, V79 CHECK) */
    public enum Kind {
        /** 작업 종료 때 그 Tier 0 가 연결도 열린 GPU 세션도 없음 */
        WORK_ENDED,
        /** 그 Tier 0 의 GPU 세션이 모두 끝났고 연결도 없음 */
        SESSION_ENDED,
        /** 살아 있는 Tier 0 의 재동기화 스냅샷 활성 목록에 없음 */
        NOT_IN_SNAPSHOT
    }

    /** risk_event.resolution 의 EC2 마감 종류(SQL IN 목록). Tier 0 가 돌아오면 이 종류만 되돌린다 */
    static final String KINDS_SQL = "('WORK_ENDED', 'SESSION_ENDED', 'NOT_IN_SNAPSHOT')";

    private static final String CLOSE = """
            WITH targets AS (
                SELECT event_id, lifecycle, event_sequence FROM risk_event
                WHERE source = 'LIVE' AND gateway_id = :g AND lifecycle <> 'RESOLVED'%s
                ORDER BY event_id
                FOR UPDATE
            ), recorded AS (
                INSERT INTO risk_event_server_close (close_id, event_id, kind, gateway_id, trigger_ref, reason,
                    lifecycle_before, last_event_sequence, closed_at)
                SELECT gen_random_uuid(), t.event_id, :kind, :g, :ref, :reason, t.lifecycle, t.event_sequence, :now
                FROM targets t
                RETURNING close_id, event_id, last_event_sequence
            )
            UPDATE risk_event e SET
                lifecycle = 'RESOLVED',
                resolved_at = :now,
                resolution = jsonb_build_object(
                    'kind', CAST(:kind AS text),
                    'close_id', r.close_id::text,
                    'closed_at', CAST(:nowMs AS bigint),
                    'reason', CAST(:reason AS text),
                    'trigger_ref', CAST(:ref AS text),
                    'last_event_sequence', r.last_event_sequence)
            FROM recorded r
            WHERE e.event_id = r.event_id
            RETURNING e.event_id
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;

    public ServerEventCloser(NamedParameterJdbcTemplate jdbc, Clock clock) {
        this.jdbc = jdbc;
        this.clock = clock;
    }

    /**
     * 그 Tier 0(gateway_id)의 해소 전 LIVE 이벤트를 닫는다.
     * @param keep 닫지 않을 이벤트(스냅샷 활성 목록). 비어도 된다
     * @param notAfter 있으면 마지막 Tier 0 시각(updated_at, 없으면 raised_at)이 이보다 늦은 이벤트는 닫지 않는다(시각 모름은 닫는다)
     * @return 닫은 event_id
     */
    public List<UUID> close(String gatewayId, Kind kind, String triggerRef, String reason, Collection<UUID> keep,
            Instant notAfter) {
        if (gatewayId == null || gatewayId.isBlank()) {
            return List.of();
        }
        Instant now = clock.instant().truncatedTo(ChronoUnit.MILLIS);
        StringBuilder where = new StringBuilder();
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("g", gatewayId)
                .addValue("kind", kind.name())
                .addValue("ref", triggerRef)
                .addValue("reason", reason.length() > 500 ? reason.substring(0, 500) : reason)
                .addValue("now", I1Json.utc(now))
                .addValue("nowMs", now.toEpochMilli());
        if (keep != null && !keep.isEmpty()) {
            where.append(" AND event_id NOT IN (:keep)");
            params.addValue("keep", keep);
        }
        if (notAfter != null) {
            where.append(" AND (COALESCE(updated_at, raised_at) IS NULL OR COALESCE(updated_at, raised_at) <= :notAfter)");
            params.addValue("notAfter", I1Json.utc(notAfter));
        }
        return jdbc.queryForList(CLOSE.formatted(where), params, UUID.class);
    }
}
