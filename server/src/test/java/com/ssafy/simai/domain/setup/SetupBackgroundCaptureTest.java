package com.ssafy.simai.domain.setup;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;
import tools.jackson.databind.json.JsonMapper;
import tools.jackson.databind.node.ObjectNode;

/** 세팅 배경 촬영 진행 모양 검사 단위 시험(S15P31A101-686) */
class SetupBackgroundCaptureTest {

    private final JsonMapper mapper = JsonMapper.builder().build();

    private ObjectNode snapshot() {
        ObjectNode snap = mapper.createObjectNode();
        ObjectNode capture = snap.putObject("background_capture")
                .put("since_ms", 1_763_020_800_000L).put("required_samples", 12);
        ObjectNode cams = capture.putObject("cameras");
        cams.putObject("cam_A").put("state", "READY").put("sample_count", 12);
        cams.putObject("cam_B").put("state", "PERSON_OCCLUDED").put("sample_count", 4).put("person_visible", true);
        return snap;
    }

    @Test
    void 모양이_맞는_촬영_진행만_저장하고_어긋나면_버린다() {
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, snapshot())).contains("PERSON_OCCLUDED", "\"required_samples\":12");
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, mapper.createObjectNode())).isNull();

        ObjectNode unknownState = snapshot();
        ((ObjectNode) unknownState.at("/background_capture/cameras/cam_A")).put("state", "REFERENCE_FALLBACK");
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, unknownState)).isNull();

        ObjectNode noSince = snapshot();
        ((ObjectNode) noSince.get("background_capture")).remove("since_ms");
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, noSince)).isNull();

        ObjectNode negativeCount = snapshot();
        ((ObjectNode) negativeCount.at("/background_capture/cameras/cam_B")).put("sample_count", -1);
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, negativeCount)).isNull();

        ObjectNode emptyCameras = snapshot();
        ((ObjectNode) emptyCameras.get("background_capture")).putObject("cameras");
        assertThat(SetupBackgroundCapture.fromSnapshot(mapper, emptyCameras)).isNull();
    }
}
