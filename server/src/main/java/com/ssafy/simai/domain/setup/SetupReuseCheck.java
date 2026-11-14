package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.coverage.CoverageGuideService;
import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.domain.scene.SceneCompatibility;
import com.ssafy.simai.domain.scene.SceneService;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.jdbc.core.namedparam.MapSqlParameterSource;
import org.springframework.jdbc.core.namedparam.NamedParameterJdbcTemplate;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 「이전 맵 재사용」을 지금 고를 수 있는지(S15P31A101-689, docs/design/S15P31A101-689-reuse-always-selectable.md).
 * 세팅 화면 1단계의 선택(GET /api/setup-sessions/reuse-check)과 세팅 시작(POST mode=REUSE)이 같은 판단을 쓴다.
 * <ol>
 *   <li>현재 관제 맵(현장당 하나, COMMITTED)과 그 보정 행이 없으면 NO_SCENE.
 *   <li>그 맵이 Tier 0 지금 보정과 다른 World 로 판정되면(Scene 정합 MISMATCH) SCENE_MISMATCH.
 *   <li>직전 보정과 달라진 카메라가 있으면 CAMERA_CHANGED. 근거(앞선 것이 먼저):
 *       <ul>
 *         <li>TIER0_DRIFT: Tier 0 배경 회전 감시(-540)가 지금 재세팅을 요구하는 카메라(viewer_health re_setup 과 같은 값).
 *         <li>VIEW_MATCH(S15P31A101-712): 세팅 1단계 게이트웨이 헬퍼가 이 맵을 구운 카메라별 배경 사진과 지금 키프레임을 같은 카메라끼리
 *             비교해 2회 연속 달라졌다고 본 카메라(coverage_guide 1.1.0 reuse_view_check, 이 맵 기준·viewCheckTtl 안의 결과만).
 *             GPU·Tier 0 없이 배경 촬영 전에 보이고, -540 기준 배경이 Pod 를 켠 뒤 처음 본 화면이라 못 잡는 "다른 날 옮긴 카메라"를 잡는다.
 *         <li>LAST_SETUP_REJECTED: 이 현장의 가장 최근 세팅 시도가 재사용 World 다시 맞추기 거부(REUSE_WORLD_REJECTED, 카메라 이동 사유)로
 *             멈췄다. 그 뒤 새 세팅이 없으므로 같은 비교를 다시 해도 같은 결과다.
 *       </ul>
 * </ol>
 * Tier 0 가 연결돼 달라진 카메라가 없거나 시점 확인이 4대 모두 같다고 보면 NOT_DETECTED,
 * 카메라 근거가 없으면(Tier 0 끊김·옛 Tier 0·헬퍼 결과 없음) camera_status=UNKNOWN 으로 두고 막지 않는다. 워커의 World 다시 맞추기가 마지막으로 확인하고,
 * 거부되면 새 맵으로 넘어가지 않고 실패로 멈춘다.
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupReuseCheck {

    static final String NO_SCENE = "NO_SCENE";
    static final String SCENE_MISMATCH = "SCENE_MISMATCH";
    static final String CAMERA_CHANGED = "CAMERA_CHANGED";
    static final String CHANGED = "CHANGED";
    static final String NOT_DETECTED = "NOT_DETECTED";
    static final String UNKNOWN = "UNKNOWN";
    static final String TIER0_DRIFT = "TIER0_DRIFT";
    static final String LAST_SETUP_REJECTED = "LAST_SETUP_REJECTED";
    static final String VIEW_MATCH = "VIEW_MATCH";

    private final NamedParameterJdbcTemplate jdbc;
    private final ObjectMapper mapper;
    private final SceneCompatibility compatibility;
    private final ViewerHealthService health;
    private final CoverageGuideService coverageGuides;

    SetupReuseCheck(NamedParameterJdbcTemplate jdbc, ObjectMapper mapper, SceneCompatibility compatibility,
            ViewerHealthService health, CoverageGuideService coverageGuides) {
        this.jdbc = jdbc;
        this.mapper = mapper;
        this.compatibility = compatibility;
        this.health = health;
        this.coverageGuides = coverageGuides;
    }

    /**
     * @param blockedReason  NO_SCENE | SCENE_MISMATCH | CAMERA_CHANGED, 고를 수 있으면 null
     * @param cameraStatus   CHANGED | NOT_DETECTED(Tier 0 감시가 달라진 카메라를 보지 못함) | UNKNOWN(근거 없음)
     * @param changedCameras 달라진 카메라(오름차순). 카메라를 짚을 수 없으면 빈 목록
     * @param cameraSource   TIER0_DRIFT | VIEW_MATCH | LAST_SETUP_REJECTED, 근거가 없으면 null
     */
    record Result(String blockedReason, String sceneVersion, String calibrationVersion, String cameraStatus,
            List<String> changedCameras, String cameraSource) {
        boolean available() {
            return blockedReason == null;
        }
    }

    Result check(String siteId) {
        List<String[]> current = jdbc.query("SELECT s.scene_version, s.calibration_version FROM ("
                + SceneService.CURRENT_COMMITTED + """
                ) s JOIN calibration_version c ON c.calibration_version = s.calibration_version AND c.site_id = s.site_id
                WHERE s.site_id = :site
                LIMIT 1
                """, new MapSqlParameterSource("site", siteId),
                (rs, i) -> new String[] {rs.getString("scene_version"), rs.getString("calibration_version")});
        String scene = current.isEmpty() ? null : current.getFirst()[0];
        String calibration = current.isEmpty() ? null : current.getFirst()[1];
        Camera camera = camera(siteId, scene);
        String blocked;
        if (scene == null) {
            blocked = NO_SCENE;
        } else if ("MISMATCH".equals(compatibility.current(scene, calibration).path("verification").asString(""))) {
            blocked = SCENE_MISMATCH;
        } else if (CHANGED.equals(camera.status())) {
            blocked = CAMERA_CHANGED;
        } else {
            blocked = null;
        }
        return new Result(blocked, scene, calibration, camera.status(), camera.cameras(), camera.source());
    }

    ObjectNode toJson(Result r) {
        ObjectNode out = mapper.createObjectNode();
        out.put("available", r.available());
        out.put("blocked_reason", r.blockedReason());
        out.put("scene_version", r.sceneVersion());
        out.put("calibration_version", r.calibrationVersion());
        out.put("camera_status", r.cameraStatus());
        ArrayNode cams = out.putArray("changed_cameras");
        r.changedCameras().forEach(cams::add);
        out.put("camera_source", r.cameraSource());
        return out;
    }

    private record Camera(String status, List<String> cameras, String source) {
    }

    private Camera camera(String siteId, String scene) {
        List<String> drifted = health.reSetupCameraIds();
        if (drifted != null && !drifted.isEmpty()) {
            return new Camera(CHANGED, List.copyOf(drifted), TIER0_DRIFT);
        }
        Optional<CoverageGuideService.ViewCheck> view = scene == null || coverageGuides == null
                ? Optional.empty() : coverageGuides.reuseViewCheck(scene);
        if (view.isPresent() && !view.get().changedCameras().isEmpty()) {
            return new Camera(CHANGED, view.get().changedCameras(), VIEW_MATCH);
        }
        List<String> rejected = lastRejectedCameras(siteId);
        if (rejected != null) {
            return new Camera(CHANGED, rejected, LAST_SETUP_REJECTED);
        }
        boolean seen = drifted != null || view.map(CoverageGuideService.ViewCheck::allSame).orElse(false);
        return new Camera(seen ? NOT_DETECTED : UNKNOWN, List.of(), null);
    }

    /** 가장 최근 세팅 시도가 카메라 이동 사유의 재사용 거부로 멈췄으면 그 달라진 카메라(없으면 빈 목록), 아니면 null */
    private List<String> lastRejectedCameras(String siteId) {
        List<Map<String, Object>> rows = jdbc.queryForList("""
                SELECT status, failure_code, world_anchor::text AS world_anchor FROM setup_session
                WHERE site_id = :site ORDER BY started_at DESC LIMIT 1
                """, new MapSqlParameterSource("site", siteId));
        if (rows.isEmpty()) {
            return null;
        }
        Map<String, Object> row = rows.getFirst();
        String rawAnchor = (String) row.get("world_anchor");
        if (!"FAILED".equals(row.get("status")) || !SetupReuseMode.WORLD_REJECTED_FAILURE.equals(row.get("failure_code"))
                || rawAnchor == null) {
            return null;
        }
        JsonNode anchor = mapper.readTree(rawAnchor);
        if (anchor.path("accepted").asBoolean(false) || !SetupReuseMode.MOVED_REASONS.contains(anchor.path("reason").asString(""))) {
            return null;
        }
        List<String> cameras = new ArrayList<>();
        anchor.path("changed_cameras").forEach(c -> {
            if (c.isString()) {
                cameras.add(c.asString());
            }
        });
        return cameras.stream().sorted().toList();
    }
}
