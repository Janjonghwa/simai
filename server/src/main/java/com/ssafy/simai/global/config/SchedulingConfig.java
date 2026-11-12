package com.ssafy.simai.global.config;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;
import org.springframework.scheduling.concurrent.ThreadPoolTaskScheduler;

/**
 * 주기 작업(journal_ack 몰아 보내기 등). 관리 명령 모드에서는 돌지 않는다.
 * STOMP 브로커(StompConfig)가 TaskScheduler 빈(messageBrokerTaskScheduler)을 만들면 Spring Boot 기본 스케줄러가 빠지고
 * 주기 작업이 하트비트 스케줄러를 같이 쓴다. I1 송신이 막혀 주기 작업이 늦어져도 관제 앱 하트비트가 밀리지 않게 따로 둔다.
 * 이름이 taskScheduler 라 @Scheduled 가 이 빈을 쓴다.
 * 1초 주기 작업(journal_ack, 보류 이벤트 만료, 작업 대여 만료, Scene 참조)과 10초 주기 I1 송신 작업이 함께 쓰므로,
 * 송신 하나가 막혀(전송 한도 5초) 다른 주기 작업이 밀리지 않게 스레드를 여럿 둔다(같은 작업끼리는 fixedDelay 라 겹치지 않는다).
 */
@Configuration
@EnableScheduling
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SchedulingConfig {

    private static final Logger log = LoggerFactory.getLogger(SchedulingConfig.class);
    /** 1초 주기 작업 4개가 송신 지연 하나에 묶이지 않을 만큼 */
    static final int POOL_SIZE = 4;

    @Bean
    ThreadPoolTaskScheduler taskScheduler() {
        ThreadPoolTaskScheduler scheduler = new ThreadPoolTaskScheduler();
        scheduler.setThreadNamePrefix("scheduling-");
        scheduler.setPoolSize(POOL_SIZE);
        // 한 주기의 예외가 로그 없이 사라지지 않게 남긴다(다음 주기는 그대로 돈다)
        scheduler.setErrorHandler(e -> log.error("주기 작업 실패", e));
        return scheduler;
    }
}
