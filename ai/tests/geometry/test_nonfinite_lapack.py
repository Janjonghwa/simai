"""유한하지 않은 입력이 LAPACK 호출에서 멈추지 않고 "해 없음"이 되는지 본다 (S15P31A101-245, 명세 5.2).

Linux OpenBLAS 의 SVD 는 ±inf 가 든 행렬에서 끝나지 않는다(NaN 은 LinAlgError). Tier 0 기하는 SVD 앞에서 유한하지 않은
계수를 걸러 그 점을 해 없음(NaN·ok False)으로 둔다. 걸러지는 경로: 묶음 DLT(`triangulation._dlt_batch`), Cross-view 2-view
DLT(`cross_view._two_view_arrays`), 삼각측량 공분산의 조건수·고윳값(`joint_gate.triangulation_covariances`), 카메라 모델의
행렬 노름과 왜곡 가지 다항식의 근(`RigCamera`, 보정 값·왜곡 계수 검사). 유한한 행의 결과는 유한한 행만 넣었을 때와 비트 단위로 같아야 한다.

시험 본문(`_check_*`)은 새 프로세스에서 시간 제한을 두고 돈다(`bounded.run_bounded`). 거르기를 빼면 Linux 에서는 시간 초과로,
Cross-view·공분산의 NaN 입력은 어느 OS 에서나 LinAlgError 로 실패한다.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from simai_ai.geometry import distortion
from simai_ai.geometry import joint_gate as joint_gate_module
from simai_ai.geometry import triangulation as tri
from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.tracking import cross_view as cross_view_module

HERE = Path(__file__).resolve().parent


def _load(name: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


bench = _load("bench_tier0_geometry")
bounded = _load("bounded")

POINTS = np.array([[4.5, 3.75, 0.9], [2.0, 5.0, 1.7], [6.5, 2.0, 0.1], [3.0, 3.0, 1.2], [5.5, 6.0, 0.5]])


def _same_bits(a: np.ndarray, b: np.ndarray) -> None:
    """비트 단위로 같다(`assert_array_equal` 은 +0 과 −0 을 같다고 본다)."""
    a, b = np.asarray(a), np.asarray(b)
    assert a.shape == b.shape and a.dtype == b.dtype and a.tobytes() == b.tobytes()


def _cameras(n: int = 2):
    rig = bench.make_rig()
    return [rig.cameras[f"cam{k}"] for k in range(1, n + 1)]


def _normalized(cams, points=POINTS) -> np.ndarray:
    """(M, C, 2) 점마다 카메라마다의 정규화 좌표."""
    out = []
    for cam in cams:
        xc = cam.to_camera(points)
        out.append(xc[:, :2] / xc[:, 2:3])
    return np.stack(out, axis=1)


def _poison(xn: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """유한한 행 사이에 inf·−inf·NaN 이 섞인 행을 끼운 묶음과 유한한 행 표시."""
    bad = np.repeat(xn[:1], 6, axis=0)
    bad[0, 0, 0] = math.inf
    bad[1, -1, 1] = -math.inf
    bad[2, 0, 1] = math.nan
    bad[3, :, :] = math.inf  # 모든 카메라
    bad[4, 0, 0], bad[4, -1, 1] = math.inf, math.nan  # inf 와 NaN 이 한 행에
    bad[5, 0, 1] = -math.inf
    rows = np.concatenate([xn[:2], bad[:3], xn[2:4], bad[3:], xn[4:]])
    return rows, np.isfinite(rows).all(axis=(1, 2))


def _check_mixed_batch():
    """inf·−inf·NaN 행이 섞인 묶음(카메라 2·4대): 그 행만 해가 없고, 유한한 행은 유한한 행만 넣었을 때와 비트 단위로 같다."""
    for n in (2, 4):
        cams = _cameras(n)
        rows, finite = _poison(_normalized(cams))
        with np.errstate(all="ignore"):
            got = tri.triangulate_normalized_many(cams, rows)
            alone = tri.triangulate_normalized_many(cams, rows[finite])
            solvable = tri.solvable_normalized_many(cams, rows)
            singles = [tri.triangulate_normalized(cams, rows[m]) for m in np.flatnonzero(~finite)]
        _same_bits(got[finite], alone)
        assert np.isfinite(alone).all()
        assert np.isnan(got[~finite]).all()
        assert solvable.tolist() == finite.tolist()
        assert singles == [None] * len(singles)


def _check_all_nonfinite():
    """묶음 전체가 유한하지 않다(inf 만, −inf 만, NaN 만, 섞임): 모든 행이 해 없음이고 예외로 멈추지 않는다."""
    cams = _cameras(2)
    xn = _normalized(cams)
    for value in (math.inf, -math.inf, math.nan, None):
        rows = xn.copy()
        if value is None:
            rows[0::3, 0, 0], rows[1::3, 1, 1], rows[2::3, 0, 1] = math.inf, -math.inf, math.nan
        else:
            rows[:, 0, 0] = value
        with np.errstate(all="ignore"):
            got = tri.triangulate_normalized_many(cams, rows)
            solvable = tri.solvable_normalized_many(cams, rows)
        assert np.isnan(got).all() and not solvable.any(), value
    # 픽셀 입력(왜곡 제거 단계에서 이미 걸러진다)도 같은 답이다
    assert tri.triangulate_point(cams, np.array([[math.inf, 10.0], [100.0, 200.0]])) is None


def _check_one_camera_nonfinite():
    """한 카메라만 유한하지 않다: 그 카메라를 쓴 묶음 풀이는 해 없음, 그 카메라 가중치가 넘치는 큰 유한값(1e308)이어도
    해 없음이다(계수 행렬이 inf). 사람 단위 경로에서는 그 카메라만 빠지고 나머지 카메라 결과는 그 카메라가 없을 때와 같다."""
    for n in (2, 4):
        cams = _cameras(n)
        xn = _normalized(cams)
        weights = np.ones(xn.shape[:2])
        weights[:, 1] = 1e308
        with np.errstate(all="ignore"):
            assert np.isnan(tri.triangulate_normalized_many(cams, xn, weights)).all()
            for cam, part in ((0, 0), (n - 1, slice(None))):  # cam1 의 x 만 inf, 마지막 카메라의 x·y 모두 inf
                rows = xn.copy()
                rows[:, cam, part] = math.inf
                assert np.isnan(tri.triangulate_normalized_many(cams, rows)).all(), (n, cam)
                assert not tri.solvable_normalized_many(cams, rows).any(), (n, cam)
    # 사람 단위: cam3 keypoint 는 모두 inf, cam2 신뢰도는 1e308. cam3 은 관측으로 세지 않고, cam2 는 해가 없어 빠진다
    rig = bench.make_rig()
    joints = np.array([POINTS[0] + [0.0, 0.0, 0.05 * j] for j in range(17)])
    views, ref_views = [], []
    for cam in rig.cameras.values():
        px, ok = bench.project(cam, joints)
        kps = [(float(u), float(v), 0.9) if good else None for (u, v), good in zip(px, ok)]
        if cam.camera_id == "cam3":
            views.append(_view(cam.camera_id, [(math.inf, math.inf, 0.9)] * 17))
            continue
        if cam.camera_id == "cam2":
            views.append(_view(cam.camera_id, [None if k is None else (k[0], k[1], 1e308) for k in kps]))
            continue
        views.append(_view(cam.camera_id, kps))
        ref_views.append(_view(cam.camera_id, kps))
    with np.errstate(all="ignore"):
        got = tri.triangulate_skeleton(views, rig)
        ref = tri.triangulate_skeleton(ref_views, rig)
    assert [j.xyz for j in got.joints] == [j.xyz for j in ref.joints]
    assert all("cam3" not in j.camera_ids and "cam2" not in j.camera_ids for j in got.joints)
    assert any("cam2" in j.rejected_camera_ids for j in got.joints)


def _view(camera_id, keypoints):
    return InterpolatedView(camera_id, bench.T0, tuple(keypoints), "SIM", 0.0, (bench.T0,))


def _check_cross_view_two_view():
    """Cross-view 2-view 연결 삼각측량: 유한하지 않은 관측의 관절은 XYZ NaN·재투영 오차 inf(해 없음)이고 예외(NaN 의
    LinAlgError)나 멈춤이 없다. 유한한 관절은 유한한 관절만 넣었을 때와 비트 단위로 같다."""
    a, b = _cameras(2)
    rows, finite = _poison(_normalized([a, b]))
    w = np.full(len(rows), 0.8)
    w[1] = 1e308  # 넘치는 가중치: 계수 행렬이 inf
    finite[1] = False
    with np.errstate(all="ignore"):
        sides = [(rows[:, s], w, cam.ideal_pixels(rows[:, s])) for s, cam in enumerate((a, b))]
        xyz, err = cross_view_module._two_view_arrays(a, b, *sides)
        alone = [(xn[finite], wt[finite], tg[finite]) for xn, wt, tg in sides]
        xyz_ok, err_ok = cross_view_module._two_view_arrays(a, b, *alone)
    _same_bits(xyz[finite], xyz_ok)
    _same_bits(err[finite], err_ok)
    assert np.isfinite(xyz_ok).all()
    assert np.isnan(xyz[~finite]).all() and np.isinf(err[~finite]).all()


def _check_covariance_overflow():
    """카메라 중심에 붙은 점(카메라 z 1e-170)은 정보 행렬이 넘쳐 inf·NaN 이다. 그 점은 공분산을 구하지 못한 점(NaN, ok False)
    이고 조건수(SVD)에서 멈추거나 LinAlgError 로 끝나지 않는다. 다른 점은 그 점이 없을 때와 비트 단위로 같다.

    중심이 World 원점인 카메라를 쓴다(중심 좌표가 1 m 규모면 1e-170 을 더해도 같은 수라 z 가 그만큼 작아지지 않는다)."""
    doc = bench.calibration_doc()["cameras"][0]
    origin = RigCamera.from_calibration({**doc, "camera_id": "origin", "R": np.eye(3).tolist(), "t": [0.0, 0.0, 0.0]})
    cams = [origin, _cameras(2)[1]]
    near = np.array([1e-171, 2e-171, 1e-170])
    X = np.array([POINTS[0], near, POINTS[1]])
    mask = np.ones((3, 2), dtype=bool)
    with np.errstate(all="ignore"):
        cov, ok = joint_gate_module.triangulation_covariances(cams, mask, X, 2.0)
        cov_ok, ok_ok = joint_gate_module.triangulation_covariances(cams, mask[[0, 2]], X[[0, 2]], 2.0)
        single = joint_gate_module.triangulation_covariance(cams, near, 2.0)
    assert ok.tolist() == [True, False, True] and ok_ok.all()
    _same_bits(cov[[0, 2]], cov_ok)
    assert np.isnan(cov[1]).all() and single is None


def _check_covariance_inverse_overflow():
    """정보 행렬이 유한하고 조건수도 작지만 아주 작으면(초점거리 1e-154) 역행렬이 넘친다. 그 점도 공분산을 구하지 못한
    점(NaN, ok False, 표준편차 inf)이고 eigvalsh 에 넣지 않는다(넣으면 LinAlgError, 또는 NaN 표준편차로 게이트 문턱 비교가
    거짓이 된다)."""
    doc = bench.calibration_doc(())
    f = 1e-154
    for cam in doc["cameras"]:
        cam["K"] = [[f, 0.0, cam["K"][0][2]], [0.0, f, cam["K"][1][2]], [0.0, 0.0, 1.0]]
    rig = CameraRig.from_provider(bench._DictProvider(doc))
    cams = [rig.cameras["cam1"], rig.cameras["cam2"]]
    with np.errstate(all="ignore"):
        cov, ok = joint_gate_module.triangulation_covariances(cams, np.ones((1, 2), dtype=bool), POINTS[:1], 2.0)
    assert not ok[0] and np.isnan(cov[0]).all()
    joints = [tri.JointEstimate(tuple(map(float, POINTS[0])), tri.JointValidity.VALID, ("cam1", "cam2"))]
    joints += [tri.JointEstimate(None, tri.JointValidity.UNAVAILABLE)] * 16
    skeleton = tri.SkeletonEstimate(bench.T0, "COCO17", tuple(joints), rig.calibration_version)
    with np.errstate(all="ignore"):
        sigma_xyz, sigma_max = joint_gate_module._position_sigmas(skeleton, rig, 2.0)
    assert sigma_max[0] == math.inf and sigma_xyz[0] == (math.inf, math.inf, math.inf)


def _check_distortion_rejects_nonfinite():
    """왜곡 계수가 유한하지 않거나(NaN·±inf) 너무 커서 가지 다항식 계수가 넘치면(k4 1e200) 카메라 모델을 만들지 않는다
    (ValueError). 동반 행렬 고윳값(Polynomial.roots → LAPACK dgeev)에 넣지 않는다. 왜곡 제거 허용치 inf 도 거절한다."""
    doc = bench.calibration_doc()["cameras"][0]
    for coeffs in ([math.nan, 0.0, 0.0, 0.0, 0.0], [-0.3, math.inf, 0.0, 0.0, 0.0], [0.0, 0.0, 0.0, -math.inf]):
        model = "OPENCV_FISHEYE" if len(coeffs) == 4 else "OPENCV_PINHOLE"
        with pytest.raises(ValueError, match="유한"):
            RigCamera.from_calibration({**doc, "distortion": {"model": model, "coeffs": coeffs}})
    huge = [-0.3, 0.09, 0.0, 0.0, 0.0, 1e200, 0.0, 0.0]
    with pytest.raises(ValueError, match="너무 커"):
        RigCamera.from_calibration({**doc, "distortion": {"model": "OPENCV_PINHOLE", "coeffs": huge}})
    with pytest.raises(ValueError, match="너무 커"), np.errstate(all="ignore"):
        distortion.radial_branch("OPENCV_PINHOLE", huge)
    with pytest.raises(ValueError, match="유한한 양수"):
        RigCamera.from_calibration(doc, undistort_tolerance_px=math.inf)


def _check_rig_rejects_nonfinite():
    """보정 값(K·R·t)에 유한하지 않은 값이 있으면 카메라 모델을 만들지 않는다(ValueError). 행렬 노름 SVD 앞에서 막는다."""
    doc = bench.calibration_doc()["cameras"][0]
    for key, i, j, value in (("K", 0, 0, math.inf), ("K", 0, 1, math.nan), ("K", 0, 2, -math.inf), ("R", 1, 1, math.nan)):
        bad = {**doc, key: [list(row) for row in doc[key]]}
        bad[key][i][j] = value
        with pytest.raises(ValueError, match="유한"):
            RigCamera.from_calibration(bad)
        with pytest.raises(ValueError, match="유한"):
            PinholeCamera.from_calibration(bad)
    with pytest.raises(ValueError, match="유한"):
        RigCamera.from_calibration({**doc, "t": [0.0, math.inf, 1.0]})
    c = 2**-0.5  # R·t 는 유한하지만 중심 −R⁻¹·t 가 넘친다
    with pytest.raises(ValueError, match="중심"):
        RigCamera.from_calibration({**doc, "R": [[c, -c, 0.0], [c, c, 0.0], [0.0, 0.0, 1.0]], "t": [1.7e308, 1.7e308, -2.7]})
    RigCamera.from_calibration(doc)  # 정상 값은 그대로 만든다


@pytest.mark.parametrize(
    "check",
    [
        "_check_mixed_batch",
        "_check_all_nonfinite",
        "_check_one_camera_nonfinite",
        "_check_cross_view_two_view",
        "_check_covariance_overflow",
        "_check_covariance_inverse_overflow",
        "_check_distortion_rejects_nonfinite",
        "_check_rig_rejects_nonfinite",
    ],
)
def test_nonfinite_inputs_finish_without_solution(check):
    bounded.run_bounded(__file__, check)
