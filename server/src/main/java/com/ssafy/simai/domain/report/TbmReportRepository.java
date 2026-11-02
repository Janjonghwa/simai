package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.rule.RuleSelection;
import com.ssafy.simai.domain.work.WorkTypes;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.dao.DuplicateKeyException;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * TBM 작업 단위 보고서 저장소(V27 report, .agy/tbm-interface.md B절).
 * 보고서는 작업마다 새 report_version 행을 추가하고, 같은 행의 GENERATING → SUCCEEDED/FAILED 완료 기록만 갱신한다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class TbmReportRepository {

    private static final String SELECT_REPORT = """
            SELECT r.report_id, r.work_id, r.report_version, r.status, r.failure_reason,
                   r.layout_id, r.layout_revision, r.work_type, r.rule_version,
                   r.risk_items::text AS risk_items, r.zone_history::text AS zone_history,
                   r.sections::text AS sections, r.citations::text AS citations,
                   r.subgraph::text AS subgraph, r.steps::text AS steps,
                   r.llm_model, r.created_at, r.completed_at,
                   w.work_date, w.name AS work_name, w.starts_at, w.ends_at,
                   w.zone_id, w.place, w.planned_people, w.lead_team
            FROM report r
            JOIN work_unit w ON w.work_id = r.work_id
            WHERE w.site_id = :siteId AND r.report_kind = 'TBM'
            """;

    private static final Map<String, Integer> SEVERITY_RANK = Map.of(
            "INFO", 1,
            "CAUTION", 2,
            "HIGH", 3,
            "CRITICAL", 4);

    public record GeneratingStart(
            long reportId,
            long workId,
            int reportVersion,
            long layoutId,
            int layoutRevision,
            String workType,
            Instant evaluationTime,
            String zoneId) {
    }

    public record RuleRow(String ruleId, int ruleVersion, String legalBasis) {
    }

    public record ActiveRules(String baseConfigVersion, long representativeRuleVersion, List<RuleRow> rules) {
    }

    public record ZoneMeta(String zoneId, String zoneKind, String label) {
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public TbmReportRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 서버 재시작 때 GENERATING 으로 남아 있던 행을 FAILED(INTERRUPTED)로 마감한다. */
    public int failInterrupted(Instant now) {
        return jdbc.update("""
                UPDATE report
                SET status = 'FAILED', failure_reason = 'INTERRUPTED', completed_at = :now
                WHERE status = 'GENERATING'
                """, new MapSqlParameterSource("now", utc(now)));
    }

    public Optional<Long> findGeneratingId(long workId) {
        return jdbc.queryForList("""
                SELECT report_id FROM report WHERE work_id = :workId AND status = 'GENERATING'
                """, new MapSqlParameterSource("workId", workId), Long.class).stream().findFirst();
    }

    /**
     * 작업 행을 잠그고 레이아웃 연결 여부·진행 중 보고서 유무를 확인한 뒤 새 report_version 의 GENERATING 행을 넣는다.
     */
    public GeneratingStart createGenerating(String siteId, long workId, Instant now) {
        MapSqlParameterSource lookupParams = new MapSqlParameterSource()
                .addValue("siteId", siteId)
                .addValue("workId", workId);
        List<GeneratingStart> target = jdbc.query("""
                SELECT w.work_id, w.layout_id, w.work_type, w.starts_at, w.zone_id, l.revision AS layout_revision
                FROM work_unit w
                LEFT JOIN layout_plan l ON l.layout_id = w.layout_id AND l.site_id = w.site_id
                WHERE w.site_id = :siteId AND w.work_id = :workId
                FOR UPDATE OF w
                """, lookupParams, (rs, i) -> {
            long layoutId = rs.getLong("layout_id");
            boolean noLayout = rs.wasNull();
            int revision = rs.getInt("layout_revision");
            boolean noRevision = rs.wasNull();
            return new GeneratingStart(
                    0L,
                    rs.getLong("work_id"),
                    0,
                    noLayout ? -1L : layoutId,
                    noRevision ? -1 : revision,
                    rs.getString("work_type"),
                    rs.getTimestamp("starts_at").toInstant(),
                    rs.getString("zone_id"));
        });
        if (target.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        GeneratingStart work = target.getFirst();
        if (work.layoutId() < 0 || work.layoutRevision() < 1) {
            throw new BusinessException(ErrorCode.Report.WORK_HAS_NO_LAYOUT);
        }
        Optional<Long> existingGenerating = findGeneratingId(workId);
        if (existingGenerating.isPresent()) {
            throw new ReportInProgressException(existingGenerating.get());
        }
        Integer nextVersion = jdbc.queryForObject("""
                SELECT COALESCE(MAX(report_version), 0) + 1 FROM report WHERE work_id = :workId
                """, new MapSqlParameterSource("workId", workId), Integer.class);
        int version = nextVersion == null ? 1 : nextVersion;
        try {
            Long reportId = jdbc.queryForObject("""
                    INSERT INTO report (report_kind, work_id, layout_id, layout_revision, report_version,
                        status, work_type, created_at)
                    VALUES ('TBM', :workId, :layoutId, :layoutRevision, :version,
                        'GENERATING', :workType, :now)
                    RETURNING report_id
                    """, new MapSqlParameterSource()
                    .addValue("workId", workId)
                    .addValue("layoutId", work.layoutId())
                    .addValue("layoutRevision", work.layoutRevision())
                    .addValue("version", version)
                    .addValue("workType", work.workType())
                    .addValue("now", utc(now)), Long.class);
            return new GeneratingStart(
                    reportId == null ? 0L : reportId,
                    workId,
                    version,
                    work.layoutId(),
                    work.layoutRevision(),
                    work.workType(),
                    work.evaluationTime(),
                    work.zoneId());
        } catch (DuplicateKeyException e) {
            Optional<Long> currentGenerating = findGeneratingId(workId);
            if (currentGenerating.isPresent()) {
                throw new ReportInProgressException(currentGenerating.get());
            }
            throw e;
        }
    }

    public void completeSucceeded(
            long reportId,
            long ruleVersion,
            JsonNode riskItems,
            JsonNode zoneHistory,
            JsonNode sections,
            JsonNode citations,
            JsonNode subgraph,
            JsonNode steps,
            String llmModel,
            Instant completedAt) {
        jdbc.update("""
                UPDATE report
                SET status = 'SUCCEEDED',
                    failure_reason = NULL,
                    rule_version = :ruleVersion,
                    risk_items = CAST(:riskItems AS jsonb),
                    zone_history = CAST(:zoneHistory AS jsonb),
                    sections = CAST(:sections AS jsonb),
                    citations = CAST(:citations AS jsonb),
                    subgraph = CAST(:subgraph AS jsonb),
                    steps = CAST(:steps AS jsonb),
                    llm_model = :llmModel,
                    completed_at = :completedAt
                WHERE report_id = :reportId AND status = 'GENERATING'
                """, new MapSqlParameterSource()
                .addValue("reportId", reportId)
                .addValue("ruleVersion", ruleVersion)
                .addValue("riskItems", jsonOrDefault(riskItems, "[]"))
                .addValue("zoneHistory", jsonOrDefault(zoneHistory, "[]"))
                .addValue("sections", jsonOrDefault(sections, "[]"))
                .addValue("citations", jsonOrDefault(citations, "[]"))
                .addValue("subgraph", jsonOrDefault(subgraph, "{\"nodes\":[],\"edges\":[]}"))
                .addValue("steps", jsonOrDefault(steps, "[]"))
                .addValue("llmModel", llmModel)
                .addValue("completedAt", utc(completedAt)));
    }

    public void completeFailed(
            long reportId,
            String failureReason,
            Long ruleVersion,
            JsonNode riskItems,
            JsonNode zoneHistory,
            JsonNode subgraph,
            JsonNode steps,
            String llmModel,
            Instant completedAt) {
        jdbc.update("""
                UPDATE report
                SET status = 'FAILED',
                    failure_reason = :failureReason,
                    rule_version = COALESCE(:ruleVersion, rule_version),
                    risk_items = CAST(:riskItems AS jsonb),
                    zone_history = CAST(:zoneHistory AS jsonb),
                    sections = '[]'::jsonb,
                    citations = '[]'::jsonb,
                    subgraph = CAST(:subgraph AS jsonb),
                    steps = CAST(:steps AS jsonb),
                    llm_model = :llmModel,
                    completed_at = :completedAt
                WHERE report_id = :reportId AND status = 'GENERATING'
                """, new MapSqlParameterSource()
                .addValue("reportId", reportId)
                .addValue("failureReason", failureReason)
                .addValue("ruleVersion", ruleVersion)
                .addValue("riskItems", jsonOrDefault(riskItems, "[]"))
                .addValue("zoneHistory", jsonOrDefault(zoneHistory, "[]"))
                .addValue("subgraph", jsonOrDefault(subgraph, "{\"nodes\":[],\"edges\":[]}"))
                .addValue("steps", jsonOrDefault(steps, "[]"))
                .addValue("llmModel", llmModel)
                .addValue("completedAt", utc(completedAt)));
    }

    public Optional<ObjectNode> findOne(String siteId, long reportId) {
        return jdbc.query(SELECT_REPORT + " AND r.report_id = :reportId",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("reportId", reportId),
                (rs, i) -> view(rs)).stream().findFirst();
    }

    public ObjectNode findByWork(String siteId, long workId) {
        Boolean exists = jdbc.queryForObject("""
                SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :siteId AND work_id = :workId)
                """, new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId), Boolean.class);
        if (!Boolean.TRUE.equals(exists)) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        List<ObjectNode> reports = jdbc.query(
                SELECT_REPORT + " AND r.work_id = :workId ORDER BY r.report_version DESC, r.report_id DESC",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId),
                (rs, i) -> view(rs));
        ObjectNode out = mapper.createObjectNode();
        if (reports.isEmpty()) {
            out.putNull("latest");
        } else {
            out.set("latest", reports.getFirst());
        }
        ArrayNode versions = out.putArray("versions");
        for (ObjectNode r : reports) {
            ObjectNode v = versions.addObject();
            v.put("report_id", r.get("report_id").asLong());
            v.put("report_version", r.get("report_version").asInt());
            v.put("status", r.get("status").asString());
            v.put("created_at_ms", r.get("created_at_ms").asLong());
        }
        return out;
    }

    public ArrayNode findLayoutItems(String siteId, long layoutId, int revision) {
        List<String> revItems = jdbc.queryForList("""
                SELECT r.items::text
                FROM layout_plan_revision r
                JOIN layout_plan l ON l.layout_id = r.layout_id
                WHERE l.site_id = :siteId AND r.layout_id = :layoutId AND r.revision = :revision
                """, new MapSqlParameterSource()
                .addValue("siteId", siteId)
                .addValue("layoutId", layoutId)
                .addValue("revision", revision), String.class);
        String json = revItems.isEmpty() ? null : revItems.getFirst();
        if (json == null) {
            List<String> planItems = jdbc.queryForList("""
                    SELECT items::text FROM layout_plan WHERE site_id = :siteId AND layout_id = :layoutId
                    """, new MapSqlParameterSource().addValue("siteId", siteId).addValue("layoutId", layoutId), String.class);
            json = planItems.isEmpty() ? "[]" : planItems.getFirst();
        }
        JsonNode parsed = mapper.readTree(json);
        return parsed instanceof ArrayNode arr ? arr : mapper.createArrayNode();
    }

    public ActiveRules findActiveRules(String siteId) {
        MapSqlParameterSource site = new MapSqlParameterSource("site", siteId);
        List<String> applied = jdbc.queryForList("""
                SELECT config_version FROM config_bundle
                WHERE site_id = :site AND applied_at IS NOT NULL
                ORDER BY applied_at DESC, issued_at DESC LIMIT 1
                """, site, String.class);
        String baseConfig = applied.isEmpty() ? null : applied.getFirst();
        List<RuleRow> rows = baseConfig != null
                ? jdbc.query("""
                        SELECT r.rule_id, r.rule_version, r.legal_basis
                        FROM config_bundle_item i
                        JOIN rule_version r ON r.rule_id = i.item_id AND r.rule_version = i.item_version
                        WHERE i.config_version = :v AND i.item_type = 'RULE'
                        ORDER BY length(r.rule_id), r.rule_id
                        """, new MapSqlParameterSource("v", baseConfig),
                        (rs, i) -> new RuleRow(rs.getString("rule_id"), rs.getInt("rule_version"), rs.getString("legal_basis")))
                : List.of();
        if (rows.isEmpty()) {
            rows = jdbc.query(
                    "SELECT r.rule_id, r.rule_version, r.legal_basis FROM (" + RuleSelection.ACTIVE_FOR_PROFILE
                            + ") r ORDER BY length(r.rule_id), r.rule_id",
                    new MapSqlParameterSource("profile", "OPERATION"),
                    (rs, i) -> new RuleRow(rs.getString("rule_id"), rs.getInt("rule_version"), rs.getString("legal_basis")));
        }
        long repVersion = rows.stream().mapToLong(RuleRow::ruleVersion).max().orElse(1L);
        return new ActiveRules(baseConfig, repVersion, rows);
    }

    public Optional<ZoneMeta> findWorkZone(String siteId, String zoneId) {
        if (zoneId == null || zoneId.isBlank()) {
            return Optional.empty();
        }
        return jdbc.query("""
                SELECT zone_id, zone_kind, name
                FROM zone
                WHERE site_id = :siteId AND zone_id = :zoneId
                ORDER BY zone_version DESC LIMIT 1
                """, new MapSqlParameterSource().addValue("siteId", siteId).addValue("zoneId", zoneId),
                (rs, i) -> new ZoneMeta(rs.getString("zone_id"), rs.getString("zone_kind"), rs.getString("name")))
                .stream().findFirst();
    }

    /** 작업 구역(들)의 최근 7일 LIVE risk_event 요약(.agy/tbm-interface.md A절 zone_history). */
    public ArrayNode aggregateZoneHistory(Collection<String> zoneIds, Instant since, Instant until) {
        ArrayNode result = mapper.createArrayNode();
        if (zoneIds == null || zoneIds.isEmpty()) {
            return result;
        }
        Map<String, ZoneAgg> byZone = new LinkedHashMap<>();
        jdbc.query("""
                SELECT zone_id, rule_id, severity, raised_at
                FROM risk_event
                WHERE source = 'LIVE'
                  AND zone_id IN (:zoneIds)
                  AND raised_at >= :since
                  AND raised_at <= :until
                ORDER BY raised_at ASC
                """, new MapSqlParameterSource()
                .addValue("zoneIds", new ArrayList<>(zoneIds))
                .addValue("since", utc(since))
                .addValue("until", utc(until)), rs -> {
            String zId = rs.getString("zone_id");
            ZoneAgg agg = byZone.computeIfAbsent(zId, k -> new ZoneAgg());
            agg.count++;
            String ruleId = rs.getString("rule_id");
            if (ruleId != null && !ruleId.isBlank()) {
                agg.rules.add(ruleId);
            }
            String sev = rs.getString("severity");
            if (sev != null && SEVERITY_RANK.getOrDefault(sev, 0) > SEVERITY_RANK.getOrDefault(agg.maxSeverity, 0)) {
                agg.maxSeverity = sev;
            }
            Timestamp ts = rs.getTimestamp("raised_at");
            if (ts != null && ts.getTime() > agg.lastAtMs) {
                agg.lastAtMs = ts.getTime();
            }
        });
        for (String zId : zoneIds) {
            ZoneAgg agg = byZone.get(zId);
            if (agg == null || agg.count == 0) {
                continue;
            }
            ObjectNode entry = result.addObject();
            entry.put("zone_id", zId);
            entry.put("days", 7);
            entry.put("live_event_count", agg.count);
            ArrayNode rulesNode = entry.putArray("rules");
            agg.rules.stream()
                    .sorted((a, b) -> {
                        int cmp = Integer.compare(a.length(), b.length());
                        return cmp != 0 ? cmp : a.compareTo(b);
                    })
                    .forEach(rulesNode::add);
            entry.put("max_severity", agg.maxSeverity);
            entry.put("last_at_ms", agg.lastAtMs);
        }
        return result;
    }

    private static final class ZoneAgg {
        int count = 0;
        final Set<String> rules = new LinkedHashSet<>();
        String maxSeverity = "INFO";
        long lastAtMs = 0L;
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode o = mapper.createObjectNode();
        o.put("report_id", rs.getLong("report_id"));
        o.put("work_id", rs.getLong("work_id"));
        o.put("report_version", rs.getInt("report_version"));
        o.put("status", rs.getString("status"));
        putText(o, "failure_reason", rs.getString("failure_reason"));
        long layoutId = rs.getLong("layout_id");
        if (rs.wasNull()) {
            o.putNull("layout_id");
        } else {
            o.put("layout_id", layoutId);
        }
        int layoutRev = rs.getInt("layout_revision");
        if (rs.wasNull()) {
            o.putNull("layout_revision");
        } else {
            o.put("layout_revision", layoutRev);
        }
        String workTypeCode = rs.getString("work_type");
        if (workTypeCode == null) {
            o.putNull("work_type");
        } else {
            ObjectNode wt = o.putObject("work_type");
            wt.put("code", workTypeCode);
            wt.put("label", WorkTypes.find(workTypeCode).map(WorkTypes.WorkType::label).orElse(workTypeCode));
        }

        ObjectNode header = o.putObject("header");
        header.put("date", rs.getObject("work_date", LocalDate.class).toString());
        header.put("name", rs.getString("work_name"));
        header.put("from_ms", rs.getTimestamp("starts_at").getTime());
        header.put("to_ms", rs.getTimestamp("ends_at").getTime());
        putText(header, "zone_id", rs.getString("zone_id"));
        putText(header, "place", rs.getString("place"));
        header.put("planned_people", rs.getInt("planned_people"));
        putText(header, "lead", rs.getString("lead_team"));

        long ruleVersion = rs.getLong("rule_version");
        if (rs.wasNull()) {
            o.putNull("rule_version");
        } else {
            o.put("rule_version", ruleVersion);
        }
        o.set("risk_items", mapper.readTree(rs.getString("risk_items")));
        o.set("zone_history", mapper.readTree(rs.getString("zone_history")));
        o.set("sections", mapper.readTree(rs.getString("sections")));
        o.set("citations", mapper.readTree(rs.getString("citations")));
        o.set("subgraph", mapper.readTree(rs.getString("subgraph")));
        o.set("steps", mapper.readTree(rs.getString("steps")));
        putText(o, "llm_model", rs.getString("llm_model"));
        o.put("created_at_ms", rs.getTimestamp("created_at").getTime());
        Timestamp completedAt = rs.getTimestamp("completed_at");
        if (completedAt == null) {
            o.putNull("completed_at_ms");
        } else {
            o.put("completed_at_ms", completedAt.getTime());
        }
        return o;
    }

    private String jsonOrDefault(JsonNode node, String fallback) {
        if (node == null || node.isNull() || node.isMissingNode()) {
            return fallback;
        }
        return mapper.writeValueAsString(node);
    }

    private static void putText(ObjectNode node, String field, String value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }
}
