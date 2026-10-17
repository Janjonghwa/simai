"""S15P31A101-168 이전(관절·카메라마다 1점씩 왜곡 제거) 삼각측량·관절 게이트 구현의 고정 사본. 회귀 비교 전용이다.

`test_batch_undistort_regression.py` 가 묶음 왜곡 제거 구현과 결과가 같은지 비교하는 기준이다. 고치지 않는다.
원본: dev 95ba73d 의 `geometry/triangulation.py`, `geometry/joint_gate.py`, `tracking/cross_view.py` (함수 본문을 그대로
옮겼다. Cross-view 의 메서드는 self 를 첫 인자로 받는 함수로 옮겼다: `cross_view_pair`, `cross_view_epipolar_px`,
`cross_view_assign`, `cross_view_validate`, `cross_view_two_view_triangulation`).
"""

# ruff: noqa
from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import InterpolatedView, Keypoint
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.geometry.triangulation import (
    DLT_RANK_TOLERANCE,
    JOINT_COUNTS,
    MIN_VIEWS,
    UNTRUSTED_CALIBRATION_STATES,
    JointEstimate,
    JointIssue,
    JointValidity,
    SkeletonEstimate,
    TriangulationConfig,
)
from simai_ai.geometry.joint_gate import JointGateConfig
from simai_ai.tracking.cross_view import (
    PairEvidence,
    RejectReason,
    TrackKey,
    UncertainReason,
    _block_key,
    _Flag,
    _floor_gap,
    _focal,
    _Prepared,
    _Work,
)


def triangulate_skeleton(
    views: Mapping[str, InterpolatedView] | Sequence[InterpolatedView],
    rig: CameraRig,
    joint_layout: str = "COCO17",
    config: TriangulationConfig = TriangulationConfig(),
) -> SkeletonEstimate:
    """같은 사람의 카메라별 보간 keypoint 로 관절 3D 를 만든다. 모든 view 는 같은 공통 시각이어야 한다."""
    view_list = list(views.values()) if isinstance(views, Mapping) else list(views)
    if not view_list:
        raise ValueError("view 가 없다")
    n_joints = JOINT_COUNTS.get(joint_layout)
    if n_joints is None:
        raise ValueError(f"지원하지 않는 관절 배치: {joint_layout}")
    times = {v.t for v in view_list}
    if len(times) > 1:
        raise ValueError(f"공통 시각이 다른 view 가 섞여 있다: {sorted(times)}")
    ids = [v.camera_id for v in view_list]
    if len(ids) != len(set(ids)):
        raise ValueError(f"같은 카메라의 관측이 두 번 있다 (한 사람에 같은 카메라의 두 검출을 묶지 않는다): {ids}")
    for v in view_list:
        if len(v.keypoints) != n_joints:
            raise ValueError(f"{v.camera_id}: {joint_layout} 는 keypoint {n_joints}개인데 {len(v.keypoints)}개다")
    cameras = rig.subset(ids)
    trusted = rig.state not in UNTRUSTED_CALIBRATION_STATES

    usable = [_usable_keypoints(cam, v.keypoints, config) for cam, v in zip(cameras, view_list)]
    joints = []
    for j in range(n_joints):
        obs = [(cam, v.keypoints[j]) for cam, v, ok in zip(cameras, view_list, usable) if ok[j]]
        joints.append(_triangulate_joint(obs, trusted, config))
    used = sorted({cid for j in joints for cid in j.camera_ids})
    return SkeletonEstimate(
        t=view_list[0].t,
        joint_layout=joint_layout,
        joints=tuple(joints),
        calibration_version=rig.calibration_version,
        camera_ids=tuple(used),
    )


def triangulate_point(
    cameras: Sequence[RigCamera], pixels: np.ndarray, weights: np.ndarray | None = None, refine_iterations: int = 10
) -> np.ndarray | None:
    """원본 픽셀 (N, 2) 관측으로 World 점 하나를 푼다. 해가 없거나 어느 카메라 뒤에 있으면 None.

    픽셀·가중치가 유한하지 않거나 왜곡을 제거할 수 없는 관측이 하나라도 있으면 None 이다(어느 관측을 뺄지는 호출자 몫).
    """
    if len(cameras) < MIN_VIEWS:
        raise ValueError(f"삼각측량에는 카메라가 {MIN_VIEWS}대 이상 필요하다: {len(cameras)}대")
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    w = np.ones(len(cameras)) if weights is None else np.asarray(weights, dtype=float)
    normalized = np.vstack([cam.normalize(px) for cam, px in zip(cameras, pixels)])
    if not (np.all(np.isfinite(normalized)) and np.all(np.isfinite(w))):
        return None
    X = _dlt(cameras, normalized, w)
    if X is None or not _in_front(cameras, X):
        return None
    targets = np.vstack([cam.ideal_pixels(n) for cam, n in zip(cameras, normalized)])
    X = _refine(cameras, targets, w, X, refine_iterations)
    if not _in_front(cameras, X):
        return None
    return X


def reprojection_errors(cameras: Sequence[RigCamera], pixels: np.ndarray, X: np.ndarray) -> np.ndarray:
    """카메라별 재투영 오차(왜곡 제거 픽셀). 카메라 뒤이거나 그 관측의 왜곡을 제거할 수 없으면 inf."""
    pixels = np.asarray(pixels, dtype=float).reshape(-1, 2)
    out = np.empty(len(cameras))
    for i, (cam, px) in enumerate(zip(cameras, pixels)):
        xc = cam.to_camera(X)[0]
        if xc[2] <= 0:
            out[i] = math.inf
            continue
        target = cam.ideal_pixels(cam.normalize(px))[0]
        proj = cam.ideal_pixels(xc[:2] / xc[2])[0]
        err = float(np.linalg.norm(proj - target))
        out[i] = err if math.isfinite(err) else math.inf
    return out


def max_ray_angle(cameras: Sequence[RigCamera], X: np.ndarray) -> float:
    """카메라 쌍마다 카메라 중심과 X 를 잇는 시선 직선 사이 각(0~π/2)의 최댓값(라디안).

    시선은 방향이 없는 직선으로 본다. 서로 마주 보는 두 카메라의 반대 방향 시선(180° 근처)도 같은 방향(0° 근처)처럼
    거의 같은 직선이라 깊이가 불안정하다.
    """
    dirs = [(X - cam.center) / np.linalg.norm(X - cam.center) for cam in cameras]
    best = 0.0
    for a, b in itertools.combinations(dirs, 2):
        best = max(best, math.acos(float(np.clip(abs(a @ b), 0.0, 1.0))))
    return best


def _usable_keypoints(cam: RigCamera, keypoints: Sequence[Any], config: TriangulationConfig) -> list[bool]:
    """관절마다 관측으로 셀 수 있는지: 값이 있고 (u, v, 신뢰도) 모두 유한하고 신뢰도 기준 이상이며 왜곡을 제거할 수 있다."""
    candidate = []
    pixels = np.zeros((len(keypoints), 2))
    for j, kp in enumerate(keypoints):
        ok = kp is not None and all(math.isfinite(float(x)) for x in kp[:3]) and kp[2] >= config.min_keypoint_confidence
        candidate.append(ok)
        if ok:
            pixels[j] = kp[:2]
    if not any(candidate):
        return candidate
    _, undistorted = cam.normalize_with_mask(pixels)
    return [bool(c and u) for c, u in zip(candidate, undistorted)]


def _triangulate_joint(
    obs: list[tuple[RigCamera, tuple[float, float, float]]], trusted_calibration: bool, config: TriangulationConfig
) -> JointEstimate:
    if len(obs) < config.min_views:
        return JointEstimate(None, JointValidity.UNAVAILABLE, issues=(JointIssue.TOO_FEW_VIEWS,))
    rejected: list[str] = []
    while True:
        cameras = [cam for cam, _ in obs]
        pixels = np.array([kp[:2] for _, kp in obs])
        weights = np.array([kp[2] for _, kp in obs])
        X = triangulate_point(cameras, pixels, weights, config.refine_iterations)
        errors = reprojection_errors(cameras, pixels, X) if X is not None else None
        if errors is not None and float(errors.max()) > config.outlier_reprojection_error_px:
            if len(obs) == config.min_views:
                # 더 빼면 최소 관측 수 미만이다. 이상치 관측을 세어 좌표를 남기지 않는다
                return JointEstimate(
                    None,
                    JointValidity.UNAVAILABLE,
                    rejected_camera_ids=(*rejected, *(cam.camera_id for cam in cameras)),
                    issues=(JointIssue.INCONSISTENT_VIEWS,),
                )
            worst = int(np.argmax(errors))
        elif X is None and len(obs) > config.min_views:
            worst = _worst_view(obs, config)  # 해가 없거나 어느 카메라 뒤에 있다
        else:
            break
        rejected.append(obs[worst][0].camera_id)
        obs = obs[:worst] + obs[worst + 1 :]
    if X is None or errors is None:
        return JointEstimate(
            None,
            JointValidity.UNAVAILABLE,
            rejected_camera_ids=tuple(rejected),
            issues=(JointIssue.DEGENERATE,),
        )
    reproj = float(errors.max())
    angle = max_ray_angle(cameras, X)
    issues = []
    if reproj > config.max_reprojection_error_px:
        issues.append(JointIssue.HIGH_REPROJECTION_ERROR)
    if angle < config.min_ray_angle_rad:
        issues.append(JointIssue.NARROW_RAY_ANGLE)
    if not trusted_calibration:
        issues.append(JointIssue.UNTRUSTED_CALIBRATION)
    return JointEstimate(
        xyz=(float(X[0]), float(X[1]), float(X[2])),
        validity=JointValidity.UNCERTAIN if issues else JointValidity.VALID,
        camera_ids=tuple(cam.camera_id for cam in cameras),
        rejected_camera_ids=tuple(rejected),
        reprojection_error_px=reproj,
        ray_angle_rad=angle,
        issues=tuple(issues),
    )


def _worst_view(obs: list[tuple[RigCamera, tuple[float, float, float]]], config: TriangulationConfig) -> int:
    """해가 없을 때 뺄 카메라: 하나씩 빼고 풀어 남은 카메라 재투영 오차가 가장 작은 경우의 빠진 카메라."""
    best_i, best_err = 0, math.inf
    for i in range(len(obs)):
        rest = obs[:i] + obs[i + 1 :]
        cams = [cam for cam, _ in rest]
        px = np.array([kp[:2] for _, kp in rest])
        X = triangulate_point(cams, px, np.array([kp[2] for _, kp in rest]), config.refine_iterations)
        if X is None:
            continue
        err = float(reprojection_errors(cams, px, X).max())
        if err < best_err:
            best_i, best_err = i, err
    return best_i


def _dlt(cameras: Sequence[RigCamera], normalized: np.ndarray, weights: np.ndarray) -> np.ndarray | None:
    """정규화 좌표의 선형 삼각측량. 행마다 x·P₃ − P₁, y·P₃ − P₂ (P = [R | t]).

    두 번째로 작은 특이값도 0 에 가까우면 해 공간이 2차원 이상(시선 공선)이라 해가 유일하지 않다: None.
    """
    rows = []
    for cam, (x, y), w in zip(cameras, normalized, weights):
        P = np.hstack([cam.R, cam.t[:, None]])
        rows.append(w * (x * P[2] - P[0]))
        rows.append(w * (y * P[2] - P[1]))
    try:
        _, s, vt = np.linalg.svd(np.array(rows))
    except np.linalg.LinAlgError:
        return None
    if not s[0] > 0 or s[-2] <= DLT_RANK_TOLERANCE * s[0]:
        return None
    h = vt[-1]
    if abs(h[3]) < 1e-12 or not np.all(np.isfinite(h)):
        return None
    return h[:3] / h[3]


def _refine(
    cameras: Sequence[RigCamera], targets: np.ndarray, weights: np.ndarray, X: np.ndarray, iterations: int
) -> np.ndarray:
    """가중 픽셀 재투영 오차 제곱합을 가우스-뉴턴(레벤버그-마콰트 감쇠)으로 줄인다."""
    sw = np.sqrt(weights)

    def residuals(P: np.ndarray) -> tuple[np.ndarray, np.ndarray] | None:
        r = np.empty(2 * len(cameras))
        J = np.empty((2 * len(cameras), 3))
        for i, cam in enumerate(cameras):
            xc, yc, zc = cam.R @ P + cam.t
            if zc <= 0:
                return None
            fx, s, cx = cam.K[0]
            fy, cy = cam.K[1][1], cam.K[1][2]
            u = fx * xc / zc + s * yc / zc + cx
            v = fy * yc / zc + cy
            du = np.array([fx / zc, s / zc, -(fx * xc + s * yc) / zc**2])
            dv = np.array([0.0, fy / zc, -fy * yc / zc**2])
            r[2 * i : 2 * i + 2] = sw[i] * (np.array([u, v]) - targets[i])
            J[2 * i] = sw[i] * (du @ cam.R)
            J[2 * i + 1] = sw[i] * (dv @ cam.R)
        return r, J

    current = residuals(X)
    if current is None:
        return X
    damping = 1e-3
    for _ in range(iterations):
        r, J = current
        cost = float(r @ r)
        H = J.T @ J
        g = J.T @ r
        try:
            step = np.linalg.solve(H + damping * np.diag(np.diag(H)), -g)
        except np.linalg.LinAlgError:
            break
        candidate = X + step
        trial = residuals(candidate)
        if trial is not None and float(trial[0] @ trial[0]) < cost:
            X, current = candidate, trial
            damping = max(damping / 10, 1e-9)
            if float(np.linalg.norm(step)) < 1e-9:
                break
        else:
            damping *= 10
            if damping > 1e6:
                break
    return X


def _in_front(cameras: Sequence[RigCamera], X: np.ndarray) -> bool:
    return all(float((cam.R @ X + cam.t)[2]) > 0 for cam in cameras)


def triangulation_covariance(cameras: Sequence[RigCamera], X: np.ndarray, sigma_px: float) -> np.ndarray | None:
    """픽셀 잡음 σ(등방, 카메라 독립) 가정의 삼각측량 3D 공분산 σ²(JᵀJ)⁻¹. J 는 이상적 핀홀 픽셀 투영 야코비안.

    해가 퇴화했거나(정보 행렬 특이) 점이 카메라 뒤면 None.
    """
    X = np.asarray(X, dtype=float)
    rows = []
    for cam in cameras:
        xc, yc, zc = cam.R @ X + cam.t
        if zc <= 0:
            return None
        fx, s = cam.K[0][0], cam.K[0][1]
        fy = cam.K[1][1]
        du = np.array([fx / zc, s / zc, -(fx * xc + s * yc) / zc**2])
        dv = np.array([0.0, fy / zc, -fy * yc / zc**2])
        rows.append(du @ cam.R)
        rows.append(dv @ cam.R)
    J = np.array(rows)
    info = J.T @ J
    if np.linalg.cond(info) > 1e12:
        return None
    return sigma_px**2 * np.linalg.inv(info)


def epipolar_distance_px(cam_a: RigCamera, uv_a: Sequence[float], cam_b: RigCamera, uv_b: Sequence[float]) -> float:
    """cam_a 픽셀 uv_a 의 에피폴라선(cam_b 이상적 핀홀 픽셀 공간)과 cam_b 픽셀 uv_b 사이 거리.

    두 픽셀 중 하나라도 왜곡을 제거할 수 없으면(`RigCamera.normalize` 가 NaN) NaN 이다(거리를 모른다).
    """
    R = cam_b.R @ cam_a.R.T
    t = cam_b.t - R @ cam_a.t
    tx = np.array([[0.0, -t[2], t[1]], [t[2], 0.0, -t[0]], [-t[1], t[0], 0.0]])
    E = tx @ R
    xa = np.append(cam_a.normalize(np.asarray(uv_a[:2], dtype=float))[0], 1.0)
    line_n = E @ xa  # cam_b 정규화 좌표의 직선
    line_px = np.linalg.inv(cam_b.K).T @ line_n  # 이상적 핀홀 픽셀 좌표의 직선
    xb = np.append(cam_b.ideal_pixels(cam_b.normalize(np.asarray(uv_b[:2], dtype=float)))[0], 1.0)
    if not (np.all(np.isfinite(xa)) and np.all(np.isfinite(xb))):
        return math.nan
    norm = math.hypot(line_px[0], line_px[1])
    return math.inf if norm == 0 else abs(float(line_px @ xb)) / norm


def _has_epipolar_competitor(
    j: int,
    cams: Sequence[RigCamera],
    pixels: Mapping[str, Sequence[Keypoint]],
    others: Mapping[str, Sequence[Sequence[Keypoint]]],
    cfg: JointGateConfig,
) -> bool | None:
    """두 카메라 중 한쪽의 이 사람 관측과 다른 쪽의 다른 사람 같은 관절이 에피폴라 조건을 만족하는지.

    에피폴라선은 카메라 앞뒤를 가리지 않으므로, 그렇게 짝지은 두 시선이 두 카메라 앞에서 만날 때만 후보로 센다.
    다른 사람의 그 관절 값(u, v, 신뢰도)에 유한하지 않은 값이 있거나 그 픽셀의 왜곡을 제거할 수 없으면 후보인지
    알 수 없으므로 None 이다(안전 쪽).
    """
    min_conf = cfg.triangulation.min_keypoint_confidence
    unknown = False
    for own, other in ((cams[0], cams[1]), (cams[1], cams[0])):
        uv = pixels[own.camera_id][j]
        if uv is None:
            continue
        for kps in others.get(other.camera_id, ()):
            c = kps[j]
            if c is None:
                continue
            if not all(math.isfinite(float(x)) for x in c[:3]):
                unknown = True
                continue
            if c[2] < min_conf:
                continue
            distance = epipolar_distance_px(own, uv, other, c)
            if math.isnan(distance):
                unknown = True
                continue
            if distance > cfg.competitor_epipolar_px:
                continue
            if triangulate_point([own, other], np.array([uv[:2], c[:2]])) is not None:
                return True
    return None if unknown else False


def cross_view_pair(self, pa: _Prepared, pb: _Prepared) -> PairEvidence:
    cfg = self.config
    a, b = pa.view.key, pb.view.key
    common = pa.mask & pb.mask
    n_common = int(common.sum())
    if n_common < cfg.min_common_joints:
        return PairEvidence(a, b, n_common, reject=RejectReason.FEW_COMMON_JOINTS)

    epi = cross_view_epipolar_px(self, pa, pb, common)
    if epi > cfg.epipolar_gate_px:
        return PairEvidence(a, b, n_common, epipolar_px=epi, reject=RejectReason.EPIPOLAR)

    gap = None
    if pa.floor_ray is not None and pb.floor_ray is not None:
        # 기록만 한다. 가설 높이보다 높은 면 위 사람일 수 있으므로 이 값으로는 탈락시키지 않는다
        gap = _floor_gap(pa.floor_ray, pb.floor_ray, cfg.floor_hypothesis_max_height_m)

    xyz, err = cross_view_two_view_triangulation(pa, pb, common)
    tri = cfg.triangulation
    solved = np.isfinite(err) & (err <= tri.outlier_reprojection_error_px)
    n_ok = int(solved.sum())
    base = {"common_joints": n_common, "epipolar_px": epi, "floor_gap_m": gap, "consistent_joints": n_ok}
    if n_ok < max(cfg.min_common_joints, cfg.min_consistent_joint_ratio * n_common):
        return PairEvidence(a, b, **base, reject=RejectReason.TRIANGULATION)
    residual = float(np.median(err[solved]))
    z_lo, z_hi = cfg.joint_z_range_m
    z = xyz[solved, 2]
    outside = int(np.sum((z < z_lo) | (z > z_hi)))
    if outside > cfg.max_out_of_range_joint_ratio * n_ok:
        return PairEvidence(a, b, **base, residual_px=residual, reject=RejectReason.IMPLAUSIBLE_HEIGHT)
    if residual > cfg.residual_gate_px:
        return PairEvidence(a, b, **base, residual_px=residual, reject=RejectReason.RESIDUAL)
    floor_cost = cfg.floor_missing_cost if gap is None else min(gap / cfg.floor_scale_m, cfg.floor_missing_cost)
    cost = epi / cfg.epipolar_scale_px + residual / cfg.residual_scale_px + floor_cost
    return PairEvidence(
        a, b, **base, residual_px=residual, marginal=residual > cfg.residual_marginal_px, cost=float(cost)
    )


def cross_view_epipolar_px(self, pa: _Prepared, pb: _Prepared, common: np.ndarray) -> float:
    """공통 관절의 대칭 에피폴라 거리 평균. 정규화 좌표 거리에 초점 거리를 곱한 근사 픽셀이다."""
    E = self._essential_matrix(pa.camera, pb.camera)
    xa = np.column_stack([pa.normalized[common], np.ones(int(common.sum()))])
    xb = np.column_stack([pb.normalized[common], np.ones(int(common.sum()))])
    lb = xa @ E.T  # b 영상의 에피폴라선
    la = xb @ E  # a 영상의 에피폴라선
    alg = np.abs(np.sum(xb * lb, axis=1))
    db = alg / np.maximum(np.hypot(lb[:, 0], lb[:, 1]), 1e-12) * _focal(pb.camera)
    da = alg / np.maximum(np.hypot(la[:, 0], la[:, 1]), 1e-12) * _focal(pa.camera)
    return float(np.mean((da + db) / 2.0))


def cross_view_assign(
    self,
    prepared: dict[TrackKey, _Prepared],
    pairs: dict[tuple[TrackKey, TrackKey], PairEvidence],
    flags: list[_Flag],
) -> list[_Work]:
    """제약 있는 최선 우선 병합. view 하나씩인 클러스터에서 시작해, 합칠 수 있는 클러스터 쌍 중 비용이 가장 낮은
    쌍부터 합친다. 카메라 순서에 기대지 않으므로 약한 연결이 먼저 굳어 더 강한 반대 근거를 가리지 않는다.

    - 같은 카메라 금지: 카메라가 겹치는 두 클러스터는 합치지 않는다. 그래서 클러스터마다 카메라당 view 가 하나이고
      view 마다 클러스터가 하나다(카메라별 1:1).
    - 애매하면 합치지 않는다: (A, B) 를 합치는 비용과, 서로 배타적인 경쟁 병합 — A 와 B 의 카메라가 겹치는 C,
      또는 B 와 A 의 카메라가 겹치는 C — 의 비용 차가 ambiguity_margin 보다 작으면 (A, B) 를 이번 시각에 막고
      관련 클러스터에 표시한다. 잔차가 경계인 쌍이 든 병합도 막고 표시한다.
    """
    cfg = self.config
    works = [_Work([k]) for k in sorted(prepared)]
    blocked: set[frozenset[frozenset[TrackKey]]] = set()
    memo: dict[frozenset[frozenset[TrackKey]], tuple[float, bool]] = {}
    while True:
        n = len(works)
        link = {}
        for i, j in itertools.combinations(range(n), 2):
            bk = _block_key(works[i], works[j])
            if bk not in memo:
                memo[bk] = self._linkage(works[i].keys, works[j].keys, pairs)
            link[(i, j)] = link[(j, i)] = memo[bk]
        candidates = sorted(
            (cost, min(works[i].keys + works[j].keys), i, j)
            for (i, j), (cost, _) in link.items()
            if i < j and cost < cfg.new_cluster_cost and _block_key(works[i], works[j]) not in blocked
        )
        if not candidates:
            return works
        cost, _, i, j = candidates[0]
        a, b = works[i], works[j]
        if link[(i, j)][1]:
            keys = frozenset(a.keys + b.keys)
            flags.append(_Flag(UncertainReason.MARGINAL_RESIDUAL, keys, keys, keys))
            blocked.add(_block_key(a, b))
            continue
        rivals: list[TrackKey] = []
        if cfg.same_camera_exclusive:
            cams_a, cams_b = {k[0] for k in a.keys}, {k[0] for k in b.keys}
            for k in range(n):
                if k in (i, j):
                    continue
                cams_k = {key[0] for key in works[k].keys}
                if (cams_k & cams_b and link[(i, k)][0] - cost < cfg.ambiguity_margin) or (
                    cams_k & cams_a and link[(j, k)][0] - cost < cfg.ambiguity_margin
                ):
                    rivals.extend(works[k].keys)
        if rivals:
            keys = frozenset(a.keys + b.keys + rivals)
            flags.append(_Flag(UncertainReason.AMBIGUOUS_ASSIGNMENT, keys, keys, frozenset(a.keys + b.keys)))
            blocked.add(_block_key(a, b))
            continue
        a.keys.extend(b.keys)
        del works[j]


def cross_view_two_view_triangulation(pa: _Prepared, pb: _Prepared, common: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """후보 쌍 검증용 2-view 삼각측량. 공통 관절 (n,) 마다 World XYZ (n, 3) 와 재투영 오차 (n,) 를 돌려준다.

    `geometry.triangulation` 과 같은 정규화 좌표 선형 DLT(x·P₃ − P₁, y·P₃ − P₂, P = [R | t], 신뢰도 가중)를 관절 묶음으로
    한 번에 푼다. 후보 쌍은 시각마다 view 수의 제곱만큼 생겨 관절별 가우스-뉴턴까지 하면 10 Hz 를 지키기 어려워
    정밀화는 하지 않는다. 오차는 두 카메라 중 큰 쪽의 왜곡 제거 픽셀 거리이고, 해가 없거나 어느 카메라 뒤면 inf 다.
    이 결과는 연결 판단에만 쓰고 내보내지 않는다(클러스터 관절은 `triangulate_skeleton` 결과).
    """
    idx = np.flatnonzero(common)
    w_a = np.array([pa.view.view.keypoints[j][2] for j in idx])
    w_b = np.array([pb.view.view.keypoints[j][2] for j in idx])
    rows = []
    for prep, w in ((pa, w_a), (pb, w_b)):
        P = np.hstack([prep.camera.R, prep.camera.t[:, None]])
        x, y = prep.normalized[idx, 0], prep.normalized[idx, 1]
        rows.append(w[:, None] * (x[:, None] * P[2] - P[0]))
        rows.append(w[:, None] * (y[:, None] * P[2] - P[1]))
    A = np.stack(rows, axis=1)  # (n, 4, 4)
    _, s, vt = np.linalg.svd(A)
    h = vt[:, -1, :]
    ok = (s[:, 0] > 0) & (s[:, -2] > 1e-9 * s[:, 0]) & (np.abs(h[:, 3]) > 1e-12)
    xyz = np.full((len(idx), 3), np.nan)
    xyz[ok] = h[ok, :3] / h[ok, 3:4]
    err = np.full(len(idx), np.inf)
    worst = np.zeros(len(idx))
    for prep in (pa, pb):
        xc = prep.camera.to_camera(np.nan_to_num(xyz))
        front = xc[:, 2] > 0
        ok &= front
        proj = prep.camera.ideal_pixels(xc[:, :2] / np.where(front, xc[:, 2], 1.0)[:, None])
        target = prep.camera.ideal_pixels(prep.normalized[idx])
        worst = np.maximum(worst, np.linalg.norm(proj - target, axis=1))
    err[ok] = worst[ok]
    return xyz, err


def cross_view_validate(
    self, works: list[_Work], prepared: dict[TrackKey, _Prepared], flags: list[_Flag]
) -> dict[int, SkeletonEstimate]:
    """클러스터 전체를 삼각측량하고, 대부분의 관절에서 이상치로 빠진 view 를 떼어 낸다."""
    skeletons: dict[int, SkeletonEstimate] = {}
    i = 0
    while i < len(works):
        w = works[i]
        cams = [k[0] for k in w.keys]
        if len(cams) < 2 or len(set(cams)) != len(cams):
            i += 1  # 1대, 또는 (음성 대조에서) 같은 카메라가 겹친 클러스터는 삼각측량하지 않는다
            continue
        sk = triangulate_skeleton(
            [prepared[k].view.view for k in w.keys], self.rig, self.joint_layout, self.config.triangulation
        )
        used = {c: sum(1 for j in sk.joints if c in j.camera_ids) for c in cams}
        rejected = {c: sum(1 for j in sk.joints if c in j.rejected_camera_ids) for c in cams}
        worst = max(cams, key=lambda c: (rejected[c] - used[c], c))
        if rejected[worst] > used[worst]:
            key = next(k for k in w.keys if k[0] == worst)
            flags.append(_Flag(UncertainReason.VIEW_SPLIT, frozenset(w.keys), frozenset(w.keys)))
            w.keys.remove(key)
            works.append(_Work([key]))
            continue  # 같은 클러스터를 다시 검증한다
        skeletons[i] = sk
        i += 1
    return skeletons
