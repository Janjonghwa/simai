package com.ssafy.simai.domain.cctv;

/**
 * CCTV 원격 시청 경로(S15P31A101-400, 설계 docs/design/cctv-remote-view.md). Nginx location /ws 가 두 WebSocket 을 그대로 서버로 넘긴다.
 */
public final class CctvPaths {

    /** 게이트웨이 영상 중계 → EC2 수신 WebSocket(기기 토큰, SecurityConfig deviceFilterChain). */
    public static final String SOURCE_WS = "/ws/devices/cctv";

    /** EC2 → 관리자 브라우저 시청 WebSocket(일회용 입장권 ?ticket=, 업그레이드는 사용자 체인에서 permitAll). */
    public static final String VIEW_WS = "/ws/cctv/view";

    /** 관리자 JWT 로 시청 입장권을 받는다. */
    public static final String TICKETS_API = "/api/cctv/remote/tickets";

    /** 관리자 화면의 원격 CCTV 월 송출·차단 상태. */
    public static final String STATUS_API = "/api/cctv/remote/status";

    private CctvPaths() {
    }
}
