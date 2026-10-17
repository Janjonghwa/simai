"""실제 CMU Panoptic 시퀀스로 도는 시험 (marker dataset, SIMAI_PANOPTIC_DIR 가 있을 때만).

데이터 위치·해시·라이선스는 `docs/data/cmu_panoptic.md`. 확인한 시퀀스: 171204_pose1.
"""

import itertools
import statistics

import pytest

from simai_ai.providers.contract import check_tracks_match_calibration
from simai_ai.providers.dataset_gt import PanopticCalibrationProvider, PanopticWorldTrackProvider
from simai_ai.providers.testing import CalibrationProviderContract, WorldTrackProviderContract, assert_no_violations

pytestmark = pytest.mark.dataset

MAX_FRAMES = 300
L_ANKLE, R_ANKLE = 15, 16


@pytest.fixture(scope="module")
def calibration(panoptic_dir):
    return PanopticCalibrationProvider(panoptic_dir)


@pytest.fixture(scope="module")
def tracks(calibration):
    return PanopticWorldTrackProvider(calibration)


class TestPanopticCalibrationReal(CalibrationProviderContract):
    require_upright_cameras = True

    @pytest.fixture
    def provider(self, calibration):
        return calibration


class TestPanopticWorldTracksReal(WorldTrackProviderContract):
    max_frames = MAX_FRAMES

    @pytest.fixture
    def provider(self, tracks):
        return tracks


def test_real_tracks_match_calibration(tracks):
    cal = tracks.calibration.current()
    docs = itertools.islice(tracks.frames(), MAX_FRAMES)
    assert_no_violations(v for d in docs for v in check_tracks_match_calibration(d, cal))


def test_floor_is_world_z0(tracks):
    """바닥 높이 가정(floor_y_cm = 0)의 검산: 발목 관절은 바닥 위 약 3~15 cm 다."""
    docs = itertools.islice(tracks.frames(), 0, 30 * MAX_FRAMES, 30)
    ankles = [
        tr["skeleton"]["joints"][j][2]
        for d in docs
        for tr in d["tracks"]
        for j in (L_ANKLE, R_ANKLE)
        if tr["skeleton"]["joints"][j] is not None
    ]
    assert len(ankles) >= 20
    assert 0.03 <= statistics.median(ankles) <= 0.15
