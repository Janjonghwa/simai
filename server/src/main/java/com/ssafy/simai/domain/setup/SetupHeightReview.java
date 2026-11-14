package com.ssafy.simai.domain.setup;

import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 세팅 카메라 높이 빠른 확인(S15P31A101-690, docs/design/S15P31A101-690-height-check-in-input.md).
 * <p>I5 site_setup 1.17.0 INITIAL_POSE_AND_WORLD 스냅샷의 {@code height_review}(카메라별 입력 대비 상대 불일치와 확인 상태, 워커가
 * 이 스냅샷부터 쓰는 높이)를 읽는다. 워커가 쓰는 높이가 곧 {@code height_checks} 의 입력 높이라 세션 높이를 이 값으로 바꾸므로,
 * 카메라 집합이 세션과 다르거나 높이가 틀리면 스냅샷을 거부한다(INVALID_SETUP_PROGRESS).
 * <p>{@code AWAITING_CONFIRMATION} 이고 관리자 결정({@code height_decision})이 아직 없으면 세션은 사람 확인을 기다린다
 * ({@code paused_reason = HEIGHT_CHECK}). 결정은 {@code POST /api/setup-sessions/{id}/resume} 으로 한 번만 받고, 하트비트 응답
 * (job_lease_status 2.2.0 {@code height_decision})으로 워커에 간다.
 */
public final class SetupHeightReview {

    public static final String AWAITING = "AWAITING_CONFIRMATION";
    public static final String PAUSED_REASON = "HEIGHT_CHECK";
    public static final String CONFIRMED = "CONFIRMED";
    public static final String CORRECTED = "CORRECTED";
    private static final Set<String> STATUSES = Set.of("PASSED", AWAITING, CONFIRMED, CORRECTED, "TIMED_OUT");
    private static final Set<String> VERDICTS = Set.of("OK", "LOW", "HIGH");
    /** 카메라 16대를 넉넉히 담는 jsonb 한 칸 상한 */
    private static final int MAX_JSON_BYTES = 16 * 1024;
    /** 같은 높이로 보는 차(m). 화면 입력은 cm 단위(0.01 m) */
    static final double SAME_HEIGHT_M = 1e-4;

    private SetupHeightReview() {
    }

    /** 스냅샷의 height_review. json 은 저장할 문자열, heights 는 워커가 쓰는 카메라별 높이(세션 카메라 순서) */
    record Parsed(String json, String status, Map<String, Double> heights) {
    }

    /**
     * @param cameras 세션 카메라(세션 높이의 키)
     * @return 이 스냅샷에 없으면 null
     */
    static Parsed fromSnapshot(ObjectMapper mapper, JsonNode snapshot, Set<String> cameras, double maxHeightM) {
        JsonNode review = snapshot.get("height_review");
        if (review == null || review.isNull()) {
            return null;
        }
        String status = review.isObject() ? review.path("status").asString(null) : null;
        JsonNode heightsNode = review.get("camera_heights_m");
        JsonNode camerasNode = review.get("cameras");
        if (status == null || !STATUSES.contains(status) || heightsNode == null || !heightsNode.isObject()
                || camerasNode == null || !camerasNode.isObject()
                || heightsNode.properties().size() != cameras.size() || camerasNode.properties().size() != cameras.size()) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        Map<String, Double> heights = new LinkedHashMap<>();
        for (String cam : cameras) {
            JsonNode h = heightsNode.get(cam);
            JsonNode c = camerasNode.get(cam);
            if (h == null || !h.isNumber() || !Double.isFinite(h.doubleValue()) || h.doubleValue() <= 0.0
                    || h.doubleValue() > maxHeightM || c == null || !c.isObject()
                    || !VERDICTS.contains(c.path("verdict").asString(""))) {
                throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
            }
            heights.put(cam, h.doubleValue());
        }
        String json = mapper.writeValueAsString(review);
        if (json.length() > MAX_JSON_BYTES) {
            throw new BusinessException(ErrorCode.Setup.INVALID_SETUP_PROGRESS);
        }
        return new Parsed(json, status, heights);
    }

    /** 사람 확인을 기다리는가: 마지막 height_review 가 AWAITING_CONFIRMATION 이고 결정이 아직 없다 */
    static boolean awaiting(String reviewStatus, String decisionJson) {
        return AWAITING.equals(reviewStatus) && decisionJson == null;
    }
}
