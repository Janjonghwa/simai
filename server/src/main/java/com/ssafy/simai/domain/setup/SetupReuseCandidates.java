package com.ssafy.simai.domain.setup;

import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;

/**
 * 「이전 맵 재사용」 후보 맵 묶음(S15P31A101-714, docs/design/S15P31A101-714-reuse-chosen-map.md).
 * <p>묶음 = 새 맵(NEW) 세팅 세션 하나가 만든 정밀 맵(scene_version)·보정(calibration_version)·카메라 높이(camera_heights_m)와, 그 맵으로 마지막에
 * 발행한 구역이다. 재사용은 이 묶음을 그대로 쓴다(보정을 다시 계산하지 않는다).
 * <p>최근 맵부터 {@link #MAX_LISTED} 개를 보이고, DB 에 남은 사실로만 고를 수 없는 맵을 가린다(지어내지 않는다). 순서가 앞선 이유 하나를 싣는다:
 * <ul>
 *   <li>{@link #CAMERA_CHECK_NOT_PASSED}: 그 세팅의 카메라 확인(-646·-687) 기록이 있고 그 보정으로 통과하지 못했다(15차 11-13 처럼 확인 마감 INCOMPLETE).
 *       기록이 없는 옛 세팅(카메라 확인 도입 전)은 문제 사실이 없으므로 가리지 않는다.
 *   <li>{@link #NOT_COMPLETED}: 세팅 완료(C17, work_unit.setup_completed_at)를 이 세션으로 하지 않았다. 사람이 구역을 확인해 관제에 올린 맵이 아니다
 *       (미리보기·정밀 맵 자동 현재화만으로 관제 맵이 된 경우 포함).
 *   <li>{@link #MAP_RETIRED}: 그 맵이 더는 COMMITTED 가 아니다.
 *   <li>{@link #NO_CALIBRATION}: 그 보정의 원문(calibration_document)이 없다(구성 묶음에 실을 수 없다).
 *   <li>{@link #CAMERAS_CHANGED}: 그때 높이를 적은 카메라 집합이 지금 현장 카메라와 다르다(카메라를 더하거나 뺐다).
 * </ul>
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
public class SetupReuseCandidates {

    /** 화면에 보이는 최근 맵 수(가린 맵 포함). 하루 한 번 새 맵이면 한 주 남짓 */
    static final int MAX_LISTED = 8;
    /** 1단계 시점 확인에 기준 사진을 주는 후보 수(coverage_guide 1.2.0 reuse_view_checks maxItems) */
    public static final int MAX_REFERENCES = 5;

    static final String CAMERA_CHECK_NOT_PASSED = "CAMERA_CHECK_NOT_PASSED";
    static final String NOT_COMPLETED = "NOT_COMPLETED";
    static final String MAP_RETIRED = "MAP_RETIRED";
    static final String NO_CALIBRATION = "NO_CALIBRATION";
    static final String CAMERAS_CHANGED = "CAMERAS_CHANGED";

    /**
     * @param completedAt   세팅 완료 시각(완료 기록이 없으면 세팅 작업이 끝난 시각). 화면의 맵 날짜
     * @param excludedReason 고를 수 없는 이유, 고를 수 있으면 null
     */
    public record Candidate(UUID setupSessionId, String sceneVersion, String calibrationVersion, Instant completedAt,
            Map<String, Double> cameraHeights, String excludedReason) {
        public boolean eligible() {
            return excludedReason == null;
        }
    }

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;

    public SetupReuseCandidates(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper) {
        this.jdbc = jdbc;
        this.mapper = mapper;
    }

    /** 최근 맵부터(세팅 완료 시각 내림차순) 최대 {@link #MAX_LISTED} 개 */
    public List<Candidate> list(String siteId) {
        TreeSet<String> activeCameras = new TreeSet<>(jdbc.queryForList(
                "SELECT camera_id FROM camera WHERE site_id = :site AND removed_at IS NULL",
                new MapSqlParameterSource("site", siteId), String.class));
        return jdbc.query("""
                WITH maps AS (
                    SELECT s.setup_session_id, s.scene_version, s.calibration_version, s.camera_heights_m::text AS heights,
                           s.camera_check ->> 'status' AS check_status,
                           s.camera_check ->> 'calibration_version' AS check_calibration,
                           (SELECT max(w.setup_completed_at) FROM work_unit w
                            WHERE w.active_setup_session_id = s.setup_session_id) AS setup_completed_at,
                           COALESCE(s.completed_at, s.updated_at) AS finished_at,
                           sv.state AS scene_state,
                           EXISTS (SELECT 1 FROM calibration_document d
                                   WHERE d.calibration_version = s.calibration_version) AS has_document
                    FROM setup_session s
                    LEFT JOIN scene_version sv ON sv.scene_version = s.scene_version
                    WHERE s.site_id = :site AND s.mode = 'NEW' AND s.status = 'COMPLETED'
                      AND s.scene_version IS NOT NULL AND s.calibration_version IS NOT NULL
                )
                SELECT * FROM maps
                ORDER BY COALESCE(setup_completed_at, finished_at) DESC, setup_session_id
                LIMIT :limit
                """, new MapSqlParameterSource("site", siteId).addValue("limit", MAX_LISTED), (rs, i) -> {
            Timestamp completed = rs.getTimestamp("setup_completed_at");
            Timestamp finished = rs.getTimestamp("finished_at");
            Map<String, Double> heights = heights(rs.getString("heights"));
            String calibration = rs.getString("calibration_version");
            String checkStatus = rs.getString("check_status");
            String excluded;
            if (checkStatus != null && !SetupCameraCheck.passed(checkStatus, rs.getString("check_calibration"), calibration)) {
                excluded = CAMERA_CHECK_NOT_PASSED;
            } else if (completed == null) {
                excluded = NOT_COMPLETED;
            } else if (!"COMMITTED".equals(rs.getString("scene_state"))) {
                excluded = MAP_RETIRED;
            } else if (!rs.getBoolean("has_document")) {
                excluded = NO_CALIBRATION;
            } else if (!new TreeSet<>(heights.keySet()).equals(activeCameras)) {
                excluded = CAMERAS_CHANGED;
            } else {
                excluded = null;
            }
            return new Candidate(rs.getObject("setup_session_id", UUID.class), rs.getString("scene_version"), calibration,
                    (completed != null ? completed : finished).toInstant(), heights, excluded);
        });
    }

    /**
     * 이 맵이 쓰는 보정 버전들: 그 맵을 만든 보정과, 그 맵을 재사용한 세션이 쓴 보정(Tier 0 가 다른 보정을 쓰고 있을 때 같은 내용으로 새로 낸
     * 버전, {@link SetupReuseCalibration}). 그 맵으로 발행한 구역을 찾고(마지막 묶음) Tier 0 가 지금 그 맵의 보정을 쓰는지 볼 때 쓴다.
     */
    public List<String> calibrationVersionsOf(UUID sourceSessionId, String calibrationVersion) {
        List<String> out = new ArrayList<>();
        out.add(calibrationVersion);
        jdbc.queryForList("""
                SELECT DISTINCT calibration_version FROM setup_session
                WHERE reuse_source_session_id = :source AND calibration_version IS NOT NULL
                """, new MapSqlParameterSource("source", sourceSessionId), String.class).forEach(v -> {
            if (!out.contains(v)) {
                out.add(v);
            }
        });
        return out;
    }

    private Map<String, Double> heights(String raw) {
        Map<String, Double> out = new LinkedHashMap<>();
        if (raw == null) {
            return out;
        }
        JsonNode node = mapper.readTree(raw);
        node.properties().forEach(e -> {
            if (e.getValue().isNumber()) {
                out.put(e.getKey(), e.getValue().doubleValue());
            }
        });
        return out;
    }
}
