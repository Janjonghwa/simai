package com.ssafy.simai.domain.auth.service;

import com.ssafy.simai.domain.auth.repository.RefreshTokenRepository;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;
import org.springframework.transaction.annotation.Transactional;

/**
 * 만료된 갱신 토큰 행을 지운다. 갱신마다 새 행이 생기고(회전) 지우는 곳이 없어 계속 쌓였다.
 * 만료된 토큰은 어떤 경우에도 받지 않으므로 지워도 동작이 같다. 감사·문의 대응에 쓸 여유를 두고 만료 뒤 7일 지난 것만 지운다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class RefreshTokenCleaner {

    private static final Logger log = LoggerFactory.getLogger(RefreshTokenCleaner.class);
    static final Duration KEEP_AFTER_EXPIRY = Duration.ofDays(7);

    private final RefreshTokenRepository tokens;
    private final Clock clock;

    RefreshTokenCleaner(RefreshTokenRepository tokens, Clock clock) {
        this.tokens = tokens;
        this.clock = clock;
    }

    @Scheduled(initialDelayString = "PT5M", fixedDelayString = "PT6H")
    @Transactional
    public int purgeExpired() {
        Instant cutoff = clock.instant().minus(KEEP_AFTER_EXPIRY);
        int purged = tokens.purgeExpiredBefore(cutoff);
        if (purged > 0) {
            log.info("만료된 갱신 토큰 {} 행을 지웠다(만료 시각 < {})", purged, cutoff);
        }
        return purged;
    }
}
