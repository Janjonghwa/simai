"""cameras.json(OpenCV cam2world) → World → 카메라 (R, t) 변환과 단위·축 확인, World 정렬, 내부 파라미터 기록 읽기 (-214)."""

from __future__ import annotations

import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.extrinsics.initial import (
    SOURCE_PROVIDED,
    WorldAlignment,
    cam2world_to_extrinsics,
    checked_provenance,
    combine_input_provenance,
    extrinsics_to_cam2world,
    load_mapanything_cameras,
    load_world_alignment,
)
from simai_ai.calibration.intrinsics import (
    IntrinsicsEstimate,
    IntrinsicsUncertainty,
    build_intrinsics_record,
    bundle_init_from_record,
    combine_shared_lens,
    to_bundle_adjustment_init,
)
from simai_ai.calibration.intrinsics.estimate import radial_pixel_from_opencv

GRID = np.array([[x, y, z] for x in (0.0, 3.0, 7.5) for y in (0.5, 4.0) for z in (0.0, 1.7)])


def _look(center, target):
    f = np.subtract(target, center) / np.linalg.norm(np.subtract(target, center))
    r = np.cross(f, [0, 0, 1.0])
    r /= np.linalg.norm(r)
    R = np.vstack([r, np.cross(f, r), f])
    return R, -R @ np.asarray(center, dtype=float)


WORLD = {
    "cam_A": _look([0.3, 0.3, 2.7], [4, 3, 0.9]),
    "cam_B": _look([8.0, 0.3, 2.6], [4, 3, 0.9]),
    "cam_C": _look([8.0, 6.0, 2.8], [4, 3, 0.9]),
}
# MapAnything world = cam_A 의 OpenCV 축(첫 영상), 스케일 1.03 (모델 메트릭 오차 흉내)
SCALE_M = 1.03


def _mapanything_views():
    RA, tA = WORLD["cam_A"]
    views = []
    for cid, (R, t) in WORLD.items():
        Rm = R @ RA.T
        tm = SCALE_M * (t - Rm @ tA)
        views.append({"name": f"{cid}_01", "cam2world": extrinsics_to_cam2world(Rm, tm).tolist()})
    alignment = np.eye(4)  # X_world = (1/s) RA^T (X_m) − RA^T tA
    alignment[:3, :3] = RA.T / SCALE_M
    alignment[:3, 3] = -RA.T @ tA
    return views, alignment


def _write_cameras(tmp_path, views, convention="cam2world 4x4, OpenCV, MapAnything world, m (NOT_VERIFIED)"):
    path = tmp_path / "cameras.json"
    path.write_text(json.dumps({"run_id": "t", "convention": convention, "views": views}), encoding="utf-8")
    return path


def _project(R, t, X):
    x = (np.asarray(R) @ X.T).T + np.asarray(t)
    return x[:, :2] / x[:, 2:]


def test_cam2world_round_trip_on_grid():
    for R, t in WORLD.values():
        R2, t2 = cam2world_to_extrinsics(extrinsics_to_cam2world(R, t))
        assert np.allclose(R2, R) and np.allclose(t2, t)
        # 카메라 중심은 cam2world 이동 성분이다(world2cam 으로 잘못 읽으면 여기서 틀린다)
        assert np.allclose(extrinsics_to_cam2world(R, t)[:3, 3], -R.T @ t)


def test_mapanything_plus_alignment_projects_world_grid_like_truth(tmp_path):
    views, alignment = _mapanything_views()
    cams = load_mapanything_cameras(_write_cameras(tmp_path, views))
    assert set(cams.extrinsics) == set(WORLD)  # cam_A_01 → cam_A
    path = tmp_path / "align.json"
    path.write_text(json.dumps({"format": "simai.world_alignment", "mapanything_to_world": alignment.tolist()}))
    align = load_world_alignment(path)
    assert align.source == SOURCE_PROVIDED and align.scale == pytest.approx(1 / SCALE_M)
    world = align.apply(cams.extrinsics)
    for cid, (R, t) in WORLD.items():
        Rw, tw = world[cid]
        assert np.allclose(_project(Rw, tw, GRID), _project(R, t, GRID), atol=1e-9)
        # 미터 World 의 카메라 중심도 정답과 같다(스케일을 되돌렸다)
        assert np.allclose(-np.asarray(Rw).T @ tw, -R.T @ t, atol=1e-9)


def test_world_alignment_identity_keeps_extrinsics():
    R, t = WORLD["cam_B"]
    same = WorldAlignment(np.eye(3), np.zeros(3), 1.0, SOURCE_PROVIDED).apply({"cam_B": (R, t)})
    assert np.allclose(same["cam_B"][0], R) and np.allclose(same["cam_B"][1], t)


@pytest.mark.parametrize(
    "convention",
    ["world2cam 4x4, OpenCV, m", "cam2world 4x4, OpenGL, m", ""],
)
def test_unknown_convention_is_rejected(tmp_path, convention):
    views, _ = _mapanything_views()
    with pytest.raises(ValueError, match="convention"):
        load_mapanything_cameras(_write_cameras(tmp_path, views, convention))


def test_millimetre_or_normalized_units_are_rejected(tmp_path):
    views, _ = _mapanything_views()
    for factor in (1000.0, 0.01):
        scaled = [dict(v, cam2world=(np.diag([1, 1, 1, 1.0]) @ np.array(v["cam2world"])).tolist()) for v in views]
        for v in scaled:
            v["cam2world"][0][3] *= factor
            v["cam2world"][1][3] *= factor
            v["cam2world"][2][3] *= factor
        with pytest.raises(ValueError, match="미터"):
            load_mapanything_cameras(_write_cameras(tmp_path, scaled))


def test_non_rotation_and_ambiguous_views_are_rejected(tmp_path):
    views, _ = _mapanything_views()
    bad = [dict(v) for v in views]
    T = np.array(bad[1]["cam2world"])
    T[:3, :3] *= np.diag([1, 1, -1])  # 왼손(det −1)
    bad[1]["cam2world"] = T.tolist()
    with pytest.raises(ValueError, match="정규직교"):
        load_mapanything_cameras(_write_cameras(tmp_path, bad))
    dup = views + [dict(views[0], name="cam_A_02")]
    with pytest.raises(ValueError, match="view 가 여럿"):
        load_mapanything_cameras(_write_cameras(tmp_path, dup))
    picked = load_mapanything_cameras(_write_cameras(tmp_path, dup), {"cam_A": "cam_A_02", "cam_B": "cam_B_01"})
    assert set(picked.extrinsics) == {"cam_A", "cam_B"}


def test_left_handed_alignment_is_rejected(tmp_path):
    path = tmp_path / "align.json"
    path.write_text(json.dumps({"format": "simai.world_alignment", "mapanything_to_world": np.diag([1, 1, -1, 1.0]).tolist()}))
    with pytest.raises(ValueError, match="det"):
        load_world_alignment(path)


# --------------------------------------------------------------------------- 내부 파라미터 기록


def _estimates(f=1259.0, k=(-0.27, 0.046), size=(2304, 1296), focals=(0.0, 0.002, -0.003, 0.2)):
    out = []
    for i, d in enumerate(focals):
        fi = f * (1 + d)
        coeffs = (k[0], k[1], 0.0, 0.0, 0.0)
        out.append(
            IntrinsicsEstimate(
                camera_id=f"cam_{'ABCD'[i]}",
                image_size=size,
                focal_px=fi,
                distortion_coeffs=coeffs,
                radial_pixel=radial_pixel_from_opencv(coeffs, fi, size),
                uncertainty=IntrinsicsUncertainty(),
                method="ANYCALIB",
                method_version="t",
                status="OK",
                principal_point=(1151.0, 649.0),
                principal_point_assumed=False,
            )
        )
    return out


def test_bundle_init_from_record_matches_in_memory_init():
    estimates = _estimates()
    shared = combine_shared_lens(estimates)
    record = json.loads(json.dumps(build_intrinsics_record(created_at=1_792_552_246_775, frames={"source": "LIVE"},
                                                           estimates=estimates, shared=shared)))
    expected = to_bundle_adjustment_init(shared, record)
    got = bundle_init_from_record(record)
    assert [c.camera_id for c in got.cameras] == [c.camera_id for c in expected.cameras]
    for a, b in zip(got.cameras, expected.cameras, strict=True):
        assert a.camera == b.camera
        assert a.focal_prior_std_px == pytest.approx(b.focal_prior_std_px)
        assert a.k_prior_std == pytest.approx(b.k_prior_std)
        assert a.flags == b.flags
    assert "OUTLIER_CAMERA" in got.camera("cam_D").flags  # 초점 20% 다른 카메라는 사전 분포가 넓다


def test_record_without_consensus_gives_no_initial():
    estimates = _estimates(focals=(0.0, 0.0, 0.3, 0.3))
    record = build_intrinsics_record(created_at=1_792_552_246_775, frames={"source": "LIVE"}, estimates=estimates,
                                     shared=combine_shared_lens(estimates))
    with pytest.raises(ValueError, match="공통"):
        bundle_init_from_record(record)
    with pytest.raises(ValueError, match="형식"):
        bundle_init_from_record({**record, "format": "other"})


def test_rotation_helper_sanity():
    # 작은 회전 잡음(float32 수준)은 받아들이고 정규직교로 다듬는다
    T = extrinsics_to_cam2world(*WORLD["cam_C"])
    T[:3, :3] = T[:3, :3] @ Rotation.from_rotvec([1e-5, 0, 0]).as_matrix() + 1e-5
    R, _ = cam2world_to_extrinsics(T)
    assert np.allclose(R @ R.T, np.eye(3), atol=1e-12)


# --------------------------------------------------------------------------- 출처 합치기


def test_combine_input_provenance_is_conservative():
    kp = {"frames": {"source": "FILE"}, "is_upper_bound": False}
    prov, rec = combine_input_provenance(kp, {"intrinsics": None, "measured_objects": None}, "mapanything-r1")
    assert prov == {"frames": {"source": "FILE"}, "calibration": {"source": "MODEL_ESTIMATE",
                    "calibration_version": "mapanything-r1"}, "is_upper_bound": False}
    gt = {"frames": {"source": "DATASET"}, "calibration": {"source": "DATASET_GT", "calibration_version": "gt"},
          "is_upper_bound": True}
    prov, rec = combine_input_provenance(kp, {"measured_objects": gt}, "mapanything-r1")
    assert prov["is_upper_bound"] is True and prov["calibration"]["source"] == "DATASET_GT"
    # SIM 과 DATASET_GT 가 섞이면 SIM 이다(시뮬레이터 결과로 표시)
    sim = {"frames": {"source": "SIM"}, "is_upper_bound": True}
    prov, rec = combine_input_provenance(kp, {"measured_objects": gt, "intrinsics": sim}, "mapanything-r1")
    assert prov["calibration"]["source"] == "SIM" and rec["upper_bound_inputs"] == {"measured_objects": "DATASET_GT",
                                                                                   "intrinsics": "SIM"}
    with pytest.raises(ValueError, match="input_provenance"):
        checked_provenance({"frames": {"source": "SIM"}, "is_upper_bound": False}, "x.json")  # 상한 표시가 틀림
