package com.ssafy.simai.domain.health;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Clock;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.support.DefaultListableBeanFactory;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** S15P31A101-736: 녹화 재생 시험 스위치는 기본 꺼짐이고, 걷기 확인 생략은 노트북 입력이 모두 녹화 파일일 때만 허용한다 */
class ReplayTestSwitchTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private static JsonNode heartbeat(String... kinds) {
        StringBuilder cameras = new StringBuilder();
        for (int i = 0; i < kinds.length; i++) {
            cameras.append(i == 0 ? "" : ",").append("{\"camera_id\": \"cam_").append((char) ('A' + i))
                    .append("\", \"rtsp\": \"CONNECTED\"").append(kinds[i] == null ? "" : ", \"source_kind\": \"" + kinds[i] + "\"")
                    .append('}');
        }
        return MAPPER.readTree("""
                {"heartbeat_seq": 0, "tier0": {"control": "CONNECTED", "last_message_at": null, "video_cameras": []},
                 "site_display": "NORMAL", "cameras": [%s]}
                """.formatted(cameras));
    }

    private static ObjectProvider<HealthState> provider(HealthState state) {
        DefaultListableBeanFactory factory = new DefaultListableBeanFactory();
        if (state != null) {
            factory.registerSingleton("healthState", state);
        }
        return factory.getBeanProvider(HealthState.class);
    }

    @Test
    void 끄면_녹화_파일_입력이어도_건너뛰지_않는다() {
        HealthState state = new HealthState(Clock.systemUTC());
        state.recordHeartbeat("laptop-01", heartbeat("FILE", "FILE", "FILE", "FILE"), Instant.now());
        ReplayTestSwitch off = new ReplayTestSwitch(false, false, provider(state));
        assertThat(off.alerts()).isFalse();
        assertThat(off.cameraCheckSkipAllowed()).isFalse();
    }

    @Test
    void 켜면_입력이_모두_녹화_파일일_때만_건너뛴다() {
        HealthState state = new HealthState(Clock.systemUTC());
        ReplayTestSwitch on = new ReplayTestSwitch(true, true, provider(state));
        assertThat(on.alerts()).isTrue();
        assertThat(on.cameraCheckSkipAllowed()).as("하트비트 없음(입력 모름)").isFalse();
        state.recordHeartbeat("laptop-01", heartbeat("FILE", "FILE", "FILE", "FILE"), Instant.now());
        assertThat(on.cameraCheckSkipAllowed()).isTrue();
        state.recordHeartbeat("laptop-01", heartbeat("FILE", "FILE", "FILE", "RTSP"), Instant.now());
        assertThat(on.cameraCheckSkipAllowed()).as("실제 카메라가 섞임").isFalse();
        state.recordHeartbeat("laptop-01", heartbeat("RTSP", "RTSP", "RTSP", "RTSP"), Instant.now());
        assertThat(on.cameraCheckSkipAllowed()).as("라이브").isFalse();
        state.recordHeartbeat("laptop-01", heartbeat("SYNTHETIC", "SYNTHETIC"), Instant.now());
        assertThat(on.cameraCheckSkipAllowed()).as("합성 영상은 녹화 재생이 아니다").isFalse();
        state.recordHeartbeat("laptop-01", heartbeat("FILE", null), Instant.now());
        assertThat(on.cameraCheckSkipAllowed()).as("입력 종류 모름(이전 중계)").isFalse();
        assertThat(new ReplayTestSwitch(true, true, provider(null)).cameraCheckSkipAllowed()).as("HealthState 없음").isFalse();
    }

    @Test
    void 녹화_파일뿐인지는_살아_있는_노트북_모두로_본다() {
        HealthState state = new HealthState(Clock.systemUTC());
        state.recordHeartbeat("laptop-01", heartbeat("FILE", "FILE"), Instant.now());
        state.recordHeartbeat("laptop-02", heartbeat("RTSP", "RTSP"), Instant.now());
        assertThat(state.replayFileOnly()).isFalse();
        state.recordHeartbeat("laptop-02", heartbeat("FILE", "FILE"), Instant.now());
        assertThat(state.replayFileOnly()).isTrue();
        assertThat(HealthState.fileOnly(MAPPER.readTree("{\"cameras\": []}"))).as("카메라 보고 없음").isFalse();
    }
}
