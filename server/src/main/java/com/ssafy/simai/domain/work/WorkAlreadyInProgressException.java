package com.ssafy.simai.domain.work;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;

public class WorkAlreadyInProgressException extends BusinessException {
    private final long workId;

    public WorkAlreadyInProgressException(long workId) {
        super(ErrorCode.Work.WORK_ALREADY_IN_PROGRESS);
        this.workId = workId;
    }

    public long workId() {
        return workId;
    }
}
