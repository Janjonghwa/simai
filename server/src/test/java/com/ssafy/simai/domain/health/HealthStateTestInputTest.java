package com.ssafy.simai.domain.health;

import static org.assertj.core.api.Assertions.assertThat;

import java.time.Clock;
import java.time.Instant;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.json.JsonMapper;

/** S15P31A101-724: 노트북 하트비트 cameras[].source_kind 로 시험 입력(녹화 파일·합성 영상)을 판단한다(AGENTS 규칙 5) */
class HealthStateTestInputTest {

    private static final JsonMapper MAPPER = JsonMapper.builder().build();

    private static JsonNode payload(String camerasJson) {
        return MAPPER.readTree("""
                {"heartbeat_seq": 0, "tier0": {"control": "CONNECTED", "last_message_at": null, "video_cameras": []},
                 "site_display": "NORMAL" %s}
                """.formatted(camerasJson == null ? "" : ", \"cameras\": " + camerasJson));
    }

    @Test
    void FILE_나_SYNTHETIC_가_하나라도_있으면_시험_입력이다() {
        assertThat(HealthState.testInput(payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"FILE\"}]")))
                .isTrue();
        assertThat(HealthState.testInput(payload("""
                [{"camera_id": "cam_A", "rtsp": "CONNECTED", "source_kind": "RTSP"},
                 {"camera_id": "cam_B", "rtsp": "CONNECTED", "source_kind": "SYNTHETIC"}]"""))).isTrue();
    }

    @Test
    void RTSP_이거나_입력_종류가_없으면_시험_입력이_아니다() {
        assertThat(HealthState.testInput(payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"RTSP\"}]")))
                .isFalse();
        assertThat(HealthState.testInput(payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\"}]"))).as("이전 노트북").isFalse();
        assertThat(HealthState.testInput(payload(null))).as("중계 보고 없음").isFalse();
    }

    @Test
    void 노트북마다_마지막_하트비트로_판단하고_실제_카메라로_돌아오면_바로_풀린다() {
        HealthState state = new HealthState(Clock.systemUTC());
        assertThat(state.testInputActive()).isFalse();
        state.recordHeartbeat("laptop-01",
                payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"FILE\"}]"), Instant.now());
        state.recordHeartbeat("laptop-02",
                payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"RTSP\"}]"), Instant.now());
        assertThat(state.testInputActive()).as("하나라도 시험 입력").isTrue();
        state.recordHeartbeat("laptop-01",
                payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"RTSP\"}]"), Instant.now());
        assertThat(state.testInputActive()).isFalse();
    }

    @Test
    void 하트비트가_유지_시간보다_오래되면_시험_입력으로_보지_않는다() {
        long[] now = {0};
        HealthState state = new HealthState(Clock.systemUTC()) {
            @Override
            public long nanoTime() {
                return now[0];
            }
        };
        state.recordHeartbeat("laptop-01",
                payload("[{\"camera_id\": \"cam_A\", \"rtsp\": \"CONNECTED\", \"source_kind\": \"FILE\"}]"), Instant.now());
        now[0] = HealthState.TEST_INPUT_HOLD_MS * 1_000_000L;
        assertThat(state.testInputActive()).as("유지 시간 안").isTrue();
        now[0] += 1;
        assertThat(state.testInputActive()).isFalse();
    }
}
