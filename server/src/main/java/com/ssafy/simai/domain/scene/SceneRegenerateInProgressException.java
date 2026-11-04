package com.ssafy.simai.domain.scene;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;

public class SceneRegenerateInProgressException extends BusinessException {

    private final long requestId;

    public SceneRegenerateInProgressException(long requestId) {
        super(ErrorCode.Scene.REGENERATE_IN_PROGRESS);
        this.requestId = requestId;
    }

    public long requestId() {
        return requestId;
    }
}
