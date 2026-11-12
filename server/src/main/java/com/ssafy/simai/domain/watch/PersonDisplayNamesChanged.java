package com.ssafy.simai.domain.watch;

/**
 * 사람 표시 이름이나 워치 기기 표시 이름, 미확인 N 배정·병합 승계가 바뀌었음을 알리는 신호(S15P31A101-658).
 * HealthState 가 받아 viewer_health(/topic/health)를 다음 주기를 기다리지 않고 바로 내보내게 한다.
 */
public record PersonDisplayNamesChanged() {
}
