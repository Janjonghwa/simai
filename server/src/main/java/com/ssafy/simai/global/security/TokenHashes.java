package com.ssafy.simai.global.security;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.HexFormat;

/**
 * 비밀 토큰은 원문 대신 SHA-256 만 저장·비교한다(갱신 토큰, Tier 0 토큰, 기기·워커 토큰 S15P31A101-222).
 * 토큰이 무작위 256비트라 소금 없이 SHA-256 으로 충분하다(사전 공격 대상이 아니다).
 */
public final class TokenHashes {

    private static final int TOKEN_BYTES = 32;
    private static final SecureRandom RANDOM = new SecureRandom();

    private TokenHashes() {
    }

    /** 새 비밀 토큰. 무작위 256비트를 Base64url(채움 없음)로 */
    public static String newToken() {
        byte[] bytes = new byte[TOKEN_BYTES];
        RANDOM.nextBytes(bytes);
        return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
    }

    /** DB 에 두는 형태(SHA-256 16진수 64자) */
    public static String sha256Hex(String token) {
        return HexFormat.of().formatHex(sha256(token));
    }

    /** UTF-8 바이트의 SHA-256. 비교는 MessageDigest.isEqual 로 고정 시간에 한다. */
    public static byte[] sha256(String token) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(token.getBytes(StandardCharsets.UTF_8));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }
}
