package com.ssafy.simai.domain.coverage;

import com.ssafy.simai.domain.scene.SceneCameraImageService;
import com.ssafy.simai.domain.scene.SceneService;
import java.sql.Array;
import java.sql.SQLException;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 재사용 시점 확인 기준(S15P31A101-712, docs/design/S15P31A101-712-reuse-camera-mismatch.md).
 * 「이전 맵 재사용」 대상(현재 관제 맵 COMMITTED 와 그 보정, {@code SetupReuseCheck} 와 같은 질의)을 구운 카메라별 사람 지운 배경 사진의
 * GET 서명 주소({@link SceneCameraImageService}, 뷰어 VDT 와 같은 사진)와 그 보정의 렌즈 내부값(calibration_camera 의 K·왜곡·image_size)을
 * 기기 수요 응답 {@code reuse_reference} 로 준다. 게이트웨이 헬퍼가 같은 카메라의 지금 키프레임과 비교해 coverage_guide 1.1.0
 * {@code reuse_view_check} 로 돌려준다. 외부 자세(R·t)는 주지 않는다(헬퍼는 위치·보정을 만들지 않는다).
 * 맵·사진·렌즈 중 하나라도 없으면 null(지어내지 않는다, 규칙 2). 서명 주소는 로그에 남기지 않는다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class CoverageReuseReference {

    private static final Logger log = LoggerFactory.getLogger(CoverageReuseReference.class);

    private final NamedParameterJdbcTemplate jdbc;
    private final SceneCameraImageService images;
    private final ObjectMapper mapper;

    public CoverageReuseReference(NamedParameterJdbcTemplate jdbc, SceneCameraImageService images, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.images = images;
        this.mapper = mapper;
    }

    /** 현재 관제 맵의 기준 사진·렌즈. 없으면 null */
    public ObjectNode current(String siteId) {
        try {
            return build(siteId);
        } catch (RuntimeException e) {
            log.warn("재사용 시점 확인 기준을 만들지 못해 싣지 않는다: {}", e.getClass().getSimpleName());
            return null;
        }
    }

    private ObjectNode build(String siteId) {
        List<String[]> current = jdbc.query("SELECT s.scene_version, s.calibration_version FROM ("
                + SceneService.CURRENT_COMMITTED + """
                ) s JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                WHERE s.site_id = :site
                LIMIT 1
                """, new MapSqlParameterSource("site", siteId),
                (rs, i) -> new String[] {rs.getString("scene_version"), rs.getString("calibration_version")});
        if (current.isEmpty()) {
            return null;
        }
        String scene = current.getFirst()[0];
        String calibration = current.getFirst()[1];
        ObjectNode signed = images.images(scene);
        Map<String, String> urls = new LinkedHashMap<>();
        for (JsonNode image : signed.path("images")) {
            urls.putIfAbsent(image.path("camera_id").asString(""), image.path("url").asString(""));
        }
        if (urls.isEmpty() || signed.path("expires_at_ms").isNull()) {
            return null;
        }
        Map<String, ObjectNode> lenses = new LinkedHashMap<>();
        jdbc.query("""
                SELECT camera_id, image_size, k_matrix::text AS k_matrix, distortion_model, distortion_coeffs
                FROM calibration_camera WHERE calibration_version = :c ORDER BY camera_id
                """, new MapSqlParameterSource("c", calibration), rs -> {
            ObjectNode lens = mapper.createObjectNode();
            ArrayNode size = lens.putArray("image_size");
            for (Object v : values(rs.getArray("image_size"))) {
                size.add(((Number) v).intValue());
            }
            lens.set("K", mapper.readTree(rs.getString("k_matrix")));
            ObjectNode distortion = lens.putObject("distortion");
            distortion.put("model", rs.getString("distortion_model"));
            ArrayNode coeffs = distortion.putArray("coeffs");
            for (Object v : values(rs.getArray("distortion_coeffs"))) {
                coeffs.add(((Number) v).doubleValue());
            }
            lenses.put(rs.getString("camera_id"), lens);
        });
        ObjectNode out = mapper.createObjectNode();
        out.put("scene_version", scene);
        out.put("calibration_version", calibration);
        out.put("expires_at_ms", signed.path("expires_at_ms").asLong());
        ArrayNode cameras = out.putArray("cameras");
        urls.forEach((camera, url) -> {
            ObjectNode lens = lenses.get(camera);
            if (lens == null || url.isEmpty()) {
                return;
            }
            ObjectNode item = cameras.addObject();
            item.put("camera_id", camera);
            item.put("url", url);
            item.setAll(lens);
        });
        return cameras.isEmpty() ? null : out;
    }

    private static Object[] values(Array array) throws SQLException {
        return array == null ? new Object[0] : (Object[]) array.getArray();
    }
}
