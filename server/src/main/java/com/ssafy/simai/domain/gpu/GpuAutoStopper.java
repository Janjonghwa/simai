package com.ssafy.simai.domain.gpu;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * GPU 자동 정지 주기 작업(S15P31A101-702, simai.gpu.sweep-interval). 규칙은 {@link GpuPowerService#autoStop()}.
 * 한 번 실패해도(공급자 끊김 등) 다음 주기에 다시 본다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class GpuAutoStopper {

    private static final Logger log = LoggerFactory.getLogger(GpuAutoStopper.class);

    private final GpuPowerService power;

    GpuAutoStopper(GpuPowerService power) {
        this.power = power;
    }

    @Scheduled(initialDelayString = "${simai.gpu.sweep-interval:60s}", fixedDelayString = "${simai.gpu.sweep-interval:60s}")
    void sweep() {
        try {
            power.autoStop();
        } catch (RuntimeException e) {
            log.warn("GPU 자동 정지 점검 실패: {}", e.getClass().getSimpleName());
        }
    }
}
