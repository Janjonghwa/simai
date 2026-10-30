"""사람 keypoint 번들 조정의 수치 풀이 (명세 4.3 3단계, HumanCalib 방식의 자체 구현).

변수: 카메라마다 [회전 보정 ω(3), 카메라 중심 C(3), 초점 f, 방사 왜곡 k1, k2], 사람별 뼈 길이, 3D 점(관절·정물 끝점).
주점은 내부 파라미터 초기값 그대로 고정한다(사람 관측으로는 회전과 섞여 잘 정해지지 않는다).

렌즈 모드(`Problem.lens_mode`, -321): 잔차·야코비안은 위 전체 변수로 계산하고, 풀이만 변수를 줄인다(x_full = expand(x_red),
J_red = J_full · T, T 는 0/1 행렬).
- `per_camera`(기본): 카메라마다 f·k1·k2. 줄이지 않는다(이전 동작 그대로).
- `shared`: 4대가 f·k1·k2 하나를 공유하고 카메라별로는 ω·C 만. 내부 파라미터 사전 행은 카메라마다 그대로 두되 σ 를
  √(카메라 수) 배로 넓혀, 공유 변수 하나에 사전 하나(중심 = 카메라 f0 평균)가 걸린 것과 같게 한다. 시작값은 카메라 값의 평균.
- `fixed`: f·k1·k2 를 시작 벡터 값(= 초기값 또는 이전 라운드 해) 그대로 두고 ω·C 만 맞춘다. 사전 행은 상수가 된다.

- 회전은 R = R0 · exp([ω]×) 이다. ω 는 World 축 기준 회전이므로, 장면 전체를 World Z 축으로 α 만큼 돌리는 것은
  모든 카메라의 ω 가 (0, 0, −α) 만큼 바뀌는 것과 같다(1차). 그래서 ω_z 의 평균을 0 으로 묶으면 벽 방향이 고정된다.
- 잔차(모두 표준편차로 나눈 무차원 값):
  1. 재투영: (투영 픽셀 − 관측 픽셀) / (pixel_sigma × 관측별 잡음 배율 obs_scale). 투영은 OpenCV 핀홀 + 방사 왜곡 2계수
  2. 내부 파라미터 사전(같은 렌즈 공유 초기값): (f − f0)/σ_f, (k − k0)/σ_k
  3. 뼈 길이 일관성: (|X_a − X_b| − L_사람,뼈) / bone_sigma
  4. 바닥(게이지: Z = 0 과 기울기): 접지한 발 keypoint 의 (Z − 접지 높이) / floor_sigma
  5. 실측 정물(-100 자리): (|X_top − X_bottom| − 치수) / (상대 σ × 치수)
  6. 게이지 고정: 벽 방향(평균 ω_z = 0), 수평 원점(카메라 중심 XY 평균 = 초기값), 스케일(정물이 없으면 카메라 중심의
     평균 산포 = 초기값). 접지점이 부족하면 바닥 대신 기울기(평균 ω_x, ω_y = 0)와 높이(카메라 중심 Z 평균)를 초기값에 묶는다.
- 풀이는 Levenberg–Marquardt 다. 점 변수는 같은 표본의 관절끼리 묶은 블록이라 블록 역행렬로 소거하고(Schur 보수)
  카메라·뼈 길이 변수만 밀집 행렬로 푼다. 강건 손실은 soft_l1(경계 1 = 잔차 1σ)을 반복 가중으로 다룬다.
  야코비안은 해석적으로 구한 희소 행렬이다(회전은 SO(3) 오른쪽 야코비안). 재투영 잔차의 왜곡 식은 `geometry.distortion.distort` 의 OPENCV_PINHOLE 방사 2계수와 같고,
  결과 재투영 오차(`Solution.reprojection_errors`)는 `distort` 로 다시 잰다.

이 모듈은 결과 판정을 하지 않는다. 퇴화·검산·계약 형식은 `checks`·`adjust` 가 맡는다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix, diags
from scipy.sparse.csgraph import connected_components
from scipy.spatial.transform import Rotation

from simai_ai.geometry import distortion

CAM_PARAMS = 9  # ω(3), C(3), f, k1, k2
LENS_PARAMS = 3  # f, k1, k2 (카메라 변수 안 6~8 번)
LENS_PER_CAMERA = "per_camera"
LENS_SHARED = "shared"
LENS_FIXED = "fixed"
LENS_MODES = (LENS_PER_CAMERA, LENS_SHARED, LENS_FIXED)
_MIN_DEPTH = 1e-6
_XYZ = (0, 1, 2)


@dataclass(frozen=True)
class CameraInit:
    """카메라 한 대의 초기값과 내부 파라미터 사전."""

    camera_id: str
    image_size: tuple[int, int]
    R0: np.ndarray
    C0: np.ndarray
    f0: float
    principal_point: tuple[float, float]
    k0: tuple[float, float]
    focal_std_px: float
    k_std: tuple[float, float]


@dataclass(frozen=True)
class Problem:
    """번들 조정 문제 한 벌. 점 번호는 points0 의 행 번호다."""

    cameras: tuple[CameraInit, ...]
    points0: np.ndarray  # (P, 3)
    obs_cam: np.ndarray  # (M,) 카메라 번호
    obs_pt: np.ndarray  # (M,) 점 번호
    obs_uv: np.ndarray  # (M, 2) 원본 픽셀
    bone_a: np.ndarray  # (B,)
    bone_b: np.ndarray  # (B,)
    bone_var: np.ndarray  # (B,) 뼈 길이 변수 번호
    bone_len0: np.ndarray  # (nL,)
    contact_pt: np.ndarray  # (Q,)
    contact_h: np.ndarray  # (Q,)
    object_a: np.ndarray  # (O,)
    object_b: np.ndarray  # (O,)
    object_len: np.ndarray  # (O,)
    pixel_sigma: float
    bone_sigma: float
    floor_sigma: float
    object_sigma_rel: float
    gauge_sigma: float
    fix_scale_to_initial: bool
    fix_floor_to_initial: bool
    obs_scale: np.ndarray | None = None  # (M,) 관측별 픽셀 잡음 배율(관절별 κ). 없으면 모두 1
    lens_mode: str = LENS_PER_CAMERA  # 렌즈 변수 모드(모듈 설명, -321)
    floor_contacts_for_gauge: bool = True

    def __post_init__(self) -> None:
        if self.lens_mode not in LENS_MODES:
            raise ValueError(f"lens_mode 는 {LENS_MODES} 중 하나다: {self.lens_mode!r}")

    @property
    def n_cams(self) -> int:
        return len(self.cameras)

    @property
    def n_points(self) -> int:
        return len(self.points0)

    @property
    def scales(self) -> np.ndarray:
        """관측별 픽셀 잡음 배율 (M,)."""
        return np.ones(len(self.obs_cam)) if self.obs_scale is None else np.asarray(self.obs_scale, dtype=float)

    @property
    def n_bones(self) -> int:
        return len(self.bone_len0)

    def drop_observations(self, keep: np.ndarray) -> tuple[Problem, np.ndarray]:
        """keep(관측 마스크)만 남긴다. 관측 2개 미만이 된 점과 그 점을 쓰는 뼈·접지·정물 행을 함께 뺀다.

        돌려주는 두 번째 값은 새 점 번호 → 이전 점 번호다. 남은 접지점이 3개 미만이면 바닥 게이지를 초기값으로 바꾼다.
        """
        obs_cam, obs_pt, obs_uv = self.obs_cam[keep], self.obs_pt[keep], self.obs_uv[keep]
        obs_scale = self.scales[keep]
        while True:
            alive = np.bincount(obs_pt, minlength=self.n_points) >= 2
            m = alive[obs_pt]
            if m.all():
                break
            obs_cam, obs_pt, obs_uv, obs_scale = obs_cam[m], obs_pt[m], obs_uv[m], obs_scale[m]
        old_of_new = np.flatnonzero(alive)
        new_of_old = np.full(self.n_points, -1)
        new_of_old[old_of_new] = np.arange(len(old_of_new))
        bone_keep = alive[self.bone_a] & alive[self.bone_b]
        contact_keep = alive[self.contact_pt]
        object_keep = alive[self.object_a] & alive[self.object_b]
        return (
            replace(
                self,
                points0=self.points0[old_of_new],
                obs_cam=obs_cam,
                obs_pt=new_of_old[obs_pt],
                obs_uv=obs_uv,
                obs_scale=None if self.obs_scale is None else obs_scale,
                bone_a=new_of_old[self.bone_a[bone_keep]],
                bone_b=new_of_old[self.bone_b[bone_keep]],
                bone_var=self.bone_var[bone_keep],
                contact_pt=new_of_old[self.contact_pt[contact_keep]],
                contact_h=self.contact_h[contact_keep],
                object_a=new_of_old[self.object_a[object_keep]],
                object_b=new_of_old[self.object_b[object_keep]],
                object_len=self.object_len[object_keep],
                # 접지점이 3개 미만으로 줄면 바닥 대신 초기값으로 기울기·높이 게이지를 다시 잡는다
                fix_floor_to_initial=self.fix_floor_to_initial or int(contact_keep.sum()) < 3,
            ),
            old_of_new,
        )


@dataclass(frozen=True)
class CameraSolution:
    camera_id: str
    image_size: tuple[int, int]
    R: np.ndarray
    C: np.ndarray
    f: float
    principal_point: tuple[float, float]
    k: tuple[float, float]

    @property
    def K(self) -> np.ndarray:
        cx, cy = self.principal_point
        return np.array([[self.f, 0.0, cx], [0.0, self.f, cy], [0.0, 0.0, 1.0]])

    @property
    def t(self) -> np.ndarray:
        return -self.R @ self.C

    @property
    def distortion_coeffs(self) -> tuple[float, float, float, float, float]:
        return (float(self.k[0]), float(self.k[1]), 0.0, 0.0, 0.0)

    def project(self, X: np.ndarray) -> np.ndarray:
        """World 점 (N, 3) → 원본 픽셀 (N, 2) (`geometry.distortion.distort`). 카메라 뒤 점은 NaN."""
        Xc = (np.asarray(X, dtype=float).reshape(-1, 3) - self.C) @ self.R.T
        z = np.maximum(Xc[:, 2], _MIN_DEPTH)
        xd = distortion.distort(Xc[:, :2] / z[:, None], distortion.MODEL_PINHOLE, self.distortion_coeffs)
        uv = self.f * xd + np.asarray(self.principal_point, dtype=float)
        uv[Xc[:, 2] <= _MIN_DEPTH] = np.nan
        return uv


@dataclass(frozen=True)
class Solution:
    cameras: tuple[CameraSolution, ...]
    points: np.ndarray
    bone_lengths: np.ndarray
    cost: float
    nfev: int
    status: int
    message: str
    success: bool

    def reprojection_errors(self, problem: Problem) -> np.ndarray:
        """관측마다 원본 픽셀 재투영 오차 (M,). 카메라 뒤면 inf."""
        err = np.empty(len(problem.obs_cam))
        for i, cam in enumerate(self.cameras):
            idx = np.flatnonzero(problem.obs_cam == i)
            uv = cam.project(self.points[problem.obs_pt[idx]])
            e = np.linalg.norm(uv - problem.obs_uv[idx], axis=1)
            err[idx] = np.where(np.isfinite(e), e, np.inf)
        return err


def _initial_vector(problem: Problem) -> np.ndarray:
    cams = [np.concatenate([np.zeros(3), c.C0, [c.f0], c.k0]) for c in problem.cameras]
    return np.concatenate([np.concatenate(cams), problem.bone_len0, problem.points0.ravel()])


def _unpack(problem: Problem, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    nc, nl = problem.n_cams, problem.n_bones
    cam = x[: CAM_PARAMS * nc].reshape(nc, CAM_PARAMS)
    lengths = x[CAM_PARAMS * nc : CAM_PARAMS * nc + nl]
    points = x[CAM_PARAMS * nc + nl :].reshape(-1, 3)
    return cam, lengths, points


def _rotations(problem: Problem, omega: np.ndarray) -> np.ndarray:
    return np.stack([c.R0 for c in problem.cameras]) @ Rotation.from_rotvec(omega).as_matrix()


def _skew(v: np.ndarray) -> np.ndarray:
    """(..., 3) → (..., 3, 3) 반대칭 행렬 [v]×."""
    out = np.zeros(v.shape[:-1] + (3, 3))
    out[..., 0, 1], out[..., 0, 2] = -v[..., 2], v[..., 1]
    out[..., 1, 0], out[..., 1, 2] = v[..., 2], -v[..., 0]
    out[..., 2, 0], out[..., 2, 1] = -v[..., 1], v[..., 0]
    return out


def _right_jacobian(omega: np.ndarray) -> np.ndarray:
    """SO(3) 오른쪽 야코비안 J_r(ω): exp(ω + δ) ≈ exp(ω) · exp(J_r(ω) δ). (n, 3) → (n, 3, 3)."""
    theta = np.linalg.norm(omega, axis=1)
    W = _skew(omega)
    small = theta < 1e-8
    t = np.where(small, 1.0, theta)
    a = np.where(small, 0.5, (1 - np.cos(t)) / t**2)
    b = np.where(small, 1.0 / 6.0, (t - np.sin(t)) / t**3)
    return np.eye(3)[None] - a[:, None, None] * W + b[:, None, None] * (W @ W)


class _Layout:
    """변수 벡터의 열 번호."""

    def __init__(self, problem: Problem):
        self.nc = problem.n_cams
        self.bone0 = CAM_PARAMS * self.nc
        self.pt0 = self.bone0 + problem.n_bones
        self.n_vars = self.pt0 + 3 * problem.n_points

    def cam(self, i: np.ndarray, params: tuple[int, ...]) -> np.ndarray:
        return CAM_PARAMS * np.asarray(i)[:, None] + np.asarray(params)[None, :]

    def pt(self, p: np.ndarray, dims: tuple[int, ...] = _XYZ) -> np.ndarray:
        return self.pt0 + 3 * np.asarray(p)[:, None] + np.asarray(dims)[None, :]


class _Sparse:
    """야코비안 (행, 열, 값) 모음."""

    def __init__(self) -> None:
        self.rows: list[np.ndarray] = []
        self.cols: list[np.ndarray] = []
        self.vals: list[np.ndarray] = []

    def put(self, rows: np.ndarray, cols: np.ndarray, vals: np.ndarray) -> None:
        self.rows.append(np.broadcast_to(np.asarray(rows)[:, None], cols.shape).ravel())
        self.cols.append(cols.ravel())
        self.vals.append(np.broadcast_to(vals, cols.shape).ravel())

    def matrix(self, shape: tuple[int, int]) -> csr_matrix:
        return csr_matrix(
            (np.concatenate(self.vals), (np.concatenate(self.rows), np.concatenate(self.cols))), shape=shape
        )


def _evaluate(problem: Problem, x: np.ndarray, with_jacobian: bool) -> tuple[np.ndarray, csr_matrix | None]:
    """잔차와 (원하면) 해석적 야코비안."""
    L = _Layout(problem)
    cam, lengths, X = _unpack(problem, x)
    omega, C, f, k = cam[:, :3], cam[:, 3:6], cam[:, 6], cam[:, 7:9]
    R = _rotations(problem, omega)
    pp = np.array([c.principal_point for c in problem.cameras])
    res: list[np.ndarray] = []
    J = _Sparse()
    row = 0

    # 1. 재투영
    ci, pi = problem.obs_cam, problem.obs_pt
    M = len(ci)
    sig = (problem.pixel_sigma * problem.scales)[:, None]  # (M, 1)
    Y = X[pi] - C[ci]
    Rm = R[ci]
    Xc = np.einsum("mij,mj->mi", Rm, Y)
    z = np.maximum(Xc[:, 2], _MIN_DEPTH)
    xn = Xc[:, :2] / z[:, None]
    s = np.sum(xn * xn, axis=1)
    k1, k2 = k[ci, 0], k[ci, 1]
    rad = 1 + k1 * s + k2 * s * s
    xd = xn * rad[:, None]
    fm = f[ci]
    res.append(((fm[:, None] * xd + pp[ci] - problem.obs_uv) / sig).ravel())
    if with_jacobian:
        D_xn = np.zeros((M, 2, 3))
        D_xn[:, 0, 0] = D_xn[:, 1, 1] = 1 / z
        D_xn[:, :, 2] = -xn / z[:, None]
        drad = k1 + 2 * k2 * s
        D_dist = rad[:, None, None] * np.eye(2)[None] + 2 * drad[:, None, None] * xn[:, :, None] * xn[:, None, :]
        A = fm[:, None, None] * (D_dist @ D_xn)  # ∂uv/∂Xc
        dX = A @ Rm
        dW = -(dX @ _skew(Y) @ _right_jacobian(omega)[ci])
        dk1 = (fm * s)[:, None, None] * xn[:, :, None]
        block = np.concatenate([dW, -dX, xd[:, :, None], dk1, dk1 * s[:, None, None], dX], axis=2) / sig[:, :, None]
        obs_cols = np.hstack([L.cam(ci, tuple(range(CAM_PARAMS))), L.pt(pi)])  # (M, 12)
        for comp in range(2):
            J.put(row + 2 * np.arange(M) + comp, obs_cols, block[:, comp, :])
    row += 2 * M

    # 2. 내부 파라미터 사전
    idx = np.arange(L.nc)
    fs = np.array([c.focal_std_px for c in problem.cameras])
    ks = np.array([c.k_std for c in problem.cameras])
    res.append((f - np.array([c.f0 for c in problem.cameras])) / fs)
    res.append(((k - np.array([c.k0 for c in problem.cameras])) / ks).ravel())
    if with_jacobian:
        J.put(row + idx, L.cam(idx, (6,)), (1 / fs)[:, None])
        for j in range(2):
            J.put(row + L.nc + 2 * idx + j, L.cam(idx, (7 + j,)), (1 / ks[:, j])[:, None])
    row += 3 * L.nc

    # 3. 뼈 길이(목표는 변수), 5. 실측 정물(목표는 치수)
    for a, b, is_bone in ((problem.bone_a, problem.bone_b, True), (problem.object_a, problem.object_b, False)):
        n = len(a)
        if not n:
            continue
        diff = X[a] - X[b]
        d = np.linalg.norm(diff, axis=1)
        if is_bone:
            goal, sigma = lengths[problem.bone_var], np.full(n, problem.bone_sigma)
        else:
            goal, sigma = problem.object_len, problem.object_sigma_rel * problem.object_len
        res.append((d - goal) / sigma)
        if with_jacobian:
            u = diff / np.maximum(d, 1e-12)[:, None] / sigma[:, None]
            r = row + np.arange(n)
            J.put(r, L.pt(a), u)
            J.put(r, L.pt(b), -u)
            if is_bone:
                J.put(r, (L.bone0 + problem.bone_var)[:, None], (-1 / sigma)[:, None])
        row += n

    # 4. 바닥 접지
    Q = len(problem.contact_pt) if problem.floor_contacts_for_gauge else 0
    if Q:
        res.append((X[problem.contact_pt, 2] - problem.contact_h) / problem.floor_sigma)
        if with_jacobian:
            J.put(row + np.arange(Q), L.pt(problem.contact_pt, (2,)), np.full((Q, 1), 1 / problem.floor_sigma))
        row += Q

    # 6. 게이지 고정
    C0 = np.stack([c.C0 for c in problem.cameras])
    g, n = problem.gauge_sigma, L.nc
    mean_rows = [(omega[:, 2], 2), (C[:, 0] - C0[:, 0], 3), (C[:, 1] - C0[:, 1], 4)]
    if problem.fix_floor_to_initial:
        mean_rows += [(omega[:, 0], 0), (omega[:, 1], 1), (C[:, 2] - C0[:, 2], 5)]
    gauge = []
    for values, param in mean_rows:
        gauge.append(values.mean() / g)
        if with_jacobian:
            J.put(np.array([row]), L.cam(idx, (param,)).reshape(1, -1), np.full((1, n), 1 / (n * g)))
        row += 1
    if problem.fix_scale_to_initial:
        dev = C - C.mean(axis=0)
        norms = np.linalg.norm(dev, axis=1)
        spread0 = np.linalg.norm(C0 - C0.mean(axis=0), axis=1).mean()
        gauge.append((norms.mean() - spread0) / g)
        if with_jacobian:
            u = dev / np.maximum(norms, 1e-12)[:, None]
            J.put(
                np.array([row]), L.cam(idx, (3, 4, 5)).reshape(1, -1), ((u - u.mean(axis=0)) / (n * g)).reshape(1, -1)
            )
        row += 1
    res.append(np.asarray(gauge))

    return np.concatenate(res), (J.matrix((row, L.n_vars)) if with_jacobian else None)


def residuals(problem: Problem, x: np.ndarray) -> np.ndarray:
    return _evaluate(problem, x, with_jacobian=False)[0]


def jacobian(problem: Problem, x: np.ndarray) -> csr_matrix:
    J = _evaluate(problem, x, with_jacobian=True)[1]
    assert J is not None
    return J


def robust_cost(r: np.ndarray) -> float:
    """soft_l1 손실의 합: Σ (sqrt(1 + r²) − 1). 작은 잔차에서는 r²/2 와 같다."""
    return float(np.sum(np.sqrt(1.0 + r * r) - 1.0))


def _point_groups(problem: Problem) -> tuple[np.ndarray, int]:
    """뼈·정물 행으로 이어진 점끼리 한 묶음(같은 사람·같은 표본의 관절, 한 정물의 두 끝점)."""
    P = problem.n_points
    a = np.concatenate([problem.bone_a, problem.object_a])
    b = np.concatenate([problem.bone_b, problem.object_b])
    graph = coo_matrix((np.ones(len(a)), (a, b)), shape=(P, P))
    n, labels = connected_components(graph, directed=False)
    return labels, n


@dataclass(frozen=True)
class _Blocks:
    """점 변수를 묶음별 밀집 블록으로 다루기 위한 색인. 블록 크기는 가장 큰 묶음에 맞추고 남는 칸은 단위 행렬로 채운다."""

    group: np.ndarray  # 점 변수(3P)마다 묶음 번호
    slot: np.ndarray  # 점 변수마다 묶음 안 위치
    n_groups: int
    size: int

    @classmethod
    def of(cls, problem: Problem) -> _Blocks:
        labels, n = _point_groups(problem)
        var_group = np.repeat(labels, 3)
        order = np.argsort(var_group, kind="stable")
        counts = np.bincount(var_group, minlength=n)
        starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
        slot = np.empty(len(var_group), dtype=int)
        slot[order] = np.arange(len(var_group)) - np.repeat(starts, counts)
        return cls(var_group, slot, n, int(counts.max()) if len(counts) else 0)

    def inverse(self, V: coo_matrix, damping: np.ndarray) -> csr_matrix:
        """블록 대각 V(+ 감쇠)의 역행렬을 같은 희소 구조로 돌려준다."""
        dense = np.zeros((self.n_groups, self.size, self.size))
        g = self.group[V.row]
        dense[g, self.slot[V.row], self.slot[V.col]] = V.data
        diag = np.arange(self.size)
        filled = np.zeros((self.n_groups, self.size), dtype=bool)
        filled[self.group, self.slot] = True
        dense[:, diag, diag] += np.where(filled, 0.0, 1.0)
        dense[self.group, self.slot, self.slot] += damping
        inv = np.linalg.inv(dense)
        # 같은 묶음의 변수 쌍만 남긴다
        n = len(self.group)
        order = np.argsort(self.group, kind="stable")
        counts = np.bincount(self.group, minlength=self.n_groups)
        rows, cols = [], []
        start = 0
        for c in counts:
            idx = order[start : start + c]
            rows.append(np.repeat(idx, c))
            cols.append(np.tile(idx, c))
            start += c
        r = np.concatenate(rows)
        cc = np.concatenate(cols)
        return csr_matrix((inv[self.group[r], self.slot[r], self.slot[cc]], (r, cc)), shape=(n, n))


class _Reduction:
    """렌즈 모드에 따라 줄인 변수 ↔ 전체 변수(모듈 설명). per_camera 는 줄이지 않고 전체 변수를 그대로 쓴다.

    줄인 변수 순서: 카메라마다 ω·C(6), [shared: 공유 f·k1·k2(3)], 뼈 길이, 점. 점 변수가 맨 뒤라 Schur 소거가 그대로다.
    """

    def __init__(self, problem: Problem, x_full: np.ndarray):
        nc = problem.n_cams
        n_full = len(x_full)
        self.mode = problem.lens_mode
        self.base = np.array(x_full, dtype=float)
        self.T: csr_matrix | None = None
        self.red_of_full: np.ndarray | None = None  # 전체 변수 → 줄인 변수 번호(고정이면 −1)
        self.problem = problem
        self.n_global = CAM_PARAMS * nc + problem.n_bones
        if self.mode == LENS_PER_CAMERA:
            return
        cam_cols = np.arange(CAM_PARAMS * nc).reshape(nc, CAM_PARAMS)
        red_of_full = np.full(n_full, -1)
        red_of_full[cam_cols[:, :6].ravel()] = np.arange(6 * nc)
        n_lens = LENS_PARAMS if self.mode == LENS_SHARED else 0
        if self.mode == LENS_SHARED:
            red_of_full[cam_cols[:, 6:]] = 6 * nc + np.arange(LENS_PARAMS)[None, :]
            # 사전 행이 카메라마다 걸리므로 σ 를 √n 배로 넓혀 공유 변수에 사전 하나와 같게 한다
            widen = float(np.sqrt(nc))
            self.problem = replace(
                problem,
                cameras=tuple(
                    replace(c, focal_std_px=c.focal_std_px * widen, k_std=(c.k_std[0] * widen, c.k_std[1] * widen))
                    for c in problem.cameras
                ),
            )
        rest = np.arange(CAM_PARAMS * nc, n_full)
        red_of_full[rest] = 6 * nc + n_lens + (rest - CAM_PARAMS * nc)
        used = np.flatnonzero(red_of_full >= 0)
        self.red_of_full = red_of_full
        self.T = csr_matrix(
            (np.ones(len(used)), (used, red_of_full[used])), shape=(n_full, 6 * nc + n_lens + len(rest))
        )
        self.n_global = 6 * nc + n_lens + problem.n_bones

    def reduce(self, x_full: np.ndarray) -> np.ndarray:
        if self.T is None:
            return np.array(x_full, dtype=float)
        nc = self.problem.n_cams
        cam = x_full[: CAM_PARAMS * nc].reshape(nc, CAM_PARAMS)
        parts = [cam[:, :6].ravel()]
        if self.mode == LENS_SHARED:
            parts.append(cam[:, 6:].mean(axis=0))  # 카메라마다 다르면 평균에서 시작한다
        parts.append(x_full[CAM_PARAMS * nc :])
        return np.concatenate(parts)

    def expand(self, x_red: np.ndarray) -> np.ndarray:
        if self.T is None:
            return x_red
        x = self.base.copy()
        used = self.red_of_full >= 0
        x[used] = x_red[self.red_of_full[used]]
        return x

    def residuals(self, x_red: np.ndarray) -> np.ndarray:
        return residuals(self.problem, self.expand(x_red))

    def jacobian(self, x_red: np.ndarray) -> csr_matrix:
        J = jacobian(self.problem, self.expand(x_red))
        return J if self.T is None else (J @ self.T).tocsr()


def solve(
    problem: Problem, max_iterations: int = 50, x0: np.ndarray | None = None, tolerance: float = 1e-8
) -> tuple[Solution, np.ndarray]:
    """Levenberg–Marquardt(점 변수 블록을 소거하는 Schur 보수)로 강건 비용을 줄인다. 두 번째 값은 해 벡터다.

    강건 손실(soft_l1)은 반복 가중 최소제곱(가중치 1/sqrt(1 + r²))으로 다룬다. 받아들인 한 걸음의 상대 비용 감소가
    tolerance 미만이면 수렴으로 본다. max_iterations 안에 수렴하지 못하면 success = False 다.
    렌즈 모드(problem.lens_mode)에 따라 줄인 변수로 풀고, 해와 두 번째 값은 전체 변수로 돌려준다. `cost` 는 풀이에 쓴
    잔차(shared 는 넓힌 사전 σ)의 강건 비용이다.
    """
    x_full = _initial_vector(problem) if x0 is None else np.array(x0, dtype=float)
    reduction = _Reduction(problem, x_full)
    p = reduction.problem
    x = reduction.reduce(x_full)
    n_global = reduction.n_global
    blocks = _Blocks.of(p)
    r = reduction.residuals(x)
    cost = robust_cost(r)
    lam = 1e-4
    nfev, status, success, message = 1, 0, False, "최대 반복 수에 이르렀다"
    for iteration in range(max_iterations):
        w = np.sqrt(1.0 / np.sqrt(1.0 + r * r))
        Jw = diags(w) @ reduction.jacobian(x)
        H = (Jw.T @ Jw).tocsr()
        g = Jw.T @ (w * r)
        D = np.maximum(H.diagonal(), 1e-12)
        A = H[:n_global, :n_global].toarray()
        B = H[:n_global, n_global:]
        V = H[n_global:, n_global:].tocoo()
        accepted = False
        while lam <= 1e12:
            Vinv = blocks.inverse(V, lam * D[n_global:])
            BV = (B @ Vinv).tocsr()
            S = A + np.diag(lam * D[:n_global]) - (BV @ B.T).toarray()
            rhs = -g[:n_global] + BV @ g[n_global:]
            try:
                da = np.linalg.solve(S, rhs)
            except np.linalg.LinAlgError:
                lam *= 10
                continue
            db = Vinv @ (-g[n_global:] - B.T @ da)
            x_new = x + np.concatenate([da, db])
            r_new = reduction.residuals(x_new)
            nfev += 1
            cost_new = robust_cost(r_new)
            if np.isfinite(cost_new) and cost_new < cost:
                accepted = True
                break
            lam *= 10
        if not accepted:
            if iteration == 0:
                # 시작점에서 한 걸음도 줄지 않았다: 이미 최소인지 초기값이 나쁜지 구분할 수 없어 수렴으로 보지 않는다
                status, success, message = -1, False, "첫 걸음부터 비용이 줄지 않았다"
            else:
                status, success, message = 2, True, "더 줄일 수 있는 걸음이 없다(국소 최소)"
            break
        decrease = (cost - cost_new) / max(cost, 1e-12)
        x, r, cost = x_new, r_new, cost_new
        lam = max(lam / 10, 1e-12)
        if decrease < tolerance:
            status, success, message = 1, True, "상대 비용 감소가 기준 미만(수렴)"
            break
    x = reduction.expand(x)
    cam, lengths, X = _unpack(problem, x)
    R = _rotations(problem, cam[:, :3])
    cams = tuple(
        CameraSolution(
            camera_id=c.camera_id,
            image_size=c.image_size,
            R=R[i],
            C=cam[i, 3:6].copy(),
            f=float(cam[i, 6]),
            principal_point=c.principal_point,
            k=(float(cam[i, 7]), float(cam[i, 8])),
        )
        for i, c in enumerate(problem.cameras)
    )
    solution = Solution(
        cameras=cams,
        points=X.copy(),
        bone_lengths=lengths.copy(),
        cost=cost,
        nfev=nfev,
        status=status,
        message=message,
        success=success,
    )
    return solution, x


def rebase(problem: Problem, solution: Solution) -> Problem:
    """해를 새 초기값으로 삼은 문제(회전 보정 ω 를 0 으로 되돌린다). 사전(f0, k0)·게이지 기준(C0)은 그대로 둔다.

    게이지 기준 C0 을 바꾸지 않아야 라운드마다 원점·스케일이 흘러가지 않는다. 그래서 중심 시작값은 `start_vector` 가
    해에서 가져온다. 벽 방향 기준(ω_z 평균 = 0)은 R0 이 해로 바뀌므로 해의 방향(1차 라운드에서 이미 고정)을 따른다.
    """
    cams = tuple(replace(c, R0=s.R) for c, s in zip(problem.cameras, solution.cameras, strict=True))
    return replace(problem, cameras=cams, points0=solution.points)


def start_vector(problem: Problem, solution: Solution) -> np.ndarray:
    """rebase(·drop_observations) 한 문제의 시작 벡터: ω=0, 중심·초점·왜곡·뼈 길이는 해 값, 점은 문제의 points0."""
    cams = [np.concatenate([np.zeros(3), s.C, [s.f], s.k]) for s in solution.cameras]
    return np.concatenate([np.concatenate(cams), solution.bone_lengths, problem.points0.ravel()])
