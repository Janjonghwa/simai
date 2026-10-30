"""관측 픽셀의 canonical UV 누적. Tier 0 위치·ID·판정을 만들거나 고치지 않는다."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import logging
import struct
import time
import zlib

import numpy as np

from simai_ai.hmr.raster import RasterInterrupted, uv_map, z_buffer

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AppearanceConfig:
    resolution: int = 1024
    device: str = "cpu"
    face_policy: str = "OBSERVED"
    freeze_frames: int = 30
    sample_interval_ms: int = 2000
    publish_interval_ms: int = 30000
    coverage_gain: float = .02
    min_cosine: float = .25
    boundary_px: float = 4.
    outlier_rgb: float = 80.
    seam_width: int = 2
    sync_tolerance_ms: int = 40

    def __post_init__(self):
        if (self.resolution < 4 or self.freeze_frames < 1 or self.sample_interval_ms < 1
                or self.publish_interval_ms < 1 or not 0 < self.coverage_gain <= 1
                or not 0 < self.min_cosine <= 1 or self.boundary_px <= 0
                or not 0 < self.outlier_rgb <= 255 or not 0 <= self.seam_width <= 2
                or self.sync_tolerance_ms < 0):
            raise ValueError("외형 누적 설정 범위 오류")
        if self.face_policy not in {"OBSERVED", "BLURRED", "EXCLUDED"}:
            raise ValueError("지원하지 않는 face_policy")


@dataclass(frozen=True)
class AppearanceView:
    camera: object
    image_bgr: np.ndarray
    capture_time_ms: int
    bbox: tuple | list | None = None  # 원본 왜곡 영상의 xyxy
    mask: np.ndarray | None = None  # 원본 왜곡 영상의 사람 마스크


@dataclass(frozen=True)
class PngAsset:
    data: bytes

    @property
    def size_bytes(self):
        return len(self.data)

    @property
    def sha256(self):
        return "sha256:" + hashlib.sha256(self.data).hexdigest()


def png_asset(array):
    """8비트 RGB 또는 단일 채널, 무손실 PNG. 영상 라이브러리/새 의존성 없음."""
    a = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = a.shape[:2]
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    header = struct.pack(">IIBBBBB", width, height, 8, 2 if a.ndim == 3 else 0, 0, 0, 0)
    rows = b"".join(b"\0" + row.tobytes() for row in a)
    return PngAsset(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


@dataclass(frozen=True)
class AppearanceResult:
    albedo: PngAsset
    surface: PngAsset
    ratios: dict
    face_ratios: dict
    resolution: int
    face_policy: str


@lru_cache(maxsize=4)
def _undistort_maps(camera):
    """보정별 최대 4대 remap 캐시. 사람마다 같은 보정을 다시 계산하지 않는다."""
    from simai_ai.geometry.distortion import distort

    width, height = camera.image_size
    yy, xx = np.indices((height, width))
    ideal = np.column_stack([xx.ravel(), yy.ravel()])
    norm = np.column_stack([ideal, np.ones(len(ideal))]) @ np.linalg.inv(camera.K).T
    distorted = distort(norm[:, :2] / norm[:, 2:3], camera.distortion_model, camera.distortion_coeffs)
    original = camera.ideal_pixels(distorted)
    back, ok = camera.normalize_with_mask(original)
    ok &= np.linalg.norm(back - norm[:, :2] / norm[:, 2:3], axis=1) < 1e-5
    maps = original.astype(np.float32).reshape(height, width, 2)
    maps[~ok.reshape(height, width)] = -1
    return maps


def _ideal_view(view):
    """색·마스크를 같은 K의 왜곡 없는 영상으로 이동. 영상 밖은 무효."""
    import cv2

    camera, image = view.camera, view.image_bgr
    width, height = camera.image_size
    if image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise ValueError("외형 영상은 보정 해상도의 uint8 BGR이어야 한다")
    if view.mask is not None:
        if view.mask.shape != (height, width):
            raise ValueError("사람 마스크와 영상 해상도가 다르다")
        mask = (view.mask > 0).astype(np.uint8)
    elif view.bbox is not None:
        box = np.asarray(view.bbox, dtype=float)
        if box.shape != (4,) or not np.isfinite(box).all() or (box[2:] <= box[:2]).any():
            raise ValueError("외형 bbox 오류")
        x0, y0 = np.clip(np.ceil(box[:2]), 0, [width, height]).astype(int)
        x1, y1 = np.clip(np.floor(box[2:]), 0, [width, height]).astype(int)
        mask = np.zeros((height, width), np.uint8)
        mask[y0:y1, x0:x1] = 1
    else:
        raise ValueError("사람 마스크 또는 bbox가 필요하다")
    if camera.distortion_model != "NONE":
        # 저장소의 가지 검사를 쓰므로 접힌 렌즈 모델을 OpenCV가 조용히 채우지 못한다.
        maps = _undistort_maps(camera)
        image = cv2.remap(image, maps[..., 0], maps[..., 1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)
        mask = cv2.remap(mask, maps[..., 0], maps[..., 1], cv2.INTER_NEAREST, borderMode=cv2.BORDER_CONSTANT)
    # 영상 모서리도 마스크 경계로 센다.
    distance = cv2.distanceTransform(np.pad(mask, 1), cv2.DIST_L2, 3)[1:-1, 1:-1]
    return image, distance


class AppearanceAccumulator:
    """한 World ID의 UV 상태. revision 변경은 새 누적, 마지막 확정 결과는 유지."""

    def __init__(self, faces, texcoords, texcoord_faces, face_faces, *, config=None):
        self.config = config or AppearanceConfig()
        self.faces = np.asarray(faces, dtype=np.int64)
        self.texcoords = np.asarray(texcoords, dtype=float)
        self.texcoord_faces = np.asarray(texcoord_faces, dtype=np.int64)
        if self.faces.shape != self.texcoord_faces.shape or self.faces.ndim != 2 or self.faces.shape[1] != 3:
            raise ValueError("MHR 면과 UV 면이 대응하지 않는다")
        if not np.isfinite(self.texcoords).all() or ((self.texcoords < 0) | (self.texcoords > 1)).any():
            raise ValueError("MHR UV 범위 오류")
        face_faces = np.asarray(face_faces, dtype=bool)
        if face_faces.shape != (len(self.faces),):
            raise ValueError("얼굴 면 마스크 길이 오류")
        self.owner, self.bary = uv_map(self.texcoords, self.texcoord_faces, self.config.resolution,
                                        device=self.config.device)
        self.valid = self.owner >= 0
        if not self.valid.any():
            raise ValueError("UV에 유효한 표면 텍셀이 없다")
        self.face = self.valid & face_faces[np.maximum(self.owner, 0)]
        self.colors = np.zeros((len(self.owner), 3), np.float32)
        self.weights = np.zeros(len(self.owner), np.float32)
        self.revision = None
        self.stable = 0
        self.last_identity_t = -1
        self.ready = False
        self.last_sample = -1
        self.last_publish = -1
        self.published_coverage = 0.
        self.last_confirmed = None
        self.dirty = False

    def track(self, revision, confidence, t):
        if t <= self.last_identity_t:
            return self.ready
        self.last_identity_t = t
        if revision != self.revision:
            self.revision, self.stable = revision, 0
            self.colors.fill(0)
            self.weights.fill(0)
            self.published_coverage = 0.
            self.last_sample = self.last_publish = -1
            self.dirty = False
        if confidence == "IDENTITY_UNCERTAIN":
            self.stable = 0
        else:
            self.stable += 1
        self.ready = self.stable >= self.config.freeze_frames
        return self.ready

    def accumulate(self, world_id, vertices_world, views, t, *, occluders=(), admit=lambda: True):
        """동시각의 다른 사람 (World 정점, 면)도 z-buffer에 넣는다. gate 중단 시 회차를 버린다."""
        import torch

        cfg = self.config
        if not self.ready or (self.last_sample >= 0 and t - self.last_sample < cfg.sample_interval_ms):
            return False
        if not views or any(abs(v.capture_time_ms - t) > cfg.sync_tolerance_ms for v in views):
            return False
        if max(v.capture_time_ms for v in views) - min(v.capture_time_ms for v in views) > cfg.sync_tolerance_ms:
            return False
        if not admit():
            return False
        if cfg.device.startswith("cuda"):
            torch.cuda.synchronize(cfg.device)
        start, cpu_start = time.perf_counter(), time.process_time()
        gpu_ms = 0.
        if cfg.device.startswith("cuda"):
            prep_begin, prep_end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
            prep_begin.record(torch.cuda.current_stream(cfg.device))
        def tensor(value):
            return torch.as_tensor(value, dtype=torch.float32, device=cfg.device)
        vertices = np.asarray(vertices_world, dtype=float)
        if vertices.ndim != 2 or vertices.shape[1] != 3 or not np.isfinite(vertices).all():
            raise ValueError("World 메시 정점이 유한하지 않다")
        ids_np = np.flatnonzero(self.valid)
        ids = torch.as_tensor(ids_np, device=cfg.device)
        fi = torch.as_tensor(self.owner[ids_np], device=cfg.device)
        tri = tensor(vertices[self.faces])
        points = (tensor(self.bary[ids_np])[..., None] * tri[fi]).sum(dim=1)
        normal = torch.nn.functional.normalize(torch.linalg.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), dim=1)
        face_mask = torch.as_tensor(self.face[ids_np], device=cfg.device)
        uv_tri = self.texcoords[self.texcoord_faces] * cfg.resolution
        a, b = uv_tri[:, 1] - uv_tri[:, 0], uv_tri[:, 2] - uv_tri[:, 0]
        uv_area = tensor(np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]))
        all_vertices, all_faces = [vertices], [self.faces]
        count = len(vertices)
        for verts, faces in occluders:
            all_vertices.append(np.asarray(verts))
            all_faces.append(np.asarray(faces) + count)
            count += len(verts)
        all_vertices, all_faces = np.concatenate(all_vertices), np.concatenate(all_faces)
        if not np.isfinite(all_vertices).all():
            raise ValueError("같은 시각 가림막 정점이 유한하지 않다")
        colors, weights = tensor(self.colors).clone(), tensor(self.weights).clone()
        accepted = rejected = 0
        if cfg.device.startswith("cuda"):
            prep_end.record(torch.cuda.current_stream(cfg.device))
            prep_end.synchronize()
            gpu_ms += prep_begin.elapsed_time(prep_end)
        # 같은 촬영 시각의 카메라 순서를 고정해 재현 가능하게 한다.
        for view in sorted(views, key=lambda v: v.camera.camera_id):
            if not admit():
                return False
            camera = view.camera
            image, distance = _ideal_view(view)
            if not admit():
                return False
            camera_vertices = camera.to_camera(all_vertices)
            near = camera_vertices[all_faces, 2] <= 1e-4
            if (near.any(axis=1) & ~near.all(axis=1)).any():
                continue  # near plane을 가로지르는 가림막을 버리고 뒤 색을 채우지 않는다.
            if cfg.device.startswith("cuda"):
                begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                begin.record(torch.cuda.current_stream(cfg.device))
            try:
                _, owner = z_buffer(camera_vertices, all_faces, camera.K,
                                    camera.image_size, device=cfg.device, admit=admit)
            except RasterInterrupted:
                return False
            cam_points = points @ tensor(camera.R).T + tensor(camera.t)
            homo = cam_points @ tensor(camera.K).T
            pixels = homo[:, :2] / homo[:, 2:3]
            good = torch.isfinite(pixels).all(dim=1) & (cam_points[:, 2] > 1e-4)
            width, height = camera.image_size
            good &= (pixels[:, 0] >= 0) & (pixels[:, 0] <= width - 1) & (pixels[:, 1] >= 0) & (pixels[:, 1] <= height - 1)
            xy = torch.round(torch.where(good[:, None], pixels, 0.)).long()
            x, y = xy.T
            ray = torch.nn.functional.normalize(tensor(camera.center) - points, dim=1)
            cosine = (normal[fi] * ray).sum(dim=1)
            distance = tensor(distance)
            good &= (owner[y, x] == fi) & (cosine >= cfg.min_cosine) & (distance[y, x] >= 1.)
            if cfg.face_policy == "EXCLUDED":
                good &= ~face_mask
            # 투영 면적 / UV 면적 = 텍셀 하나당 픽셀 면적.
            camera_tri = camera.to_camera(vertices)[self.faces]
            projected = camera_tri @ camera.K.T
            with np.errstate(divide="ignore", invalid="ignore"):
                projected = projected[..., :2] / projected[..., 2:3]
            a, b = projected[:, 1] - projected[:, 0], projected[:, 2] - projected[:, 0]
            screen_area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0])
            resolution_weight = (tensor(screen_area)[fi] / uv_area[fi].clamp_min(1e-8)).clamp(0, 1)
            w = cosine ** 2 * resolution_weight * (distance[y, x] / cfg.boundary_px).clamp_max(1)
            observed = tensor(image[..., ::-1].copy())[y, x]
            outlier = (weights[ids] > 0) & ((colors[ids] - observed).abs().amax(dim=1) > cfg.outlier_rgb)
            rejected += int((good & outlier).sum())
            good &= ~outlier & torch.isfinite(w) & (w > 0)
            selected, w, observed = ids[good], w[good, None], observed[good]
            total = weights[selected, None] + w
            colors[selected] = (weights[selected, None] * colors[selected] + w * observed) / total
            weights[selected] = total[:, 0]
            accepted += len(selected)
            if cfg.device.startswith("cuda"):
                end.record(torch.cuda.current_stream(cfg.device))
                end.synchronize()
                gpu_ms += begin.elapsed_time(end)
        if not admit():
            return False
        self.colors, self.weights = colors.cpu().numpy(), weights.cpu().numpy()
        self.last_sample = t
        self.dirty |= accepted > 0
        if cfg.device.startswith("cuda"):
            torch.cuda.synchronize(cfg.device)
        log.info("외형 누적 world_id=%s device=%s wall_ms=%.3f cpu_ms=%.3f gpu_ms=%s accepted=%s rejected=%s",
                 world_id, cfg.device, (time.perf_counter() - start) * 1000,
                 (time.process_time() - cpu_start) * 1000,
                 f"{gpu_ms:.3f}" if cfg.device.startswith("cuda") else "NA", accepted, rejected)
        return accepted > 0

    def due(self, t):
        coverage = float(np.count_nonzero(self.weights)) / int(self.valid.sum())
        return self.ready and self.dirty and (self.last_publish < 0
            or coverage - self.published_coverage >= self.config.coverage_gain
            or t - self.last_publish >= self.config.publish_interval_ms)

    def confirm(self, result, t):
        self.last_confirmed = result
        self.last_publish = t
        self.published_coverage = float(np.count_nonzero(self.weights)) / int(self.valid.sum())
        self.dirty = False

    def result(self):
        import cv2

        n, cfg = self.config.resolution, self.config
        observed = (self.weights > 0).reshape(n, n)
        valid, face = self.valid.reshape(n, n), self.face.reshape(n, n)
        source = np.where(observed, 2, 0).astype(np.uint8)
        rgb = self.colors.reshape(n, n, 3).copy()
        # 관측으로부터 최대 2텍셀만 확장. 반복 보간 결과는 다음 번 누적에 넣지 않는다.
        filled = observed.copy()
        for _ in range(cfg.seam_width):
            sums, counts = np.zeros_like(rgb), np.zeros((n, n), np.float32)
            for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                shifted = np.roll(filled & ~face, (dy, dx), axis=(0, 1))
                if dy: shifted[0 if dy > 0 else -1, :] = False
                if dx: shifted[:, 0 if dx > 0 else -1] = False
                sums += np.roll(rgb, (dy, dx), axis=(0, 1)) * shifted[..., None]
                counts += shifted
            new = valid & ~face & ~filled & (counts > 0)
            rgb[new] = sums[new] / counts[new, None]
            filled[new], source[new] = True, 1
        if cfg.face_policy == "BLURRED":
            # 미관측·몸 표면의 색을 얼굴에 섞지 않는 정규화 합성곱.
            mask = (observed & face).astype(np.float32)
            numerator = cv2.GaussianBlur(rgb * mask[..., None], (15, 15), 3)
            denominator = cv2.GaussianBlur(mask, (15, 15), 3)
            rgb[observed & face] = (numerator / np.maximum(denominator[..., None], 1e-8))[observed & face]
        rgb[~filled] = 0
        ratios = {name: float(np.count_nonzero((source == value) & valid)) / int(valid.sum())
                  for name, value in (("OBSERVED", 2), ("INTERPOLATED", 1), ("UNOBSERVED", 0))}
        face_observed = float(np.count_nonzero(observed & face)) / int(face.sum()) if face.any() else 0.
        source[face] += 128
        return AppearanceResult(png_asset(np.clip(np.rint(rgb), 0, 255).astype(np.uint8)), png_asset(source),
                                ratios, {"OBSERVED": face_observed, "UNOBSERVED": 1 - face_observed}, n, cfg.face_policy)
