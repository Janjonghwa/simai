"""보정 걷기 keypoint 의 카메라 시각 바로잡기 (S15P31A101-313, 명세 3.9 촬영 시각 계약, 4.3 3단계 입력).

로비 보정 걷기(rec-20261028-020819z)로 번들 조정을 돌리면 관측 44% 가 이상치로 빠졌다. 원인은 관절 위치가 아니라 시각이었다.
카메라 4대 모두 RTP PTS 가 13장 묶음마다 약 370~400 ms 번갈아 어긋나고(묶음 안의 40 ms 간격은 맞다), 녹화의 추정 촬영 시각이
그 PTS 를 따른다. 영상 내용은 frame_sequence(수신·디코딩) 순서로 매끄럽다(엉덩이 중점 120 ms 걸음 크기 중앙값: frame_sequence
순서 16 px, 촬영 시각 순서 43 px). 근거와 수치는 docs/design/extrinsics-from-recording.md "로비 걷기 이상치(-313)".

두 단계로 바로잡는다. 둘 다 keypoint 파일만으로 한다(녹화 세션·영상 불필요).

1. `repair_timelines`: 카메라(stream_epoch)마다 이웃 행의 촬영 시각 차이가 frame_sequence 차이 × 프레임 간격에서
   `max_step_deviation_ms` 넘게 벗어나는 비율이 `max_broken_step_ratio` 를 넘으면 그 카메라의 시각선이 깨졌다고 보고,
   촬영 시각을 frame_sequence 의 선형 시계(최소제곱 기울기, 잔차 중앙값 절편)로 다시 매긴다. 디코딩 순서 = 촬영 순서
   (B 프레임 없음)를 가정한다. B 프레임 재정렬(1~2장, 80 ms 이하)은 기준(150 ms)에 걸리지 않는다. 절편은 PTS 어긋남
   만큼 임의라(묶음 두 벌 중 어느 쪽이 맞는지 모른다) 카메라 사이 상수 차이는 2단계가 정한다.
2. `estimate_time_offsets`: 카메라별 상수 시각 보정값(ms, 촬영 시각에 더함)을 초기 외부 파라미터로 추정한다. 카메라 c 를
   빼고 나머지 2대 이상으로 삼각측량한 관절을 c 에 투영하고, c 의 keypoint 를 보정값만큼 옮겨 보간한 위치와의 재투영 오차
   (cap 에서 자른 평균)가 가장 작은 값을 격자 탐색으로 찾는다. 카메라를 돌아가며 몇 번 반복한다(좌표 하강). 모두를 같이
   옮기면 기하가 같으므로 보정값 평균을 0 으로 묶는다. 초기 외부 파라미터 오차는 시각과 무관한 오차라 최솟값 위치를
   크게 옮기지 않는다(움직임에 따른 오차만 보정값에 따라 변한다).

- 모르면 비운다: 점이 모자라거나 최솟값이 탐색 경계이거나 점수가 시각에 둔하면(±50 ms 차이 0.5 px 미만) 그 카메라
  보정값을 정하지 않고(None) 호출자는 아무 보정도 하지 않는다(`TimeOffsets.usable`). 모두 0 보다 나아지지 않으면 0 이다.
  한계: 초기 보정 오차가 크고 걷는 길이 한 방향 고리이면 위치에 따른 오차와 시각 오차(걷는 방향 변위)가 섞여 점수가
  둔해진다(합성 장면 2° 교란에서 확인). 그때는 둔함 검사가 막고, 걷기는 방을 여러 방향으로 가로질러야 한다. 추정값과 근거(점 수, 0·최적·±50 ms 의 점수)를 기록에 남긴다.
- 시각 보간은 `geometry.interpolation` 과 같은 규칙(창 안 앞뒤 두 프레임 선형, 외삽 없음, 시각 불확실성 기준 초과 프레임
  제외)을 배열로 다시 쓴 것이다(격자 탐색에서 보간을 수만 번 부르므로). 번들 조정 관측은 여전히 `interpolate_views` 로 만든다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.calibration.intrinsics.record import BundleAdjustmentInit
from simai_ai.geometry import distortion
from simai_ai.geometry.interpolation import InterpolationConfig, KeypointSample
from simai_ai.geometry.rig import RigCamera

TIMELINE_SEQUENCE_CLOCK = "FRAME_SEQUENCE_CLOCK"
_MIN_DEPTH = 1e-6

# --------------------------------------------------------------------------- 1. 시각선 점검·다시 매기기


@dataclass(frozen=True)
class TimelineConfig:
    max_step_deviation_ms: float = 150.0
    """이웃 행(같은 카메라·epoch, frame_sequence 순서)의 촬영 시각 차이가 frame_sequence 차이 × 프레임 간격에서 이만큼 넘게
    벗어나면 깨진 걸음으로 센다. B 프레임 재정렬(1~2장 = 80 ms 이하)·수신 흔들림보다 크게, 로비 PTS 어긋남(약 380 ms)보다 작게"""
    max_broken_step_ratio: float = 0.02
    """깨진 걸음 비율이 이보다 크면 그 카메라 시각을 frame_sequence 선형 시계로 다시 매긴다(드문 재연결·끊김은 견딘다)"""
    min_rows: int = 50
    """행이 이보다 적은 카메라는 점검하지 않는다(선형 시계를 맞출 수 없다)"""


def repair_timelines(
    rows: Sequence[Mapping[str, Any]], config: TimelineConfig | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """깨진 시각선(PTS 묶음 어긋남)의 촬영 시각을 frame_sequence 선형 시계로 다시 매긴 행 목록과 점검 기록.

    깨지지 않은 카메라의 행은 그대로 둔다. 다시 매긴 행은 원래 시각을 `estimated_capture_time_ms_original` 에 남긴다.
    """
    config = config or TimelineConfig()
    groups: dict[tuple[str, int], list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        groups[(str(row["camera_id"]), int(row.get("stream_epoch", 0)))].append(i)
    out = [dict(r) for r in rows]
    record: dict[str, Any] = {}
    for (cid, epoch), idx in sorted(groups.items()):
        seq = np.array([float(rows[i]["frame_sequence"]) for i in idx])
        t = np.array([float(rows[i]["estimated_capture_time_ms"]) for i in idx])
        order = np.argsort(seq, kind="stable")
        seq, t, idx_sorted = seq[order], t[order], [idx[k] for k in order]
        key = cid if epoch == 0 else f"{cid}#{epoch}"
        entry: dict[str, Any] = {"rows": len(idx), "retimed": False}
        record[key] = entry
        if len(idx) < config.min_rows or np.ptp(seq) <= 0:
            entry["reason"] = "행이 적어 점검하지 않음"
            continue
        period, intercept = _sequence_clock(seq, t)
        dseq, dt = np.diff(seq), np.diff(t)
        broken = np.abs(dt - dseq * period) > config.max_step_deviation_ms
        ratio = float(broken.mean()) if len(broken) else 0.0
        residual = t - (intercept + period * seq)
        entry.update(
            {
                "frame_period_ms": round(period, 4),
                "broken_step_ratio": round(ratio, 4),
                "inversions": int(np.sum(dt < 0)),
                "clock_residual_ms": {
                    "p5": round(float(np.percentile(residual, 5)), 1),
                    "p50": round(float(np.percentile(residual, 50)), 1),
                    "p95": round(float(np.percentile(residual, 95)), 1),
                },
            }
        )
        if ratio <= config.max_broken_step_ratio:
            continue
        entry["retimed"] = True
        entry["method"] = TIMELINE_SEQUENCE_CLOCK
        for i, s in zip(idx_sorted, seq, strict=True):
            out[i]["estimated_capture_time_ms_original"] = rows[i]["estimated_capture_time_ms"]
            out[i]["estimated_capture_time_ms"] = round(float(intercept + period * s))
    return out, record


def _sequence_clock(seq: np.ndarray, t: np.ndarray) -> tuple[float, float]:
    """t ≈ intercept + period · seq. 기울기는 최소제곱(어긋남이 앞뒤로 번갈아 평균이 0 에 가깝다), 절편은 잔차 중앙값."""
    period = float(np.polyfit(seq, t, 1)[0])
    intercept = float(np.median(t - period * seq))
    return period, intercept


# --------------------------------------------------------------------------- 2. 카메라별 시각 보정값


@dataclass(frozen=True)
class TimeOffsetConfig:
    search_ms: int = 400
    """첫 번째 반복의 탐색 범위 ±(ms). 1단계 절편이 PTS 어긋남(약 ±200 ms)만큼 임의여도 덮게"""
    coarse_step_ms: int = 10
    fine_step_ms: int = 2
    refine_ms: int = 40
    """두 번째 반복부터는 현재 값 ± 이 범위만 다시 본다"""
    passes: int = 3
    eval_period_ms: int = 100
    """점수를 잴 공통 시각 간격"""
    min_keypoint_confidence: float = 0.3
    """번들 조정 기본값(BundleConfig.min_keypoint_confidence)과 같다"""
    error_cap_px: float = 30.0
    """재투영 오차를 이 값에서 자른 평균을 점수로 쓴다(오대응·가림 관절이 점수를 끌지 않게)"""
    min_points: int = 300
    """카메라 하나를 평가할 관절 점이 이보다 적으면 그 카메라 보정값을 정하지 않는다"""
    warp_degree: int = 2
    """점수 전에 빼는 매끄러운 영상 변형의 차수(`_warped_errors`). 초기 보정 오차가 시각 추정을 끌지 않게 한다"""
    min_sharpness_px: float = 0.5
    """최적값 ±50 ms 의 점수가 최적보다 이만큼 이상 나빠야 보정값을 정한다. 둔하면(초기 보정 오차가 크거나 걷는 길이 한
    방향으로만 돌아 위치 오차와 시각 오차가 섞이면) 정하지 않는다(모르면 비운다)"""
    min_improvement_px: float = 0.05
    """최적 점수가 보정 0 의 점수보다 이만큼 이상 낫지 않으면 보정하지 않는다(0 이 이미 최적)"""
    interpolation: InterpolationConfig = field(default_factory=InterpolationConfig)


@dataclass(frozen=True)
class TimeOffsets:
    offsets_ms: Mapping[str, int | None]
    record: Mapping[str, Any]

    @property
    def usable(self) -> bool:
        """모든 카메라 보정값이 정해졌을 때만 쓴다(일부만 옮기면 기준이 섞인다)."""
        return bool(self.offsets_ms) and all(v is not None for v in self.offsets_ms.values())


def rigs_from_initial(
    intrinsics: BundleAdjustmentInit, extrinsics: Mapping[str, tuple[Sequence[Sequence[float]], Sequence[float]]]
) -> dict[str, RigCamera]:
    """내부 파라미터 초기값 + 외부 파라미터 (R, t) → RigCamera. 둘 다 있는 카메라만."""
    out = {}
    for init in intrinsics.cameras:
        if init.camera_id not in extrinsics:
            continue
        R, t = extrinsics[init.camera_id]
        cam = dict(init.camera)
        cam.update({"camera_id": init.camera_id, "R": [list(map(float, r)) for r in R], "t": list(map(float, t))})
        out[init.camera_id] = RigCamera.from_calibration(cam)
    return out


class _Track:
    """카메라 한 대의 프레임 배열(촬영 시각 순서). uv (N, J, 2), conf (N, J) — 없는 관절은 NaN."""

    def __init__(self, samples: Sequence[KeypointSample], config: InterpolationConfig):
        ok = [
            s
            for s in samples
            if s.timestamp_uncertainty_ms is not None
            and 0.0 <= s.timestamp_uncertainty_ms <= config.max_timestamp_uncertainty_ms
        ]
        ok.sort(key=lambda s: s.estimated_capture_time)
        self.window = config.window_ms
        self.times = np.array([s.estimated_capture_time for s in ok], dtype=float)
        J = max((len(s.keypoints) for s in ok), default=0)
        self.uv = np.full((len(ok), J, 2), np.nan)
        self.conf = np.full((len(ok), J), np.nan)
        for i, s in enumerate(ok):
            for j, kp in enumerate(s.keypoints):
                if kp is not None and all(math.isfinite(float(v)) for v in kp[:3]):
                    self.uv[i, j] = kp[:2]
                    self.conf[i, j] = kp[2]

    @property
    def n_joints(self) -> int:
        return self.uv.shape[1]

    def at(self, T: np.ndarray, shift_ms: float) -> tuple[np.ndarray, np.ndarray]:
        """촬영 시각을 shift_ms 만큼 옮긴 이 카메라를 시각 T (K,) 로 보간한 uv (K, J, 2)·conf (K, J). 못 쓰면 NaN."""
        K, J = len(T), self.n_joints
        uv = np.full((K, J, 2), np.nan)
        conf = np.full((K, J), np.nan)
        n = len(self.times)
        if n == 0:
            return uv, conf
        times = self.times + shift_ms
        k = np.searchsorted(times, T, side="right")
        b, a = k - 1, k
        exact = (b >= 0) & (times[np.clip(b, 0, n - 1)] == T)
        ok = (b >= 0) & (a < n)
        bi, ai = np.clip(b, 0, n - 1), np.clip(a, 0, n - 1)
        ok &= (T - times[bi] <= self.window) & (times[ai] - T <= self.window)
        ok |= exact
        span = np.where(ok & ~exact, times[ai] - times[bi], 1.0)
        alpha = np.where(exact, 0.0, (T - times[bi]) / span)
        uv_i = self.uv[bi] + (self.uv[ai] - self.uv[bi]) * alpha[:, None, None]
        uv_i[exact] = self.uv[bi[exact]]
        # 관절은 두 프레임 모두에 있을 때만 보간한다(np.minimum 은 NaN 을 그대로 남긴다)
        conf_i = np.where(exact[:, None], self.conf[bi], np.minimum(self.conf[bi], self.conf[ai]))
        uv[ok], conf[ok] = uv_i[ok], conf_i[ok]
        return uv, conf


def _project(rig: RigCamera, X: np.ndarray) -> np.ndarray:
    """World 점 (N, 3) → 원본 픽셀 (N, 2). 카메라 뒤는 NaN."""
    Xc = rig.to_camera(X)
    z = Xc[:, 2]
    with np.errstate(invalid="ignore", divide="ignore"):
        xn = Xc[:, :2] / z[:, None]
    xd = distortion.distort(np.nan_to_num(xn), rig.distortion_model, rig.distortion_coeffs)
    uv = xd @ rig.K[:2, :2].T + rig.K[:2, 2]
    uv[~(z > _MIN_DEPTH)] = np.nan
    return uv


def _triangulate(Ps: np.ndarray, xn: np.ndarray, w: np.ndarray) -> np.ndarray:
    """선형 DLT. Ps (C, 3, 4) 정규화 투영 [R|t], xn (M, C, 2), w (M, C) 0/1. 2대 미만이거나 어느 쓴 카메라 뒤면 NaN."""
    x = np.nan_to_num(xn)
    rows = np.concatenate(
        [
            (x[:, :, 0:1] * Ps[None, :, 2, :] - Ps[None, :, 0, :]) * w[:, :, None],
            (x[:, :, 1:2] * Ps[None, :, 2, :] - Ps[None, :, 1, :]) * w[:, :, None],
        ],
        axis=1,
    )  # (M, 2C, 4)
    X = np.full((len(xn), 3), np.nan)
    enough = w.sum(axis=1) >= 2
    if not enough.any():
        return X
    _, _, vt = np.linalg.svd(rows[enough])
    h = vt[:, -1, :]
    with np.errstate(invalid="ignore", divide="ignore"):
        Xe = h[:, :3] / h[:, 3:4]
    depth = np.einsum("cij,mj->mci", Ps[:, 2:3, :3], Xe)[:, :, 0] + Ps[None, :, 2, 3]
    front = np.all((depth > _MIN_DEPTH) | (w[enough] == 0), axis=1) & np.isfinite(Xe).all(axis=1)
    Xe[~front] = np.nan
    X[enough] = Xe
    return X


def _warped_errors(proj: np.ndarray, uv: np.ndarray, size: tuple[int, int], degree: int, cap: float) -> np.ndarray:
    """투영 → 관측의 매끄러운 영상 변형(degree 차 다항식)을 빼고 남은 오차 (N,).

    초기 외부·내부 파라미터 오차는 영상 전체에 매끄러운 변위로 나타나고 시각과 무관하다. 그것을 빼야 시각 어긋남(걷는
    방향을 따라 바뀌는 변위)만 점수에 남는다. 맞춤은 최소제곱 두 번(두 번째는 cap 안 점만)이다. degree 0 이면 빼지 않는다.
    """
    if degree <= 0 or len(proj) < 30:
        return np.linalg.norm(proj - uv, axis=1)
    x = proj / np.asarray(size, dtype=float) * 2 - 1
    terms = [np.ones(len(x))]
    for d in range(1, degree + 1):
        terms += [x[:, 0] ** (d - k) * x[:, 1] ** k for k in range(d + 1)]
    A = np.column_stack(terms)
    use = np.ones(len(x), dtype=bool)
    for _ in range(2):
        W, *_ = np.linalg.lstsq(A[use], uv[use] - proj[use], rcond=None)
        e = np.linalg.norm(proj + A @ W - uv, axis=1)
        use = e < cap
        if use.sum() < A.shape[1] * 5:
            break
    return e


def estimate_time_offsets(
    samples_by_camera: Mapping[str, Sequence[KeypointSample]],
    rigs: Mapping[str, RigCamera],
    config: TimeOffsetConfig | None = None,
) -> TimeOffsets:
    """카메라별 시각 보정값(ms, 촬영 시각에 더함, 평균 0). 정하지 못한 카메라는 None 이고 이유를 기록한다."""
    config = config or TimeOffsetConfig()
    ids = sorted(cid for cid in samples_by_camera if cid in rigs and samples_by_camera[cid])
    record: dict[str, Any] = {
        "method": "LEAVE_ONE_OUT_REPROJECTION_GRID",
        "camera_ids": ids,
        "config": {
            "search_ms": config.search_ms,
            "coarse_step_ms": config.coarse_step_ms,
            "fine_step_ms": config.fine_step_ms,
            "passes": config.passes,
            "eval_period_ms": config.eval_period_ms,
            "error_cap_px": config.error_cap_px,
            "warp_degree": config.warp_degree,
        },
    }
    if len(ids) < 3:
        record["reason"] = "카메라 3대 미만(한 대를 빼고 삼각측량할 수 없다)"
        return TimeOffsets({cid: None for cid in samples_by_camera}, record)
    tracks = {cid: _Track(samples_by_camera[cid], config.interpolation) for cid in ids}
    J = min(t.n_joints for t in tracks.values())
    starts = [t.times[0] for t in tracks.values() if len(t.times)]
    ends = [t.times[-1] for t in tracks.values() if len(t.times)]
    if len(starts) < len(ids) or min(ends) <= max(starts):
        record["reason"] = "카메라 사이 겹치는 시간이 없다"
        return TimeOffsets({cid: None for cid in samples_by_camera}, record)
    # 탐색 중 옮겨도 대부분 창 안에 들게 양 끝을 탐색 범위만큼 줄인다
    lo, hi = max(starts) + config.search_ms, min(ends) - config.search_ms
    if hi <= lo:
        lo, hi = max(starts), min(ends)
    T = np.arange(lo, hi, config.eval_period_ms, dtype=float)
    P = {cid: np.hstack([rigs[cid].R, rigs[cid].t.reshape(3, 1)]) for cid in ids}
    offsets = dict.fromkeys(ids, 0.0)

    def others_points(c: str) -> np.ndarray:
        """c 를 뺀 카메라(현재 보정값)로 삼각측량한 관절 (K·J, 3)."""
        rest = [o for o in ids if o != c]
        xn = np.full((len(T) * J, len(rest), 2), np.nan)
        w = np.zeros((len(T) * J, len(rest)))
        for k, o in enumerate(rest):
            uv, conf = tracks[o].at(T, offsets[o])
            uv, conf = uv[:, :J].reshape(-1, 2), conf[:, :J].reshape(-1)
            good = np.isfinite(uv).all(axis=1) & (conf >= config.min_keypoint_confidence)
            if good.any():
                n, ok = rigs[o].normalize_with_mask(uv[good])
                rows = np.flatnonzero(good)[ok]
                xn[rows, k] = n[ok]
                w[rows, k] = 1.0
        return _triangulate(np.stack([P[o] for o in rest]), xn, w)

    def scorer(c: str):
        X = others_points(c)
        has = np.isfinite(X).all(axis=1)
        proj = np.full((len(X), 2), np.nan)
        if has.any():
            proj[has] = _project(rigs[c], X[has])

        def score(shift: float) -> tuple[float, int]:
            uv, conf = tracks[c].at(T, shift)
            uv, conf = uv[:, :J].reshape(-1, 2), conf[:, :J].reshape(-1)
            m = np.isfinite(proj).all(axis=1) & np.isfinite(uv).all(axis=1) & (conf >= config.min_keypoint_confidence)
            if not m.any():
                return math.inf, 0
            e = _warped_errors(proj[m], uv[m], rigs[c].image_size, config.warp_degree, config.error_cap_px)
            return float(np.minimum(e, config.error_cap_px).mean()), int(m.sum())

        return score

    def grid(center: float, half: float, step: float) -> np.ndarray:
        n = round(float(half / step))
        return center + step * np.arange(-n, n + 1)

    status: dict[str, dict[str, Any]] = {}
    for p in range(config.passes):
        changed = False
        for c in ids:
            score = scorer(c)
            center, half = (0.0, float(config.search_ms)) if p == 0 else (offsets[c], float(config.refine_ms))
            coarse = grid(center, half, config.coarse_step_ms)
            values = [score(s)[0] for s in coarse]
            best = float(coarse[int(np.argmin(values))])
            fine = grid(best, config.coarse_step_ms, config.fine_step_ms)
            fvals = [score(s)[0] for s in fine]
            new = float(fine[int(np.argmin(fvals))])
            at_limit = p == 0 and abs(best) >= config.search_ms - 1e-9
            s_best, n_best = score(new)
            s_zero, _ = score(0.0)
            status[c] = {
                "offset_ms": new,
                "points": n_best,
                "score_px": {
                    "zero": _r(s_zero),
                    "best": _r(s_best),
                    "best_minus_50ms": _r(score(new - 50)[0]),
                    "best_plus_50ms": _r(score(new + 50)[0]),
                },
                "at_search_limit": bool(at_limit or status.get(c, {}).get("at_search_limit", False)),
            }
            changed |= abs(new - offsets[c]) > config.fine_step_ms / 2
            offsets[c] = new
        if p > 0 and not changed:
            break
    mean = float(np.mean(list(offsets.values())))
    final: dict[str, int | None] = {}
    for c in ids:
        st = status[c]
        reason = None
        if st["points"] < config.min_points:
            reason = f"평가 점 {st['points']} < {config.min_points}"
        elif st["at_search_limit"]:
            reason = f"최솟값이 탐색 경계(±{config.search_ms} ms)"
        elif _sharpness(st["score_px"]) < config.min_sharpness_px:
            reason = f"시각에 둔하다(±50 ms 점수 차 {_sharpness(st['score_px']):.2f} < {config.min_sharpness_px} px)"
        st["offset_ms"] = round(float(offsets[c] - mean))
        st["reason"] = reason
        final[c] = None if reason else st["offset_ms"]
    # 모든 카메라가 0 에서 나아지지 않으면(이미 맞음) 보정값을 0 으로 둔다. 정하지 못한 카메라가 있으면 그대로 비운다
    gain = max((st["score_px"]["zero"] or 0.0) - (st["score_px"]["best"] or 0.0) for st in status.values())
    record["max_score_gain_px"] = _r(gain)
    if gain < config.min_improvement_px and all(v is not None for v in final.values()):
        record["reason"] = "보정 0 보다 나아지지 않음(이미 맞음)"
        final = dict.fromkeys(ids, 0)
    for cid in samples_by_camera:
        final.setdefault(cid, None)
    record["cameras"] = status
    return TimeOffsets(final, record)


def _sharpness(score: Mapping[str, float | None]) -> float:
    best, lo, hi = score["best"], score["best_minus_50ms"], score["best_plus_50ms"]
    if best is None or lo is None or hi is None:
        return 0.0
    return min(lo, hi) - best


def _r(v: float | None) -> float | None:
    return None if v is None or not math.isfinite(v) else round(float(v), 3)
