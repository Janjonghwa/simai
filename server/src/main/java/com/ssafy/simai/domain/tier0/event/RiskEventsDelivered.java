package com.ssafy.simai.domain.tier0.event;

import java.util.List;
import java.util.UUID;

/**
 * 화면에 알릴 이벤트(명세 3.2 재동기화 규칙). 동기화 뒤 실시간 전이(REALTIME)와 동기화를 끝낼 때 아직 활성이고 근거가 있는 이벤트(DELAYED_ACTIVE)다.
 * AUDIT_ONLY 는 기록만 하고 알리지 않는다. 트랜잭션이 끝난 뒤 그때의 현재 상태를 /topic/events 로 보낸다.
 */
public record RiskEventsDelivered(List<UUID> eventIds) {

    public RiskEventsDelivered {
        eventIds = List.copyOf(eventIds);
    }
}
