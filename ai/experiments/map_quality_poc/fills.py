"""바닥·벽 평면 채우기. 메시 표면이 이미 있는 칸은 건드리지 않고 '구멍'에만 격자 면을 만든다.
텍스처: 카메라에서 보이는 칸은 사진 투영, 못 본 칸은 회색(관측/추정을 눈으로 구분). PoC 전용."""
from __future__ import annotations

import cv2
import numpy as np
from scipy import ndimage

import common as C
import meshvis as M

ge = C.ge
GRAY = np.array([128, 128, 128], np.uint8)
CELL = 0.05  # 면 격자 m
TEXEL = 0.01  # 텍스처 m/화소


def floor_level(mesh):
    """바닥 높이: 바닥 근처 수평면의 넓이 가중 중앙값(분포가 -0.1~0.14 m 로 넓어 최빈값은 잡음). |중앙값| < 6 cm 이면 World Z=0 을 쓴다."""
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    sel = (np.abs(n[:, 2]) > 0.9) & (cen[:, 2] > -0.15) & (cen[:, 2] < 0.15)
    z = cen[sel, 2]
    o = np.argsort(z)
    cw = np.cumsum(area[sel][o])
    med = float(z[o][np.searchsorted(cw, cw[-1] / 2)])
    q = np.percentile(z, [5, 95])
    return (0.0 if abs(med) < 0.06 else med), {"floor_area_weighted_median_z": med, "floor_z_p5_p95": q.tolist()}


def _rect_raster(tri_uv, shape):
    tid, _, _ = ge.rasterize(tri_uv, np.zeros(tri_uv.shape[:2]), shape, perspective=False)
    return tid >= 0


def project_texels(cams, cam_idx, vis, P, nrm, mode, cos_min=0.15):
    """P (N,3) 칸 중심에서 카메라별 가시 검사 후 최선 카메라 색.
    mode: 'strict' = 메시가 그 점에서 확인될 때만 보임, 'lenient' = 시선 위에 메시가 없어도(구멍) 보임으로 친다."""
    N = len(P)
    best = np.full(N, -1)
    bscore = np.zeros(N)
    colors = np.zeros((N, 3), np.uint8)
    for ci in cam_idx:
        cam = cams[ci]
        H, W = cam.image.shape[:2]
        u, v, z = M.project(cam, P)
        iu, iv = np.rint(u).astype(int), np.rint(v).astype(int)
        inb = (iu >= 1) & (iu < W - 1) & (iv >= 1) & (iv < H - 1) & (z > 0.05)
        ii = np.nonzero(inb)[0]
        dm = vis.depth[ci][iv[ii], iu[ii]]
        tol = np.maximum(0.05, 0.03 * z[ii])
        if mode == "strict":
            dok = (dm > 0) & (np.abs(dm - z[ii]) < tol)
        else:
            dok = (dm == 0) | (dm >= z[ii] - tol)
        vok = vis.valid[ci][iv[ii], iu[ii]]
        d = M.cam_center(cam)[None] - P[ii]
        dist = np.linalg.norm(d, axis=1)
        cosn = np.abs((nrm[None] * d).sum(1)) / np.maximum(dist, 1e-9)
        ok = dok & vok & (cosn >= cos_min)
        sc = np.zeros(N)
        sc[ii[ok]] = cosn[ok] / np.maximum(dist[ok], 0.1) ** 2
        upd = sc > bscore
        if upd.any():
            sel = np.nonzero(upd)[0]
            colors[sel] = M.sample_image(cam.image, u[sel], v[sel])
            best[sel] = ci
            bscore[sel] = sc[sel]
    colors[best < 0] = GRAY
    return colors, best


def _grid_prim(cells, origin2d, cell, to_world, tex_shape, texel_per_cell):
    """cells (K,2) 정수 (열 i, 행 j) 마다 사각형 2 삼각형. uv 는 격자 좌표 그대로 텍스처 전체에 대응."""
    Hh, Ww = tex_shape
    i, j = cells[:, 0], cells[:, 1]
    corners = np.stack([np.stack([i, j], 1), np.stack([i + 1, j], 1), np.stack([i + 1, j + 1], 1), np.stack([i, j + 1], 1)], 1)
    pos = to_world(corners.reshape(-1, 2) * cell + origin2d)
    uv = np.stack([corners[..., 0] * texel_per_cell / Ww, corners[..., 1] * texel_per_cell / Hh], -1).reshape(-1, 2)
    base = np.arange(len(cells))[:, None] * 4
    idx = np.concatenate([base + [0, 1, 2], base + [0, 2, 3]], 0).astype(np.int64)
    return pos, uv, idx


def build_floor(mesh, vis, cams, cam_idx, z0, mode="strict", close_m=0.6, keep=None):
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    near = (np.abs(n[:, 2]) > 0.7) & (np.abs(cen[:, 2] - z0) < 0.12)
    if keep is not None:
        near &= keep  # 지운 면 자리는 구멍으로 보고 채운다
    xy = mesh.V[mesh.F[near]][:, :, :2]
    lo, hi = xy.reshape(-1, 2).min(0) - 0.5, xy.reshape(-1, 2).max(0) + 0.5
    nx, ny = int(np.ceil((hi[0] - lo[0]) / CELL)), int(np.ceil((hi[1] - lo[1]) / CELL))
    occ = _rect_raster((xy - lo) / CELL, (ny, nx))  # [행=y, 열=x]
    r = int(close_m / CELL)
    st = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    region = cv2.morphologyEx(occ.astype(np.uint8), cv2.MORPH_CLOSE, st).astype(bool)
    region = ndimage.binary_fill_holes(region)
    fill = region & ~cv2.dilate(occ.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    cj, ci_ = np.nonzero(fill)
    cells = np.stack([ci_, cj], 1)
    tpc = int(round(CELL / TEXEL))
    TH, TW = ny * tpc, nx * tpc
    tex = np.tile(GRAY, (TH, TW, 1)).astype(np.uint8)
    obs = np.zeros((TH, TW), np.uint8)
    sub_px = cv2.resize(fill.astype(np.uint8), (TW, TH), interpolation=cv2.INTER_NEAREST).astype(bool)
    py, px = np.nonzero(sub_px)
    P = np.stack([lo[0] + (px + 0.5) * TEXEL, lo[1] + (py + 0.5) * TEXEL, np.full(len(px), z0)], 1)
    col, best = project_texels(cams, cam_idx, vis, P, np.array([0, 0, 1.0]), mode)
    tex[py, px] = col
    obs[py, px] = best + 1
    pos, uv, idx = _grid_prim(cells, lo, CELL, lambda q: np.concatenate([q, np.full((len(q), 1), z0)], 1), (TH, TW), tpc)
    stats = {"region_m2": float(region.sum() * CELL**2), "mesh_floor_m2": float(occ.sum() * CELL**2),
             "fill_m2": float(fill.sum() * CELL**2), "observed_frac_of_fill": float((best >= 0).mean()), "mode": mode,
             "bbox_lo": lo.tolist(), "bbox_hi": hi.tolist(), "z0": z0}
    prim = ge.Prim("floor_fill", "estimated", pos, idx, uv=uv, image=0, wrap=(33071, 33071))
    return prim, tex, obs, stats


# --------------------------------------------------------------------------- 벽
def find_wall_planes(mesh, z0, rng_seed=0, dist_tol=0.06, min_area=3.0, min_len=1.5, manhattan_deg=8.0, max_planes=8, n_iter=2500, min_height=1.5, merge_d=0.25, max_outside=0.08):
    """수직면 후보: 법선이 수평에 가까운 면(|nz|<0.3)의 중심·법선·넓이로 순차 RANSAC."""
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    vert = (np.abs(n[:, 2]) < 0.3) & (cen[:, 2] > z0 + 0.1) & (cen[:, 2] < z0 + 2.6)
    ids = np.nonzero(vert)[0]
    p2, a, n2 = cen[ids, :2], area[ids], n[ids, :2]
    n2 = n2 / np.maximum(np.linalg.norm(n2, axis=1, keepdims=True), 1e-9)
    ang = np.arctan2(n2[:, 1], n2[:, 0])
    hist, bins = np.histogram(np.mod(ang, np.pi / 2), bins=90, range=(0, np.pi / 2), weights=a)
    th0 = float(bins[np.argmax(hist)] + np.pi / 360)
    rng = np.random.default_rng(rng_seed)
    alive = np.ones(len(ids), bool)
    planes, rejected = [], []
    for _ in range(max_planes * 2):
        cand = np.nonzero(alive)[0]
        if len(cand) < 50:
            break
        pick = rng.choice(cand, size=min(n_iter, len(cand)), replace=False, p=a[cand] / a[cand].sum())
        best, best_area = None, 0
        pc, nc_, ac = p2[cand], n2[cand], a[cand]
        for k in pick:
            dist = np.abs((pc - p2[k]) @ n2[k])
            inl = (dist < dist_tol) & (np.abs(nc_ @ n2[k]) > 0.9)
            s = ac[inl].sum()
            if s > best_area:
                best, best_area = (k, inl), s
        if best is None or best_area < min_area:
            break
        k, inl = best
        sel = cand[inl]
        w = a[sel]
        c = (p2[sel] * w[:, None]).sum(0) / w.sum()
        cov = ((p2[sel] - c).T * w) @ (p2[sel] - c)
        ev, evec = np.linalg.eigh(cov)
        t = evec[:, 1]
        nrm = np.array([-t[1], t[0]])
        if nrm @ n2[k] < 0:
            nrm = -nrm
        u = (p2[sel] - c) @ t
        length = float(np.percentile(u, 99) - np.percentile(u, 1))
        theta = np.arctan2(nrm[1], nrm[0])
        dev = abs(((theta - th0 + np.pi / 4) % (np.pi / 2)) - np.pi / 4)
        rec = {"n_faces": int(len(sel)), "area_m2": float(w.sum()), "length_m": length,
               "dev_from_manhattan_deg": float(np.degrees(dev)), "normal_deg": float(np.degrees(theta))}
        alive[sel] = False
        if length < min_len:
            rec["rejected"] = "length"
            rejected.append(rec)
            continue
        if np.degrees(dev) > manhattan_deg:
            rec["rejected"] = "not_orthogonal"
            rejected.append(rec)
            continue
        snapped = th0 + round((theta - th0) / (np.pi / 2)) * (np.pi / 2)
        nrm = np.array([np.cos(snapped), np.sin(snapped)])
        t = np.array([-nrm[1], nrm[0]])
        d = float(np.median(p2[sel] @ nrm))
        verts = mesh.V[mesh.F[ids[sel]]].reshape(-1, 3)
        h95 = float(np.percentile(cen[ids[sel], 2], 95) - z0)
        rec["height_p95_m"] = h95
        hb = np.histogram(cen[ids[sel], 2] - z0, bins=np.arange(0, 2.41, 0.3), weights=a[sel])[0]
        rec["height_bins_occupied"] = int((hb >= 0.03 * a[sel].sum()).sum())
        if h95 < min_height or rec["height_bins_occupied"] < 5:
            rec["rejected"] = "low_or_partial_height(가구 옆면 등)"
            rejected.append(rec)
            continue
        fl = (np.abs(n[:, 2]) > 0.7) & (np.abs(cen[:, 2] - z0) < 0.3)
        outside = float(area[fl & (cen[:, :2] @ nrm < d - 0.15)].sum() / area[fl].sum())
        rec["floor_outside_frac"] = outside
        if outside > max_outside:
            rec["rejected"] = "not_room_boundary(바닥이 평면 바깥쪽에도 있음, 방 안 구조물)"
            rejected.append(rec)
            continue
        uu = verts[:, :2] @ t
        rec.update({"n": nrm.tolist(), "t": t.tolist(), "d": d,
                    "u_range": [float(np.percentile(uu, 1)), float(np.percentile(uu, 99))],
                    "z_top": float(np.percentile(verts[:, 2], 99))})
        dup = next((q for q in planes if abs(np.dot(q["n"], nrm)) > 0.99 and abs(q["d"] - d * np.sign(np.dot(q["n"], nrm))) < merge_d), None)
        if dup is not None:  # 같은 벽의 두 겹(잡음) -> 합침(범위 합집합)
            rec["rejected"] = "duplicate_of_earlier"
            rejected.append(rec)
            if np.dot(dup["t"], t) > 0:
                dup["u_range"] = [min(dup["u_range"][0], rec["u_range"][0]), max(dup["u_range"][1], rec["u_range"][1])]
            else:
                dup["u_range"] = [min(dup["u_range"][0], -rec["u_range"][1]), max(dup["u_range"][1], -rec["u_range"][0])]
            continue
        planes.append(rec)
        if len(planes) >= max_planes:
            break
    return planes, rejected, {"manhattan_theta0_deg": float(np.degrees(th0)), "vertical_faces": int(len(ids))}


def build_wall(mesh, vis, cams, cam_idx, z0, pl, tag, mode="lenient", keep=None):
    n_, d_, t_ = np.array(pl["n"]), pl["d"], np.array(pl["t"])
    u0, u1 = pl["u_range"]
    u0, u1 = u0 - 0.1, u1 + 0.1
    z1 = pl["z_top"]
    nu, nz = int(np.ceil((u1 - u0) / CELL)), int(np.ceil((z1 - z0) / CELL))
    V = mesh.V
    dist = np.abs(V[:, :2] @ n_ - d_)
    near = (dist[mesh.F] < 0.10).all(1)
    if keep is not None:
        near &= keep
    tri = V[mesh.F[near]]
    uu = tri[:, :, :2] @ t_
    uvz = np.stack([(uu - u0) / CELL, (tri[:, :, 2] - z0) / CELL], -1)
    cov = _rect_raster(uvz, (nz, nu))
    cov = cv2.dilate(cov.astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
    fill = ~cov
    cj, ci_ = np.nonzero(fill)
    cells = np.stack([ci_, cj], 1)
    tpc = int(round(CELL / TEXEL))
    TH, TW = nz * tpc, nu * tpc
    tex = np.tile(GRAY, (TH, TW, 1)).astype(np.uint8)
    obs = np.zeros((TH, TW), np.uint8)
    sub_px = cv2.resize(fill.astype(np.uint8), (TW, TH), interpolation=cv2.INTER_NEAREST).astype(bool)
    py, px = np.nonzero(sub_px)
    uc = u0 + (px + 0.5) * TEXEL
    zc = z0 + (py + 0.5) * TEXEL
    P = np.stack([n_[0] * d_ + t_[0] * uc, n_[1] * d_ + t_[1] * uc, zc], 1)
    col, best = project_texels(cams, cam_idx, vis, P, np.r_[n_, 0.0], mode)
    tex[py, px] = col
    obs[py, px] = best + 1

    def to_world(q):  # q = (u, z)
        return np.stack([n_[0] * d_ + t_[0] * q[:, 0], n_[1] * d_ + t_[1] * q[:, 0], q[:, 1]], 1)

    pos, uv, idx = _grid_prim(cells, np.array([u0, z0]), CELL, to_world, (TH, TW), tpc)
    stats = {"u_range": [u0, u1], "z_range": [z0, z1], "wall_m2": float(nu * nz * CELL**2), "mesh_covered_m2": float(cov.sum() * CELL**2),
             "fill_m2": float(fill.sum() * CELL**2), "observed_frac_of_fill": float((best >= 0).mean()), "mode": mode, "n": pl["n"], "d": d_}
    prim = ge.Prim(tag, "estimated", pos, idx, uv=uv, image=0, wrap=(33071, 33071))
    return prim, tex, obs, stats
