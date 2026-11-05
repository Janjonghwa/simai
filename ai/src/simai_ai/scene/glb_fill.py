"""GLB 시각 보강 2단계: 늘어난 면 제거와 바닥·벽 평면 채우기 (S15P31A101-511, 근거 S15P31A101-505 PoC).

texrecon 맵(`textured.glb`)은 TSDF 메시라 카메라에서 먼 바닥·깊이 경계에 사선으로 늘어난 면이 있고, 가구 밑·방 가장자리에는
메시가 없어 검게 뚫린다. `glb_enhance.enhance_glb` 가 이 모듈을 불러 아래 세 가지를 한다(보정 카메라와 사진이 있을 때만).

1. 늘어난 면 제거: 어느 카메라에서든 보이는 면 중 보이는 모든 시선에서 |cos(법선, 시선)| < 0.15 (81° 이상 사선)인 면을 지운다.
   안 보인 면(가림·시야 밖)은 늘어난 게 아니라 "모름"이라 지우지 않는다.
2. 바닥 채우기: World Z=0 평면에서 방 바닥 영역(메시 바닥 칸 닫힘 + 카메라 광선이 가림 없이 닿는 바닥 + 구멍 메움, 수직 벽 평면 안쪽)
   중 메시가 이미 있는 칸을 뺀 구멍에만 5 cm 격자 면을 만든다. 기존 면은 건드리지 않는다.
3. 벽 평면 채우기: RANSAC 으로 찾은 수직 평면(지지 면적·길이·높이 점유·방 경계 조건을 모두 통과한 것만)의 범위 안 구멍에만 면을 만든다.
   평면이 없는 방향은 만들지 않는다.

채운 면의 색과 출처(규칙 2·7):
- 칸 중심(1 cm 텍셀)을 보정 카메라에 투영해, (strict) 그 점에서 메시 표면이 깊이로 확인되는 시선이 있거나 (consistent, 기본)
  가림 없이 보이는 카메라가 둘 이상이고 색이 일치하면 사진 색을 입히고 `ESTIMATED_PLANE`(3: 색은 관측 픽셀이나 형상은 맞춘 평면)으로 표시한다.
  그 밖의 칸은 중립 회색 `UNOBSERVED`(0)다. 색을 지어내지 않는다(확산·복원 없음).
- 텍셀별 출처 지도(0/3)를 PNG 로 GLB 에 같이 넣고(노드 extras `source_map_image`), 면적 비율을 report·메타에 남긴다.
- 시각용이다(`safety_use: false`). 위험 판정·구역 좌표·`observed_surface_ratio` 에 쓰지 않는다. 문 같은 실제 개구부도 구멍과 구분하지 못해 메울 수 있다.
- World→glTF 변환은 `texture_mesh.to_glb_points` 한 곳이다(쓰기는 `glb_enhance.write_enhanced`).
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .glb_enhance import CheckCamera, EnhanceConfig, EnhanceSkip, Prim, SceneModel, check_deadline, rasterize
from .texture_provenance import bilinear

GRAY = np.array([128, 128, 128], np.uint8)
SRC_GRAY = 0  # UNOBSERVED
SRC_PROJECTED = 3  # ESTIMATED_PLANE: 색은 관측 픽셀, 형상은 맞춘 평면
NEAR_M = 0.05
IMAGE_KEY0 = 100_000  # SceneModel.images 에서 채움 텍스처가 쓰는 번호(문서 이미지 번호와 겹치지 않게)


# --------------------------------------------------------------------------- 결과 자료형


@dataclass
class FillPrim:
    prim: Prim  # kind "estimated", image = IMAGE_KEY0 + 번호
    image: np.ndarray  # (H, W, 3) uint8
    source: np.ndarray  # (H, W) uint8, 3 = 사진 투영, 0 = 회색/채움 아님
    role: str  # floor | wall_<k>
    normal_world: np.ndarray
    stats: dict[str, Any]
    region: np.ndarray | None = None  # (H, W) bool, 채움 칸에 속한 텍셀(칸 밖은 False)

    @property
    def inpaint_mask(self) -> np.ndarray:
        """다음 단계(AI 인페인팅)가 채울 곳: 채움 칸 안이면서 사진 근거가 없는(회색) 텍셀. 255 = 채울 곳."""
        return ((self.region if self.region is not None else np.ones(self.source.shape, bool)) & (self.source == SRC_GRAY)).astype(np.uint8) * 255


@dataclass
class FillResult:
    new_idx: dict[int, np.ndarray]  # model.prims 위치 → 늘어난 면을 뺀 새 색인(바뀐 프리미티브만)
    fills: list[FillPrim]
    report: dict[str, Any]
    triangles_delta: int = 0
    steps: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- 메시 합치기·기하


@dataclass
class Merged:
    V: np.ndarray
    F: np.ndarray
    face_prim: np.ndarray  # 면 → model.prims 위치
    face_local: np.ndarray  # 면 → 그 프리미티브 안 면 번호
    n: np.ndarray
    area: np.ndarray
    edge_max: np.ndarray
    cen: np.ndarray


def merge_mesh(prims: list[Prim]) -> Merged:
    """추정(estimated) 프리미티브를 뺀 메시를 정점 합쳐 하나로. 면 순서는 프리미티브 순서를 지킨다(되돌려 쓰기 위해)."""
    Vs, Fs, fp, fl, off = [], [], [], [], 0
    for k, p in enumerate(prims):
        if p.kind == "estimated" or p.name == "estimated_floor_plane" or not len(p.idx):
            continue
        Vs.append(p.pos)
        Fs.append(p.idx + off)
        fp.append(np.full(len(p.idx), k, np.int64))
        fl.append(np.arange(len(p.idx), dtype=np.int64))
        off += len(p.pos)
    if not Vs:
        raise EnhanceSkip("FILL_NO_MESH", "채울 메시 없음")
    V, F = np.concatenate(Vs), np.concatenate(Fs)
    key = np.round(V / 1e-5).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    V = V[first]
    F = inv.reshape(-1)[F]
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    nn = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(nn, axis=1)
    n = nn / np.maximum(2 * area, 1e-12)[:, None]
    edge = np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 1).max(1)
    return Merged(V, F, np.concatenate(fp), np.concatenate(fl), n, area, edge, (a + b + c) / 3)


# --------------------------------------------------------------------------- 카메라 가시성


@dataclass
class CamVis:
    cam: CheckCamera
    scale: float
    depth: np.ndarray  # (H, W) float32, 덮이지 않은 곳 0 (render 배율 좌표)
    seen_px: np.ndarray  # (m,) 보이는 화소 수(render 배율)
    cos: np.ndarray  # (m,) |법선·시선|
    center: np.ndarray


def cam_center(cam: CheckCamera) -> np.ndarray:
    return -np.asarray(cam.R, float).T @ np.asarray(cam.t, float).reshape(3)


def project(cam: CheckCamera, P: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """World → 화소 색인 좌표(화소 중심이 정수), 카메라 z."""
    pc = P @ np.asarray(cam.R, float).T + np.asarray(cam.t, float).reshape(3)
    z = pc[..., 2]
    zz = np.where(np.abs(z) < 1e-9, 1e-9, z)
    K = np.asarray(cam.K, float)
    return K[0, 0] * pc[..., 0] / zz + K[0, 2], K[1, 1] * pc[..., 1] / zz + K[1, 2], z


def render_visibility(mesh: Merged, cam: CheckCamera, long_side: int, keep: np.ndarray | None = None,
                      deadline: float | None = None) -> CamVis:
    h0, w0 = cam.image.shape[:2]
    s = min(1.0, long_side / max(h0, w0))
    H, W = max(1, round(h0 * s)), max(1, round(w0 * s))
    ids = np.arange(len(mesh.F)) if keep is None else np.nonzero(keep)[0]
    u, v, z = project(cam, mesh.V[mesh.F[ids]])
    ok = z.min(1) >= NEAR_M
    ids, u, v, z = ids[ok], u[ok], v[ok], z[ok]
    tid, _, depth = rasterize(np.stack([(u + 0.5) * s, (v + 0.5) * s], axis=2), z, (H, W), perspective=True, deadline=deadline)
    seen = np.zeros(len(mesh.F), np.int64)
    hit = tid >= 0
    if hit.any():
        seen += np.bincount(ids[tid[hit]], minlength=len(mesh.F))
    c = cam_center(cam)
    d = c[None] - mesh.cen
    cos = np.abs((mesh.n * d).sum(1)) / np.maximum(np.linalg.norm(d, axis=1), 1e-9)
    return CamVis(cam, s, np.where(np.isfinite(depth), depth, 0.0).astype(np.float32), seen, cos, c)


# --------------------------------------------------------------------------- 1. 늘어난 면


def stretched_faces(mesh: Merged, vis: list[CamVis], cfg: EnhanceConfig) -> tuple[np.ndarray, dict[str, Any]]:
    """True = 지울 면. 보이는 면 중 보이는 모든 시선에서 |cos| < fill_cos_slant 인 면(+ 선택: 긴 변)."""
    m = len(mesh.F)
    seen_any = np.zeros(m, bool)
    best = np.zeros(m)
    for cv in vis:
        px_min = max(1, round(6 * cv.scale * cv.scale))
        seen = cv.seen_px >= px_min
        best = np.maximum(best, np.where(seen, cv.cos, 0.0))
        seen_any |= seen
    slant = seen_any & (best < cfg.fill_cos_slant)
    long_ = mesh.edge_max > cfg.fill_long_edge_m if cfg.fill_long_edge_m > 0 else np.zeros(m, bool)
    drop = slant | long_
    rec = {"cos_slant": cfg.fill_cos_slant, "long_edge_m": cfg.fill_long_edge_m, "faces": int(m), "seen_by_any_camera": int(seen_any.sum()),
           "slant_faces": int(slant.sum()), "long_edge_faces": int(long_.sum()), "removed_faces": int(drop.sum()),
           "removed_area_m2": round(float(mesh.area[drop].sum()), 3), "removed_area_ratio": round(float(mesh.area[drop].sum() / max(mesh.area.sum(), 1e-12)), 5)}
    return drop, rec


# --------------------------------------------------------------------------- 바닥 높이·벽 평면


def floor_level(mesh: Merged, keep: np.ndarray, cfg: EnhanceConfig) -> tuple[float, dict[str, Any]]:
    """바닥 높이: 바닥 근처 수평면의 넓이 가중 중앙값. |중앙값| ≤ fill_floor_snap_m 이면 World Z=0 을 쓴다(규약), 아니면 그 중앙값."""
    sel = keep & (np.abs(mesh.n[:, 2]) > 0.9) & (np.abs(mesh.cen[:, 2]) < 0.15)
    if sel.sum() < 50:
        raise EnhanceSkip("FILL_NO_FLOOR", f"바닥 근처 수평면 {int(sel.sum())} < 50")
    z = mesh.cen[sel, 2]
    o = np.argsort(z)
    cw = np.cumsum(mesh.area[sel][o])
    med = float(z[o][np.searchsorted(cw, cw[-1] / 2)])
    q = np.percentile(z, [5, 95])
    snapped = abs(med) <= cfg.fill_floor_snap_m
    return (0.0 if snapped else med), {"area_weighted_median_z_m": round(med, 4), "z_p5_p95_m": [round(float(x), 4) for x in q],
                                       "z_source": "WORLD_Z0" if snapped else "MESH_MEDIAN"}


def find_wall_planes(mesh: Merged, keep: np.ndarray, z0: float, cfg: EnhanceConfig, seed: int = 0) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """수직 면(|nz| < 0.3, 바닥 위 0.1~2.6 m)의 넓이 가중 순차 RANSAC. 통과한 평면과 기각 사유를 돌려준다.

    가짜 벽 방지 조건(모두 통과): 길이·넓이, 지배 방향 직교(±fill_wall_manhattan_deg), 높이 구간 점유(5/8 이상),
    방 경계(바닥 면적이 평면 바깥쪽에 fill_wall_max_outside 넘게 있으면 방 안 구조물), 같은 벽의 중복 병합.
    평면은 n(방 안쪽을 향함)·d(n·p = d)·t(수평 접선)·u_range·z_top 으로 돌려준다."""
    n, area, cen = mesh.n, mesh.area, mesh.cen
    ids = np.nonzero(keep & (np.abs(n[:, 2]) < 0.3) & (cen[:, 2] > z0 + 0.1) & (cen[:, 2] < z0 + 2.6))[0]
    if len(ids) < 50:
        return [], []
    p2, a, n2 = cen[ids, :2], area[ids], n[ids, :2]
    n2 = n2 / np.maximum(np.linalg.norm(n2, axis=1, keepdims=True), 1e-9)
    ang = np.arctan2(n2[:, 1], n2[:, 0])
    hist, bins = np.histogram(np.mod(ang, np.pi / 2), bins=90, range=(0, np.pi / 2), weights=a)
    th0 = float(bins[np.argmax(hist)] + np.pi / 360)
    res = ((ang - th0 + np.pi / 4) % (np.pi / 2)) - np.pi / 4  # 지배 방향에서 벗어난 각(직교 4방향 접음)
    near_dom = np.abs(res) < np.radians(2.0)
    if a[near_dom].sum() > 0:  # 1° 칸 중심 대신 ±2° 안 면의 넓이 가중 평균으로 다듬는다(6 m 에서 1° = 10 cm 어긋남)
        th0 += float((res[near_dom] * a[near_dom]).sum() / a[near_dom].sum())
    floor_f = keep & (np.abs(n[:, 2]) > 0.7) & (np.abs(cen[:, 2] - z0) < 0.3)
    rng = np.random.default_rng(seed)
    alive = np.ones(len(ids), bool)
    planes: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for _ in range(cfg.fill_wall_max_planes * 2):
        cand = np.nonzero(alive)[0]
        if len(cand) < 50:
            break
        ev = cand if len(cand) <= 20000 else rng.choice(cand, 20000, replace=False)
        pick = rng.choice(cand, size=min(cfg.fill_wall_ransac_iter, len(cand)), replace=False, p=a[cand] / a[cand].sum())
        pe, ne, ae = p2[ev], n2[ev], a[ev]
        best_k, best_area = None, 0.0
        for k in pick:
            inl = (np.abs((pe - p2[k]) @ n2[k]) < cfg.fill_wall_dist_tol_m) & (np.abs(ne @ n2[k]) > 0.9)
            s_ = ae[inl].sum()
            if s_ > best_area:
                best_k, best_area = k, s_
        if best_k is None:
            break
        k = best_k
        inl_all = (np.abs((p2[cand] - p2[k]) @ n2[k]) < cfg.fill_wall_dist_tol_m) & (np.abs(n2[cand] @ n2[k]) > 0.9)
        sel = cand[inl_all]
        w = a[sel]
        if w.sum() < cfg.fill_wall_min_area_m2:
            break
        c = (p2[sel] * w[:, None]).sum(0) / w.sum()
        cov = ((p2[sel] - c).T * w) @ (p2[sel] - c)
        _, evec = np.linalg.eigh(cov)
        t = evec[:, 1]
        nrm = np.array([-t[1], t[0]])
        if nrm @ n2[k] < 0:
            nrm = -nrm
        u = (p2[sel] - c) @ t
        length = float(np.percentile(u, 99) - np.percentile(u, 1))
        theta = float(np.arctan2(nrm[1], nrm[0]))
        dev = abs(((theta - th0 + np.pi / 4) % (np.pi / 2)) - np.pi / 4)
        rec: dict[str, Any] = {"n_faces": int(len(sel)), "area_m2": round(float(w.sum()), 3), "length_m": round(length, 3),
                               "dev_from_manhattan_deg": round(float(np.degrees(dev)), 2)}
        alive[sel] = False
        if length < cfg.fill_wall_min_len_m:
            planes_rej(rejected, rec, "LENGTH")
            continue
        if np.degrees(dev) > cfg.fill_wall_manhattan_deg:
            planes_rej(rejected, rec, "NOT_ORTHOGONAL")
            continue
        snapped = th0 + round((theta - th0) / (np.pi / 2)) * (np.pi / 2)
        nrm = np.array([np.cos(snapped), np.sin(snapped)])
        t = np.array([-nrm[1], nrm[0]])
        d = float(np.median(p2[sel] @ nrm))
        fid = ids[sel]
        verts = mesh.V[mesh.F[fid]].reshape(-1, 3)
        hb = np.histogram(cen[fid, 2] - z0, bins=np.arange(0, 2.41, 0.3), weights=a[sel])[0]
        occupied = hb >= 0.03 * a[sel].sum()
        h95 = float(np.percentile(cen[fid, 2], 95) - z0)
        rec.update({"height_p95_m": round(h95, 3), "height_bins_occupied": int(occupied.sum())})
        if h95 < 1.5 or occupied.sum() < 5:
            planes_rej(rejected, rec, "LOW_OR_PARTIAL_HEIGHT")
            continue
        # 방 경계: 바닥 면적이 어느 쪽에 있는지로 방 안쪽을 정하고, 반대쪽 바닥이 많으면 방 안 구조물
        dd = cen[floor_f, :2] @ nrm - d
        af = area[floor_f]
        if af.sum() <= 0:
            planes_rej(rejected, rec, "NO_FLOOR_REFERENCE")
            continue
        pos_frac = float(af[dd > 0.15].sum() / af.sum())
        neg_frac = float(af[dd < -0.15].sum() / af.sum())
        if pos_frac < neg_frac:
            nrm, t, d = -nrm, -t, -d
            outside = pos_frac
        else:
            outside = neg_frac
        rec["floor_outside_frac"] = round(outside, 4)
        if outside > cfg.fill_wall_max_outside:
            planes_rej(rejected, rec, "NOT_ROOM_BOUNDARY")
            continue
        uu = verts[:, :2] @ t
        rec.update({"n": nrm.tolist(), "t": t.tolist(), "d": d, "u_range": [float(np.percentile(uu, 0.5)), float(np.percentile(uu, 99.5))],
                    "z_top": float(np.percentile(verts[:, 2], 99))})
        dup = next((q for q in planes if abs(np.dot(q["n"], nrm)) > 0.99 and abs(q["d"] - d * np.sign(np.dot(q["n"], nrm))) < 0.25), None)
        if dup is not None:
            planes_rej(rejected, rec, "DUPLICATE")
            lo, hi = (rec["u_range"] if np.dot(dup["t"], t) > 0 else [-rec["u_range"][1], -rec["u_range"][0]])
            dup["u_range"] = [min(dup["u_range"][0], lo), max(dup["u_range"][1], hi)]
            dup["z_top"] = max(dup["z_top"], rec["z_top"])
            continue
        planes.append(rec)
        if len(planes) >= cfg.fill_wall_max_planes:
            break
    return planes, rejected


def planes_rej(rejected: list[dict[str, Any]], rec: dict[str, Any], reason: str) -> None:
    rejected.append({**{k: v for k, v in rec.items() if k not in ("n", "t", "u_range")}, "rejected": reason})


# --------------------------------------------------------------------------- 칸 색 투영


def project_colors(P: np.ndarray, nrm: np.ndarray, vis: list[CamVis], cfg: EnhanceConfig, deadline: float | None = None) -> tuple[np.ndarray, np.ndarray]:
    """P (N,3) 점마다 (색 uint8 (N,3), 출처 uint8 (N,): 3 = 사진 투영, 0 = 회색).

    카메라별 보임 = 영상 안·유효(사람 마스크 밖)·|cos| ≥ 0.15·그 점 앞에 메시가 없음(깊이 버퍼). strict = 그 점에서 메시 표면이 깊이로 확인.
    fill_color_mode: strict(strict 한 시선 하나 이상) / consistent(strict 이거나 보이는 카메라 둘 이상이고 색 최대 채널 차이 ≤ 허용) / lenient(하나 이상)."""
    N = len(P)
    nc = len(vis)
    cols = np.zeros((nc, N, 3), np.float32)
    seen = np.zeros((nc, N), bool)
    strict = np.zeros((nc, N), bool)
    score = np.zeros((nc, N))
    for ci, cv in enumerate(vis):
        check_deadline(deadline)
        cam = cv.cam
        h0, w0 = cam.image.shape[:2]
        u, v, z = project(cam, P)
        inb = (u >= 0) & (u <= w0 - 1) & (v >= 0) & (v <= h0 - 1) & (z > NEAR_M)
        ii = np.nonzero(inb)[0]
        if not len(ii):
            continue
        H, W = cv.depth.shape
        ru = np.clip(np.floor((u[ii] + 0.5) * cv.scale).astype(int), 0, W - 1)
        rv = np.clip(np.floor((v[ii] + 0.5) * cv.scale).astype(int), 0, H - 1)
        dm = cv.depth[rv, ru]
        tol = np.maximum(0.05, 0.03 * z[ii])
        strict_ok = (dm > 0) & (np.abs(dm - z[ii]) < tol)
        free_ok = (dm == 0) | (dm >= z[ii] - tol)
        if cam.valid is not None:
            vu = np.clip(np.rint(u[ii]).astype(int), 0, w0 - 1)
            vv = np.clip(np.rint(v[ii]).astype(int), 0, h0 - 1)
            vok = cam.valid[vv, vu]
        else:
            vok = np.ones(len(ii), bool)
        d = cv.center[None] - P[ii]
        dist = np.linalg.norm(d, axis=1)
        cosn = np.abs((nrm[None] * d).sum(1)) / np.maximum(dist, 1e-9)
        ok = free_ok & vok & (cosn >= 0.15)
        seen[ci, ii] = ok
        strict[ci, ii] = ok & strict_ok
        score[ci, ii] = np.where(ok, cosn / np.maximum(dist, 0.1) ** 2, 0.0)
        sel = ii[ok]
        if len(sel):
            cols[ci, sel] = bilinear(cam.image, u[sel], v[sel])
    n_seen = seen.sum(0)
    any_strict = strict.any(0)
    best = np.argmax(np.where(seen, score, -1.0), axis=0)
    color = cols[best, np.arange(N)]
    if cfg.fill_color_mode == "strict":
        observed = any_strict
    elif cfg.fill_color_mode == "lenient":
        observed = n_seen >= 1
    else:
        big = np.where(seen[:, :, None], cols, -np.inf).max(0)
        small = np.where(seen[:, :, None], cols, np.inf).min(0)
        spread = np.where(n_seen >= 2, (big - small).max(1), np.inf)
        observed = any_strict | (spread <= cfg.fill_consistency_max_diff)
    out = np.tile(GRAY, (N, 1)).astype(np.uint8)
    out[observed] = np.clip(np.rint(color[observed]), 0, 255).astype(np.uint8)
    return out, np.where(observed, SRC_PROJECTED, SRC_GRAY).astype(np.uint8)


# --------------------------------------------------------------------------- 격자 면


def _disk(r: int) -> np.ndarray:
    yy, xx = np.mgrid[-r : r + 1, -r : r + 1]
    return (xx * xx + yy * yy) <= r * r


def _raster_mask(tri_cells: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    tid, _, _ = rasterize(tri_cells, np.zeros(tri_cells.shape[:2]), shape, perspective=False)
    return tid >= 0


def _grid_prim(name: str, cells: np.ndarray, origin2d: np.ndarray, cell: float, to_world, tex_hw: tuple[int, int], tpc: int, image_key: int) -> Prim:
    """cells (K,2) 정수 (열 i, 행 j) 마다 사각형 2 삼각형. uv 는 격자 좌표 그대로 텍스처 전체에 대응(원점 왼쪽 위, glTF 규약)."""
    Hh, Ww = tex_hw
    i, j = cells[:, 0], cells[:, 1]
    corners = np.stack([np.stack([i, j], 1), np.stack([i + 1, j], 1), np.stack([i + 1, j + 1], 1), np.stack([i, j + 1], 1)], 1)
    pos = to_world(corners.reshape(-1, 2) * cell + origin2d)
    uv = np.stack([corners[..., 0] * tpc / Ww, corners[..., 1] * tpc / Hh], -1).reshape(-1, 2)
    base = np.arange(len(cells))[:, None] * 4
    idx = np.concatenate([base + [0, 1, 2], base + [0, 2, 3]], 0).astype(np.int64)
    return Prim(name=name, kind="estimated", pos=pos, idx=idx, uv=uv, image=image_key, wrap=(33071, 33071))


def _texture_for(cells_mask: np.ndarray, cell: float, tpc: int, texel_to_world, nrm: np.ndarray, vis: list[CamVis], cfg: EnhanceConfig,
                 deadline: float | None) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """채울 칸(격자 마스크 (행, 열))의 텍셀마다 색·출처를 정한다. 칸 밖 텍셀은 회색·0."""
    ny, nx = cells_mask.shape
    TH, TW = ny * tpc, nx * tpc
    tex = np.tile(GRAY, (TH, TW, 1)).astype(np.uint8)
    src = np.zeros((TH, TW), np.uint8)
    texel_mask = np.repeat(np.repeat(cells_mask, tpc, axis=0), tpc, axis=1)
    py, px = np.nonzero(texel_mask)
    P = texel_to_world(px, py)
    col, s = project_colors(P, nrm, vis, cfg, deadline)
    tex[py, px] = col
    src[py, px] = s
    n = max(len(py), 1)
    return tex, src, {"_region": texel_mask, "texels": int(len(py)), "projected_ratio": round(float((s == SRC_PROJECTED).sum()) / n, 4),
                      "gray_ratio": round(float((s == SRC_GRAY).sum()) / n, 4)}


def _tpc(cell: float, texel: float, nx: int, ny: int, max_side: int = 4096) -> int:
    tpc = max(1, round(cell / texel))
    while tpc > 1 and max(nx, ny) * tpc > max_side:
        tpc -= 1
    return tpc


def ray_floor_cells(vis: list[CamVis], z0: float, lo: np.ndarray, cell: float, shape: tuple[int, int], max_range: float, stride: int = 2) -> np.ndarray:
    """카메라 광선이 가림(메시) 없이 z=z0 평면에 닿는 격자 칸(행, 열). 그 칸은 사진에 바닥이 보인다는 근거다."""
    ny, nx = shape
    out = np.zeros(shape, bool)
    for cv in vis:
        cam = cv.cam
        K = np.asarray(cam.K, float)
        R = np.asarray(cam.R, float)
        H, W = cv.depth.shape
        jj, ii = np.meshgrid(np.arange(0, W, stride), np.arange(0, H, stride))
        u = (jj + 0.5) / cv.scale - 0.5
        v = (ii + 0.5) / cv.scale - 0.5
        d_cam = np.stack([(u - K[0, 2]) / K[0, 0], (v - K[1, 2]) / K[1, 1], np.ones_like(u)], -1)
        d_w = d_cam @ R  # R.T 를 오른쪽에서 곱한 것과 같다(행 벡터)
        c = cv.center
        dz = d_w[..., 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            lam = (z0 - c[2]) / dz
        dm = cv.depth[ii, jj]
        tol = np.maximum(0.05, 0.03 * lam)
        ok = (dz < -1e-3) & (lam > NEAR_M) & (lam < max_range) & ((dm == 0) | (dm >= lam - tol))
        P = c[None] + lam[ok][:, None] * d_w[ok]
        cx = np.floor((P[:, 0] - lo[0]) / cell).astype(int)
        cy = np.floor((P[:, 1] - lo[1]) / cell).astype(int)
        inb = (cx >= 0) & (cx < nx) & (cy >= 0) & (cy < ny)
        out[cy[inb], cx[inb]] = True
    return out


def build_floor_fill(mesh: Merged, keep: np.ndarray, z0: float, planes: list[dict[str, Any]], vis: list[CamVis], cfg: EnhanceConfig,
                     image_key: int, deadline: float | None = None) -> FillPrim:
    import scipy.ndimage as ndi

    cell = cfg.fill_cell_m
    near = keep & (np.abs(mesh.n[:, 2]) > 0.7) & (np.abs(mesh.cen[:, 2] - z0) < 0.12)
    if near.sum() < 50:
        raise EnhanceSkip("FILL_NO_FLOOR", "바닥 면 부족")
    xy = mesh.V[mesh.F[near]][:, :, :2]
    lo = xy.reshape(-1, 2).min(0) - cfg.fill_floor_reach_m
    hi = xy.reshape(-1, 2).max(0) + cfg.fill_floor_reach_m
    nx, ny = int(np.ceil((hi[0] - lo[0]) / cell)), int(np.ceil((hi[1] - lo[1]) / cell))
    if nx * ny > 4_000_000:
        raise EnhanceSkip("FILL_FLOOR_TOO_LARGE", f"바닥 격자 {nx}x{ny}")
    occ = _raster_mask((xy - lo) / cell, (ny, nx))
    r = max(1, int(round(cfg.fill_floor_close_m / cell)))
    closed = ndi.binary_closing(np.pad(occ, r), structure=_disk(r))[r:-r, r:-r]
    reach = ndi.binary_dilation(occ, structure=_disk(max(1, int(round(cfg.fill_floor_reach_m / cell)))))
    rays = ray_floor_cells(vis, z0, lo, cell, (ny, nx), cfg.fill_floor_ray_range_m)
    rays = ndi.binary_closing(np.pad(rays, 4), structure=_disk(4))[4:-4, 4:-4]
    region = closed | (rays & reach)
    region = ndi.binary_fill_holes(region)
    # 방 경계: 벽 평면 바깥쪽 칸은 뺀다(평면은 방 안쪽을 향하는 n, n·p = d)
    gx, gy = np.meshgrid(lo[0] + (np.arange(nx) + 0.5) * cell, lo[1] + (np.arange(ny) + 0.5) * cell)
    for pl in planes:
        region &= (gx * pl["n"][0] + gy * pl["n"][1] - pl["d"]) >= -cell * 0.6
    fill = region & ~ndi.binary_dilation(occ, structure=np.ones((3, 3), bool))
    if not fill.any():
        raise EnhanceSkip("FILL_NO_FLOOR_HOLE", "채울 바닥 구멍 없음")
    tpc = _tpc(cell, cfg.fill_texel_m, nx, ny)
    TH, TW = ny * tpc, nx * tpc

    def texel_to_world(px, py):
        return np.stack([lo[0] + (px + 0.5) * cell / tpc, lo[1] + (py + 0.5) * cell / tpc, np.full(len(px), z0)], 1)

    tex, src, tstats = _texture_for(fill, cell, tpc, texel_to_world, np.array([0.0, 0.0, 1.0]), vis, cfg, deadline)
    region_px = tstats.pop("_region")
    cj, ci = np.nonzero(fill)
    prim = _grid_prim("estimated_floor_fill", np.stack([ci, cj], 1), lo, cell,
                      lambda q: np.concatenate([q, np.full((len(q), 1), z0)], 1), (TH, TW), tpc, image_key)
    area = float(fill.sum() * cell * cell)
    stats = {"z_m": round(z0, 4), "region_m2": round(float(region.sum() * cell * cell), 3), "mesh_floor_m2": round(float(occ.sum() * cell * cell), 3),
             "fill_m2": round(area, 3), "ray_visible_m2": round(float(rays.sum() * cell * cell), 3), "cell_m": cell, "texel_m": round(cell / tpc, 4),
             "color_mode": cfg.fill_color_mode, **tstats,
             "area_by_source_m2": {"ESTIMATED_PLANE": round(area * tstats["projected_ratio"], 3), "UNOBSERVED": round(area * tstats["gray_ratio"], 3)},
             # 텍셀 (px, py) → World: origin + (px + 0.5) * texel_m * u_axis + (py + 0.5) * texel_m * v_axis (인페인팅 인터페이스)
             "frame": {"origin": [float(lo[0]), float(lo[1]), float(z0)], "u_axis": [1.0, 0.0, 0.0], "v_axis": [0.0, 1.0, 0.0],
                       "texel_m": cell / tpc, "size_px": [int(TW), int(TH)]}}
    return FillPrim(prim, tex, src, "floor", np.array([0.0, 0.0, 1.0]), stats, region_px)


def build_wall_fill(mesh: Merged, keep: np.ndarray, z0: float, pl: dict[str, Any], k: int, vis: list[CamVis], cfg: EnhanceConfig,
                    image_key: int, deadline: float | None = None) -> FillPrim:
    import scipy.ndimage as ndi

    cell = cfg.fill_cell_m
    n_, d_, t_ = np.array(pl["n"]), float(pl["d"]), np.array(pl["t"])
    u0, u1 = pl["u_range"]  # 평면 범위 = 지지 정점 u 의 0.5~99.5 분위(여유를 더하지 않는다: 범위 밖에 면을 만들지 않는다)
    z1 = min(float(pl["z_top"]), z0 + cfg.fill_wall_max_height_m)
    nu, nz = int(np.ceil((u1 - u0) / cell)), int(np.ceil((z1 - z0) / cell))
    if nu < 2 or nz < 2:
        raise EnhanceSkip("FILL_WALL_TOO_SMALL", f"벽 {k} 격자 {nu}x{nz}")
    dist = np.abs(mesh.V[:, :2] @ n_ - d_)
    near = keep & (dist[mesh.F] < 0.10).all(1)
    tri = mesh.V[mesh.F[near]]
    uu = tri[:, :, :2] @ t_
    cov = _raster_mask(np.stack([(uu - u0) / cell, (tri[:, :, 2] - z0) / cell], -1), (nz, nu))
    cov = ndi.binary_dilation(cov, structure=np.ones((3, 3), bool))
    fill = ~cov
    if fill.mean() > cfg.fill_wall_max_fill_frac:
        raise EnhanceSkip("FILL_WALL_UNSUPPORTED", f"벽 {k}: 구멍 비율 {fill.mean():.2f} 가 너무 크다(지지 부족)")
    if not fill.any():
        raise EnhanceSkip("FILL_NO_WALL_HOLE", f"벽 {k}: 채울 구멍 없음")
    tpc = _tpc(cell, cfg.fill_texel_m, nu, nz)
    TH, TW = nz * tpc, nu * tpc

    def to_world(q):  # q = (u, z)
        return np.stack([n_[0] * d_ + t_[0] * q[:, 0], n_[1] * d_ + t_[1] * q[:, 0], q[:, 1]], 1)

    def texel_to_world(px, py):
        return to_world(np.stack([u0 + (px + 0.5) * cell / tpc, z0 + (py + 0.5) * cell / tpc], 1))

    nrm3 = np.array([n_[0], n_[1], 0.0])
    tex, src, tstats = _texture_for(fill, cell, tpc, texel_to_world, nrm3, vis, cfg, deadline)
    region_px = tstats.pop("_region")
    cj, ci = np.nonzero(fill)
    prim = _grid_prim(f"estimated_wall_fill_{k}", np.stack([ci, cj], 1), np.array([u0, z0]), cell, to_world, (TH, TW), tpc, image_key)
    area = float(fill.sum() * cell * cell)
    stats = {"n": [round(float(x), 5) for x in n_], "d_m": round(d_, 4), "u_range_m": [round(u0, 3), round(u1, 3)], "z_range_m": [round(z0, 3), round(z1, 3)],
             "wall_m2": round(float(nu * nz * cell * cell), 3), "mesh_covered_m2": round(float(cov.sum() * cell * cell), 3), "fill_m2": round(area, 3),
             "color_mode": cfg.fill_color_mode, **tstats,
             "area_by_source_m2": {"ESTIMATED_PLANE": round(area * tstats["projected_ratio"], 3), "UNOBSERVED": round(area * tstats["gray_ratio"], 3)},
             "frame": {"origin": [float(n_[0] * d_ + t_[0] * u0), float(n_[1] * d_ + t_[1] * u0), float(z0)],
                       "u_axis": [float(t_[0]), float(t_[1]), 0.0], "v_axis": [0.0, 0.0, 1.0], "texel_m": cell / tpc, "size_px": [int(TW), int(TH)]}}
    return FillPrim(prim, tex, src, f"wall_{k}", np.array([n_[0], n_[1], 0.0]), stats, region_px)


# --------------------------------------------------------------------------- 진입점


def run_fill(model: SceneModel, cameras: list[CheckCamera], cfg: EnhanceConfig, deadline: float | None = None) -> FillResult:
    """늘어난 면 제거 + 바닥·벽 채움. 실패하면 EnhanceSkip. 모델은 바꾸지 않는다(결과만 돌려준다)."""
    if not cameras:
        raise EnhanceSkip("FILL_NO_CAMERAS", "보정 카메라·사진이 없어 채우지 않는다")
    steps: list[str] = []
    rep: dict[str, Any] = {"cameras": [c.camera_id for c in cameras], "color_mode": cfg.fill_color_mode, "purpose": "VISUAL_ONLY", "safety_use": False}
    mesh = merge_mesh(model.prims)
    keep = np.ones(len(mesh.F), bool)
    vis = [render_visibility(mesh, c, cfg.fill_vis_long_side, None, deadline) for c in cameras]
    new_idx: dict[int, np.ndarray] = {}
    removed = 0
    if cfg.fill_stretched:
        drop, rec = stretched_faces(mesh, vis, cfg)
        rep["stretched"] = rec
        if drop.any():
            keep = ~drop
            removed = int(drop.sum())
            steps.append("stretched_removal")
            for k in np.unique(mesh.face_prim[drop]):
                sel = mesh.face_prim == k
                new_idx[int(k)] = model.prims[int(k)].idx[keep[sel]]
            vis = [render_visibility(mesh, c, cfg.fill_vis_long_side, keep, deadline) for c in cameras]  # 지운 면이 가리지 않게 다시
    else:
        rep["stretched"] = {"applied": False, "reason": "DISABLED"}
    fills: list[FillPrim] = []
    z0, zinfo = floor_level(mesh, keep, cfg)
    rep["floor_level"] = zinfo
    planes: list[dict[str, Any]] = []
    if cfg.fill_floor or cfg.fill_walls:
        planes, rejected = find_wall_planes(mesh, keep, z0, cfg)
        rep["wall_planes"] = {"accepted": [{k: v for k, v in p.items() if k != "t"} for p in planes], "rejected": rejected}
    if cfg.fill_floor:
        try:
            fp = build_floor_fill(mesh, keep, z0, planes, vis, cfg, IMAGE_KEY0 + len(fills), deadline)
            fills.append(fp)
            rep["floor"] = {"applied": True, **fp.stats}
            steps.append("floor_fill")
        except EnhanceSkip as e:
            rep["floor"] = {"applied": False, "reason": e.reason}
    if cfg.fill_walls:
        rep["walls"] = []
        for k, pl in enumerate(planes):
            try:
                fp = build_wall_fill(mesh, keep, z0, pl, k, vis, cfg, IMAGE_KEY0 + len(fills), deadline)
                fills.append(fp)
                rep["walls"].append({"applied": True, **fp.stats})
            except EnhanceSkip as e:
                rep["walls"].append({"applied": False, "plane": k, "reason": e.reason})
        if any(w.get("applied") for w in rep["walls"]):
            steps.append("wall_fill")
    tris = sum(len(f.prim.idx) for f in fills)
    by_src = {"ESTIMATED_PLANE": 0.0, "UNOBSERVED": 0.0}
    for f in fills:
        for key, v in f.stats["area_by_source_m2"].items():
            by_src[key] += v
    fill_area = sum(by_src.values())
    mesh_area = float(mesh.area[keep].sum())
    rep["surface_source"] = {
        "values": {"ESTIMATED_PLANE": SRC_PROJECTED, "UNOBSERVED": SRC_GRAY},
        "meaning": {"ESTIMATED_PLANE": "색은 보정 카메라 사진 투영(관측 픽셀)이고 형상은 맞춘 평면", "UNOBSERVED": "사진 근거 없음, 중립 회색(색을 지어내지 않음)"},
        "fill_area_m2": {k: round(v, 3) for k, v in by_src.items()},
        "fill_ratio": {k: round(v / fill_area, 4) if fill_area > 0 else None for k, v in by_src.items()},
        "fill_share_of_surface": round(fill_area / max(fill_area + mesh_area, 1e-12), 4),
        "mesh_area_m2_after_removal": round(mesh_area, 3)}
    rep["removed_faces"], rep["added_triangles"] = removed, int(tris)
    return FillResult(new_idx, fills, rep, int(tris) - removed, steps)


# --------------------------------------------------------------------------- PNG(의존성 없이)


def encode_png(img: np.ndarray) -> bytes:
    """(H, W) uint8 또는 (H, W, 3) uint8 → PNG 바이트(무손실)."""
    a = np.ascontiguousarray(img, np.uint8)
    h, w = a.shape[:2]
    c = 1 if a.ndim == 2 else a.shape[2]
    color_type = {1: 0, 3: 2}[c]
    raw = np.concatenate([np.zeros((h, 1), np.uint8), a.reshape(h, w * c)], axis=1).tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def write_sidecar(fills: list[FillPrim], out_dir) -> list[dict[str, Any]]:
    """평면마다 별도 이미지를 저장한다(다음 단계 AI 인페인팅 입력): `<role>_albedo.png`(사진 투영 + 회색), `<role>_inpaint_mask.png`(255 = 사진 근거 없는
    채울 곳), `<role>_surface_source.png`(0/3). 이미지와 같은 해상도, 텍셀 → World 는 `fill_atlas.json` 의 frame. 인페인팅은 mask 밖 픽셀을 바꾸지 않는다."""
    import json
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for fp in fills:
        files = {"albedo": f"{fp.role}_albedo.png", "inpaint_mask": f"{fp.role}_inpaint_mask.png", "surface_source": f"{fp.role}_surface_source.png"}
        (out / files["albedo"]).write_bytes(encode_png(fp.image))
        (out / files["inpaint_mask"]).write_bytes(encode_png(fp.inpaint_mask))
        (out / files["surface_source"]).write_bytes(encode_png(fp.source))
        entries.append({"role": fp.role, "files": files, "frame": fp.stats["frame"], "normal_world": [float(x) for x in fp.normal_world],
                        "fill_m2": fp.stats["fill_m2"], "texel_ratios": {"ESTIMATED_PLANE": fp.stats["projected_ratio"], "UNOBSERVED": fp.stats["gray_ratio"]}})
    doc = {"format": "simai.fill_atlas", "version": "1.0.0", "purpose": "VISUAL_ONLY", "safety_use": False,
           "image_convention": "행 0 = 위(glTF UV 원점 왼쪽 위), World→glTF 변환은 GLB 어댑터 한 곳이다. frame 은 World(m, +Z 상방) 기준",
           "source_values": {"ESTIMATED_PLANE": SRC_PROJECTED, "UNOBSERVED": SRC_GRAY}, "planes": entries}
    (out / "fill_atlas.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return entries
