"""번들 조정 수치 풀이: 해석적 야코비안과 유한 차분 비교, 결과 투영(geometry.distortion)과 잔차 식의 일치."""

from __future__ import annotations

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import solver


def _look_at(center, target):
    forward = np.asarray(target, float) - center
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    return np.vstack([right, down, forward])


def _problem(fix_scale=True, fix_floor=False, with_objects=True):
    rng = np.random.default_rng(3)
    centers = [np.array(c) for c in ([0.3, 0.3, 2.7], [8.7, 0.3, 2.6], [8.7, 7.2, 2.8], [0.3, 7.2, 2.5])]
    cams = tuple(
        solver.CameraInit(
            camera_id=f"cam{i + 1}",
            image_size=(1152, 648),
            R0=_look_at(C, [4.5, 3.75, 0.9]) @ Rotation.from_rotvec(rng.normal(scale=0.01, size=3)).as_matrix(),
            C0=C,
            f0=440.0,
            principal_point=(576.0, 324.0),
            k0=(-0.05, 0.01),
            focal_std_px=20.0,
            k_std=(0.05, 0.03),
        )
        for i, C in enumerate(centers)
    )
    P = 12
    points = np.column_stack([rng.uniform(2, 7, P), rng.uniform(2, 5.5, P), rng.uniform(0.05, 1.7, P)])
    obs_cam = np.repeat(np.arange(4), P)
    obs_pt = np.tile(np.arange(P), 4)
    obs_uv = rng.uniform(100, 600, size=(4 * P, 2))
    return solver.Problem(
        cameras=cams,
        points0=points,
        obs_cam=obs_cam,
        obs_pt=obs_pt,
        obs_uv=obs_uv,
        bone_a=np.array([0, 2, 4]),
        bone_b=np.array([1, 3, 5]),
        bone_var=np.array([0, 0, 1]),
        bone_len0=np.array([0.4, 0.3]),
        contact_pt=np.array([6, 7, 8]),
        contact_h=np.full(3, 0.02),
        object_a=np.array([9]) if with_objects else np.zeros(0, int),
        object_b=np.array([10]) if with_objects else np.zeros(0, int),
        object_len=np.array([1.0]) if with_objects else np.zeros(0),
        pixel_sigma=1.5,
        bone_sigma=0.02,
        floor_sigma=0.01,
        object_sigma_rel=0.005,
        gauge_sigma=1e-2,
        fix_scale_to_initial=fix_scale,
        fix_floor_to_initial=fix_floor,
    )


def _central_difference(fun, x, rel_step=1e-6):
    """중앙 차분 야코비안 (변수마다 h = rel_step · max(1, |x|))."""
    cols = []
    for i in range(len(x)):
        h = rel_step * max(1.0, abs(x[i]))
        xp, xm = x.copy(), x.copy()
        xp[i] += h
        xm[i] -= h
        cols.append((fun(xp) - fun(xm)) / (2 * h))
    return np.column_stack(cols)


@pytest.mark.parametrize("fix_scale,fix_floor,with_objects", [(True, False, False), (False, True, True)])
def test_analytic_jacobian_matches_finite_difference(fix_scale, fix_floor, with_objects):
    problem = _problem(fix_scale, fix_floor, with_objects)
    rng = np.random.default_rng(5)
    x = solver._initial_vector(problem)
    x[: solver.CAM_PARAMS * 4] += rng.normal(scale=0.02, size=solver.CAM_PARAMS * 4) * np.tile(
        [1, 1, 1, 1, 1, 1, 100, 1, 1], 4
    )
    analytic = solver.jacobian(problem, x).toarray()
    numeric = _central_difference(lambda v: solver.residuals(problem, v), x)
    assert analytic.shape == numeric.shape
    scale = np.maximum(1.0, np.abs(numeric))
    assert np.max(np.abs(analytic - numeric) / scale) < 1e-4


def test_residual_projection_matches_geometry_distortion():
    problem = _problem()
    x = solver._initial_vector(problem)
    solution, _ = solver.solve(problem, max_iterations=2, x0=x)
    # 해 그대로 재투영 잔차(잔차 식)와 결과 투영(geometry.distortion.distort)이 같은 값이어야 한다
    x_sol = np.concatenate(
        [
            np.concatenate([np.concatenate([np.zeros(3), c.C, [c.f], c.k]) for c in solution.cameras]),
            solution.bone_lengths,
            solution.points.ravel(),
        ]
    )
    rebased = solver.rebase(problem, solution)
    r = solver.residuals(rebased, x_sol)[: 2 * len(problem.obs_cam)].reshape(-1, 2) * problem.pixel_sigma
    errors = solution.reprojection_errors(problem)
    assert np.allclose(np.linalg.norm(r, axis=1), errors, atol=1e-6)


def test_drop_observations_removes_points_seen_once():
    problem = _problem()
    keep = np.ones(len(problem.obs_cam), bool)
    keep[(problem.obs_pt == 0) & (problem.obs_cam > 0)] = False  # 점 0 은 1대에서만 남는다
    dropped, old_of_new = problem.drop_observations(keep)
    assert 0 not in old_of_new
    assert dropped.n_points == problem.n_points - 1
    assert len(dropped.bone_a) == len(problem.bone_a) - 1  # 점 0 을 쓰던 뼈가 빠진다
    assert np.all(np.bincount(dropped.obs_pt) >= 2)
