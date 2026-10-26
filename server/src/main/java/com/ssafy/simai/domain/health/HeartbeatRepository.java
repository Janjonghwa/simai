package com.ssafy.simai.domain.health;

import com.ssafy.simai.domain.tier0.i1.I1Json;
import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 노트북 하트비트 기록(V1 device_heartbeat, hypertable, 추가만)과 기기의 최근 수신 시각(device.last_heartbeat_at).
 * 노트북 시계(sent_at)와 EC2 수신 시각(received_at)을 함께 두어 시계 차이를 나중에 볼 수 있다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class HeartbeatRepository {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    HeartbeatRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    String kindOf(String deviceId) {
        return jdbc.queryForList("SELECT kind FROM device WHERE device_id = :id", new MapSqlParameterSource("id", deviceId),
                String.class).stream().findFirst().orElse(null);
    }

    void insert(String deviceId, Instant sentAt, JsonNode p, Instant receivedAt) {
        JsonNode tier0 = p.get("tier0");
        JsonNode ec2 = p.get("ec2");
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("receivedAt", utc(receivedAt)).addValue("deviceId", deviceId).addValue("sentAt", utc(sentAt))
                .addValue("seq", p.get("heartbeat_seq").longValue())
                .addValue("control", tier0.get("control").stringValue())
                .addValue("lastMessageAt", I1Json.time(tier0, "last_message_at"))
                .addValue("cameras", I1Json.textArray(tier0, "video_cameras"))
                .addValue("ec2State", ec2 == null ? null : I1Json.text(ec2, "state"))
                .addValue("ec2LastResponseAt", ec2 == null ? null : I1Json.time(ec2, "last_response_at"))
                .addValue("siteDisplay", p.get("site_display").stringValue())
                .addValue("alarm", json(p.get("alarm"))).addValue("cameraLinks", json(p.get("cameras")));
        jdbc.update("""
                INSERT INTO device_heartbeat (received_at, device_id, sent_at, heartbeat_seq, tier0_control, tier0_last_message_at,
                    tier0_video_cameras, ec2_state, site_display, alarm, cameras, ec2_last_response_at)
                VALUES (:receivedAt, :deviceId, :sentAt, :seq, :control, :lastMessageAt, CAST(:cameras AS text[]), :ec2State,
                    :siteDisplay, CAST(:alarm AS jsonb), CAST(:cameraLinks AS jsonb), :ec2LastResponseAt)
                ON CONFLICT DO NOTHING
                """, params);
        jdbc.update("UPDATE device SET last_heartbeat_at = :receivedAt WHERE device_id = :deviceId", params);
    }

    private String json(JsonNode node) {
        return node == null || node.isNull() ? null : mapper.writeValueAsString(node);
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
