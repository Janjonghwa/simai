package com.ssafy.simai.domain.watch;

import static org.assertj.core.api.Assertions.assertThat;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import com.ssafy.simai.domain.health.HealthState;
import com.ssafy.simai.domain.health.ReplayTestSwitch;
import com.ssafy.simai.domain.realtime.RealtimePublisher;
import java.time.Clock;
import java.time.Duration;
import java.util.UUID;
import org.junit.jupiter.api.Test;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import tools.jackson.databind.json.JsonMapper;

/**
 * S15P31A101-736: 재생 시험 알림 스위치를 켜면 시험 입력(녹화 파일·합성 영상) 중에도 워치로 보낸다. 끄면 -724 그대로 막고 기억한다
 * (끈 동작의 끝까지 흐름은 WatchAlertIntegrationTest).
 */
class WatchAlertReplayTestSwitchTest {

    private WatchAlertService service(boolean alertsSwitch, HealthState health) {
        ReplayTestSwitch replayTest = mock(ReplayTestSwitch.class);
        when(replayTest.alerts()).thenReturn(alertsSwitch);
        WatchProperties properties = new WatchProperties(Duration.ofSeconds(20), Duration.ofSeconds(5), Duration.ofSeconds(10),
                Duration.ofHours(1), Duration.ofSeconds(2), null);
        return new WatchAlertService(mock(NamedParameterJdbcTemplate.class), mock(WatchBindings.class), mock(WatchHub.class),
                properties, mock(RealtimePublisher.class), JsonMapper.builder().build(), Clock.systemUTC(), health, replayTest);
    }

    @Test
    void 스위치를_켜면_시험_입력_중에도_막지_않고_기억하지도_않는다() {
        HealthState health = mock(HealthState.class);
        when(health.testInputActive()).thenReturn(true);
        UUID eventId = UUID.randomUUID();
        try (WatchAlertService on = service(true, health)) {
            assertThat(on.suppressedForTestInput(eventId)).isFalse();
            when(health.testInputActive()).thenReturn(false);
            assertThat(on.suppressedForTestInput(eventId)).isFalse();
        }
    }

    @Test
    void 스위치를_끄면_시험_입력_이벤트는_막고_실제_카메라로_돌아와도_막는다() {
        HealthState health = mock(HealthState.class);
        when(health.testInputActive()).thenReturn(true);
        UUID replayed = UUID.randomUUID();
        try (WatchAlertService off = service(false, health)) {
            assertThat(off.suppressedForTestInput(replayed)).isTrue();
            when(health.testInputActive()).thenReturn(false);
            assertThat(off.suppressedForTestInput(replayed)).isTrue();
            assertThat(off.suppressedForTestInput(UUID.randomUUID())).as("실제 카메라 이벤트").isFalse();
        }
    }
}
