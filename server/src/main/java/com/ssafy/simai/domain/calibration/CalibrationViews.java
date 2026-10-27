package com.ssafy.simai.domain.calibration;

import java.sql.ResultSet;
import java.sql.SQLException;
import java.time.OffsetDateTime;
import java.util.Map;
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
 * 현재 보정은 Tier 0 가 가장 늦게 알린 calibration_update(sent_at 순)이고, 값은 그 알림 그대로다(버전 행은 처음 만든 값을 둔다).
 * 알림을 받은 적이 없으면 UNCALIBRATED 로 답한다. 설치 안내는 현재 보정 버전의 coverage_guide 행이다(보정 전이면 버전 없는 행).
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
        ObjectNode node = jdbc.query("""
                SELECT m.calibration_state, m.sent_at, m.payload::text AS payload,
                    EXISTS (SELECT 1 FROM calibration_document d WHERE d.calibration_version = m.calibration_version) AS document_uploaded
                FROM calibration_update_message m ORDER BY m.sent_at DESC, m.received_at DESC LIMIT 1
                """, Map.of(), (rs, i) -> view(rs)).stream().findFirst().orElseGet(this::uncalibrated);
        // 알림 전에 올린 문서도 있다고 알린다(화면이 이미 올린 문서를 다시 올리라고 하지 않게)
        node.put("latest_uploaded_version", jdbc.queryForList(
                "SELECT calibration_version FROM calibration_document ORDER BY uploaded_at DESC LIMIT 1", Map.of(), String.class)
                .stream().findFirst().orElse(null));
        return node;
    }

    ObjectNode coverageGuides() {
        String version = jdbc.queryForList("""
                SELECT calibration_version FROM calibration_update_message ORDER BY sent_at DESC, received_at DESC LIMIT 1
                """, Map.of(), String.class).stream().findFirst().orElse(null);
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

    private ObjectNode view(ResultSet rs) throws SQLException {
        JsonNode p = mapper.readTree(rs.getString("payload"));
        ObjectNode node = mapper.createObjectNode();
        node.put("calibration_state", rs.getString("calibration_state"));
        node.set("calibration_version", p.get("calibration_version"));
        node.set("previous_calibration_version", p.get("previous_calibration_version"));
        node.set("provider_source", p.get("provider_source"));
        node.set("reason", p.get("reason"));
        // Tier 0 가 이 버전으로 전환한 시각
        node.set("effective_at", p.get("effective_at"));
        // 갱신 시각: 이 상태를 알린 calibration_update 의 sent_at(Tier 0 시계)
        node.put("updated_at", rs.getObject("sent_at", OffsetDateTime.class).toInstant().toEpochMilli());
        JsonNode cameras = p.get("cameras");
        node.set("cameras", cameras == null ? mapper.createArrayNode() : cameras);
        // 구성 묶음에 넣을 현재 버전의 보정 문서(POST /api/calibrations)가 EC2 에 있는지
        node.put("document_uploaded", rs.getBoolean("document_uploaded"));
        return node;
    }

    private ObjectNode uncalibrated() {
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
    }
}
