"""CMU Panoptic DATASET_GT 어댑터 시험 (합성 표본 `panoptic_mini/`, 실제 데이터 없이 돈다)."""

import json
import shutil
from pathlib import Path

import pytest

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.providers import build_input_provenance
from simai_ai.providers.contract import check_provenance_carries, check_tracks_match_calibration
from simai_ai.providers.dataset_gt import PanopticCalibrationProvider, PanopticWorldTrackProvider
from simai_ai.providers.dataset_gt.panoptic import (
    ADAPTER_VERSION,
    COCO17_FROM_COCO19,
    DEFAULT_EPOCH_MS,
    convert_extrinsics,
    panoptic_to_world,
)
from simai_ai.providers.testing import CalibrationProviderContract, WorldTrackProviderContract, assert_no_violations

PANOPTIC_MINI = Path(__file__).resolve().parent / "panoptic_mini"

HEAD = range(5)  # nose, eyes, ears
LEGS = (13, 14, 15, 16)  # knees, ankles
L_WRIST, R_EAR = 9, 4


def _calibration(camera_ids=None, root=PANOPTIC_MINI, **kw):
    return PanopticCalibrationProvider(root, camera_ids, **kw)


def _tracks(camera_ids=None, root=PANOPTIC_MINI, **kw):
    return PanopticWorldTrackProvider(_calibration(camera_ids, root, **kw))


def _raw(name):
    return json.loads((PANOPTIC_MINI / name).read_text(encoding="utf-8"))


def _track(doc, world_id):
    return next(tr for tr in doc["tracks"] if tr["world_id"] == world_id)


# --------------------------------------------------------------------------- 공통 계약 시험


class TestPanopticCalibrationMini(CalibrationProviderContract):
    require_upright_cameras = True

    @pytest.fixture
    def provider(self):
        return _calibration()


class TestPanopticCalibrationSubset(CalibrationProviderContract):
    @pytest.fixture
    def provider(self):
        return _calibration(["hd_00_00", "hd_00_03"], floor_y_cm=-3.0)


class TestPanopticWorldTracksMini(WorldTrackProviderContract):
    @pytest.fixture
    def provider(self):
        return _tracks()


class TestPanopticWorldTracksSubset(WorldTrackProviderContract):
    """다리가 화면 밖인 카메라가 섞인 경우 (관절별 2대 규칙)."""

    @pytest.fixture
    def provider(self):
        return _tracks(["hd_00_00", "hd_00_03"])


# --------------------------------------------------------------------------- 좌표 변환


def test_axis_and_unit_conversion():
    # (x, y, z) cm, +Y 아래 → (x, z, floor − y) m, +Z 위
    assert panoptic_to_world((10.0, -150.0, 20.0)) == pytest.approx((0.1, 0.2, 1.5))
    assert panoptic_to_world((10.0, -150.0, 20.0), floor_y_cm=-5.0) == pytest.approx((0.1, 0.2, 1.45))
    assert panoptic_to_world((0.0, 0.0, 0.0))[2] == 0.0  # 바닥 y = floor_y_cm 이 World Z = 0


@pytest.mark.parametrize("floor_y_cm", [0.0, -4.0, 7.5])
def test_extrinsics_keep_projection(floor_y_cm):
    """변환한 카메라로 World 점을 투영하면 원본 카메라로 Panoptic 점을 투영한 것과 같은 픽셀이다."""
    raw = {c["name"]: c for c in _raw("calibration_mini.json")["cameras"] if c["type"] == "hd"}
    points = [(-30.0, -95.0, 20.0), (80.0, -160.0, -60.0), (5.0, -8.0, 40.0)]
    for cam in raw.values():
        R, t = cam["R"], [v[0] for v in cam["t"]]
        R_w, t_w = convert_extrinsics(R, t, floor_y_cm)
        world_cam = PinholeCamera(K=cam["K"], R=R_w, t=t_w)
        for p in points:
            xc = [sum(R[i][k] * p[k] for k in range(3)) + t[i] for i in range(3)]
            K = cam["K"]
            expected = (K[0][0] * xc[0] / xc[2] + K[0][2], K[1][1] * xc[1] / xc[2] + K[1][2])
            got = world_cam.project(panoptic_to_world(p, floor_y_cm))
            assert got == pytest.approx(expected, abs=1e-6)


def test_floor_offset_moves_cameras_and_changes_version():
    base, shifted = _calibration().current(), _calibration(floor_y_cm=-10.0).current()
    assert base["calibration_version"] != shifted["calibration_version"]
    for a, b in zip(base["cameras"], shifted["cameras"]):
        assert b["camera_height_m"] == pytest.approx(a["camera_height_m"] - 0.10, abs=1e-5)


# --------------------------------------------------------------------------- 보정 문서


def test_calibration_document():
    provider = _calibration()
    doc = provider.current()
    assert provider.camera_ids == ("hd_00_00", "hd_00_01", "hd_00_02", "hd_00_03")  # VGA 01_01 은 빠진다
    assert doc["provider_source"] == "DATASET_GT"
    assert doc["state"] == "PROVISIONAL"  # DATASET_GT 는 CALIBRATED 불가
    assert doc["scale_basis"] == "DATASET_GT"
    assert doc["created_at"] == DEFAULT_EPOCH_MS
    assert doc["coordinate_frame"]["site_id"] == "cmu_panoptic/mini"
    assert doc["calibration_version"].startswith("panoptic-mini-")
    assert all(c["camera_height_m"] == pytest.approx(2.0, abs=1e-5) for c in doc["cameras"][:3])
    assert provider.provenance() == {"source": "DATASET_GT", "calibration_version": doc["calibration_version"]}


def test_calibration_current_is_a_copy():
    provider = _calibration()
    provider.current()["state"] = "CALIBRATED"
    assert provider.current()["state"] == "PROVISIONAL"


def test_calibration_rejects_unknown_camera():
    with pytest.raises(ValueError):
        _calibration(["hd_00_00", "vga_01_01"])
    with pytest.raises(ValueError):
        _calibration(["hd_00_00", "hd_00_00"])


def test_calibration_needs_single_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        PanopticCalibrationProvider(tmp_path)
    with pytest.raises(FileNotFoundError):
        _calibration(sequence="other")


# --------------------------------------------------------------------------- World 트랙


def test_coco19_to_coco17_mapping():
    doc = next(_tracks().frames())
    body = _raw("hdPose3d_stage1_coco19/body3DScene_00000100.json")["bodies"][0]
    joints = _track(doc, "w-0000")["skeleton"]["joints"]
    for j17, j19 in enumerate(COCO17_FROM_COCO19):
        expected = panoptic_to_world(body["joints19"][4 * j19 : 4 * j19 + 3])
        assert joints[j17] == pytest.approx(list(expected), abs=1e-5)
    # 골반은 BodyCenter
    assert _track(doc, "w-0000")["pelvis_xyz"] == pytest.approx(list(panoptic_to_world(body["joints19"][8:11])))


def test_low_confidence_joints_are_null():
    tr = _track(next(_tracks().frames()), "w-0001")
    sk = tr["skeleton"]
    for j in (L_WRIST, R_EAR):
        assert sk["joints"][j] is None
        assert sk["visible"][j] is False
        assert sk["validity"][j] == "UNAVAILABLE"
    assert sum(p is None for p in sk["joints"]) == 2
    assert set(sk["joint_source"]) == {"observed"}


def test_body_without_center_is_skipped():
    docs = list(_tracks().frames())
    assert [sorted(tr["world_id"] for tr in d["tracks"]) for d in docs] == [["w-0000", "w-0001"]] * 3


def test_joints_need_two_views():
    """hd_00_03 은 다리가 화면 밖이라 2대 중 1대에서만 보인 다리 관절은 비운다."""
    sk = _track(next(_tracks(["hd_00_00", "hd_00_03"]).frames()), "w-0000")["skeleton"]
    assert all(sk["joints"][j] is None for j in LEGS)
    assert all(sk["joints"][j] is not None for j in HEAD)


def test_person_seen_by_one_camera_is_not_emitted():
    assert all(d["tracks"] == [] for d in _tracks(["hd_00_01"]).frames())


def test_no_ground_anchor_or_head_z_from_dataset():
    for doc in _tracks().frames():
        for tr in doc["tracks"]:
            assert tr["ground_anchor_xyz"] is None
            assert tr["support_surface_validity"] == "UNKNOWN"
            assert tr["contact_validity"] == "UNAVAILABLE"
            assert tr["head_z"] is None
            assert tr["position_xy"] == tr["pelvis_xyz"][:2]


def test_time_and_frame_ref_from_hd_frame_index():
    provider = _tracks()
    docs = list(provider.frames())
    assert provider.frame_indices == (100, 101, 103)
    assert [d["t"] for d in docs] == [DEFAULT_EPOCH_MS + round(i * 1001 / 30) for i in (100, 101, 103)]
    obs = _track(docs[2], "w-0000")["per_camera"][0]
    assert obs["frame_ref"] == {
        "file_uri": "volume://AP-JP-1/datasets/cmu_panoptic/mini/hdVideos/hd_00_00.mp4",
        "frame_index": 103,
    }
    assert obs["estimated_capture_time"] == docs[2]["t"]
    assert obs["timestamp_source"] == "FILE"
    assert "keypoints_2d" not in obs


def test_epoch_origin_is_configurable():
    docs = list(_tracks(epoch_ms=1_000_000).frames())
    assert docs[0]["t"] == 1_000_000 + round(100 * 1001 / 30)


def test_tracks_match_calibration():
    provider = _tracks()
    cal = provider.calibration.current()
    assert_no_violations(v for d in provider.frames() for v in check_tracks_match_calibration(d, cal))


def test_input_provenance_is_upper_bound():
    tracks = _tracks()
    frames = {"source": "DATASET", "dataset_id": "cmu_panoptic/mini"}
    prov = build_input_provenance(frames, tracks.calibration.provenance(), tracks.provenance())
    assert prov["is_upper_bound"] is True
    assert prov["tracks"] == {"source": "DATASET_GT", "provider_version": ADAPTER_VERSION}
    assert_no_violations(check_provenance_carries(prov, None, tracks.calibration, tracks))


def test_unsupported_fps_type(tmp_path):
    root = tmp_path / "seq"
    shutil.copytree(PANOPTIC_MINI, root, ignore=shutil.ignore_patterns("*.py", "__pycache__"))
    path = root / "hdPose3d_stage1_coco19" / "body3DScene_00000100.json"
    scene = json.loads(path.read_text(encoding="utf-8"))
    scene["fpsType"] = "vga_25"
    path.write_text(json.dumps(scene), encoding="utf-8")
    with pytest.raises(ValueError):
        list(_tracks(root=root).frames())


def test_missing_pose_dir(tmp_path):
    shutil.copy(PANOPTIC_MINI / "calibration_mini.json", tmp_path)
    with pytest.raises(ValueError):
        _tracks(root=tmp_path)


def test_projection_without_distortion_matches_pinhole():
    """왜곡 계수가 0 인 hd_00_03 은 geometry.PinholeCamera 투영과 같다."""
    cam = next(c for c in _calibration().projection_cameras if c.camera_id == "hd_00_03")
    p = panoptic_to_world((10.0, -150.0, 5.0))
    assert cam.project(p) == pytest.approx(cam.pinhole.project(p))
    assert cam.project(panoptic_to_world((0.0, -8.0, 0.0))) is None  # 발은 화면 밖
