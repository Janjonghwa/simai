"""관측 픽셀의 canonical UV 누적. Tier 0 위치·ID·판정을 만들거나 고치지 않는다."""

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import logging
import struct
import time
import zlib

import numpy as np

from simai_ai.hmr.appearance_quality import CameraColors, blur_weight, fit_weight, silhouette_weight
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
    silhouette_enabled: bool = True
    silhouette_erode_px: float = 3.
    silhouette_fade_px: float = 3.
    color_enabled: bool = True
    color_ema: float = .2
    color_gain_min: float = .7
    color_gain_max: float = 1.4
    color_min_common: int = 64
    color_min_quality: float = .25
    color_min_rgb: float = 10.
    color_max_rgb: float = 245.
    blur_enabled: bool = True
    blur_history: int = 15
    blur_min_pixels: int = 64
    blur_floor: float = .1
    fit_enabled: bool = True
    fit_full_px: float = 8.
    fit_reject_px: float = 40.
    fit_min_joints: int = 6
    fit_min_confidence: float = .3
    fit_sparse_weight: float = .5
    # 텍셀 합치기(S15P31A101-789). best: 품질 높은 관측 우선, mean: 기존 전체 가중 평균(되돌림용, 결과 비트 동일)
    fusion: str = "best"
    fusion_power: float = 4.
    fusion_keep: float = 4.
    fusion_reject_decay: float = .5

    def __post_init__(self):
        if (self.resolution < 4 or self.freeze_frames < 1 or self.sample_interval_ms < 1
                or self.publish_interval_ms < 1 or not 0 < self.coverage_gain <= 1
                or not 0 < self.min_cosine <= 1 or self.boundary_px <= 0
                or not 0 < self.outlier_rgb <= 255 or not 0 <= self.seam_width <= 2
                or self.sync_tolerance_ms < 0):
            raise ValueError("외형 누적 설정 범위 오류")
        if self.face_policy not in {"OBSERVED", "BLURRED", "EXCLUDED"}:
            raise ValueError("지원하지 않는 face_policy")
        if (self.fusion not in {"best", "mean"} or not self.fusion_power >= 1 or not self.fusion_keep >= 1
                or not 0 <= self.fusion_reject_decay < 1):
            raise ValueError("외형 텍셀 합치기 설정 범위 오류")
        numeric = [v for v in vars(self).values() if isinstance(v, (float, int))]
        if (not np.isfinite(numeric).all() or self.silhouette_erode_px < 0
                or self.silhouette_fade_px <= 0 or not 0 < self.color_ema <= 1
                or not 0 < self.color_gain_min <= 1 <= self.color_gain_max
                or self.color_min_common < 1 or not 0 < self.color_min_quality <= 1
                or not 0 < self.color_min_rgb < self.color_max_rgb < 255
                or self.blur_history < 1 or self.blur_min_pixels < 1
                or not 0 < self.blur_floor <= 1
                or not 0 <= self.fit_full_px < self.fit_reject_px
                or not 1 <= self.fit_min_joints <= 17 or not 0 <= self.fit_min_confidence <= 1
                or not 0 < self.fit_sparse_weight <= 1):
            raise ValueError("외형 품질 설정 범위 오류")


@dataclass(frozen=True)
class AppearanceView:
    camera: object
    image_bgr: np.ndarray
    capture_time_ms: int
    bbox: tuple | list | None = None  # 원본 왜곡 영상의 xyxy
    mask: np.ndarray | None = None  # 원본 왜곡 영상의 사람 마스크
    keypoints_2d: list | tuple | None = None  # Tier 0 COCO17, 원본 픽셀·신뢰도


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
class TexelSnapshot:
    """한 World ID 의 누적 텍셀(외형 이어받기·병합용, S15P31A101-743). 배열은 공유하므로 받는 쪽은 복사해 쓴다.

    peaks: best 합치기의 텍셀별 최고 관측 세기(S15P31A101-789). mean 이면 쓰지 않는다. 거절 압력은 넘기지 않는다(일시 상태).
    """
    colors: np.ndarray
    weights: np.ndarray
    inherited: np.ndarray
    last_confirmed: object = None
    peaks: np.ndarray | None = None


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


def fuse_best(colors, weights, peaks, rejects, inherited, selected, w, observed, cfg):
    """품질 높은 관측 우선 합치기(S15P31A101-789). torch 텐서를 제자리에서 고친다(CPU·CUDA 같은 식).

    한 카메라의 관측(텍셀 `selected`, 가중 w∈(0,1], 이득 보정 RGB)을 받는다. 세기 s = w^p 로 날카롭게 한 뒤
    - 새 텍셀·이어받은 텍셀: 관측으로 시작(C=c, W=s, P=s, R=0).
    - 기존 색과 채널 차 ≤ outlier_rgb(인라이어): 최고 세기 P=max(P,s), 쌓인 질량을 k·P 로 묶은 Wc=min(W,k·P) 로
      C=(Wc·C+s·c)/(Wc+s), W=Wc+s, 거절 압력 R←R·ρ. 질량 상한 때문에 최고 품질 관측 약 k 개 분량만 남고,
      흐린 관측 수백 개(작은 s)는 좋은 관측 몇 개를 이기지 못한다. 같은 품질이면 지수 평균처럼 새 관측이 옛 것을 밀어낸다.
    - 이상치: 버리되 R←R+s 로 거절 압력을 쌓고, R > min(W,k·P) 이면 지금 관측으로 갈아 끼운다(C=c, W=s, P=s, R=0).
      처음 들어온 틀린 관측(-755 의 0 고착 같은)이 뒤의 일관된 바른 관측을 영영 막지 못한다.
    반환: (받아들인 수, 거절 수, 갈아 끼운 수).
    """
    import torch

    s = w.pow(cfg.fusion_power)
    keep = s > 0   # 아주 작은 가중의 거듭제곱이 0 으로 내려가면 관측으로 적지 않는다
    selected, s, observed = selected[keep], s[keep], observed[keep]
    C, W, P, R = colors[selected], weights[selected], peaks[selected], rejects[selected]
    fresh = inherited[selected] | (W <= 0)
    inlier = ~fresh & ((C - observed).abs().amax(dim=1) <= cfg.outlier_rgb)
    peak = torch.maximum(P, s)
    held = torch.minimum(W, cfg.fusion_keep * peak)
    blended = (held[:, None] * C + s[:, None] * observed) / (held + s)[:, None]
    pressure = R + s
    takeover = ~fresh & ~inlier & (pressure > torch.minimum(W, cfg.fusion_keep * P))
    replace = fresh | takeover
    zero = torch.zeros_like(R)
    colors[selected] = torch.where(replace[:, None], observed, torch.where(inlier[:, None], blended, C))
    weights[selected] = torch.where(replace, s, torch.where(inlier, held + s, W))
    peaks[selected] = torch.where(replace, s, torch.where(inlier, peak, P))
    rejects[selected] = torch.where(replace, zero, torch.where(inlier, R * cfg.fusion_reject_decay, pressure))
    inherited[selected] = False
    accepted = int((replace | inlier).sum())
    return accepted, len(selected) - accepted, int(takeover.sum())


class AppearanceAccumulator:
    """한 World ID의 UV 상태. revision 변경은 새 누적, 마지막 확정 결과는 유지.

    `inherited`: 다른 World ID 에서 이어받은 텍셀(S15P31A101-743). 표시용 시작값이라 이 ID 의 첫 관측이 오면 평균하지 않고
    덮어쓰며(이상치 거절도 하지 않음) 표시를 지운다. 표면 출처 지도에는 관측(2)으로 적는다(다른 ID 에서 관측한 픽셀).
    """

    def __init__(self, faces, texcoords, texcoord_faces, face_faces, *, config=None, camera_colors=None):
        self.config = config or AppearanceConfig()
        self.camera_colors = camera_colors if camera_colors is not None else CameraColors()
        self.sharpness = {}
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
        # best 합치기 상태(S15P31A101-789): 텍셀별 최고 관측 세기, 이상치 거절 압력. mean 이면 0 그대로
        self.peaks = np.zeros(len(self.owner), np.float32)
        self.rejects = np.zeros(len(self.owner), np.float32)
        self.inherited = np.zeros(len(self.owner), bool)
        self.carry_publish = False
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
            # 제자리에서 비우지 않는다: 소실 ID 사본(TexelSnapshot)이 같은 배열을 볼 수 있다
            self.colors = np.zeros_like(self.colors)
            self.weights = np.zeros_like(self.weights)
            self.peaks = np.zeros_like(self.peaks)
            self.rejects = np.zeros_like(self.rejects)
            self.inherited = np.zeros_like(self.inherited)
            self.carry_publish = False
            self.published_coverage = 0.
            self.last_sample = self.last_publish = -1
            self.dirty = False
            self.sharpness.clear()
        if confidence == "IDENTITY_UNCERTAIN":
            self.stable = 0
        else:
            self.stable += 1
        self.ready = self.stable >= self.config.freeze_frames
        return self.ready

    def accumulate(self, world_id, vertices_world, views, t, *, joints_world=None, occluders=(), admit=lambda: True):
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
        inherited = torch.as_tensor(self.inherited, device=cfg.device).clone()
        best = cfg.fusion == "best"
        if best:
            peaks, rejects = tensor(self.peaks).clone(), tensor(self.rejects).clone()
        camera_colors = self.camera_colors.copy()
        sharpness = {k: list(v) for k, v in self.sharpness.items()}
        samples, color_samples = [], {}
        accepted = rejected = takeovers = 0
        if cfg.device.startswith("cuda"):
            prep_end.record(torch.cuda.current_stream(cfg.device))
            prep_end.synchronize()
            gpu_ms += prep_begin.elapsed_time(prep_end)
        # 같은 촬영 시각의 카메라 순서를 고정해 재현 가능하게 한다.
        for view in sorted(views, key=lambda v: v.camera.camera_id):
            if not admit():
                return False
            camera = view.camera
            fit, error, joint_count = fit_weight(view, joints_world, cfg)
            if fit <= 0:
                log.info("외형 품질 world_id=%s camera=%s fit=%.3f error_px=%s joints=%s blur=NA",
                         world_id, camera.camera_id, fit, error, joint_count)
                continue
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
            # 타인 면 번호와 빈 픽셀은 0. 삼각형 사이 내부 모서리는 실루엣이 아니다.
            edge, blur, variance = None, 1., None
            if cfg.silhouette_enabled or cfg.blur_enabled:
                silhouette = ((owner >= 0) & (owner < len(self.faces))).cpu().numpy().astype(np.uint8)
                edge = silhouette_weight(silhouette, cfg)
                blur, variance = blur_weight(image, silhouette & (distance >= 1),
                                            sharpness.setdefault(camera, []), cfg)
            log.info("외형 품질 world_id=%s camera=%s fit=%.3f error_px=%s joints=%s blur=%.3f sharpness=%s",
                     world_id, camera.camera_id, fit, error, joint_count, blur, variance)
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
            quality = cosine ** 2 * (distance[y, x] / cfg.boundary_px).clamp_max(1)
            if edge is not None:
                quality *= tensor(edge)[y, x]
            quality *= blur * fit
            w = quality * resolution_weight
            observed = tensor(image[..., ::-1].copy())[y, x]
            # 공유 통로 크롭 밖·왜곡 제거 영상 밖은 RGB 0 으로 온다. 세 채널이 모두 0 인 픽셀은 관측으로 적지 않는다(-755):
            # 한번 0 이 들어가면 뒤의 바른 관측이 이상치(>80)로 거절되어 검정 점이 굳는다.
            good &= torch.isfinite(w) & (w > 0) & (observed.amax(dim=1) > 0)
            if cfg.color_enabled:
                high = good & (quality >= cfg.color_min_quality)
                color_samples[camera] = (ids_np[high.cpu().numpy()], observed[high].cpu().numpy())
            samples.append((camera, good, w, observed))
            if cfg.device.startswith("cuda"):
                end.record(torch.cuda.current_stream(cfg.device))
                end.synchronize()
                gpu_ms += begin.elapsed_time(end)
        if not admit():
            return False
        if cfg.color_enabled:
            camera_colors.estimate(color_samples, cfg)
        for camera, good, w, observed in samples:
            if not admit():
                return False
            if cfg.device.startswith("cuda"):
                begin.record(torch.cuda.current_stream(cfg.device))
            if cfg.color_enabled:
                observed = (observed * tensor(camera_colors.gain(camera))).clamp(0, 255)
            if best:
                good &= torch.isfinite(w) & (w > 0)
                ok, bad, swapped = fuse_best(colors, weights, peaks, rejects, inherited,
                                             ids[good], w[good], observed[good], cfg)
                accepted, rejected, takeovers = accepted + ok, rejected + bad, takeovers + swapped
                if cfg.device.startswith("cuda"):
                    end.record(torch.cuda.current_stream(cfg.device))
                    end.synchronize()
                    gpu_ms += begin.elapsed_time(end)
                continue
            fresh = inherited[ids]   # 이어받은 텍셀: 이 ID 의 첫 관측이 덮는다(평균·이상치 거절 없음)
            outlier = (weights[ids] > 0) & ~fresh & ((colors[ids] - observed).abs().amax(dim=1) > cfg.outlier_rgb)
            rejected += int((good & outlier).sum())
            good &= ~outlier & torch.isfinite(w) & (w > 0)
            selected, w, observed, fresh = ids[good], w[good, None], observed[good], fresh[good, None]
            total = torch.where(fresh, w, weights[selected, None] + w)
            colors[selected] = torch.where(fresh, observed,
                                           (weights[selected, None] * colors[selected] + w * observed) / total)
            weights[selected] = total[:, 0]
            inherited[selected] = False
            accepted += len(selected)
            if cfg.device.startswith("cuda"):
                end.record(torch.cuda.current_stream(cfg.device))
                end.synchronize()
                gpu_ms += begin.elapsed_time(end)
        if not admit():
            return False
        self.colors, self.weights = colors.cpu().numpy(), weights.cpu().numpy()
        self.inherited = inherited.cpu().numpy()
        if best:
            self.peaks, self.rejects = peaks.cpu().numpy(), rejects.cpu().numpy()
        self.camera_colors.commit(camera_colors)
        self.sharpness = sharpness
        if cfg.color_enabled:
            for camera, *_ in samples:
                log.info("외형 색 이득 camera=%s rgb=%s", camera.camera_id,
                         np.round(camera_colors.gain(camera), 4).tolist())
        self.last_sample = t
        self.dirty |= accepted > 0
        if cfg.device.startswith("cuda"):
            torch.cuda.synchronize(cfg.device)
        log.info("외형 누적 world_id=%s device=%s wall_ms=%.3f cpu_ms=%.3f gpu_ms=%s accepted=%s rejected=%s "
                 "fusion=%s takeover=%s", world_id, cfg.device, (time.perf_counter() - start) * 1000,
                 (time.process_time() - cpu_start) * 1000,
                 f"{gpu_ms:.3f}" if cfg.device.startswith("cuda") else "NA", accepted, rejected, cfg.fusion, takeovers)
        return accepted > 0

    def snapshot(self):
        return TexelSnapshot(self.colors, self.weights, self.inherited, self.last_confirmed, self.peaks)

    def absorb(self, other, *, inherit):
        """다른 누적(TexelSnapshot)을 합친다(S15P31A101-743). 표시용이며 신원 판단에 쓰지 않는다.

        inherit=False(병합·같은 ID 재연결, Tier 0 가 같은 사람이라고 한 경우): 두 쪽 본 관측 텍셀은 품질 가중 평균, 한쪽만
        관측한 텍셀은 그 값(본 관측이 이어받은 값보다 먼저). inherit=True(다른 새 World ID 로 이어받기): 이 ID 의 본 관측이
        없는 텍셀만 채우고 이어받음 표시. 바뀌면 다음 발행 때 안정 프레임을 기다리지 않고 한 번 낸다(carry_publish).

        best 합치기(S15P31A101-789)의 두 쪽 본 관측 텍셀: 각자 질량을 k·최고 세기로 묶은 뒤(누적 때와 같은 상한) 색이
        outlier_rgb 안이면 그 질량으로 가중 평균하고 최고 세기는 큰 쪽, 색이 어긋나면 질량이 큰 쪽(같으면 이쪽)을 통째로 쓴다.
        평균하면 어긋난 두 색이 섞여 번지고, 최고 세기로 고르면 한 번의 선명한 오관측이 이긴다. 가져온 텍셀의 거절 압력은 0.
        """
        colors = np.asarray(other.colors, np.float32)
        weights = np.asarray(other.weights, np.float32)
        theirs_inherited = np.asarray(other.inherited, bool)
        if colors.shape != self.colors.shape or weights.shape != self.weights.shape:
            return False
        colors, weights = colors.copy(), weights.copy()
        self.colors, self.weights, self.inherited = self.colors.copy(), self.weights.copy(), self.inherited.copy()
        mine = (self.weights > 0) & ~self.inherited
        theirs = weights > 0
        if self.config.fusion == "best":
            return self._absorb_best(other, colors, weights, theirs_inherited, mine, theirs, inherit=inherit)
        if inherit:
            both = np.zeros_like(mine)
            take = theirs & ~mine
            mark = take
        else:
            theirs_own = theirs & ~theirs_inherited
            both = mine & theirs_own
            # 본 관측은 늘, 이어받은 값은 이쪽이 빈 텍셀일 때만 가져온다
            take = theirs & ~mine & (theirs_own | (self.weights <= 0))
            mark = take & theirs_inherited
            total = self.weights[both] + weights[both]
            self.colors[both] = ((self.weights[both, None] * self.colors[both]
                                  + weights[both, None] * colors[both]) / total[:, None])
            self.weights[both] = total
        self.colors[take], self.weights[take] = colors[take], weights[take]
        self.inherited[take] = mark[take]
        changed = bool(both.any() or take.any())
        if changed:
            self.dirty = self.carry_publish = True
        if self.last_confirmed is None:
            self.last_confirmed = other.last_confirmed
        return changed

    def _absorb_best(self, other, colors, weights, theirs_inherited, mine, theirs, *, inherit):
        peaks = None if other.peaks is None else np.asarray(other.peaks, np.float32)
        if peaks is None or peaks.shape != weights.shape:
            peaks = weights   # 최고 세기를 모르면 질량 전체를 한 관측으로 본다(상한이 질량을 깎지 않는다)
        peaks = peaks.copy()
        self.peaks, self.rejects = self.peaks.copy(), self.rejects.copy()
        if inherit:
            both = np.zeros_like(mine)
            take = theirs & ~mine
            mark = take
        else:
            theirs_own = theirs & ~theirs_inherited
            both = mine & theirs_own
            take = theirs & ~mine & (theirs_own | (self.weights <= 0))
            mark = take & theirs_inherited
            keep = self.config.fusion_keep
            # 최고 세기 0(알 수 없음)이면 질량을 깎지 않는다
            held_mine = np.minimum(self.weights[both], keep * np.where(self.peaks[both] > 0, self.peaks[both], np.inf))
            held_theirs = np.minimum(weights[both], keep * np.where(peaks[both] > 0, peaks[both], np.inf))
            agree = np.abs(self.colors[both] - colors[both]).max(axis=1) <= self.config.outlier_rgb
            total = held_mine + held_theirs
            mixed = (held_mine[:, None] * self.colors[both] + held_theirs[:, None] * colors[both]) / total[:, None]
            theirs_win = ~agree & (held_theirs > held_mine)
            self.colors[both] = np.where(agree[:, None], mixed,
                                         np.where(theirs_win[:, None], colors[both], self.colors[both]))
            self.weights[both] = np.where(agree, total, np.where(theirs_win, held_theirs, held_mine))
            self.peaks[both] = np.where(agree, np.maximum(self.peaks[both], peaks[both]),
                                        np.where(theirs_win, peaks[both], self.peaks[both]))
            self.rejects[both] = np.where(theirs_win, 0, self.rejects[both])
        self.colors[take], self.weights[take], self.peaks[take] = colors[take], weights[take], peaks[take]
        self.rejects[take] = 0
        self.inherited[take] = mark[take]
        changed = bool(both.any() or take.any())
        if changed:
            self.dirty = self.carry_publish = True
        if self.last_confirmed is None:
            self.last_confirmed = other.last_confirmed
        return changed

    def adopt(self, old):
        """같은 사람·같은 UV 의 이전 누적을 잇는다(체형만 다시 구워 등록이 바뀐 경우). 이어 쓰면 True."""
        if (old is None or old.revision != self.revision or old.colors.shape != self.colors.shape
                or not np.array_equal(old.valid, self.valid)):
            return False
        self.colors, self.weights, self.inherited = old.colors, old.weights, old.inherited
        self.peaks, self.rejects = old.peaks, old.rejects
        self.dirty, self.carry_publish = old.dirty, old.carry_publish
        self.last_sample, self.last_publish = old.last_sample, old.last_publish
        self.published_coverage, self.sharpness = old.published_coverage, old.sharpness
        return True

    def inherited_ratio(self):
        return float(np.count_nonzero(self.inherited & (self.weights > 0))) / int(self.valid.sum())

    def due(self, t):
        if self.carry_publish and self.dirty:
            return True
        coverage = float(np.count_nonzero(self.weights)) / int(self.valid.sum())
        return self.ready and self.dirty and (self.last_publish < 0
            or coverage - self.published_coverage >= self.config.coverage_gain
            or t - self.last_publish >= self.config.publish_interval_ms)

    def confirm(self, result, t):
        self.last_confirmed = result
        self.last_publish = t
        self.published_coverage = float(np.count_nonzero(self.weights)) / int(self.valid.sum())
        self.dirty = self.carry_publish = False

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
