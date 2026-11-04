package com.ssafy.simai.domain.report;

import com.ssafy.simai.domain.report.TbmReportRepository.ZoneMeta;
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
import java.util.List;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 일일 안전 일지 저장소(report_kind DAILY, V36, S15P31A101-33). 현장·날짜마다 재생성할 때 새 report_version 행을 넣고,
 * GENERATING → SUCCEEDED/FAILED 마감은 TBM 과 같은 {@link TbmReportRepository#completeSucceeded}·{@code completeFailed} 로 한다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class DailyReportRepository {

    /** 그날 LIVE 이벤트 한 건(사람 ID·근거 문장은 읽지 않는다, 5.4·11.4-5) */
    record EventFact(String ruleId, String severity, String zoneId, Instant raisedAt, boolean resolved, boolean needsCheck) {
    }

    record DailyStart(long reportId, LocalDate reportDate, int reportVersion) {
    }

    private static final String SELECT_DAILY = """
            SELECT report_id, report_date, report_version, status, failure_reason, rule_version,
                   events::text AS events, event_summary::text AS event_summary,
                   sections::text AS sections, citations::text AS citations,
                   subgraph::text AS subgraph, steps::text AS steps,
                   llm_model, created_at, completed_at
            FROM report
            WHERE site_id = :siteId AND report_kind = 'DAILY'
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    DailyReportRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    Optional<Long> findGeneratingId(String siteId, LocalDate date) {
        return jdbc.queryForList("""
                SELECT report_id FROM report
                WHERE site_id = :siteId AND report_date = :date AND report_kind = 'DAILY' AND status = 'GENERATING'
                """, new MapSqlParameterSource("siteId", siteId).addValue("date", date), Long.class).stream().findFirst();
    }

    /** 그 날짜의 새 report_version 을 GENERATING 으로 넣는다. 이미 생성 중이면 409 와 그 report_id */
    DailyStart createGenerating(String siteId, LocalDate date, Instant now) {
        Optional<Long> generating = findGeneratingId(siteId, date);
        if (generating.isPresent()) {
            throw new ReportInProgressException(ErrorCode.Report.DAILY_REPORT_IN_PROGRESS, generating.get());
        }
        MapSqlParameterSource params = new MapSqlParameterSource("siteId", siteId).addValue("date", date)
                .addValue("now", utc(now));
        // 같은 날짜를 동시에 요청하면 부분 유일 색인에 걸린다. 예외로 받으면 트랜잭션이 깨져(25P02) 다시 조회할 수 없으니
        // ON CONFLICT DO NOTHING 으로 행 없이 돌아오게 하고, 먼저 넣은 쪽의 report_id 로 409 를 준다
        List<DailyStart> inserted = jdbc.query("""
                INSERT INTO report (report_kind, site_id, report_date, report_version, status, created_at)
                SELECT 'DAILY', :siteId, :date, COALESCE(MAX(report_version), 0) + 1, 'GENERATING', :now
                FROM report WHERE site_id = :siteId AND report_date = :date AND report_kind = 'DAILY'
                ON CONFLICT DO NOTHING
                RETURNING report_id, report_version
                """, params, (rs, i) -> new DailyStart(rs.getLong("report_id"), date, rs.getInt("report_version")));
        if (!inserted.isEmpty()) {
            return inserted.getFirst();
        }
        // 생성 중인 행을 못 찾으면 그 사이 끝났고 버전만 겹쳤다. 다시 요청하면 된다(같은 409, report_id -1)
        Optional<Long> current = findGeneratingId(siteId, date);
        throw new ReportInProgressException(ErrorCode.Report.DAILY_REPORT_IN_PROGRESS, current.orElse(-1L));
    }

    /** 생성에 넘긴 사실을 먼저 남긴다. 엔진이 실패해도 조회 화면은 그날 이벤트 근거 표를 보여 준다 */
    void saveFacts(long reportId, JsonNode events, JsonNode summary) {
        jdbc.update("""
                UPDATE report SET events = CAST(:events AS jsonb), event_summary = CAST(:summary AS jsonb)
                WHERE report_id = :id AND report_kind = 'DAILY'
                """, new MapSqlParameterSource("id", reportId)
                .addValue("events", mapper.writeValueAsString(events))
                .addValue("summary", mapper.writeValueAsString(summary)));
    }

    /** [from, to) 안에 발생한 LIVE 이벤트, 발생 순. REPLAY 는 넣지 않는다(7.4) */
    List<EventFact> findEvents(Instant from, Instant to) {
        return jdbc.query("""
                SELECT rule_id, severity, zone_id, raised_at, lifecycle, needs_check
                FROM risk_event
                WHERE source = 'LIVE' AND raised_at >= :from AND raised_at < :to
                ORDER BY raised_at, event_id
                """, new MapSqlParameterSource("from", utc(from)).addValue("to", utc(to)),
                (rs, i) -> new EventFact(rs.getString("rule_id"), rs.getString("severity"), rs.getString("zone_id"),
                        rs.getTimestamp("raised_at").toInstant(), "RESOLVED".equals(rs.getString("lifecycle")),
                        rs.getBoolean("needs_check")));
    }

    /** 구역 ID 들의 최신 판 종류·이름. 지워진 구역은 빠진다 */
    List<ZoneMeta> findZones(String siteId, Collection<String> zoneIds) {
        if (zoneIds.isEmpty()) {
            return List.of();
        }
        return jdbc.query("""
                SELECT DISTINCT ON (zone_id) zone_id, zone_kind, name FROM zone
                WHERE site_id = :siteId AND zone_id IN (:ids)
                ORDER BY zone_id, zone_version DESC
                """, new MapSqlParameterSource("siteId", siteId).addValue("ids", new ArrayList<>(zoneIds)),
                (rs, i) -> new ZoneMeta(rs.getString("zone_id"), rs.getString("zone_kind"), rs.getString("name")));
    }

    Optional<ObjectNode> findOne(String siteId, long reportId) {
        return jdbc.query(SELECT_DAILY + " AND report_id = :id",
                new MapSqlParameterSource("siteId", siteId).addValue("id", reportId), (rs, i) -> view(rs))
                .stream().findFirst();
    }

    /** 그 날짜의 최신 일지와 버전 이력(TBM 의 작업별 조회와 같은 모양) */
    ObjectNode findByDate(String siteId, LocalDate date) {
        List<ObjectNode> rows = jdbc.query(SELECT_DAILY + " AND report_date = :date ORDER BY report_version DESC, report_id DESC",
                new MapSqlParameterSource("siteId", siteId).addValue("date", date), (rs, i) -> view(rs));
        ObjectNode out = mapper.createObjectNode();
        out.put("report_date", date.toString());
        if (rows.isEmpty()) {
            out.putNull("latest");
        } else {
            out.set("latest", rows.getFirst());
        }
        ArrayNode versions = out.putArray("versions");
        for (ObjectNode r : rows) {
            ObjectNode v = versions.addObject();
            v.put("report_id", r.get("report_id").asLong());
            v.put("report_version", r.get("report_version").asInt());
            v.put("status", r.get("status").asString());
            v.put("created_at_ms", r.get("created_at_ms").asLong());
        }
        return out;
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode o = mapper.createObjectNode();
        o.put("report_id", rs.getLong("report_id"));
        o.put("report_kind", "DAILY");
        o.put("report_date", rs.getObject("report_date", LocalDate.class).toString());
        o.put("report_version", rs.getInt("report_version"));
        o.put("status", rs.getString("status"));
        putText(o, "failure_reason", rs.getString("failure_reason"));
        long ruleVersion = rs.getLong("rule_version");
        if (rs.wasNull()) {
            o.putNull("rule_version");
        } else {
            o.put("rule_version", ruleVersion);
        }
        o.set("events", mapper.readTree(rs.getString("events")));
        o.set("event_summary", mapper.readTree(rs.getString("event_summary")));
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
