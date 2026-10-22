package com.ssafy.simai.domain.auth.service;

import com.ssafy.simai.domain.auth.entity.RefreshToken;
import com.ssafy.simai.domain.auth.repository.RefreshTokenRepository;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import com.ssafy.simai.global.properties.AuthProperties;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.Base64;
import java.util.HexFormat;
import java.util.UUID;
import org.springframework.stereotype.Service;
import org.springframework.transaction.annotation.Transactional;

/**
 * 갱신 토큰 발급·교체·폐기. 토큰은 JWT 가 아닌 무작위 값(256비트)이고 DB 에는 SHA-256 해시만 남긴다.
 * 갱신할 때마다 쓴 토큰을 폐기하고 새 토큰을 준다. 폐기된 토큰이 다시 오면 그 계정 토큰을 모두 폐기한다.
 */
@Service
public class RefreshTokenService {

    private static final int TOKEN_BYTES = 32;
    // 교체 직후 이 시간 안에 같은 토큰이 다시 오면 동시 갱신(탭 두 개·재시도)으로 보고 전체 폐기하지 않는다
    static final Duration ROTATION_GRACE = Duration.ofSeconds(30);

    private final RefreshTokenRepository tokens;
    private final Duration ttl;
    private final Clock clock;
    private final SecureRandom random = new SecureRandom();

    RefreshTokenService(RefreshTokenRepository tokens, AuthProperties properties, Clock clock) {
        this.tokens = tokens;
        this.ttl = properties.refreshToken().ttl();
        this.clock = clock;
    }

    @Transactional
    public IssuedRefreshToken issue(Long userId) {
        byte[] bytes = new byte[TOKEN_BYTES];
        random.nextBytes(bytes);
        String value = Base64.getUrlEncoder().withoutPadding().encodeToString(bytes);
        Instant now = clock.instant();
        tokens.save(new RefreshToken(UUID.randomUUID(), userId, hash(value), now, now.plus(ttl)));
        return new IssuedRefreshToken(value, ttl);
    }

    /**
     * 쓴 토큰을 폐기하고 그 계정의 user_id 를 돌려준다. 새 토큰 발급은 부르는 쪽이 한다.
     * 실패해도 재사용 감지로 한 폐기는 남겨야 하므로 롤백하지 않는다.
     */
    @Transactional(noRollbackFor = BusinessException.class)
    public Long consume(String value) {
        RefreshToken token = tokens.findByTokenHash(hash(value)).orElseThrow(RefreshTokenService::invalid);
        Instant now = clock.instant();
        if (token.isRevoked()) {
            if (rotatedJustNow(token, now)) {
                throw rotated();
            }
            // 오래전에 폐기된 토큰이 다시 왔다. 유출로 보고 그 계정의 살아 있는 토큰을 모두 폐기한다
            tokens.revokeAllActive(token.getUserId(), now);
            throw invalid();
        }
        if (token.isExpired(now)) {
            throw invalid();
        }
        if (tokens.revokeIfActive(token.getId(), now) == 0) {
            // 조회와 폐기 사이에 같은 쿠키를 든 다른 요청이 먼저 교체했다
            throw rotated();
        }
        return token.getUserId();
    }

    /** 로그아웃. 없거나 이미 폐기된 토큰이어도 조용히 끝낸다(토큰이 있는지 알리지 않는다). */
    @Transactional
    public void revoke(String value) {
        tokens.findByTokenHash(hash(value)).ifPresent(token -> tokens.revokeIfActive(token.getId(), clock.instant()));
    }

    // 방금(유예 시간 안) 갱신으로 교체되어 뒤이은 새 토큰이 있다. 로그아웃으로 폐기된 토큰은 새 토큰이 없어 여기 들지 않는다
    private boolean rotatedJustNow(RefreshToken token, Instant now) {
        Instant revokedAt = token.getRevokedAt();
        return now.isBefore(revokedAt.plus(ROTATION_GRACE))
                && tokens.existsByUserIdAndIssuedAtGreaterThanEqual(token.getUserId(), revokedAt);
    }

    private static BusinessException invalid() {
        return new BusinessException(ErrorCode.Auth.INVALID_REFRESH_TOKEN);
    }

    private static BusinessException rotated() {
        return new BusinessException(ErrorCode.Auth.REFRESH_TOKEN_ROTATED);
    }

    // 무작위 256비트라 소금 없이 SHA-256 으로 충분하다(사전 공격 대상이 아니다)
    private static String hash(String value) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(digest);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    public record IssuedRefreshToken(String value, Duration ttl) {
    }
}
