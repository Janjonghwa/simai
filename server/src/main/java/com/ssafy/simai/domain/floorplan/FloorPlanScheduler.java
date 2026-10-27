package com.ssafy.simai.domain.floorplan;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Component;

/**
 * 보정이 CALIBRATED 가 되거나 버전이 바뀌면 BEV 평면도 작업을 넣는다(명세 12.5, S15P31A101-240). 보정 수신 코드에 끼우지 않고 주기적으로
 * 대조한다: 보정 알림·관리자 업로드·프레임 등록 어느 쪽이 먼저 와도 한 곳에서 같은 규칙으로 판단한다. 평면도는 BACKGROUND 라 몇 초 늦어도 된다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class FloorPlanScheduler {

    private static final Logger log = LoggerFactory.getLogger(FloorPlanScheduler.class);

    private final FloorPlanService floorPlans;

    FloorPlanScheduler(FloorPlanService floorPlans) {
        this.floorPlans = floorPlans;
    }

    @Scheduled(fixedDelayString = "${simai.floor-plan.reconcile-interval:10s}",
            initialDelayString = "${simai.floor-plan.reconcile-interval:10s}")
    void reconcile() {
        try {
            floorPlans.reconcile();
        } catch (RuntimeException e) {
            // DB 오류·현장 설정(SITE_NOT_SINGLE) 등. 다음 주기에 다시 본다(주기마다 스택을 쌓지 않게 한 줄만)
            log.warn("BEV 평면도 대조를 하지 못했다: {} {}", e.getClass().getSimpleName(), e.getMessage());
        }
    }
}
