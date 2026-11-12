package com.ssafy.simai.domain.setup;

import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * GPU 워커가 가져가지 않은 세팅 작업을 시간 초과로 닫는다(S15P31A101-641). 대기 작업에는 마감이 없어(마감은 vlm_rationale 만)
 * GPU 인스턴스가 꺼져 있으면 세션이 보정·생성 0% 에서 끝없이 기다리고, 같은 현장의 새 세팅과 작업 시작을 모두 막았다.
 * 기준 시간은 GPU 인스턴스를 켜고 워커가 붙는 데 걸리는 시간보다 넉넉히 둔다(기본 10분).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupQueueTimeout {

    private static final Logger log = LoggerFactory.getLogger(SetupQueueTimeout.class);

    private final SetupService setups;
    private final Clock clock;
    private final Duration timeout;

    SetupQueueTimeout(SetupService setups, Clock clock,
            @Value("${simai.setup.queue-timeout:PT10M}") Duration timeout) {
        this.setups = setups;
        this.clock = clock;
        this.timeout = timeout;
    }

    @Scheduled(initialDelayString = "${simai.setup.queue-check-interval:PT30S}",
            fixedDelayString = "${simai.setup.queue-check-interval:PT30S}")
    public int closeUnclaimed() {
        Instant cutoff = clock.instant().minus(timeout);
        int closed = 0;
        for (UUID sessionId : setups.unclaimedBefore(cutoff)) {
            try {
                if (setups.failUnclaimed(sessionId, cutoff)) {
                    closed++;
                }
            } catch (RuntimeException e) {
                // 한 세션이 실패해도 나머지는 닫는다. 다음 주기에 다시 본다
                log.warn("대여되지 않은 세팅 작업을 닫지 못했다: session={}", sessionId, e);
            }
        }
        return closed;
    }
}
