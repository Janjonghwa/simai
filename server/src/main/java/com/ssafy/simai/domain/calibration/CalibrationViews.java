package com.ssafy.simai.domain.calibration;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.Map;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Repository;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 보정 상태(GET /api/calibrations/current)와 설치 안내(GET /api/cameras/coverage-guides)(S15P31A101-263, 명세 4.4·4.5, FR-25·26).
 * 현재 보정은 Tier 0 가 가장 늦게 알린 calibration_update(sent_at 순)다. 알림을 받은 적이 없으면 UNCALIBRATED 로 답한다.
 * 설치 안내는 현재 보정 버전의 coverage_guide 행이다(보정 전이면 버전 없는 행).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class CalibrationViews {

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    CalibrationViews(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    ObjectNode current() {
        return latest().orElseGet(() -> {
            ObjectNode node = mapper.createObjectNode();
            node.put("calibration_state", "UNCALIBRATED");
            node.putNull("calibration_version");
            node.putNull("previous_calibration_version");
            node.putNull("provider_source");
            node.putNull("reason");
            node.putNull("effective_at");
            node.putNull("updated_at");
            node.putArray("cameras");
            node.put("document_uploaded", false);
            return node;
        });
    }

    ObjectNode coverageGuides() {
        String version = latest().map(n -> n.get("calibration_version").asString()).orElse(null);
        ObjectNode node = mapper.createObjectNode();
        node.put("calibration_version", version);
        ArrayNode guides = node.putArray("guides");
        jdbc.query("""
                SELECT guide_id, camera_id, message, metrics::text AS metrics, created_at FROM coverage_guide
                WHERE calibration_version IS NOT DISTINCT FROM :v ORDER BY camera_id NULLS LAST, guide_id
                """, new MapSqlParameterSource("v", version), rs -> {
            ObjectNode guide = guides.addObject();
            guide.put("guide_id", rs.getLong("guide_id"));
            // null 이면 카메라 하나가 아니라 전체(걷기 경로 안내)다
            guide.put("camera_id", rs.getString("camera_id"));
            guide.put("message", rs.getString("message"));
            String metrics = rs.getString("metrics");
            guide.set("metrics", metrics == null ? null : mapper.readTree(metrics));
            guide.put("created_at", millis(rs, "created_at"));
        });
        return node;
    }

    private Optional<ObjectNode> latest() {
        return jdbc.query("""
                SELECT m.calibration_version, m.calibration_state, m.reason, m.sent_at, m.payload::text AS payload,
                    v.provider_source, v.effective_at,
                    EXISTS (SELECT 1 FROM calibration_document d WHERE d.calibration_version = m.calibration_version) AS document_uploaded
                FROM calibration_update_message m JOIN calibration_version v ON v.calibration_version = m.calibration_version
                ORDER BY m.sent_at DESC, m.received_at DESC LIMIT 1
                """, Map.of(), (rs, i) -> view(rs)).stream().findFirst();
    }

    private ObjectNode view(ResultSet rs) throws SQLException {
        JsonNode payload = mapper.readTree(rs.getString("payload"));
        ObjectNode node = mapper.createObjectNode();
        node.put("calibration_state", rs.getString("calibration_state"));
        node.put("calibration_version", rs.getString("calibration_version"));
        // 버전 행은 EC2 가 모르는 이전 버전을 비워 두므로 Tier 0 가 보낸 값을 그대로 준다
        node.set("previous_calibration_version", payload.get("previous_calibration_version"));
        node.put("provider_source", rs.getString("provider_source"));
        node.put("reason", rs.getString("reason"));
        node.put("effective_at", millis(rs, "effective_at"));
        // 갱신 시각: 이 상태를 알린 calibration_update 의 sent_at(Tier 0 시계)
        node.put("updated_at", millis(rs, "sent_at"));
        JsonNode cameras = payload.get("cameras");
        node.set("cameras", cameras == null ? mapper.createArrayNode() : cameras);
        // 구성 묶음에 넣을 보정 문서(POST /api/calibrations)가 EC2 에 있는지
        node.put("document_uploaded", rs.getBoolean("document_uploaded"));
        return node;
    }

    private static long millis(ResultSet rs, String column) throws SQLException {
        return rs.getObject(column, OffsetDateTime.class).toInstant().toEpochMilli();
    }
}
