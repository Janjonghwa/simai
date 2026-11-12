package com.ssafy.simai.domain.setup;

import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 시험용 site_setup 1.14.0 {@code camera_check}(S15P31A101-646). 계약 모양을 지킨 최소 값이다.
 * 구역 단계·세팅 완료 관문을 통과해야 하는 시험은 READY 스냅샷에 {@link #passed} 를 싣는다.
 */
public final class SetupCameraCheckFixtures {

    private SetupCameraCheckFixtures() {
    }

    public static ObjectNode check(ObjectMapper mapper, String calibrationVersion, String status) {
        ObjectNode check = mapper.createObjectNode()
                .put("status", status)
                .put("calibration_version", calibrationVersion)
                .put("progress_pct", "PASSED".equals(status) ? 100.0 : 40.0)
                .put("elapsed_s", 42.0);
        check.putObject("thresholds")
                .put("min_duration_s", 30.0).put("max_duration_s", 120.0).put("min_pair_samples", 30)
                .put("min_pair_pass_rate", 0.75).put("max_pair_residual_median_px", 8.0).put("min_camera_views", 30)
                .put("min_loo_samples", 20).put("max_loo_median_px", 8.0).put("min_evaluated_pairs", 3);
        ObjectNode pair = check.putObject("pairs").putObject("cam1-cam2");
        pair.putArray("camera_ids").add("cam1").add("cam2");
        pair.put("required", true).put("samples", 60).put("pass_rate", 0.95).put("residual_median_px", 2.4)
                .put("verdict", "PASSED".equals(status) ? "PASS" : "FAIL");
        check.putObject("cameras").putObject("cam1").put("observed_views", 120).put("loo_samples", 80)
                .put("loo_median_px", 2.1).put("loo_p95_px", 5.0).put("verdict", "PASS");
        var problems = check.putArray("problems");
        if (!"PASSED".equals(status) && !"WAITING".equals(status) && !"MEASURING".equals(status)) {
            problems.addObject().put("kind", "PAIR").put("id", "cam1-cam2").put("reason", "PASS_RATE_LOW")
                    .put("message", "cam1–cam2: 두 카메라가 같은 사람을 같은 위치로 보지 못했습니다");
        }
        return check;
    }

    public static ObjectNode passed(ObjectMapper mapper, String calibrationVersion) {
        return check(mapper, calibrationVersion, "PASSED");
    }

    /** 세션 행에 직접 넣는 PASSED 값(JSON 문자열) */
    public static String passedJson(ObjectMapper mapper, String calibrationVersion) {
        return mapper.writeValueAsString(passed(mapper, calibrationVersion));
    }
}
