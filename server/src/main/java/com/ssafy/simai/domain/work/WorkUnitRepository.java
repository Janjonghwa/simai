package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.setup.SetupService;
import com.ssafy.simai.domain.setup.SetupSessionBlockingException;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneId;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * TBM 작업(V21 work_unit). 응답은 관제 앱 히스토리 화면의 작업 모양(features/history/types.ts workUnitSchema)과 같은 snake_case 다.
 * event_counts 는 작업 구간 [from, to] 에 시작한(raised_at) LIVE 이벤트의 등급별 수다. 구역 작업은 그 구역 이벤트만,
 * 구역 밖 작업은 현장 전체를 센다(작업 다시 보기가 그 구간의 현장 전체를 보여 준다).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class WorkUnitRepository {

    static final int MAX_NAME = 100;
    static final int MAX_PLACE = 100;
    static final int MAX_LEAD = 50;
    static final int MAX_PEOPLE = 100;
    static final Duration MAX_SPAN = Duration.ofHours(24);
    // 9999-12-31T23:59:59.999Z. 그보다 큰 값은 timestamptz 로 바꾸다 500 이 난다
    static final long MAX_EPOCH_MS = 253_402_300_799_999L;

    private static final String SELECT = """
            SELECT w.work_id, w.work_date, w.name, w.starts_at, w.ends_at, w.zone_id, w.place, w.planned_people,
                   w.lead_team, w.layout_id, w.work_type, w.status, w.actual_started_at, w.actual_ended_at, w.active_setup_session_id,
                   w.daily, w.setup_completed_at,
                   EXISTS (SELECT 1 FROM setup_session s WHERE s.work_unit_id = w.work_id) AS has_setup,
                   t.report_id AS tbm_report_id, t.status AS tbm_status, t.submitted_at AS tbm_submitted_at,
                   (SELECT z.name FROM zone z WHERE z.zone_id = w.zone_id ORDER BY z.zone_version DESC LIMIT 1) AS zone_name,
                   c.critical, c.high
            FROM work_unit w
            LEFT JOIN LATERAL (
                -- 최신 TBM 보고서 요약(달력이 작업마다 보고서를 따로 조회하지 않게)
                SELECT r.report_id, r.status, r.submitted_at
                FROM report r WHERE r.work_id = w.work_id AND r.report_kind = 'TBM'
                ORDER BY r.report_version DESC, r.report_id DESC LIMIT 1
            ) t ON true
            LEFT JOIN LATERAL (
                SELECT count(*) FILTER (WHERE e.severity = 'CRITICAL') AS critical,
                       count(*) FILTER (WHERE e.severity = 'HIGH') AS high
                FROM risk_event e
                WHERE e.source = 'LIVE' AND e.raised_at BETWEEN COALESCE(w.actual_started_at, w.starts_at)
                    AND COALESCE(w.actual_ended_at, CASE WHEN w.status = 'IN_PROGRESS' THEN CURRENT_TIMESTAMP ELSE w.ends_at END)
                  AND (w.zone_id IS NULL OR e.zone_id = w.zone_id)
                  -- 날짜 작업은 계획 시간이 하루 전체라 실제 시작 전에는 세지 않는다
                  AND (NOT w.daily OR w.actual_started_at IS NOT NULL)
            ) c ON true
            """;

    /** 현장이 한국이라 날짜를 Asia/Seoul 로 자른다(현장별 시간대 칸이 없다, DailyReportService 와 같다) */
    public static final ZoneId SITE_ZONE = ZoneId.of("Asia/Seoul");

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public WorkUnitRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 끝난 지 days 일이 안 된 작업(아직 안 끝난 작업 포함), 늦게 시작한 것부터 */
    public List<ObjectNode> findRecent(String siteId, Instant since) {
        return jdbc.query(SELECT + """
                 WHERE w.site_id = :siteId AND (w.status = 'IN_PROGRESS' OR COALESCE(w.actual_ended_at, w.ends_at) >= :since)
                 ORDER BY COALESCE(w.actual_started_at, w.starts_at) DESC, w.work_id DESC
                """,
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("since", utc(since)), (rs, i) -> view(rs));
    }

    /** 작업 날짜가 [from, to](양끝 포함)인 작업. 달력이 한 달씩 받는다. 예전 작업도 함께 나온다 */
    public List<ObjectNode> findByDateRange(String siteId, LocalDate from, LocalDate to) {
        return jdbc.query(SELECT + """
                 WHERE w.site_id = :siteId AND w.work_date BETWEEN :from AND :to
                 ORDER BY w.work_date, COALESCE(w.actual_started_at, w.starts_at), w.work_id
                """,
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("from", from).addValue("to", to),
                (rs, i) -> view(rs));
    }

    /**
     * 그날 날짜 작업이 있으면 그 id, 없으면 만들어서 id 를 돌려준다(여러 번 불러도 하나, 색인 work_unit_one_daily_per_date).
     * 계획 시간은 그날 0시~24시(Asia/Seoul)로 채운다. 이름은 비워 두고 작업내용을 고르면 보고서 양식 저장이 채운다.
     * 오늘 이전 날짜는 새로 만들지 않는다(이미 있으면 돌려준다).
     */
    public long findOrCreateDaily(String siteId, LocalDate date, long userId, Instant now) {
        MapSqlParameterSource p = new MapSqlParameterSource().addValue("siteId", siteId).addValue("date", date);
        List<Long> existing = jdbc.queryForList(
                "SELECT work_id FROM work_unit WHERE site_id = :siteId AND work_date = :date AND daily", p, Long.class);
        if (!existing.isEmpty()) {
            return existing.getFirst();
        }
        if (date.isBefore(LocalDate.ofInstant(now, SITE_ZONE))) {
            throw new BusinessException(ErrorCode.Work.WORK_DATE_PAST);
        }
        jdbc.update("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, planned_people, daily,
                    created_by, created_at, updated_at)
                VALUES (:siteId, :date, NULL, :startsAt, :endsAt, 0, true, :userId, :now, :now)
                ON CONFLICT (site_id, work_date) WHERE daily DO NOTHING
                """, p.addValue("startsAt", utc(date.atStartOfDay(SITE_ZONE).toInstant()))
                .addValue("endsAt", utc(date.plusDays(1).atStartOfDay(SITE_ZONE).toInstant()))
                .addValue("userId", userId).addValue("now", utc(now)));
        Long id = jdbc.queryForObject(
                "SELECT work_id FROM work_unit WHERE site_id = :siteId AND work_date = :date AND daily", p, Long.class);
        return id == null ? 0 : id;
    }

    /**
     * 날짜 작업은 작업 날짜가 오늘(Asia/Seoul)일 때만 세팅 시작·세팅 완료·작업 시작을 받는다(409 WORK_NOT_TODAY).
     * 예전 작업(daily=false)은 검사하지 않는다. 세팅 쪽(SetupService)도 이 메서드를 부른다.
     */
    public void requireToday(String siteId, long workId, Instant now) {
        record Day(LocalDate date, boolean daily) { }
        List<Day> rows = jdbc.query(
                "SELECT work_date, daily FROM work_unit WHERE site_id = :site AND work_id = :id",
                new MapSqlParameterSource("site", siteId).addValue("id", workId),
                (rs, i) -> new Day(rs.getObject("work_date", LocalDate.class), rs.getBoolean("daily")));
        if (rows.isEmpty()) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        requireToday(rows.getFirst().daily(), rows.getFirst().date(), now);
    }

    private static void requireToday(boolean daily, LocalDate workDate, Instant now) {
        if (daily && !workDate.equals(LocalDate.ofInstant(now, SITE_ZONE))) {
            throw new BusinessException(ErrorCode.Work.WORK_NOT_TODAY);
        }
    }

    public Optional<ObjectNode> findOne(String siteId, long workId) {
        return jdbc.query(SELECT + " WHERE w.site_id = :siteId AND w.work_id = :workId",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId), (rs, i) -> view(rs))
                .stream().findFirst();
    }

    public long create(String siteId, WorkUnitRequest request, long userId, Instant now) {
        MapSqlParameterSource params = params(siteId, request, now).addValue("userId", userId);
        Long id = jdbc.queryForObject("""
                INSERT INTO work_unit (site_id, work_date, name, starts_at, ends_at, zone_id, place, planned_people, lead_team,
                    layout_id, work_type, created_by, created_at, updated_at)
                VALUES (:siteId, :date, :name, :startsAt, :endsAt, :zoneId, :place, :people, :lead,
                    :layoutId, :workType, :userId, :now, :now)
                RETURNING work_id
                """, params, Long.class);
        return id == null ? 0 : id;
    }

    /** 사이트 행은 서로 다른 작업의 동시 시작도 직렬화한다. 보고서 생성과 같은 작업 행 잠금 순서를 유지한다. */
    public ObjectNode lockSession(String siteId, long workId) {
        jdbc.queryForList("SELECT site_id FROM site WHERE site_id = :site FOR UPDATE",
                new MapSqlParameterSource("site", siteId), String.class);
        List<Long> ids = jdbc.queryForList(
                "SELECT work_id FROM work_unit WHERE site_id = :site AND work_id = :id FOR UPDATE",
                new MapSqlParameterSource("site", siteId).addValue("id", workId), Long.class);
        if (ids.isEmpty()) throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        return findOne(siteId, workId).orElseThrow();
    }

    public ObjectNode startSession(String siteId, long workId, Instant now) {
        ObjectNode work = lockSession(siteId, workId);
        if (!"PLANNED".equals(work.path("status").asString())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        if (work.path("daily").asBoolean()) {
            requireToday(true, LocalDate.parse(work.path("date").asString()), now);
            // 날짜 작업은 작업 전 확인에서 참석자 서명을 1명 이상 받은 뒤에 시작한다(최신 보고서 버전의 서명만 센다)
            Integer signed = jdbc.queryForObject("""
                    SELECT count(*) FROM report_signature s JOIN report r ON r.report_id = s.report_id
                    WHERE s.report_id = :report AND s.report_version = r.report_version AND s.signer_role = 'PARTICIPANT'
                    """, new MapSqlParameterSource("report", work.path("tbm_report").path("report_id").asLong(-1)), Integer.class);
            if (signed == null || signed < 1) {
                throw new BusinessException(ErrorCode.Work.WORK_NO_SIGNATURE);
            }
        }
        // 같은 현장의 현장 세팅이 미리보기 맵(READY_FOR_ZONES) 전이면 시작하지 않는다(명세 3.2 5항: 작업 시작은 미리보기 맵으로
        // 관제를 시작하고 뒤쪽 굽기만 회수한다). READY 전 단계(배경 수집·보정·MapAnything·미리보기)는 회수 대상이 아니라 LIVE 중에도
        // GPU 를 쓰고, 그 세팅의 보정은 작업이 끝날 때까지 Tier 0 에 발행되지 않는다(S15P31A101-624 M5)
        // 막고 있는 세션과 그 작업을 응답에 싣는다. 다른 날짜 작업의 세션이면 화면이 찾아가 취소할 수 있게(S15P31A101-641)
        List<Map<String, Object>> notReady = jdbc.queryForList("""
                SELECT setup_session_id, work_unit_id FROM setup_session WHERE site_id = :site AND status IN (:pre)
                ORDER BY started_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId).addValue("pre", SetupService.PRE_READY_STATUSES));
        if (!notReady.isEmpty()) {
            throw new SetupSessionBlockingException(ErrorCode.Work.WORK_SETUP_NOT_READY,
                    (UUID) notReady.getFirst().get("setup_session_id"), ((Number) notReady.getFirst().get("work_unit_id")).longValue());
        }
        List<Long> active = jdbc.queryForList(
                "SELECT work_id FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS'",
                new MapSqlParameterSource("site", siteId), Long.class);
        if (!active.isEmpty()) throw new WorkAlreadyInProgressException(active.getFirst());
        jdbc.update("""
                UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = :now, updated_at = :now
                WHERE site_id = :site AND work_id = :id
                """, new MapSqlParameterSource("site", siteId).addValue("id", workId).addValue("now", utc(now)));
        return findOne(siteId, workId).orElseThrow();
    }

    public ObjectNode endSession(String siteId, long workId, Instant now) {
        ObjectNode work = lockSession(siteId, workId);
        if (!"IN_PROGRESS".equals(work.path("status").asString())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        jdbc.update("""
                UPDATE work_unit SET status = 'DONE', actual_ended_at = GREATEST(:now, actual_started_at), updated_at = :now
                WHERE site_id = :site AND work_id = :id
                """, new MapSqlParameterSource("site", siteId).addValue("id", workId).addValue("now", utc(now)));
        return findOne(siteId, workId).orElseThrow();
    }

    /** 전체를 바꾼다. 없으면 WORK_UNIT_NOT_FOUND */
    public void replace(String siteId, long workId, WorkUnitRequest request, Instant now) {
        rejectDaily(siteId, workId);
        int updated = jdbc.update("""
                UPDATE work_unit SET work_date = :date, name = :name, starts_at = :startsAt, ends_at = :endsAt,
                    zone_id = :zoneId, place = :place, planned_people = :people, lead_team = :lead,
                    layout_id = :layoutId, work_type = :workType, updated_at = :now
                WHERE site_id = :siteId AND work_id = :workId
                """, params(siteId, request, now).addValue("workId", workId));
        if (updated == 0) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
    }

    public void delete(String siteId, long workId) {
        rejectDaily(siteId, workId);
        int deleted = jdbc.update("DELETE FROM work_unit WHERE site_id = :siteId AND work_id = :workId",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId));
        if (deleted == 0) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
    }

    /** 날짜 작업은 날짜·시간·이름을 서버가 정하므로 작업 수정·삭제 API 로 바꾸지 않는다(409 WORK_INVALID_STATE) */
    private void rejectDaily(String siteId, long workId) {
        Boolean daily = jdbc.query("SELECT daily FROM work_unit WHERE site_id = :site AND work_id = :id",
                new MapSqlParameterSource("site", siteId).addValue("id", workId), (rs, i) -> rs.getBoolean("daily"))
                .stream().findFirst().orElse(false);
        if (daily) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
    }

    /** 검사하고 SQL 값으로 바꾼다. 구역은 이 현장의 구역 ID(어느 버전이든)여야 한다(보관된 구역의 지난 작업도 고칠 수 있게) */
    private MapSqlParameterSource params(String siteId, WorkUnitRequest r, Instant now) {
        if (r == null) {
            throw invalid();
        }
        LocalDate date = parseDate(r.date());
        String name = text(r.name(), MAX_NAME, true);
        if (r.fromMs() == null || r.toMs() == null || r.fromMs() < 0 || r.toMs() > MAX_EPOCH_MS
                || r.toMs() <= r.fromMs() || r.toMs() - r.fromMs() > MAX_SPAN.toMillis()) {
            throw invalid();
        }
        String zoneId = text(r.zoneId(), MAX_NAME, false);
        String place = text(r.place(), MAX_PLACE, false);
        String lead = text(r.lead(), MAX_LEAD, false);
        int people = r.plannedPeople() == null ? -1 : r.plannedPeople();
        if (people < 0 || people > MAX_PEOPLE) {
            throw invalid();
        }
        if (zoneId != null) {
            Boolean exists = jdbc.queryForObject(
                    "SELECT EXISTS (SELECT 1 FROM zone WHERE site_id = :siteId AND zone_id = :zoneId)",
                    new MapSqlParameterSource().addValue("siteId", siteId).addValue("zoneId", zoneId), Boolean.class);
            if (!Boolean.TRUE.equals(exists)) {
                throw new BusinessException(ErrorCode.Work.UNKNOWN_WORK_ZONE);
            }
        }
        if (r.workType() != null && !WorkTypes.contains(r.workType())) {
            throw invalid();
        }
        if (r.layoutId() != null && !Boolean.TRUE.equals(jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM layout_plan WHERE site_id = :site AND layout_id = :id)",
                new MapSqlParameterSource("site", siteId).addValue("id", r.layoutId()), Boolean.class))) {
            throw invalid();
        }
        return new MapSqlParameterSource()
                .addValue("layoutId", r.layoutId())
                .addValue("workType", r.workType())
                .addValue("siteId", siteId)
                .addValue("date", date)
                .addValue("name", name)
                .addValue("startsAt", utc(Instant.ofEpochMilli(r.fromMs())))
                .addValue("endsAt", utc(Instant.ofEpochMilli(r.toMs())))
                .addValue("zoneId", zoneId)
                .addValue("place", place)
                .addValue("people", people)
                .addValue("lead", lead)
                .addValue("now", utc(now));
    }

    private static LocalDate parseDate(String value) {
        if (value == null || !value.matches("\\d{4}-\\d{2}-\\d{2}")) {
            throw invalid();
        }
        try {
            return LocalDate.parse(value);
        } catch (DateTimeParseException e) {
            throw invalid();
        }
    }

    /** 앞뒤 공백을 뺀다. 비면 null(required 면 오류). 길이는 코드 포인트로 세고 PostgreSQL text 가 받지 않는 NUL 은 거부한다 */
    private static String text(String value, int max, boolean required) {
        String stripped = value == null ? "" : value.strip();
        if (stripped.isEmpty()) {
            if (required) {
                throw invalid();
            }
            return null;
        }
        if (stripped.indexOf(0) >= 0 || stripped.codePointCount(0, stripped.length()) > max) {
            throw invalid();
        }
        return stripped;
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Work.INVALID_WORK_UNIT);
    }

    private static OffsetDateTime utc(Instant instant) {
        return OffsetDateTime.ofInstant(instant, ZoneOffset.UTC);
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        ObjectNode w = mapper.createObjectNode();
        // 관제 앱은 work_id 를 글자로 다룬다
        w.put("work_id", String.valueOf(rs.getLong("work_id")));
        w.put("date", rs.getObject("work_date", LocalDate.class).toString());
        // 날짜 작업은 작업내용을 고르기 전에 이름이 없다. 화면들이 name 을 글자로 다루므로 빈 글자로 낸다
        String name = rs.getString("name");
        w.put("name", name == null ? "" : name);
        w.put("from_ms", rs.getTimestamp("starts_at").getTime());
        w.put("to_ms", rs.getTimestamp("ends_at").getTime());
        putText(w, "zone_id", rs.getString("zone_id"));
        // 화면은 place 에 구역 이름 또는 구역 밖 장소를 보인다
        String place = rs.getString("place");
        putText(w, "place", place != null ? place : rs.getString("zone_name"));
        w.put("planned_people", rs.getInt("planned_people"));
        putText(w, "lead", rs.getString("lead_team"));
        long layoutId = rs.getLong("layout_id");
        if (rs.wasNull()) w.putNull("layout_id"); else w.put("layout_id", layoutId);
        putText(w, "work_type", rs.getString("work_type"));
        w.put("status", rs.getString("status"));
        putText(w, "active_setup_session_id", rs.getString("active_setup_session_id"));
        for (String field : List.of("actual_started_at", "actual_ended_at")) {
            var timestamp = rs.getTimestamp(field);
            if (timestamp == null) w.putNull(field); else w.put(field, timestamp.getTime());
        }
        w.put("daily", rs.getBoolean("daily"));
        var setupDone = rs.getTimestamp("setup_completed_at");
        if (setupDone == null) w.putNull("setup_completed_at"); else w.put("setup_completed_at", setupDone.getTime());
        long reportId = rs.getLong("tbm_report_id");
        boolean noReport = rs.wasNull();
        var submittedAt = rs.getTimestamp("tbm_submitted_at");
        if (noReport) {
            w.putNull("tbm_report");
        } else {
            ObjectNode report = w.putObject("tbm_report");
            report.put("report_id", reportId);
            String status = rs.getString("tbm_status");
            // PENDING(초안 작성 전)은 draft_status null 이다(보고서 조회와 같다)
            putText(report, "draft_status", "PENDING".equals(status) ? null : status);
            if (submittedAt == null) report.putNull("submitted_at"); else report.put("submitted_at", submittedAt.getTime());
        }
        w.put("progress", progress(rs.getString("status"), submittedAt != null, setupDone != null, rs.getBoolean("has_setup")));
        ObjectNode counts = w.putObject("event_counts");
        counts.put("CRITICAL", rs.getLong("critical"));
        counts.put("HIGH", rs.getLong("high"));
        return w;
    }

    /** 달력 진행 칩. 뒤 단계부터 본다: 제출 → 종료 → 진행 중 → 세팅 완료 → 세팅 중 → 보고서 */
    static String progress(String status, boolean submitted, boolean setupCompleted, boolean hasSetup) {
        if (submitted) return "SUBMITTED";
        if ("DONE".equals(status)) return "ENDED";
        if ("IN_PROGRESS".equals(status)) return "IN_PROGRESS";
        if (setupCompleted) return "SETUP_DONE";
        if (hasSetup) return "SETUP";
        return "REPORT";
    }

    private static void putText(ObjectNode node, String field, String value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }
}
