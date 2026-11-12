package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.domain.report.TbmReportService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.LocalDate;
import java.time.format.DateTimeParseException;
import java.time.temporal.ChronoUnit;
import java.util.List;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.transaction.support.TransactionTemplate;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.PutMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * TBM 작업 저장·목록(히스토리 화면의 작업 다시 보기, docs/design/server-work-units.md). 쓰기는 관리자, 조회는 열람 이상.
 * 목록은 끝난 지 days 일(기본 7, 1~90)이 안 된 작업이다.
 */
@RestController
@RequestMapping("/api/work-units")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class WorkUnitController {

    static final int DEFAULT_DAYS = 7;
    static final int MAX_DAYS = 90;
    /** 달력 한 달(앞뒤 주 포함 6주)이 넉넉히 들어가는 기간 */
    static final int MAX_RANGE_DAYS = 92;

    private final WorkUnitRepository works;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final WorkSessionService sessions;
    private final TbmReportService reports;

    WorkUnitController(WorkUnitRepository works, SiteLookup sites, TransactionTemplate transaction, Clock clock,
            WorkSessionService sessions, TbmReportService reports) {
        this.reports = reports;
        this.works = works;
        this.sites = sites;
        this.transaction = transaction;
        this.clock = clock;
        this.sessions = sessions;
    }

    /** 목록 응답. 관제 앱이 works 를 하나씩 검사한다 */
    record WorkUnitsResponse(List<ObjectNode> works) {
    }

    /**
     * from·to(YYYY-MM-DD, 양끝 포함, 92일 이내)를 주면 작업 날짜로 거른다(달력, S15P31A101-610).
     * 주지 않으면 예전처럼 끝난 지 days 일이 안 된 작업이다.
     */
    @GetMapping
    WorkUnitsResponse list(@RequestParam(defaultValue = "" + DEFAULT_DAYS) int days,
            @RequestParam(required = false) String from, @RequestParam(required = false) String to) {
        if (from != null || to != null) {
            LocalDate start = parseDay(from, ErrorCode.Work.INVALID_WORK_RANGE);
            LocalDate end = parseDay(to, ErrorCode.Work.INVALID_WORK_RANGE);
            if (end.isBefore(start) || ChronoUnit.DAYS.between(start, end) >= MAX_RANGE_DAYS) {
                throw new BusinessException(ErrorCode.Work.INVALID_WORK_RANGE);
            }
            return new WorkUnitsResponse(works.findByDateRange(sites.siteId(), start, end));
        }
        if (days < 1 || days > MAX_DAYS) {
            throw new BusinessException(ErrorCode.Work.INVALID_WORK_DAYS);
        }
        Instant since = clock.instant().minus(Duration.ofDays(days));
        return new WorkUnitsResponse(works.findRecent(sites.siteId(), since));
    }

    /**
     * 날짜 작업 만들기·찾기(S15P31A101-610). 그날 날짜 작업이 있으면 그대로, 없으면 빈 TBM 보고서와 함께 만든다.
     * 오늘 이전 날짜는 새로 만들지 않는다(409 WORK_DATE_PAST).
     */
    @PutMapping("/daily/{date}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode daily(@PathVariable String date, @AuthenticationPrincipal Jwt jwt) {
        LocalDate day = parseDay(date, ErrorCode.Work.INVALID_WORK_UNIT);
        String siteId = sites.siteId();
        long userId = Long.parseLong(jwt.getSubject());
        Long id = transaction.execute(s -> {
            Instant now = clock.instant();
            long workId = works.findOrCreateDaily(siteId, day, userId, now);
            // 지난 날짜는 다시 보기만 한다. 보고서가 없던 예전 데이터에 새 기록을 만들지 않는다
            if (!day.isBefore(LocalDate.ofInstant(now, WorkUnitRepository.SITE_ZONE))) {
                reports.createDailyBlank(workId, userId);
            }
            return workId;
        });
        return works.findOne(siteId, id == null ? 0 : id)
                .orElseThrow(() -> new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND));
    }

    private static LocalDate parseDay(String value, ErrorCode error) {
        if (value == null || !value.matches("\\d{4}-\\d{2}-\\d{2}")) {
            throw new BusinessException(error);
        }
        try {
            return LocalDate.parse(value);
        } catch (DateTimeParseException e) {
            throw new BusinessException(error);
        }
    }

    @PostMapping("/{workId}/start")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode start(@PathVariable long workId, @AuthenticationPrincipal Jwt jwt) {
        return sessions.start(workId, Long.parseLong(jwt.getSubject()));
    }

    @PostMapping("/{workId}/end")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode end(@PathVariable long workId, @AuthenticationPrincipal Jwt jwt) {
        return sessions.end(workId, Long.parseLong(jwt.getSubject()));
    }

    /** 시연 복구: 실수로 완료한 오늘 작업을 계획으로 되돌린다(S15P31A101-649). 관제 앱 화면 없이 Swagger·curl 로 부른다 */
    @PostMapping("/{workId}/reopen")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode reopen(@PathVariable long workId, @AuthenticationPrincipal Jwt jwt) {
        return sessions.reopen(workId, Long.parseLong(jwt.getSubject()));
    }

    @ExceptionHandler(WorkAlreadyInProgressException.class)
    ProblemDetail alreadyInProgress(WorkAlreadyInProgressException e) {
        ProblemDetail problem = GlobalExceptionHandler.problem(e.getErrorCode());
        problem.setProperty("work_id", String.valueOf(e.workId()));
        return problem;
    }

    @GetMapping("/{workId}")
    ObjectNode one(@PathVariable long workId) {
        return works.findOne(sites.siteId(), workId)
                .orElseThrow(() -> new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND));
    }

    @PostMapping
    @ResponseStatus(HttpStatus.CREATED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode create(@RequestBody(required = false) WorkUnitRequest request, @AuthenticationPrincipal Jwt jwt) {
        String siteId = sites.siteId();
        long id = transaction.execute(s -> works.create(siteId, request, Long.parseLong(jwt.getSubject()), clock.instant()));
        return works.findOne(siteId, id).orElseThrow(() -> new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND));
    }

    /** 전체 바꾸기(회의록 화면은 작업 하나를 통째로 저장한다) */
    @PutMapping("/{workId}")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode replace(@PathVariable long workId, @RequestBody(required = false) WorkUnitRequest request) {
        String siteId = sites.siteId();
        transaction.executeWithoutResult(s -> works.replace(siteId, workId, request, clock.instant()));
        return works.findOne(siteId, workId).orElseThrow(() -> new BusinessException(ErrorCode.Work.WORK_UNIT_NOT_FOUND));
    }

    @DeleteMapping("/{workId}")
    @ResponseStatus(HttpStatus.NO_CONTENT)
    @PreAuthorize("hasRole('ADMIN')")
    void delete(@PathVariable long workId) {
        transaction.executeWithoutResult(s -> works.delete(sites.siteId(), workId));
    }
}
