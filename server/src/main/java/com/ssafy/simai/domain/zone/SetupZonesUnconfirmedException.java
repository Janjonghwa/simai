package com.ssafy.simai.domain.zone;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;

/**
 * 세팅 완료 때 세션 구역 목록에 확인 필요(needs_review) 항목이 남았다(409 SETUP_ZONES_UNCONFIRMED, S15P31A101-629).
 * 응답에 남은 개수(unconfirmed_count)를 싣는다(SetupController).
 */
public class SetupZonesUnconfirmedException extends BusinessException {

    private final int remaining;

    public SetupZonesUnconfirmedException(int remaining) {
        super(ErrorCode.Config.SETUP_ZONES_UNCONFIRMED);
        this.remaining = remaining;
    }

    public int remaining() {
        return remaining;
    }
}
