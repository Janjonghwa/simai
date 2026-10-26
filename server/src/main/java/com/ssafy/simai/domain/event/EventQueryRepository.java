package com.ssafy.simai.domain.event;

import java.sql.Array;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.OffsetDateTime;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 관제 화면이 읽는 LIVE 이벤트(risk_event)와 확인 기록(event_ack). 응답 항목은 계약 risk_event payload 와 같은 snake_case 이고
 * 시각은 UTC ms 다(명세 12.4·12.6, 관제 앱 features/events/types.ts). acks[] 는 event_ack 에서 만든다(risk_event 에 칸이 없다).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventQueryRepository {

    private static final String COLUMNS = """
            event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence, needs_check,
            confidence_reasons, world_ids, zone_id, metrics, rationale, proposed_severity, resume_count, event_sequence,
            config_version, calibration_version, scene_version, raised_at, updated_at, resolved_at, delivery,
            related_event_id, resolution
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public EventQueryRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 최신 발생순(raised_at, 모르면 뒤로). LIVE 만 */
    public List<ObjectNode> findPage(int page, int size) {
        List<ObjectNode> events = jdbc.query("SELECT " + COLUMNS + " FROM risk_event WHERE source = 'LIVE'"
                + " ORDER BY raised_at DESC NULLS LAST, updated_at DESC NULLS LAST, event_id"
                + " LIMIT :size OFFSET :offset",
                new MapSqlParameterSource().addValue("size", size).addValue("offset", (long) page * size),
                (rs, i) -> toPayload(rs));
        attachAcks(events);
        return events;
    }

    public long count() {
        Long count = jdbc.queryForObject("SELECT count(*) FROM risk_event WHERE source = 'LIVE'", Map.of(), Long.class);
        return count == null ? 0 : count;
    }

    public Optional<ObjectNode> findOne(UUID eventId) {
        List<ObjectNode> events = jdbc.query("SELECT " + COLUMNS + " FROM risk_event WHERE source = 'LIVE' AND event_id = :id",
                new MapSqlParameterSource("id", eventId), (rs, i) -> toPayload(rs));
        attachAcks(events);
        return events.stream().findFirst();
    }

    /** 확인 기록 하나(EC2 가 만든 확인). target 은 화면이 본 event_sequence */
    public void insertAck(UUID ackId, UUID eventId, Integer target, String channel, String actorRole, long userId,
            OffsetDateTime ackedAt) {
        jdbc.update("""
                INSERT INTO event_ack (ack_id, event_id, target_event_sequence, channel, actor_role, actor_user_id, acked_at)
                VALUES (:ackId, :eventId, :target, :channel, :role, :userId, :ackedAt)
                """, new MapSqlParameterSource()
                .addValue("ackId", ackId).addValue("eventId", eventId).addValue("target", target)
                .addValue("channel", channel).addValue("role", actorRole).addValue("userId", userId)
                .addValue("ackedAt", ackedAt));
    }

    private void attachAcks(List<ObjectNode> events) {
        if (events.isEmpty()) {
            return;
        }
        Map<String, ArrayNode> byEvent = new LinkedHashMap<>();
        for (ObjectNode event : events) {
            byEvent.put(event.get("event_id").asString(), event.putArray("acks"));
        }
        Collection<UUID> ids = byEvent.keySet().stream().map(UUID::fromString).toList();
        jdbc.query("""
                SELECT event_id, ack_id, channel, actor_role, actor_user_id, actor_device_id, acked_at
                FROM event_ack WHERE event_id IN (:ids) ORDER BY acked_at, ack_id
                """, new MapSqlParameterSource("ids", ids), rs -> {
            ObjectNode ack = byEvent.get(rs.getString("event_id")).addObject();
            ack.put("ack_id", rs.getString("ack_id"));
            ack.put("channel", rs.getString("channel"));
            ack.put("actor_role", rs.getString("actor_role"));
            ack.put("actor_id", actorId(rs));
            ack.put("acked_at", rs.getTimestamp("acked_at").getTime());
        });
    }

    /** 계약 operator_ack.actor_id: 계정 ID(사람 이름 금지). 관제·반장은 user_id, 현장 버튼은 기기 ID */
    private static String actorId(ResultSet rs) throws SQLException {
        long userId = rs.getLong("actor_user_id");
        if (!rs.wasNull()) {
            return String.valueOf(userId);
        }
        String deviceId = rs.getString("actor_device_id");
        return deviceId == null ? "unknown" : deviceId;
    }

    private ObjectNode toPayload(ResultSet rs) throws SQLException {
        ObjectNode e = mapper.createObjectNode();
        e.put("event_id", rs.getString("event_id"));
        e.put("source", rs.getString("source"));
        e.put("gateway_id", rs.getString("gateway_id"));
        e.put("rule_id", rs.getString("rule_id"));
        e.put("rule_version", rs.getInt("rule_version"));
        e.put("lifecycle", rs.getString("lifecycle"));
        e.put("verification", rs.getString("verification"));
        e.put("severity", rs.getString("severity"));
        e.put("confidence", rs.getString("confidence"));
        e.put("needs_check", rs.getBoolean("needs_check"));
        putTextArray(e, "confidence_reasons", rs.getArray("confidence_reasons"));
        putTextArray(e, "world_ids", rs.getArray("world_ids"));
        e.put("zone_id", rs.getString("zone_id"));
        e.set("metrics", json(rs.getString("metrics")));
        e.put("rationale", rs.getString("rationale"));
        e.put("proposed_severity", rs.getString("proposed_severity"));
        e.put("resume_count", rs.getInt("resume_count"));
        int sequence = rs.getInt("event_sequence");
        if (!rs.wasNull()) {
            e.put("event_sequence", sequence);
        }
        e.put("config_version", rs.getString("config_version"));
        e.put("calibration_version", rs.getString("calibration_version"));
        e.put("scene_version", rs.getString("scene_version"));
        putMillis(e, "raised_at", rs.getTimestamp("raised_at"));
        putMillis(e, "updated_at", rs.getTimestamp("updated_at"));
        putMillis(e, "resolved_at", rs.getTimestamp("resolved_at"));
        e.put("delivery", rs.getString("delivery"));
        e.put("related_event_id", rs.getString("related_event_id"));
        e.set("resolution", json(rs.getString("resolution")));
        return e;
    }

    private JsonNode json(String text) {
        return text == null ? mapper.nullNode() : mapper.readTree(text);
    }

    private static void putMillis(ObjectNode node, String field, Timestamp time) {
        if (time == null) {
            node.putNull(field);
        } else {
            node.put(field, time.getTime());
        }
    }

    private static void putTextArray(ObjectNode node, String field, Array array) throws SQLException {
        if (array == null) {
            node.putNull(field);
            return;
        }
        ArrayNode out = node.putArray(field);
        for (Object value : (Object[]) array.getArray()) {
            out.add(value == null ? null : value.toString());
        }
    }
}
