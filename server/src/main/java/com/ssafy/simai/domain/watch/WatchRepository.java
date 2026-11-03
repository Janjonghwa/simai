package com.ssafy.simai.domain.watch;

import java.sql.Timestamp;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import org.springframework.transaction.annotation.Transactional;

/**
 * 워치 기기(device kind WATCH)와 연결 기록 watch_connection(V28, S15P31A101-336). 하트비트마다 쓰는 것은 device.last_heartbeat_at 하나다.
 */
@Repository
class WatchRepository {

    /** 등록된 워치 하나(폐기 안 됨) */
    record RegisteredWatch(String deviceId, Instant lastHeartbeatAt) {
    }

    /**
     * 끝나는 연결 c 의 마지막 하트비트. device.last_heartbeat_at 이 이 연결이 열린 뒤 값일 때만 이 연결의 것이다
     * (하트비트를 못 받고 끝난 연결에 앞 연결의 시각을 적지 않게)
     */
    private static final String LAST_OF_THIS_CONNECTION = """
            (SELECT d.last_heartbeat_at FROM device d WHERE d.device_id = c.device_id AND d.last_heartbeat_at >= c.connected_at)""";

    private final NamedParameterJdbcTemplate jdbc;

    WatchRepository(NamedParameterJdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    String deviceKind(String deviceId) {
        return jdbc.queryForList("SELECT kind FROM device WHERE device_id = :id", new MapSqlParameterSource("id", deviceId),
                String.class).stream().findFirst().orElse(null);
    }

    /** 옛 열린 연결을 REPLACED 로 닫고 새 연결 행을 만든다(워치 하나에 열린 연결 하나, 부분 유일 색인) */
    @Transactional
    long open(String deviceId, Instant connectedAt) {
        MapSqlParameterSource params = new MapSqlParameterSource().addValue("id", deviceId).addValue("now", utc(connectedAt));
        jdbc.update("""
                UPDATE watch_connection c SET ended_at = :now, end_reason = 'REPLACED',
                       last_heartbeat_at = """ + LAST_OF_THIS_CONNECTION + """
                WHERE device_id = :id AND ended_at IS NULL
                """, params);
        Long id = jdbc.queryForObject("""
                INSERT INTO watch_connection (device_id, connected_at) VALUES (:id, :now) RETURNING connection_id
                """, params, Long.class);
        return id == null ? -1 : id;
    }

    /**
     * 하트비트를 받은 시각을 적는다. 이 연결을 연 토큰이 아직 그 워치의 폐기 안 된 토큰일 때만 적는다(연결 중 폐기·회전을 잡는다).
     *
     * @return 토큰이 아직 유효하면 true
     */
    boolean touch(String deviceId, String tokenHash, Instant receivedAt) {
        return jdbc.update("""
                UPDATE device SET last_heartbeat_at = :at
                WHERE device_id = :id AND token_hash = :hash AND kind = 'WATCH' AND revoked_at IS NULL
                """, new MapSqlParameterSource().addValue("id", deviceId).addValue("hash", tokenHash)
                .addValue("at", utc(receivedAt))) > 0;
    }

    /** 첫 하트비트와 전송 경로·앱 버전(바뀔 때만 부른다) */
    void describe(long connectionId, Instant firstHeartbeatAt, String transport, String appVersion) {
        jdbc.update("""
                UPDATE watch_connection SET first_heartbeat_at = COALESCE(first_heartbeat_at, :at), transport = :transport,
                       app_version = :version
                WHERE connection_id = :cid
                """, new MapSqlParameterSource().addValue("cid", connectionId).addValue("at", utc(firstHeartbeatAt))
                .addValue("transport", transport).addValue("version", appVersion));
    }

    void close(long connectionId, Instant endedAt, String reason, Instant lastHeartbeatAt) {
        jdbc.update("""
                UPDATE watch_connection SET ended_at = :now, end_reason = :reason, last_heartbeat_at = :last
                WHERE connection_id = :cid AND ended_at IS NULL
                """, new MapSqlParameterSource().addValue("cid", connectionId).addValue("now", utc(endedAt))
                .addValue("reason", reason).addValue("last", lastHeartbeatAt == null ? null : utc(lastHeartbeatAt)));
    }

    /**
     * EC2 가 다시 뜨면 전에 열린 연결은 모두 끝난 것이다. 끝 시각은 기동 시각이 아니라 마지막으로 살아 있던 때(이 연결의 마지막 하트비트,
     * 없으면 연결 시각)로 적는다. EC2 가 내려가 있던 시간을 연결 시간에 넣지 않는다(-341 연결 비율)
     */
    int closeAllOpen() {
        return jdbc.update("""
                UPDATE watch_connection c SET end_reason = 'EC2_RESTART',
                       last_heartbeat_at = """ + LAST_OF_THIS_CONNECTION + """
                       , ended_at = COALESCE(""" + LAST_OF_THIS_CONNECTION + """
                       , c.connected_at)
                WHERE ended_at IS NULL
                """, new MapSqlParameterSource());
    }

    /** 폐기되지 않은 워치, device_id 오름차순 */
    List<RegisteredWatch> registered() {
        return jdbc.query("""
                SELECT device_id, last_heartbeat_at FROM device WHERE kind = 'WATCH' AND revoked_at IS NULL ORDER BY device_id
                """, (rs, i) -> {
            Timestamp last = rs.getTimestamp("last_heartbeat_at");
            return new RegisteredWatch(rs.getString("device_id"), last == null ? null : last.toInstant());
        });
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
