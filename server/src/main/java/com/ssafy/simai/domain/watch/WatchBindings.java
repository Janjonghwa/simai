package com.ssafy.simai.domain.watch;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.Clock;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.LinkedHashMap;
import java.util.Map;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.RowCallbackHandler;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 익명 World ID ↔ 워치 배정(watch_binding, V35, 확장 C.6). 움직임 대조(MOTION_MATCH, S15P31A101-340 WatchMotionMatcher)가 기본이고,
 * 관리자 수동 배정(MANUAL, -338)은 덮어쓰기로 남는다: 수동 배정이 걸린 워치·World ID 는 대조가 건드리지 않는다.
 * 1:1: 지금 배정은 워치마다 하나, World ID 마다 하나다.
 * 사람 이름은 두지 않는다(11.4-5). 배정은 풀 때까지 이어지고 워치가 끊겨도 남는다(끊긴 동안 채널은 LOST).
 */
@Service
public class WatchBindings {

    /** 지금 배정 하나 */
    /** score 는 대조 신뢰도(0~1), MANUAL 은 1 */
    public record Binding(long bindingId, String deviceId, String worldId, String method, double score, Instant boundAt) {
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final Clock clock;

    WatchBindings(NamedParameterJdbcTemplate jdbc, Clock clock) {
        this.jdbc = jdbc;
        this.clock = clock;
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
        jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'REPLACED'
                WHERE unbound_at IS NULL AND (device_id = :device OR world_id = :world)
                """, params);
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
        return new Binding(id == null ? -1 : id, deviceId, worldId, "MANUAL", 1, now.toInstant());
    }

    /**
     * 움직임 대조 배정(MOTION_MATCH, S15P31A101-340). 그 워치나 그 World ID 의 옛 대조 배정은 REPLACED 로 끝낸다.
     * 관리자 수동 배정(MANUAL)이 걸린 워치·World ID 는 건드리지 않는다(관리자 덮어쓰기가 이긴다).
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
                  AND (b.device_id = :device OR b.world_id = :world)
                """, params, Integer.class);
        if (manual != null && manual > 0) {
            return false;
        }
        jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = 'REPLACED'
                WHERE unbound_at IS NULL AND method = 'MOTION_MATCH' AND (device_id = :device OR world_id = :world)
                """, params);
        // 예외로 받으면 트랜잭션이 깨지니 부분 유일 색인 충돌은 행 없이 돌아오게 한다
        return !jdbc.queryForList("""
                INSERT INTO watch_binding (device_id, world_id, method, score, bound_at)
                VALUES (:device, :world, 'MOTION_MATCH', :score, :now)
                ON CONFLICT DO NOTHING RETURNING binding_id
                """, params, Long.class).isEmpty();
    }

    /** 대조 배정만 끝낸다(MATCH_LOST·IDENTITY_CHANGED). 수동 배정은 남는다. 끝낸 행 수 */
    public int unbindMotion(String deviceId, String worldId, String reason) {
        return jdbc.update("""
                UPDATE watch_binding SET unbound_at = :now, unbind_reason = :reason
                WHERE unbound_at IS NULL AND method = 'MOTION_MATCH'
                  AND (device_id = :device OR world_id = :world)
                """, new MapSqlParameterSource().addValue("device", deviceId).addValue("world", worldId)
                .addValue("reason", reason).addValue("now", OffsetDateTime.ofInstant(clock.instant(), ZoneOffset.UTC)));
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
    }

    /** 지금 배정 전부, device_id 오름차순. 폐기된 워치의 배정은 뺀다(보낼 수 없다) */
    public List<Binding> active() {
        return jdbc.query("""
                SELECT b.binding_id, b.device_id, b.world_id, b.method, b.score, b.bound_at
                FROM watch_binding b JOIN device d ON d.device_id = b.device_id
                WHERE b.unbound_at IS NULL AND d.revoked_at IS NULL ORDER BY b.device_id
                """, (rs, i) -> new Binding(rs.getLong("binding_id"), rs.getString("device_id"), rs.getString("world_id"),
                rs.getString("method"), rs.getDouble("score"), rs.getTimestamp("bound_at").toInstant()));
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
