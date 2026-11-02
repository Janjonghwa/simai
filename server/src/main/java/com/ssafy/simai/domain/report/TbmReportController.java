package com.ssafy.simai.domain.report;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
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
 * TBM 작업 단위 보고서 API(POST·GET /api/reports/tbm, S15P31A101-418, .agy/tbm-interface.md B절).
 * 생성은 관리자(ROLE_ADMIN), 단건·목록 조회는 열람 이상(인증된 계정).
 */
@RestController
@RequestMapping("/api/reports/tbm")
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class TbmReportController {

    private final TbmReportService service;

    TbmReportController(TbmReportService service) {
        this.service = service;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode generate(@RequestBody(required = false) TbmReportRequest request) {
        return service.start(request);
    }

    @GetMapping("/{reportId}")
    ObjectNode one(@PathVariable long reportId) {
        return service.getOne(reportId);
    }

    @GetMapping
    ObjectNode byWork(@RequestParam(name = "work_id", required = false) Long workId) {
        if (workId == null || workId < 1) {
            throw new BusinessException(ErrorCode.Report.INVALID_REPORT_REQUEST);
        }
        return service.getByWork(workId);
    }

    @ExceptionHandler(ReportInProgressException.class)
    ProblemDetail handleInProgress(ReportInProgressException e) {
        ProblemDetail problem = GlobalExceptionHandler.problem(e.getErrorCode());
        problem.setProperty("report_id", e.reportId());
        return problem;
    }
}
