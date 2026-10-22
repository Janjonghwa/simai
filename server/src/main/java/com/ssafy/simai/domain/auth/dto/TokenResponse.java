package com.ssafy.simai.domain.auth.dto;

import com.ssafy.simai.domain.user.entity.AppUser;
import com.ssafy.simai.domain.user.entity.Role;

/**
 * 로그인·갱신 응답 본문. expiresIn 은 access 토큰 유효 시간(초)이다.
 * 갱신 토큰은 본문에 없고 HttpOnly 쿠키로만 간다. user.id 는 user_id 를 문자열로 준다(관제 앱 세션 형식).
 */
public record TokenResponse(String accessToken, String tokenType, long expiresIn, User user) {

    public record User(String id, Role role, String displayLabel) {

        public static User from(AppUser user) {
            return new User(String.valueOf(user.getId()), user.getRole(), user.getDisplayLabel());
        }
    }
}
