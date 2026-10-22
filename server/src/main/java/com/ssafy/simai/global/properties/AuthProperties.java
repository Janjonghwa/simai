package com.ssafy.simai.global.properties;

import java.time.Duration;
import java.util.List;
import org.springframework.boot.context.properties.ConfigurationProperties;

@ConfigurationProperties("simai.auth")
public record AuthProperties(Jwt jwt, RefreshToken refreshToken, List<String> corsAllowedOrigins) {

    public AuthProperties {
        corsAllowedOrigins = corsAllowedOrigins == null ? List.of() : List.copyOf(corsAllowedOrigins);
    }

    /** secret 은 Base64 로 적은 HS256 키다. */
    public record Jwt(String secret, Duration accessTokenTtl) {
    }

    /** cookieSecure 는 쿠키 Secure 속성이다. https 가 아닌 로컬 시험에서만 끈다. */
    public record RefreshToken(Duration ttl, boolean cookieSecure) {
    }
}
