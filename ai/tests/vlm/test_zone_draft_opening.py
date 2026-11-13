"""시연 개구부 모드(`ZoneDraftOptions.opening_only`) 단위 시험 (S15P31A101-671).

- 합성 점군: U자 단상 + 가운데 낮은·빈 자리 → 개구부 1개, 단상(고소작업면) 후보 0개
- 가려진 바닥 빈 칸 → 개구부 0개, 카메라 두 대가 가림 없이 본 빈 칸은 남김
- 겹친 후보 합치기
- 합성 이미지의 노랑·검정 줄무늬 검출과 후보 상자 투영 비율
- 줄무늬 근거 순위·VLM 개구부 확인(아니면 버림, 등급 CRITICAL)
- 스위치 끔이면 이전 추출 결과와 같다
"""

from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
import pytest

from simai_ai.vlm.client import VlmResult, VlmUnavailable
from simai_ai.vlm.face_blur import Person, prepare_vlm_frames
from simai_ai.vlm.hazard_stripes import (
    convex_hull,
    convex_polygon_mask,
    hazard_stripe_mask,
)
from simai_ai.vlm.zone_draft import (
    NO_OPINION_TEXT,
    OPENING_PROMPT_VERSION,
    OPENING_RESPONSE_FORMAT,
    OPENING_SYSTEM_PROMPT,
    PLATFORM_OPENING_NAME,
    PROMPT_VERSION,
    CameraPose,
    GeometryCandidate,
    ZoneDraftOptions,
    ZoneGeometryConfig,
    assess_opening_candidates,
    cameras_from_calibration,
    candidate_stripe_evidence,
    extract_geometry_candidates,
    filter_and_dedup_candidates,
    project_world_points_distorted,
    rank_opening_candidates,
)

DEMO = ZoneDraftOptions()
DEMO_CFG = DEMO.geometry_config()


def _grid(x0: float, x1: float, y0: float, y1: float, z: float, step: float = 0.05) -> np.ndarray:
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)])


def _without(pts: np.ndarray, boxes: list[tuple[float, float, float, float]]) -> np.ndarray:
    keep = np.ones(len(pts), dtype=bool)
    for x0, x1, y0, y1 in boxes:
        keep &= ~((pts[:, 0] >= x0) & (pts[:, 0] <= x1) & (pts[:, 1] >= y0) & (pts[:, 1] <= y1))
    return pts[keep]


# U자 단상: 단상 A(y 0~1.0)·단상 B(y 1.8~2.8)를 x 0~2.5 에 나란히, 한쪽 끝(x 0~0.8)을 방석(0.40 m)으로 막고 가운데(x 0.8~2.5, y 1.0~1.8)가 낮거나 비었다
U_BOXES = [(0.0, 2.5, 0.0, 1.0), (0.0, 2.5, 1.8, 2.8), (0.0, 0.8, 1.0, 1.8)]
GAP = (0.8, 2.5, 1.0, 1.8)


def _u_platform_cloud(*, gap: str = "empty") -> np.ndarray:
    floor = _without(_grid(-2.0, 4.5, -2.0, 4.5, 0.0), U_BOXES + [GAP])
    parts = [
        floor,
        _grid(0.0, 2.5, 0.0, 1.0, 0.45),
        _grid(0.0, 2.5, 1.8, 2.8, 0.45),
        _grid(0.0, 0.8, 1.0, 1.8, 0.40),
    ]
    if gap == "floor":
        parts.append(_grid(*GAP, 0.0))
    elif gap == "cushion":
        parts.append(_grid(*GAP, 0.42))
    return np.vstack(parts)


def _look_at(cam_id: str, center: tuple[float, float, float], target: tuple[float, float, float]) -> CameraPose:
    c = np.asarray(center, dtype=np.float64)
    f = np.asarray(target, dtype=np.float64) - c
    f /= np.linalg.norm(f)
    r = np.cross(f, np.array([0.0, 0.0, 1.0]))
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    rot = np.vstack([r, d, f])
    k = np.array([[400.0, 0.0, 320.0], [0.0, 400.0, 240.0], [0.0, 0.0, 1.0]])
    return CameraPose(camera_id=cam_id, K=k, R=rot, t=-rot @ c, image_size=(640, 480))


class _NoPeopleProvider:
    def people(self, image: np.ndarray) -> list[Person]:
        return []


def _frame(cam_id: str, img: np.ndarray):
    return prepare_vlm_frames([(cam_id, img)], "BLURRED", _NoPeopleProvider()).frames[0]


class _FakeVlm:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.model = "nvidia/Cosmos-Reason2-2B"

    def complete(self, prompt, frames, *, system=None, response_format=None):
        self.calls.append({"prompt": prompt, "frames": tuple(frames), "system": system, "response_format": response_format})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        content = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        return VlmResult(content=content, model=self.model, latency_ms=300, finish_reason="stop")


# ---------------------------------------------------------------------------- 기하 후보


@pytest.mark.parametrize("gap", ["empty", "floor"])
def test_u_platform_gap_gives_exactly_one_opening_and_no_platform_candidates(gap):
    cands = extract_geometry_candidates(_u_platform_cloud(gap=gap), DEMO_CFG)
    assert [c.candidate_kind for c in cands] == ["OPENING"]
    (op,) = cands
    assert op.source == "PLATFORM_GAP"
    xy = np.asarray(op.polygon_xy)
    assert xy[:, 0].mean() == pytest.approx(1.65, abs=0.15)
    assert xy[:, 1].mean() == pytest.approx(1.4, abs=0.1)
    assert op.enclosed_directions is not None and op.enclosed_directions >= 3  # 방석 쪽까지 U자
    assert op.surround_top_m == pytest.approx(0.45, abs=0.03)
    assert op.z_min_m == 0.0 and op.z_max_m == pytest.approx(0.65, abs=0.03)
    if gap == "floor":
        assert op.drop_m == pytest.approx(0.45, abs=0.03) and op.empty_ratio == 0.0
    else:
        assert op.drop_m is None and op.empty_ratio == 1.0  # 모르면 비운다


def test_gap_filled_with_cushions_at_top_height_is_not_an_opening():
    assert extract_geometry_candidates(_u_platform_cloud(gap="cushion"), DEMO_CFG) == []


def test_switch_off_keeps_previous_candidates_and_never_adds_platform_gap():
    pts = _u_platform_cloud(gap="empty")
    off = ZoneDraftOptions(opening_only=False).geometry_config(None)
    assert off == ZoneGeometryConfig()
    legacy = extract_geometry_candidates(pts)
    assert [c.to_dict() for c in extract_geometry_candidates(pts, off)] == [c.to_dict() for c in legacy]
    assert any(c.candidate_kind == "ELEVATED_WORK_SURFACE" for c in legacy)
    assert all(c.source != "PLATFORM_GAP" for c in legacy)


def _occluded_void_cloud() -> np.ndarray:
    # 바닥 가운데 빈 칸(x 0.1~0.6, y -0.4~0.4). 그 앞(x 0)에 키 1.5 m 판(윗면 없음, 고소작업면 후보 아님)이 서 있다
    floor = _without(_grid(-3.0, 3.0, -2.0, 2.0, 0.0), [(0.1, 0.6, -0.4, 0.4)])
    zs = np.arange(0.1, 1.5, 0.05)
    ys = np.arange(-0.6, 0.6, 0.05)
    gy, gz = np.meshgrid(ys, zs)
    board = np.column_stack([np.zeros(gy.size), gy.ravel(), gz.ravel()])
    return np.vstack([floor, board])


def test_occluded_floor_void_is_dropped_but_seen_void_is_kept():
    pts = _occluded_void_cloud()
    legacy = [c for c in extract_geometry_candidates(pts) if c.candidate_kind == "OPENING"]
    assert len(legacy) == 1 and legacy[0].source == "FLOOR_VOID"  # 이전 동작: 가려진 바닥이 개구부로 잡힌다

    behind_board = {
        "camL": _look_at("camL", (-3.0, 0.3, 2.0), (0.35, 0.0, 0.0)),
        "camR": _look_at("camR", (-3.0, -0.3, 2.0), (0.35, 0.0, 0.0)),
    }
    assert extract_geometry_candidates(pts, DEMO_CFG, cameras=behind_board) == []
    assert extract_geometry_candidates(pts, DEMO_CFG, cameras=None) == []  # 카메라를 모르면 확인할 수 없어 버린다

    overhead = {
        "camA": _look_at("camA", (2.5, 1.5, 2.5), (0.35, 0.0, 0.0)),
        "camB": _look_at("camB", (2.5, -1.5, 2.5), (0.35, 0.0, 0.0)),
    }
    kept = extract_geometry_candidates(pts, DEMO_CFG, cameras=overhead)
    assert [c.source for c in kept] == ["FLOOR_VOID"]


def _opening(cid: str, cx: float, cy: float, size: float = 0.8, source: str | None = "PLATFORM_GAP") -> GeometryCandidate:
    h = size / 2
    return GeometryCandidate(
        candidate_id=cid,
        candidate_kind="OPENING",
        polygon_xy=((cx - h, cy - h), (cx + h, cy - h), (cx + h, cy + h), (cx - h, cy + h)),
        z_min_m=0.0,
        z_max_m=0.65,
        area_m2=size * size,
        source=source,
        enclosed_directions=3,
    )


def test_dedup_merges_same_place_keeps_priority_and_separate_places():
    a = _opening("cand-platform-opening-01", 1.0, 1.0, 1.0)
    b = _opening("cand-opening-01", 1.1, 1.05, 0.6, source="FLOOR_VOID")  # 같은 자리, 다른 갈래
    c = _opening("cand-opening-02", 3.0, 3.0, 0.6, source="FLOOR_VOID")
    elev = replace(a, candidate_id="cand-elevated-01", candidate_kind="ELEVATED_WORK_SURFACE")
    cfg = replace(DEMO_CFG, allowed_kinds=None)
    out = filter_and_dedup_candidates([b, a, c, elev], cfg)
    assert [x.candidate_id for x in out] == ["cand-platform-opening-01", "cand-opening-02", "cand-elevated-01"]
    assert [x.candidate_id for x in filter_and_dedup_candidates([b, a, c, elev], DEMO_CFG)] == [
        "cand-platform-opening-01",
        "cand-opening-02",
    ]
    assert filter_and_dedup_candidates([b, a, c, elev], ZoneGeometryConfig()) == [b, a, c, elev]


def test_options_from_env_default_on_and_switch_off():
    on = ZoneDraftOptions.from_env({})
    assert on.opening_only is True and on.max_openings == 1 and on.prompt_version == OPENING_PROMPT_VERSION
    assert on.geometry_config().allowed_kinds == ("OPENING",)
    off = ZoneDraftOptions.from_env({"SIMAI_ZONE_DRAFT_OPENING_ONLY": "0"})
    assert off.opening_only is False and off.prompt_version == PROMPT_VERSION
    tuned = ZoneDraftOptions.from_env(
        {"SIMAI_ZONE_DRAFT_MAX_OPENINGS": "2", "SIMAI_ZONE_DRAFT_PLATFORM_DROP_M": "0.2", "SIMAI_ZONE_DRAFT_FLOOR_VOID_MIN_CAMERAS": "x"}
    )
    assert tuned.max_openings == 2 and tuned.platform_opening_min_drop_m == 0.2 and tuned.floor_void_min_visible_cameras == 2


# ---------------------------------------------------------------------------- 줄무늬 근거


def _stripes(img: np.ndarray, hull: np.ndarray, period: int = 8) -> None:
    """볼록 다각형 영역을 노랑·검정 대각 줄무늬로 칠한다(BGR)."""
    mask = convex_polygon_mask(hull, img.shape)
    yy, xx = np.mgrid[0 : img.shape[0], 0 : img.shape[1]]
    yellow = ((xx + yy) // period) % 2 == 0
    img[mask & yellow] = (20, 220, 235)
    img[mask & ~yellow] = (15, 15, 15)


def test_stripe_mask_finds_alternating_yellow_black_not_plain_yellow_or_gray():
    img = np.full((240, 320, 3), 150, dtype=np.uint8)
    _stripes(img, np.array([[20.0, 20.0], [120.0, 20.0], [120.0, 100.0], [20.0, 100.0]]))
    img[140:220, 180:300] = (20, 220, 235)  # 줄무늬 없는 노란 판
    mask = hazard_stripe_mask(img)
    assert mask[30:90, 30:110].mean() > 0.6  # 창(5 px)보다 넓은 줄 가운데는 한 색만 들어온다
    assert mask[150:210, 190:290].mean() == 0.0
    assert mask[150:230, 20:150].mean() == 0.0
    assert not hazard_stripe_mask(np.full((240, 320, 3), 150, dtype=np.uint8)).any()


def test_convex_hull_and_mask_basics():
    hull = convex_hull(np.array([[0, 0], [10, 0], [10, 10], [0, 10], [5, 5]], dtype=float))
    assert len(hull) == 4
    assert convex_polygon_mask(hull, (20, 20)).sum() == 100


OVERHEAD = _look_at("cam1", (1.6, -2.5, 3.0), (1.6, 1.4, 0.3))


def _striped_frame_around(cand: GeometryCandidate, pose: CameraPose) -> np.ndarray:
    img = np.full((480, 640, 3), 140, dtype=np.uint8)
    pts = np.array([[x, y, z] for x, y in cand.polygon_xy for z in (0.0, 1.6)])
    uv, ok = project_world_points_distorted(pts, pose)
    assert ok.all()
    _stripes(img, convex_hull(uv))
    return img


def test_stripe_evidence_projects_candidate_box_onto_frame():
    target = _opening("cand-platform-opening-01", 1.65, 1.4)
    other = _opening("cand-opening-09", -1.0, 3.5, source="FLOOR_VOID")
    pose2 = _look_at("cam2", (4.5, 1.4, 3.0), (1.6, 1.4, 0.3))
    masks = {
        "cam1": hazard_stripe_mask(_striped_frame_around(target, OVERHEAD)),
        "cam2": hazard_stripe_mask(_striped_frame_around(target, pose2)),
    }
    cams = {"cam1": OVERHEAD, "cam2": pose2}
    ev = candidate_stripe_evidence(target, masks, cams)
    assert {c.camera_id for c in ev.cameras} == {"cam1", "cam2"}
    assert all(c.inside_ratio > 0.5 for c in ev.cameras)
    assert ev.consensus_ratio > 0.3 and ev.score(DEMO.stripe_full_ratio) == 1.0
    far = candidate_stripe_evidence(other, masks, cams)
    assert far.consensus_ratio < 0.05
    # 카메라 하나만 보면 그 절반만 믿는다
    single = candidate_stripe_evidence(target, {"cam1": masks["cam1"]}, cams)
    assert single.consensus_ratio == pytest.approx(single.best_ratio * 0.5)


def test_distortion_is_read_from_calibration_and_used_only_for_stripe_projection():
    doc = {
        "cameras": [
            {
                "camera_id": "cam_A",
                "image_size": [2304, 1296],
                "K": [[1284.3, 0.0, 1151.0], [0.0, 1284.3, 649.1], [0.0, 0.0, 1.0]],
                "R": [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],
                "t": [0.0, 0.0, 3.0],
                "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [-0.29, 0.06, 0.0, 0.0, 0.0]},
            }
        ]
    }
    pose = cameras_from_calibration(doc)["cam_A"]
    assert pose.distortion == (-0.29, 0.06, 0.0, 0.0, 0.0)
    uv_d, ok = project_world_points_distorted(np.array([[1.5, 0.0, 0.0]]), pose)
    assert ok[0]
    # 술통형 왜곡이라 화면 중심 쪽으로 당겨진다
    assert 1151.0 < uv_d[0, 0] < 1151.0 + 1284.3 * 0.5


# ---------------------------------------------------------------------------- 순위·VLM 확인


def _two_candidates_with_one_striped():
    target = _opening("cand-platform-opening-01", 1.65, 1.4)
    other = _opening("cand-platform-opening-02", -1.0, 3.5)
    frames = {"cam1": _frame("cam1", _striped_frame_around(target, OVERHEAD))}
    return target, other, frames, {"cam1": OVERHEAD}


def test_rank_prefers_striped_candidate_and_default_draft_keeps_evidence():
    target, other, frames, cams = _two_candidates_with_one_striped()
    ranked = rank_opening_candidates([other, target], frames, cams)
    assert [r.candidate.candidate_id for r in ranked] == [target.candidate_id, other.candidate_id]
    assert ranked[0].score > ranked[1].score

    drafts, status, model = assess_opening_candidates([other, target], vlm_client=None, frames_by_camera=frames, cameras_by_id=cams)
    assert status == "NO_OPINION" and model is None
    (d,) = drafts  # 시연 기본 max_openings=1
    assert d.candidate_id == target.candidate_id
    assert d.recommended_name == f"{PLATFORM_OPENING_NAME} 1"
    assert d.recommended_severity == "CRITICAL" and d.zone_kind == "OPENING" and d.eval_type == "GROUND_FOOTPRINT"
    assert d.rationale.startswith(NO_OPINION_TEXT) and "줄무늬 cam1" in d.rationale
    assert len(d.rationale) <= 500


def test_no_frames_means_no_stripe_evidence_but_geometry_candidate_stays():
    target, _, _, cams = _two_candidates_with_one_striped()
    drafts, _, _ = assess_opening_candidates([target], vlm_client=None, frames_by_camera={}, cameras_by_id=cams)
    (d,) = drafts
    assert "줄무늬 근거 없음" in d.rationale


def _answer(ok: bool, severity: str = "CRITICAL") -> dict:
    return {
        "is_marked_opening": ok,
        "markings_seen": ["RAILING", "TAPE"] if ok else [],
        "recommended_name": "단상 개구부",
        "recommended_severity": severity,
        "rationale": "줄무늬 난간과 테이프가 단상 사이 빈 자리를 두르고 있다." if ok else "표시가 보이지 않는다.",
        "confidence": 0.8,
    }


def test_vlm_rejection_drops_candidate_and_next_rank_gets_a_chance():
    target, other, frames, cams = _two_candidates_with_one_striped()
    frames["cam1b"] = frames["cam1"]
    cams = {**cams, "cam1b": OVERHEAD}
    vlm = _FakeVlm([_answer(False), _answer(True, severity="HIGH")])
    drafts, status, model = assess_opening_candidates([other, target], vlm_client=vlm, frames_by_camera=frames, cameras_by_id=cams)
    assert status == "SUCCEEDED" and model == "nvidia/Cosmos-Reason2-2B"
    assert len(vlm.calls) == 2
    assert vlm.calls[0]["system"] == OPENING_SYSTEM_PROMPT
    assert vlm.calls[0]["response_format"] == OPENING_RESPONSE_FORMAT
    assert "노랑·검정 줄무늬" in str(vlm.calls[0]["prompt"]) and target.candidate_id in str(vlm.calls[0]["prompt"])
    (d,) = drafts
    assert d.candidate_id == other.candidate_id
    assert d.recommended_name == "단상 개구부"  # VLM 이름이 하나뿐이면 구분자 없음
    assert d.recommended_severity == "CRITICAL"  # VLM 은 개구부 기본 등급을 낮추지 못한다
    assert d.vlm_status == "VLM" and "줄무늬 난간" in d.rationale


def test_vlm_rejecting_everything_gives_no_drafts():
    target, _, frames, cams = _two_candidates_with_one_striped()
    drafts, status, _ = assess_opening_candidates([target], vlm_client=_FakeVlm([_answer(False)]), frames_by_camera=frames, cameras_by_id=cams)
    assert drafts == [] and status == "SUCCEEDED"


@pytest.mark.parametrize("failure", [VlmUnavailable("down"), "not-json", {"is_marked_opening": "yes"}])
def test_vlm_failure_keeps_top_candidate_as_no_opinion(failure):
    target, other, frames, cams = _two_candidates_with_one_striped()
    drafts, status, _ = assess_opening_candidates(
        [other, target], vlm_client=_FakeVlm([failure]), frames_by_camera=frames, cameras_by_id=cams
    )
    assert status == "NO_OPINION"
    assert [d.candidate_id for d in drafts] == [target.candidate_id]
    assert drafts[0].vlm_status == "NO_OPINION"


def test_non_opening_candidates_are_ignored_in_opening_mode():
    elev = replace(_opening("cand-elevated-01", 0.0, 0.0), candidate_kind="ELEVATED_WORK_SURFACE")
    drafts, status, model = assess_opening_candidates([elev], vlm_client=None)
    assert drafts == [] and status == "NO_OPINION" and model is None
