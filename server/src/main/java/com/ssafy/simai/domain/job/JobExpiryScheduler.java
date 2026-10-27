package com.ssafy.simai.domain.job;

import java.time.Clock;
import java.time.temporal.ChronoUnit;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 하트비트·제출 없이 만료된 대여를 1초마다 거둔다(명세 12.5 재할당). 대여 요청·하트비트·제출도 그때그때 만료를 보므로
 * 이 주기는 아무도 대여를 요청하지 않을 때 작업이 LEASED 로 남지 않게 하는 용도다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class JobExpiryScheduler {

    private final JobLeaseService leases;
    private final Clock clock;

    JobExpiryScheduler(JobLeaseService leases, Clock clock) {
        this.leases = leases;
        this.clock = clock;
    }

    @Scheduled(fixedDelay = 1000)
    void expire() {
        leases.expireDue(clock.instant().truncatedTo(ChronoUnit.MILLIS));
    }
}
