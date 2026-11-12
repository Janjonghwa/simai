package com.ssafy.simai.global.exception;

import com.ssafy.simai.domain.scene.SceneRegenerateInProgressException;
import org.springframework.http.HttpHeaders;
import org.springframework.http.HttpStatus;
import org.springframework.http.HttpStatusCode;
import org.springframework.http.ProblemDetail;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;
import org.springframework.web.context.request.WebRequest;
import org.springframework.web.servlet.mvc.method.annotation.ResponseEntityExceptionHandler;

/**
 * 오류 본문은 ProblemDetail 이다. 관제 앱 http.ts 가 detail 을 읽고, code 로 오류 종류를 가른다.
 * Spring MVC 표준 예외(본문 검증 실패·읽을 수 없는 본문·없는 경로·허용하지 않는 메서드 등)도 이 클래스가 받는다
 * (ResponseEntityExceptionHandler 를 이어받아 Boot 기본 처리기를 대신한다). 그 응답에는 code 가 없었으므로 HTTP 상태 이름
 * (BAD_REQUEST, METHOD_NOT_ALLOWED …)을 code 로 싣는다.
 */
@RestControllerAdvice
public class GlobalExceptionHandler extends ResponseEntityExceptionHandler {

    @ExceptionHandler(SceneRegenerateInProgressException.class)
    ProblemDetail handleSceneRegenerateInProgress(SceneRegenerateInProgressException e) {
        ProblemDetail problem = problem(e.getErrorCode());
        problem.setProperty("request_id", e.requestId());
        return problem;
    }

    @ExceptionHandler(BusinessException.class)
    ProblemDetail handleBusiness(BusinessException e) {
        return problem(e.getErrorCode());
    }

    @Override
    protected ResponseEntity<Object> handleExceptionInternal(Exception ex, Object body, HttpHeaders headers,
            HttpStatusCode statusCode, WebRequest request) {
        ResponseEntity<Object> response = super.handleExceptionInternal(ex, body, headers, statusCode, request);
        if (response != null && response.getBody() instanceof ProblemDetail problem
                && (problem.getProperties() == null || !problem.getProperties().containsKey("code"))) {
            HttpStatus status = HttpStatus.resolve(statusCode.value());
            problem.setProperty("code", status != null ? status.name() : String.valueOf(statusCode.value()));
        }
        return response;
    }

    /** 응답 헤더를 더 붙여야 하는 컨트롤러(쿠키 삭제 등)도 같은 본문을 쓴다. */
    public static ProblemDetail problem(ErrorCode errorCode) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(errorCode.status(), errorCode.message());
        problem.setProperty("code", errorCode.name());
        return problem;
    }
}
