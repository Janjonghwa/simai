package com.ssafy.simai.domain.ppe;

import java.time.Instant;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import org.springframework.stereotype.Component;

/**
 * Tier 0 별로 EC2 가 I1 ppe_state 를 마지막으로 받은 시각(S15P31A101-634). 사람이 0명인 메시지도 센다(그런 메시지는 DB 에 행을 남기지
 * 않는다). 관제 화면이 'PPE 상태 수신 없음'(PPE 프로세스가 안 떴거나 멈춤)을 안내하는 근거이고, 경보·판정에는 쓰지 않는다.
 * 메모리에만 둔다: EC2 가 다시 뜨면 비고, PPE 는 0.5초마다 보내므로 곧 다시 찬다.
 */
@Component
class PpeReceipts {

    private final Map<String, Instant> lastReceived = new ConcurrentHashMap<>();

    /** 계약 검사를 지난 ppe_state 를 받았다. 늦게 처리된 앞 메시지가 더 새 시각을 덮지 않게 큰 값만 남긴다 */
    void received(String sourceId, Instant at) {
        lastReceived.merge(sourceId, at, (old, now) -> now.isAfter(old) ? now : old);
    }

    Optional<Instant> lastReceived(String sourceId) {
        return Optional.ofNullable(lastReceived.get(sourceId));
    }

    /** 이 EC2 가 시작된 뒤 ppe_state 를 한 번이라도 보낸 Tier 0 */
    Set<String> sources() {
        return Set.copyOf(lastReceived.keySet());
    }
}
