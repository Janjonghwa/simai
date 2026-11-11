package com.ssafy.simai.domain.event;

import com.ssafy.simai.domain.tier0.event.Tier0HeldEvents;
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
 * escalations[] 는 Tier 0 저널 전이(UPDATED escalation)로 반영된 상향 기록이다(S15P31A101-276).
 * held_by_tier0 는 지금 Tier 0 가 그 이벤트를 활성으로 들고 있는지(Tier0HeldEvents, 저장하지 않는 파생값, S15P31A101-595)다.
 * 관제 화면은 해소 전이면서 이 값이 true 인 이벤트만 「진행 중」으로 보이고, 나머지 해소 전 이벤트는 「지난 관제 · 확인 필요」로 내린다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class EventQueryRepository {

    private static final String COLUMNS = """
            event_id, source, gateway_id, rule_id, rule_version, lifecycle, verification, severity, confidence, needs_check,
            confidence_reasons, world_ids, zone_id, metrics, rationale, proposed_severity, resume_count, event_sequence,
            config_version, calibration_version, scene_version, raised_at, updated_at, resolved_at, delivery,
            related_event_id, resolution, escalations
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final Tier0HeldEvents held;

    public EventQueryRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, Tier0HeldEvents held) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.held = held;
    }

    /**
     * 최신 발생순. 발생 시각을 모르는 이벤트(RAISED 보다 뒤 전이가 먼저 왔거나 스냅샷으로 만든 것)는 마지막 갱신 시각으로 보고,
     * 그것도 없으면 앞에 둔다. 관제 앱은 첫 페이지만 읽으므로 활성 경보가 뒤로 밀려 안 보이면 안 된다. LIVE 만
     */
    public List<ObjectNode> findPage(int page, int size) {
        List<ObjectNode> events = jdbc.query("SELECT " + COLUMNS + " FROM risk_event WHERE source = 'LIVE'"
                + " ORDER BY COALESCE(raised_at, updated_at) DESC NULLS FIRST, event_id"
                + " LIMIT :size OFFSET :offset",
                new MapSqlParameterSource().addValue("size", size).addValue("offset", (long) page * size),
                (rs, i) -> toPayload(rs));
        attachAcks(events);
        attachSummaries(events);
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
        attachSummaries(events);
        return events.stream().findFirst();
    }

    /**
     * 이벤트 상세(명세 12.6 GET /api/events/{id}, S15P31A101-320 히스토리). findOne 의 칸에 더한다:
     * <ul>
     *   <li>zone: 관련 구역의 이름·종류·판정 방식·등급. 이벤트가 난 시각(raised_at, 모르면 목록 정렬과 같이 updated_at)에 유효했던 판이다(구역은 새 판을 더한다, 11.4).
     *       구역이 없으면(구역 밖 규칙, zone_id 도 null) null. zone_id 는 있는데 null 이면 그 시각에 유효했던 판을 찾지 못한 것이다.
     *   <li>transitions: Tier 0 전이 기록(risk_event_update) event_sequence 순. delivery 는 그 전이의 재동기화 복원 여부(3.2, 계약 risk_event.delivery 와 같은 값)다.
     *   <li>assessments: VLM 평가 기록(vlm_assessment) 최신순(S15P31A101-276).
     * </ul>
     */
    public Optional<ObjectNode> detail(UUID eventId) {
        Optional<ObjectNode> found = findOne(eventId);
        found.ifPresent(event -> {
            MapSqlParameterSource params = new MapSqlParameterSource("id", eventId);
            event.set("zone", jdbc.query("""
                    SELECT z.zone_id, z.zone_version, z.name, z.zone_kind, z.eval_type, z.severity
                    FROM risk_event e JOIN zone z ON z.zone_id = e.zone_id
                    WHERE e.event_id = :id AND z.valid_from <= COALESCE(e.raised_at, e.updated_at, now())
                      AND (z.valid_to IS NULL OR z.valid_to > COALESCE(e.raised_at, e.updated_at, now()))
                    ORDER BY z.zone_version DESC LIMIT 1
                    """, params, (rs, i) -> {
                ObjectNode zone = mapper.createObjectNode();
                zone.put("zone_id", rs.getString("zone_id"));
                zone.put("zone_version", rs.getInt("zone_version"));
                zone.put("name", rs.getString("name"));
                zone.put("zone_kind", rs.getString("zone_kind"));
                zone.put("eval_type", rs.getString("eval_type"));
                zone.put("severity", rs.getString("severity"));
                return (JsonNode) zone;
            }).stream().findFirst().orElse(mapper.nullNode()));
            ArrayNode transitions = event.putArray("transitions");
            jdbc.query("""
                    SELECT event_sequence, transition, occurred_at, observation_at, severity, verification, confidence, needs_check,
                        resumed, resent, delivery_mode, ack_id, close_id, escalation_id
                    FROM risk_event_update WHERE event_id = :id ORDER BY event_sequence
                    """, params, rs -> {
                ObjectNode t = transitions.addObject();
                t.put("event_sequence", rs.getInt("event_sequence"));
                t.put("transition", rs.getString("transition"));
                t.put("occurred_at", rs.getTimestamp("occurred_at").getTime());
                t.put("observation_at", rs.getTimestamp("observation_at").getTime());
                t.put("severity", rs.getString("severity"));
                t.put("verification", rs.getString("verification"));
                t.put("confidence", rs.getString("confidence"));
                t.put("needs_check", rs.getBoolean("needs_check"));
                t.put("resumed", rs.getBoolean("resumed"));
                t.put("resent", rs.getBoolean("resent"));
                t.put("delivery", rs.getString("delivery_mode"));
                putIfPresent(t, "ack_id", rs.getString("ack_id"));
                putIfPresent(t, "close_id", rs.getString("close_id"));
                putIfPresent(t, "escalation_id", rs.getString("escalation_id"));
            });
            ArrayNode assessments = event.putArray("assessments");
            jdbc.query("""
                    SELECT assessment_id, event_sequence, job_id, job_type, model_version, rule_severity,
                        text, clause_ids, proposed_severity, decision, decided_by, decided_at, created_at
                    FROM vlm_assessment WHERE event_id = :id ORDER BY created_at DESC, assessment_id DESC
                    """, params, rs -> {
                ObjectNode a = assessments.addObject();
                a.put("assessment_id", rs.getLong("assessment_id"));
                a.put("event_sequence", rs.getInt("event_sequence"));
                a.put("job_id", rs.getString("job_id"));
                a.put("job_type", rs.getString("job_type"));
                a.put("model_version", rs.getString("model_version"));
                a.put("rule_severity", rs.getString("rule_severity"));
                a.put("text", rs.getString("text"));
                putTextArray(a, "clause_ids", rs.getArray("clause_ids"));
                a.put("proposed_severity", rs.getString("proposed_severity"));
                a.put("decision", rs.getString("decision"));
                long decidedBy = rs.getLong("decided_by");
                if (!rs.wasNull()) {
                    a.put("decided_by", decidedBy);
                }
                putMillis(a, "decided_at", rs.getTimestamp("decided_at"));
                putMillis(a, "created_at", rs.getTimestamp("created_at"));
            });
        });
        return found;
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

    private void attachSummaries(List<ObjectNode> events) {
        if (events.isEmpty()) {
            return;
        }
        Map<String, ArrayNode> byEvent = new LinkedHashMap<>();
        for (ObjectNode event : events) {
            byEvent.put(event.get("event_id").asString(), event.putArray("summaries"));
        }
        Collection<UUID> ids = byEvent.keySet().stream().map(UUID::fromString).toList();
        jdbc.query("""
                SELECT summary_id, event_id, job_id, status, requested_at, finished_at, summary,
                    evidence_timestamps_ms, window_start_ms, window_end_ms, error_code
                FROM incident_summary
                WHERE event_id IN (:ids)
                ORDER BY requested_at DESC, summary_id DESC
                """, new MapSqlParameterSource("ids", ids), rs -> {
            ObjectNode s = byEvent.get(rs.getString("event_id")).addObject();
            IncidentSummaryService.mapSummaryRow(rs, s);
        });
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
            // 계약(operator_ack)의 현장 버튼 ↔ SITE 짝이 틀린 행은 빼서, 행 하나 때문에 항목 전체가 관제 앱 검사에서 버려지지 않게 한다
            if ("SITE_BUTTON".equals(rs.getString("channel")) != "SITE".equals(rs.getString("actor_role"))) {
                return;
            }
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

    /**
     * 관제 앱 스키마(types.ts)에서 선택 항목인 칸은 값이 없으면 빼고 보낸다(null 은 검증에 실패한다).
     * rationale·zone_id·proposed_severity 는 필수·null 허용이라 null 로 싣는다.
     */
    private ObjectNode toPayload(ResultSet rs) throws SQLException {
        ObjectNode e = mapper.createObjectNode();
        e.put("event_id", rs.getString("event_id"));
        e.put("source", rs.getString("source"));
        putIfPresent(e, "gateway_id", rs.getString("gateway_id"));
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
        e.set("metrics", rs.getString("metrics") == null ? mapper.createObjectNode() : mapper.readTree(rs.getString("metrics")));
        e.put("rationale", rs.getString("rationale"));
        e.put("proposed_severity", rs.getString("proposed_severity"));
        e.put("resume_count", rs.getInt("resume_count"));
        int sequence = rs.getInt("event_sequence");
        if (!rs.wasNull()) {
            e.put("event_sequence", sequence);
        }
        putIfPresent(e, "config_version", rs.getString("config_version"));
        putIfPresent(e, "calibration_version", rs.getString("calibration_version"));
        putIfPresent(e, "scene_version", rs.getString("scene_version"));
        putMillis(e, "raised_at", rs.getTimestamp("raised_at"));
        putMillis(e, "updated_at", rs.getTimestamp("updated_at"));
        putMillis(e, "resolved_at", rs.getTimestamp("resolved_at"));
        putIfPresent(e, "delivery", rs.getString("delivery"));
        putIfPresent(e, "related_event_id", rs.getString("related_event_id"));
        if (rs.getString("resolution") != null) {
            e.set("resolution", mapper.readTree(rs.getString("resolution")));
        }
        if (rs.getString("escalations") != null) {
            JsonNode escalations = mapper.readTree(rs.getString("escalations"));
            if (escalations.isArray() && !escalations.isEmpty()) {
                e.set("escalations", escalations);
            }
        }
        // 계약 risk_event 2.3.0: true 는 LIVE 의 RAISED·UPDATED 에만. 해소된 이벤트는 집합에 남아 있어도 false 다
        e.put("held_by_tier0", !"RESOLVED".equals(rs.getString("lifecycle"))
                && held.isHeld(rs.getString("gateway_id"), UUID.fromString(rs.getString("event_id"))));
        return e;
    }

    private static void putIfPresent(ObjectNode node, String field, String value) {
        if (value != null) {
            node.put(field, value);
        }
    }

    private static void putMillis(ObjectNode node, String field, Timestamp time) {
        if (time != null) {
            node.put(field, time.getTime());
        }
    }

    private static void putTextArray(ObjectNode node, String field, Array array) throws SQLException {
        if (array == null) {
            return;
        }
        ArrayNode out = node.putArray(field);
        for (Object value : (Object[]) array.getArray()) {
            out.add(value == null ? null : value.toString());
        }
    }
}
