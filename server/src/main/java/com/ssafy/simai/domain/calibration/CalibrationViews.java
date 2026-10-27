package com.ssafy.simai.domain.calibration;

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
 * 현재 보정은 Tier 0 가 가장 늦게 알린 calibration_update 이고, 값은 그 알림 그대로다(버전 행은 처음 만든 값을 둔다).
 * 순서는 LEAST(sent_at, received_at): 보낸 시각으로 늦게 온 옛 재전송을 거르고, 받은 시각을 상한으로 둬 Tier 0 시계가 한 번 미래로
 * 앞선 알림이 계속 현재로 남지 않게 한다. 구성 묶음도 이 현재 버전의 문서를 싣는다(ConfigBundleService).
 */
@Repository
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CalibrationViews {

    private static final String LATEST_MESSAGE = """
            SELECT m.calibration_version, m.calibration_state, m.sent_at, m.payload::text AS payload,
                EXISTS (SELECT 1 FROM calibration_document d WHERE d.calibration_version = m.calibration_version) AS document_uploaded
            FROM calibration_update_message m
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
        return node;
    }

    ObjectNode coverageGuides() {
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
        return jdbc.query(LATEST_MESSAGE, Map.of(), (rs, i) -> new Latest(rs.getString("calibration_version"),
                rs.getString("calibration_state"), rs.getObject("sent_at", OffsetDateTime.class).toInstant().toEpochMilli(),
                mapper.readTree(rs.getString("payload")), rs.getBoolean("document_uploaded"))).stream().findFirst();
    }

    private record Latest(String version, String state, long sentAtMs, JsonNode payload, boolean documentUploaded) {
    }
}
