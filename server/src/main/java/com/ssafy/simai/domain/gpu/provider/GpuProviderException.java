package com.ssafy.simai.domain.gpu.provider;

/**
 * 공급자 호출 실패(S15P31A101-702). code 는 기록·화면용 짧은 이름이고 문구에는 비밀 값·응답 본문을 싣지 않는다.
 * <ul>
 * <li>NOT_CONFIGURED: API 키가 없다</li>
 * <li>OUT_OF_STOCK: 공급자가 재고 없음으로 거절했다</li>
 * <li>REJECTED: 공급자가 요청을 거절했다(4xx)</li>
 * <li>UNAVAILABLE: 연결 실패·시간 초과·5xx</li>
 * <li>BAD_RESPONSE: 응답 형식이 다르다</li>
 * </ul>
 */
public class GpuProviderException extends Exception {

    private final String code;

    public GpuProviderException(String code, String message) {
        super(message);
        this.code = code;
    }

    public GpuProviderException(String code, String message, Throwable cause) {
        super(message, cause);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
