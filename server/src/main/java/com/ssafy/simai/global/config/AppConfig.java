package com.ssafy.simai.global.config;

import com.ssafy.simai.domain.cctv.CctvRemoteProperties;
import com.ssafy.simai.domain.coverage.CoverageGuideProperties;
import com.ssafy.simai.domain.watch.WatchProperties;
import com.ssafy.simai.global.properties.AuthProperties;
import com.ssafy.simai.global.properties.ConfigBundleProperties;
import com.ssafy.simai.global.properties.RiskEngineProperties;
import com.ssafy.simai.global.properties.StorageProperties;
import com.ssafy.simai.global.properties.Tier0Properties;
import java.time.Clock;
import org.springframework.boot.context.properties.EnableConfigurationProperties;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.security.crypto.factory.PasswordEncoderFactories;
import org.springframework.security.crypto.password.PasswordEncoder;

/** 웹 서버와 관리 명령(global/command)이 함께 쓰는 빈. */
@Configuration
@EnableConfigurationProperties({AuthProperties.class, Tier0Properties.class, ConfigBundleProperties.class,
        StorageProperties.class, CctvRemoteProperties.class, CoverageGuideProperties.class,
        RiskEngineProperties.class, WatchProperties.class})
public class AppConfig {

    @Bean
    PasswordEncoder passwordEncoder() {
        // {bcrypt} 접두어를 붙여 저장한다. 나중에 알고리즘을 바꿔도 기존 해시를 읽는다
        return PasswordEncoderFactories.createDelegatingPasswordEncoder();
    }

    @Bean
    Clock clock() {
        return Clock.systemUTC();
    }
}
