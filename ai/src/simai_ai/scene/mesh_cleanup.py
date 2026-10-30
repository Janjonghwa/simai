"""EXP-01 텍스처 메시 형상 정리(S15P31A101-314): TSDF 전 카메라 간 깊이 정렬, 수평 평면 정리, 정리 확인 지표.

texture_mesh.py 가 옵션(--align-views, --plane-cleanup)으로 부르고, 시험(ai/tests/experiments/static_scene/test_mesh_cleanup.py)이
합성 장면으로 잰다. numpy 만 쓴다. 설계·정한 수치의 근거는 docs/design/exp01-textured-mesh.md "형상 정리(-314)".

1. 카메라 간 정렬: 카메라마다 닮음 변환(회전 ω·이동 t·축척 σ, 카메라 중심 기준) 또는 축척만(카메라 중심 기준 깊이 배율)을 둔다.
   기준 카메라는 고정하고, 나머지를 겹치는 면의 점-평면 거리(상대: 거리 / 깊이)로 함께 맞춘다(투영 대응, Gauss-Newton, Huber).
   보정은 그 카메라의 깊이 배율(1+σ)과 자세(회전 R(ω), 중심 + t)로 남는다. 자기 깊이를 자기 광선 위에서만 바꾸므로(자세가 바뀌면
   그 자세로) 텍스처 투영도 같은 자세를 써야 한다(texture_mesh 가 그렇게 한다). 상한을 넘는 카메라는 원래대로 둔다(모르면 비운다).
2. 수평 평면: World(+Z 상방)에서 법선이 위(+Z)를 향한 점(카메라 쪽 법선, 천장 제외)으로 1점 RANSAC(수평 사전) → XY 칸 연결
   군집 → 군집마다 평면 재적합(기울기·잔차·넓이 기준). 깊이 픽셀은 광선-평면 교점으로(TSDF 가 한 겹을 보게), TSDF 메시의 면은 정점을 평면에 올리고 ESTIMATED_PLANE 으로
   표시한다(형상이 관측 그대로가 아니라 맞춘 평면이다, AGENTS 규칙 7). 평면 밖(거리·법선·발자국 밖)은 건드리지 않는다.

좌표: 카메라 OpenCV cam2world 4×4(MapAnything world). 수평 판정은 World 닮음 변환 M(MapAnything → World)으로 옮겨서 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np


class View(Protocol):  # texture_mesh.DepthView 와 같은 필드
    name: str
    depth: np.ndarray
    valid: np.ndarray
    K: np.ndarray
    cam2world: np.ndarray


# --------------------------------------------------------------------------- 공통


def stats(x: np.ndarray, nd: int = 5) -> dict[str, float] | None:
    x = np.asarray(x, float)
    if not len(x):
        return None
    p = np.percentile(x, [50, 90, 95])
    return {"n": len(x), "mean": round(float(x.mean()), nd), "p50": round(float(p[0]), nd),
            "p90": round(float(p[1]), nd), "p95": round(float(p[2]), nd)}


def grid_points(depth: np.ndarray, valid: np.ndarray, K: np.ndarray, cam2world: np.ndarray) -> np.ndarray:
    """깊이 격자 → World 점(H, W, 3). 무효 픽셀은 NaN."""
    h, w = depth.shape
    v, u = np.mgrid[0:h, 0:w].astype(np.float64)
    Ki = np.linalg.inv(np.asarray(K, float))
    rays = np.stack([u, v, np.ones_like(u)], -1) @ Ki.T
    z = np.where(valid, depth, np.nan).astype(np.float64)
    T = np.asarray(cam2world, float)
    return (rays * z[..., None]) @ T[:3, :3].T + T[:3, 3]


def grid_normals(P: np.ndarray, depth: np.ndarray, jump: float = 0.10) -> tuple[np.ndarray, np.ndarray]:
    """격자 점의 법선(중심 차분, 방향은 호출자가 맞춘다). 네 이웃이 모두 있고, 이웃과 깊이 차가 jump(상대, run_hybrid_depth
    FLYING_JUMP 와 같다) 안이며, 양쪽 이웃까지 거리 비가 3 배 안(한쪽이 다른 표면이면 깨진다)일 때만 유효."""
    h, w, _ = P.shape
    N = np.full_like(P, np.nan)
    ok = np.zeros((h, w), bool)
    if h < 3 or w < 3:
        return N, ok
    du = P[1:-1, 2:] - P[1:-1, :-2]
    dv = P[2:, 1:-1] - P[:-2, 1:-1]
    n = np.cross(du, dv)
    ln = np.linalg.norm(n, axis=-1)
    c = P[1:-1, 1:-1]
    d = np.asarray(depth, float)
    dc = d[1:-1, 1:-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        good = np.isfinite(ln) & (ln > 0)
        for nb in (d[1:-1, 2:], d[1:-1, :-2], d[2:, 1:-1], d[:-2, 1:-1]):
            good &= np.abs(nb - dc) <= jump * dc
        for p1, p2 in ((P[1:-1, 2:], P[1:-1, :-2]), (P[2:, 1:-1], P[:-2, 1:-1])):
            a, b = np.linalg.norm(p1 - c, axis=-1), np.linalg.norm(p2 - c, axis=-1)
            good &= (a <= 3 * b + 1e-12) & (b <= 3 * a + 1e-12)
        nn = n / ln[..., None]
    N[1:-1, 1:-1] = np.where(good[..., None], nn, np.nan)
    ok[1:-1, 1:-1] = good
    return N, ok


def oriented_normals(view: View, jump: float = 0.10) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(World 점, 카메라 쪽을 향한 법선, 법선 유효)."""
    P = grid_points(view.depth, view.valid, view.K, view.cam2world)
    N, ok = grid_normals(P, np.where(view.valid, view.depth, np.nan), jump)
    c = np.asarray(view.cam2world, float)[:3, 3]
    with np.errstate(invalid="ignore"):
        flip = ((c - P) * N).sum(-1) < 0
    N[flip] *= -1
    return P, N, ok & view.valid


def rodrigues(w: np.ndarray) -> np.ndarray:
    th = float(np.linalg.norm(w))
    if th < 1e-15:
        return np.eye(3)
    k = np.asarray(w, float) / th
    Kx = np.array([[0, -k[2], k[1]], [k[2], 0, -k[0]], [-k[1], k[0], 0]])
    return np.eye(3) + np.sin(th) * Kx + (1 - np.cos(th)) * Kx @ Kx


def rot_angle_deg(R: np.ndarray) -> float:
    return float(np.degrees(np.arccos(np.clip((np.trace(R) - 1) / 2, -1.0, 1.0))))


# --------------------------------------------------------------------------- 1. 카메라 간 정렬


@dataclass(frozen=True)
class AlignConfig:
    """정한 수치의 근거: 설계 문서 "형상 정리(-314)". 길이는 메시(MapAnything) 단위."""

    mode: str = "sim3"  # sim3(회전·이동·축척) | scale(카메라 중심 기준 깊이 배율만)
    reference: str | None = None  # None = 다른 카메라와 대응이 가장 많은 카메라
    max_points: int = 30_000  # 카메라마다 표본 점(격자 균등)
    assoc_tol: float = 0.05  # 대응: 투영한 깊이와 그 카메라 깊이의 상대 차 ≤ 5%(run_hybrid_depth 충돌 기준과 같다)
    normal_deg: float = 30.0  # 대응: 두 법선 사이 각 ≤ 30°
    huber: float = 0.01  # 상대 점-평면 거리 Huber 문턱(1%)
    min_pairs: int = 500  # 카메라마다 대응이 이보다 적으면 맞추지 않는다
    max_iter: int = 30
    min_improve: float = 0.01  # 수렴: Huber 비용이 한 번 갱신에 1% 넘게 줄지 않으면 멈춘다(가장 좋은 상태)
    tol_scale: float = 1e-5  # 수렴: 한 번 갱신의 |σ|, |ω|(rad), |t|/중앙 깊이가 모두 이 아래
    tol_rot: float = 1e-5
    tol_trans: float = 1e-5
    cap_scale: float = 0.05  # 상한: 누적 축척 |s − 1| ≤ 5%
    cap_rot_deg: float = 3.0  # 상한: 누적 회전 ≤ 3°
    cap_trans_rel: float = 0.10  # 상한: 중심 이동 ≤ 카메라 중앙 깊이의 10%
    stats_tol: float = 0.20  # 분포 지표: 같이 보임으로 보는 상대 깊이 차(cross_view_consistency 와 같다)


def _sample_grid(ok: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    v, u = np.nonzero(ok)
    if len(u) > n:
        step = len(u) / n
        pick = np.floor(np.arange(n) * step).astype(np.int64)
        v, u = v[pick], u[pick]
    return v, u


def correspondences(vi: View, vj: View, Pi: np.ndarray, Ni: np.ndarray, Pj: np.ndarray, Nj: np.ndarray, okj: np.ndarray,
                    vv: np.ndarray, uu: np.ndarray, assoc_tol: float, normal_cos: float | None) -> dict[str, np.ndarray]:
    """i 의 표본(vv, uu)을 j 에 투영(가장 가까운 픽셀)해 j 의 점·법선과 짝짓는다. 반환: 짝의 i 점, j 점, j 법선, j 깊이,
    상대 깊이 차, i 표본 번호. normal_cos 가 None 이면 법선 조건 없이(분포 지표용) 깊이 조건만."""
    X = Pi[vv, uu]
    T = np.asarray(vj.cam2world, float)
    Xc = (X - T[:3, 3]) @ T[:3, :3]
    z = Xc[:, 2]
    h, w = vj.depth.shape
    with np.errstate(invalid="ignore", divide="ignore"):
        q = Xc @ np.asarray(vj.K, float).T
        x, y = q[:, 0] / q[:, 2], q[:, 1] / q[:, 2]
    inside = (z > 0) & np.isfinite(x) & np.isfinite(y) & (x > -0.5) & (y > -0.5) & (x < w - 0.5) & (y < h - 0.5)
    idx = np.nonzero(inside)[0]
    xi, yi = np.rint(x[idx]).astype(np.int64), np.rint(y[idx]).astype(np.int64)
    good = vj.valid[yi, xi]
    idx, xi, yi = idx[good], xi[good], yi[good]
    dj = vj.depth[yi, xi].astype(float)
    rel = (z[idx] - dj) / dj
    keep = np.abs(rel) <= assoc_tol
    if normal_cos is not None:
        nj_ok = okj[yi, xi]
        cosn = (Ni[vv[idx], uu[idx]] * Nj[yi, xi]).sum(-1)
        with np.errstate(invalid="ignore"):
            keep &= nj_ok & np.isfinite(cosn) & (cosn >= normal_cos)
    idx, xi, yi, dj, rel = idx[keep], xi[keep], yi[keep], dj[keep], rel[keep]
    return {"X": X[idx], "Y": Pj[yi, xi], "n": Nj[yi, xi], "d": dj, "rel": rel, "i": idx}


def cross_view_distances(views: list[View], cfg: AlignConfig | None = None, unit_scale: float = 1.0,
                         max_points: int = 20_000) -> dict[str, Any]:
    """카메라 간 거리 분포: i 의 점을 j 에 투영해 같이 보임(상대 깊이 차 ≤ stats_tol)인 짝의 |Δd|/d 와 점-평면 거리(미터 = 메시 단위
    × unit_scale, j 법선 유효한 짝만). 짝마다와 전체."""
    cfg = cfg or AlignConfig()
    geo = [oriented_normals(v) for v in views]
    pairs, rel_all, p2p_all = {}, [], []
    for a, vi in enumerate(views):
        vv, uu = _sample_grid(vi.valid, max_points)
        for b, vj in enumerate(views):
            if a == b:
                continue
            c = correspondences(vi, vj, geo[a][0], geo[a][1], geo[b][0], geo[b][1], geo[b][2], vv, uu, cfg.stats_tol, None)
            rel = np.abs(c["rel"])
            with np.errstate(invalid="ignore"):
                p2p = np.abs(((c["X"] - c["Y"]) * c["n"]).sum(-1)) * unit_scale
            p2p = p2p[np.isfinite(p2p) & (rel <= cfg.assoc_tol)]
            pairs[f"{vi.name}->{vj.name}"] = {"abs_rel_depth": stats(rel), "within_1pct": _frac(rel, 0.01),
                                              "within_2pct": _frac(rel, 0.02), "within_5pct": _frac(rel, 0.05),
                                              "point_to_plane_m": stats(p2p)}
            rel_all.append(rel)
            p2p_all.append(p2p)
    rel = np.concatenate(rel_all) if rel_all else np.zeros(0)
    p2p = np.concatenate(p2p_all) if p2p_all else np.zeros(0)
    return {"covis_tol": cfg.stats_tol, "all": {"abs_rel_depth": stats(rel), "within_1pct": _frac(rel, 0.01),
                                                 "within_2pct": _frac(rel, 0.02), "within_5pct": _frac(rel, 0.05),
                                                 "point_to_plane_m": stats(p2p)}, "pairs": pairs}


def _frac(x: np.ndarray, t: float) -> float | None:
    return round(float(np.mean(x <= t)), 4) if len(x) else None


@dataclass
class AlignResult:
    views: list[Any]  # 같은 순서, depth·cam2world 가 바뀐 사본(원래대로 둔 카메라는 그대로)
    record: dict[str, Any] = field(default_factory=dict)


def _apply(view: Any, s: float, R: np.ndarray, t: np.ndarray) -> Any:
    """카메라 중심 기준 닮음 변환: 점 X → c + t + s·R(X − c). = 깊이 ×s, 자세 회전 R·R_cam, 중심 c + t."""
    import copy

    T = np.asarray(view.cam2world, float)
    out = copy.copy(view)
    T2 = np.eye(4)
    T2[:3, :3] = R @ T[:3, :3]
    T2[:3, 3] = T[:3, 3] + t
    out.cam2world = T2
    out.depth = (view.depth * s).astype(view.depth.dtype)
    return out


def align_views(views: list[Any], cfg: AlignConfig | None = None) -> AlignResult:
    """카메라 간 정렬(기준 카메라 고정). 상한을 넘거나 대응이 적은 카메라는 고정(원래대로)하고 나머지로 다시 푼다."""
    cfg = cfg or AlignConfig()
    if cfg.mode not in ("sim3", "scale"):
        raise ValueError(f"정렬 방식은 sim3·scale 만: {cfg.mode}")
    names = [v.name for v in views]
    med = {v.name: float(np.nanmedian(np.where(v.valid, v.depth, np.nan))) for v in views}
    # 기준 카메라: 대응이 가장 많은 카메라(같으면 이름순)
    geo = [oriented_normals(v) for v in views]
    samples = [_sample_grid(g[2], cfg.max_points) for g in geo]
    ncos = float(np.cos(np.radians(cfg.normal_deg)))
    counts = np.zeros(len(views), np.int64)
    for a in range(len(views)):
        for b in range(len(views)):
            if a != b:
                c = correspondences(views[a], views[b], geo[a][0], geo[a][1], geo[b][0], geo[b][1], geo[b][2], *samples[a],
                                    cfg.assoc_tol, ncos)
                counts[a] += len(c["d"])
                counts[b] += len(c["d"])
    if cfg.reference is not None:
        if cfg.reference not in names:
            raise ValueError(f"기준 카메라 {cfg.reference} 가 없다")
        ref = cfg.reference
    else:
        ref = names[int(np.argmax(counts))]
    fixed = {ref}
    status = {ref: "REFERENCE"}
    attempts = []
    while True:
        cur, it_rec, converged, ncorr = _solve(views, fixed, cfg, med, excluded=fixed - {ref})
        totals = {}
        bad = []
        for k, v in enumerate(views):
            T0, T1 = np.asarray(v.cam2world, float), np.asarray(cur[k].cam2world, float)
            s = float(np.nanmedian(cur[k].depth[v.valid] / v.depth[v.valid])) if v.valid.any() else 1.0
            ang = rot_angle_deg(T1[:3, :3] @ T0[:3, :3].T)
            dc = float(np.linalg.norm(T1[:3, 3] - T0[:3, 3]))
            totals[v.name] = {"scale": round(s, 6), "rot_deg": round(ang, 4), "center_shift": round(dc, 5),
                              "center_shift_rel": round(dc / med[v.name], 5), "pairs": int(ncorr.get(v.name, 0))}
            if v.name in fixed:
                continue
            if ncorr.get(v.name, 0) < cfg.min_pairs:
                bad.append((v.name, "FEW_PAIRS"))
            elif abs(s - 1) > cfg.cap_scale or ang > cfg.cap_rot_deg or dc > cfg.cap_trans_rel * med[v.name]:
                bad.append((v.name, "CAPPED"))
        attempts.append({"fixed": sorted(fixed), "iterations": len(it_rec), "converged": converged, "totals": totals})
        if not bad:
            break
        for n_, why in bad:
            fixed.add(n_)
            status[n_] = why
    for n_ in names:
        status.setdefault(n_, "OK")
    rec = {"mode": cfg.mode, "reference": ref, "reference_rule": "given" if cfg.reference else "MOST_CORRESPONDENCES",
           "correspondences_by_camera": dict(zip(names, counts.tolist())), "status": status,
           "converged": attempts[-1]["converged"], "iterations": attempts[-1]["iterations"], "history": it_rec,
           "totals": attempts[-1]["totals"], "attempts": attempts,
           "config": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}}
    return AlignResult(cur, rec)


def _solve(views: list[Any], fixed: set[str], cfg: AlignConfig, med: dict[str, float], excluded: set[str] = frozenset()):
    """fixed 는 움직이지 않고, excluded(상한·대응 부족으로 원래대로 둔 카메라)는 다른 카메라를 끌지 않게 대응에서도 뺀다."""
    cur = list(views)
    free = [k for k, v in enumerate(views) if v.name not in fixed]
    npar = 7 if cfg.mode == "sim3" else 1
    ncos = float(np.cos(np.radians(cfg.normal_deg)))
    history = []
    converged = False
    ncorr: dict[str, int] = {}
    best = None
    if not free:
        return cur, history, True, ncorr
    col = {k: i * npar for i, k in enumerate(free)}
    for it in range(cfg.max_iter):
        geo = [oriented_normals(v) for v in cur]
        H = np.zeros((len(free) * npar, len(free) * npar))
        g = np.zeros(len(free) * npar)
        ncorr = {v.name: 0 for v in cur}
        res_all = []
        for a, vi in enumerate(cur):
            vv, uu = _sample_grid(geo[a][2], cfg.max_points)
            for b, vj in enumerate(cur):
                if a == b or (a not in col and b not in col) or vi.name in excluded or vj.name in excluded:
                    continue
                c = correspondences(vi, vj, geo[a][0], geo[a][1], geo[b][0], geo[b][1], geo[b][2], vv, uu, cfg.assoc_tol, ncos)
                if not len(c["d"]):
                    continue
                ncorr[vi.name] += len(c["d"])
                ncorr[vj.name] += len(c["d"])
                n, X, Y, d = c["n"], c["X"], c["Y"], c["d"]
                r = ((X - Y) * n).sum(-1) / d  # 상대 점-평면 거리
                res_all.append(np.abs(r))
                wh = np.where(np.abs(r) <= cfg.huber, 1.0, cfg.huber / np.maximum(np.abs(r), 1e-12))
                blocks = []
                for k, P, sign in ((a, X, 1.0), (b, Y, -1.0)):
                    if k not in col:
                        continue
                    ck = np.asarray(cur[k].cam2world, float)[:3, 3]
                    A = P - ck
                    if cfg.mode == "sim3":
                        J = np.column_stack([np.cross(A, n), n, (A * n).sum(-1)]) * sign / d[:, None]
                    else:
                        J = ((A * n).sum(-1) * sign / d)[:, None]
                    blocks.append((col[k], J))
                for ca, Ja in blocks:
                    g[ca:ca + npar] += (Ja * (wh * r)[:, None]).sum(0)
                    for cb, Jb in blocks:
                        H[ca:ca + npar, cb:cb + npar] += (Ja * wh[:, None]).T @ Jb
        ra = np.concatenate(res_all) if res_all else np.zeros(0)
        med_r = float(np.mean(np.where(ra <= cfg.huber, 0.5 * ra ** 2, cfg.huber * (ra - 0.5 * cfg.huber)))) if len(ra) else None
        step = {"iter": it, "median_abs_rel": round(float(np.median(ra)), 6) if len(ra) else None,
                "huber_cost": None if med_r is None else float(f"{med_r:.6g}")}
        # 비용 수렴: 지금 상태의 Huber 비용(상대 점-평면 거리) 평균이 지금까지 가장 좋은 값보다 min_improve 넘게 줄지 않으면 가장 좋은 상태로 멈춘다
        # (겹치는 면이 대부분 바닥·벽이라 평면을 따라 미끄러지는 방향이 약하게 정해져, 비용이 줄지 않는데 자세가 흘러가는 것을 막는다)
        if med_r is not None and best is not None and med_r > best[0] * (1 - cfg.min_improve):
            history.append({**step, "stop": "COST_CONVERGED"})
            converged = True
            if med_r > best[0]:
                cur, ncorr = best[1], best[2]
            break
        if med_r is not None and (best is None or med_r < best[0]):
            best = (med_r, list(cur), dict(ncorr))
        lam = 1e-9 * max(float(np.trace(H)), 1e-12)
        try:
            delta = -np.linalg.solve(H + lam * np.eye(len(g)), g)
        except np.linalg.LinAlgError:
            break
        mx_s = mx_r = mx_t = 0.0
        for k in free:
            p = delta[col[k]:col[k] + npar]
            if cfg.mode == "sim3":
                w, t, sg = p[:3], p[3:6], p[6]
            else:
                w, t, sg = np.zeros(3), np.zeros(3), p[0]
            cur[k] = _apply(cur[k], 1.0 + sg, rodrigues(w), t)
            mx_s, mx_r = max(mx_s, abs(sg)), max(mx_r, float(np.linalg.norm(w)))
            mx_t = max(mx_t, float(np.linalg.norm(t)) / med[cur[k].name])
        step.update({"max_dscale": mx_s, "max_drot_rad": mx_r, "max_dtrans_rel": mx_t})
        history.append(step)
        if mx_s < cfg.tol_scale and mx_r < cfg.tol_rot and mx_t < cfg.tol_trans:
            converged = True
            break
    return cur, history, converged, ncorr


# --------------------------------------------------------------------------- 2. 수평 평면


@dataclass(frozen=True)
class PlaneConfig:
    """길이는 미터(World). 근거는 설계 문서."""

    normal_deg: float = 20.0  # 평면 찾기 후보 점: 법선과 +Z 의 각 ≤ 20°
    snap_normal_deg: float = 45.0  # 맞추기: 깊이 픽셀·메시 정점의 법선과 평면 법선의 각 ≤ 45°(격자 법선은 거칠다. 옆면(≈90°)은 제외)
    tilt_deg: float = 5.0  # 평면: 법선과 +Z 의 각 ≤ 5°(가구 윗면·바닥은 수평)
    inlier_m: float = 0.03  # 평면 거리 ≤ 3 cm 면 그 평면의 점
    max_rms_m: float = 0.015  # 군집 점의 평면 잔차 RMS ≤ 1.5 cm 여야 평면으로 받는다
    min_area_m2: float = 0.10  # 군집 발자국(XY 칸) 넓이 ≥ 0.1 m²
    cell_m: float = 0.05  # 발자국 칸
    ransac_iters: int = 200
    max_planes: int = 60
    max_points: int = 400_000  # 평면 찾기에 쓰는 점(균등 표본)
    seed: int = 0


@dataclass
class Plane:
    n: np.ndarray  # World 단위 법선(+Z 쪽)
    d: float  # n·X = d
    cells: set  # 발자국 XY 칸 (ix, iy)
    area_m2: float
    rms_m: float
    points: int
    cell_m: float = 0.05

    def dist(self, X: np.ndarray) -> np.ndarray:
        return X @ self.n - self.d

    def cell_keys(self) -> np.ndarray:
        """발자국 칸 열쇠(넓히지 않음)."""
        return np.array(sorted(_key(x, y) for x, y in self.cells), np.int64)

    def footprint_keys(self) -> np.ndarray:
        """발자국을 한 칸 넓힌 칸 열쇠(정렬)."""
        if getattr(self, "_keys", None) is None:
            self._keys = np.unique(np.fromiter((_key(x, y) for x, y in _dilated(self.cells)), np.int64))
        return self._keys

    def inside(self, XY: np.ndarray) -> np.ndarray:
        c = _cells(XY, self.cell_m)
        return np.isin(_key(c[:, 0], c[:, 1]), self.footprint_keys())

    def record(self) -> dict[str, Any]:
        z_c = None
        if abs(self.n[2]) > 1e-9:
            xs = (np.array(sorted(self.cells), float) + 0.5) * self.cell_m
            cx, cy = xs.mean(0) if len(xs) else (0.0, 0.0)
            z_c = (self.d - self.n[0] * cx - self.n[1] * cy) / self.n[2]
        return {"normal": [round(float(x), 5) for x in self.n], "d": round(self.d, 4),
                "tilt_deg": round(float(np.degrees(np.arccos(min(1.0, abs(self.n[2]))))), 3),
                "height_m_at_center": None if z_c is None else round(float(z_c), 4),
                "area_m2": round(self.area_m2, 3), "rms_m": round(self.rms_m, 4), "points": self.points}


def _fit_plane(X: np.ndarray, w: np.ndarray | None = None) -> tuple[np.ndarray, float]:
    w = np.ones(len(X)) if w is None else w
    c = (X * w[:, None]).sum(0) / w.sum()
    A = (X - c) * np.sqrt(w)[:, None]
    _, _, Vt = np.linalg.svd(A, full_matrices=False)
    n = Vt[-1]
    if n[2] < 0:
        n = -n
    return n, float(n @ c)


def _cells(XY: np.ndarray, cell: float) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return np.floor(np.nan_to_num(XY, nan=-1e9) / cell).astype(np.int64)


def _key(x: Any, y: Any) -> Any:
    return x * 1_000_003 + y


def _cell_components(cells: np.ndarray) -> np.ndarray:
    """칸 (n, 2) 의 8-연결 성분 번호(같은 칸은 같은 번호)."""
    uniq, inv = np.unique(cells, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    index = {tuple(c): i for i, c in enumerate(map(tuple, uniq))}
    parent = np.arange(len(uniq))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, (x, y) in enumerate(map(tuple, uniq)):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                j = index.get((x + dx, y + dy))
                if j is not None and j != i:
                    ra, rb = find(i), find(j)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    roots = np.array([find(i) for i in range(len(uniq))])
    _, comp = np.unique(roots, return_inverse=True)
    return comp.reshape(-1)[inv]


def find_horizontal_planes(X: np.ndarray, N: np.ndarray, cfg: PlaneConfig | None = None) -> tuple[list[Plane], dict[str, Any]]:
    """World 점 X(n, 3)·법선 N(n, 3, NaN 허용, 위를 향한 것만 후보)에서 수평 평면 군집을 찾는다. 1점 RANSAC(수평 사전: 가설 = 그 점 높이의 수평면,
    점수 = 안쪽 점의 발자국 칸 수) → 최소제곱 재적합(기울기 ≤ tilt) → 발자국 8-연결 군집마다 재적합·RMS·넓이 판정."""
    cfg = cfg or PlaneConfig()
    rng = np.random.default_rng(cfg.seed)
    ok = np.isfinite(X).all(1) & np.isfinite(N).all(1)
    ncos = np.cos(np.radians(cfg.normal_deg))
    # 위를 향한 면만(카메라 쪽 법선, 카메라는 위에 있다): 바닥·책상·소파 윗면. 천장·덕트 아랫면은 맞추지 않는다
    cand = np.nonzero(ok & (N[:, 2] >= ncos))[0]
    if len(cand) > cfg.max_points:
        cand = np.sort(rng.choice(cand, cfg.max_points, replace=False))
    Xc = X[cand]
    alive = np.ones(len(cand), bool)
    planes: list[Plane] = []
    rejected = []
    tilt_cos = np.cos(np.radians(cfg.tilt_deg))
    min_cells = cfg.min_area_m2 / cfg.cell_m ** 2
    cells_all = _cells(Xc[:, :2], cfg.cell_m)
    ckey = _key(cells_all[:, 0], cells_all[:, 1])
    for _ in range(cfg.max_planes):
        idx = np.nonzero(alive)[0]
        if len(idx) < 3:
            break
        best, best_score = None, 0
        seeds = rng.choice(idx, min(cfg.ransac_iters, len(idx)), replace=False)
        Z = Xc[idx, 2]
        Zs = np.sort(Z)
        z0s = Xc[seeds, 2]
        cnt = np.searchsorted(Zs, z0s + cfg.inlier_m, "right") - np.searchsorted(Zs, z0s - cfg.inlier_m, "left")
        for s in seeds[np.argsort(-cnt, kind="stable")[:10]]:  # 점 수 상위 10 가설만 칸 수로 다시 센다(점 수가 칸 수의 상한)
            m = np.abs(Z - Xc[s, 2]) <= cfg.inlier_m
            score = len(np.unique(ckey[idx[m]]))
            if score > best_score:
                best, best_score = Xc[s, 2], score
        if best is None or best_score < min_cells:
            break
        m = idx[np.abs(Xc[idx, 2] - best) <= cfg.inlier_m]
        n, d = np.array([0, 0, 1.0]), float(best)
        for _ in range(3):  # 재적합 → 안쪽 다시
            n, d = _fit_plane(Xc[m])
            if n[2] < tilt_cos:
                n, d = np.array([0, 0, 1.0]), float(np.median(Xc[m, 2]))
            m = idx[np.abs(Xc[idx] @ n - d) <= cfg.inlier_m]
        alive[m] = False
        comp = _cell_components(cells_all[m])
        for c in range(int(comp.max()) + 1 if len(comp) else 0):
            mm = m[comp == c]
            area = len(np.unique(ckey[mm])) * cfg.cell_m ** 2
            if area < cfg.min_area_m2:
                continue
            nc, dc = _fit_plane(Xc[mm])
            r = Xc[mm] @ nc - dc
            rms = float(np.sqrt(np.mean(r ** 2)))
            rec = {"area_m2": round(area, 3), "rms_m": round(rms, 4), "tilt_deg": round(float(np.degrees(np.arccos(min(1.0, nc[2])))), 3),
                   "height_m": round(float(np.median(Xc[mm, 2])), 3)}
            if nc[2] < tilt_cos:
                rejected.append({**rec, "reason": "TILT"})
                continue
            if rms > cfg.max_rms_m:
                rejected.append({**rec, "reason": "RMS"})
                continue
            cells = set(map(tuple, np.unique(cells_all[mm], axis=0).tolist()))
            # 이미 받은 평면과 발자국이 겹치고(이 군집 칸의 30% 이상이 그 평면 칸) 높이 차가 2·inlier 안이면 같은 표면의 어긋난 겹으로
            # 보고 받지 않는다(바닥 위 3~4 cm 조각 같은 것: 따로 맞추면 없는 턱이 생긴다). 옆에 붙은 다른 높이의 가구(겹치지 않음)는 받는다
            uk = np.unique(ckey[mm])
            near = [q for q in planes if np.isin(uk, q.cell_keys()).mean() >= 0.3
                    and abs(float(np.median(Xc[mm] @ q.n - q.d))) <= 2 * cfg.inlier_m]
            if near:
                rejected.append({**rec, "reason": "LAYER_OF_ACCEPTED"})
                continue
            planes.append(Plane(nc, dc, cells, area, rms, len(mm), cfg.cell_m))
    return planes, {"candidates": len(cand), "planes": len(planes), "rejected": rejected,
                    "config": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__}}


def _dilated(cells: set, r: int = 1) -> set:
    out = set()
    for x, y in cells:
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                out.add((x + dx, y + dy))
    return out


def plane_membership(X: np.ndarray, N: np.ndarray | None, planes: list[Plane], cfg: PlaneConfig,
                     two_sided: bool = False) -> np.ndarray:
    """점마다 속한 평면 번호(−1 = 없음): 발자국(1칸 넓힘) 안, 거리 ≤ inlier, 법선(있으면) ≤ snap_normal_deg. 여럿이면 가장 가까운 평면."""
    out = np.full(len(X), -1, np.int64)
    best = np.full(len(X), np.inf)
    if not planes or not len(X):
        return out
    ncos = np.cos(np.radians(cfg.snap_normal_deg))
    finite = np.isfinite(X).all(1)
    for k, p in enumerate(planes):
        inside = p.inside(X[:, :2]) & finite
        with np.errstate(invalid="ignore"):
            dist = np.abs(p.dist(X))
            m = inside & (dist <= cfg.inlier_m)
            if N is not None:
                c = N @ p.n
                m &= np.isfinite(N).all(1) & ((np.abs(c) if two_sided else c) >= ncos)
        m &= dist < best
        out[m], best[m] = k, dist[m]
    return out


def world_to_mesh_plane(p: Plane, M: np.ndarray) -> tuple[np.ndarray, float]:
    """World 평면 n·Y = d 를 메시(MapAnything) 좌표 평면 n'·X = d' 로(Y = A X + b). n' 는 단위 벡터로."""
    A, b = np.asarray(M, float)[:3, :3], np.asarray(M, float)[:3, 3]
    n2 = A.T @ p.n
    d2 = p.d - p.n @ b
    s = float(np.linalg.norm(n2))
    return n2 / s, d2 / s


def snap_depth_to_planes(view: Any, planes: list[Plane], M: np.ndarray, cfg: PlaneConfig) -> tuple[Any, dict[str, Any]]:
    """평면에 속한 픽셀의 깊이를 광선-평면 교점으로 바꾼 사본(자세 그대로, 자기 광선 위에서만 움직인다)."""
    import copy

    P, N, okn = oriented_normals(view)
    A, b = np.asarray(M, float)[:3, :3], np.asarray(M, float)[:3, 3]
    s = float(np.cbrt(np.linalg.det(A)))
    vv, uu = np.nonzero(view.valid & okn)
    Xw = P[vv, uu] @ A.T + b
    Nw = N[vv, uu] @ (A / s).T
    lab = plane_membership(Xw, Nw, planes, cfg)
    out = copy.copy(view)
    depth = view.depth.copy()
    T = np.asarray(view.cam2world, float)
    Ki = np.linalg.inv(np.asarray(view.K, float))
    moved = []
    for k, p in enumerate(planes):
        sel = lab == k
        if not sel.any():
            continue
        n2, d2 = world_to_mesh_plane(p, M)
        v, u = vv[sel], uu[sel]
        ray = (np.column_stack([u, v, np.ones(len(u))]) @ Ki.T) @ T[:3, :3].T  # World 방향(z=1 기준)
        den = ray @ n2
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (d2 - T[:3, 3] @ n2) / den
        good = np.isfinite(z) & (z > 0) & (np.abs(den) > 1e-6)
        moved.append(np.abs(z[good] - depth[v[good], u[good]]) * s)
        depth[v[good], u[good]] = z[good]
    out.depth = depth
    mv = np.concatenate(moved) if moved else np.zeros(0)
    n = max(int(view.valid.sum()), 1)
    return out, {"snapped_ratio": round(len(mv) / n, 5), "moved_m": stats(mv, 4)}


def face_normals(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return n / ln


def vertex_normals(V: np.ndarray, F: np.ndarray) -> np.ndarray:
    """넓이 가중 정점 법선(단위)."""
    n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    out = np.zeros_like(V)
    for k in range(3):
        np.add.at(out, F[:, k], n)
    ln = np.linalg.norm(out, axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return out / ln


def snap_mesh_to_planes(V: np.ndarray, F: np.ndarray, planes: list[Plane], M: np.ndarray,
                        cfg: PlaneConfig) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """평면에 속한 정점(발자국 안·거리 ≤ inlier·정점 법선 ≤ snap_normal_deg)을 평면에 수직 투영한다. 세 정점이 모두 같은 평면에
    올라간 면이 평면 면이다(정확히 평평하다). 일부 정점만 올라간 면은 관측 형상으로 둔다(이동은 inlier 이하).
    반환 (새 정점(메시 좌표), 평면 면 마스크, 기록)."""
    A, b = np.asarray(M, float)[:3, :3], np.asarray(M, float)[:3, 3]
    s = float(np.cbrt(np.linalg.det(A)))
    Vw = V @ A.T + b
    vlab = plane_membership(Vw, vertex_normals(Vw, F), planes, cfg, two_sided=True)  # TSDF 면 방향은 믿지 않는다
    fl = vlab[F]
    lab = np.where((fl[:, 0] >= 0) & (fl[:, 0] == fl[:, 1]) & (fl[:, 1] == fl[:, 2]), fl[:, 0], -1)
    is_plane = lab >= 0
    V2w = Vw.copy()
    moved = []
    per = []
    for k, p in enumerate(planes):
        vs = np.nonzero(vlab == k)[0]
        dist = p.dist(Vw[vs])
        V2w[vs] = Vw[vs] - dist[:, None] * p.n[None]
        moved.append(np.abs(dist))
        per.append({**p.record(), "faces": int((lab == k).sum()), "vertices": len(vs),
                    "moved_m_p90": round(float(np.percentile(np.abs(dist), 90)), 4) if len(vs) else None})
    V2 = (V2w - b) @ np.linalg.inv(A).T
    mv = np.concatenate(moved) if moved else np.zeros(0)
    return V2, is_plane, {"plane_faces": int(is_plane.sum()), "plane_vertices": int((vlab >= 0).sum()),
                          "moved_m": stats(mv, 4), "planes": per, "unit_scale_m": s}


# --------------------------------------------------------------------------- 3. 확인 지표


def region_flatness(Vw: np.ndarray, F: np.ndarray, region: tuple[float, float, float, float], z_min: float = 0.2,
                    z_max: float = 1.5, band_m: float = 0.05, cfg: PlaneConfig | None = None) -> dict[str, Any]:
    """확인 영역(World x0, x1, y0, y1) 안 가구 윗면(바닥 위 z_min~z_max) 평평함. 위쪽 면으로 수평 평면을 찾고(정리 기준과 별개로
    넓은 띠 band_m), 평면마다 띠 안 면(법선 무관)의 넓이 가중 잔차 RMS, 1 cm 안 넓이 비율, 띠 안 면 중 법선이 20° 넘게 기운 넓이
    비율(뾰족함·겹친 면)을 낸다."""
    cfg = cfg or PlaneConfig(inlier_m=band_m, max_rms_m=band_m, min_area_m2=0.05)
    x0, x1, y0, y1 = region
    C = Vw[F].mean(1)
    Nf = face_normals(Vw, F)
    area = 0.5 * np.linalg.norm(np.cross(Vw[F[:, 1]] - Vw[F[:, 0]], Vw[F[:, 2]] - Vw[F[:, 0]]), axis=1)
    inr = (C[:, 0] >= x0) & (C[:, 0] <= x1) & (C[:, 1] >= y0) & (C[:, 1] <= y1) & (C[:, 2] >= z_min) & (C[:, 2] <= z_max)
    if not inr.any():
        return {"region": list(region), "faces": 0, "planes": []}
    Nu = Nf[inr] * np.sign(Nf[inr][:, 2:3] + 1e-12)
    planes, _ = find_horizontal_planes(C[inr], Nu, cfg)
    out = []
    ncos = np.cos(np.radians(20.0))
    tot_a = tot_r2 = tot_1cm = tot_rough = 0.0
    for p in planes:
        inside = p.inside(C[inr][:, :2])
        dist = p.dist(C[inr])
        m = inside & (np.abs(dist) <= band_m)
        a = area[inr][m]
        if a.sum() <= 0:
            continue
        r = dist[m]
        rough = np.abs(Nf[inr][m][:, 2]) < ncos
        rec = p.record()
        rec.update({"band_area_m2": round(float(a.sum()), 4), "rms_m": round(float(np.sqrt((a * r ** 2).sum() / a.sum())), 4),
                    "within_1cm": round(float(a[np.abs(r) <= 0.01].sum() / a.sum()), 4),
                    "rough_area_ratio": round(float(a[rough].sum() / a.sum()), 4)})
        out.append(rec)
        tot_a += a.sum()
        tot_r2 += (a * r ** 2).sum()
        tot_1cm += a[np.abs(r) <= 0.01].sum()
        tot_rough += a[rough].sum()
    summary = None
    if tot_a > 0:
        summary = {"band_area_m2": round(float(tot_a), 4), "rms_m": round(float(np.sqrt(tot_r2 / tot_a)), 4),
                   "within_1cm": round(float(tot_1cm / tot_a), 4), "rough_area_ratio": round(float(tot_rough / tot_a), 4)}
    # 평면 가정 없는 지표: 영역 안 윗면 쪽 면(법선과 ±Z 각 ≤ 60°) 넓이 중 10° 넘게 기운 비율(울퉁불퉁·뾰족함)
    tilt = np.degrees(np.arccos(np.clip(np.abs(Nf[inr][:, 2]), 0, 1)))
    up = np.isfinite(tilt) & (tilt <= 60)
    a_up = area[inr][up]
    if summary is not None and a_up.sum() > 0:
        summary["top_area_m2"] = round(float(a_up.sum()), 4)
        summary["top_tilt_gt10_ratio"] = round(float(a_up[tilt[up] > 10].sum() / a_up.sum()), 4)
        summary["top_tilt_mean_deg"] = round(float((a_up * tilt[up]).sum() / a_up.sum()), 3)
    return {"region": list(region), "z_range_m": [z_min, z_max], "band_m": band_m, "faces": int(inr.sum()),
            "planes": sorted(out, key=lambda r: -r["band_area_m2"]), "summary": summary}


def region_layers(views: list[Any], M: np.ndarray, region: tuple[float, float, float, float], z_min: float = 0.2,
                  z_max: float = 1.5, band_m: float = 0.10, cfg: PlaneConfig | None = None) -> dict[str, Any]:
    """확인 영역 안 가구 윗면에서 카메라별 깊이 점이 몇 겹으로 어긋났는지: 모든 카메라 점(위쪽 법선)으로 평면을 찾고, 평면마다
    카메라별 부호 거리 중앙값(겹 위치)과 그 폭(최대 − 최소)을 낸다(TSDF 전 입력 기준, 정렬 효과)."""
    cfg = cfg or PlaneConfig(inlier_m=band_m, max_rms_m=band_m, min_area_m2=0.05)
    A, b = np.asarray(M, float)[:3, :3], np.asarray(M, float)[:3, 3]
    s = float(np.cbrt(np.linalg.det(A)))
    Xs, Ns, cam = [], [], []
    for k, v in enumerate(views):
        P, N, ok = oriented_normals(v)
        Xw = P[ok] @ A.T + b
        Nw = N[ok] @ (A / s).T
        x0, x1, y0, y1 = region
        m = (Xw[:, 0] >= x0) & (Xw[:, 0] <= x1) & (Xw[:, 1] >= y0) & (Xw[:, 1] <= y1) & (Xw[:, 2] >= z_min) & (Xw[:, 2] <= z_max)
        Xs.append(Xw[m])
        Ns.append(Nw[m])
        cam.append(np.full(int(m.sum()), k))
    X, N, cam = np.concatenate(Xs), np.concatenate(Ns), np.concatenate(cam)
    if not len(X):
        return {"region": list(region), "planes": []}
    planes, _ = find_horizontal_planes(X, N, cfg)
    out = []
    spreads = []
    for p in planes:
        lab = plane_membership(X, N, [p], cfg)
        m = lab == 0
        dist = p.dist(X[m])
        per = {}
        for k, v in enumerate(views):
            dk = dist[cam[m] == k]
            if len(dk) >= 50:
                per[v.name] = round(float(np.median(dk)), 4)
        rec = p.record()
        spread = (max(per.values()) - min(per.values())) if len(per) >= 2 else None
        rec.update({"camera_offset_m": per, "layer_spread_m": None if spread is None else round(spread, 4),
                    "rms_m_all": round(float(np.sqrt(np.mean(dist ** 2))), 4) if len(dist) else None})
        out.append(rec)
        if spread is not None:
            spreads.append(spread)
    return {"region": list(region), "band_m": band_m, "planes": sorted(out, key=lambda r: -r["area_m2"]),
            "layer_spread_m": stats(np.array(spreads), 4)}
