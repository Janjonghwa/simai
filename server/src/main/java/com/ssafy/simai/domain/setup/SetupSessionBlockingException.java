package com.ssafy.simai.domain.setup;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.UUID;

/**
 * 같은 현장의 다른 세팅 세션 때문에 거절한다(409 SETUP_ALREADY_IN_PROGRESS, S15P31A101-641). 작업 시작은 그 작업의 세팅 완료만 본다(-642).
 * 응답에 막고 있는 세션과 그 세션의 작업을 실어, 그 세션이 다른 날짜 작업 소속이어도 화면이 찾아가 취소할 수 있게 한다.
 */
public class SetupSessionBlockingException extends BusinessException {

    private final UUID setupSessionId;
    private final long workId;

    public SetupSessionBlockingException(ErrorCode errorCode, UUID setupSessionId, long workId) {
        super(errorCode);
        this.setupSessionId = setupSessionId;
        this.workId = workId;
    }

    public UUID setupSessionId() {
        return setupSessionId;
    }

    public long workId() {
        return workId;
    }
}
