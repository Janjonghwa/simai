package com.ssafy.simai.domain.watch;

/** 워치의 I4 연결이 새로 열렸다(옛 연결을 대체한 경우 포함, S15P31A101-510). 지금 배정 상태를 한 번 보내는 신호다 */
record WatchConnected(String deviceId) {
}
