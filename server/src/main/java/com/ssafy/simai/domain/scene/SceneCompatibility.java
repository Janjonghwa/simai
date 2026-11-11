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
 * 세팅 보정이 World 다시 맞추기(world_anchor 채택, -584)로 Scene 의 World 에 옮겨졌으면 World 계보 ID 가 달라도 같은 World 로 보고
 * 채택 한도에 맞춘 값으로 자세 변화를 판정한다(이전 모델 재사용 세팅, S15P31A101-614). 판정 이유 값은 그대로다(계약 변경 없음).
 */
@Service
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SceneCompatibility {
    static final double MAX_CENTER_M = 0.10;
    static final double MAX_ROTATION_RAD = Math.toRadians(2);
    /**
     * World 다시 맞추기(world_anchor, -584)로 같은 World 에 옮긴 세팅 보정의 한도(S15P31A101-614, docs/design/S15P31A101-614-setup-reuse-mode.md).
     * 채택 기준(수평 배치 잔차 0.15 m·축척 ±3%·방향 10°)을 통과한 같은 배치도 Stage A 차이·실측 높이·바닥 기울기 보정 차이로 카메라 중심이
     * 3차원으로 약 15 cm(12·13차 관측: 수평 적용 잔차 ≤ 11.4 cm, 판정에 쓰지 않는 높이 차 ≤ 9.4 cm), 방향이 최대 6° 달라진다. 0.10 m·2° 로
     * 보면 방이 그대로인 재사용 세팅이 거의 늘 흐리게 나오므로, 중심은 수평 한도 0.15 m 에 높이 차 0.10 m 를 더한 0.25 m, 방향은 채택 한도
     * 10° 를 쓴다. 넘으면 기존처럼 LARGE_CHANGE(흐림)다.
     */
    static final double ANCHORED_MAX_CENTER_M = 0.25;
    static final double ANCHORED_MAX_ROTATION_RAD = Math.toRadians(10);
    /** 재사용 세팅을 거듭해 생긴 world_anchor 사슬을 따라가는 최대 단계 수 */
    static final int MAX_ANCHOR_HOPS = 8;
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
        if (!oldWorld.equals(newWorld) && anchoredToWorldOf(source, a, target)) {
            // 세팅 보정이 World 다시 맞추기(-584)로 Scene 의 World 에 옮겨졌다(이전 모델 재사용 -614 의 정상 경로). Tier 0 는 세팅 보정을
            // 새 계보로 채택하므로 world_frame_id 는 다르지만 좌표계는 같다. 자세 변화는 그 채택 한도에 맞춘 값으로 본다
            return evaluate(out, a.path("camera_poses"), b.path("camera_poses"), ANCHORED_MAX_CENTER_M, ANCHORED_MAX_ROTATION_RAD);
        }
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

    /**
     * target 보정이 world_anchor(accepted, -584)로 source 의 World 에 놓였는지. 기준 보정(reference_calibration_version)이 source 이거나
     * source 와 같은 World 계보(world_frame_id)면 같은 World 다. 기준 보정도 다른 보정에 맞춰졌으면 그 사슬을 {@link #MAX_ANCHOR_HOPS} 까지
     * 따라간다(재사용 세팅을 거듭한 경우). 기록이 없거나 거부면 false(같은 World 라고 지어내지 않는다).
     */
    private boolean anchoredToWorldOf(String source, JsonNode sourceMeta, String target) {
        String sourceWorld = sourceMeta.path("world_frame_id").asString("");
        String cursor = target;
        for (int hop = 0; hop < MAX_ANCHOR_HOPS; hop++) {
            String reference = anchorReference(cursor);
            if (reference == null || reference.equals(cursor)) return false;
            if (reference.equals(source)) return true;
            JsonNode referenceMeta = metadata(reference);
            if (referenceMeta != null && !sourceWorld.isBlank()
                    && sourceWorld.equals(referenceMeta.path("world_frame_id").asString(""))) return true;
            cursor = reference;
        }
        return false;
    }

    /** 보정 문서 world_anchor 가 채택(ANCHORED)이면 그 기준 보정 버전, 아니면 null */
    private String anchorReference(String version) {
        List<byte[]> docs = jdbc.queryForList("SELECT content FROM calibration_document WHERE calibration_version = :v",
                Map.of("v", version), byte[].class);
        if (docs == null || docs.isEmpty()) return null;
        JsonNode anchor = mapper.readTree(docs.getFirst()).path("world_anchor");
        if (!anchor.path("accepted").asBoolean(false) || !"ANCHORED".equals(anchor.path("reason").asString(""))) return null;
        String reference = anchor.path("reference_calibration_version").asString("");
        return reference.isBlank() ? null : reference;
    }

    static ObjectNode evaluate(ObjectNode out, JsonNode oldPoses, JsonNode newPoses) {
        return evaluate(out, oldPoses, newPoses, MAX_CENTER_M, MAX_ROTATION_RAD);
    }

    static ObjectNode evaluate(ObjectNode out, JsonNode oldPoses, JsonNode newPoses, double maxCenterM, double maxRotationRad) {
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
            boolean small = maxDistance <= maxCenterM + 1e-9 && maxAngle <= maxRotationRad + 1e-9;
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
