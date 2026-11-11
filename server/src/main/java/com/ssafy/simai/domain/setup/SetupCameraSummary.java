package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.calibration.CalibrationViews;
import java.util.HashMap;
import java.util.Map;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 세팅 세션 조회(GET /api/setup-sessions/{id})의 카메라별 요약 {@code cameras}(S15P31A101-602,
 * docs/design/S15P31A101-602-setup-camera-summary.md). 이 세션의 보정 문서(contracts/providers/calibration, EC2 calibration_document)에서
 * 계산·복사만 한다. 값을 지어내지 않는다.
 * <ul>
 *   <li>위치·정면·수평 시야각은 관제 보정 상태와 같은 계산({@link CalibrationViews#cameraPoses(ObjectMapper, byte[])})을 쓴다.
 *       World 미터, 오른손, +Z 상방이다.
 *   <li>{@code yaw_rad} = atan2(forward_y, forward_x)(World +X 에서 +Y 쪽으로 반시계), {@code pitch_rad} = asin(forward_z)(아래를 보면 음수).
 *       정면이 거의 수직이라 방위가 정해지지 않으면 yaw 는 null 이다.
 *   <li>{@code image_size}·{@code camera_height_m}·{@code reprojection_error_px}(세팅 홀드아웃 median·p95)는 문서 값 그대로이고, 없으면 null 이다.
 *   <li>{@code world_anchor} 는 문서의 {@code world_anchor}(같은 현장 이전 보정에 다시 맞춘 기록)에서 이 카메라 값만 뽑는다.
 *       기록이 없거나 비교한 이전 보정({@code reference_calibration_version})이 없으면 null, 그 카메라 값이 없으면 그 칸만 null 이다.
 * </ul>
 * 문서가 없거나 읽을 수 없거나 위치 계산이 거부되면(빈 목록) null 을 돌려준다(빈 배열이 아니라 '아직 모름').
 * 표시용이며 위치·위험 판정에 쓰지 않는다(Tier 0 정본 불변).
 */
final class SetupCameraSummary {

    private static final double VERTICAL_EPS = 1e-9;

    private SetupCameraSummary() {
    }

    /** @return 카메라별 요약 배열, 만들 수 없으면 JSON null */
    static JsonNode of(ObjectMapper mapper, byte[] calibrationDocument) {
        ArrayNode poses = CalibrationViews.cameraPoses(mapper, calibrationDocument);
        if (poses.isEmpty()) {
            return mapper.nullNode();
        }
        // cameraPoses 가 이미 읽고 검사한 문서다(읽지 못했다면 빈 목록이었다)
        JsonNode doc = mapper.readTree(calibrationDocument);
        Map<String, JsonNode> docCameras = new HashMap<>();
        for (JsonNode cam : doc.path("cameras")) {
            docCameras.put(cam.path("camera_id").asString("").strip(), cam);
        }
        JsonNode anchor = doc.path("world_anchor");
        String reference = anchor.isObject() ? text(anchor.get("reference_calibration_version")) : null;

        ArrayNode out = mapper.createArrayNode();
        for (JsonNode pose : poses) {
            String cameraId = pose.path("camera_id").asString();
            JsonNode cam = docCameras.get(cameraId);
            JsonNode forward = pose.path("forward_xyz");
            double fx = forward.path(0).asDouble(), fy = forward.path(1).asDouble(), fz = forward.path(2).asDouble();

            ObjectNode item = out.addObject();
            item.put("camera_id", cameraId);
            item.set("position_xyz", pose.get("position_xyz"));
            item.set("forward_xyz", forward);
            if (Math.hypot(fx, fy) < VERTICAL_EPS) {
                item.putNull("yaw_rad");
            } else {
                item.put("yaw_rad", Math.atan2(fy, fx));
            }
            item.put("pitch_rad", Math.asin(Math.max(-1.0, Math.min(1.0, fz))));
            item.set("hfov_rad", pose.get("hfov_rad"));
            item.set("image_size", cam.get("image_size").deepCopy());
            item.set("camera_height_m", number(mapper, cam.get("camera_height_m")));
            item.set("reprojection_error_px", reprojection(mapper, cam.get("reprojection_error_px")));
            item.set("world_anchor", reference == null ? mapper.nullNode() : anchorFor(mapper, anchor, reference, cameraId));
        }
        return out;
    }

    private static JsonNode reprojection(ObjectMapper mapper, JsonNode node) {
        if (node == null || !node.isObject()) {
            return mapper.nullNode();
        }
        ObjectNode out = mapper.createObjectNode();
        out.set("median", number(mapper, node.get("median")));
        out.set("p95", number(mapper, node.get("p95")));
        return out;
    }

    private static ObjectNode anchorFor(ObjectMapper mapper, JsonNode anchor, String reference, String cameraId) {
        ObjectNode out = mapper.createObjectNode();
        out.put("reference_calibration_version", reference);
        JsonNode accepted = anchor.get("accepted");
        if (accepted != null && accepted.isBoolean()) {
            out.put("accepted", accepted.asBoolean());
        } else {
            out.putNull("accepted");
        }
        String reason = text(anchor.get("reason"));
        if (reason == null) {
            out.putNull("reason");
        } else {
            out.put("reason", reason);
        }
        out.set("orientation_change_rad", number(mapper, anchor.path("orientation_changes_rad").get(cameraId)));
        out.set("center_residual_m", number(mapper, anchor.path("center_residuals_m").get(cameraId)));
        out.set("applied_center_residual_m", number(mapper, anchor.path("applied_center_residuals_m").get(cameraId)));
        out.set("vertical_difference_m", number(mapper, anchor.path("vertical_differences_m").get(cameraId)));
        return out;
    }

    private static JsonNode number(ObjectMapper mapper, JsonNode node) {
        if (node == null || !node.isNumber() || !Double.isFinite(node.asDouble())) {
            return mapper.nullNode();
        }
        return node.deepCopy();
    }

    private static String text(JsonNode node) {
        if (node == null || !node.isString() || node.asString().isBlank()) {
            return null;
        }
        return node.asString();
    }
}
