package com.ssafy.simai.domain.auth.controller;

import com.ssafy.simai.global.properties.AuthProperties;
import java.time.Duration;
import org.springframework.http.ResponseCookie;
import org.springframework.stereotype.Component;

/**
 * 갱신 토큰 쿠키. 프론트 JS 가 읽지 못하게 HttpOnly 로 두고(XSS 로 훔칠 수 없다),
 * 갱신·로그아웃 요청에만 실리게 Path 를 /api/auth 로 좁힌다.
 */
@Component
class RefreshTokenCookie {

    static final String NAME = "simai_refresh";
    private static final String PATH = "/api/auth";

    private final boolean secure;

    RefreshTokenCookie(AuthProperties properties) {
        this.secure = properties.refreshToken().cookieSecure();
    }

    String issue(String value, Duration ttl) {
        return base(value).maxAge(ttl).build().toString();
    }

    /** 브라우저에서 지운다(로그아웃). */
    String clear() {
        return base("").maxAge(Duration.ZERO).build().toString();
    }

    private ResponseCookie.ResponseCookieBuilder base(String value) {
        return ResponseCookie.from(NAME, value)
                .httpOnly(true)
                .secure(secure)
                .sameSite("Strict")
                .path(PATH);
    }
}
