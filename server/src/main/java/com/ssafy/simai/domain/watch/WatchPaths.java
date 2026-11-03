package com.ssafy.simai.domain.watch;

/**
 * 워치 I4 경로(S15P31A101-336). 워치 앱 watch/.../config/WatchConfig.kt 의 WATCH_WS_PATH 와 같아야 한다.
 * Nginx location /ws 가 그대로 서버로 넘긴다(읽기 시한 1시간, 하트비트 20초가 그보다 자주 오간다).
 */
public final class WatchPaths {

    /** 워치 ↔ EC2 WebSocket(기기 토큰, SecurityConfig deviceFilterChain, kind WATCH 만) */
    public static final String WS = "/ws/watch";

    private WatchPaths() {
    }
}
