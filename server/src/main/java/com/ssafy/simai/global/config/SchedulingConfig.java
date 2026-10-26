package com.ssafy.simai.global.config;

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
 */
@Configuration
@EnableScheduling
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SchedulingConfig {

    @Bean
    ThreadPoolTaskScheduler taskScheduler() {
        ThreadPoolTaskScheduler scheduler = new ThreadPoolTaskScheduler();
        scheduler.setThreadNamePrefix("scheduling-");
        return scheduler;
    }
}
