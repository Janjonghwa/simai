"""-657: 4대 중 한 카메라 쌍만 EPIPOLAR 로 기각돼도 5쌍 통과·4대 엉덩이 한 점 재투영이 맞으면 한 사람 4대 묶음 (14차 cam_B/cam_D)."""

from __future__ import annotations

import pytest

from simai_ai.geometry.rig import CameraRig
from simai_ai.tracking.cross_view import (
    CrossViewAssociator,
    CrossViewConfig,
    PairEvidence,
    RejectReason,
)

from .test_cross_view import (
    CORNERS,
    DictCalibrationProvider,
    T,
    calibration_doc,
    lv,
    project,
    skeleton,
)

BAD = ("cam2", "cam4")


@pytest.fixture
def rig() -> CameraRig:
    return CameraRig.from_provider(DictCalibrationProvider(calibration_doc()))


class _OnePairEpipolar(CrossViewAssociator):
    """cam2/cam4 쌍 근거만 EPIPOLAR 기각으로 바꾼다(보정 잔차로 그 쌍만 늘 경계를 넘는 장면)."""

    def _pairs(self, prepared):
        out = super()._pairs(prepared)
        for (a, b), ev in list(out.items()):
            if (a[0], b[0]) == BAD:
                out[(a, b)] = PairEvidence(a, b, ev.common_joints, epipolar_px=35.0, reject=RejectReason.EPIPOLAR)
                self._last_pair_joints.pop((a, b), None)
        return out


def _views(rig, person):
    return [lv(c, f"{c}-a", project(rig, c, person), T) for c in CORNERS]


@pytest.mark.parametrize("enabled,clusters", [(True, 1), (False, 2)])
def test_single_epipolar_pair_no_longer_splits_one_person_two_plus_two(rig, enabled, clusters):
    frame = _OnePairEpipolar(rig, CrossViewConfig(four_view_soft_pair_enabled=enabled)).associate(T, _views(rig, skeleton(4.0, 3.0)))
    assert len(frame.clusters) == clusters
    if enabled:
        assert set(frame.clusters[0].camera_ids) == set(CORNERS)
    else:
        assert all(len(c.camera_ids) == 2 for c in frame.clusters)
        assert not any(set(BAD) <= set(c.camera_ids) for c in frame.clusters)


def test_four_view_hips_must_reproject_to_one_point(rig):
    """4대 엉덩이 한 점 재투영이 상한을 넘으면 묶지 않는다(같은 사람이라는 근거 없음)."""
    cfg = CrossViewConfig(four_view_pelvis_reprojection_px=1e-15)  # 완전한 합성 view 도 넘게
    frame = _OnePairEpipolar(rig, cfg).associate(T, _views(rig, skeleton(4.0, 3.0)))
    assert len(frame.clusters) == 2
