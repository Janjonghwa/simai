package com.ssafy.simai.domain.cctv;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * CCTV 원격 시청 설정(simai.cctv-remote, S15P31A101-400). 비우면 초기값(설계 docs/design/cctv-remote-view.md).
 *
 * @param maxViewers 동시 시청 상한(초기 5). 넘으면 viewer_limit 후 닫는다
 * @param monthlyQuotaBytes 월(UTC 달력 월) 원격 송출 상한 바이트(초기 80 GB = 80×10^9). 이 값 이상이면 monthly_quota 로 모두 닫고 수요를 끈다
 * @param demandLinger 마지막 시청자가 나간 뒤 수요를 끄기까지(초기 15 s, 새로고침 깜빡임 방지)
 * @param ticketTtl 시청 입장권 수명(초기 60 s, 한 번만)
 * @param pingInterval ping 간격(초기 15 s, 수신·시청 모두)
 * @param pongTimeout pong 없이 이만큼 지나면 끊는다(초기 30 s)
 * @param usageFlushInterval 사용 기록(추가만)을 DB 에 남기는 간격(초기 1분)
 * @param viewerQueueFrames 시청자 하나의 대기 프레임 상한(초기 50, 로컬 미리보기와 같은 기준)
 * @param viewerQueueBytes 시청자 하나의 대기 바이트 상한(초기 16 MiB, 로컬 미리보기와 같은 기준)
 * @param sourceMaxMessageBytes 게이트웨이가 올리는 이진 메시지 하나의 상한(초기 4 MiB, 2304x1296 키프레임 여러 장)
 */
@ConfigurationProperties("simai.cctv-remote")
public record CctvRemoteProperties(Integer maxViewers, Long monthlyQuotaBytes, Duration demandLinger, Duration ticketTtl,
        Duration pingInterval, Duration pongTimeout, Duration usageFlushInterval, Integer viewerQueueFrames,
        Long viewerQueueBytes, Integer sourceMaxMessageBytes) {

    public CctvRemoteProperties {
        maxViewers = positive(maxViewers, 5);
        monthlyQuotaBytes = monthlyQuotaBytes == null || monthlyQuotaBytes <= 0 ? 80_000_000_000L : monthlyQuotaBytes;
        demandLinger = positive(demandLinger, Duration.ofSeconds(15));
        ticketTtl = positive(ticketTtl, Duration.ofSeconds(60));
        pingInterval = positive(pingInterval, Duration.ofSeconds(15));
        pongTimeout = positive(pongTimeout, Duration.ofSeconds(30));
        usageFlushInterval = positive(usageFlushInterval, Duration.ofMinutes(1));
        viewerQueueFrames = positive(viewerQueueFrames, 50);
        viewerQueueBytes = viewerQueueBytes == null || viewerQueueBytes <= 0 ? 16L * 1024 * 1024 : viewerQueueBytes;
        sourceMaxMessageBytes = positive(sourceMaxMessageBytes, 4 * 1024 * 1024);
    }

    /** 설정 없이 초기값만(시험·관리 명령). */
    public static CctvRemoteProperties defaults() {
        return new CctvRemoteProperties(null, null, null, null, null, null, null, null, null, null);
    }

    private static Integer positive(Integer value, int fallback) {
        return value == null || value <= 0 ? fallback : value;
    }

    private static Duration positive(Duration value, Duration fallback) {
        return value == null || value.isZero() || value.isNegative() ? fallback : value;
    }
}
