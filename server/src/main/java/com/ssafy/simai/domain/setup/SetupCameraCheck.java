package com.ssafy.simai.domain.setup;

import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 세팅 보정 품질 게이트와 「카메라 확인」(S15P31A101-646, docs/design/S15P31A101-646-setup-camera-check.md).
 * <p>I5 site_setup 1.14.0 스냅샷의 {@code quality_gate}·{@code camera_check} 를 세션에 남길 모양으로 고르고, 구역 단계·세팅 완료를
 * 받을 수 있는지 판단한다. 서버는 하트비트를 스키마로 검사하지 않으므로 여기서 모양(객체·상태 값·크기)만 본다. 표시용 값이 어긋나면
 * 저장하지 않고 이전 값을 둔다(스냅샷 자체를 거부하지 않는다). 받는 쪽은 status 와 calibration_version 만 판단에 쓴다.
 */
public final class SetupCameraCheck {

    /** 세션 행 jsonb 한 칸의 상한(쌍 256·카메라 64 를 넉넉히 담는다) */
    private static final int MAX_JSON_BYTES = 256 * 1024;
    private static final Set<String> GATE_VERDICTS = Set.of("PASSED", "NEEDS_CHECK", "FAILED");
    private static final Set<String> CHECK_STATUSES = Set.of("WAITING", "MEASURING", "PASSED", "FAILED", "INCOMPLETE");
    public static final String PASSED = "PASSED";

    private SetupCameraCheck() {
    }

    /** @return 저장할 quality_gate JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    static String qualityGate(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode gate = snapshot.get("quality_gate");
        if (gate == null || !gate.isObject() || !GATE_VERDICTS.contains(gate.path("verdict").asString(""))
                || !gate.path("pairs").isObject() || !gate.path("cameras").isObject() || !gate.path("problems").isArray()) {
            return null;
        }
        return bounded(mapper, gate);
    }

    /** @return 저장할 camera_check JSON 문자열, 이 스냅샷에 없거나 어긋나면 null */
    static String cameraCheck(ObjectMapper mapper, JsonNode snapshot) {
        JsonNode check = snapshot.get("camera_check");
        if (check == null || !check.isObject() || !CHECK_STATUSES.contains(check.path("status").asString(""))
                || !check.path("calibration_version").isString() || check.path("calibration_version").asString().isBlank()
                || !check.path("pairs").isObject() || !check.path("cameras").isObject() || !check.path("problems").isArray()) {
            return null;
        }
        return bounded(mapper, check);
    }

    private static String bounded(ObjectMapper mapper, JsonNode node) {
        String json = mapper.writeValueAsString(node);
        return json.length() > MAX_JSON_BYTES ? null : json;
    }

    /**
     * 카메라 확인을 통과했는지: 상태 PASSED 이고 그 확인이 잰 보정이 지금 세션 보정과 같다. 재대여로 보정이 바뀌면 이전 대여의 통과는
     * 새 보정의 통과가 아니다. 모르면(확인 결과 없음, 보정 없음) 통과가 아니다.
     */
    public static boolean passed(String status, String checkedCalibrationVersion, String sessionCalibrationVersion) {
        return PASSED.equals(status) && sessionCalibrationVersion != null
                && sessionCalibrationVersion.equals(checkedCalibrationVersion);
    }
}
