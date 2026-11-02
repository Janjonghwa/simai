package com.ssafy.simai.domain.work;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.OffsetDateTime;
import java.time.ZoneOffset;
import java.time.format.DateTimeParseException;
import java.util.List;
import java.util.Optional;
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
                   w.lead_team,
                   (SELECT z.name FROM zone z WHERE z.zone_id = w.zone_id ORDER BY z.zone_version DESC LIMIT 1) AS zone_name,
                   c.critical, c.high
            FROM work_unit w
            LEFT JOIN LATERAL (
                SELECT count(*) FILTER (WHERE e.severity = 'CRITICAL') AS critical,
                       count(*) FILTER (WHERE e.severity = 'HIGH') AS high
                FROM risk_event e
                WHERE e.source = 'LIVE' AND e.raised_at BETWEEN w.starts_at AND w.ends_at
                  AND (w.zone_id IS NULL OR e.zone_id = w.zone_id)
            ) c ON true
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public WorkUnitRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 끝난 지 days 일이 안 된 작업(아직 안 끝난 작업 포함), 늦게 시작한 것부터 */
    public List<ObjectNode> findRecent(String siteId, Instant since) {
        return jdbc.query(SELECT + " WHERE w.site_id = :siteId AND w.ends_at >= :since ORDER BY w.starts_at DESC, w.work_id DESC",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("since", utc(since)), (rs, i) -> view(rs));
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
                    created_by, created_at, updated_at)
                VALUES (:siteId, :date, :name, :startsAt, :endsAt, :zoneId, :place, :people, :lead,
                    :userId, :now, :now)
                RETURNING work_id
                """, params, Long.class);
        return id == null ? 0 : id;
    }

    /** 전체를 바꾼다. 없으면 WORK_UNIT_NOT_FOUND */
    public void replace(String siteId, long workId, WorkUnitRequest request, Instant now) {
        int updated = jdbc.update("""
                UPDATE work_unit SET work_date = :date, name = :name, starts_at = :startsAt, ends_at = :endsAt,
                    zone_id = :zoneId, place = :place, planned_people = :people, lead_team = :lead,
                    updated_at = :now
                WHERE site_id = :siteId AND work_id = :workId
                """, params(siteId, request, now).addValue("workId", workId));
        if (updated == 0) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
    }

    public void delete(String siteId, long workId) {
        int deleted = jdbc.update("DELETE FROM work_unit WHERE site_id = :siteId AND work_id = :workId",
                new MapSqlParameterSource().addValue("siteId", siteId).addValue("workId", workId));
        if (deleted == 0) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
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
        return new MapSqlParameterSource()
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
        w.put("name", rs.getString("name"));
        w.put("from_ms", rs.getTimestamp("starts_at").getTime());
        w.put("to_ms", rs.getTimestamp("ends_at").getTime());
        putText(w, "zone_id", rs.getString("zone_id"));
        // 화면은 place 에 구역 이름 또는 구역 밖 장소를 보인다
        String place = rs.getString("place");
        putText(w, "place", place != null ? place : rs.getString("zone_name"));
        w.put("planned_people", rs.getInt("planned_people"));
        putText(w, "lead", rs.getString("lead_team"));
        ObjectNode counts = w.putObject("event_counts");
        counts.put("CRITICAL", rs.getLong("critical"));
        counts.put("HIGH", rs.getLong("high"));
        return w;
    }

    private static void putText(ObjectNode node, String field, String value) {
        if (value == null) {
            node.putNull(field);
        } else {
            node.put(field, value);
        }
    }
}
