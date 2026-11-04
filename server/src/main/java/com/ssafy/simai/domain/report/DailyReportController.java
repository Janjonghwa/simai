package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.ResponseStatus;
import org.springframework.web.bind.annotation.RestController;
import tools.jackson.databind.node.ObjectNode;

/**
 * 일일 안전 일지 API(S15P31A101-33, FR-122, 12.6). 생성은 관리자, 조회는 열람 이상(TBM 보고서 API 와 같다).
 * 생성은 비동기다: 202 와 report_id 를 주고, 조회의 status 가 GENERATING 에서 SUCCEEDED·FAILED 로 바뀐다.
 */
@RestController
@RequestMapping("/api/reports/daily")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class DailyReportController {

    private final DailyReportService service;

    DailyReportController(DailyReportService service) {
        this.service = service;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode generate(@RequestBody(required = false) DailyReportRequest request) {
        return service.start(request);
    }

    @GetMapping("/{reportId}")
    ObjectNode one(@PathVariable long reportId) {
        return service.getOne(reportId);
    }

    /** 그 날짜(비우면 오늘)의 최신 일지와 버전 이력 */
    @GetMapping
    ObjectNode byDate(@RequestParam(name = "date", required = false) String date) {
        return service.getByDate(date);
    }

    @ExceptionHandler(ReportInProgressException.class)
    ProblemDetail handleInProgress(ReportInProgressException e) {
        ProblemDetail problem = GlobalExceptionHandler.problem(e.getErrorCode());
        problem.setProperty("report_id", e.reportId());
        return problem;
    }
}
