"""병합 메시 읽기, 카메라별 면 가시성(id 버퍼·깊이·보이는 화소 수), 늘어난 면 판정. PoC 전용."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

import common as C

ge = C.ge


@dataclass
class MeshData:
    V: np.ndarray  # (n,3) World m
    F: np.ndarray  # (m,3)
    neutral: np.ndarray  # (m,) bool, texrecon 이 못 본 면(회색 메시)
    slices: list  # [(prim 이름, 시작, 끝)] 병합 면 번호 범위, 원본 prim 순서
    plane_prim: object = None  # 기존 estimated_floor_plane (있으면)
    g: object = None
    model: object = None


def load_mesh(path: Path = C.BASELINE_GLB) -> MeshData:
    g, model = C.load_glb(path)
    Vs, Fs, neu, slices, off, nf = [], [], [], [], 0, 0
    plane = None
    for p in model.prims:
        if p.name == "estimated_floor_plane":
            plane = p
            continue
        Vs.append(p.pos)
        Fs.append(p.idx + off)
        neu.append(np.full(len(p.idx), p.kind == "unobserved"))
        slices.append((p.name, nf, nf + len(p.idx)))
        off += len(p.pos)
        nf += len(p.idx)
    V, F = np.concatenate(Vs), np.concatenate(Fs)
    key = np.round(V / 1e-5).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    V = V[first]
    F = inv.reshape(-1)[F]
    return MeshData(V, F, np.concatenate(neu), slices, plane, g, model)


def face_geom(V, F):
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    n = np.cross(b - a, c - a)
    area = 0.5 * np.linalg.norm(n, axis=1)
    n = n / np.maximum(2 * area, 1e-12)[:, None]
    e = np.stack([np.linalg.norm(b - a, axis=1), np.linalg.norm(c - b, axis=1), np.linalg.norm(a - c, axis=1)], 1)
    return n, area, e, (a + b + c) / 3


def cam_center(cam):
    return -np.asarray(cam.R).T @ np.asarray(cam.t).reshape(3)


def project(cam, P):
    """World → 화소 인덱스 좌표(OpenCV, 화소 중심이 정수), 카메라 z."""
    pc = P @ np.asarray(cam.R).T + np.asarray(cam.t).reshape(3)
    z = pc[..., 2]
    zz = np.where(np.abs(z) < 1e-9, 1e-9, z)
    return cam.K[0, 0] * pc[..., 0] / zz + cam.K[0, 2], cam.K[1, 1] * pc[..., 1] / zz + cam.K[1, 2], z


def render_ids(cam, V, F, near=0.05, keep=None):
    """전체 해상도 id 버퍼(-1 = 없음)와 카메라 z 깊이(없으면 0). keep: 그릴 면 마스크."""
    H, W = cam.image.shape[:2]
    ids = np.arange(len(F)) if keep is None else np.nonzero(keep)[0]
    P = V[F[ids]]
    u, v, z = project(cam, P)
    ok = z.min(1) >= near
    ids, u, v, z = ids[ok], u[ok], v[ok], z[ok]
    tid, _, depth = ge.rasterize(np.stack([u + 0.5, v + 0.5], axis=2), z, (H, W), perspective=True)
    tid = np.where(tid >= 0, ids[np.maximum(tid, 0)], -1)
    return tid, np.where(np.isfinite(depth), depth, 0.0).astype(np.float32)


@dataclass
class Vis:
    vis_px: np.ndarray  # (m,ncam) 보이는 화소 수(유효 마스크 안)
    vis_all: np.ndarray  # (m,ncam) 보이는 화소 수(마스크 무관)
    area_px: np.ndarray  # (m,ncam) 가림 무시한 투영 넓이(화소)
    cos: np.ndarray  # (m,ncam) |법선·시선|
    dist: np.ndarray  # (m,ncam) 면 중심까지 거리
    depth: list = field(default_factory=list)  # 카메라별 깊이 버퍼
    valid: list = field(default_factory=list)  # 카메라별 유효 마스크(침식)


def compute_vis(mesh: MeshData, cams, keep=None) -> Vis:
    n, area, e, cen = face_geom(mesh.V, mesh.F)
    m, nc = len(mesh.F), len(cams)
    vis_px = np.zeros((m, nc), np.int32)
    vis_all = np.zeros((m, nc), np.int32)
    area_px = np.zeros((m, nc), np.float32)
    cs = np.zeros((m, nc), np.float32)
    ds = np.zeros((m, nc), np.float32)
    depths, valids = [], []
    for k, cam in enumerate(cams):
        tid, depth = render_ids(cam, mesh.V, mesh.F, keep=keep)
        valid = cv2.erode(cam.valid.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        sel = tid >= 0
        vis_all[:, k] = np.bincount(tid[sel], minlength=m)
        vis_px[:, k] = np.bincount(tid[sel & valid], minlength=m)
        u, v, z = project(cam, mesh.V[mesh.F])
        ok = z.min(1) > 0.05
        a2 = 0.5 * np.abs((u[:, 1] - u[:, 0]) * (v[:, 2] - v[:, 0]) - (u[:, 2] - u[:, 0]) * (v[:, 1] - v[:, 0]))
        area_px[:, k] = np.where(ok, a2, 0)
        c = cam_center(cam)
        d = c[None] - cen
        dn = np.linalg.norm(d, axis=1)
        cs[:, k] = np.abs((n * d).sum(1)) / np.maximum(dn, 1e-9)
        ds[:, k] = dn
        depths.append(depth)
        valids.append(valid)
        print(f"  vis {cam.camera_id}: 보이는 면 {int((vis_all[:, k] > 0).sum())}/{m}", flush=True)
    return Vis(vis_px, vis_all, area_px, cs, ds, depths, valids)


def view_score(vis: Vis, cos_min=0.15, frac_min=0.25, px_min=6):
    """면×카메라 점수(0 = 쓸 수 없음). 보이는 유효 화소 수 = 해상도·각도·가림을 한꺼번에 반영."""
    frac = vis.vis_all / np.maximum(vis.area_px, 1.0)
    ok = (vis.vis_px >= px_min) & (vis.cos >= cos_min) & (frac >= frac_min)
    return np.where(ok, vis.vis_px, 0).astype(np.float32)


def stretched_faces(mesh: MeshData, vis: Vis, cams_idx, cos_slant=0.25, long_edge=0.25, px_min=6):
    """늘어난 면: (a) 긴 변 > long_edge, (b) 쓰인 카메라 중 보이는 것이 있는데 그 모든 시선에서 |cos| < cos_slant.
    안 보인 면(가림·시야 밖)은 늘어난 게 아니라 '모름'이라 지우지 않는다."""
    n, area, e, cen = face_geom(mesh.V, mesh.F)
    long_ = e.max(1) > long_edge
    seen = vis.vis_all[:, cams_idx] >= px_min
    best = np.where(seen, vis.cos[:, cams_idx], 0).max(1)
    any_seen = seen.any(1)
    slant = any_seen & (best < cos_slant)
    return long_, slant


def sample_image(img, u, v):
    """cv2.remap 은 한 변이 32767 미만이어야 해서 (N,) 좌표를 1024 폭 2D 로 펴서 읽는다. img uint8 (H,W,3) -> (N,3) uint8."""
    N = len(u)
    if N == 0:
        return np.zeros((0, 3), np.uint8)
    W = 1024
    Hh = -(-N // W)
    mx = np.zeros(Hh * W, np.float32); my = np.zeros(Hh * W, np.float32)
    mx[:N], my[:N] = u, v
    out = cv2.remap(img, mx.reshape(Hh, W), my.reshape(Hh, W), cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return out.reshape(-1, 3)[:N]
