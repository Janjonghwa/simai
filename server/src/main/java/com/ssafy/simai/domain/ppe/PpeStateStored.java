package com.ssafy.simai.domain.ppe;

/**
 * ppe_state 를 저장해 사람별 현재 상태(ppe_current_state)를 하나 이상 바꿨다(S15P31A101-531). 커밋 뒤 /topic/ppe 로 그 Tier 0 의
 * 스냅샷(ppe_view)을 보낸다.
 *
 * @param sourceId 현재 상태를 바꾼 Tier 0 연결(I1 봉투 source_id)
 */
public record PpeStateStored(String sourceId) {
}
