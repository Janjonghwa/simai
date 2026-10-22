package com.ssafy.simai.domain.auth.service;

import com.ssafy.simai.domain.auth.dto.TokenResponse;
import java.time.Duration;

/** 로그인·갱신 결과. body 는 응답 본문, 갱신 토큰은 컨트롤러가 HttpOnly 쿠키로만 내보낸다. */
public record AuthTokens(TokenResponse body, String refreshToken, Duration refreshTokenTtl) {
}
