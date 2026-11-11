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

/**
 * 종료 트랜잭션 안에서 사실을 고정한다. 예전 작업 보고서(양식 없음)는 기존 본문을 새 버전에 복사하고,
 * 날짜 작업 보고서(양식 있음, S15P31A101-616 B10)는 새 버전 없이 같은 행의 review.ending 을 채운다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkWrapUpRepository {
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public WorkWrapUpRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 마무리 결과의 mode: 날짜 작업은 같은 행의 양식 종료 미팅, 예전 작업은 새 버전 마무리. */
    public static final String MODE_FORM_ENDING = "FORM_ENDING";
    public static final String MODE_NEW_VERSION = "NEW_VERSION";

    public ObjectNode prepare(long workId, ObjectNode work, Instant now) {
        ObjectNode result = mapper.createObjectNode();
        // 날짜 작업 보고서(최신 TBM 보고서에 양식이 있음)는 새 버전을 만들지 않는다. 작업 전에 받은 서명이 버전에 묶여 있다.
        record Latest(long reportId, boolean daily, boolean locked) { }
        List<Latest> latest = jdbc.query("""
                SELECT report_id, form IS NOT NULL AS daily, locked_at IS NOT NULL AS locked
                FROM report WHERE work_id = :work AND report_kind = 'TBM'
                ORDER BY report_version DESC, report_id DESC LIMIT 1
                FOR UPDATE
                """, new MapSqlParameterSource("work", workId),
                (rs, i) -> new Latest(rs.getLong("report_id"), rs.getBoolean("daily"), rs.getBoolean("locked")));
        if (!latest.isEmpty() && latest.getFirst().daily()) {
            long reportId = latest.getFirst().reportId();
            result.put("mode", MODE_FORM_ENDING);
            if (latest.getFirst().locked()) {
                // 잠긴(제출된) 보고서는 고치지 않는다. -611 전에는 첫 서명이 잠근다.
                return result.put("status", "SKIPPED").put("report_id", reportId).put("reason", "REPORT_LOCKED");
            }
            ObjectNode facts = aggregate(work);
            jdbc.update("""
                    UPDATE report SET wrap_up_status = 'GENERATING', wrap_up_facts = CAST(:facts AS jsonb),
                        wrap_up_fallback = NULL, wrap_up_model = NULL
                    WHERE report_id = :id
                    """, new MapSqlParameterSource("id", reportId).addValue("facts", mapper.writeValueAsString(facts)));
            return result.put("status", "GENERATING").put("report_id", reportId).putNull("reason");
        }
        result.put("mode", MODE_NEW_VERSION);
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
                    -- 최신 보고서에 양식이 있는 날짜 작업은 위에서 새 버전 없이 처리한다(S15P31A101-616 B10). 여기 오는 보고서는 보통 양식이 없다.
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

    /** 날짜 작업 종료 미팅 요청(계약 tbm_form_ending_request). 사실은 종료 트랜잭션에서 고정한 wrap_up_facts 다. */
    public ObjectNode endingRequest(long reportId, ObjectNode work) {
        String facts = jdbc.queryForObject("SELECT wrap_up_facts::text FROM report WHERE report_id = :id",
                new MapSqlParameterSource("id", reportId), String.class);
        ObjectNode request = mapper.createObjectNode();
        request.put("request_id", "rpt-" + reportId + "-end");
        request.putArray("window_ms").add(work.path("actual_started_at").asLong()).add(work.path("actual_ended_at").asLong());
        request.set("facts", mapper.readTree(facts));
        return request;
    }

    /** 재기동 때 종료 미팅을 쓰다 멈춘 날짜 작업 보고서와 그 고정 사실. */
    public List<Map.Entry<Long, ObjectNode>> generatingEndings() {
        return jdbc.query("""
                SELECT report_id, wrap_up_facts::text AS facts FROM report
                WHERE wrap_up_status = 'GENERATING' AND form IS NOT NULL
                """, Map.of(), (rs, i) -> Map.entry(rs.getLong("report_id"),
                (ObjectNode) mapper.readTree(rs.getString("facts") == null ? "{}" : rs.getString("facts"))));
    }

    /**
     * 날짜 작업 종료 미팅을 같은 행에 마감한다. 행을 잠그고 review.ending 이 비어 있을 때만 채우며 그때 form_revision 을 1 올린다.
     * status(초안 상태)·wrap_up_summary(서명 해시가 덮는 칸)는 건드리지 않는다. 그 사이 잠겼으면 양식은 그대로 두고 FAILED.
     *
     * @return review.ending 을 채웠으면 true
     */
    public boolean completeEnding(long reportId, String ending, boolean fallback, String model) {
        MapSqlParameterSource p = new MapSqlParameterSource("id", reportId).addValue("fallback", fallback).addValue("model", model);
        record Row(String form, String wrapStatus, boolean locked) { }
        List<Row> rows = jdbc.query("""
                SELECT form::text AS form, wrap_up_status, locked_at IS NOT NULL AS locked
                FROM report WHERE report_id = :id FOR UPDATE
                """, p, (rs, i) -> new Row(rs.getString("form"), rs.getString("wrap_up_status"), rs.getBoolean("locked")));
        if (rows.isEmpty() || !"GENERATING".equals(rows.getFirst().wrapStatus()) || rows.getFirst().form() == null) {
            return false;
        }
        if (rows.getFirst().locked()) {
            jdbc.update("""
                    UPDATE report SET wrap_up_status = 'FAILED', wrap_up_fallback = :fallback, wrap_up_model = :model
                    WHERE report_id = :id
                    """, p);
            return false;
        }
        ObjectNode form = (ObjectNode) mapper.readTree(rows.getFirst().form());
        boolean fill = form.path("review").path("ending").asString("").isEmpty() && form.path("review").isObject();
        if (fill) {
            ((ObjectNode) form.path("review")).put("ending", ending);
        }
        jdbc.update("""
                UPDATE report SET form = CAST(:form AS jsonb), form_revision = form_revision + :bump,
                    wrap_up_status = 'SUCCEEDED', wrap_up_fallback = :fallback, wrap_up_model = :model
                WHERE report_id = :id
                """, p.addValue("form", mapper.writeValueAsString(form)).addValue("bump", fill ? 1 : 0));
        return fill;
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
