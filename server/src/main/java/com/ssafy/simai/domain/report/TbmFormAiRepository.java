package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.time.LocalDate;
import java.util.List;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 날짜 작업 TBM 보고서 양식 칸 AI 초안(B9, S15P31A101-616) 저장소. 보고서 행은 새 report_version 없이 같은 행의
 * status 만 PENDING·SUCCEEDED·FAILED → GENERATING → SUCCEEDED·FAILED 로 바꾸고, 양식은 빈 칸만 채운다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class TbmFormAiRepository {

    /** 초안 시작 때 잠근 행에서 읽은 값. form 은 GENERATING 으로 바꾼 시점의 양식(요청의 existing 출처). */
    record DraftStart(long reportId, long workId, int reportVersion, int formRevision, LocalDate workDate, ObjectNode form) {
    }

    /** 초안을 채울 때 다시 잠근 행. */
    record FillTarget(String status, boolean locked, boolean latest, ObjectNode form) {
    }

    /** 초안 요청 zones 한 줄의 원본(구역 표시 이름·유형). */
    record SetupZone(String name, String zoneKind) {
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public TbmFormAiRepository(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 작업의 최신 TBM 보고서 id. 작업이 없으면 WORK_UNIT_NOT_FOUND, 보고서가 없으면 REPORT_NOT_FOUND. */
    long latestReportId(String siteId, long workId) {
        MapSqlParameterSource p = new MapSqlParameterSource("site", siteId).addValue("work", workId);
        Boolean exists = jdbc.queryForObject(
                "SELECT EXISTS (SELECT 1 FROM work_unit WHERE site_id = :site AND work_id = :work)", p, Boolean.class);
        if (!Boolean.TRUE.equals(exists)) {
            throw new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND);
        }
        return jdbc.queryForList("""
                SELECT report_id FROM report WHERE work_id = :work AND report_kind = 'TBM'
                ORDER BY report_version DESC, report_id DESC LIMIT 1
                """, p, Long.class).stream().findFirst()
                .orElseThrow(() -> new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND));
    }

    /**
     * 작업 행 → 보고서 행 순서로 잠그고(서명·예전 생성 경로와 같은 순서) 상태를 확인한 뒤 같은 행을 GENERATING 으로 바꾼다.
     * PENDING(첫 초안)·FAILED(재시도)·SUCCEEDED(다시 초안, 빈 칸만 채우므로 사람이 쓴 칸은 그대로)를 받는다.
     */
    DraftStart begin(String siteId, long reportId) {
        MapSqlParameterSource p = new MapSqlParameterSource("site", siteId).addValue("id", reportId);
        List<Long> works = jdbc.queryForList("""
                SELECT w.work_id FROM work_unit w JOIN report r ON r.work_id = w.work_id
                WHERE r.report_id = :id AND r.report_kind = 'TBM' AND w.site_id = :site
                FOR UPDATE OF w
                """, p, Long.class);
        if (works.isEmpty()) {
            throw new BusinessException(ErrorCode.Report.REPORT_NOT_FOUND);
        }
        record Row(long workId, int version, String status, String form, int revision, boolean locked, boolean latest,
                LocalDate workDate) { }
        Row row = jdbc.query("""
                SELECT r.work_id, r.report_version, r.status, r.form::text AS form, r.form_revision,
                       r.locked_at IS NOT NULL AS locked,
                       r.report_version = (SELECT max(x.report_version) FROM report x WHERE x.work_id = r.work_id) AS latest,
                       (SELECT w.work_date FROM work_unit w WHERE w.work_id = r.work_id) AS work_date
                FROM report r WHERE r.report_id = :id
                FOR UPDATE OF r
                """, p, (rs, i) -> new Row(rs.getLong("work_id"), rs.getInt("report_version"), rs.getString("status"),
                rs.getString("form"), rs.getInt("form_revision"), rs.getBoolean("locked"), rs.getBoolean("latest"),
                rs.getObject("work_date", LocalDate.class))).getFirst();
        if (row.form() == null) {
            throw new BusinessException(ErrorCode.Report.REPORT_NO_FORM);
        }
        if ("GENERATING".equals(row.status())) {
            throw new ReportInProgressException(reportId);
        }
        if (!row.latest()) {
            throw new BusinessException(ErrorCode.Report.REPORT_VERSION_STALE);
        }
        if (row.locked()) {
            throw new BusinessException(ErrorCode.Report.REPORT_LOCKED);
        }
        ObjectNode form = (ObjectNode) mapper.readTree(row.form());
        if (form.path("info").path("tasks").isEmpty()) {
            throw new BusinessException(ErrorCode.Report.REPORT_FORM_NO_TASKS);
        }
        // 같은 작업에 다른 GENERATING 행이 있으면(예전 생성 경로) 유일 색인에 걸리기 전에 409 로 알린다. 작업 행을 잠가 둬서 경쟁이 없다.
        Optional<Long> other = jdbc.queryForList(
                "SELECT report_id FROM report WHERE work_id = :work AND status = 'GENERATING'",
                new MapSqlParameterSource("work", row.workId()), Long.class).stream().findFirst();
        if (other.isPresent()) {
            throw new ReportInProgressException(other.get());
        }
        jdbc.update("""
                UPDATE report SET status = 'GENERATING', failure_reason = NULL, completed_at = NULL
                WHERE report_id = :id
                """, p);
        return new DraftStart(reportId, row.workId(), row.version(), row.revision(), row.workDate(), form);
    }

    /**
     * 그 작업의 현장 세팅(work_unit.active_setup_session_id)에서 만든 구역 중 지금 유효하고(valid_to 없음) 구성 묶음으로 발행한 것.
     * SetupZoneLayoutService.list 와 같은 기준이다. 세팅이 없으면 빈 목록(장소를 비운다, AGENTS 규칙 2).
     */
    List<SetupZone> setupZones(String siteId, long workId) {
        return jdbc.query("""
                SELECT z.name, z.zone_kind FROM setup_session_zone sz
                JOIN setup_session s ON s.setup_session_id = sz.setup_session_id
                JOIN work_unit w ON w.active_setup_session_id = s.setup_session_id AND w.work_id = s.work_unit_id
                JOIN zone z ON z.zone_id = sz.zone_id AND z.site_id = s.site_id AND z.valid_to IS NULL
                WHERE w.work_id = :work AND w.site_id = :site AND s.site_id = :site
                  AND EXISTS (SELECT 1 FROM config_bundle_item i JOIN config_bundle b ON b.config_version = i.config_version
                    WHERE b.site_id = :site AND i.item_type = 'ZONE' AND i.item_id = z.zone_id AND i.item_version = z.zone_version)
                ORDER BY sz.created_at, sz.zone_id
                """, new MapSqlParameterSource("work", workId).addValue("site", siteId),
                (rs, i) -> new SetupZone(rs.getString("name"), rs.getString("zone_kind")));
    }

    /** 초안을 채우기 전에 행을 다시 잠그고 지금 양식을 읽는다(그 사이 사람이 고쳤을 수 있다). */
    Optional<FillTarget> lockForFill(long reportId) {
        return jdbc.query("""
                SELECT r.status, r.form::text AS form, r.locked_at IS NOT NULL AS locked,
                       r.report_version = (SELECT max(x.report_version) FROM report x WHERE x.work_id = r.work_id) AS latest
                FROM report r WHERE r.report_id = :id
                FOR UPDATE OF r
                """, new MapSqlParameterSource("id", reportId), (rs, i) -> {
            String form = rs.getString("form");
            return new FillTarget(rs.getString("status"), rs.getBoolean("locked"), rs.getBoolean("latest"),
                    form == null ? null : (ObjectNode) mapper.readTree(form));
        }).stream().findFirst();
    }

    /** 채운 양식을 저장하고 form_revision 을 1 올린다(화면 낙관적 잠금이 새 판을 다시 읽게). */
    void saveForm(long reportId, ObjectNode form) {
        jdbc.update("""
                UPDATE report SET form = CAST(:form AS jsonb), form_revision = form_revision + 1
                WHERE report_id = :id
                """, new MapSqlParameterSource("id", reportId).addValue("form", mapper.writeValueAsString(form)));
    }
}
