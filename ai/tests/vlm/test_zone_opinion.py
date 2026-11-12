"""변경 의심 구역 VLM 의견: 카메라 고르기·크롭·프롬프트·파서·NO_OPINION 폴백 시험 (S15P31A101-631)."""

from __future__ import annotations

import json

import numpy as np
import pytest

from simai_ai.vlm.client import VlmResult, VlmTimeout
from simai_ai.vlm.face_blur import Person, VlmFrame, prepare_vlm_frames
from simai_ai.vlm.zone_draft import CameraPose
from simai_ai.vlm.zone_opinion import (
    MAX_RATIONALE_CHARS,
    OpinionView,
    SuspectedZone,
    ZoneOpinionConfig,
    assess_zone_opinions,
    build_opinion_prompt,
    crop_box_norm,
    norm_to_pixel_rect,
    parse_opinion_response,
    project_zone_box,
    select_opinion_view,
)

CFG = ZoneOpinionConfig()


def _down_pose(
    cam_id: str = "cam1",
    *,
    x: float = 1.4,
    y: float = 0.9,
    z: float = 4.0,
    size=(320, 240),
) -> CameraPose:
    """(x, y, z) 에서 바닥을 내려다보는 카메라(X_c = R X_w + t, R = diag(1, -1, -1))."""
    r = np.diag([1.0, -1.0, -1.0])
    t = -r @ np.array([x, y, z])
    k = np.array(
        [[200.0, 0.0, size[0] / 2], [0.0, 200.0, size[1] / 2], [0.0, 0.0, 1.0]]
    )
    return CameraPose(camera_id=cam_id, K=k, R=r, t=t, image_size=size)


def _up_pose(cam_id: str = "cam2") -> CameraPose:
    """천장을 보는 카메라: 바닥 구역은 모두 카메라 뒤다."""
    k = np.array([[200.0, 0.0, 160.0], [0.0, 200.0, 120.0], [0.0, 0.0, 1.0]])
    return CameraPose(
        camera_id=cam_id,
        K=k,
        R=np.eye(3),
        t=np.array([0.0, 0.0, -3.0]),
        image_size=(320, 240),
    )


ZONE = SuspectedZone(
    zone_id="zone-platform-1",
    polygon_xy=((0.8, 0.5), (2.0, 0.5), (2.0, 1.3), (0.8, 1.3)),
    name="중앙 단상",
    zone_kind="WORK_SURFACE",
    z_min_m=0.0,
    z_max_m=2.5,
    changed_area_ratio=0.42,
    max_height_diff_m=0.61,
    max_diff_xy=(1.45, 0.95),
)


class _NoPeople:
    def people(self, image: np.ndarray) -> list[Person]:
        return []


def _frame(source: str, shape=(240, 320)) -> VlmFrame:
    img = np.full((*shape, 3), 120, dtype=np.uint8)
    return prepare_vlm_frames([(source, img)], "BLURRED", _NoPeople()).frames[0]


class _FakeVlm:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.model = "nvidia/Cosmos-Reason2-2B"

    def complete(
        self, prompt, frames, *, system=None, response_format=None, timeout_s=None
    ):
        frames = tuple(frames)
        assert all(isinstance(f, VlmFrame) for f in frames)
        self.calls.append(
            {
                "prompt": prompt,
                "frames": frames,
                "system": system,
                "timeout_s": timeout_s,
            }
        )
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        content = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
        return VlmResult(
            content=content, model=self.model, latency_ms=300, finish_reason="stop"
        )


GOOD = {
    "recommended_action": "MODIFY",
    "rationale": "단상 위 상자가 치워져 높이가 낮아졌습니다.",
    "confidence": 0.7,
}


def test_box_z_range_clamps_height_above_floor():
    assert ZONE.box_z_range(CFG) == (0.0, 2.0)  # 2.5 m 는 2.0 m 로 자른다
    floor = SuspectedZone("z", ((0, 0), (1, 0), (1, 1)))
    assert floor.box_z_range(CFG) == (0.0, 1.0)  # 높이를 모르면 바닥 위 1 m
    opening = SuspectedZone("o", ((0, 0), (1, 0), (1, 1)), z_min_m=-0.6, z_max_m=0.2)
    assert opening.box_z_range(CFG) == (-0.6, 0.3)  # 바닥 위 최소 0.3 m


def test_project_zone_box_visible_and_behind_camera():
    box = project_zone_box(ZONE, _down_pose(), CFG)
    assert box is not None
    x1, y1, x2, y2 = box.box_px
    assert 0 <= x1 < x2 <= 320 and 0 <= y1 < y2 <= 240
    assert 0 < box.visible_ratio <= 1 and 0 < box.area_ratio <= 1
    assert project_zone_box(ZONE, _up_pose(), CFG) is None


def test_crop_box_norm_pads_and_stays_in_frame():
    box = project_zone_box(ZONE, _down_pose(), CFG)
    assert box is not None
    u1, v1, u2, v2 = crop_box_norm(box, CFG)
    assert 0.0 <= u1 < u2 <= 1.0 and 0.0 <= v1 < v2 <= 1.0
    x1, y1, x2, y2 = box.box_px
    assert u1 <= x1 / 320 and u2 >= x2 / 320 and v1 <= y1 / 240 and v2 >= y2 / 240
    # 크롭 최소 변(96 px)
    assert (u2 - u1) * 320 >= 96 - 1 or u1 == 0.0 or u2 == 1.0


def test_norm_to_pixel_rect_is_resolution_independent():
    norm = (0.25, 0.5, 0.75, 1.0)
    assert norm_to_pixel_rect(norm, 320, 240) == (80, 120, 240, 240)
    assert norm_to_pixel_rect(norm, 1280, 960) == (320, 480, 960, 960)
    assert norm_to_pixel_rect((0.0, 0.0, 0.0, 0.0), 100, 100) == (0, 0, 1, 1)


def test_select_opinion_view_needs_same_camera_in_both():
    before = {"cam1": _down_pose("cam1"), "cam2": _up_pose("cam2")}
    after = {"cam1": _down_pose("cam1", x=1.45), "cam2": _up_pose("cam2")}
    view = select_opinion_view(ZONE, before, after, cfg=CFG)
    assert view is not None and view.camera_id == "cam1"
    assert len(view.before_box_norm) == 4 and len(view.after_box_norm) == 4
    # 이전 보정에서 안 보이면(카메라를 돌림) 고르지 않는다
    assert (
        select_opinion_view(
            ZONE, {"cam1": _up_pose("cam1")}, {"cam1": _down_pose("cam1")}, cfg=CFG
        )
        is None
    )
    # 화면에 전·후 사진이 둘 다 있는 카메라만
    assert (
        select_opinion_view(ZONE, before, after, allowed_cameras={"cam2"}, cfg=CFG)
        is None
    )


def test_select_opinion_view_prefers_larger_visible_projection():
    near = _down_pose("camA", z=3.0)
    far = _down_pose("camB", z=8.0)
    view = select_opinion_view(
        ZONE, {"camA": near, "camB": far}, {"camA": near, "camB": far}, cfg=CFG
    )
    assert view is not None and view.camera_id == "camA"


def test_prompt_has_height_numbers_and_no_person_fields():
    text = build_opinion_prompt(ZONE, "cam1")
    assert "0.61" in text and "42%" in text and "cam1" in text
    assert "첫 번째 사진은 이전 세팅" in text


VIEW = OpinionView("cam1", (0.1, 0.1, 0.5, 0.5), (0.1, 0.12, 0.5, 0.52), 0.2)


def test_parse_opinion_response_valid_and_one_line():
    op = parse_opinion_response(json.dumps(GOOD, ensure_ascii=False), "z1", VIEW, "m")
    assert (
        op.status == "OPINION"
        and op.recommended_action == "MODIFY"
        and op.confidence == 0.7
    )
    assert op.camera_id == "cam1" and op.before_crop_box_norm == VIEW.before_box_norm
    multi = dict(GOOD, rationale="\n  첫 줄입니다.\n둘째 줄")
    assert (
        parse_opinion_response(json.dumps(multi), "z1", VIEW, "m").rationale
        == "첫 줄입니다."
    )
    long = dict(GOOD, rationale="가" * 300)
    assert (
        len(parse_opinion_response(json.dumps(long), "z1", VIEW, "m").rationale)
        == MAX_RATIONALE_CHARS
    )


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        json.dumps(dict(GOOD, recommended_action="DELETE")),
        json.dumps(dict(GOOD, rationale="  ")),
        json.dumps(dict(GOOD, confidence=1.5)),
        json.dumps(dict(GOOD, confidence=True)),
    ],
)
def test_parse_opinion_response_rejects_bad_shapes(content):
    with pytest.raises((ValueError, TypeError)):
        parse_opinion_response(content, "z1", VIEW, "m")


def _assess(
    vlm,
    zones=(ZONE,),
    *,
    before_frames=None,
    after_frames=None,
    remaining=None,
    cfg=CFG,
    blocked=None,
):
    cams_b = {"cam1": _down_pose("cam1")}
    cams_a = {"cam1": _down_pose("cam1", x=1.45)}
    return assess_zone_opinions(
        list(zones),
        vlm_client=vlm,
        before_cameras=cams_b,
        after_cameras=cams_a,
        before_frames=before_frames
        if before_frames is not None
        else {"cam1": _frame("before")},
        after_frames=after_frames
        if after_frames is not None
        else {"cam1": _frame("after")},
        allowed_cameras={"cam1"},
        blocked_reason=blocked,
        remaining_s=(lambda: remaining) if remaining is not None else None,
        cfg=cfg,
    )


def test_assess_sends_before_then_after_crops_and_returns_opinion():
    vlm = _FakeVlm(GOOD)
    [op] = _assess(vlm, remaining=60.0)
    assert op.status == "OPINION" and op.model_version == "nvidia/Cosmos-Reason2-2B"
    call = vlm.calls[0]
    assert [f.source for f in call["frames"]] == ["before", "after"]
    assert all(
        f.pixels.shape[0] < 240 or f.pixels.shape[1] < 320 for f in call["frames"]
    )  # 잘라 넣는다
    assert call["timeout_s"] == pytest.approx(58.0)


def test_assess_no_opinion_reasons_keep_crops_when_camera_chosen():
    [failed] = _assess(_FakeVlm(VlmTimeout("slow")))
    assert failed.status == "NO_OPINION" and failed.reason == "VLM_FAILED"
    assert failed.camera_id == "cam1" and failed.before_crop_box_norm is not None
    assert (
        failed.recommended_action is None
        and failed.rationale is None
        and failed.model_version is None
    )

    [no_vlm] = _assess(None)
    assert no_vlm.reason == "VLM_UNAVAILABLE" and no_vlm.camera_id == "cam1"

    [no_before] = _assess(_FakeVlm(GOOD), before_frames={})
    assert no_before.reason == "PREVIOUS_FRAMES_UNAVAILABLE"

    [no_after] = _assess(_FakeVlm(GOOD), after_frames={"camX": _frame("x")})
    assert no_after.reason == "CURRENT_FRAMES_UNAVAILABLE"

    [budget] = _assess(_FakeVlm(GOOD), remaining=3.0)
    assert budget.reason == "TIME_BUDGET"

    hidden = SuspectedZone("far", ((50.0, 50.0), (51.0, 50.0), (51.0, 51.0)))
    [not_visible] = _assess(_FakeVlm(GOOD), zones=(hidden,))
    assert not_visible.reason == "NOT_VISIBLE" and not_visible.camera_id is None

    [blocked] = _assess(_FakeVlm(GOOD), blocked="NO_PREVIOUS_FRAMES")
    assert blocked.reason == "NO_PREVIOUS_FRAMES" and blocked.camera_id is None


def test_assess_caps_zone_count_and_gates_only_chosen_before_frames():
    asked: list[set[str]] = []

    def before_frames(cams: set[str]):
        asked.append(set(cams))
        return {"cam1": _frame("before")}

    zones = [SuspectedZone(f"z{i}", ZONE.polygon_xy, z_max_m=0.5) for i in range(3)]
    vlm = _FakeVlm(GOOD, GOOD)
    ops = _assess(
        vlm, zones, before_frames=before_frames, cfg=ZoneOpinionConfig(max_zones=2)
    )
    assert [o.status for o in ops] == ["OPINION", "OPINION", "NO_OPINION"]
    assert ops[2].reason == "ZONE_LIMIT"
    assert asked == [{"cam1"}] and len(vlm.calls) == 2
