package com.ssafy.simai.domain.watch;

/**
 * 워치 하나의 배정이 생기거나 풀렸다(S15P31A101-510). WatchBindings 가 배정이 바뀌는 지점에서 내고 WatchBindingNotifier 가 받아
 * 연결된 그 워치에 watch_binding_notice 를 보낸다. 둘이 서로를 부르지 않게 이벤트로 넘긴다.
 * 지금 상태는 받는 쪽이 DB 에서 다시 읽으므로 이 이벤트는 "이 워치를 다시 보라"는 신호다.
 *
 * @param deviceId 배정이 바뀐 워치
 * @param unbindReason 풀렸다면 사유(RELEASED·REPLACED·MATCH_LOST·IDENTITY_CHANGED 등). 배정이 생긴 쪽이면 null
 */
record WatchBindingChanged(String deviceId, String unbindReason) {
}
