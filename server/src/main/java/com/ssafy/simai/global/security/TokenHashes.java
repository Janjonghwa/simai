package com.ssafy.simai.global.security;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;

/**
 * 비밀 토큰은 원문 대신 SHA-256 만 저장·비교한다(갱신 토큰, Tier 0 토큰, 이후 기기·워커 토큰 S15P31A101-222).
 * 토큰이 무작위 256비트라 소금 없이 SHA-256 으로 충분하다(사전 공격 대상이 아니다).
 */
public final class TokenHashes {

    private TokenHashes() {
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
