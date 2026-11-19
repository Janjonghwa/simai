package com.ssafy.simai.domain.watch;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.Timestamp;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;
import org.springframework.transaction.interceptor.TransactionAspectSupport;

/**
 * 익명 World ID ↔ 워치 배정(watch_binding, V35, 확장 C.6). 움직임 대조(MOTION_MATCH, S15P31A101-340 WatchMotionMatcher)가 기본이고,
 * 관리자 수동 배정(MANUAL, -338)은 덮어쓰기로 남는다: 수동 배정이 걸린 워치·World ID 는 대조가 건드리지 않는다.
 * 1:1: 지금 배정은 워치마다 하나, World ID 마다 하나다.
 * 사람 이름은 두지 않는다(11.4-5). 배정은 풀 때까지 이어지고 워치가 끊겨도 남는다(끊긴 동안 채널은 LOST).
 * <p>
 * 자리 이어받기(S15P31A101-793, V85): 배정된 트랙이 사라진 자리에 새 트랙 하나만 나타나면 {@link #handoff} 로 배정을 새 ID 로 옮긴다.
 * 옮긴 행은 {@code handoff_from} 에 옛 ID 를 적고 걸음 대조가 같은 짝을 다시 뽑을 때까지 임시다. 임시인 동안은 수동 배정이라도
 * 대조가 반대 근거로 풀 수 있다(관리자가 고른 ID 는 이미 사라졌고, 옮긴 것은 시스템의 추정이라서다).
 */
@Service
public class WatchBindings {

    /**
     * 지금 배정 하나. score 는 대조 신뢰도(0~1), MANUAL 은 1.
     *
     * @param handoffFrom 자리 이어받기로 옮겨 온 배정이면 사라진 옛 World ID, 아니면 null
     * @param handoffConfirmedAt 옮겨 온 배정을 대조가 확정한 시각. handoffFrom 이 있는데 null 이면 임시
     */
    public record Binding(long bindingId, String deviceId, String worldId, String method, double score, Instant boundAt,
            String handoffFrom, Instant handoffConfirmedAt) {

        /** 자리 이어받기로 옮겨 온 뒤 아직 대조가 확정하지 않은 배정. 수동이라도 대조가 풀 수 있다 */
        public boolean tentative() {
            return handoffFrom != null && handoffConfirmedAt == null;
        }

        /** 관리자 선택으로 대조가 건드리지 않는 배정(수동이고 임시가 아님) */
        public boolean manualFixed() {
            return "MANUAL".equals(method) && !tentative();
        }
    }

    private static final String COLUMNS = """
            b.binding_id, b.device_id, b.world_id, b.method, b.score, b.bound_at, b.handoff_from, b.handoff_confirmed_at""";
    private static final RowMapper<Binding> ROW = (rs, i) -> {
        Timestamp confirmed = rs.getTimestamp("handoff_confirmed_at");
        return new Binding(rs.getLong("binding_id"), rs.getString("device_id"), rs.getString("world_id"), rs.getString("method"),
                rs.getDouble("score"), rs.getTimestamp("bound_at").toInstant(), rs.getString("handoff_from"),
                confirmed == null ? null : confirmed.toInstant());
    };
    /** 대조가 끝낼 수 있는 행: 대조 배정과 임시 이어받기(수동이라도). 확정된 수동은 관리자 것이라 건드리지 않는다 */
    private static final String MOTION_OWNED = "(method = 'MOTION_MATCH' OR (handoff_from IS NOT NULL AND handoff_confirmed_at IS NULL))";

    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;
    private final ApplicationEventPublisher events;

    WatchBindings(NamedParameterJdbcTemplate jdbc, Clock clock, ApplicationEventPublisher events) {
        this.jdbc = jdbc;
        this.clock = clock;
        this.events = events;
    }

    /** 배정이 바뀐 워치를 알린다(S15P31A101-510 WatchBindingNotifier). 사유가 있으면 풀린 쪽이다 */
    private void changed(String deviceId, String unbindReason) {
        events.publishEvent(new WatchBindingChanged(deviceId, unbindReason));
    }

    /** 방금 끝낸 행들의 워치를 알린다. 새 배정을 받는 워치(keep)는 호출부가 따로 알리니 건너뛴다 */
    private void changedEnded(List<String> endedDevices, String keep, String reason) {
        for (String device : endedDevices) {
            if (!device.equals(keep)) {
                changed(device, reason);
            }
        }
    }

    /**
     * 워치를 World ID 에 잇는다. 그 워치나 그 World ID 의 지금 배정은 REPLACED 로 끝낸다.
     *
     * @param actorUserId 배정한 관리자(app_user.user_id)
     */
    @Transactional
    public Binding bindManually(String deviceId, String worldId, long actorUserId) {
        if (worldId == null || worldId.isBlank() || worldId.length() > 64) {
            throw new BusinessException(ErrorCode.Watch.INVALID_WORLD_ID);
        }
        Integer watches = jdbc.queryForObject("""
                SELECT count(*) FROM device WHERE device_id = :id AND kind = 'WATCH' AND revoked_at IS NULL
                """, new MapSqlParameterSource("id", deviceId), Integer.class);
        if (watches == null || watches == 0) {
            throw new BusinessException(ErrorCode.Watch.WATCH_NOT_FOUND);
        }
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("device", deviceId).addValue("world", worldId)
                .addValue("actor", actorUserId).addValue("now", now);
        List<String> ended = jdbc.queryForList("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'REPLACED'
                WHERE unbound_at IS NULL AND (device_id = :device OR world_id = :world) RETURNING device_id
                """, params, String.class);
        Long id;
        try {
            id = jdbc.queryForObject("""
                    INSERT INTO watch_binding (device_id, world_id, method, score, bound_by_user_id, bound_at)
                    VALUES (:device, :world, 'MANUAL', 1, :actor, :now) RETURNING binding_id
                    """, params, Long.class);
        } catch (DuplicateKeyException e) {
            // 같은 워치·World ID 를 동시에 이으려 했다(부분 유일 색인). 먼저 끝난 쪽이 이긴다
            throw new BusinessException(ErrorCode.Watch.BINDING_CONFLICT);
        }
        changedEnded(ended, deviceId, "REPLACED");
        changed(deviceId, null);
        return new Binding(id == null ? -1 : id, deviceId, worldId, "MANUAL", 1, now.toInstant(), null, null);
    }

    /**
     * 움직임 대조 배정(MOTION_MATCH, S15P31A101-340). 그 워치나 그 World ID 의 옛 대조 배정(임시 이어받기 포함)은 REPLACED 로 끝낸다.
     * 관리자 수동 배정(MANUAL, 확정된 것)이 걸린 워치·World ID 는 건드리지 않는다(관리자 덮어쓰기가 이긴다).
     *
     * @return 새로 이었으면 true. 수동 배정에 막혔거나 같은 순간 수동 배정이 들어와 부분 유일 색인에 걸렸으면 false
     */
    @Transactional
    public boolean bindByMotion(String deviceId, String worldId, double score) {
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("device", deviceId).addValue("world", worldId)
                .addValue("score", Math.max(0, Math.min(1, score)))
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC));
        Integer manual = jdbc.queryForObject("""
                SELECT count(*) FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.unbound_at IS NULL AND b.method = 'MANUAL' AND d.revoked_at IS NULL
                  AND NOT (b.handoff_from IS NOT NULL AND b.handoff_confirmed_at IS NULL)
                  AND (b.device_id = :device OR b.world_id = :world)
                """, params, Integer.class);
        if (manual != null && manual > 0) {
            return false;
        }
        List<String> ended = jdbc.queryForList("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'REPLACED'
                WHERE unbound_at IS NULL AND %s AND (device_id = :device OR world_id = :world) RETURNING device_id
                """.formatted(MOTION_OWNED), params, String.class);
        // 예외로 받으면 트랜잭션이 깨지니 부분 유일 색인 충돌은 행 없이 돌아오게 한다
        boolean bound = !jdbc.queryForList("""
                INSERT INTO watch_binding (device_id, world_id, method, score, bound_at)
                VALUES (:device, :world, 'MOTION_MATCH', :score, :now)
                ON CONFLICT DO NOTHING RETURNING binding_id
                """, params, Long.class).isEmpty();
        changedEnded(ended, bound ? deviceId : null, "REPLACED");
        if (bound) {
            changed(deviceId, null);
        }
        return bound;
    }

    /** 대조 배정과 임시 이어받기만 끝낸다(MATCH_LOST·IDENTITY_CHANGED). 확정된 수동 배정은 남는다. 끝낸 행 수 */
    public int unbindMotion(String deviceId, String worldId, String reason) {
        List<String> ended = jdbc.queryForList("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = :reason
                WHERE unbound_at IS NULL AND %s
                  AND (device_id = :device OR world_id = :world) RETURNING device_id
                """.formatted(MOTION_OWNED), new MapSqlParameterSource().addValue("device", deviceId).addValue("world", worldId)
                .addValue("reason", reason).addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC)),
                String.class);
        changedEnded(ended, null, reason);
        return ended.size();
    }

    /**
     * World ID 병합(world_state aliases): 사라진 {@code oldWorldId} 에 걸린 배정을 살아남은 {@code survivorId} 로 옮긴다
     * (S15P31A101-508). 새 행을 같은 워치·방식·점수로 더하고 옛 행은 ID_MERGED 로 닫는다(덮어쓰지 않는다).
     * <ul>
     *   <li>살아남은 ID 에 배정이 없으면 옮긴다(MOTION_MATCH·MANUAL 모두)</li>
     *   <li>옛 것이 MANUAL, 살아남은 것이 MOTION_MATCH 면 대조 배정을 REPLACED 로 닫고 수동을 옮긴다(관리자 선택이 이긴다)</li>
     *   <li>살아남은 ID 가 이미 MANUAL 이면 그대로 두고 옛 MANUAL 은 ID_MERGED 로 닫는다(옛 MOTION_MATCH 는 호출자가 푼다)</li>
     *   <li>둘 다 MOTION_MATCH 면 어느 쪽이 맞는지 모르니 옮기지 않는다(호출자가 둘 다 푼다)</li>
     * </ul>
     * 이어받기 표시(handoff_from·확정 시각)는 그대로 따라간다.
     *
     * @return 옮겼으면 옮긴 새 배정, 아니면 null
     */
    @Transactional
    public Binding followMerge(String oldWorldId, String survivorId) {
        if (oldWorldId.equals(survivorId)) {
            return null;
        }
        Binding old = activeFor(oldWorldId);
        if (old == null) {
            return null;
        }
        Binding survivor = activeFor(survivorId);
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("now", now).addValue("old", oldWorldId)
                .addValue("survivor", survivorId);
        boolean oldManual = "MANUAL".equals(old.method());
        if (survivor != null) {
            if ("MANUAL".equals(survivor.method())) {
                if (oldManual) {
                    changedEnded(jdbc.queryForList("""
                            UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'ID_MERGED'
                            WHERE unbound_at IS NULL AND world_id = :old RETURNING device_id
                            """, params, String.class), null, "ID_MERGED");
                }
                return null;
            }
            if (!oldManual) {
                return null;
            }
            changedEnded(jdbc.queryForList("""
                    UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'REPLACED'
                    WHERE unbound_at IS NULL AND world_id = :survivor RETURNING device_id
                    """, params, String.class), old.deviceId(), "REPLACED");
        }
        jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'ID_MERGED'
                WHERE unbound_at IS NULL AND world_id = :old
                """, params);
        params.addValue("device", old.deviceId()).addValue("method", old.method()).addValue("score", old.score());
        // 수동 배정의 배정자는 그대로 이어받는다(watch_binding_manual_actor_check)
        params.addValue("oldId", old.bindingId());
        // 이어받기 표시는 따라가되, 옛 ID 로 되돌아가는 병합(새 ID 가 옛 ID 에 흡수)이면 자기 자신을 가리키게 되므로 보통 배정으로 둔다
        boolean inserted = !jdbc.queryForList("""
                INSERT INTO watch_binding (device_id, world_id, method, score, bound_by_user_id, bound_at, handoff_from, handoff_confirmed_at)
                SELECT :device, :survivor, :method, :score, bound_by_user_id, :now,
                       CASE WHEN handoff_from = :survivor THEN NULL ELSE handoff_from END,
                       CASE WHEN handoff_from = :survivor THEN NULL ELSE handoff_confirmed_at END
                FROM watch_binding WHERE binding_id = :oldId
                ON CONFLICT DO NOTHING RETURNING binding_id
                """, params, Long.class).isEmpty();
        // 옮겼으면 같은 워치가 새 World ID 로 BOUND, 못 옮겼으면(동시 배정 충돌) 그 워치는 풀렸다(S15P31A101-510)
        changed(old.deviceId(), inserted ? null : "ID_MERGED");
        boolean backToOrigin = survivorId.equals(old.handoffFrom());
        return inserted ? new Binding(-1, old.deviceId(), survivorId, old.method(), old.score(), now.toInstant(),
                backToOrigin ? null : old.handoffFrom(), backToOrigin ? null : old.handoffConfirmedAt()) : null;
    }

    /**
     * 자리 이어받기(S15P31A101-793): 사라진 {@code vanishedWorldId} 의 배정(수동·대조·이어받기)을 그 자리에 나타난 {@code newWorldId} 로
     * 옮긴다. 새 행은 같은 워치·방식·점수(수동은 배정자도)에 {@code handoff_from} 을 옛 ID 로 적어 임시로 더하고, 옛 행은 HANDOFF 로 닫는다.
     * 새 ID 에 이미 배정이 있으면 옮기지 않는다(호출자가 배정 없는 트랙만 고른다. 같은 순간 수동 배정이 들어온 경우가 여기 걸린다).
     *
     * @return 옮겼으면 옮긴 새 배정, 아니면 null
     */
    @Transactional
    public Binding handoff(String vanishedWorldId, String newWorldId) {
        if (vanishedWorldId.equals(newWorldId)) {
            return null;
        }
        Binding old = activeFor(vanishedWorldId);
        if (old == null || activeFor(newWorldId) != null) {
            return null;
        }
        OffsetDateTime now = OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC);
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("now", now).addValue("old", vanishedWorldId)
                .addValue("world", newWorldId).addValue("device", old.deviceId()).addValue("method", old.method())
                .addValue("score", old.score()).addValue("oldId", old.bindingId());
        // 읽은 그 행만 닫는다. 읽은 뒤 관리자가 풀었거나 바꿨으면(REST 스레드와 경합) 0행이라 옮기지 않는다: 닫힌 배정을 되살리지 않는다
        int closed = jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'HANDOFF'
                WHERE unbound_at IS NULL AND binding_id = :oldId
                """, params);
        if (closed == 0) {
            return null;
        }
        boolean inserted = !jdbc.queryForList("""
                INSERT INTO watch_binding (device_id, world_id, method, score, bound_by_user_id, bound_at, handoff_from)
                SELECT :device, :world, :method, :score, bound_by_user_id, :now, :old
                FROM watch_binding WHERE binding_id = :oldId
                ON CONFLICT DO NOTHING RETURNING binding_id
                """, params, Long.class).isEmpty();
        if (!inserted) {
            // 같은 순간 새 ID 에 다른 배정이 들어왔다(부분 유일 색인). 옛 배정을 닫은 채 두면 관리자 배정이 대체 없이 사라지므로 되돌린다
            TransactionAspectSupport.currentTransactionStatus().setRollbackOnly();
            return null;
        }
        // 같은 워치가 새 World ID 로 BOUND(워치는 커밋 뒤 배정 알림으로 안다)
        changed(old.deviceId(), null);
        return new Binding(-1, old.deviceId(), newWorldId, old.method(), old.score(), now.toInstant(), vanishedWorldId, null);
    }

    /**
     * 임시 이어받기를 확정한다(걸음 대조가 같은 짝을 다시 뽑았다). 확정되면 수동 배정은 다시 관리자 것이 되고 대조 배정은 보통 대조 배정이다.
     *
     * @return 확정했으면 true(이미 확정됐거나 임시가 아니면 false)
     */
    public boolean confirmHandoff(String deviceId, String worldId) {
        return jdbc.update("""
                UPDATE watch_binding SET handoff_confirmed_at = :now
                WHERE unbound_at IS NULL AND device_id = :device AND world_id = :world
                  AND handoff_from IS NOT NULL AND handoff_confirmed_at IS NULL
                """, new MapSqlParameterSource().addValue("device", deviceId).addValue("world", worldId)
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC))) > 0;
    }

    private Binding activeFor(String worldId) {
        List<Binding> rows = jdbc.query("SELECT " + COLUMNS + """
                 FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.unbound_at IS NULL AND d.revoked_at IS NULL AND b.world_id = :world
                """, new MapSqlParameterSource("world", worldId), ROW);
        return rows.isEmpty() ? null : rows.get(0);
    }

    /** 그 워치의 지금 배정을 RELEASED 로 끝낸다. 없으면 BINDING_NOT_FOUND */
    public void release(String deviceId) {
        int rows = jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'RELEASED' WHERE device_id = :device AND unbound_at IS NULL
                """, new MapSqlParameterSource().addValue("device", deviceId)
                .addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC)));
        if (rows == 0) {
            throw new BusinessException(ErrorCode.Watch.BINDING_NOT_FOUND);
        }
        changed(deviceId, "RELEASED");
    }

    /** 그 워치의 지금 배정(폐기된 워치면 없음) */
    public Optional<Binding> activeForDevice(String deviceId) {
        return jdbc.query("SELECT " + COLUMNS + """
                 FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.device_id = :device AND b.unbound_at IS NULL AND d.revoked_at IS NULL
                """, new MapSqlParameterSource("device", deviceId), ROW).stream().findFirst();
    }

    /** 지금 배정 전부, device_id 오름차순. 폐기된 워치의 배정은 뺀다(보낼 수 없다) */
    public List<Binding> active() {
        return jdbc.query("SELECT " + COLUMNS + """
                 FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.unbound_at IS NULL AND d.revoked_at IS NULL ORDER BY b.device_id
                """, ROW);
    }

    /** 그 World ID 들에 지금 배정된 워치(폐기된 워치 제외), World ID → 기기 ID. 배정 없는 World ID 는 빠진다 */
    Map<String, String> devicesFor(List<String> worldIds) {
        Map<String, String> out = new LinkedHashMap<>();
        jdbc.query("""
                SELECT b.world_id, b.device_id FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.world_id IN (:worlds) AND b.unbound_at IS NULL AND d.revoked_at IS NULL
                """, new MapSqlParameterSource("worlds", worldIds),
                (RowCallbackHandler) rs -> {
                    out.put(rs.getString("world_id"), rs.getString("device_id"));
                });
        return out;
    }
}
