package com.ssafy.simai.domain.scene;

import com.ssafy.simai.domain.health.HealthState;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Service;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 보기용 Scene을 생성 보정 대비 누적 자세 변화로 판정한다. 위치·위험 판정에 쓰지 않는다.
 * GET /api/scenes/current({@link #current})는 최대값 옆에 카메라별 중심 이동·회전 변화 {@code cameras} 도 싣는다(S15P31A101-602).
 * viewer_health({@link #compare})에는 싣지 않는다(주기 송신 크기·viewer_health 판 유지).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneCompatibility {
    static final double MAX_CENTER_M = 0.10;
    static final double MAX_ROTATION_RAD = Math.toRadians(2);
    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final HealthState health;

    public SceneCompatibility(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, HealthState health) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.health = health;
    }

    public ObjectNode current(String scene, String source) {
        var world = health.world();
        return compareWithCameras(scene, source, world == null ? null : world.calibrationVersion());
    }

    /** viewer_health 용. 카메라별 값({@code cameras})은 뺀다 */
    public ObjectNode compare(String scene, String source, String target) {
        ObjectNode out = compareWithCameras(scene, source, target);
        out.remove("cameras");
        return out;
    }

    /**
     * 카메라별 값까지 실은 판정. {@code cameras} 는 두 보정의 카메라 자세를 실제로 비교했을 때만 배열이고(카메라 ID 순),
     * 같은 보정(SAME_CALIBRATION: 모두 0)·다른 World·자세 미상이면 null 이다.
     */
    ObjectNode compareWithCameras(String scene, String source, String target) {
        ObjectNode out = mapper.createObjectNode();
        out.put("scene_version", scene);
        out.put("calibration_version", target);
        out.put("verification", "DIMMED");
        out.put("regeneration_required", true);
        out.put("reason", "UNKNOWN");
        out.putNull("max_center_delta_m");
        out.putNull("max_rotation_delta_rad");
        out.putNull("cameras");
        if (source == null || target == null) return out;
        // 버전은 불변이며 정확히 같은 보정은 별도 자세 자료 없이도 같은 좌표를 사용한다.
        if (source.equals(target)) return result(out, "VERIFIED", "SAME_CALIBRATION", 0., 0.);
        List<String> sites = jdbc.queryForList("SELECT site_id FROM calibration_version WHERE calibration_version IN (:a, :b)",
                Map.of("a", source, "b", target), String.class);
        if (sites.size() != 2) return out;
        if (!sites.get(0).equals(sites.get(1))) return result(out, "MISMATCH", "DIFFERENT_WORLD", null, null);
        JsonNode a = metadata(source), b = metadata(target);
        if (a == null || b == null) return out;
        String oldWorld = a.path("world_frame_id").asString(null), newWorld = b.path("world_frame_id").asString(null);
        if (oldWorld == null || oldWorld.isBlank() || newWorld == null || newWorld.isBlank()) return out;
        if (!oldWorld.equals(newWorld)) {
            // 과거 업로드 문서의 자기 버전은 root와 같을 때만 계보 연결 근거다. 다른 ID면 계보 미상이다.
            if (a.path("legacy").asBoolean(false) || b.path("legacy").asBoolean(false)) return out;
            return result(out, "MISMATCH", "DIFFERENT_WORLD", null, null);
        }
        return evaluate(out, a.path("camera_poses"), b.path("camera_poses"));
    }

    private JsonNode metadata(String version) {
        var params = Map.of("v", version);
        List<String> updates = jdbc.queryForList("""
                SELECT payload::text FROM calibration_update_message WHERE calibration_version = :v
                  AND payload -> 'world_frame_id' IS NOT NULL AND payload -> 'camera_poses' IS NOT NULL
                ORDER BY received_at DESC, msg_id DESC LIMIT 1
                """, params, String.class);
        if (!updates.isEmpty()) return mapper.readTree(updates.getFirst());
        List<byte[]> docs = jdbc.queryForList("SELECT content FROM calibration_document WHERE calibration_version = :v",
                params, byte[].class);
        if (docs.isEmpty()) return null;
        JsonNode doc = mapper.readTree(docs.getFirst());
        ObjectNode legacy = mapper.createObjectNode();
        legacy.put("world_frame_id", version);
        legacy.put("legacy", true);
        legacy.set("camera_poses", doc.path("cameras"));
        return legacy;
    }

    static ObjectNode evaluate(ObjectNode out, JsonNode oldPoses, JsonNode newPoses) {
        try {
            Map<String, double[][]> old = poses(oldPoses), now = poses(newPoses);
            if (old.isEmpty() || !old.keySet().equals(now.keySet())) return out;
            double maxDistance = 0, maxAngle = 0;
            ArrayNode cameras = out.arrayNode();
            for (String id : old.keySet()) {
                double[][] a = old.get(id), b = now.get(id);
                double distance = 0, trace = 0;
                for (int i = 0; i < 3; i++) {
                    double ca = 0, cb = 0;
                    for (int j = 0; j < 3; j++) {
                        ca -= a[j][i] * a[3][j];
                        cb -= b[j][i] * b[3][j];
                        trace += a[i][j] * b[i][j];
                    }
                    distance += (ca - cb) * (ca - cb);
                }
                double centerDelta = Math.sqrt(distance), rotationDelta = Math.acos(Math.clamp((trace - 1) / 2, -1., 1.));
                maxDistance = Math.max(maxDistance, centerDelta);
                maxAngle = Math.max(maxAngle, rotationDelta);
                cameras.addObject().put("camera_id", id).put("center_delta_m", centerDelta).put("rotation_delta_rad", rotationDelta);
            }
            if (!Double.isFinite(maxDistance) || !Double.isFinite(maxAngle)) return out;
            boolean small = maxDistance <= MAX_CENTER_M + 1e-9 && maxAngle <= MAX_ROTATION_RAD + 1e-9;
            result(out, small ? "VERIFIED" : "DIMMED", small ? "SMALL_CHANGE" : "LARGE_CHANGE", maxDistance, maxAngle);
            out.set("cameras", cameras);
            return out;
        } catch (IllegalArgumentException e) {
            return out;
        }
    }

    private static Map<String, double[][]> poses(JsonNode poses) {
        if (!poses.isArray()) throw new IllegalArgumentException("자세 없음");
        Map<String, double[][]> result = new TreeMap<>();
        for (JsonNode pose : poses) {
            String id = pose.path("camera_id").asString("");
            JsonNode rotation = pose.path("R"), translation = pose.path("t");
            if (id.isBlank() || rotation.size() != 3 || translation.size() != 3 || result.containsKey(id))
                throw new IllegalArgumentException("카메라 누락/중복");
            double[][] value = new double[4][3];
            for (int i = 0; i < 3; i++) {
                if (rotation.path(i).size() != 3) throw new IllegalArgumentException("회전 크기 오류");
                value[3][i] = number(translation.path(i));
                for (int j = 0; j < 3; j++) value[i][j] = number(rotation.path(i).path(j));
            }
            for (int i = 0; i < 3; i++) for (int j = 0; j < 3; j++) {
                double dot = 0;
                for (int k = 0; k < 3; k++) dot += value[i][k] * value[j][k];
                if (Math.abs(dot - (i == j ? 1 : 0)) > 1e-5) throw new IllegalArgumentException("회전 직교 오류");
            }
            double det = value[0][0] * (value[1][1]*value[2][2]-value[1][2]*value[2][1])
                    - value[0][1] * (value[1][0]*value[2][2]-value[1][2]*value[2][0])
                    + value[0][2] * (value[1][0]*value[2][1]-value[1][1]*value[2][0]);
            if (Math.abs(det - 1) > 1e-5) throw new IllegalArgumentException("회전 방향 오류");
            result.put(id, value);
        }
        return result;
    }

    private static double number(JsonNode node) {
        if (!node.isNumber() || !Double.isFinite(node.asDouble())) throw new IllegalArgumentException("유한수 아님");
        return node.asDouble();
    }

    private static ObjectNode result(ObjectNode out, String verification, String reason, Double distance, Double rotation) {
        out.put("verification", verification);
        out.put("regeneration_required", "DIMMED".equals(verification));
        out.put("reason", reason);
        out.put("max_center_delta_m", distance);
        out.put("max_rotation_delta_rad", rotation);
        return out;
    }
}
