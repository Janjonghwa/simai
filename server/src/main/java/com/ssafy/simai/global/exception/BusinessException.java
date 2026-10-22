package com.ssafy.simai.global.exception;

import lombok.Getter;

/** 서비스가 ErrorCode 로 실패를 알린다. 응답 변환은 GlobalExceptionHandler 가 한다. */
@Getter
public class BusinessException extends RuntimeException {

    private final ErrorCode errorCode;

    public BusinessException(ErrorCode errorCode) {
        super(errorCode.name());
        this.errorCode = errorCode;
    }
}
