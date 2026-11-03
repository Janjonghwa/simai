package com.ssafy.simai.domain.watch;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 워치 I4 연결 설정(simai.watch, S15P31A101-336, 설계 docs/design/server-watch-link.md). 비우면 초기값.
 *
 * @param heartbeatInterval 워치에 주는 하트비트 주기 interval_ms(초기 20 s, 계약 범위 1~60 s). 실기기는 절전 중 10초 주기가 최대
 *        27초까지 늘어나 20초 × 3 = 60초로 끊김을 본다(docs/design/watch-i4-contract.md)
 * @param sweepInterval 끊김을 살피는 간격(초기 1 s). 끊김 판정은 interval × 3 + 이 값 안에 난다
 */
@ConfigurationProperties("simai.watch")
public record WatchProperties(Duration heartbeatInterval, Duration sweepInterval) {

    /** 계약 watch_heartbeat.interval_ms 범위 */
    static final long MIN_INTERVAL_MS = 1_000;
    static final long MAX_INTERVAL_MS = 60_000;

    /** 이만큼 하트비트를 잇달아 못 받으면 끊김(LOST). 계약 설명·노트북 하트비트와 같은 3회 */
    static final int MISSED_HEARTBEATS = 3;

    public WatchProperties {
        heartbeatInterval = positive(heartbeatInterval, Duration.ofSeconds(20));
        long ms = heartbeatInterval.toMillis();
        if (ms < MIN_INTERVAL_MS || ms > MAX_INTERVAL_MS) {
            throw new IllegalArgumentException("simai.watch.heartbeat-interval 은 1~60초여야 한다(계약 interval_ms)");
        }
        sweepInterval = positive(sweepInterval, Duration.ofSeconds(1));
    }

    long intervalMs() {
        return heartbeatInterval.toMillis();
    }

    /** interval × 3. 이보다 오래 하트비트가 없으면 LOST */
    long timeoutNanos() {
        return heartbeatInterval.toNanos() * MISSED_HEARTBEATS;
    }

    private static Duration positive(Duration value, Duration fallback) {
        return value == null || value.isZero() || value.isNegative() ? fallback : value;
    }
}
