package com.ssafy.simai.domain.calibration;

import java.time.OffsetDateTime;
import java.util.HashSet;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 보정 상태(GET /api/calibrations/current)와 설치 안내(GET /api/cameras/coverage-guides)(S15P31A101-263·-522, 명세 4.4·4.5, FR-25·26).
 * 현재 보정은 Tier 0 가 가장 늦게 알린 calibration_update 이고, 값은 그 알림 그대로다(버전 행은 처음 만든 값을 둔다).
 * 순서는 LEAST(sent_at, received_at): 보낸 시각으로 늦게 온 옛 재전송을 거르고, 받은 시각을 상한으로 둬 Tier 0 시계가 한 번 미래로
 * 앞선 알림이 계속 현재로 남지 않게 한다. 구성 묶음도 이 현재 버전의 문서를 싣는다(ConfigBundleService).
 * 현재 버전의 보정 문서(calibration_document)가 있으면 관제 3D 카메라 마커·시야 절두체 표시용 요약(camera_poses)을 함께 내려준다.
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CalibrationViews {

    private static final String LATEST_MESSAGE = """
            SELECT m.calibration_version, m.calibration_state, m.sent_at, m.payload::text AS payload,
                d.content AS document_content
            FROM calibration_update_message m
            LEFT JOIN calibration_document d ON d.calibration_version = m.calibration_version
            ORDER BY LEAST(m.sent_at, m.received_at) DESC, m.received_at DESC LIMIT 1
            """;

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    CalibrationViews(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** Tier 0 가 알린 현재 보정 버전. 알림을 받은 적이 없으면 empty */
    public Optional<String> currentVersion() {
        return latest().map(Latest::version);
    }

    ObjectNode current() {
        ObjectNode node = mapper.createObjectNode();
        Optional<Latest> latest = latest();
        JsonNode p = latest.map(Latest::payload).orElse(mapper.createObjectNode());
        node.put("calibration_state", latest.map(Latest::state).orElse("UNCALIBRATED"));
        // 알림이 없거나 칸이 없으면 null(필드 모양은 늘 같다). effective_at 은 Tier 0 가 이 버전으로 전환한 시각
        for (String field : new String[] {"calibration_version", "previous_calibration_version", "provider_source", "reason",
                "effective_at"}) {
            JsonNode value = p.get(field);
            node.set(field, value == null ? mapper.nullNode() : value);
        }
        // 갱신 시각: 이 상태를 알린 calibration_update 의 sent_at(Tier 0 시계)
        node.put("updated_at", latest.map(Latest::sentAtMs).orElse(null));
        JsonNode cameras = p.get("cameras");
        node.set("cameras", cameras == null ? mapper.createArrayNode() : cameras);
        // 구성 묶음에 넣을 현재 버전의 보정 문서(POST /api/calibrations)가 EC2 에 있는지
        node.put("document_uploaded", latest.map(Latest::documentUploaded).orElse(false));
        // 알림 전에 올린 문서도 있다고 알린다(화면이 이미 올린 문서를 다시 올리라고 하지 않게)
        node.put("latest_uploaded_version", jdbc.queryForList(
                "SELECT calibration_version FROM calibration_document ORDER BY uploaded_at DESC LIMIT 1", Map.of(), String.class)
                .stream().findFirst().orElse(null));
        node.set("camera_poses", latest.map(Latest::documentContent).map(this::cameraPoses).orElseGet(mapper::createArrayNode));
        return node;
    }

    /**
     * 보정 문서(contracts/providers/calibration.schema.json, OPENCV_WORLD_TO_CAMERA: x_cam = R x_world + t)에서
     * 카메라별 camera_id, position_xyz(C = -R^T t), forward_xyz(R 셋째 행 단위 벡터), hfov_rad(2 * atan(W / (2 * fx)))만 뽑는다.
     * 문서가 없거나 읽을 수 없거나 수치가 어긋나면 지어내지 않고 빈 배열을 돌려준다.
     */
    ArrayNode cameraPoses(byte[] content) {
        ArrayNode empty = mapper.createArrayNode();
        if (content == null || content.length == 0) {
            return empty;
        }
        final JsonNode doc;
        try {
            doc = mapper.readTree(content);
        } catch (Exception e) {
            return empty;
        }
        if (doc == null || !doc.isObject()) {
            return empty;
        }
        if (doc.has("extrinsic_convention")
                && !"OPENCV_WORLD_TO_CAMERA".equals(doc.path("extrinsic_convention").asString(""))) {
            return empty;
        }
        if (doc.has("coordinate_frame")) {
            JsonNode cf = doc.path("coordinate_frame");
            if (!"WORLD".equals(cf.path("frame").asString(""))
                    || !"m".equals(cf.path("unit").asString(""))
                    || !"right".equals(cf.path("handedness").asString(""))
                    || !"+Z".equals(cf.path("up_axis").asString(""))) {
                return empty;
            }
        }
        JsonNode cameras = doc.get("cameras");
        if (cameras == null || !cameras.isArray() || cameras.isEmpty()) {
            return empty;
        }
        ArrayNode out = mapper.createArrayNode();
        Set<String> seenIds = new HashSet<>();
        for (JsonNode cam : cameras) {
            if (cam == null || !cam.isObject()) {
                return empty;
            }
            String cameraId = cam.path("camera_id").asString("").strip();
            if (cameraId.isEmpty() || !seenIds.add(cameraId)) {
                return empty;
            }
            double[][] r = mat3(cam.get("R"));
            double[] t = vec3(cam.get("t"));
            double[][] k = mat3(cam.get("K"));
            double[] imageSize = vec2(cam.get("image_size"));
            if (r == null || t == null || k == null || imageSize == null || !isRotation(r)) {
                return empty;
            }
            double width = imageSize[0];
            double height = imageSize[1];
            double fx = k[0][0];
            double fy = k[1][1];
            if (width <= 0.0 || height <= 0.0 || fx <= 0.0 || fy <= 0.0) {
                return empty;
            }
            double hfovRad = 2.0 * Math.atan2(width, 2.0 * fx);
            if (!Double.isFinite(hfovRad) || hfovRad <= 0.0 || hfovRad >= Math.PI) {
                return empty;
            }
            // C = -R^T * t
            double cx = cleanZero(-(r[0][0] * t[0] + r[1][0] * t[1] + r[2][0] * t[2]));
            double cy = cleanZero(-(r[0][1] * t[0] + r[1][1] * t[1] + r[2][1] * t[2]));
            double cz = cleanZero(-(r[0][2] * t[0] + r[1][2] * t[1] + r[2][2] * t[2]));
            if (!Double.isFinite(cx) || !Double.isFinite(cy) || !Double.isFinite(cz)) {
                return empty;
            }
            // OPENCV_WORLD_TO_CAMERA 에서 카메라 +Z(정면)의 World 방향은 R^T [0, 0, 1]^T = R 의 셋째 행(R[2])
            double r20 = r[2][0];
            double r21 = r[2][1];
            double r22 = r[2][2];
            double norm = Math.sqrt(r20 * r20 + r21 * r21 + r22 * r22);
            if (!Double.isFinite(norm) || norm < 1e-6) {
                return empty;
            }
            double fwx = cleanZero(clampUnit(r20 / norm));
            double fwy = cleanZero(clampUnit(r21 / norm));
            double fwz = cleanZero(clampUnit(r22 / norm));

            ObjectNode item = out.addObject();
            item.put("camera_id", cameraId);
            item.putArray("position_xyz").add(cx).add(cy).add(cz);
            item.putArray("forward_xyz").add(fwx).add(fwy).add(fwz);
            item.put("hfov_rad", hfovRad);
        }
        return out;
    }

    private static double cleanZero(double v) {
        return Math.abs(v) < 1e-12 ? 0.0 : v;
    }

    private static double clampUnit(double v) {
        return Math.max(-1.0, Math.min(1.0, v));
    }

    private static double[] vec2(JsonNode n) {
        if (n == null || !n.isArray() || n.size() != 2) {
            return null;
        }
        double a = num(n.get(0));
        double b = num(n.get(1));
        return Double.isFinite(a) && Double.isFinite(b) ? new double[] {a, b} : null;
    }

    private static double[] vec3(JsonNode n) {
        if (n == null || !n.isArray() || n.size() != 3) {
            return null;
        }
        double a = num(n.get(0));
        double b = num(n.get(1));
        double c = num(n.get(2));
        return Double.isFinite(a) && Double.isFinite(b) && Double.isFinite(c) ? new double[] {a, b, c} : null;
    }

    private static double[][] mat3(JsonNode n) {
        if (n == null || !n.isArray() || n.size() != 3) {
            return null;
        }
        double[][] m = new double[3][];
        for (int i = 0; i < 3; i++) {
            m[i] = vec3(n.get(i));
            if (m[i] == null) {
                return null;
            }
        }
        return m;
    }

    private static double num(JsonNode n) {
        return n != null && n.isNumber() ? n.asDouble() : Double.NaN;
    }

    private static boolean isRotation(double[][] r) {
        for (int i = 0; i < 3; i++) {
            for (int j = 0; j < 3; j++) {
                double dot = r[i][0] * r[j][0] + r[i][1] * r[j][1] + r[i][2] * r[j][2];
                if (Math.abs(dot - (i == j ? 1.0 : 0.0)) > 1e-2) {
                    return false;
                }
            }
        }
        double det = r[0][0] * (r[1][1] * r[2][2] - r[1][2] * r[2][1])
                - r[0][1] * (r[1][0] * r[2][2] - r[1][2] * r[2][0])
                + r[0][2] * (r[1][0] * r[2][1] - r[1][1] * r[2][0]);
        return Math.abs(det - 1.0) <= 1e-2;
    }

    public ObjectNode coverageGuides() {
        String version = currentVersion().orElse(null);
        ObjectNode node = mapper.createObjectNode();
        node.put("calibration_version", version);
        ArrayNode guides = node.putArray("guides");
        // IS NOT DISTINCT FROM 은 색인을 쓰지 못해 버전이 있을 때와 없을 때를 나눈다
        String where = version == null ? "calibration_version IS NULL" : "calibration_version = :v";
        jdbc.query("SELECT guide_id, camera_id, message, metrics::text AS metrics, created_at FROM coverage_guide WHERE " + where
                + " ORDER BY camera_id NULLS LAST, guide_id", new MapSqlParameterSource("v", version), rs -> {
            ObjectNode guide = guides.addObject();
            guide.put("guide_id", rs.getLong("guide_id"));
            // null 이면 카메라 하나가 아니라 전체(걷기 경로 안내)다
            guide.put("camera_id", rs.getString("camera_id"));
            guide.put("message", rs.getString("message"));
            String metrics = rs.getString("metrics");
            guide.set("metrics", metrics == null ? null : mapper.readTree(metrics));
            guide.put("created_at", rs.getObject("created_at", OffsetDateTime.class).toInstant().toEpochMilli());
        });
        return node;
    }

    private Optional<Latest> latest() {
        return jdbc.query(LATEST_MESSAGE, Map.of(), (rs, i) -> {
            byte[] content = rs.getBytes("document_content");
            return new Latest(rs.getString("calibration_version"),
                    rs.getString("calibration_state"), rs.getObject("sent_at", OffsetDateTime.class).toInstant().toEpochMilli(),
                    mapper.readTree(rs.getString("payload")), content != null, content);
        }).stream().findFirst();
    }

    private record Latest(String version, String state, long sentAtMs, JsonNode payload, boolean documentUploaded,
            byte[] documentContent) {
    }
}

