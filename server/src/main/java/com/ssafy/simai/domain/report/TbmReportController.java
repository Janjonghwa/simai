package com.ssafy.simai.domain.report;

import com.fasterxml.jackson.annotation.JsonProperty;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.http.HttpStatus;
import org.springframework.http.ProblemDetail;
import org.springframework.security.access.prepost.PreAuthorize;
import org.springframework.security.core.annotation.AuthenticationPrincipal;
import org.springframework.security.oauth2.jwt.Jwt;
import org.springframework.web.bind.annotation.PutMapping;
import tools.jackson.databind.JsonNode;
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
    private final TbmFormAiService formAi;

    TbmReportController(TbmReportService service, TbmFormAiService formAi) {
        this.service = service;
        this.formAi = formAi;
    }

    @PostMapping
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode generate(@RequestBody(required = false) TbmReportRequest request, @AuthenticationPrincipal Jwt jwt) {
        return service.start(request, Long.parseLong(jwt.getSubject()));
    }

    /** 양식 칸 저장 본문. form 은 통째로, form_revision 은 마지막으로 읽은 값이다. */
    record FormRequest(JsonNode form, @JsonProperty("form_revision") Integer formRevision) {
    }

    /** 양식 칸을 통째로 저장한다(S15P31A101-609). 새 보고서 버전을 만들지 않는다. */
    @PutMapping("/{reportId}/form")
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode updateForm(@PathVariable long reportId, @RequestBody(required = false) FormRequest request) {
        if (request == null) {
            throw new BusinessException(ErrorCode.Report.INVALID_REPORT_FORM);
        }
        return service.updateForm(reportId, request.form(), request.formRevision());
    }

    /**
     * 날짜 작업 보고서 양식의 빈 칸을 AI 초안으로 채운다(S15P31A101-616 B9). 같은 행을 GENERATING 으로 바꾸고 202 를 돌려준 뒤 비동기로 채운다.
     * 새 보고서 버전을 만들지 않는다. 진행 상태는 GET 의 draft_status·form_revision 으로 본다.
     */
    @PostMapping("/{reportId}/form-draft")
    @ResponseStatus(HttpStatus.ACCEPTED)
    @PreAuthorize("hasRole('ADMIN')")
    ObjectNode formDraft(@PathVariable long reportId) {
        return formAi.startDraft(reportId);
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
