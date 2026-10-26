package com.ssafy.simai.global.config;

import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.context.annotation.Configuration;
import org.springframework.scheduling.annotation.EnableScheduling;

/** 주기 작업(journal_ack 몰아 보내기 등). 관리 명령 모드에서는 돌지 않는다. */
@Configuration
@EnableScheduling
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SchedulingConfig {
}
