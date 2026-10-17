"""Provider 출처가 결과물의 input_provenance 로 그대로 전달되는지 본다 (명세 20.4 규칙 2·3)."""

import pytest

from simai_ai.providers import (
    CalibrationProvider,
    FileCalibrationProvider,
    FileWorldTrackProvider,
    FrameSource,
    WorldTrackProvider,
    build_input_provenance,
    provenance_from_providers,
)
from simai_ai.providers.contract import check_input_provenance, check_provenance_carries
from simai_ai.providers.testing import assert_no_violations, example_path, load_example


class _Frames(FrameSource):
    def __init__(self, source="FILE"):
        self._source = source

    source = property(lambda self: self._source)
    camera_ids = property(lambda self: ("hd_00_00", "hd_00_03"))

    def frames(self):
        return iter(())


@pytest.mark.parametrize(
    ("frames", "calibration", "tracks", "upper"),
    [
        ("FILE", "G1_AUTO", None, False),
        ("FILE", "MODEL_ESTIMATE", None, False),
        ("FILE", None, None, False),
        ("LIVE", "G1_AUTO", "TIER0_LIVE", False),
        ("FILE", "G1_AUTO", "TIER0_LOG", False),
        ("DATASET", "DATASET_GT", "DATASET_GT", True),
        ("DATASET", "G1_AUTO", None, False),
        ("FILE", "G1_AUTO", "SIM", True),
        ("FILE", "SIM", None, True),
        ("SIM", None, None, True),
    ],
)
def test_upper_bound_follows_sources(frames, calibration, tracks, upper):
    prov = build_input_provenance(
        {"source": frames},
        {"source": calibration, "calibration_version": "v"} if calibration else None,
        {"source": tracks, "provider_version": "p"} if tracks else None,
    )
    assert prov["is_upper_bound"] is upper
    assert_no_violations(check_input_provenance(prov))


def test_build_copies_fragments():
    frames = {"source": "FILE"}
    prov = build_input_provenance(frames)
    frames["source"] = "SIM"
    assert prov["frames"]["source"] == "FILE"


def test_calibration_omitted_for_image_only_runs():
    prov = build_input_provenance({"source": "FILE", "dataset_id": "room4/2026-10-07"})
    assert "calibration" not in prov and "tracks" not in prov


@pytest.mark.parametrize(
    "name",
    [
        "human_mesh_output.avatar_asset.json",
        "human_mesh_output.pose_frame.json",
        "run_record.json",
        "run_record.image_only.json",
        "run_record.known_model_estimate.json",
        "scene_output.json",
    ],
)
def test_contract_examples_provenance_consistent(name):
    """contracts 예시 결과물의 input_provenance 도 같은 규칙을 지킨다."""
    assert_no_violations(check_input_provenance(load_example(name)["input_provenance"]))


def test_wrong_upper_bound_is_caught():
    prov = build_input_provenance({"source": "DATASET"}, {"source": "DATASET_GT", "calibration_version": "v"})
    prov["is_upper_bound"] = False
    assert any("is_upper_bound" in v for v in check_input_provenance(prov))


def test_schema_violation_is_caught():
    assert check_input_provenance({"frames": {"source": "RTSP"}, "is_upper_bound": False})


def _providers():
    calibration = FileCalibrationProvider(example_path("calibration.json"))
    tracks = FileWorldTrackProvider(example_path("world_tracks.json"))
    return _Frames(), calibration, tracks


def test_provenance_from_providers_carries_sources():
    frames, calibration, tracks = _providers()
    prov = provenance_from_providers(frames, calibration, tracks)
    assert prov == {
        "frames": {"source": "FILE", "camera_ids": ["hd_00_00", "hd_00_03"]},
        "calibration": {"source": "G1_AUTO", "calibration_version": "cal-0"},
        "tracks": {"source": "DATASET_GT", "provider_version": "panoptic-adapter-0.1.0"},
        "is_upper_bound": True,
    }
    assert_no_violations(check_provenance_carries(prov, frames, calibration, tracks))


def test_carries_allows_extra_frame_details():
    """결과물은 dataset_id·dataset_hash 같은 정보를 더 적을 수 있다. Provider 가 준 값만 같으면 된다."""
    frames, calibration, _ = _providers()
    prov = provenance_from_providers(frames, calibration)
    prov["frames"]["dataset_id"] = "classroom-empty/2026-10-20"
    assert_no_violations(check_provenance_carries(prov, frames, calibration))


def test_carries_catches_missing_and_changed():
    frames, calibration, tracks = _providers()
    prov = provenance_from_providers(frames, calibration)
    assert any("tracks 가 없다" in v for v in check_provenance_carries(prov, frames, calibration, tracks))

    prov["calibration"]["calibration_version"] = "cal-9"
    assert any("calibration" in v for v in check_provenance_carries(prov, frames, calibration))


def test_interfaces_are_abstract():
    for cls in (FrameSource, CalibrationProvider, WorldTrackProvider):
        with pytest.raises(TypeError):
            cls()
