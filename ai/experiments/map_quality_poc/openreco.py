"""OpenReco 방식 굽기: 기하 그대로, xatlas UV, 면마다 최선 카메라(가시 화소 수·각도·가림), 텍셀 단위 깊이 검사,
카메라별 노출 이득 보정. PoC 전용(운영 코드 무변경)."""
from __future__ import annotations

import time

import cv2
import numpy as np
from scipy import ndimage

import common as C
import meshvis as M

ge = C.ge
GRAY = np.array([128, 128, 128], np.uint8)


# --------------------------------------------------------------------------- UV
def make_atlas(mesh: M.MeshData, resolution=4096, padding=2, tpu=120.0):
    """xatlas 를 면 번호 구간(chunk)별로 돌려 2x2 사분면에 배치한다. 한꺼번에 돌리면 150k 면(잡음 많은 회색 메시 + 매끈한 텍스처 메시)에서
    15분 넘게 끝나지 않아(패킹 단계로 추정, 구간별로는 각 1~3 s) 구간으로 나눴다. 삼각형 순서는 보존된다."""
    import xatlas

    m = len(mesh.F)
    first_tex = min((s[1] for s in mesh.slices if s[0].startswith("textured_")), default=m // 2)
    third = (m - first_tex) // 3
    bounds = [0, first_tex, first_tex + third, first_tex + 2 * third, m]
    half = resolution // 2
    t0 = time.time()
    vmaps, inds, uvs, voff, charts = [], [], [], 0, 0
    for k in range(4):
        F = mesh.F[bounds[k]:bounds[k + 1]]
        used, inv = np.unique(F, return_inverse=True)
        atlas = xatlas.Atlas()
        atlas.add_mesh(mesh.V[used].astype(np.float32), inv.reshape(-1, 3).astype(np.uint32))
        co, po = xatlas.ChartOptions(), xatlas.PackOptions()
        po.resolution, po.padding, po.bilinear, po.texels_per_unit = half, padding, True, tpu
        atlas.generate(co, po)
        vm, idx, uv = atlas[0]
        assert atlas.width <= half and atlas.height <= half, (k, atlas.width, atlas.height)
        assert len(idx) == len(F) and np.allclose(mesh.V[used][vm][idx], mesh.V[F], atol=1e-5), "xatlas 가 삼각형 순서를 바꿨다"
        off = np.array([(k % 2) * 0.5, (k // 2) * 0.5])
        uvs.append(uv * np.array([atlas.width, atlas.height]) / resolution + off)
        vmaps.append(used[vm])
        inds.append(idx.astype(np.int64) + voff)
        voff += len(vm)
        charts += int(atlas.chart_count)
        print(f"  atlas chunk {k}: faces {len(F)} charts {atlas.chart_count} size {atlas.width}x{atlas.height} util {atlas.utilization:.2f}", flush=True)
    return {"vmapping": np.concatenate(vmaps), "indices": np.concatenate(inds), "uv": np.concatenate(uvs),
            "size": int(resolution), "sec": time.time() - t0, "charts": charts, "tpu": tpu, "chunks": bounds}


# --------------------------------------------------------------------------- 노출 이득
def exposure_gains(mesh, vis, cams, score, use=None, cos_pair=0.5, px_pair=20):
    """겹쳐 보이는 면의 중심 색(흐린 사진, 선형)을 카메라 쌍마다 비교해 채널별 로그 이득 이음새 보정. 반환 (ncam,3) 곱셈 이득."""
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    nc = len(cams)
    use = list(range(nc)) if use is None else list(use)
    samp = np.full((len(cen), nc, 3), np.nan)
    for k in use:
        cam = cams[k]
        img = cv2.blur(cam.image, (15, 15))
        lin = ge.srgb_to_linear(img.astype(np.float64) / 255.0)
        ok = (vis.vis_px[:, k] >= px_pair) & (vis.cos[:, k] >= cos_pair)
        u, v, z = M.project(cam, cen[ok])
        inb = (u >= 0) & (u < img.shape[1] - 1) & (v >= 0) & (v < img.shape[0] - 1)
        idx = np.nonzero(ok)[0][inb]
        samp[idx, k] = ge.srgb_to_linear(M.sample_image(img, u[inb], v[inb]).astype(np.float64) / 255.0)
    rows, rhs = [], []
    for i in use:
        for j in use:
            if j <= i:
                continue
            both = ~np.isnan(samp[:, i, 0]) & ~np.isnan(samp[:, j, 0])
            if both.sum() < 30:
                continue
            r = np.median(np.log(np.maximum(samp[both, i], 1e-4)) - np.log(np.maximum(samp[both, j], 1e-4)), axis=0)
            row = np.zeros(nc); row[i], row[j] = 1, -1
            rows.append(row); rhs.append(r)
    if not rows:
        return np.ones((nc, 3)), {}
    cons = np.zeros(nc); cons[use] = 10.0
    A = np.vstack(rows + [cons])  # 합 0 제약
    b = np.vstack(rhs + [np.zeros(3)])
    g = np.linalg.lstsq(A, b, rcond=None)[0]  # (nc,3) 로그 이득(카메라가 찍은 색 = 참값 + g)
    info = {"pairs": len(rows), "log_gain": np.round(g, 3).tolist()}
    return np.exp(-g), info


# --------------------------------------------------------------------------- 굽기
def bake(mesh, vis, cams, cam_idx, atlas, score=None, gains=None, face_keep=None):
    """cam_idx 의 카메라만으로 아틀라스를 굽는다. 반환: (rgb uint8 HxWx3, obs HxW uint8: 0 = 미관측, k+1 = 카메라 번호(cam_idx 순서 아님, 전체 번호), stats)."""
    t0 = time.time()
    S = atlas["size"]
    if score is None:
        score = M.view_score(vis)
    sub = score[:, cam_idx]
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    uvt = atlas["uv"][atlas["indices"]]  # (m,3,2) 0..1
    tid, lam, _ = ge.rasterize(uvt * S, np.zeros((len(mesh.F), 3)), (S, S), perspective=False)
    ys, xs = np.nonzero(tid >= 0)
    f = tid[ys, xs]
    w = lam[ys, xs].astype(np.float64)
    P = (mesh.V[mesh.F[f]] * w[:, :, None]).sum(1)
    rank = np.argsort(-sub, axis=1)  # (m, ncam_sub)
    rgb = np.zeros((S, S, 3), np.uint8)
    obs = np.zeros((S, S), np.uint8)
    pending = np.ones(len(f), bool)
    if face_keep is not None:
        pending &= face_keep[f]
    for r in range(len(cam_idx)):
        loc = rank[f, r]
        has = sub[f, loc] > 0
        todo = pending & has
        for li, ci in enumerate(cam_idx):
            sel = np.nonzero(todo & (loc == li))[0]
            if not len(sel):
                continue
            cam = cams[ci]
            u, v, z = M.project(cam, P[sel])
            H, W = cam.image.shape[:2]
            iu, iv = np.rint(u).astype(int), np.rint(v).astype(int)
            inb = (iu >= 1) & (iu < W - 1) & (iv >= 1) & (iv < H - 1) & (z > 0.05)
            ok = np.zeros(len(sel), bool)
            ii = np.nonzero(inb)[0]
            dm = vis.depth[ci][iv[ii], iu[ii]]
            tol = np.maximum(0.04, 0.025 * z[ii])
            depth_ok = (dm > 0) & (np.abs(dm - z[ii]) < tol)
            val_ok = vis.valid[ci][iv[ii], iu[ii]]
            c = M.cam_center(cam)
            d = c[None] - P[sel[ii]]
            cosn = np.abs((n[f[sel[ii]]] * d).sum(1)) / np.maximum(np.linalg.norm(d, axis=1), 1e-9)
            ok[ii] = depth_ok & val_ok & (cosn >= 0.1)
            got = sel[ok]
            if not len(got):
                continue
            col = M.sample_image(cam.image, u[ok], v[ok])
            if gains is not None:
                lin = ge.srgb_to_linear(col.astype(np.float64) / 255.0) * gains[ci][None]
                col = np.rint(ge.linear_to_srgb(np.clip(lin, 0, 1)) * 255).astype(np.uint8)
            rgb[ys[got], xs[got]] = col
            obs[ys[got], xs[got]] = ci + 1
            pending[got] = False
    unobs = pending.copy()
    if face_keep is not None:
        unobs |= ~face_keep[f]  # 거른 면은 회색(모름)
    rgb[ys[unobs], xs[unobs]] = GRAY
    face_mask = np.zeros((S, S), bool)
    face_mask[ys, xs] = True
    # 패딩: 가장 가까운 면 텍셀 색으로 4 화소까지
    dist, (iy, ix) = ndimage.distance_transform_edt(~face_mask, return_indices=True)
    pad = (~face_mask) & (dist <= 4)
    rgb[pad] = rgb[iy[pad], ix[pad]]
    rgb[(~face_mask) & ~pad] = GRAY
    stats = {"texels_in_faces": int(len(f)), "observed_texel_frac": float((~unobs).sum() / max(len(f), 1)),
             "bake_sec": time.time() - t0}
    return rgb, obs, stats


def atlas_prim(mesh, atlas, image_id=0, keep=None, name="openreco"):
    idx = atlas["indices"] if keep is None else atlas["indices"][keep]
    return ge.Prim(name, "observed", mesh.V[atlas["vmapping"]], idx, uv=atlas["uv"], image=image_id, wrap=(33071, 33071))


def photo_consistency(mesh, vis, cams, use, gains, thr=0.10, px_min=8, cos_min=0.3):
    """면 중심을 보이는 카메라마다 흐린 사진에서 읽어(노출 보정 뒤) 카메라 간 색 차이를 잰다.
    기하가 사진과 어긋난 면(TSDF 잡음, 사람 흔적)은 카메라마다 색이 달라 '모름(회색)'으로 돌린다.
    반환 ok(m,): 2대 이상 보이고 최대 쌍 차이 < thr 이거나, 원래 텍스처 면(texrecon 이 관측으로 판정)이 1대에만 보인 경우."""
    n, area, e, cen = M.face_geom(mesh.V, mesh.F)
    nc = len(cams)
    samp = np.full((len(cen), nc, 3), np.nan)
    for k in use:
        cam = cams[k]
        img = cv2.blur(cam.image, (9, 9))
        ok = (vis.vis_px[:, k] >= px_min) & (vis.cos[:, k] >= cos_min)
        u, v, z = M.project(cam, cen[ok])
        inb = (u >= 0) & (u < img.shape[1] - 1) & (v >= 0) & (v < img.shape[0] - 1)
        idx = np.nonzero(ok)[0][inb]
        c = M.sample_image(img, u[inb], v[inb]).astype(np.float64) / 255.0
        lin = ge.srgb_to_linear(c) * gains[k][None]
        samp[idx, k] = ge.linear_to_srgb(np.clip(lin, 0, 1))
    cnt = (~np.isnan(samp[:, :, 0])).sum(1)
    mx = np.zeros(len(cen))
    for i in use:
        for j in use:
            if j > i:
                d = np.nanmean(np.abs(samp[:, i] - samp[:, j]), axis=1)
                mx = np.where(np.isnan(d), mx, np.maximum(mx, d))
    textured = ~mesh.neutral
    return ((cnt >= 2) & (mx < thr)) | ((cnt == 1) & textured), {"inconsistent_faces": int(((cnt >= 2) & (mx >= thr)).sum()), "single_view_neutral_gray": int(((cnt == 1) & ~textured).sum())}
