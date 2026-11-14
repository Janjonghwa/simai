package com.ssafy.simai.domain.setup;

import java.util.Map;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 세팅 배경 촬영 진행(S15P31A101-686, docs/design/S15P31A101-686-fresh-setup-background.md).
 * <p>I5 site_setup 1.16.0 COLLECTING_BACKGROUND 스냅샷의 {@code background_capture}(세팅 시작 뒤 새로 모은 표본 수와 카메라별
 * 상태 READY·INSUFFICIENT_SAMPLES·PERSON_OCCLUDED·NO_VIDEO)를 세션에 남길 모양으로 고른다. 화면이 배경 촬영 창에서 그 카메라만 다시
 * 안내하는 데 쓰는 표시용 값이라, 모양이 어긋나면 저장하지 않고 이전 값을 둔다(스냅샷 자체를 거부하지 않는다).
 */
public final class SetupBackgroundCapture {

    /** 카메라 16대를 넉넉히 담는 jsonb 한 칸 상한 */
    private static final int MAX_JSON_BYTES = 16 * 1024;
    private static final Set<String> STATES = Set.of("READY", "INSUFFICIENT_SAMPLES", "PERSON_OCCLUDED", "NO_VIDEO");

    private SetupBackgroundCapture() {
    }

    /** @return 저장할 background_capture JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    static String fromSnapshot(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode capture = snapshot.get("background_capture");
        if (capture == null || !capture.isObject()
                || !isNonNegativeLong(capture.get("since_ms"))
                || !isPositiveInt(capture.get("required_samples"))) {
            return null;
        }
        JsonNode cameras = capture.get("cameras");
        if (cameras == null || !cameras.isObject() || cameras.properties().isEmpty()) {
            return null;
        }
        for (Map.Entry<String, JsonNode> e : cameras.properties()) {
            JsonNode cam = e.getValue();
            if (!cam.isObject() || !STATES.contains(cam.path("state").asString(""))
                    || !isNonNegativeLong(cam.get("sample_count"))) {
                return null;
            }
        }
        String json = mapper.writeValueAsString(capture);
        return json.length() > MAX_JSON_BYTES ? null : json;
    }

    private static boolean isNonNegativeLong(JsonNode node) {
        return node != null && node.isIntegralNumber() && node.asLong() >= 0;
    }

    private static boolean isPositiveInt(JsonNode node) {
        return node != null && node.isIntegralNumber() && node.asLong() >= 1;
    }
}
