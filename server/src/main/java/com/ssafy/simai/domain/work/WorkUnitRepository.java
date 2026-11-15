package com.ssafy.simai.domain.work;

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
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
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
                   w.daily, w.setup_completed_at, w.helmet_required, w.vest_required,
                   EXISTS (SELECT 1 FROM setup_session s WHERE s.work_unit_id = w.work_id) AS has_setup,
                   t.report_id AS tbm_report_id, t.status AS tbm_status, t.submitted_at AS tbm_submitted_at,
                   (SELECT z.name FROM zone z WHERE z.zone_id = w.zone_id ORDER BY z.zone_version DESC LIMIT 1) AS zone_name,
                   c.critical, c.high,
                   -- 실행(S15P31A101-696): 작업 시작 ~ 작업 종료 한 번씩, 실행 번호순
                   (SELECT json_agg(json_build_object('run_no', r.run_no,
                        'started_at', floor(extract(epoch FROM r.started_at) * 1000)::bigint,
                        'ended_at', floor(extract(epoch FROM r.ended_at) * 1000)::bigint) ORDER BY r.run_no)::text
                    FROM work_run_window r WHERE r.work_id = w.work_id) AS runs
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
                    layout_id, work_type, helmet_required, vest_required, created_by, created_at, updated_at)
                VALUES (:siteId, :date, :name, :startsAt, :endsAt, :zoneId, :place, :people, :lead,
                    :layoutId, :workType, COALESCE(:helmet, true), COALESCE(:vest, true), :userId, :now, :now)
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

    /** 저장된 안전모·조끼 요구 그대로 시작한다 */
    public ObjectNode startSession(String siteId, long workId, Instant now) {
        return startSession(siteId, workId, now, null, null);
    }

    public ObjectNode startSession(String siteId, long workId, Instant now, Boolean helmet, Boolean vest) {
        return startSession(siteId, workId, now, helmet, vest, null);
    }

    /**
     * 작업 시작 = 새 실행 시작(S15P31A101-696). 처음(PLANNED)이면 실행 1, 실행을 끝낸 작업(DONE)이면 보고서를 제출하기 전까지
     * 「한 번 더 실행」으로 다음 번호를 연다(제출했으면 409 WORK_REPORT_SUBMITTED). 진행 중 실행이 있으면(이 작업이든 다른 작업이든)
     * 시작하지 않는다. 같은 현장의 동시 시작은 lockSession 의 현장 행 잠금이 한 줄로 세우고, 표의 부분 유일 색인
     * (work_one_in_progress_per_site·work_run_one_open_per_site)이 마지막으로 막는다.
     * 작업 전 확인(세팅 완료·서명 1명 이상·안전모·조끼 요구)은 실행마다 다시 본다. helmet·vest 가 있으면 시작과 함께 그 작업의 요구로
     * 저장한다(null 이면 저장된 값, 기본 필수, S15P31A101-654). 시작 묶음의 ppe_policy 가 이 값을 싣는다.
     * work_unit 은 하루 요약이다: actual_started_at 은 첫 실행 시작을 지키고(COALESCE), actual_ended_at 은 실행 중 NULL(V30 CHECK).
     */
    public ObjectNode startSession(String siteId, long workId, Instant now, Boolean helmet, Boolean vest, Long userId) {
        ObjectNode work = lockSession(siteId, workId);
        String status = work.path("status").asString();
        boolean again = "DONE".equals(status);
        if (!"PLANNED".equals(status) && !again) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        if (again && work.path("tbm_report").path("submitted_at").isNumber()) {
            // 보고서는 하루 끝에 한 번 제출한다. 제출본(잠금·해시)은 그때까지의 실행 기록이라 그 뒤 실행을 받지 않는다
            throw new BusinessException(ErrorCode.Work.WORK_REPORT_SUBMITTED);
        }
        boolean daily = work.path("daily").asBoolean();
        if (daily) {
            requireToday(true, LocalDate.parse(work.path("date").asString()), now);
        }
        // 이 작업의 세팅 완료(C17, setup_completed_at) 뒤에만 시작한다(명세 16장 수정 54 "세팅 완료 후에만 LIVE 관제 시작",
        // S15P31A101-642). 새 맵·보정·구역은 세팅 완료 때 관제에 들어가므로, 완료 없이 시작하면 이전 맵·보정·구역으로 관제한다.
        // 세팅 완료는 새 모델이면 정밀 맵까지, 재사용이면 기존 맵 맞추기까지 끝나야 되므로(S15P31A101-688 SetupCompletionGate)
        // -624 의 현장 세팅 READY 전 거절과 "미리보기로 관제 시작"(남은 굽기 회수) 금지도 이 기준에 들어 있다. 같은 현장의 다른
        // 세팅이 굽는 중이면 그 굽기는 시작 때 회수한다(대기열 -625, 대여 중 -597). 예전 작업(daily=false)도 같은 기준이다
        if (!work.path("setup_completed_at").isNumber()) {
            throw new BusinessException(ErrorCode.Work.WORK_SETUP_NOT_COMPLETED);
        }
        Integer signed = null;
        if (daily) {
            // 날짜 작업은 작업 전 확인에서 참석자 서명을 1명 이상 받은 뒤에 시작한다(최신 보고서 버전의 서명만 센다). 실행마다 다시 본다
            signed = jdbc.queryForObject("""
                    SELECT count(*) FROM report_signature s JOIN report r ON r.report_id = s.report_id
                    WHERE s.report_id = :report AND s.report_version = r.report_version AND s.signer_role = 'PARTICIPANT'
                    """, new MapSqlParameterSource("report", work.path("tbm_report").path("report_id").asLong(-1)), Integer.class);
            if (signed == null || signed < 1) {
                throw new BusinessException(ErrorCode.Work.WORK_NO_SIGNATURE);
            }
        }
        List<Long> active = jdbc.queryForList(
                "SELECT work_id FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS'",
                new MapSqlParameterSource("site", siteId), Long.class);
        if (!active.isEmpty()) throw new WorkAlreadyInProgressException(active.getFirst());
        MapSqlParameterSource p = new MapSqlParameterSource("site", siteId).addValue("id", workId).addValue("now", utc(now))
                .addValue("helmet", helmet).addValue("vest", vest).addValue("user", userId);
        // 실행 표 밖에서 끝난 작업(work_unit 만 DONE·PLANNED 로 바꾼 경로)의 열린 실행은 그 작업 종료 시각으로 닫는다.
        // 닫지 않으면 현장당 열린 실행 하나 색인에 걸려 새 실행을 열 수 없다
        jdbc.update("""
                UPDATE work_run r SET ended_at = GREATEST(COALESCE(w.actual_ended_at, :now), r.started_at)
                FROM work_unit w
                WHERE w.work_id = r.work_id AND r.site_id = :site AND r.ended_at IS NULL AND w.status <> 'IN_PROGRESS'
                """, p);
        // 실행 기록 없이 시각만 있는 작업(실행 표 밖 경로)은 그 시각을 실행 1로 먼저 남긴다(뷰가 실행 1을 잃지 않게)
        jdbc.update("""
                INSERT INTO work_run (work_id, run_no, site_id, started_at, ended_at)
                SELECT w.work_id, 1, w.site_id, w.actual_started_at, w.actual_ended_at FROM work_unit w
                WHERE w.work_id = :id AND w.actual_started_at IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM work_run r WHERE r.work_id = w.work_id)
                """, p);
        jdbc.update("""
                UPDATE work_unit SET status = 'IN_PROGRESS', actual_started_at = COALESCE(actual_started_at, :now),
                    actual_ended_at = NULL, updated_at = :now,
                    helmet_required = COALESCE(:helmet, helmet_required), vest_required = COALESCE(:vest, vest_required)
                WHERE site_id = :site AND work_id = :id
                """, p);
        ObjectNode started = findOne(siteId, workId).orElseThrow();
        // 시작 전 확인 스냅샷(시작 때 값을 그대로 남긴다)
        ObjectNode preflight = mapper.createObjectNode();
        if (signed == null) preflight.putNull("signer_count"); else preflight.put("signer_count", signed);
        JsonNode report = started.path("tbm_report");
        long reportId = report.path("report_id").asLong(-1);
        if (reportId > 0) preflight.put("report_id", reportId); else preflight.putNull("report_id");
        List<Integer> version = jdbc.queryForList("SELECT report_version FROM report WHERE report_id = :r",
                new MapSqlParameterSource("r", reportId), Integer.class);
        if (version.isEmpty()) preflight.putNull("report_version"); else preflight.put("report_version", version.getFirst());
        preflight.put("helmet", started.path("helmet").asString());
        preflight.put("vest", started.path("vest").asString());
        jdbc.update("""
                INSERT INTO work_run (work_id, run_no, site_id, started_at, started_by, preflight)
                VALUES (:id, (SELECT COALESCE(max(run_no), 0) + 1 FROM work_run WHERE work_id = :id), :site, :now, :user,
                    CAST(:preflight AS jsonb))
                """, p.addValue("preflight", mapper.writeValueAsString(preflight)));
        return findOne(siteId, workId).orElseThrow();
    }

    public ObjectNode endSession(String siteId, long workId, Instant now) {
        return endSession(siteId, workId, now, null);
    }

    /** 작업 종료 = 진행 중 실행 종료(S15P31A101-696). work_unit.actual_ended_at 은 마지막 실행 종료다 */
    public ObjectNode endSession(String siteId, long workId, Instant now, Long userId) {
        ObjectNode work = lockSession(siteId, workId);
        if (!"IN_PROGRESS".equals(work.path("status").asString())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        MapSqlParameterSource p = new MapSqlParameterSource("site", siteId).addValue("id", workId).addValue("now", utc(now))
                .addValue("user", userId);
        jdbc.update("""
                UPDATE work_unit SET status = 'DONE', actual_ended_at = GREATEST(:now, actual_started_at), updated_at = :now
                WHERE site_id = :site AND work_id = :id
                """, p);
        // 열린 실행 하나만 한 번 닫는다(추가 전용: 끝난 실행은 고치지 않는다)
        jdbc.update("""
                UPDATE work_run SET ended_at = GREATEST(:now, started_at), ended_by = :user
                WHERE work_id = :id AND ended_at IS NULL
                """, p);
        return findOne(siteId, workId).orElseThrow();
    }

    /**
     * 완료(DONE)를 계획(PLANNED)으로 되돌리고 실제 시각을 비운다(S15P31A101-649 시연 복구). 되돌리기 전 작업을 돌려준다.
     * 작업 날짜가 오늘(Asia/Seoul)인 작업만(지난 기록은 바꾸지 않는다), 제출한 TBM 보고서가 있으면 거절한다(제출본은 그 실행의 기록).
     * 다른 작업이 진행 중이면 거절한다(되돌려도 시작할 수 없다). 잠금 순서는 시작·종료와 같다(lockSession).
     */
    public ObjectNode reopenSession(String siteId, long workId, Instant now) {
        ObjectNode work = lockSession(siteId, workId);
        if (!"DONE".equals(work.path("status").asString())) {
            throw new BusinessException(ErrorCode.Work.WORK_INVALID_STATE);
        }
        // 예전 작업(daily=false)도 오늘 것만: 시작·종료와 달리 이건 기록을 지우는 일이라 범위를 좁힌다
        if (!LocalDate.parse(work.path("date").asString()).equals(LocalDate.ofInstant(now, SITE_ZONE))) {
            throw new BusinessException(ErrorCode.Work.WORK_NOT_TODAY);
        }
        if (work.path("tbm_report").path("submitted_at").isNumber()) {
            throw new BusinessException(ErrorCode.Work.WORK_REPORT_SUBMITTED);
        }
        List<Long> active = jdbc.queryForList(
                "SELECT work_id FROM work_unit WHERE site_id = :site AND status = 'IN_PROGRESS'",
                new MapSqlParameterSource("site", siteId), Long.class);
        if (!active.isEmpty()) throw new WorkAlreadyInProgressException(active.getFirst());
        MapSqlParameterSource p = new MapSqlParameterSource("site", siteId).addValue("id", workId).addValue("now", utc(now));
        // 첫 종료의 마무리 생성이 아직 돌면 그 결과가 되돌린 뒤에 옛 사실로 양식을 채운다(같은 request_id, 구분 장치 없음)
        Integer generating = jdbc.queryForObject("""
                SELECT count(*) FROM report WHERE work_id = :id AND report_kind = 'TBM'
                  AND (status = 'GENERATING' OR wrap_up_status = 'GENERATING')
                """, p, Integer.class);
        if (generating != null && generating > 0) {
            throw new BusinessException(ErrorCode.Work.WORK_WRAP_UP_GENERATING);
        }
        // 날짜 작업 종료 미팅(review.ending)은 비어 있을 때만 채워진다(WorkWrapUpRepository.completeEnding). 첫 실행 문구를 비워
        // 다음 종료가 새 실행의 사실로 다시 쓰게 한다. 관제 앱 양식 편집의 낙관적 잠금이 알도록 form_revision 을 올린다
        jdbc.update("""
                UPDATE report SET form = jsonb_set(form, '{review,ending}', '""'::jsonb), form_revision = form_revision + 1
                WHERE work_id = :id AND report_kind = 'TBM' AND form IS NOT NULL AND locked_at IS NULL
                  AND COALESCE(form #>> '{review,ending}', '') <> ''
                """, p);
        jdbc.update("""
                UPDATE work_unit SET status = 'PLANNED', actual_started_at = NULL, actual_ended_at = NULL, updated_at = :now
                WHERE site_id = :site AND work_id = :id
                """, p);
        return work;
    }

    /** 전체를 바꾼다. 없으면 WORK_UNIT_NOT_FOUND */
    public void replace(String siteId, long workId, WorkUnitRequest request, Instant now) {
        rejectDaily(siteId, workId);
        int updated = jdbc.update("""
                UPDATE work_unit SET work_date = :date, name = :name, starts_at = :startsAt, ends_at = :endsAt,
                    zone_id = :zoneId, place = :place, planned_people = :people, lead_team = :lead,
                    layout_id = :layoutId, work_type = :workType, updated_at = :now,
                    helmet_required = COALESCE(:helmet, helmet_required), vest_required = COALESCE(:vest, vest_required)
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
                .addValue("helmet", requirement(r.helmet()))
                .addValue("vest", requirement(r.vest()))
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

    /**
     * 안전모·조끼 요구 글자(REQUIRED·NOT_REQUIRED, 계약 data/ppe_policy 값)를 필수 여부로. 없으면 null(기본값·지금 값 그대로),
     * 다른 글자면 INVALID_WORK_UNIT
     */
    public static Boolean requirement(String value) {
        if (value == null) {
            return null;
        }
        return switch (value) {
            case "REQUIRED" -> true;
            case "NOT_REQUIRED" -> false;
            default -> throw invalid();
        };
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
        // 작업 중 안전모·조끼 요구(S15P31A101-654). 시작 묶음의 ppe_policy 가 이 값을 싣는다
        w.put("helmet", rs.getBoolean("helmet_required") ? "REQUIRED" : "NOT_REQUIRED");
        w.put("vest", rs.getBoolean("vest_required") ? "REQUIRED" : "NOT_REQUIRED");
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
        // 관제 카드 기준 사건 수(S15P31A101-751): 경보 기록 건수(event_counts)와 나란히 보인다
        IncidentCounter.Counts incidents = incidentCounts(rs.getLong("work_id"));
        ObjectNode incidentCounts = w.putObject("incident_counts");
        incidentCounts.put("CRITICAL", incidents.critical());
        incidentCounts.put("HIGH", incidents.high());
        // 실행 목록(S15P31A101-696). 시작 전이면 빈 배열, 진행 중 실행은 ended_at null 이다
        String runs = rs.getString("runs");
        w.set("runs", runs == null ? mapper.createArrayNode() : mapper.readTree(runs));
        return w;
    }

    /**
     * 작업 구간의 위험·경고를 관제 카드 기준 사건으로 센 수(event_counts 와 같은 구간·구역 범위). 이벤트는 바꾸지 않는다.
     */
    private IncidentCounter.Counts incidentCounts(long workId) {
        List<IncidentCounter.Alert> alerts = jdbc.query("""
                SELECT e.rule_id, e.zone_id, e.world_ids[1] AS person, e.severity, e.raised_at,
                       COALESCE(e.updated_at, e.raised_at) AS last_at
                FROM work_unit w
                JOIN risk_event e ON e.source = 'LIVE' AND e.severity IN ('CRITICAL', 'HIGH')
                    AND e.raised_at BETWEEN COALESCE(w.actual_started_at, w.starts_at)
                        AND COALESCE(w.actual_ended_at, CASE WHEN w.status = 'IN_PROGRESS' THEN CURRENT_TIMESTAMP ELSE w.ends_at END)
                    AND (w.zone_id IS NULL OR e.zone_id = w.zone_id)
                WHERE w.work_id = :w AND (NOT w.daily OR w.actual_started_at IS NOT NULL)
                """, new MapSqlParameterSource("w", workId), (rs, i) -> new IncidentCounter.Alert(
                rs.getString("rule_id"), rs.getString("zone_id"), rs.getString("person"), rs.getString("severity"),
                rs.getObject("raised_at", OffsetDateTime.class).toInstant(),
                rs.getObject("last_at", OffsetDateTime.class).toInstant()));
        return IncidentCounter.count(alerts);
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
