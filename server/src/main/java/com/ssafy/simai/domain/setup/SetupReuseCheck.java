package com.ssafy.simai.domain.setup;

import com.ssafy.simai.domain.calibration.CalibrationViews;
import com.ssafy.simai.domain.coverage.CoverageGuideService;
import com.ssafy.simai.domain.health.ViewerHealthService;
import com.ssafy.simai.global.exception.BusinessException;
import com.ssafy.simai.global.exception.ErrorCode;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.UUID;
import org.springframework.boot.autoconfigure.condition.ConditionalOnWebApplication;
import org.springframework.stereotype.Component;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.ObjectNode;

/**
 * 「이전 맵 재사용」에서 어떤 맵을 고를 수 있는지(S15P31A101-714, docs/design/S15P31A101-714-reuse-chosen-map.md; 앞선 -689·-712).
 * 세팅 화면 1단계의 맵 목록(GET /api/setup-sessions/reuse-check)과 세팅 시작(POST mode=REUSE)이 같은 판단을 쓴다.
 * <p>재사용은 고른 맵의 묶음(보정·카메라 높이·구역·Scene)을 그대로 쓰고 보정을 다시 계산하지 않으므로, 「카메라가 그 맵을 만들 때와 같은 자리인가」만
 * 본다. 근거는 1단계 게이트웨이 헬퍼가 후보 맵마다 그 맵의 카메라 사진과 지금 키프레임을 같은 카메라끼리 비교한 결과(coverage_guide 1.2.0
 * reuse_view_checks, -712 방법)다. 후보마다:
 * <ul>
 *   <li>{@link #EXCLUDED}: DB 사실로 고를 수 없는 맵({@link SetupReuseCandidates} 의 이유).
 *   <li>{@link #CHANGED}: 2회 연속 달라짐이 확인된 카메라가 있다(VIEW_MATCH), 또는 Tier 0 가 지금 이 맵의 보정을 쓰는데 배경 회전 감시(-540)가
 *       재세팅을 요구한다(TIER0_DRIFT).
 *   <li>{@link #SHIFTED}: 4대 모두 같은 시점이지만 순수 회전이 {@link #AS_IS_MAX_ROTATION_RAD} 를 넘은 카메라가 있다. -712 의 10° 한도는 보정을 다시
 *       맞추던 때의 한도이고, 보정을 그대로 쓰면 1° 회전도 8 m 앞에서 약 14 cm 어긋난다. 미세 보정 단계를 두지 않으므로(근거 없음) 막는다.
 *   <li>{@link #MATCH}: 4대 모두 같은 시점이고 회전이 한도 안이다. 이것만 고를 수 있다(그대로 사용).
 *   <li>{@link #UNCONFIRMED}: 결과가 없거나 일부 카메라를 아직 모른다(가림·확인 중·영상 없음·사진 없음). 보정을 다시 계산하지 않으므로 모르면 쓰지 않는다.
 * </ul>
 * 순위: MATCH(대응 수 합이 많은 순 = 지금 화면과 가장 잘 맞는 맵, 같으면 최근) → UNCONFIRMED → SHIFTED → CHANGED → EXCLUDED(각각 최근 순).
 * 추천(기본 선택)은 첫 MATCH 다. 맞는 맵이 없으면 재사용을 끄고 이유를 보인다(조용히 새 맵으로 바꾸지 않는다).
 */
@Component
@ConditionalOnWebApplication(type = ConditionalOnWebApplication.Type.SERVLET)
class SetupReuseCheck {

    static final String NO_SCENE = "NO_SCENE";
    static final String CAMERA_CHANGED = "CAMERA_CHANGED";
    static final String VIEW_UNCONFIRMED = "VIEW_UNCONFIRMED";
    static final String NOT_DETECTED = "NOT_DETECTED";
    static final String UNKNOWN = "UNKNOWN";
    static final String TIER0_DRIFT = "TIER0_DRIFT";
    static final String VIEW_MATCH = "VIEW_MATCH";

    static final String MATCH = "MATCH";
    static final String CHANGED = "CHANGED";
    static final String SHIFTED = "SHIFTED";
    static final String UNCONFIRMED = "UNCONFIRMED";
    static final String EXCLUDED = "EXCLUDED";
    /** UNCONFIRMED 이유: 헬퍼 결과 없음 / 그 맵 사진·렌즈 없음 / 일부 카메라 확인 중 */
    static final String NO_RESULT = "NO_RESULT";
    static final String NO_PHOTOS = "NO_PHOTOS";
    static final String CHECKING = "CHECKING";

    /**
     * 「그대로 사용」의 순수 회전 한도 0.01 rad(≈0.57°). 헬퍼는 분석 해상도(긴 변 960 px, 로비 광각 초점 ≈ 400 px)에서 대응의 절반 이상이 3 px 안에
     * 있으면 회전 0 으로 본다(≈0.0075 rad). 그 경계 바로 위 추정 잡음을 넘길 만큼만 둔다. 이 각도의 시선 어긋남은 8 m 앞에서 약 8 cm 로
     * -584 배치 잔차 한도 15 cm 안이다.
     */
    static final double AS_IS_MAX_ROTATION_RAD = 0.01;

    private static final List<String> STATUS_ORDER = List.of(MATCH, UNCONFIRMED, SHIFTED, CHANGED, EXCLUDED);

    private final ObjectMapper mapper;
    private final SetupReuseCandidates candidates;
    private final ViewerHealthService health;
    private final CoverageGuideService coverageGuides;
    private final CalibrationViews calibrations;

    SetupReuseCheck(ObjectMapper mapper, SetupReuseCandidates candidates, ViewerHealthService health,
            CoverageGuideService coverageGuides, CalibrationViews calibrations) {
        this.mapper = mapper;
        this.candidates = candidates;
        this.health = health;
        this.coverageGuides = coverageGuides;
        this.calibrations = calibrations;
    }

    /**
     * 후보 하나의 판정.
     *
     * @param reason       EXCLUDED: 후보 제외 이유, UNCONFIRMED: NO_RESULT|NO_PHOTOS|CHECKING, 그 밖 null
     * @param cameras      카메라별 시점 확인(view_check_item 모양, 결과가 없으면 빈 배열)
     * @param score        MATCH 순위 점수(카메라별 대응 수 합)
     * @param source       CHANGED 근거 VIEW_MATCH|TIER0_DRIFT, 그 밖 null
     * @param viewSnapshot 시작 때 남길 시점 확인({received_at_ms, scene_version, cameras}), 결과가 없으면 null
     */
    record Evaluated(SetupReuseCandidates.Candidate candidate, String status, String reason, List<String> changedCameras,
            List<String> shiftedCameras, List<String> unconfirmedCameras, ArrayNode cameras, long score, String source,
            ObjectNode viewSnapshot) {
        boolean selectable() {
            return MATCH.equals(status);
        }
    }

    /** ranked: 순위 순 후보, recommended: 첫 MATCH(없으면 null) */
    record Result(List<Evaluated> ranked, Evaluated recommended) {
        boolean available() {
            return recommended != null;
        }

        /** 맞는 맵이 없을 때 막는 이유(있으면 null): 후보 없음 NO_SCENE, 확인 중인 후보가 있으면 VIEW_UNCONFIRMED, 모두 어긋남 CAMERA_CHANGED */
        String blockedReason() {
            if (recommended != null) {
                return null;
            }
            List<Evaluated> eligible = ranked.stream().filter(e -> !EXCLUDED.equals(e.status())).toList();
            if (eligible.isEmpty()) {
                return NO_SCENE;
            }
            return eligible.stream().anyMatch(e -> UNCONFIRMED.equals(e.status())) ? VIEW_UNCONFIRMED : CAMERA_CHANGED;
        }

        /** 옛 단일 필드(scene_version·camera_status 등)에 쓸 대표 후보: 추천, 없으면 순위 첫 후보(제외 아님) */
        Evaluated top() {
            if (recommended != null) {
                return recommended;
            }
            return ranked.stream().filter(e -> !EXCLUDED.equals(e.status())).findFirst().orElse(null);
        }
    }

    Result check(String siteId) {
        List<SetupReuseCandidates.Candidate> listed = candidates.list(siteId);
        String tier0Calibration = calibrations.currentVersion().orElse(null);
        List<String> drifted = health.reSetupCameraIds();
        List<Evaluated> ranked = new ArrayList<>();
        for (SetupReuseCandidates.Candidate c : listed) {
            ranked.add(evaluate(c, tier0Calibration, drifted));
        }
        // 안정 정렬: 같은 상태 안에서는 최근 순(목록 순서)을 둔다. MATCH 만 대응 수 합이 많은 순
        ranked.sort(Comparator.comparingInt((Evaluated e) -> STATUS_ORDER.indexOf(e.status()))
                .thenComparingLong(e -> MATCH.equals(e.status()) ? -e.score() : 0L));
        Evaluated recommended = ranked.isEmpty() || !ranked.getFirst().selectable() ? null : ranked.getFirst();
        return new Result(List.copyOf(ranked), recommended);
    }

    /**
     * 세팅 시작이 쓸 후보: 고른 맵(없으면 추천)이 MATCH 여야 한다. 아니면 409 —
     * 후보 없음 SETUP_REUSE_SCENE_MISSING, 고른 맵이 목록에 없거나 제외 SETUP_REUSE_MAP_UNAVAILABLE, 어긋남 SETUP_REUSE_CAMERA_CHANGED,
     * 아직 모름 SETUP_REUSE_VIEW_UNCONFIRMED.
     */
    Evaluated require(String siteId, UUID chosen) {
        return require(siteId, chosen, false);
    }

    /**
     * open: 시연 계정({@link SetupDemoAccounts}). 카메라 시점 확인과 추천 없이 제외(EXCLUDED: 보정 없음·완료 전 등 DB 사실로 쓸 수 없는 맵)가 아닌
     * 맵을 모두 고를 수 있다. 고른 맵이 없으면 가장 최근에 완료된 맵을 쓴다. 쓸 맵이 없으면 SETUP_REUSE_SCENE_MISSING, 고른 맵이 목록에 없거나
     * 제외면 SETUP_REUSE_MAP_UNAVAILABLE. open 이 아니면 위 판정 그대로다.
     */
    Evaluated require(String siteId, UUID chosen, boolean open) {
        Result result = check(siteId);
        if (open) {
            List<Evaluated> usable = result.ranked().stream().filter(e -> !EXCLUDED.equals(e.status())).toList();
            Evaluated picked = chosen == null
                    ? usable.stream().max(Comparator.comparing((Evaluated e) -> e.candidate().completedAt()))
                            .orElseThrow(() -> new BusinessException(ErrorCode.Setup.SETUP_REUSE_SCENE_MISSING))
                    : usable.stream().filter(e -> chosen.equals(e.candidate().setupSessionId())).findFirst()
                            .orElseThrow(() -> new BusinessException(ErrorCode.Setup.SETUP_REUSE_MAP_UNAVAILABLE));
            return withDemoSnapshot(picked);
        }
        if (chosen == null) {
            if (result.recommended() != null) {
                return result.recommended();
            }
            throw new BusinessException(switch (result.blockedReason()) {
                case NO_SCENE -> ErrorCode.Setup.SETUP_REUSE_SCENE_MISSING;
                case VIEW_UNCONFIRMED -> ErrorCode.Setup.SETUP_REUSE_VIEW_UNCONFIRMED;
                default -> ErrorCode.Setup.SETUP_REUSE_CAMERA_CHANGED;
            });
        }
        Evaluated picked = result.ranked().stream().filter(e -> chosen.equals(e.candidate().setupSessionId()))
                .findFirst().orElse(null);
        if (picked == null || EXCLUDED.equals(picked.status())) {
            throw new BusinessException(ErrorCode.Setup.SETUP_REUSE_MAP_UNAVAILABLE);
        }
        return switch (picked.status()) {
            case MATCH -> picked;
            case UNCONFIRMED -> throw new BusinessException(ErrorCode.Setup.SETUP_REUSE_VIEW_UNCONFIRMED);
            default -> throw new BusinessException(ErrorCode.Setup.SETUP_REUSE_CAMERA_CHANGED);
        };
    }

    /**
     * 시연 계정이 고른 맵의 시작 기록(setup_session.reuse_view_check, 객체여야 한다 — V78). 시점 확인 결과가 없으면(확인 중·Tier 0 회전 감시)
     * 받은 시각 없이 카메라별 결과를 비워 둔다. 어느 쪽이든 시점 확인을 통과하지 않고 시연 계정이 골랐다는 표시(demo_account=true)를 남긴다.
     */
    private Evaluated withDemoSnapshot(Evaluated e) {
        ObjectNode snapshot;
        if (e.viewSnapshot() != null) {
            snapshot = e.viewSnapshot().deepCopy();
        } else {
            snapshot = mapper.createObjectNode();
            snapshot.putNull("received_at_ms");
            snapshot.put("scene_version", e.candidate().sceneVersion());
            snapshot.set("cameras", e.cameras().deepCopy());
        }
        snapshot.put("demo_account", true);
        return new Evaluated(e.candidate(), e.status(), e.reason(), e.changedCameras(), e.shiftedCameras(),
                e.unconfirmedCameras(), e.cameras(), e.score(), e.source(), snapshot);
    }

    private Evaluated evaluate(SetupReuseCandidates.Candidate c, String tier0Calibration, List<String> drifted) {
        ArrayNode cameras = mapper.createArrayNode();
        List<String> all = c.cameraHeights().keySet().stream().sorted().toList();
        if (!c.eligible()) {
            return new Evaluated(c, EXCLUDED, c.excludedReason(), List.of(), List.of(), List.of(), cameras, 0, null, null);
        }
        // Tier 0 가 지금 이 맵의 보정(또는 같은 내용으로 새로 낸 보정)을 쓰고 배경 회전 감시가 재세팅을 요구하면 그 카메라는 그때와 다르다
        if (drifted != null && !drifted.isEmpty() && tier0Calibration != null
                && candidates.calibrationVersionsOf(c.setupSessionId(), c.calibrationVersion()).contains(tier0Calibration)) {
            return new Evaluated(c, CHANGED, null, sorted(drifted), List.of(), List.of(), cameras, 0, TIER0_DRIFT, null);
        }
        Optional<CoverageGuideService.ViewCheck> view = coverageGuides == null ? Optional.empty()
                : coverageGuides.reuseViewCheck(c.sceneVersion());
        if (view.isEmpty()) {
            boolean missing = coverageGuides != null && coverageGuides.referenceMissing(c.sceneVersion());
            return new Evaluated(c, UNCONFIRMED, missing ? NO_PHOTOS : NO_RESULT, List.of(), List.of(), all, cameras, 0,
                    null, null);
        }
        List<String> changed = new ArrayList<>();
        List<String> shifted = new ArrayList<>();
        List<String> unconfirmed = new ArrayList<>();
        long score = 0;
        int same = 0;
        boolean allNoReference = true;
        for (JsonNode camera : view.get().cameras()) {
            cameras.add(camera.deepCopy());
            String id = camera.path("camera_id").asString("");
            String status = camera.path("status").asString("");
            score += camera.path("matches").asLong(0);
            if ("CHANGED".equals(status)) {
                changed.add(id);
            } else if ("SAME".equals(status)) {
                JsonNode rotation = camera.path("rotation_rad");
                if (rotation.isNumber() && rotation.doubleValue() > AS_IS_MAX_ROTATION_RAD) {
                    shifted.add(id);
                } else {
                    same++;
                }
            } else {
                unconfirmed.add(id);
            }
            if (!"NO_REFERENCE".equals(camera.path("reason").asString(""))) {
                allNoReference = false;
            }
        }
        ObjectNode snapshot = mapper.createObjectNode();
        snapshot.put("received_at_ms", view.get().receivedAt().toEpochMilli());
        snapshot.put("scene_version", c.sceneVersion());
        snapshot.set("cameras", cameras.deepCopy());
        String status;
        String reason = null;
        if (!changed.isEmpty()) {
            status = CHANGED;
        } else if (!shifted.isEmpty()) {
            status = SHIFTED;
        } else if (unconfirmed.isEmpty() && same > 0 && same == all.size()) {
            status = MATCH;
        } else {
            status = UNCONFIRMED;
            reason = allNoReference ? NO_PHOTOS : CHECKING;
        }
        return new Evaluated(c, status, reason, sorted(changed), sorted(shifted), sorted(unconfirmed), cameras, score,
                CHANGED.equals(status) ? VIEW_MATCH : null, snapshot);
    }

    private static List<String> sorted(List<String> ids) {
        return ids.stream().sorted().toList();
    }

    /**
     * 화면 응답. 옛 필드(-689·-712: available·blocked_reason·scene_version·calibration_version·camera_status·changed_cameras·camera_source)는
     * 대표 후보로 채우고, 새 필드 {@code recommended_setup_session_id}·{@code candidates}(순위 순)를 더한다.
     */
    ObjectNode toJson(Result r) {
        return toJson(r, false);
    }

    /**
     * open: 시연 계정({@link SetupDemoAccounts}) — 제외가 아닌 후보는 모두 selectable, 추천 없음(recommended=false,
     * recommended_setup_session_id=null), 쓸 후보가 있으면 available·blocked_reason=null, 없으면 NO_SCENE.
     */
    ObjectNode toJson(Result r, boolean open) {
        ObjectNode out = mapper.createObjectNode();
        Evaluated top = r.top();
        boolean usable = r.ranked().stream().anyMatch(e -> !EXCLUDED.equals(e.status()));
        out.put("available", open ? usable : r.available());
        out.put("blocked_reason", open ? (usable ? null : NO_SCENE) : r.blockedReason());
        out.put("scene_version", top == null ? null : top.candidate().sceneVersion());
        out.put("calibration_version", top == null ? null : top.candidate().calibrationVersion());
        String cameraStatus = top == null ? UNKNOWN : switch (top.status()) {
            case MATCH -> NOT_DETECTED;
            case CHANGED, SHIFTED -> CHANGED;
            default -> UNKNOWN;
        };
        out.put("camera_status", cameraStatus);
        ArrayNode changed = out.putArray("changed_cameras");
        if (top != null && CHANGED.equals(cameraStatus)) {
            (CHANGED.equals(top.status()) ? top.changedCameras() : top.shiftedCameras()).forEach(changed::add);
        }
        out.put("camera_source", top == null || !CHANGED.equals(cameraStatus) ? null
                : top.source() != null ? top.source() : VIEW_MATCH);
        out.put("recommended_setup_session_id", open || r.recommended() == null ? null
                : r.recommended().candidate().setupSessionId().toString());
        out.put("as_is_max_rotation_rad", AS_IS_MAX_ROTATION_RAD);
        ArrayNode list = out.putArray("candidates");
        for (Evaluated e : r.ranked()) {
            ObjectNode item = list.addObject();
            SetupReuseCandidates.Candidate c = e.candidate();
            item.put("setup_session_id", c.setupSessionId().toString());
            item.put("scene_version", c.sceneVersion());
            item.put("calibration_version", c.calibrationVersion());
            item.put("completed_at_ms", c.completedAt().toEpochMilli());
            ObjectNode heights = item.putObject("camera_heights_m");
            for (Map.Entry<String, Double> h : c.cameraHeights().entrySet()) {
                heights.put(h.getKey(), h.getValue());
            }
            item.put("status", e.status());
            item.put("reason", e.reason());
            item.put("selectable", open ? !EXCLUDED.equals(e.status()) : e.selectable());
            item.put("recommended", !open && e == r.recommended());
            e.changedCameras().forEach(item.putArray("changed_cameras")::add);
            e.shiftedCameras().forEach(item.putArray("shifted_cameras")::add);
            e.unconfirmedCameras().forEach(item.putArray("unconfirmed_cameras")::add);
            item.put("camera_source", e.source());
            item.set("cameras", e.cameras());
        }
        return out;
    }
}
