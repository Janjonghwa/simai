package com.ssafy.simai.global.exception;

import org.springframework.http.ProblemDetail;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

/** 오류 본문은 ProblemDetail 이다. 관제 앱 http.ts 가 detail 을 읽고, code 로 오류 종류를 가른다. */
@RestControllerAdvice
public class GlobalExceptionHandler {

    @ExceptionHandler(BusinessException.class)
    ProblemDetail handleBusiness(BusinessException e) {
        return problem(e.getErrorCode());
    }

    /** 응답 헤더를 더 붙여야 하는 컨트롤러(쿠키 삭제 등)도 같은 본문을 쓴다. */
    public static ProblemDetail problem(ErrorCode errorCode) {
        ProblemDetail problem = ProblemDetail.forStatusAndDetail(errorCode.status(), errorCode.message());
        problem.setProperty("code", errorCode.name());
        return problem;
    }
}
