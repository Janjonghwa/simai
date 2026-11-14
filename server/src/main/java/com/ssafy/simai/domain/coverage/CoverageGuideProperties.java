package com.ssafy.simai.domain.coverage;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 세팅 1단계 카메라 각도 헬퍼 중계 설정(S15P31A101-447, 명세 4.3·4.5·12.2·12.6·13.4·18.1).
 *
 * <ul>
 *   <li>{@code staleAfter}: 최신 안내 수신 후 이 시간이 지나면 관리자 조회 응답을 {@code STALE} 로 표시한다(초기 10 s).</li>
 *   <li>{@code demandTtl}: 마지막 관리자 조회 시각으로부터 이 시간 안에는 기기 POST 응답의 {@code active_demand} 를
 *       {@code true} 로 돌려줘 게이트웨이 헬퍼(-446)가 계산을 유지하게 한다(초기 15 s).</li>
 *   <li>{@code viewCheckTtl}: 재사용 시점 확인(S15P31A101-712) 결과를 재사용 판정 근거로 쓰는 시간(초기 60 s). 카메라 위치는 초 단위로
 *       바뀌지 않고, 노트북 CPU 로 도는 헬퍼 한 바퀴(16~24 s)를 두 번 넘게 기다릴 수 있어야 한다.</li>
 * </ul>
 */
@ConfigurationProperties(prefix = "simai.coverage-guide")
public record CoverageGuideProperties(
        Duration staleAfter,
        Duration demandTtl,
        Duration viewCheckTtl) {

    public CoverageGuideProperties {
        staleAfter = staleAfter == null || staleAfter.isNegative() || staleAfter.isZero()
                ? Duration.ofSeconds(10) : staleAfter;
        demandTtl = demandTtl == null || demandTtl.isNegative() || demandTtl.isZero()
                ? Duration.ofSeconds(15) : demandTtl;
        viewCheckTtl = viewCheckTtl == null || viewCheckTtl.isNegative() || viewCheckTtl.isZero()
                ? Duration.ofSeconds(60) : viewCheckTtl;
    }
}
