package com.ssafy.simai.domain.gpu;

/** GPU 세션 API 경로(S15P31A101-699). SecurityConfig 의 부팅 비밀값 체인과 컨트롤러가 같이 쓴다 */
public final class GpuPaths {

    /** POST: 등록(부팅 비밀값) */
    public static final String SESSIONS = "/api/gpu/sessions";
    /** POST: 진행·종료 보고(부팅 비밀값) */
    public static final String REPORTS = "/api/gpu/sessions/*/reports";
    /** GET: 상태 조회(관리자 JWT) */
    public static final String CURRENT = "/api/gpu/sessions/current";
    /** GET: 현장 게이트웨이용 가장 최근 세션(기기 토큰, LAPTOP, S15P31A101-701). /api/devices/** 라 기기 체인이 본다 */
    public static final String DEVICE_SESSION = "/api/devices/gpu-session";
    /** POST: 게이트웨이 자동 전환 상태(기기 토큰, LAPTOP, S15P31A101-701) */
    public static final String DEVICE_GATEWAY_STATE = "/api/devices/gpu-session/gateway-state";

    private GpuPaths() {
    }
}
