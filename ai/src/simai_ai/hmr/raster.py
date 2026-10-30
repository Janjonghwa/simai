"""torch 삼각형 래스터화. 카메라 픽셀 중심은 OpenCV의 정수 좌표, UV는 텍셀 중앙."""

import numpy as np


class RasterInterrupted(Exception):
    """Tier 0 admission이 닫혀 남은 래스터화를 취소했다."""


def fragments(triangles, width, height, *, device="cpu", chunk_size=262144, pixel_offset=.5):
    """(F,3,2) → (면 번호, 평탄 픽셀 번호, 화면 barycentric) 조각 묶음.

    삼각형 경계 상자의 후보를 펼치되 한 번에 chunk_size개만 만든다.
    한 면이 화면 전체를 덮어도 임시 GPU 텐서는 이 상한을 따른다.
    """
    import torch

    triangles = np.asarray(triangles, dtype=np.float32)
    valid = np.isfinite(triangles).all(axis=(1, 2))
    safe = np.where(valid[:, None, None], triangles, 0)
    lo = np.maximum(np.floor(safe.min(axis=1)), 0).astype(np.int64)
    hi = np.minimum(np.ceil(safe.max(axis=1)), [width - 1, height - 1]).astype(np.int64)
    sizes = np.maximum(hi - lo + 1, 0)
    counts = sizes.prod(axis=1) * valid
    ends = np.cumsum(counts)
    if not len(ends) or not ends[-1]:
        return
    t = torch.as_tensor(safe, device=device)
    starts = torch.as_tensor(ends - counts, device=device)
    ends_t = torch.as_tensor(ends, device=device)
    lo_t = torch.as_tensor(lo, device=device)
    widths = torch.as_tensor(sizes[:, 0], device=device)
    for start in range(0, int(ends[-1]), chunk_size):
        flat = torch.arange(start, min(start + chunk_size, int(ends[-1])), device=device)
        face = torch.searchsorted(ends_t, flat, right=True)
        local = flat - starts[face]
        x = lo_t[face, 0] + local % widths[face]
        y = lo_t[face, 1] + local // widths[face]
        p = torch.stack([x + pixel_offset, y + pixel_offset], dim=1)
        a, b, c = t[face].unbind(dim=1)
        ab, ac, ap = b - a, c - a, p - a
        det = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
        nonzero = det.abs() > 1e-10
        den = torch.where(nonzero, det, torch.ones_like(det))
        v = (ap[:, 0] * ac[:, 1] - ap[:, 1] * ac[:, 0]) / den
        w = (ab[:, 0] * ap[:, 1] - ab[:, 1] * ap[:, 0]) / den
        bary = torch.stack([1 - v - w, v, w], dim=1)
        keep = nonzero & (bary >= -1e-6).all(dim=1)
        yield face[keep], y[keep] * width + x[keep], bary[keep]


def z_buffer(camera_vertices, faces, K, image_size, *, device="cpu", admit=lambda: True):
    """원근 보정 z-buffer와 면 번호. near plane을 가로지르는 면은 보수적으로 제외."""
    import torch

    width, height = image_size
    points = np.asarray(camera_vertices)
    tri_z = points[faces, 2]
    homo = points @ np.asarray(K).T
    with np.errstate(divide="ignore", invalid="ignore"):
        screen = homo[:, :2] / homo[:, 2:3]
    tri = screen[faces].copy()
    tri[(tri_z <= 1e-4).any(axis=1)] = np.nan
    depth = torch.full((width * height,), float("inf"), device=device)
    z = torch.as_tensor(tri_z, dtype=torch.float32, device=device)
    for fi, pi, bary in fragments(tri, width, height, device=device, pixel_offset=0.):
        if not admit():
            raise RasterInterrupted
        d = 1 / (bary / z[fi]).sum(dim=1)
        depth.scatter_reduce_(0, pi, d, reduce="amin")
    owner = torch.full((width * height,), len(faces), dtype=torch.int64, device=device)
    for fi, pi, bary in fragments(tri, width, height, device=device, pixel_offset=0.):
        if not admit():
            raise RasterInterrupted
        d = 1 / (bary / z[fi]).sum(dim=1)
        keep = d <= depth[pi] + 1e-6
        owner.scatter_reduce_(0, pi[keep], fi[keep], reduce="amin")
    return depth.reshape(height, width), owner.reshape(height, width)


def uv_map(texcoords, texcoord_faces, resolution, *, device="cpu"):
    """MHR UV(아래 원점)를 glTF TEXCOORD_0(위 원점)로 바꾼 PNG 텍셀 대응표."""
    uv = np.asarray(texcoords).copy()
    uv[:, 1] = 1 - uv[:, 1]
    n = resolution
    owner = np.full(n * n, -1, dtype=np.int64)
    barycentric = np.zeros((n * n, 3), np.float32)
    for fi, pi, bary in fragments(uv[texcoord_faces] * n, n, n, device=device):
        fi, pi, bary = fi.cpu().numpy(), pi.cpu().numpy(), bary.cpu().numpy()
        # MHR의 비중첩 UV를 전제. 공유 모서리의 같은 텍셀은 마지막 면에 귀속된다.
        owner[pi], barycentric[pi] = fi, bary
    return owner, barycentric
