package com.ssafy.simai.domain.watch;

import java.time.Duration;
import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * 워치 I4 연결 설정(simai.watch, S15P31A101-336, 설계 docs/design/server-watch-link.md). 비우면 초기값.
 *
 * @param heartbeatInterval 워치에 주는 하트비트 주기 interval_ms(초기 20 s, 계약 범위 1~60 s). 실기기는 절전 중 10초 주기가 최대
 *        27초까지 늘어나 20초 × 3 = 60초로 끊김을 본다(docs/design/watch-i4-contract.md)
 * @param sweepInterval 끊김을 살피는 간격(초기 1 s). 끊김 판정은 interval × 3 + 이 값 안에 난다
 * @param ackWithin watch_alert 의 확인 기한 ack_within_ms(초기 10 s, 계약 1 ms~10분). 이 안에 VIBRATED ACK 가 EC2 에 닿지 않으면
 *        관리자에게 격상한다(S15P31A101-338). 10초는 '진입 → 진동 p95 1.5초' 와 재연결 백오프(최대 30초)를 함께 본 초깃값(watch-i4-contract.md)
 * @param escalationInterval ACK 기한이 지난 알림을 살피는 간격(초기 1 s). 격상은 기한 + 이 값 안에 난다
 * @param matchInterval 움직임 대조·배정을 다시 계산하는 간격(초기 2 s, 워치가 걸음 특징 창을 보내는 주기와 같다, S15P31A101-340)
 * @param demoDevice 시연 스위치(S15P31A101-793): 비어 있지 않으면 배정과 상관없이 모든 LIVE 위험 이벤트를 이 워치(기기 ID)로도 보낸다.
 *        시연은 사람이 하나라 걸음 대조·수동 배정을 기다리지 않고 바로 울리게 한다. 배정 표는 건드리지 않는다. 본 운영에서는 비운다
 */
@ConfigurationProperties("simai.watch")
public record WatchProperties(Duration heartbeatInterval, Duration sweepInterval, Duration ackWithin, Duration escalationInterval,
        Duration matchInterval, String demoDevice) {

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
        ackWithin = positive(ackWithin, Duration.ofSeconds(10));
        escalationInterval = positive(escalationInterval, Duration.ofSeconds(1));
        matchInterval = positive(matchInterval, Duration.ofSeconds(2));
        demoDevice = demoDevice == null || demoDevice.isBlank() ? null : demoDevice.strip();
        if (ackWithin.toMillis() < 1 || ackWithin.toMillis() > 600_000) {
            throw new IllegalArgumentException("simai.watch.ack-within 은 1 ms~10분이어야 한다(계약 ack_within_ms)");
        }
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
