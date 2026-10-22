package com.ssafy.simai.global.security;

import java.nio.charset.StandardCharsets;

/** 비밀번호 길이 규칙. 계정 생성(UserService)과 로그인(AuthService)이 같이 쓴다. */
public final class PasswordPolicy {

    public static final int MIN_LENGTH = 8;
    // BCrypt 는 72바이트까지만 쓰고, 넘으면 Spring 이 IllegalArgumentException 을 던진다
    public static final int MAX_BYTES = 72;

    private PasswordPolicy() {
    }

    public static boolean tooShort(String password) {
        return password == null || password.length() < MIN_LENGTH;
    }

    public static boolean tooLong(String password) {
        return password != null && password.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES;
    }
}
