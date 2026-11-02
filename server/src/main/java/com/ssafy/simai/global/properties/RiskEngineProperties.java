package com.ssafy.simai.global.properties;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * EC2 옆 Python Risk Engine 컨테이너 호출 설정(S15P31A101-418, .agy/tbm-interface.md A절).
 *
 * @param baseUrl compose 내부망 HTTP 주소(기본 http://risk-engine:8080)
 * @param timeout 요청 하나당 읽기 타임아웃(기본 90s)
 */
@ConfigurationProperties("simai.risk-engine")
public record RiskEngineProperties(String baseUrl, Duration timeout) {

    public RiskEngineProperties {
        String stripped = baseUrl == null ? "" : baseUrl.strip().replaceAll("/+$", "");
        baseUrl = stripped.isEmpty() ? "http://risk-engine:8080" : stripped;
        timeout = timeout == null || timeout.isZero() || timeout.isNegative() ? Duration.ofSeconds(90) : timeout;
    }
}
