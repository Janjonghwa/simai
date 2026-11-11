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
import java.time.LocalTime;
import java.time.OffsetDateTime;
import java.time.ZoneId;
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
                   r.llm_model, r.created_at, r.completed_at, r.wrap_up_summary, r.wrap_up_status,
                   r.locked_at, r.content_sha256, r.approved_at,
                   (SELECT COALESCE(u.display_label, u.login_id) FROM app_user u WHERE u.user_id=r.approved_by) AS approved_by_name,
                   r.form::text AS form, r.form_revision, r.leader_user_id,
                   COALESCE(lu.display_label, lu.login_id) AS leader_name, lu.org AS leader_org, lu.title AS leader_title,
                   w.work_date, w.name AS work_name, w.starts_at, w.ends_at,
                   w.zone_id, w.place, w.planned_people, w.lead_team
            FROM report r
            JOIN work_unit w ON w.work_id = r.work_id
            LEFT JOIN app_user lu ON lu.user_id = r.leader_user_id
            WHERE w.site_id = :siteId AND r.report_kind = 'TBM'
            """;

    /** 날짜 작업 보고서의 일시 기본값(미래 날짜). 요청서 B5·아티팩트 기준 07:30(Asia/Seoul). */
    static final LocalTime DAILY_DEFAULT_TIME = LocalTime.of(7, 30);
    static final ZoneId SITE_ZONE = ZoneId.of("Asia/Seoul");

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

    /**
     * 서버 재시작 때 GENERATING 으로 남아 있던 행을 FAILED(INTERRUPTED)로 마감한다. 날짜 작업 AI 초안(-616 B9)도 같다.
     * 날짜 작업의 종료 미팅(양식 있음, B10)은 초안 상태와 따로 가므로 여기서 건드리지 않고 TbmFormAiService.recoverEndings 가 마감한다.
     */
    public int failInterrupted(Instant now) {
        return jdbc.update("""
                UPDATE report
                SET status = 'FAILED', failure_reason = 'INTERRUPTED', completed_at = :now,
                    wrap_up_status = CASE WHEN wrap_up_status = 'GENERATING' AND form IS NULL THEN 'FAILED' ELSE wrap_up_status END
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
    public GeneratingStart createGenerating(String siteId, long workId, Long leaderUserId, Instant now) {
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
        // 날짜 작업 보고서(최신 TBM 보고서에 양식이 있음)는 이 새 버전 경로를 받지 않는다(S15P31A101-616).
        // 양식 없는 새 버전이 최신이 되면 양식과 작업 전 서명이 가려진다. 초안은 POST .../{id}/form-draft 로 같은 행에 받는다.
        Boolean formManaged = jdbc.queryForObject("""
                SELECT COALESCE((SELECT form IS NOT NULL FROM report WHERE work_id = :workId AND report_kind = 'TBM'
                    ORDER BY report_version DESC, report_id DESC LIMIT 1), false)
                """, lookupParams, Boolean.class);
        if (Boolean.TRUE.equals(formManaged)) {
            throw new BusinessException(ErrorCode.Report.REPORT_FORM_MANAGED);
        }
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
                        status, work_type, created_at, leader_user_id)
                    VALUES ('TBM', :workId, :layoutId, :layoutRevision, :version,
                        'GENERATING', :workType, :now, :leader)
                    RETURNING report_id
                    """, new MapSqlParameterSource()
                    .addValue("workId", workId)
                    .addValue("leader", leaderUserId)
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

    /**
     * 날짜 작업의 빈 보고서(PENDING, 빈 양식)를 만든다. 이미 보고서가 있으면 새로 만들지 않고 최신 보고서 id 를 돌려준다.
     * 레이아웃 검사(WORK_HAS_NO_LAYOUT)는 하지 않는다. 일시는 작업 날짜가 오늘(또는 지난 날)이면 지금, 미래면 그날 07:30.
     * 작업 행을 잠가 같은 작업에 두 번 만들지 않는다. 부르는 쪽의 트랜잭션 안에서 부른다.
     */
    public long createBlank(String siteId, long workId, Long leaderUserId, Instant now) {
        MapSqlParameterSource p = new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId);
        List<LocalDate> dates = jdbc.query("""
                SELECT work_date FROM work_unit WHERE site_id = :siteId AND work_id = :workId FOR UPDATE
                """, p, (rs, i) -> rs.getObject("work_date", LocalDate.class));
        if (dates.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        List<Long> existing = jdbc.queryForList("""
                SELECT report_id FROM report WHERE work_id = :workId AND report_kind = 'TBM'
                ORDER BY report_version DESC, report_id DESC LIMIT 1
                """, p, Long.class);
        if (!existing.isEmpty()) {
            return existing.getFirst();
        }
        LocalDate workDate = dates.getFirst();
        Instant dateTime = workDate.isAfter(LocalDate.ofInstant(now, SITE_ZONE))
                ? workDate.atTime(DAILY_DEFAULT_TIME).atZone(SITE_ZONE).toInstant()
                : now;
        Long id = jdbc.queryForObject("""
                INSERT INTO report (report_kind, work_id, report_version, status, created_at, form, leader_user_id)
                VALUES ('TBM', :workId, 1, 'PENDING', :now, CAST(:form AS jsonb), :leader)
                RETURNING report_id
                """, p.addValue("now", utc(now))
                .addValue("form", mapper.writeValueAsString(TbmForm.blank(dateTime.toEpochMilli())))
                .addValue("leader", leaderUserId), Long.class);
        return id == null ? 0L : id;
    }

    /**
     * 양식 칸을 통째로 바꾼다. 새 report_version 을 만들지 않고 form_revision 만 올린다.
     * 최신 버전이 아니면 REPORT_VERSION_STALE, 잠겼으면 REPORT_LOCKED, revision 이 다르면 REPORT_FORM_CONFLICT.
     */
    public void updateForm(String siteId, long reportId, JsonNode form, int expectedRevision) {
        MapSqlParameterSource p = new MapSqlParameterSource().addValue("siteId", siteId).addValue("reportId", reportId);
        record Target(int revision, boolean locked, boolean latest) { }
        List<Target> rows = jdbc.query("""
                SELECT r.form_revision, r.locked_at IS NOT NULL AS locked,
                       r.report_version = (SELECT max(x.report_version) FROM report x WHERE x.work_id = r.work_id) AS latest
                FROM report r JOIN work_unit w ON w.work_id = r.work_id
                WHERE w.site_id = :siteId AND r.report_id = :reportId AND r.report_kind = 'TBM'
                FOR UPDATE OF r
                """, p, (rs, i) -> new Target(rs.getInt("form_revision"), rs.getBoolean("locked"), rs.getBoolean("latest")));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        }
        Target t = rows.getFirst();
        if (!t.latest()) {
            throw new BusinessException(ErrorCode.Report.REPORT_VERSION_STALE);
        }
        if (t.locked()) {
            throw new BusinessException(ErrorCode.Report.REPORT_LOCKED);
        }
        if (t.revision() != expectedRevision) {
            throw new BusinessException(ErrorCode.Report.REPORT_FORM_CONFLICT);
        }
        jdbc.update("""
                UPDATE report SET form = CAST(:form AS jsonb), form_revision = form_revision + 1
                WHERE report_id = :reportId
                """, p.addValue("form", mapper.writeValueAsString(form)));
        renameDailyWork(reportId, form);
    }

    /** 날짜 작업 이름은 고른 작업내용으로 만든다(첫 항목 + ' 외 N건', 고르지 않았으면 비움, S15P31A101-610). */
    private void renameDailyWork(long reportId, JsonNode form) {
        jdbc.update("""
                UPDATE work_unit SET name = :name, updated_at = now()
                WHERE daily AND work_id = (SELECT work_id FROM report WHERE report_id = :reportId)
                  AND name IS DISTINCT FROM :name
                """, new MapSqlParameterSource("reportId", reportId).addValue("name", TbmForm.workName(form)));
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
            v.set("wrap_up_status", r.get("wrap_up_status"));
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
        String status = rs.getString("status");
        o.put("status", status);
        // draft_status: AI 초안 상태. PENDING(초안 작성 전)은 null 로 낸다.
        putText(o, "draft_status", "PENDING".equals(status) ? null : status);
        putText(o, "failure_reason", rs.getString("failure_reason"));
        String form = rs.getString("form");
        o.set("form", form == null ? mapper.nullNode() : mapper.readTree(form));
        o.put("form_revision", rs.getInt("form_revision"));
        long leaderId = rs.getLong("leader_user_id");
        if (rs.wasNull()) {
            o.putNull("leader");
        } else {
            ObjectNode leader = o.putObject("leader");
            leader.put("user_id", leaderId);
            putText(leader, "name", rs.getString("leader_name"));
            putText(leader, "org", rs.getString("leader_org"));
            putText(leader, "title", rs.getString("leader_title"));
        }
        putText(o, "wrap_up_summary", rs.getString("wrap_up_summary"));
        putText(o, "wrap_up_status", rs.getString("wrap_up_status"));
        putText(o, "content_sha256", rs.getString("content_sha256"));
        Timestamp lockedAt = rs.getTimestamp("locked_at");
        if (lockedAt == null) o.putNull("locked_at"); else o.put("locked_at", lockedAt.getTime());
        Timestamp approvedAt = rs.getTimestamp("approved_at");
        if (approvedAt == null) o.putNull("approved_at"); else o.put("approved_at", approvedAt.getTime());
        putText(o, "approved_by_name", rs.getString("approved_by_name"));
        // 서명 표는 -440(V29)가 소유한다. 독립 worktree에는 없으므로 0으로 시작한다.
        boolean signaturesExist = Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT to_regclass('report_signature') IS NOT NULL", Map.of(), Boolean.class));
        long signatures = signaturesExist ? jdbc.queryForObject(
                "SELECT count(*) FROM report_signature WHERE report_id = :id AND report_version = :version",
                new MapSqlParameterSource("id", rs.getLong("report_id")).addValue("version", rs.getInt("report_version")), Long.class) : 0;
        o.put("signature_count", signatures);
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
