package com.ssafy.simai.domain.report;

import java.time.Instant;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.List;
import java.util.HashMap;
import java.util.Map;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/** 종료 트랜잭션 안에서 사실을 고정하고 기존 보고서 본문을 새 버전에 복사한다. */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkWrapUpRepository {
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public WorkWrapUpRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    public ObjectNode prepare(long workId, ObjectNode work, Instant now) {
        ObjectNode result = mapper.createObjectNode();
        List<Long> generating = jdbc.queryForList("""
                SELECT report_id FROM report WHERE work_id = :work AND report_kind = 'TBM' AND status = 'GENERATING'
                ORDER BY report_version DESC LIMIT 1
                """, new MapSqlParameterSource("work", workId), Long.class);
        if (!generating.isEmpty()) {
            return result.put("status", "SKIPPED").put("report_id", generating.getFirst()).put("reason", "REPORT_GENERATING");
        }
        List<Long> ready = jdbc.queryForList("""
                SELECT report_id FROM report WHERE work_id = :work AND report_kind = 'TBM' AND status = 'SUCCEEDED'
                ORDER BY report_version DESC LIMIT 1
                """, new MapSqlParameterSource("work", workId), Long.class);
        if (ready.isEmpty()) {
            result.put("status", "SKIPPED").putNull("report_id").put("reason", "NO_REPORT");
            return result;
        }
        ObjectNode facts = aggregate(work);
        Long id = jdbc.queryForObject("""
                INSERT INTO report (report_kind, work_id, layout_id, layout_revision, report_version, status,
                    work_type, rule_version, risk_items, zone_history, sections, citations, subgraph, steps,
                    llm_model, created_at, wrap_up_status, wrap_up_facts, form, form_revision, leader_user_id)
                SELECT report_kind, work_id, layout_id, layout_revision,
                    (SELECT MAX(report_version) + 1 FROM report WHERE work_id = :work), 'GENERATING',
                    work_type, rule_version, risk_items, zone_history, sections, citations, subgraph, steps,
                    llm_model, :now, 'GENERATING', CAST(:facts AS jsonb),
                    -- 사람이 고친 양식 칸과 리더는 새 버전으로 옮긴다(S15P31A101-609). B10 에서 새 버전 없이 바꿀 때까지의 보완.
                    form, form_revision, leader_user_id
                FROM report WHERE report_id = :base
                RETURNING report_id
                """, new MapSqlParameterSource("work", workId).addValue("base", ready.getFirst())
                .addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC))
                .addValue("facts", mapper.writeValueAsString(facts)), Long.class);
        return result.put("status", "GENERATING").put("report_id", id).putNull("reason");
    }

    /** MVP는 SiteLookup이 단일 현장을 보장한다. risk_event에는 site_id가 없다. */
    ObjectNode aggregate(ObjectNode work) {
        MapSqlParameterSource params = new MapSqlParameterSource()
                .addValue("start", at(work.path("actual_started_at").asLong()))
                .addValue("end", at(work.path("actual_ended_at").asLong()))
                .addValue("zone", work.path("zone_id").isNull() ? null : work.path("zone_id").asString(), java.sql.Types.VARCHAR);
        ObjectNode facts = mapper.createObjectNode();
        facts.put("event_count", 0).put("operator_ack_count", 0).put("operator_close_count", 0).put("outside_work_entry_count", 0);
        facts.put("unacknowledged_critical_count", 0);
        Map<String, Long> zones = new HashMap<>();
        facts.putNull("max_severity");
        ObjectNode phases = facts.putObject("phase_counts");
        phases.put("early", 0).put("middle", 0).put("late", 0);
        facts.putNull("first_event_min").putNull("last_event_min");
        ObjectNode severities = facts.putObject("severity_counts");
        List<String> ranks = List.of("INFO", "CAUTION", "HIGH", "CRITICAL");
        ranks.forEach(severity -> severities.put(severity, 0));
        ObjectNode rules = facts.putObject("rule_counts");
        jdbc.query("""
                SELECT e.severity, e.rule_id, e.raised_at,
                    (SELECT z.name FROM zone z WHERE z.zone_id = e.zone_id AND z.valid_from <= e.raised_at
                        AND (z.valid_to IS NULL OR z.valid_to > e.raised_at)
                        ORDER BY z.valid_from DESC, z.zone_version DESC LIMIT 1) AS zone_name,
                    (CAST(:zone AS text) IS NULL OR e.zone_id = :zone) AS in_scope,
                    (e.rule_id = 'R1' AND e.zone_id IS NOT NULL AND CAST(:zone AS text) IS NOT NULL
                        AND e.zone_id <> :zone) AS outside_entry,
                    EXISTS (SELECT 1 FROM event_ack a WHERE a.event_id = e.event_id
                        AND a.acked_at BETWEEN :start AND :end) AS acked,
                    EXISTS (SELECT 1 FROM event_close c WHERE c.event_id = e.event_id AND c.result = 'ACCEPTED'
                        AND c.decided_at BETWEEN :start AND :end) AS closed
                FROM risk_event e WHERE e.source = 'LIVE' AND e.raised_at BETWEEN :start AND :end
                """, params, rs -> {
            if (rs.getBoolean("outside_entry")) increment(facts, "outside_work_entry_count");
            if (!rs.getBoolean("in_scope")) return;
            increment(facts, "event_count");
            addTiming(facts, work.path("actual_started_at").asLong(), work.path("actual_ended_at").asLong(),
                    rs.getObject("raised_at", OffsetDateTime.class).toInstant().toEpochMilli());
            String severity = rs.getString("severity");
            increment(severities, severity);
            increment(rules, rs.getString("rule_id"));
            if (ranks.indexOf(severity) > ranks.indexOf(facts.path("max_severity").asString(""))) {
                facts.put("max_severity", severity);
            }
            String zoneName = safeZoneName(rs.getString("zone_name"));
            zones.merge(zoneName, 1L, Long::sum);
            if (rs.getBoolean("acked")) increment(facts, "operator_ack_count");
            else if ("CRITICAL".equals(severity)) increment(facts, "unacknowledged_critical_count");
            if (rs.getBoolean("closed")) increment(facts, "operator_close_count");
        });
        var topZones = facts.putArray("top_zones");
        zones.entrySet().stream().sorted(Map.Entry.<String, Long>comparingByValue().reversed()
                .thenComparing(Map.Entry.comparingByKey())).limit(3).forEach(entry ->
                    topZones.addObject().put("name", entry.getKey()).put("count", entry.getValue()));
        return facts;
    }

    static String safeZoneName(String name) {
        if (name == null || name.isBlank() || !name.equals(name.strip())
                || Character.isSpaceChar(name.codePointAt(0)) || Character.isSpaceChar(name.codePointBefore(name.length()))
                || name.codePointCount(0, name.length()) > 40
                || name.codePoints().anyMatch(c -> Character.getType(c) == Character.CONTROL
                    || Character.getType(c) == Character.FORMAT || Character.getType(c) == Character.SURROGATE
                    || Character.getType(c) == Character.PRIVATE_USE || Character.getType(c) == Character.UNASSIGNED)) {
            return "현장 전체";
        }
        return name;
    }

    static void addTiming(ObjectNode facts, long start, long end, long raised) {
        long elapsed = raised - start;
        long duration = end - start;
        // 경계는 다음 구간, 끝점은 후반. 0 길이는 초반으로 센다.
        int part = duration == 0 ? 0 : elapsed >= duration ? 2
                : java.math.BigInteger.valueOf(elapsed).multiply(java.math.BigInteger.valueOf(3))
                    .divide(java.math.BigInteger.valueOf(duration)).intValue();
        String phase = List.of("early", "middle", "late").get(part);
        increment((ObjectNode) facts.path("phase_counts"), phase);
        long minute = elapsed / 60_000;
        if (facts.path("first_event_min").isNull() || minute < facts.path("first_event_min").asLong()) {
            facts.put("first_event_min", minute);
        }
        if (facts.path("last_event_min").isNull() || minute > facts.path("last_event_min").asLong()) {
            facts.put("last_event_min", minute);
        }
    }

    public ObjectNode request(long reportId, ObjectNode work) {
        String facts = jdbc.queryForObject("SELECT wrap_up_facts::text FROM report WHERE report_id = :id",
                new MapSqlParameterSource("id", reportId), String.class);
        ObjectNode request = mapper.createObjectNode();
        request.set("work_type", work.path("work_type"));
        request.putArray("window_ms").add(work.path("actual_started_at").asLong()).add(work.path("actual_ended_at").asLong());
        request.set("facts", mapper.readTree(facts));
        return request;
    }

    public void complete(long reportId, String summary, boolean fallback, String model, Instant now) {
        jdbc.update("""
                UPDATE report SET status = 'SUCCEEDED', wrap_up_status = 'SUCCEEDED', wrap_up_summary = :summary,
                    wrap_up_fallback = :fallback, wrap_up_model = :model, completed_at = :now
                WHERE report_id = :id AND status = 'GENERATING' AND wrap_up_status = 'GENERATING'
                """, new MapSqlParameterSource("id", reportId).addValue("summary", summary).addValue("fallback", fallback)
                .addValue("model", model).addValue("now", OffsetDateTime.ofInstant(now, ZoneOffset.UTC)));
    }

    private static OffsetDateTime at(long ms) {
        return OffsetDateTime.ofInstant(Instant.ofEpochMilli(ms), ZoneOffset.UTC);
    }

    private static void increment(ObjectNode node, String key) {
        node.put(key, node.path(key).asLong(0) + 1);
    }
}
