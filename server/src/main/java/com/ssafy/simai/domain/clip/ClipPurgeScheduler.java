package com.ssafy.simai.domain.clip;

import java.time.Clock;
import java.time.temporal.ChronoUnit;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 클립 삭제 작업을 10분마다 돌린다(명세 11.5). 보관 기간이 일 단위라 분 단위 지연은 상관없고, 주 1회 확인(18.4)은 점검 쿼리로
 * 삭제 예정일이 지난 클립 0건을 본다. 저장소 오류로 멈춘 주기는 다음 주기에 이어서 지운다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class ClipPurgeScheduler {

    private static final Logger log = LoggerFactory.getLogger(ClipPurgeScheduler.class);

    private final ClipPurgeService purge;
    private final Clock clock;

    ClipPurgeScheduler(ClipPurgeService purge, Clock clock) {
        this.purge = purge;
        this.clock = clock;
    }

    @Scheduled(initialDelayString = "${simai.clip.purge-initial-delay:PT1M}", fixedDelayString = "${simai.clip.purge-interval:PT10M}")
    void run() {
        try {
            purge.purge(clock.instant().truncatedTo(ChronoUnit.MILLIS));
        } catch (RuntimeException e) {
            // 저장소·DB 오류. 오류 문구에는 키가 실릴 수 있어 종류만 남긴다
            log.warn("클립 삭제 작업이 멈춤(다음 주기에 다시): {}", e.getClass().getSimpleName());
        }
    }
}
