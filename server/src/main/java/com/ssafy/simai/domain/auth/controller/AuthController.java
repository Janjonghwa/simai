package com.ssafy.simai.domain.auth.controller;

import com.ssafy.simai.domain.auth.dto.LoginRequest;
import com.ssafy.simai.domain.auth.dto.TokenResponse;
import com.ssafy.simai.domain.auth.service.AuthService;
import com.ssafy.simai.domain.auth.service.AuthTokens;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.exception.GlobalExceptionHandler;
import jakarta.validation.Valid;
import org.springframework.http.HttpHeaders;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.CookieValue;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/**
 * 명세 12.6 /api/auth(공개). 갱신·로그아웃은 명세 14 반장 PWA 긴 갱신 토큰.
 * 갱신 토큰은 본문에 싣지 않고 HttpOnly 쿠키(RefreshTokenCookie)로만 주고받는다.
 */
@RestController
@RequestMapping("/api/auth")
public class AuthController {

    private final AuthService authService;
    private final RefreshTokenCookie cookie;

    AuthController(AuthService authService, RefreshTokenCookie cookie) {
        this.authService = authService;
        this.cookie = cookie;
    }

    @PostMapping("/login")
    ResponseEntity<TokenResponse> login(@Valid @RequestBody LoginRequest request) {
        return withCookie(authService.login(request));
    }

    @PostMapping("/refresh")
    ResponseEntity<?> refresh(@CookieValue(name = RefreshTokenCookie.NAME, required = false) String refreshToken) {
        try {
            if (refreshToken == null || refreshToken.isBlank()) {
                throw new BusinessException(ErrorCode.Auth.INVALID_REFRESH_TOKEN);
            }
            return withCookie(authService.refresh(refreshToken));
        } catch (BusinessException e) {
            // 쓸 수 없는 토큰이면 쿠키도 지운다(죽은 쿠키가 14일 동안 남아 계속 오지 않게).
            // REFRESH_TOKEN_ROTATED 는 지우지 않는다. 먼저 끝난 요청이 넣은 새 쿠키를 덮어쓰게 된다
            if (e.getErrorCode() != ErrorCode.Auth.INVALID_REFRESH_TOKEN) {
                throw e;
            }
            return ResponseEntity.status(e.getErrorCode().status())
                    .header(HttpHeaders.SET_COOKIE, cookie.clear())
                    .body(GlobalExceptionHandler.problem(e.getErrorCode()));
        }
    }

    @PostMapping("/logout")
    ResponseEntity<Void> logout(@CookieValue(name = RefreshTokenCookie.NAME, required = false) String refreshToken) {
        if (refreshToken != null && !refreshToken.isBlank()) {
            authService.logout(refreshToken);
        }
        return ResponseEntity.noContent().header(HttpHeaders.SET_COOKIE, cookie.clear()).build();
    }

    private ResponseEntity<TokenResponse> withCookie(AuthTokens tokens) {
        return ResponseEntity.ok()
                .header(HttpHeaders.SET_COOKIE, cookie.issue(tokens.refreshToken(), tokens.refreshTokenTtl()))
                .body(tokens.body());
    }
}
