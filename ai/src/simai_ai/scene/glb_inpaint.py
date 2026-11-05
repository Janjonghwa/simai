"""GLB 평면 채움 3단계: 사이드카 마스크 영역 이미지 인페인팅 및 GLB 되돌려 쓰기 (S15P31A101-514).

S15P31A101-511(`glb_fill.py`)이 GLB 옆 `<이름>_fill/` 에 남긴 평면별 사이드카
(`<role>_albedo.png`, `<role>_inpaint_mask.png`, `<role>_surface_source.png`, `fill_atlas.json`)에서
마스크 영역(`inpaint_mask > 0`, 사진 근거가 없는 채울 곳)만 이미지 인페인팅으로 채워 GLB 의 해당 평면
텍스처(`estimated_<role>_albedo`)와 출처 지도(`estimated_<role>_surface_source`)에 되돌려 쓴다.

규칙(AGENTS.md 규칙 2·7, docs/design/scene-glb-enhance.md):
- 관측 텍셀(`inpaint_mask == 0`)은 바이트 단위로 절대 바뀌지 않는다.
- 인페인팅으로 채운 텍셀의 출처는 `GENERATED`(4, `texture_provenance.GENERATED`)로 기록해
  카메라 사진 투영(`ESTIMATED_PLANE` = 3) 및 미관측 회색(`UNOBSERVED` = 0)과 구분한다.
- 시각 전용(`purpose: VISUAL_ONLY`, `safety_use: False`)이며 Tier 0 위치·위험 판정·`observed_surface_ratio` 에 쓰지 않는다.
- 백엔드 3단 폴백:
  (a) `diffusion`: GPU 확산 모델 인페인팅(diffusers — FLUX.1 Fill dev 또는 SDXL Inpaint, 함수 안 지연 import)
  (b) `classic`: 전통 인페인팅(cv2 Telea + 같은 평면의 관측 텍셀 패치 기반 텍스처 합성)
  (c) `gray`: 앞 단계가 실패하면 회색 자리표시(128, `UNOBSERVED`=0) 유지
- 큰 평면(최대 4096 px)은 겹침 타일(`tile_size`, `tile_overlap`)로 나눠 경계를 코사인 가중치로 이어 붙인다.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from .glb_enhance import GlbFile, _add_view, parse_glb, serialize_glb
from .glb_fill import GRAY, SRC_GRAY, SRC_PROJECTED, encode_png
from .texture_provenance import FILL_SURFACE_VALUES, GENERATED, gaussian_blur

SRC_GENERATED = GENERATED  # 4: 인페인팅으로 생성한 텍셀(관측 근거 없음, 시각 전용)
INPAINT_MODES = ("off", "classic", "diffusion")

DEFAULT_DIFFUSION_MODEL_ID = "diffusers/stable-diffusion-xl-1.0-inpainting-0.1"
DEFAULT_PROMPT_FLOOR = "same floor material, seamless, uniform lighting, top-down surface texture"
DEFAULT_PROMPT_WALL = "same wall material, seamless, uniform lighting, flat wall surface texture"
DEFAULT_NEGATIVE_PROMPT = "objects, furniture, people, text, shadows, seams, borders, artifacts"

_DIFFUSION_PIPE_CACHE: dict[tuple[str, str], Any] = {}


@dataclass(frozen=True)
class InpaintConfig:
    """평면 인페인팅 설정. 환경변수(`SIMAI_GLB_INPAINT` 등) 또는 작업 옵션(`options`)으로 제어한다."""

    mode: str = "classic"  # off | classic | diffusion (기본 classic, GPU 세션에서 diffusion)
    model_id: str = DEFAULT_DIFFUSION_MODEL_ID
    model_path: str | None = None
    prompt_floor: str = DEFAULT_PROMPT_FLOOR
    prompt_wall: str = DEFAULT_PROMPT_WALL
    negative_prompt: str = DEFAULT_NEGATIVE_PROMPT
    seed: int = 42
    num_inference_steps: int = 20
    guidance_scale: float = 7.5
    strength: float = 0.99
    diffusion_infer_size: int = 1024
    device: str = "cuda"
    tile_size: int = 1024
    tile_overlap: int = 128
    max_plane_side: int = 4096
    telea_radius: int = 9
    patch_size: int = 16
    patch_blend: float = 0.65

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> InpaintConfig:
        env = os.environ if env is None else env
        cfg = cls()
        kw: dict[str, Any] = {}
        raw_mode = env.get("SIMAI_GLB_INPAINT", "").strip().lower()
        if raw_mode != "":
            if raw_mode in ("0", "false", "off", "no", "none"):
                kw["mode"] = "off"
            elif raw_mode in ("1", "true", "yes", "classic"):
                kw["mode"] = "classic"
            elif raw_mode == "diffusion":
                kw["mode"] = "diffusion"
            else:
                raise ValueError(f"지원하지 않는 SIMAI_GLB_INPAINT={raw_mode!r} (off|classic|diffusion)")
        for env_key in ("SIMAI_INPAINT_MODEL_ID", "SIMAI_INPAINT_MODEL"):
            if (val := env.get(env_key, "").strip()) != "":
                kw["model_id"] = val
                break
        if (val := env.get("SIMAI_INPAINT_MODEL_PATH", "").strip()) != "":
            kw["model_path"] = val
        for env_key, field_name, cast in (
            ("SIMAI_INPAINT_PROMPT_FLOOR", "prompt_floor", str),
            ("SIMAI_INPAINT_PROMPT_WALL", "prompt_wall", str),
            ("SIMAI_INPAINT_NEGATIVE_PROMPT", "negative_prompt", str),
            ("SIMAI_INPAINT_SEED", "seed", int),
            ("SIMAI_INPAINT_STEPS", "num_inference_steps", int),
            ("SIMAI_INPAINT_GUIDANCE", "guidance_scale", float),
            ("SIMAI_INPAINT_STRENGTH", "strength", float),
            ("SIMAI_INPAINT_INFER_SIZE", "diffusion_infer_size", int),
            ("SIMAI_INPAINT_DEVICE", "device", str),
            ("SIMAI_INPAINT_TILE_SIZE", "tile_size", int),
            ("SIMAI_INPAINT_TILE_OVERLAP", "tile_overlap", int),
            ("SIMAI_INPAINT_TELEA_RADIUS", "telea_radius", int),
            ("SIMAI_INPAINT_PATCH_SIZE", "patch_size", int),
            ("SIMAI_INPAINT_PATCH_BLEND", "patch_blend", float),
        ):
            if (val := env.get(env_key, "").strip()) != "":
                kw[field_name] = cast(val)
        if options:
            if "inpaint" in options and options["inpaint"] is not None:
                opt_inpaint = options["inpaint"]
                if isinstance(opt_inpaint, bool):
                    kw["mode"] = "classic" if opt_inpaint else "off"
                elif isinstance(opt_inpaint, str):
                    low = opt_inpaint.strip().lower()
                    if low in ("0", "false", "off", "no", "none"):
                        kw["mode"] = "off"
                    elif low in ("classic", "diffusion"):
                        kw["mode"] = low
                    else:
                        raise ValueError(f"지원하지 않는 options.inpaint={opt_inpaint!r}")
                else:
                    raise ValueError(f"지원하지 않는 options.inpaint={opt_inpaint!r}")
            for opt_key, field_name, cast in (
                ("inpaint_model_id", "model_id", str),
                ("inpaint_model_path", "model_path", str),
                ("inpaint_prompt_floor", "prompt_floor", str),
                ("inpaint_prompt_wall", "prompt_wall", str),
                ("inpaint_seed", "seed", int),
                ("inpaint_tile_size", "tile_size", int),
                ("inpaint_tile_overlap", "tile_overlap", int),
            ):
                if options.get(opt_key) is not None:
                    kw[field_name] = cast(options[opt_key])
        out = replace(cfg, **kw) if kw else cfg
        if out.tile_size < 16:
            raise ValueError(f"tile_size 는 16 이상이어야 한다: {out.tile_size}")
        if out.tile_overlap < 0 or out.tile_overlap >= out.tile_size:
            raise ValueError(f"tile_overlap({out.tile_overlap})은 0 이상 tile_size({out.tile_size}) 미만이어야 한다")
        return out


@dataclass
class PlaneInpaintResult:
    role: str
    image: np.ndarray  # (H, W, 3) uint8
    source: np.ndarray  # (H, W) uint8 (0=UNOBSERVED, 3=ESTIMATED_PLANE, 4=GENERATED)
    applied: bool
    backend_used: str  # diffusion | classic | gray_fallback | off | no_mask
    texel_ratios: dict[str, float]
    area_by_source_m2: dict[str, float]
    fill_m2: float
    masked_texels: int
    filled_texels: int
    tiles: int = 1
    fallback_from: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- PNG 읽기(무손실)


def decode_png(raw: bytes) -> np.ndarray:
    """`encode_png` 가 쓴 무손실 PNG(또는 일반 PNG)를 (H, W) 또는 (H, W, 3) uint8 로 디코딩한다."""
    if len(raw) >= 8 and raw[:8] == b"\x89PNG\r\n\x1a\n":
        pos = 8
        w = h = ct = interlace = 0
        idat = bytearray()
        while pos + 8 <= len(raw):
            ln = int.from_bytes(raw[pos : pos + 4], "big")
            tag = raw[pos + 4 : pos + 8]
            body = raw[pos + 8 : pos + 8 + ln]
            if tag == b"IHDR" and len(body) >= 13:
                w = int.from_bytes(body[0:4], "big")
                h = int.from_bytes(body[4:8], "big")
                bit_depth = body[8]
                ct = body[9]
                interlace = body[12]
                if bit_depth != 8 or ct not in (0, 2) or interlace != 0:
                    w = 0
                    break
            elif tag == b"IDAT":
                idat.extend(body)
            elif tag == b"IEND":
                break
            pos += 12 + ln
        if w > 0 and h > 0 and idat:
            c = 1 if ct == 0 else 3
            decomp = zlib.decompress(bytes(idat))
            stride = 1 + w * c
            if len(decomp) == h * stride:
                rows = np.frombuffer(decomp, dtype=np.uint8).reshape(h, stride)
                if np.all(rows[:, 0] == 0):
                    arr = rows[:, 1:].reshape(h, w, c).copy()
                    return arr[:, :, 0] if c == 1 else arr
    import cv2

    buf = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    if img is None:
        raise ValueError("PNG 디코딩 실패")
    if img.ndim == 2:
        return np.ascontiguousarray(img, dtype=np.uint8)
    if img.shape[2] == 4:
        img = img[:, :, :3]
    return np.ascontiguousarray(img[:, :, ::-1], dtype=np.uint8)


# --------------------------------------------------------------------------- 타일 분할 및 코사인 경계 블렌딩


def _tile_starts(length: int, tile_size: int, stride: int) -> list[int]:
    if length <= tile_size:
        return [0]
    starts = list(range(0, length - tile_size, stride))
    if not starts or starts[-1] != length - tile_size:
        starts.append(length - tile_size)
    return starts


def tile_boxes(h: int, w: int, tile_size: int, overlap: int) -> list[tuple[int, int, int, int]]:
    """(H, W) 평면을 겹침(`overlap`) 있는 `(y0, y1, x0, x1)` 타일 목록으로 나눈다."""
    th = min(h, tile_size)
    tw = min(w, tile_size)
    stride_y = max(1, th - overlap)
    stride_x = max(1, tw - overlap)
    ys = _tile_starts(h, th, stride_y)
    xs = _tile_starts(w, tw, stride_x)
    return [(y0, min(h, y0 + th), x0, min(w, x0 + tw)) for y0 in ys for x0 in xs]


def _axis_feather(length: int, has_prev: bool, has_next: bool, overlap: int) -> np.ndarray:
    w = np.ones(length, dtype=np.float64)
    ov = min(overlap, length // 2)
    if ov <= 0:
        return w
    t = (np.arange(ov, dtype=np.float64) + 0.5) / ov
    ramp = np.maximum(0.5 - 0.5 * np.cos(np.pi * t), 1e-3)
    if has_prev:
        w[:ov] = np.minimum(w[:ov], ramp)
    if has_next:
        w[length - ov :] = np.minimum(w[length - ov :], ramp[::-1])
    return w


def inpaint_tiled(
    img: np.ndarray,
    mask: np.ndarray,
    tile_fn: Callable[[np.ndarray, np.ndarray, int], np.ndarray],
    *,
    tile_size: int = 1024,
    overlap: int = 128,
) -> tuple[np.ndarray, int]:
    """큰 평면 이미지를 겹침 타일로 나눠 `tile_fn(tile_rgb, tile_mask, tile_idx)`을 실행하고 경계가 이어지게 합친다."""
    h, w = img.shape[:2]
    boxes = tile_boxes(h, w, tile_size, overlap)
    if len(boxes) == 1:
        out = tile_fn(img, mask, 0)
        res = img.copy()
        m = mask > 0
        res[m] = out[m]
        return res, 1

    acc = np.zeros((h, w, 3), dtype=np.float64)
    wsum = np.zeros((h, w), dtype=np.float64)
    used_tiles = 0
    for idx, (y0, y1, x0, x1) in enumerate(boxes):
        sub_mask = mask[y0:y1, x0:x1]
        sub_img = img[y0:y1, x0:x1]
        if (sub_mask > 0).any():
            tile_out = tile_fn(sub_img, sub_mask, idx)
            used_tiles += 1
        else:
            tile_out = sub_img
        wy = _axis_feather(y1 - y0, y0 > 0, y1 < h, overlap)
        wx = _axis_feather(x1 - x0, x0 > 0, x1 < w, overlap)
        w2d = wy[:, None] * wx[None, :]
        acc[y0:y1, x0:x1] += tile_out.astype(np.float64) * w2d[:, :, None]
        wsum[y0:y1, x0:x1] += w2d

    blended = np.clip(np.rint(acc / np.maximum(wsum[:, :, None], 1e-8)), 0, 255).astype(np.uint8)
    res = img.copy()
    m = mask > 0
    res[m] = blended[m]
    return res, max(1, used_tiles)


# --------------------------------------------------------------------------- (b) 전통 인페인팅: Telea + 같은 평면 패치 합성


def _propagate_observed_base(
    albedo: np.ndarray,
    obs_mask: np.ndarray,
    target_mask: np.ndarray,
    telea_radius: int,
) -> tuple[np.ndarray, np.ndarray]:
    """관측 텍셀(`obs_mask`)에서만 색을 전파해 영역 밖 회색(128)이 번지지 않는 부드러운 바탕 영상을 만든다."""
    import cv2
    import scipy.ndimage as ndi

    dist, (iy, ix) = ndi.distance_transform_edt(~obs_mask, return_indices=True)
    nearest_rgb = albedo[iy, ix].astype(np.float64)

    # 다중 스케일 가우스 블러로 먼 구멍 안쪽까지 급격한 보로노이 경계 없이 매끄러운 색 기저를 만든다.
    blur_fine = gaussian_blur(nearest_rgb, 6.0)
    blur_mid = gaussian_blur(nearest_rgb, 24.0)
    w_mid = np.clip((dist - 4.0) / 32.0, 0.0, 1.0)[:, :, None]
    harmonic = blur_fine * (1.0 - w_mid) + blur_mid * w_mid

    seeded = np.clip(np.rint(harmonic), 0, 255).astype(np.uint8)
    seeded[obs_mask] = albedo[obs_mask]

    # 관측 경계 주변을 cv2.inpaint(Telea)로 풀되, 영역 밖 배경은 이미 nearest/harmonic 으로 채워져 회색(128) 번짐이 없다.
    inpaint_u8 = (target_mask & ~obs_mask).astype(np.uint8) * 255
    if inpaint_u8.any():
        r = max(1, int(telea_radius))
        telea_bgr = cv2.inpaint(seeded[:, :, ::-1], inpaint_u8, float(r), cv2.INPAINT_TELEA)
        telea_rgb = np.ascontiguousarray(telea_bgr[:, :, ::-1])
        # 경계에서 2*r 안쪽은 Telea 를 우선하고 깊은 내부는 다중 스케일 조화 기저와 부드럽게 섞는다.
        alpha = np.clip(dist / max(float(r * 3), 6.0), 0.0, 0.65)[:, :, None]
        base = np.clip(
            np.rint(telea_rgb.astype(np.float64) * (1.0 - alpha) + harmonic * alpha),
            0,
            255,
        ).astype(np.uint8)
    else:
        base = seeded
    base[obs_mask] = albedo[obs_mask]
    return base, dist


def _extract_observed_patches(
    base_rgb: np.ndarray,
    obs_mask: np.ndarray,
    patch_size: int,
    max_patches: int = 512,
) -> tuple[np.ndarray, np.ndarray] | None:
    """같은 평면의 관측 영역(`obs_mask`)에서 패치 평균색 `means (K, 3)` 과 영평균 무늬 변이 `details (K, P, P, 3)` 를 뽑는다."""
    h, w = obs_mask.shape
    P = min(patch_size, h, w)
    if P < 4:
        return None
    step = max(2, P // 2)
    ys = range(0, h - P + 1, step)
    xs = range(0, w - P + 1, step)

    means: list[np.ndarray] = []
    details: list[np.ndarray] = []
    for min_cov in (0.75, 0.35):
        for y0 in ys:
            for x0 in xs:
                m_win = obs_mask[y0 : y0 + P, x0 : x0 + P]
                if float(m_win.mean()) < min_cov:
                    continue
                patch = base_rgb[y0 : y0 + P, x0 : x0 + P].astype(np.float64)
                obs_pixels = patch[m_win]
                mu = obs_pixels.mean(axis=0)
                det = np.where(m_win[:, :, None], patch - mu[None, None, :], 0.0)
                # 지나치게 튀는 경계 이상치는 눌러 자연스러운 바닥/벽 결만 이어 붙인다.
                det = np.clip(det, -32.0, 32.0)
                means.append(mu)
                details.append(det)
        if len(means) >= 8:
            break

    if not means:
        return None
    if len(means) > max_patches:
        idx = np.linspace(0, len(means) - 1, max_patches, dtype=int)
        means = [means[i] for i in idx]
        details = [details[i] for i in idx]
    return np.asarray(means, dtype=np.float64), np.asarray(details, dtype=np.float64)


def _synthesize_tile_patches(
    tile_base: np.ndarray,
    tile_mask: np.ndarray,
    tile_dist: np.ndarray,
    cand_means: np.ndarray,
    cand_details: np.ndarray,
    *,
    patch_blend: float,
    seed: int,
) -> np.ndarray:
    """한 타일의 마스크 영역에 관측 패치 무늬 변이를 한 겹의 50% 겹침 코사인 창으로 합성한다."""
    h, w = tile_mask.shape
    P = cand_details.shape[1]
    if h < P or w < P or patch_blend <= 0.0:
        return tile_base

    step = max(2, P // 2)
    ys = _tile_starts(h, P, step)
    xs = _tile_starts(w, P, step)
    t = (np.arange(P, dtype=np.float64) + 0.5) / P
    hann1d = np.maximum(0.5 - 0.5 * np.cos(2.0 * np.pi * t), 1e-3)
    win2d = hann1d[:, None] * hann1d[None, :]

    detail_acc = np.zeros((h, w, 3), dtype=np.float64)
    w_acc = np.zeros((h, w), dtype=np.float64)
    top_k = min(8, len(cand_means))

    for gy, y0 in enumerate(ys):
        for gx, x0 in enumerate(xs):
            if not (tile_mask[y0 : y0 + P, x0 : x0 + P] > 0).any():
                continue
            target_mu = tile_base[y0 : y0 + P, x0 : x0 + P].reshape(-1, 3).mean(axis=0)
            d2 = np.sum((cand_means - target_mu[None, :]) ** 2, axis=1)
            if top_k > 1:
                nearest = np.argpartition(d2, top_k - 1)[:top_k]
                nearest = nearest[np.argsort(d2[nearest])]
                pick_idx = ((gy * 73856093) ^ (gx * 19349663) ^ (seed * 83492791)) % top_k
                chosen = int(nearest[pick_idx])
            else:
                chosen = int(np.argmin(d2))
            det = cand_details[chosen]
            if ((gy + gx + seed) & 1) == 1:
                det = det[:, ::-1, :]
            if ((gy * 3 + gx + seed) & 2) == 2:
                det = det[::-1, :, :]
            detail_acc[y0 : y0 + P, x0 : x0 + P] += det * win2d[:, :, None]
            w_acc[y0 : y0 + P, x0 : x0 + P] += win2d

    valid = w_acc > 1e-6
    synth_detail = np.zeros((h, w, 3), dtype=np.float64)
    synth_detail[valid] = detail_acc[valid] / w_acc[valid, None]
    # 관측 경계에서는 Telea 연속성을 그대로 두고 안쪽으로 들어갈수록 패치 무늬를 자연스럽게 키운다.
    boundary_ramp = np.clip(tile_dist / max(float(P) * 0.75, 2.0), 0.0, 1.0)[:, :, None]
    out = tile_base.astype(np.float64) + float(patch_blend) * boundary_ramp * synth_detail
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def classic_inpaint_plane(
    albedo: np.ndarray,
    mask: np.ndarray,
    surface_source: np.ndarray | None = None,
    *,
    role: str = "floor",
    config: InpaintConfig | None = None,
) -> tuple[np.ndarray, int]:
    """(b) 전통 인페인팅: cv2 Telea 색 전파 + 같은 평면의 관측 텍셀 패치 합성(타일 겹침 블렌딩)."""
    cfg = config or InpaintConfig()
    target_mask = mask > 0
    if not target_mask.any():
        return albedo.copy(), 0

    if surface_source is not None and ((mask == 0) & (surface_source == SRC_PROJECTED)).any():
        obs_mask = (mask == 0) & (surface_source == SRC_PROJECTED)
    else:
        obs_mask = mask == 0
    if not obs_mask.any():
        raise RuntimeError(f"{role}: 평면에 관측 텍셀이 없어 전통 인페인팅을 수행할 수 없다")

    base_rgb, dist = _propagate_observed_base(albedo, obs_mask, target_mask, cfg.telea_radius)
    patches = _extract_observed_patches(base_rgb, obs_mask, cfg.patch_size)

    h, w = albedo.shape[:2]
    boxes = tile_boxes(h, w, cfg.tile_size, cfg.tile_overlap)

    def _run_tile(tile_img: np.ndarray, tile_m: np.ndarray, tile_idx: int) -> np.ndarray:
        y0, y1, x0, x1 = boxes[tile_idx]
        t_base = base_rgb[y0:y1, x0:x1]
        t_dist = dist[y0:y1, x0:x1]
        if patches is None:
            return t_base
        return _synthesize_tile_patches(
            t_base,
            tile_m,
            t_dist,
            patches[0],
            patches[1],
            patch_blend=cfg.patch_blend,
            seed=cfg.seed + tile_idx,
        )

    out, tiles = inpaint_tiled(
        albedo,
        mask,
        _run_tile,
        tile_size=cfg.tile_size,
        overlap=cfg.tile_overlap,
    )
    out[~target_mask] = albedo[~target_mask]
    return out, tiles


# --------------------------------------------------------------------------- (a) GPU 확산 모델 인페인팅 (지연 import)


def prompt_for_role(role: str, cfg: InpaintConfig) -> str:
    return cfg.prompt_floor if role.startswith("floor") else cfg.prompt_wall


def _load_diffusion_pipeline(cfg: InpaintConfig) -> Any:
    """diffusers 인페인팅 파이프라인을 지연 로드한다(FLUX.1 Fill dev 또는 SDXL/SD Inpaint)."""
    import torch
    from diffusers import AutoPipelineForInpainting

    model_ref = cfg.model_path or cfg.model_id
    cache_key = (model_ref, cfg.device)
    if cache_key in _DIFFUSION_PIPE_CACHE:
        return _DIFFUSION_PIPE_CACHE[cache_key]

    if cfg.device.startswith("cuda") and not torch.cuda.is_available():
        raise RuntimeError("CUDA GPU 를 사용할 수 없어 diffusion 인페인팅을 실행할 수 없다")

    dtype = torch.bfloat16 if (cfg.device.startswith("cuda") and "flux" in model_ref.lower()) else (
        torch.float16 if cfg.device.startswith("cuda") else torch.float32
    )
    if "flux" in model_ref.lower():
        from diffusers import FluxFillPipeline

        pipe = FluxFillPipeline.from_pretrained(model_ref, torch_dtype=dtype)
    else:
        pipe = AutoPipelineForInpainting.from_pretrained(model_ref, torch_dtype=dtype)
    pipe = pipe.to(cfg.device)
    if hasattr(pipe, "set_progress_bar_config"):
        pipe.set_progress_bar_config(disable=True)
    _DIFFUSION_PIPE_CACHE[cache_key] = pipe
    return pipe


def diffusion_inpaint_plane(
    albedo: np.ndarray,
    mask: np.ndarray,
    surface_source: np.ndarray | None = None,
    *,
    role: str = "floor",
    config: InpaintConfig | None = None,
) -> tuple[np.ndarray, int]:
    """(a) GPU 확산 모델 인페인팅. 모델·해상도·타일링·프롬프트·시드 고정은 `InpaintConfig` 를 따른다."""
    import cv2
    import torch
    from PIL import Image

    cfg = config or InpaintConfig(mode="diffusion")
    target_mask = mask > 0
    if not target_mask.any():
        return albedo.copy(), 0

    pipe = _load_diffusion_pipeline(cfg)
    # 확산 모델에 회색(128) 대신 관측 전파 바탕을 주면 타일 내부에서도 평면의 색조·밝기가 유지된다.
    try:
        obs_mask = (
            (mask == 0) & (surface_source == SRC_PROJECTED)
            if surface_source is not None and ((mask == 0) & (surface_source == SRC_PROJECTED)).any()
            else (mask == 0)
        )
        cond_rgb, _ = _propagate_observed_base(albedo, obs_mask, target_mask, cfg.telea_radius)
    except Exception:  # noqa: BLE001
        cond_rgb = albedo.copy()

    prompt = prompt_for_role(role, cfg)
    infer_size = max(256, (int(cfg.diffusion_infer_size) // 8) * 8)

    def _run_tile(tile_img: np.ndarray, tile_m: np.ndarray, tile_idx: int) -> np.ndarray:
        th, tw = tile_img.shape[:2]
        pil_img = Image.fromarray(tile_img).resize((infer_size, infer_size), Image.Resampling.BILINEAR)
        pil_mask = Image.fromarray((tile_m > 0).astype(np.uint8) * 255).resize(
            (infer_size, infer_size), Image.Resampling.NEAREST
        )
        gen = torch.Generator(device=cfg.device).manual_seed(int(cfg.seed) + int(tile_idx))
        call_kw: dict[str, Any] = {
            "prompt": prompt,
            "image": pil_img,
            "mask_image": pil_mask,
            "num_inference_steps": int(cfg.num_inference_steps),
            "guidance_scale": float(cfg.guidance_scale),
            "generator": gen,
            "width": infer_size,
            "height": infer_size,
        }
        if "flux" not in (cfg.model_path or cfg.model_id).lower():
            call_kw["negative_prompt"] = cfg.negative_prompt
            call_kw["strength"] = float(cfg.strength)
        res_pil = pipe(**call_kw).images[0]
        res_arr = np.asarray(res_pil.convert("RGB"), dtype=np.uint8)
        if res_arr.shape[:2] != (th, tw):
            res_arr = cv2.resize(res_arr, (tw, th), interpolation=cv2.INTER_LINEAR)
        return res_arr

    out, tiles = inpaint_tiled(
        cond_rgb,
        mask,
        _run_tile,
        tile_size=cfg.tile_size,
        overlap=cfg.tile_overlap,
    )
    out[~target_mask] = albedo[~target_mask]
    return out, tiles


# --------------------------------------------------------------------------- 평면 1개 인페인팅 + 출처/불변성 보장


BackendFn = Callable[..., np.ndarray | tuple[np.ndarray, int]]


def _call_backend(
    fn: BackendFn,
    albedo: np.ndarray,
    mask: np.ndarray,
    surface_source: np.ndarray | None,
    role: str,
    cfg: InpaintConfig,
) -> tuple[np.ndarray, int]:
    try:
        res = fn(albedo, mask, surface_source, role=role, config=cfg)
    except TypeError:
        res = fn(albedo, mask, surface_source, role, cfg)
    if isinstance(res, tuple):
        img, tiles = res
        return np.asarray(img, dtype=np.uint8), int(tiles)
    return np.asarray(res, dtype=np.uint8), len(tile_boxes(albedo.shape[0], albedo.shape[1], cfg.tile_size, cfg.tile_overlap))


def _compute_plane_ratios(
    source: np.ndarray,
    region_mask: np.ndarray,
    fill_m2: float,
) -> tuple[dict[str, float], dict[str, float]]:
    n_reg = max(int(region_mask.sum()), 1)
    s_reg = source[region_mask]
    ratios = {
        "ESTIMATED_PLANE": round(float((s_reg == SRC_PROJECTED).sum()) / n_reg, 4),
        "GENERATED": round(float((s_reg == SRC_GENERATED).sum()) / n_reg, 4),
        "UNOBSERVED": round(float((s_reg == SRC_GRAY).sum()) / n_reg, 4),
    }
    areas = {k: round(float(fill_m2) * v, 3) for k, v in ratios.items()}
    return ratios, areas


def inpaint_plane(
    albedo: np.ndarray,
    inpaint_mask: np.ndarray,
    surface_source: np.ndarray,
    *,
    role: str = "floor",
    fill_m2: float = 0.0,
    config: InpaintConfig | None = None,
    diffusion_fn: BackendFn | None = None,
    classic_fn: BackendFn | None = None,
) -> PlaneInpaintResult:
    """평면 하나를 3단 백엔드(diffusion → classic → gray)로 인페인팅하고 관측 텍셀 불변·GENERATED 출처를 보장한다."""
    cfg = config or InpaintConfig()
    albedo_u8 = np.ascontiguousarray(albedo, dtype=np.uint8)
    mask_u8 = np.ascontiguousarray(inpaint_mask, dtype=np.uint8)
    src_u8 = np.ascontiguousarray(surface_source, dtype=np.uint8)
    if albedo_u8.ndim != 3 or albedo_u8.shape[2] != 3:
        raise ValueError(f"{role}: albedo 모양 오류 {albedo_u8.shape}")
    if mask_u8.shape != albedo_u8.shape[:2] or src_u8.shape != albedo_u8.shape[:2]:
        raise ValueError(f"{role}: mask/surface_source 해상도가 albedo 와 다르다")
    if max(albedo_u8.shape[:2]) > cfg.max_plane_side:
        raise ValueError(f"{role}: 평면 해상도 {albedo_u8.shape[:2]} 가 상한 {cfg.max_plane_side} px 를 넘는다")

    target = mask_u8 > 0
    region_mask = (src_u8 != SRC_GRAY) | target
    masked_count = int(target.sum())

    if cfg.mode == "off":
        ratios, areas = _compute_plane_ratios(src_u8, region_mask, fill_m2)
        return PlaneInpaintResult(
            role=role,
            image=albedo_u8.copy(),
            source=src_u8.copy(),
            applied=False,
            backend_used="off",
            texel_ratios=ratios,
            area_by_source_m2=areas,
            fill_m2=float(fill_m2),
            masked_texels=masked_count,
            filled_texels=0,
            tiles=0,
        )

    if masked_count == 0:
        ratios, areas = _compute_plane_ratios(src_u8, region_mask, fill_m2)
        return PlaneInpaintResult(
            role=role,
            image=albedo_u8.copy(),
            source=src_u8.copy(),
            applied=False,
            backend_used="no_mask",
            texel_ratios=ratios,
            area_by_source_m2=areas,
            fill_m2=float(fill_m2),
            masked_texels=0,
            filled_texels=0,
            tiles=0,
        )

    diff_runner = diffusion_fn or diffusion_inpaint_plane
    classic_runner = classic_fn or classic_inpaint_plane
    chain: list[tuple[str, BackendFn]] = []
    if cfg.mode == "diffusion":
        chain.append(("diffusion", diff_runner))
    chain.append(("classic", classic_runner))

    errors: list[str] = []
    fallback_from: str | None = None
    for backend_name, runner in chain:
        try:
            cand_img, tiles = _call_backend(runner, albedo_u8, mask_u8, src_u8, role, cfg)
            if cand_img.shape != albedo_u8.shape:
                raise ValueError(f"백엔드 출력 모양 불일치: {cand_img.shape} != {albedo_u8.shape}")
            # 관측 텍셀(mask == 0)은 바이트 단위로 절대 바꾸지 않는다.
            out_img = albedo_u8.copy()
            out_img[target] = cand_img[target]
            if not np.array_equal(out_img[~target], albedo_u8[~target]):
                raise AssertionError("관측 텍셀 불변성 위반")

            out_src = src_u8.copy()
            out_src[target] = SRC_GENERATED
            ratios, areas = _compute_plane_ratios(out_src, region_mask, fill_m2)
            return PlaneInpaintResult(
                role=role,
                image=out_img,
                source=out_src,
                applied=True,
                backend_used=backend_name,
                texel_ratios=ratios,
                area_by_source_m2=areas,
                fill_m2=float(fill_m2),
                masked_texels=masked_count,
                filled_texels=masked_count,
                tiles=tiles,
                fallback_from=fallback_from,
                error="; ".join(errors) if errors else None,
            )
        except Exception as exc:  # noqa: BLE001 - 백엔드 실패 시 다음 단계(classic → 회색 유지)로 폴백
            errors.append(f"{backend_name}:{type(exc).__name__}:{exc}")
            if fallback_from is None:
                fallback_from = backend_name
            else:
                fallback_from = f"{fallback_from}->{backend_name}"

    # (c) 모든 백엔드 실패 시 회색 유지(UNOBSERVED = 0 그대로, 관측 텍셀 불변)
    out_img = albedo_u8.copy()
    out_src = src_u8.copy()
    ratios, areas = _compute_plane_ratios(out_src, region_mask, fill_m2)
    return PlaneInpaintResult(
        role=role,
        image=out_img,
        source=out_src,
        applied=False,
        backend_used="gray_fallback",
        texel_ratios=ratios,
        area_by_source_m2=areas,
        fill_m2=float(fill_m2),
        masked_texels=masked_count,
        filled_texels=0,
        tiles=0,
        fallback_from=fallback_from,
        error="; ".join(errors) if errors else None,
    )


# --------------------------------------------------------------------------- 출처 비율 집계 및 GLB 되돌려 쓰기


def summarize_surface_source(
    planes: Sequence[PlaneInpaintResult],
    mesh_area_m2: float | None = None,
) -> dict[str, Any]:
    """평면별 인페인팅 결과를 합산해 `ESTIMATED_PLANE`/`GENERATED`/`UNOBSERVED` 면적·비율을 낸다."""
    by_src = {"ESTIMATED_PLANE": 0.0, "GENERATED": 0.0, "UNOBSERVED": 0.0}
    for p in planes:
        for k in by_src:
            by_src[k] += float(p.area_by_source_m2.get(k, 0.0))
    fill_area = sum(by_src.values())
    out: dict[str, Any] = {
        "values": dict(FILL_SURFACE_VALUES),
        "meaning": {
            "ESTIMATED_PLANE": "색은 보정 카메라 사진 투영(관측 픽셀)이고 형상은 맞춘 평면",
            "GENERATED": "사이드카 마스크(미관측) 영역을 이미지 인페인팅으로 채운 생성 텍셀(시각 전용)",
            "UNOBSERVED": "사진 근거 없음, 중립 회색(색을 지어내지 않음)",
        },
        "fill_area_m2": {k: round(v, 3) for k, v in by_src.items()},
        "fill_ratio": {k: round(v / fill_area, 4) if fill_area > 0 else None for k, v in by_src.items()},
    }
    if mesh_area_m2 is not None:
        out["fill_share_of_surface"] = round(fill_area / max(fill_area + float(mesh_area_m2), 1e-12), 4)
        out["mesh_area_m2_after_removal"] = round(float(mesh_area_m2), 3)
    return out


def apply_inpaint_to_glb(
    raw_glb: bytes,
    planes: Sequence[PlaneInpaintResult],
    inpaint_summary: Mapping[str, Any],
    surface_source_summary: Mapping[str, Any] | None = None,
) -> bytes:
    """인페인팅된 평면 알베도·출처 지도를 GLB 의 `estimated_<role>_*` 이미지와 노드/문서 메타데이터에 되돌려 쓴다.

    원본 관측 메시·텍스처 바이트 접두부는 그대로 두고 새 PNG bufferView 를 BIN 끝에 덧붙인다.
    """
    g: GlbFile = parse_glb(raw_glb)
    doc = g.doc
    images = doc.get("images") or []
    nodes = doc.get("nodes") or []
    by_role = {p.role: p for p in planes}

    for role, pres in by_role.items():
        albedo_name = f"estimated_{role}_albedo"
        source_name = f"estimated_{role}_surface_source"
        node_name = f"estimated_{role}_fill"
        for im in images:
            if im.get("name") == albedo_name:
                im["bufferView"] = _add_view(g, encode_png(pres.image), None)
                im["mimeType"] = "image/png"
            elif im.get("name") == source_name:
                im["bufferView"] = _add_view(g, encode_png(pres.source), None)
                im["mimeType"] = "image/png"
        for node in nodes:
            if node.get("name") == node_name:
                sim = node.setdefault("extras", {}).setdefault("simai", {})
                sim["source_values"] = dict(FILL_SURFACE_VALUES)
                sim["texel_ratios"] = dict(pres.texel_ratios)
                sim["inpaint"] = {
                    "applied": pres.applied,
                    "backend": pres.backend_used,
                    "masked_texels": pres.masked_texels,
                    "filled_texels": pres.filled_texels,
                }

    simai = doc.setdefault("extras", {}).setdefault("simai", {})
    enh = simai.setdefault("enhanced", {})
    if isinstance(enh, dict):
        steps = list(enh.get("steps") or [])
        if any(p.applied for p in planes) and "plane_inpaint" not in steps:
            steps.append("plane_inpaint")
            enh["steps"] = steps
        if surface_source_summary is not None and isinstance(enh.get("fill"), dict):
            enh["fill"]["surface_source"] = dict(surface_source_summary)
        enh["inpaint"] = {
            "applied": bool(inpaint_summary.get("applied")),
            "mode": inpaint_summary.get("mode"),
            "backend_used": inpaint_summary.get("backend_used"),
            "surface_source": dict(surface_source_summary) if surface_source_summary is not None else None,
        }
    return serialize_glb(g)


def inpaint_sidecar_to_glb(
    glb_path: Path,
    sidecar_dir: Path,
    *,
    config: InpaintConfig | None = None,
    fill_report: dict[str, Any] | None = None,
    diffusion_fn: BackendFn | None = None,
    classic_fn: BackendFn | None = None,
) -> dict[str, Any]:
    """사이드카 폴더(`<이름>_fill/`)의 마스크 영역을 인페인팅해 `glb_path` 에 되돌려 쓰고 요약을 돌려준다."""
    t0 = time.monotonic()
    cfg = config or InpaintConfig.from_env()
    glb_path = Path(glb_path)
    sidecar_dir = Path(sidecar_dir)
    atlas_path = sidecar_dir / "fill_atlas.json"
    if not atlas_path.is_file():
        return {
            "applied": False,
            "reason": "NO_SIDECAR_ATLAS",
            "mode": cfg.mode,
            "backend_used": None,
            "purpose": "VISUAL_ONLY",
            "safety_use": False,
            "seconds": round(time.monotonic() - t0, 3),
        }

    atlas = json.loads(atlas_path.read_text(encoding="utf-8"))
    plane_entries = atlas.get("planes") or []
    if cfg.mode == "off":
        return {
            "applied": False,
            "reason": "DISABLED",
            "mode": "off",
            "backend_used": "off",
            "purpose": "VISUAL_ONLY",
            "safety_use": False,
            "config": asdict(cfg),
            "planes": [p.get("role") for p in plane_entries],
            "seconds": round(time.monotonic() - t0, 3),
        }

    results: list[PlaneInpaintResult] = []
    for entry in plane_entries:
        role = str(entry["role"])
        files = entry["files"]
        albedo = decode_png((sidecar_dir / files["albedo"]).read_bytes())
        mask = decode_png((sidecar_dir / files["inpaint_mask"]).read_bytes())
        # 재실행(예: classic 뒤 GPU diffusion)에서도 원본 투영 출처(3)와 마스크(255)로부터 깨끗하게 출발한다.
        raw_src = decode_png((sidecar_dir / files["surface_source"]).read_bytes())
        base_src = np.where(mask > 0, SRC_GRAY, raw_src).astype(np.uint8)
        fill_m2 = float(entry.get("fill_m2") or 0.0)
        pres = inpaint_plane(
            albedo,
            mask,
            base_src,
            role=role,
            fill_m2=fill_m2,
            config=cfg,
            diffusion_fn=diffusion_fn,
            classic_fn=classic_fn,
        )
        results.append(pres)

        # 사이드카에 인페인팅 결과 이미지(`<role>_inpainted.png`)와 갱신된 출처 지도(`<role>_surface_source.png`)를 저장한다.
        # 원본 `<role>_albedo.png` 와 `<role>_inpaint_mask.png` 는 보존해 GPU 세션 등에서 다시 인페인팅할 수 있게 한다.
        inpainted_name = f"{role}_inpainted.png"
        (sidecar_dir / inpainted_name).write_bytes(encode_png(pres.image))
        (sidecar_dir / files["surface_source"]).write_bytes(encode_png(pres.source))
        files["inpainted"] = inpainted_name
        entry["texel_ratios"] = dict(pres.texel_ratios)
        entry["area_by_source_m2"] = dict(pres.area_by_source_m2)
        entry["inpaint"] = {
            "applied": pres.applied,
            "backend_used": pres.backend_used,
            "masked_texels": pres.masked_texels,
            "filled_texels": pres.filled_texels,
            "tiles": pres.tiles,
            "fallback_from": pres.fallback_from,
            "error": pres.error,
        }

    mesh_area = None
    if isinstance(fill_report, Mapping):
        mesh_area = (fill_report.get("surface_source") or {}).get("mesh_area_m2_after_removal")
    ss_summary = summarize_surface_source(results, mesh_area_m2=mesh_area)

    atlas["source_values"] = dict(FILL_SURFACE_VALUES)
    atlas["surface_source"] = ss_summary
    backends_used = sorted({r.backend_used for r in results if r.applied})
    primary_backend = backends_used[0] if len(backends_used) == 1 else ("+".join(backends_used) if backends_used else (results[0].backend_used if results else None))
    any_applied = any(r.applied for r in results)

    summary: dict[str, Any] = {
        "applied": any_applied,
        "reason": None if any_applied else (results[0].backend_used.upper() if results else "NO_PLANES"),
        "mode": cfg.mode,
        "backend_used": primary_backend,
        "purpose": "VISUAL_ONLY",
        "safety_use": False,
        "config": asdict(cfg),
        "surface_source": ss_summary,
        "planes": {
            r.role: {
                "applied": r.applied,
                "backend_used": r.backend_used,
                "fill_m2": r.fill_m2,
                "masked_texels": r.masked_texels,
                "filled_texels": r.filled_texels,
                "tiles": r.tiles,
                "texel_ratios": r.texel_ratios,
                "area_by_source_m2": r.area_by_source_m2,
                "fallback_from": r.fallback_from,
                "error": r.error,
            }
            for r in results
        },
        "seconds": round(time.monotonic() - t0, 3),
    }
    atlas["inpaint"] = {
        "applied": any_applied,
        "mode": cfg.mode,
        "backend_used": primary_backend,
        "seconds": summary["seconds"],
    }
    atlas_path.write_text(json.dumps(atlas, ensure_ascii=False, indent=1), encoding="utf-8")

    if any_applied and glb_path.is_file():
        updated_glb = apply_inpaint_to_glb(glb_path.read_bytes(), results, summary, ss_summary)
        tmp = Path(str(glb_path) + ".inpaint.tmp")
        tmp.write_bytes(updated_glb)
        os.replace(tmp, glb_path)

    if isinstance(fill_report, dict) and any_applied:
        fill_report["surface_source"] = ss_summary
        if "floor" in fill_report and isinstance(fill_report["floor"], dict) and "floor" in summary["planes"]:
            pf = summary["planes"]["floor"]
            fill_report["floor"]["area_by_source_m2"] = dict(pf["area_by_source_m2"])
            fill_report["floor"]["generated_ratio"] = pf["texel_ratios"]["GENERATED"]
            fill_report["floor"]["gray_ratio"] = pf["texel_ratios"]["UNOBSERVED"]
        if "walls" in fill_report and isinstance(fill_report["walls"], list):
            for k, wrec in enumerate(fill_report["walls"]):
                wrole = f"wall_{k}"
                if isinstance(wrec, dict) and wrec.get("applied") and wrole in summary["planes"]:
                    pw = summary["planes"][wrole]
                    wrec["area_by_source_m2"] = dict(pw["area_by_source_m2"])
                    wrec["generated_ratio"] = pw["texel_ratios"]["GENERATED"]
                    wrec["gray_ratio"] = pw["texel_ratios"]["UNOBSERVED"]

    return summary


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="GLB 평면 채움 사이드카(<이름>_fill/)의 미관측 마스크 영역을 인페인팅해 GLB 에 되돌려 쓴다(시각 전용)."
    )
    ap.add_argument("--glb", required=True, type=Path, help="보강된 GLB 파일 경로(제자리 갱신 또는 --out 으로 출력)")
    ap.add_argument("--sidecar", type=Path, help="사이드카 폴더 경로(기본: <glb 이름>_fill/)")
    ap.add_argument("--out", type=Path, help="결과 GLB 를 다른 경로로 저장할 때 지정")
    ap.add_argument("--mode", choices=["off", "classic", "diffusion"], help="인페인팅 백엔드 모드(기본: SIMAI_GLB_INPAINT 또는 classic)")
    ap.add_argument("--model-id", help="diffusion 모델 ID 또는 로컬 가중치 경로")
    ap.add_argument("--seed", type=int, help="시드 고정값")
    ap.add_argument("--tile-size", type=int, help="타일 한 변 크기(px)")
    ap.add_argument("--tile-overlap", type=int, help="타일 겹침 폭(px)")
    ap.add_argument("--report", type=Path, help="인페인팅 결과 JSON 저장 경로")
    args = ap.parse_args(argv)

    opts: dict[str, Any] = {}
    if args.mode:
        opts["inpaint"] = args.mode
    if args.model_id:
        opts["inpaint_model_id"] = args.model_id
    if args.seed is not None:
        opts["inpaint_seed"] = args.seed
    if args.tile_size is not None:
        opts["inpaint_tile_size"] = args.tile_size
    if args.tile_overlap is not None:
        opts["inpaint_tile_overlap"] = args.tile_overlap

    cfg = InpaintConfig.from_env(options=opts)
    target_glb = args.glb
    if args.out is not None and args.out.resolve() != args.glb.resolve():
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_bytes(args.glb.read_bytes())
        target_glb = args.out
    sidecar = args.sidecar if args.sidecar is not None else args.glb.with_name(args.glb.stem + "_fill")
    rep = inpaint_sidecar_to_glb(target_glb, sidecar, config=cfg)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: rep.get(k) for k in ("applied", "mode", "backend_used", "seconds")}, ensure_ascii=False))
    return 0


__all__ = [
    "DEFAULT_DIFFUSION_MODEL_ID",
    "DEFAULT_NEGATIVE_PROMPT",
    "DEFAULT_PROMPT_FLOOR",
    "DEFAULT_PROMPT_WALL",
    "GRAY",
    "INPAINT_MODES",
    "InpaintConfig",
    "PlaneInpaintResult",
    "SRC_GENERATED",
    "SRC_GRAY",
    "SRC_PROJECTED",
    "apply_inpaint_to_glb",
    "classic_inpaint_plane",
    "decode_png",
    "diffusion_inpaint_plane",
    "inpaint_plane",
    "inpaint_sidecar_to_glb",
    "inpaint_tiled",
    "main",
    "prompt_for_role",
    "summarize_surface_source",
    "tile_boxes",
]


if __name__ == "__main__":
    raise SystemExit(main())
