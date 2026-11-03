package com.ssafy.simai.domain.work;

import com.ssafy.simai.domain.config.SiteLookup;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
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

    private final WorkUnitRepository works;
    private final SiteLookup sites;
    private final TransactionTemplate transaction;
    private final Clock clock;
    private final WorkSessionService sessions;

    WorkUnitController(WorkUnitRepository works, SiteLookup sites, TransactionTemplate transaction, Clock clock,
            WorkSessionService sessions) {
        this.works = works;
        this.sites = sites;
        this.transaction = transaction;
        this.clock = clock;
        this.sessions = sessions;
    }

    /** 목록 응답. 관제 앱이 works 를 하나씩 검사한다 */
    record WorkUnitsResponse(List<ObjectNode> works) {
    }

    @GetMapping
    WorkUnitsResponse list(@RequestParam(defaultValue = "" + DEFAULT_DAYS) int days) {
        if (days < 1 || days > MAX_DAYS) {
            throw new BusinessException(ErrorCode.Work.INVALID_WORK_DAYS);
        }
        Instant since = clock.instant().minus(Duration.ofDays(days));
        return new WorkUnitsResponse(works.findRecent(sites.siteId(), since));
    }

    @PostMapping("/{workId}/start")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode start(@PathVariable long workId) {
        return sessions.start(workId);
    }

    @PostMapping("/{workId}/end")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode end(@PathVariable long workId) {
        return sessions.end(workId);
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
