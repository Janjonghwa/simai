"""Pod 의 사람 지운 배경 묶음(-363)을 객체 저장소에 올리고 `static_reconstruct`(-364) 작업 입력을 만드는 세션 도구(S15P31A101-372, -461).

Tier 0 가 모은 `clean-background/` 묶음 폴더(또는 4대 카메라 배경 이미지)와 활성 보정 문서를 받아,
1) 로컬에서 묶음 무결성(`status.json`·`COMPLETE`·`clean_backgrounds.json`·이미지 해시·해상도)과 보정 문서를 먼저 검사하고,
2) 최근 구간 배경 중앙값 합성·이전 배경/텍스처 채움·경계 이음새 블렌딩 및 품질 비율(`previous_fill_ratio`, `unrevealed_ratio`) 기록(-461)을 수행하며,
3) 서명 업로드(`POST /api/uploads/sign` + `PUT upload_url`)로 각 카메라 이미지를 객체 저장소(`s3://...`)에 올린 뒤,
4) `-364` `static_reconstruct` 핸들러가 그대로 받는 작업 입력 JSON 을 만들고 스키마·계약 검사를 거쳐 저장하며,
5) EC2 에서 실행할 `admin job enqueue`, `admin scene list`, `admin scene activate` 명령과 LIVE 감시 중 거절 안내를 출력한다.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid
import warnings

try:
    import cv2
except ImportError:  # pragma: no cover - cv2 없는 최소 CI 환경 대비
    cv2 = None  # type: ignore[assignment]
import numpy as np

from simai_ai.providers.contract import schema_errors
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.scene.reconstruct import (
    ReconstructionError,
    read_json,
    sha256,
    validate_input,
    write_json,
)

# 서명 업로드에 쓸 수 있는 업로드 목적과 기본 토큰 환경변수 우선순위
IMAGE_KINDS = ("CLEAN_BACKGROUND", "FRAME")
OBJECT_URI_RE = re.compile(r"^s3://(?P<bucket>[a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/\S+$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
# 워커 토큰은 정적 3D 입력 목적(STATIC_RECONSTRUCT_INPUT, 키 static-inputs/, 사진·보정 JSON, S15P31A101-403)을 쓴다
UPLOAD_PURPOSES = ("EVENT_FRAME", "STATIC_RECONSTRUCT_INPUT")
WORKER_PURPOSE = "STATIC_RECONSTRUCT_INPUT"
TOKEN_ENV_VARS = ("SIMAI_UPLOAD_TOKEN", "SIMAI_TIER0_TOKEN", "SIMAI_DEVICE_TOKEN", "SIMAI_WORKER_TOKEN")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DEFAULT_DRY_RUN_BUCKET = "simai-dry-run"
DEFAULT_TIMEOUT_MS = 4_200_000
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RECENT_WINDOW_FRAMES = 30
DEFAULT_SEAM_BLEND_RADIUS_PX = 8
DEFAULT_BBOX_PAD_PX = 12


class CleanBgJobError(RuntimeError):
    """배경 묶음 검사·서명 업로드·작업 입력 생성 중 일어난 오류."""


class _NoRedirect(HTTPRedirectHandler):
    """서명 업로드 요청이나 Authorization 헤더가 다른 호스트로 넘어가지 않도록 리다이렉트를 막는다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, D401
        raise HTTPError(req.full_url, code, "리다이렉트는 허용하지 않는다", headers, fp)


@dataclass(frozen=True)
class CameraRegenerateQuality:
    """단일 카메라의 재생성 배경 합성 품질 비율(S15P31A101-461)."""

    previous_fill_ratio: float
    unrevealed_ratio: float

    def to_dict(self) -> dict[str, float]:
        return {
            "previous_fill_ratio": round(float(np.clip(self.previous_fill_ratio, 0.0, 1.0)), 6),
            "unrevealed_ratio": round(float(np.clip(self.unrevealed_ratio, 0.0, 1.0)), 6),
        }


@dataclass(frozen=True)
class RegenerateQuality:
    """묶음 전체 및 카메라별 재생성 배경 합성 품질 요약(S15P31A101-461)."""

    previous_fill_ratio: float
    unrevealed_ratio: float
    cameras: dict[str, CameraRegenerateQuality]

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_fill_ratio": round(float(np.clip(self.previous_fill_ratio, 0.0, 1.0)), 6),
            "unrevealed_ratio": round(float(np.clip(self.unrevealed_ratio, 0.0, 1.0)), 6),
            "cameras": {cid: cam_q.to_dict() for cid, cam_q in self.cameras.items()},
        }


@dataclass(frozen=True)
class SynthesizedBackground:
    """최근 구간 중앙값 + 이전 배경 채움 + 이음새 블렌딩 결과(S15P31A101-461)."""

    image_bgr: np.ndarray
    valid_mask: np.ndarray
    recent_revealed_mask: np.ndarray
    previous_filled_mask: np.ndarray
    unrevealed_mask: np.ndarray
    quality: CameraRegenerateQuality
    frames_used: int


@dataclass(frozen=True)
class PreparedImage:
    """업로드할 단일 카메라 이미지 정보."""

    camera_id: str
    path: Path
    content_type: str
    raw_bytes: bytes
    sha256_prefixed: str
    sha256_hex: str
    width: int
    height: int
    kind: str


@dataclass(frozen=True)
class PreparedBundle:
    """검사를 마친 배경 이미지 묶음과 입력 출처."""

    calibration_version: str
    images: tuple[PreparedImage, ...]
    frames_provenance: dict[str, Any]
    preserve_upper_bound: bool = False
    regenerate_quality: dict[str, Any] | None = None


def _person_mask_for_frame(
    spec: Any,
    height: int,
    width: int,
    *,
    bbox_pad_px: int = DEFAULT_BBOX_PAD_PX,
) -> np.ndarray:
    """프레임 하나에 대한 사람 가림 마스크(`True`=사람에 가려진 픽셀)를 만든다."""
    if spec is None:
        return np.zeros((height, width), dtype=bool)
    if isinstance(spec, np.ndarray):
        if spec.shape != (height, width):
            raise CleanBgJobError(
                f"사람 마스크 해상도({spec.shape})가 프레임 해상도({(height, width)})와 다르다"
            )
        return spec > 0
    mask = np.zeros((height, width), dtype=bool)
    pad = max(0, int(bbox_pad_px))
    for item in spec:
        if isinstance(item, Mapping):
            coords = item.get("bbox_xyxy") or item.get("bbox")
        else:
            coords = item
        if coords is None or len(coords) != 4:
            raise CleanBgJobError(f"유효하지 않은 사람 bbox 형식이다: {item!r}")
        x1, y1, x2, y2 = (float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3]))
        ix1 = max(0, min(width, int(np.floor(min(x1, x2))) - pad))
        iy1 = max(0, min(height, int(np.floor(min(y1, y2))) - pad))
        ix2 = max(0, min(width, int(np.ceil(max(x1, x2))) + pad))
        iy2 = max(0, min(height, int(np.ceil(max(y1, y2))) + pad))
        if ix2 > ix1 and iy2 > iy1:
            mask[iy1:iy2, ix1:ix2] = True
    return mask


def compute_recent_window_median(
    frames: Sequence[np.ndarray],
    person_masks: Sequence[Any] | None = None,
    *,
    window_frames: int = DEFAULT_RECENT_WINDOW_FRAMES,
    bbox_pad_px: int = DEFAULT_BBOX_PAD_PX,
) -> tuple[np.ndarray, np.ndarray, int]:
    """최근 창(`window_frames`) 안에서 사람 마스크를 제외한 픽셀들의 중앙값 배경을 계산한다(S15P31A101-461).

    사람이 계속 앉아 있어 최근 창 내내 가려진 픽셀은 절대 가려진 프레임들의 중앙값으로 채우지 않고,
    `recent_revealed=False` 및 픽셀 값 `0` 으로 남겨 다음 단계(이전 배경 채움 또는 비움 기록)로 넘긴다.
    """
    if window_frames < 1:
        raise CleanBgJobError(f"window_frames 는 1 이상이어야 한다: {window_frames}")
    if not frames:
        raise CleanBgJobError("배경 합성에 필요한 프레임이 비어 있다")
    if person_masks is not None and len(person_masks) != len(frames):
        raise CleanBgJobError(
            f"frames 길이({len(frames)})와 person_masks 길이({len(person_masks)})가 다르다"
        )

    recent_frames = list(frames[-window_frames:])
    recent_specs = list(person_masks[-window_frames:]) if person_masks is not None else [None] * len(recent_frames)

    first = np.asarray(recent_frames[0])
    if first.ndim != 3 or first.shape[2] != 3:
        raise CleanBgJobError(f"프레임은 (H, W, 3) BGR 배열이어야 한다: {first.shape}")
    height, width = int(first.shape[0]), int(first.shape[1])

    frame_stack = np.empty((len(recent_frames), height, width, 3), dtype=np.float32)
    person_stack = np.zeros((len(recent_frames), height, width), dtype=bool)
    for idx, (frm, spec) in enumerate(zip(recent_frames, recent_specs)):
        arr = np.asarray(frm)
        if arr.shape != (height, width, 3):
            raise CleanBgJobError(
                f"{idx}번째 프레임 해상도({arr.shape})가 첫 프레임({(height, width, 3)})과 다르다"
            )
        p_mask = _person_mask_for_frame(spec, height, width, bbox_pad_px=bbox_pad_px)
        person_stack[idx] = p_mask
        frame_f32 = arr.astype(np.float32)
        frame_f32[p_mask] = np.nan
        frame_stack[idx] = frame_f32

    recent_revealed = np.any(~person_stack, axis=0)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        median_f32 = np.nanmedian(frame_stack, axis=0)

    median_f32 = np.nan_to_num(median_f32, nan=0.0)
    median_f32[~recent_revealed] = 0.0
    recent_bg = np.clip(np.rint(median_f32), 0, 255).astype(np.uint8)
    return recent_bg, recent_revealed, len(recent_frames)


def _distance_to_mask(target_mask: np.ndarray) -> np.ndarray:
    """`target_mask`(`True` 영역)까지의 픽셀 유클리드 거리(`float32`)를 계산한다."""
    non_target = (~target_mask).astype(np.uint8)
    if cv2 is not None:
        return cv2.distanceTransform(non_target, cv2.DIST_L2, 3).astype(np.float32)
    from scipy.ndimage import distance_transform_edt  # pragma: no cover

    return distance_transform_edt(non_target).astype(np.float32)


def _normalized_gaussian_extend(
    image_bgr: np.ndarray,
    valid_mask: np.ndarray,
    radius_px: int,
) -> np.ndarray:
    """유효 마스크 안의 픽셀만 가우시안 정규화 합성곱으로 경계 바깥으로 부드럽게 확장한다."""
    ksize = max(3, int(radius_px) * 2 + 1)
    if ksize % 2 == 0:
        ksize += 1
    sigma = max(1.0, float(radius_px) * 0.6)
    mask_f = valid_mask.astype(np.float32)
    weighted = image_bgr.astype(np.float32) * mask_f[..., None]
    if cv2 is not None:
        num = cv2.GaussianBlur(weighted, (ksize, ksize), sigmaX=sigma, sigmaY=sigma)
        den = cv2.GaussianBlur(mask_f, (ksize, ksize), sigmaX=sigma, sigmaY=sigma)[..., None]
    else:  # pragma: no cover
        from scipy.ndimage import gaussian_filter

        num = gaussian_filter(weighted, sigma=(sigma, sigma, 0.0))
        den = gaussian_filter(mask_f, sigma=(sigma, sigma))[..., None]
    extended = np.where(den > 1e-5, num / np.maximum(den, 1e-5), image_bgr.astype(np.float32))
    # 원래 유효 영역 내부는 원본 색을 중심으로 유지하고 경계 바깥만 확장값을 제공한다
    return np.where(valid_mask[..., None], image_bgr.astype(np.float32), extended)


def blend_seam_feather(
    recent_bg: np.ndarray,
    recent_revealed: np.ndarray,
    previous_bg: np.ndarray,
    prev_filled_mask: np.ndarray,
    *,
    prev_available_mask: np.ndarray | None = None,
    blend_radius_px: int = DEFAULT_SEAM_BLEND_RADIUS_PX,
) -> np.ndarray:
    """최근 중앙값 배경(`recent_revealed`)과 이전 배경으로 채운 영역(`prev_filled_mask`)의 경계를 거리 가중으로 섞는다(S15P31A101-461)."""
    composite = np.zeros_like(recent_bg, dtype=np.uint8)
    composite[recent_revealed] = recent_bg[recent_revealed]
    composite[prev_filled_mask] = previous_bg[prev_filled_mask]

    if blend_radius_px <= 0 or not np.any(recent_revealed) or not np.any(prev_filled_mask):
        return composite

    radius = float(blend_radius_px)
    dist_to_prev = _distance_to_mask(prev_filled_mask)
    dist_to_recent = _distance_to_mask(recent_revealed)

    on_recent_seam = recent_revealed & (dist_to_prev <= radius)
    on_prev_seam = prev_filled_mask & (dist_to_recent <= radius)
    seam_band = on_recent_seam | on_prev_seam
    if not np.any(seam_band):
        return composite

    prev_avail = prev_filled_mask if prev_available_mask is None else prev_available_mask.astype(bool)
    recent_ext = _normalized_gaussian_extend(recent_bg, recent_revealed, blend_radius_px)
    prev_ext = _normalized_gaussian_extend(previous_bg, prev_avail, blend_radius_px)

    signed_dist = np.where(recent_revealed, dist_to_prev, -dist_to_recent)
    alpha = np.clip(0.5 + 0.5 * (signed_dist / radius), 0.0, 1.0)[..., None]

    blended = alpha * recent_ext + (1.0 - alpha) * prev_ext
    composite[seam_band] = np.clip(np.rint(blended[seam_band]), 0, 255).astype(np.uint8)
    # 관측되지도 않고 이전 배경으로도 채우지 못한 영역은 절대 건드리지 않고 0(비움)으로 유지한다
    unrevealed = ~(recent_revealed | prev_filled_mask)
    composite[unrevealed] = 0
    return composite


def compose_with_previous_background(
    recent_bg: np.ndarray,
    recent_revealed: np.ndarray,
    *,
    previous_background: np.ndarray | None = None,
    previous_valid_mask: np.ndarray | None = None,
    blend_radius_px: int = DEFAULT_SEAM_BLEND_RADIUS_PX,
    frames_used: int = 0,
) -> SynthesizedBackground:
    """최근 구간 중앙값 배경의 미관측 픽셀을 이전 배경으로 채우고 이음새를 섞어 품질 비율을 계산한다(S15P31A101-461)."""
    if recent_bg.ndim != 3 or recent_bg.shape[2] != 3:
        raise CleanBgJobError(f"recent_bg 는 (H, W, 3) 배열이어야 한다: {recent_bg.shape}")
    height, width = int(recent_bg.shape[0]), int(recent_bg.shape[1])
    rev_mask = np.asarray(recent_revealed, dtype=bool)
    if rev_mask.shape != (height, width):
        raise CleanBgJobError(
            f"recent_revealed 해상도({rev_mask.shape})가 이미지 해상도({(height, width)})와 다르다"
        )

    if previous_background is not None:
        prev_arr = np.asarray(previous_background, dtype=np.uint8)
        if prev_arr.shape != (height, width, 3):
            raise CleanBgJobError(
                f"previous_background 해상도({prev_arr.shape})가 현재 배경({(height, width, 3)})과 다르다"
            )
        if previous_valid_mask is not None:
            prev_valid = np.asarray(previous_valid_mask) > 0
            if prev_valid.shape != (height, width):
                raise CleanBgJobError(
                    f"previous_valid_mask 해상도({prev_valid.shape})가 현재 배경({(height, width)})과 다르다"
                )
        else:
            prev_valid = np.ones((height, width), dtype=bool)

        prev_filled_mask = (~rev_mask) & prev_valid
        unrevealed_mask = (~rev_mask) & (~prev_valid)
        image_bgr = blend_seam_feather(
            recent_bg,
            rev_mask,
            prev_arr,
            prev_filled_mask,
            prev_available_mask=prev_valid,
            blend_radius_px=blend_radius_px,
        )
    else:
        prev_filled_mask = np.zeros((height, width), dtype=bool)
        unrevealed_mask = ~rev_mask
        image_bgr = recent_bg.copy()
        image_bgr[unrevealed_mask] = 0

    valid_mask = np.where(rev_mask | prev_filled_mask, 255, 0).astype(np.uint8)
    quality = CameraRegenerateQuality(
        previous_fill_ratio=float(np.mean(prev_filled_mask)),
        unrevealed_ratio=float(np.mean(unrevealed_mask)),
    )
    return SynthesizedBackground(
        image_bgr=image_bgr,
        valid_mask=valid_mask,
        recent_revealed_mask=rev_mask,
        previous_filled_mask=prev_filled_mask,
        unrevealed_mask=unrevealed_mask,
        quality=quality,
        frames_used=int(frames_used),
    )


def synthesize_regenerated_background(
    frames: Sequence[np.ndarray],
    person_masks: Sequence[Any] | None = None,
    *,
    window_frames: int = DEFAULT_RECENT_WINDOW_FRAMES,
    previous_background: np.ndarray | None = None,
    previous_valid_mask: np.ndarray | None = None,
    blend_radius_px: int = DEFAULT_SEAM_BLEND_RADIUS_PX,
    bbox_pad_px: int = DEFAULT_BBOX_PAD_PX,
) -> SynthesizedBackground:
    """최근 구간 중앙값 계산 → 못 본 곳 이전 배경 채움 → 이음새 블렌딩 → 품질 기록을 한 번에 수행한다(S15P31A101-461)."""
    recent_bg, recent_revealed, frames_used = compute_recent_window_median(
        frames,
        person_masks,
        window_frames=window_frames,
        bbox_pad_px=bbox_pad_px,
    )
    return compose_with_previous_background(
        recent_bg,
        recent_revealed,
        previous_background=previous_background,
        previous_valid_mask=previous_valid_mask,
        blend_radius_px=blend_radius_px,
        frames_used=frames_used,
    )


def _load_bgr_image(path: Path) -> np.ndarray:
    if cv2 is None:  # pragma: no cover
        raise CleanBgJobError("OpenCV(cv2)가 설치되어 있지 않아 이미지를 읽을 수 없다")
    raw = path.read_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.ndim != 3:
        raise CleanBgJobError(f"배경 이미지를 디코딩할 수 없다: {path}")
    return img


def _load_gray_mask(path: Path) -> np.ndarray:
    if cv2 is None:  # pragma: no cover
        raise CleanBgJobError("OpenCV(cv2)가 설치되어 있지 않아 마스크를 읽을 수 없다")
    raw = path.read_bytes()
    arr = np.frombuffer(raw, dtype=np.uint8)
    mask = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if mask is None or mask.ndim != 2:
        raise CleanBgJobError(f"유효 마스크를 디코딩할 수 없다: {path}")
    return mask


def _write_bgr_image(path: Path, image_bgr: np.ndarray, *, jpeg_quality: int = 95) -> None:
    if cv2 is None:  # pragma: no cover
        raise CleanBgJobError("OpenCV(cv2)가 설치되어 있지 않아 이미지를 쓸 수 없다")
    ext = path.suffix.lower()
    params = [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)] if ext in {".jpg", ".jpeg"} else []
    ok, buf = cv2.imencode(ext if ext in {".jpg", ".jpeg", ".png"} else ".jpg", image_bgr, params)
    if not ok:
        raise CleanBgJobError(f"배경 이미지를 인코딩할 수 없다: {path}")
    path.write_bytes(buf.tobytes())


def _write_gray_mask(path: Path, mask: np.ndarray) -> None:
    if cv2 is None:  # pragma: no cover
        raise CleanBgJobError("OpenCV(cv2)가 설치되어 있지 않아 마스크를 쓸 수 없다")
    ok, buf = cv2.imencode(".png", mask)
    if not ok:
        raise CleanBgJobError(f"유효 마스크를 인코딩할 수 없다: {path}")
    path.write_bytes(buf.tobytes())


def _discover_previous_camera_background(
    bundle_dir: Path,
    cid: str,
    cal_version: str,
    generation: Any,
    *,
    previous_bundle_dir: Path | None = None,
    previous_backgrounds: Mapping[str, np.ndarray | Path] | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """카메라 `cid` 의 이전 배경 이미지와 유효 마스크를 찾는다."""
    if previous_backgrounds and cid in previous_backgrounds:
        item = previous_backgrounds[cid]
        if isinstance(item, np.ndarray):
            return item, None
        p = Path(item)
        if p.is_file():
            mask_p = p.with_name(f"{cid}.valid.png")
            return _load_bgr_image(p), (_load_gray_mask(mask_p) if mask_p.is_file() else None)

    candidate_dirs: list[Path] = []
    if previous_bundle_dir is not None and previous_bundle_dir.is_dir():
        candidate_dirs.append(previous_bundle_dir)

    version_dir = bundle_dir.parent
    if version_dir.is_dir():
        siblings: list[tuple[float, Path]] = []
        for child in version_dir.iterdir():
            if not child.is_dir() or child.resolve() == bundle_dir.resolve():
                continue
            if not (child.name.startswith("bundle-") or child.name.startswith("camera-")):
                continue
            meta_p = child / "clean_backgrounds.json"
            if not meta_p.is_file():
                continue
            try:
                mtime = meta_p.stat().st_mtime
            except OSError:
                mtime = 0.0
            siblings.append((mtime, child))
        # 최신 완료 묶음 또는 초기 REFERENCE 카메라 폴더 순으로 탐색한다
        siblings.sort(key=lambda pair: pair[0], reverse=True)
        candidate_dirs.extend(folder for _, folder in siblings)

    for folder in candidate_dirs:
        meta_p = folder / "clean_backgrounds.json"
        if not meta_p.is_file():
            img_candidates = [folder / f"{cid}{ext}" for ext in (".png", ".jpg", ".jpeg") if (folder / f"{cid}{ext}").is_file()]
            if img_candidates:
                mask_p = folder / f"{cid}.valid.png"
                return _load_bgr_image(img_candidates[0]), (_load_gray_mask(mask_p) if mask_p.is_file() else None)
            continue
        try:
            doc = read_json(meta_p)
        except Exception:
            continue
        if doc.get("calibration_version") != cal_version:
            continue
        doc_gens = doc.get("camera_generations")
        if generation is not None and isinstance(doc_gens, Mapping) and doc_gens.get(cid) not in (None, generation):
            continue
        cam_entry = (doc.get("cameras") or {}).get(cid)
        if not isinstance(cam_entry, Mapping) or not cam_entry.get("ready"):
            continue
        rel_img = cam_entry.get("image")
        if not isinstance(rel_img, str) or not (folder / rel_img).is_file():
            continue
        img_bgr = _load_bgr_image(folder / rel_img)
        rel_mask = cam_entry.get("valid_mask")
        mask_arr = (
            _load_gray_mask(folder / rel_mask)
            if isinstance(rel_mask, str) and (folder / rel_mask).is_file()
            else None
        )
        return img_bgr, mask_arr
    return None, None


def refine_bundle_backgrounds(
    bundle_dir: Path,
    *,
    previous_bundle_dir: Path | None = None,
    previous_backgrounds: Mapping[str, np.ndarray | Path] | None = None,
    window_frames: int = DEFAULT_RECENT_WINDOW_FRAMES,
    blend_radius_px: int = DEFAULT_SEAM_BLEND_RADIUS_PX,
) -> RegenerateQuality | None:
    """묶음 폴더의 각 카메라 배경에 대해 못 본 곳을 이전 배경으로 채우고 이음새를 섞은 뒤 품질 지표를 기록한다(S15P31A101-461)."""
    meta_path = bundle_dir / "clean_backgrounds.json"
    if not meta_path.is_file():
        return None
    try:
        meta_doc = read_json(meta_path)
    except Exception as exc:
        raise CleanBgJobError(f"clean_backgrounds.json 읽기 실패 ({meta_path}): {exc}") from exc

    cameras_meta = meta_doc.get("cameras")
    if not isinstance(cameras_meta, dict) or not cameras_meta:
        return None

    cal_version = str(meta_doc.get("calibration_version") or "")
    generations = meta_doc.get("camera_generations") if isinstance(meta_doc.get("camera_generations"), Mapping) else {}
    cam_qualities: dict[str, CameraRegenerateQuality] = {}
    modified = False

    for cid, entry in cameras_meta.items():
        if not isinstance(entry, dict):
            continue
        rel_img = entry.get("image")
        if not isinstance(rel_img, str) or not (bundle_dir / rel_img).is_file():
            continue
        img_path = bundle_dir / rel_img
        bg_source = entry.get("background_source")

        # 1) 카메라 전체가 세팅 기준(REFERENCE_FALLBACK)으로 대체된 경우
        if bg_source == "REFERENCE_FALLBACK":
            rel_mask = entry.get("valid_mask")
            if isinstance(rel_mask, str) and (bundle_dir / rel_mask).is_file():
                valid_arr = _load_gray_mask(bundle_dir / rel_mask)
                unrev_ratio = float(np.mean(valid_arr == 0))
                prev_ratio = float(np.mean(valid_arr > 0))
            else:
                unrev_ratio = float(entry.get("unrevealed_ratio", entry.get("invalid_ratio", 0.0)))
                prev_ratio = max(0.0, 1.0 - unrev_ratio)
            q = CameraRegenerateQuality(previous_fill_ratio=prev_ratio, unrevealed_ratio=unrev_ratio)
            entry["previous_fill_ratio"] = q.to_dict()["previous_fill_ratio"]
            entry["unrevealed_ratio"] = q.to_dict()["unrevealed_ratio"]
            cam_qualities[cid] = q
            modified = True
            continue

        # 2) 묶음 내 최근 창 표본(samples/<cid>/samples.json)이 있으면 최근 창 중앙값부터 합성한다
        samples_manifest = bundle_dir / "samples" / cid / "samples.json"
        recent_bg: np.ndarray | None = None
        recent_revealed: np.ndarray | None = None
        if samples_manifest.is_file():
            s_doc = read_json(samples_manifest)
            s_items = s_doc.get("samples") or []
            s_frames: list[np.ndarray] = []
            s_masks: list[Any] = []
            for item in s_items:
                f_path = samples_manifest.parent / str(item["frame"])
                s_frames.append(_load_bgr_image(f_path))
                if "mask" in item and item["mask"] is not None:
                    s_masks.append(_load_gray_mask(samples_manifest.parent / str(item["mask"])))
                else:
                    s_masks.append(item.get("bboxes") or [])
            recent_bg, recent_revealed, _ = compute_recent_window_median(
                s_frames,
                s_masks,
                window_frames=window_frames,
            )

        # 3) 아니면 Tier 0 가 내보낸 현재 배경 이미지 + 유효 마스크(<cid>.valid.png)를 읽는다
        if recent_bg is None or recent_revealed is None:
            rel_mask = entry.get("valid_mask")
            mask_path = bundle_dir / rel_mask if isinstance(rel_mask, str) else bundle_dir / f"{cid}.valid.png"
            if mask_path.is_file():
                # 이미 이전 실행에서 채움이 끝난 묶음이고 original_valid_mask 가 따로 없다면 기록된 값을 유지한다
                if "previous_fill_ratio" in entry and "unrevealed_ratio" in entry and previous_backgrounds is None and previous_bundle_dir is None:
                    q = CameraRegenerateQuality(
                        previous_fill_ratio=float(entry["previous_fill_ratio"]),
                        unrevealed_ratio=float(entry["unrevealed_ratio"]),
                    )
                    cam_qualities[cid] = q
                    continue
                recent_bg = _load_bgr_image(img_path)
                valid_arr = _load_gray_mask(mask_path)
                recent_revealed = valid_arr > 0
            else:
                prev_ratio = float(entry.get("previous_fill_ratio", 0.0))
                unrev_ratio = float(
                    entry.get(
                        "unrevealed_ratio",
                        entry.get("invalid_ratio", entry.get("never_revealed_ratio", 0.0)),
                    )
                )
                q = CameraRegenerateQuality(previous_fill_ratio=prev_ratio, unrevealed_ratio=unrev_ratio)
                entry["previous_fill_ratio"] = q.to_dict()["previous_fill_ratio"]
                entry["unrevealed_ratio"] = q.to_dict()["unrevealed_ratio"]
                cam_qualities[cid] = q
                modified = True
                continue

        # 4) 못 본 곳(~recent_revealed)이 있으면 이전 배경을 찾아 채우고 이음새를 섞는다
        prev_img: np.ndarray | None = None
        prev_mask: np.ndarray | None = None
        if not np.all(recent_revealed):
            prev_img, prev_mask = _discover_previous_camera_background(
                bundle_dir,
                cid,
                cal_version,
                generations.get(cid) if isinstance(generations, Mapping) else None,
                previous_bundle_dir=previous_bundle_dir,
                previous_backgrounds=previous_backgrounds,
            )
            if prev_img is not None and prev_img.shape != recent_bg.shape:
                prev_img, prev_mask = None, None

        synth = compose_with_previous_background(
            recent_bg,
            recent_revealed,
            previous_background=prev_img,
            previous_valid_mask=prev_mask,
            blend_radius_px=blend_radius_px,
        )
        _write_bgr_image(img_path, synth.image_bgr)
        mask_name = entry.get("valid_mask") if isinstance(entry.get("valid_mask"), str) else f"{cid}.valid.png"
        _write_gray_mask(bundle_dir / mask_name, synth.valid_mask)

        entry["valid_mask"] = mask_name
        entry["image_sha256"] = sha256(img_path.read_bytes())
        entry["invalid_ratio"] = synth.quality.to_dict()["unrevealed_ratio"]
        entry["previous_fill_ratio"] = synth.quality.to_dict()["previous_fill_ratio"]
        entry["unrevealed_ratio"] = synth.quality.to_dict()["unrevealed_ratio"]
        cam_qualities[cid] = synth.quality
        modified = True

    if not cam_qualities:
        return None

    overall_prev = float(np.mean([q.previous_fill_ratio for q in cam_qualities.values()]))
    overall_unrev = float(np.mean([q.unrevealed_ratio for q in cam_qualities.values()]))
    regen_quality = RegenerateQuality(
        previous_fill_ratio=overall_prev,
        unrevealed_ratio=overall_unrev,
        cameras=cam_qualities,
    )
    if modified or "regenerate_quality" not in meta_doc:
        meta_doc["regenerate_quality"] = regen_quality.to_dict()
        write_json(meta_path, meta_doc)
        parent_status = bundle_dir.parent.parent / "status.json"
        if parent_status.is_file():
            try:
                status_doc = read_json(parent_status)
                if isinstance(status_doc.get("bundle"), str) and Path(status_doc["bundle"]).name == bundle_dir.name:
                    status_doc["cameras"] = cameras_meta
                    status_doc["regenerate_quality"] = regen_quality.to_dict()
                    write_json(parent_status, status_doc)
            except Exception:
                pass
    return regen_quality


def _sanitize_url(url: str) -> str:
    """오류 메시지에 쿼리 문자열(서명 토큰 등)이 남지 않도록 스킴·호스트·경로만 남긴다."""
    try:
        parts = urlsplit(url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        pass
    return "<redacted-url>"


def _check_transport_url(url: str, *, label: str) -> str:
    """HTTPS 주소(또는 로컬 시험용 loopback HTTP)만 허용한다."""
    clean = (url or "").strip().rstrip("/")
    parts = urlsplit(clean)
    if parts.scheme == "https" and parts.netloc:
        return clean
    if parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost", "::1"}:
        return clean
    raise CleanBgJobError(f"{label} 는 https:// 주소여야 한다(로컬 시험은 127.0.0.1/localhost 만 허용): {_sanitize_url(clean)}")


def _detect_content_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if ext == ".png":
        return "image/png"
    raise CleanBgJobError(f"지원하지 않는 이미지 확장자다(.jpg/.jpeg/.png 만 허용): {path.name}")


def _decode_image_size(raw_bytes: bytes, *, label: str) -> tuple[int, int]:
    if cv2 is None:  # pragma: no cover
        raise CleanBgJobError("OpenCV(cv2)가 설치되어 있지 않아 이미지 해상도를 확인할 수 없다")
    arr = np.frombuffer(raw_bytes, dtype=np.uint8)
    if arr.size == 0:
        raise CleanBgJobError(f"이미지 파일이 비어 있다: {label}")
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None or img.ndim != 3:
        raise CleanBgJobError(f"이미지를 디코딩할 수 없다: {label}")
    height, width = int(img.shape[0]), int(img.shape[1])
    return width, height


def load_calibration(path: Path) -> tuple[dict[str, Any], str, bytes]:
    """보정 JSON 문서를 읽고 스키마·상태를 검증한 뒤 (문서, sha256:<hex64>, 원문 바이트)를 돌려준다."""
    if not path.is_file():
        raise CleanBgJobError(f"보정 파일이 없다: {path}")
    raw_bytes = path.read_bytes()
    if not raw_bytes:
        raise CleanBgJobError(f"보정 파일이 비어 있다: {path}")
    try:
        cal = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise CleanBgJobError(f"보정 JSON 파싱 실패 ({path}): {exc}") from exc
    if not isinstance(cal, dict):
        raise CleanBgJobError(f"보정 문서는 JSON 객체여야 한다: {path}")
    if cal.get("state") not in {"PROVISIONAL", "CALIBRATED"}:
        raise CleanBgJobError(f"보정 상태는 PROVISIONAL 또는 CALIBRATED 여야 한다: {cal.get('state')!r}")
    if errors := schema_errors(cal, "calibration"):
        raise CleanBgJobError(f"보정 문서가 계약을 위반했다: {'; '.join(errors)}")
    return cal, sha256(raw_bytes), raw_bytes


def _cameras_by_id(cal: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """보정 문서의 `cameras` 배열을 `camera_id -> 카메라 사전`으로 색인한다."""
    raw = cal.get("cameras")
    if isinstance(raw, list):
        return {str(c["camera_id"]): c for c in raw if isinstance(c, dict) and "camera_id" in c}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    raise CleanBgJobError("보정 문서에 유효한 cameras 목록이 없다")


def _resolve_bundle_dir(bundle_arg: Path, cal_version: str) -> tuple[Path, dict[str, Any] | None]:
    """`clean-background/` 루트 또는 `bundle-<uuid>/` 경로에서 활성 묶음 디렉터리와 `status.json` 을 찾는다.

    `status.json` 이 없는 상위 폴더에서 `bundle-*` 를 글로빙으로 고르지 않는다(-363 소비자 규칙).
    """
    if not bundle_arg.is_dir():
        raise CleanBgJobError(f"배경 묶음 디렉터리가 없다: {bundle_arg}")

    status_path = bundle_arg / "status.json"
    if status_path.is_file():
        try:
            status = read_json(status_path)
        except Exception as exc:
            raise CleanBgJobError(f"status.json 읽기 실패 ({status_path}): {exc}") from exc
        if status.get("calibration_version") != cal_version:
            raise CleanBgJobError(
                f"status.json 의 calibration_version({status.get('calibration_version')!r})이 "
                f"보정 문서({cal_version!r})와 다르다"
            )
        if status.get("complete") is not True:
            raise CleanBgJobError("status.json 의 complete 가 true 가 아니다(모든 카메라 배경 수집이 끝나지 않음)")
        bundle_ref = status.get("bundle")
        if not isinstance(bundle_ref, str) or not bundle_ref.strip():
            raise CleanBgJobError("status.json 에 완료된 bundle 경로가 없다")
        direct = Path(bundle_ref)
        bundle_name = direct.name
        local_candidate = bundle_arg / cal_version / bundle_name
        # Pod 내부 절대 경로(/workspace/simai/...)가 적힌 status.json 을 노트북으로 복사해 온 경우 로컬 하위 경로를 먼저 본다
        if local_candidate.is_dir():
            return local_candidate, status
        if direct.is_dir():
            return direct, status
        raise CleanBgJobError(f"status.json 이 가리키는 bundle 폴더를 찾을 수 없다: {bundle_ref}")

    complete_path = bundle_arg / "COMPLETE"
    meta_path = bundle_arg / "clean_backgrounds.json"
    if complete_path.is_file() or meta_path.is_file():
        parent_status_path = bundle_arg.parent.parent / "status.json"
        status: dict[str, Any] | None = None
        if parent_status_path.is_file():
            try:
                status = read_json(parent_status_path)
            except Exception as exc:
                raise CleanBgJobError(f"상위 status.json 읽기 실패 ({parent_status_path}): {exc}") from exc
            if status.get("calibration_version") != cal_version:
                raise CleanBgJobError(
                    f"상위 status.json 의 calibration_version({status.get('calibration_version')!r})이 "
                    f"보정 문서({cal_version!r})와 다르다"
                )
            if status.get("complete") is not True:
                raise CleanBgJobError("상위 status.json 의 complete 가 true 가 아니다")
            bundle_ref = status.get("bundle")
            if not isinstance(bundle_ref, str) or Path(bundle_ref).name != bundle_arg.name:
                raise CleanBgJobError(
                    f"상위 status.json 의 활성 묶음({bundle_ref!r})과 지정한 폴더({bundle_arg.name!r})가 다르다"
                )
        return bundle_arg, status

    raise CleanBgJobError(
        f"유효한 -363 배경 묶음이 아니다(status.json 또는 COMPLETE + clean_backgrounds.json 필요): {bundle_arg}"
    )


def prepare_from_bundle(
    bundle_arg: Path,
    cal: dict[str, Any],
    *,
    image_kind: str = "CLEAN_BACKGROUND",
) -> PreparedBundle:
    """`-363` 배경 묶음 폴더를 검증하고 업로드할 이미지 목록을 준비한다."""
    if image_kind not in IMAGE_KINDS:
        raise CleanBgJobError(f"지원하지 않는 이미지 kind 다: {image_kind!r}")
    cal_version = str(cal["calibration_version"])
    cal_cameras = _cameras_by_id(cal)
    bundle_dir, status = _resolve_bundle_dir(bundle_arg, cal_version)

    complete_path = bundle_dir / "COMPLETE"
    meta_path = bundle_dir / "clean_backgrounds.json"
    if not complete_path.is_file():
        raise CleanBgJobError(f"배경 묶음에 COMPLETE 표식이 없다: {complete_path}")
    if not meta_path.is_file():
        raise CleanBgJobError(f"배경 묶음에 clean_backgrounds.json 이 없다: {meta_path}")

    try:
        complete_doc = read_json(complete_path)
        meta_doc = read_json(meta_path)
    except Exception as exc:
        raise CleanBgJobError(f"배경 묶음 메타데이터 읽기 실패: {exc}") from exc

    if complete_doc.get("calibration_version") != cal_version:
        raise CleanBgJobError(
            f"COMPLETE 의 calibration_version({complete_doc.get('calibration_version')!r})이 "
            f"보정 문서({cal_version!r})와 다르다"
        )
    if complete_doc.get("metadata") != "clean_backgrounds.json":
        raise CleanBgJobError(f"COMPLETE 의 metadata 필드가 올바르지 않다: {complete_doc.get('metadata')!r}")

    if meta_doc.get("format") != "simai.clean_backgrounds":
        raise CleanBgJobError(f"clean_backgrounds.json format 불일치: {meta_doc.get('format')!r}")
    if meta_doc.get("calibration_version") != cal_version:
        raise CleanBgJobError(
            f"clean_backgrounds.json 의 calibration_version({meta_doc.get('calibration_version')!r})이 "
            f"보정 문서({cal_version!r})와 다르다"
        )
    if meta_doc.get("ready") is not True or meta_doc.get("complete") is not True:
        raise CleanBgJobError("clean_backgrounds.json 의 ready/complete 가 true 가 아니다")

    if status is not None and status.get("camera_generations") != meta_doc.get("camera_generations"):
        raise CleanBgJobError("status.json 과 clean_backgrounds.json 의 camera_generations 가 일치하지 않는다")

    cameras_meta = meta_doc.get("cameras")
    if not isinstance(cameras_meta, dict) or not (2 <= len(cameras_meta) <= 16):
        raise CleanBgJobError("clean_backgrounds.json 의 cameras 는 2~16대여야 한다")

    complete_cids = complete_doc.get("camera_ids")
    # Tier 0 는 COMPLETE 에 정렬된 목록을, cameras 사전은 표본 도착 순서로 쓴다(12차: A,B,C,D vs B,A,C,D).
    # 같은 카메라 집합인지만 보고, 뒤 순서는 COMPLETE 순서로 고정한다.
    if (
        not isinstance(complete_cids, list)
        or not all(isinstance(cid, str) for cid in complete_cids)
        or len(set(complete_cids)) != len(complete_cids)
        or sorted(complete_cids) != sorted(cameras_meta.keys())
    ):
        raise CleanBgJobError("COMPLETE 의 camera_ids 와 clean_backgrounds.json 의 cameras 목록이 일치하지 않는다")

    bundle_root = bundle_dir.resolve()
    prepared: list[PreparedImage] = []
    cam_qualities: dict[str, dict[str, float]] = {}
    has_quality_fields = False
    for cid in complete_cids:
        entry = cameras_meta[cid]
        if cid not in cal_cameras:
            raise CleanBgJobError(f"배경 묶음 카메라 {cid!r} 가 보정 문서에 없다")
        if not isinstance(entry, dict) or entry.get("camera_id") != cid:
            raise CleanBgJobError(f"clean_backgrounds.json 의 카메라 항목이 올바르지 않다: {cid!r}")
        if entry.get("ready") is not True:
            raise CleanBgJobError(f"카메라 {cid!r} 의 배경 이미지가 ready 상태가 아니다")
        if entry.get("calibration_version") != cal_version:
            raise CleanBgJobError(f"카메라 {cid!r} 의 calibration_version 이 보정 문서와 다르다")
        rel_name = entry.get("image")
        if not isinstance(rel_name, str) or not rel_name.strip():
            raise CleanBgJobError(f"카메라 {cid!r} 의 image 파일명이 비어 있다")
        img_path = (bundle_dir / rel_name).resolve()
        if not img_path.is_relative_to(bundle_root) or not img_path.is_file():
            raise CleanBgJobError(f"카메라 {cid!r} 의 이미지 파일이 없거나 묶음 폴더 밖이다: {rel_name}")
        content_type = _detect_content_type(img_path)
        raw_bytes = img_path.read_bytes()
        digest_prefixed = sha256(raw_bytes)
        if entry.get("image_sha256") != digest_prefixed:
            raise CleanBgJobError(
                f"카메라 {cid!r} 이미지 해시 불일치: 기대={entry.get('image_sha256')!r}, 실제={digest_prefixed!r}"
            )
        width, height = _decode_image_size(raw_bytes, label=f"{cid} ({img_path.name})")
        expected_size = [int(x) for x in cal_cameras[cid]["image_size"]]
        meta_size = entry.get("image_size")
        if list(meta_size or []) != [width, height]:
            raise CleanBgJobError(
                f"카메라 {cid!r} 의 실제 해상도({[width, height]})가 clean_backgrounds.json({meta_size})과 다르다"
            )
        if [width, height] != expected_size:
            raise CleanBgJobError(
                f"카메라 {cid!r} 의 해상도({[width, height]})가 보정 문서({expected_size})와 다르다"
            )
        if "previous_fill_ratio" in entry or "unrevealed_ratio" in entry or "invalid_ratio" in entry:
            has_quality_fields = True
            prev_r = float(entry.get("previous_fill_ratio", 0.0))
            unrev_r = float(entry.get("unrevealed_ratio", entry.get("invalid_ratio", 0.0)))
            cam_qualities[cid] = CameraRegenerateQuality(prev_r, unrev_r).to_dict()
        prepared.append(
            PreparedImage(
                camera_id=cid,
                path=img_path,
                content_type=content_type,
                raw_bytes=raw_bytes,
                sha256_prefixed=digest_prefixed,
                sha256_hex=digest_prefixed.split(":", 1)[1],
                width=width,
                height=height,
                kind=image_kind,
            )
        )

    raw_prov = meta_doc.get("input_provenance")
    if not isinstance(raw_prov, dict) or not isinstance(raw_prov.get("frames"), dict):
        raise CleanBgJobError("clean_backgrounds.json 에 유효한 input_provenance.frames 가 없다")
    frames_prov = dict(raw_prov["frames"])
    frames_prov["camera_ids"] = [img.camera_id for img in prepared]
    preserve_ub = bool(raw_prov.get("is_upper_bound", False))

    regen_quality_doc: dict[str, Any] | None = None
    if isinstance(meta_doc.get("regenerate_quality"), dict):
        regen_quality_doc = dict(meta_doc["regenerate_quality"])
    elif has_quality_fields and cam_qualities:
        regen_quality_doc = {
            "previous_fill_ratio": round(
                float(np.mean([c["previous_fill_ratio"] for c in cam_qualities.values()])),
                6,
            ),
            "unrevealed_ratio": round(
                float(np.mean([c["unrevealed_ratio"] for c in cam_qualities.values()])),
                6,
            ),
            "cameras": cam_qualities,
        }

    return PreparedBundle(
        calibration_version=cal_version,
        images=tuple(prepared),
        frames_provenance=frames_prov,
        preserve_upper_bound=preserve_ub,
        regenerate_quality=regen_quality_doc,
    )


def prepare_from_images(
    image_map: list[tuple[str, Path]],
    cal: dict[str, Any],
    *,
    image_kind: str = "CLEAN_BACKGROUND",
    frame_source: str = "LIVE",
    dataset_id: str | None = None,
    dataset_hash: str | None = None,
) -> PreparedBundle:
    """명시적 카메라 이미지 목록(`--image` 또는 `--images-dir`)을 검증하고 준비한다."""
    if image_kind not in IMAGE_KINDS:
        raise CleanBgJobError(f"지원하지 않는 이미지 kind 다: {image_kind!r}")
    if not (2 <= len(image_map) <= 16):
        raise CleanBgJobError(f"카메라 이미지는 2~16대여야 한다: {len(image_map)}대")
    cal_version = str(cal["calibration_version"])
    cal_cameras = _cameras_by_id(cal)
    seen: set[str] = set()
    prepared: list[PreparedImage] = []
    for cid, img_path in image_map:
        if cid in seen:
            raise CleanBgJobError(f"중복된 camera_id 가 있다: {cid!r}")
        seen.add(cid)
        if cid not in cal_cameras:
            raise CleanBgJobError(f"카메라 {cid!r} 가 보정 문서에 없다")
        if not img_path.is_file():
            raise CleanBgJobError(f"카메라 {cid!r} 이미지 파일이 없다: {img_path}")
        content_type = _detect_content_type(img_path)
        raw_bytes = img_path.read_bytes()
        digest_prefixed = sha256(raw_bytes)
        width, height = _decode_image_size(raw_bytes, label=f"{cid} ({img_path.name})")
        expected_size = [int(x) for x in cal_cameras[cid]["image_size"]]
        if [width, height] != expected_size:
            raise CleanBgJobError(
                f"카메라 {cid!r} 의 해상도({[width, height]})가 보정 문서({expected_size})와 다르다"
            )
        prepared.append(
            PreparedImage(
                camera_id=cid,
                path=img_path.resolve(),
                content_type=content_type,
                raw_bytes=raw_bytes,
                sha256_prefixed=digest_prefixed,
                sha256_hex=digest_prefixed.split(":", 1)[1],
                width=width,
                height=height,
                kind=image_kind,
            )
        )

    frames_prov: dict[str, Any] = {
        "source": frame_source,
        "camera_ids": [img.camera_id for img in prepared],
    }
    if dataset_id:
        frames_prov["dataset_id"] = dataset_id
    if dataset_hash:
        frames_prov["dataset_hash"] = dataset_hash
    return PreparedBundle(
        calibration_version=cal_version,
        images=tuple(prepared),
        frames_provenance=frames_prov,
        preserve_upper_bound=False,
    )


def discover_images_in_dir(images_dir: Path, cal: dict[str, Any]) -> list[tuple[str, Path]]:
    """보정 문서의 카메라 순서대로 디렉터리에서 `<camera_id>.(jpg|jpeg|png)` 파일을 찾는다."""
    if not images_dir.is_dir():
        raise CleanBgJobError(f"이미지 디렉터리가 없다: {images_dir}")
    found: list[tuple[str, Path]] = []
    for cid in _cameras_by_id(cal):
        matches = [
            images_dir / f"{cid}{ext}"
            for ext in (".jpg", ".jpeg", ".png")
            if (images_dir / f"{cid}{ext}").is_file()
        ]
        if len(matches) > 1:
            raise CleanBgJobError(f"카메라 {cid!r} 이미지 파일이 여러 개다: {[m.name for m in matches]}")
        if len(matches) == 1:
            found.append((cid, matches[0]))
    if not found:
        raise CleanBgJobError(f"디렉터리에서 보정 카메라와 일치하는 이미지 파일을 찾지 못했다: {images_dir}")
    return found


def sign_and_upload_object(
    *,
    server_url: str,
    token: str,
    purpose: str,
    content_type: str,
    raw_bytes: bytes,
    sha256_hex: str,
    event_id: str | None = None,
    timeout_s: float = 30.0,
    label: str = "object",
    extra: dict[str, Any] | None = None,
) -> str:
    """`POST /api/uploads/sign` 으로 서명 주소를 받고 `PUT upload_url` 로 바이트를 올린 뒤 `s3://...` 주소를 돌려준다.

    extra 는 서명 요청에 덧붙일 키 식별자(STATIC_RECONSTRUCT_INPUT 의 bundle_id·file_name 등)다.
    """
    base = _check_transport_url(server_url, label="서버 주소")
    if purpose not in UPLOAD_PURPOSES:
        raise CleanBgJobError(f"지원하지 않는 업로드 purpose 다: {purpose!r}")
    if not HEX64_RE.match(sha256_hex):
        raise CleanBgJobError(f"{label} sha256 형식이 올바르지 않다")

    sign_payload: dict[str, Any] = {
        "purpose": purpose,
        "content_type": content_type,
        "size_bytes": len(raw_bytes),
        "sha256": sha256_hex,
    }
    for key, value in (extra or {}).items():
        if value is not None:
            sign_payload[key] = value
    if purpose == "EVENT_FRAME":
        if not event_id or not UUID_RE.match(event_id):
            raise CleanBgJobError("EVENT_FRAME 업로드에는 유효한 UUID event_id 가 필요하다")
        sign_payload["event_id"] = event_id

    opener = build_opener(_NoRedirect)
    sign_url = f"{base}/api/uploads/sign"
    sign_req = Request(
        sign_url,
        data=json.dumps(sign_payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with opener.open(sign_req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
            resp_body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise CleanBgJobError(f"{label} 서명 요청 실패: HTTP {exc.code} ({_sanitize_url(sign_url)})") from None
    except URLError as exc:
        raise CleanBgJobError(f"{label} 서명 서버 연결 실패 ({_sanitize_url(sign_url)}): {exc.reason}") from None

    if status != 200:
        raise CleanBgJobError(f"{label} 서명 요청 실패: HTTP {status}")

    try:
        signed = json.loads(resp_body)
    except Exception as exc:
        raise CleanBgJobError(f"{label} 서명 응답 JSON 파싱 실패: {exc}") from None

    if not isinstance(signed, dict):
        raise CleanBgJobError(f"{label} 서명 응답이 JSON 객체가 아니다")
    object_uri = signed.get("object_uri")
    upload_url = signed.get("upload_url")
    headers = signed.get("headers")
    if not isinstance(object_uri, str) or not OBJECT_URI_RE.match(object_uri):
        raise CleanBgJobError(f"{label} 서명 응답의 object_uri 가 유효한 s3:// 주소가 아니다: {object_uri!r}")
    if not isinstance(upload_url, str) or not upload_url.strip():
        raise CleanBgJobError(f"{label} 서명 응답에 upload_url 이 없다")
    _check_transport_url(upload_url, label="서명 업로드 주소")
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise CleanBgJobError(f"{label} 서명 응답에 유효한 headers 가 없다")

    put_req = Request(upload_url, data=raw_bytes, headers=dict(headers), method="PUT")
    try:
        with opener.open(put_req, timeout=timeout_s) as resp:
            put_status = getattr(resp, "status", 200)
    except HTTPError as exc:
        raise CleanBgJobError(
            f"{label} 객체 업로드(PUT) 실패: HTTP {exc.code} ({_sanitize_url(upload_url)})"
        ) from None
    except URLError as exc:
        raise CleanBgJobError(
            f"{label} 객체 업로드(PUT) 연결 실패 ({_sanitize_url(upload_url)}): {exc.reason}"
        ) from None

    if put_status not in {200, 201, 204}:
        raise CleanBgJobError(f"{label} 객체 업로드(PUT) 실패: HTTP {put_status}")

    return object_uri


def build_job_input(
    *,
    prepared: PreparedBundle,
    cal: dict[str, Any],
    calibration_ref: str,
    calibration_hash: str,
    image_uris: dict[str, str],
    align_views: str = "off",
    plane_cleanup: bool = False,
) -> dict[str, Any]:
    """`-364` `static_reconstruct` 핸들러가 받는 작업 입력 사전을 조립하고 계약 검사를 수행한다."""
    cal_match = OBJECT_URI_RE.match(calibration_ref)
    if not cal_match:
        raise CleanBgJobError(f"calibration_ref 가 유효한 s3:// 주소가 아니다: {calibration_ref!r}")
    cal_bucket = cal_match.group("bucket")
    if not SHA256_RE.match(calibration_hash):
        raise CleanBgJobError(f"calibration_hash 형식이 올바르지 않다: {calibration_hash!r}")

    cal_cameras = _cameras_by_id(cal)
    images_list: list[dict[str, str]] = []
    for img in prepared.images:
        if img.camera_id not in cal_cameras:
            raise CleanBgJobError(f"카메라 {img.camera_id!r} 가 보정 문서에 없다")
        uri = image_uris.get(img.camera_id)
        uri_match = OBJECT_URI_RE.match(uri or "")
        if not uri or not uri_match:
            raise CleanBgJobError(f"카메라 {img.camera_id!r} 의 업로드 주소가 유효하지 않다: {uri!r}")
        if uri_match.group("bucket") != cal_bucket:
            raise CleanBgJobError(
                f"카메라 {img.camera_id!r} 의 업로드 버킷({uri_match.group('bucket')!r})이 "
                f"calibration_ref 버킷({cal_bucket!r})과 다르다"
            )
        images_list.append(
            {
                "camera_id": img.camera_id,
                "uri": uri,
                "kind": img.kind,
            }
        )

    cal_prov = {
        "source": str(cal["provider_source"]),
        "calibration_version": str(cal["calibration_version"]),
    }
    input_provenance = build_input_provenance(prepared.frames_provenance, cal_prov)
    if prepared.preserve_upper_bound:
        input_provenance["is_upper_bound"] = True

    job_input: dict[str, Any] = {
        "calibration_version": prepared.calibration_version,
        "calibration_ref": calibration_ref,
        "calibration_hash": calibration_hash,
        "images": images_list,
        "input_provenance": input_provenance,
        "options": {
            "align_views": align_views,
            "plane_cleanup": bool(plane_cleanup),
        },
    }
    if prepared.regenerate_quality is not None:
        job_input["regenerate_quality"] = prepared.regenerate_quality

    try:
        validate_input(job_input)
    except ReconstructionError as exc:
        raise CleanBgJobError(f"static_reconstruct 입력 검증 실패 ({exc.code}): {exc}") from exc

    # I5 job_request 봉투 스키마까지 함께 통과하는지 확인한다
    envelope = {
        "msg_type": "job_request",
        "msg_id": str(uuid.uuid4()),
        "schema_version": "2.0.0",
        "sent_at": 1_760_000_000_000,
        "source_id": "ec2",
        "payload": {
            "job_id": str(uuid.uuid4()),
            "lease_id": str(uuid.uuid4()),
            "job_type": "static_reconstruct",
            "priority": "BACKGROUND",
            "attempt": 1,
            "max_attempts": DEFAULT_MAX_ATTEMPTS,
            "leased_at": 1_760_000_000_000,
            "lease_expires_at": 1_760_000_060_000,
            "heartbeat_interval_ms": 15_000,
            "timeout_ms": DEFAULT_TIMEOUT_MS,
            "input": job_input,
        },
    }
    if errors := schema_errors(envelope, "job_request"):
        raise CleanBgJobError(f"job_request 계약 위반: {'; '.join(errors)}")

    return job_input


def format_operator_guide(
    *,
    output_path: Path,
    calibration_version: str,
    camera_ids: list[str],
    site_id: str | None,
    dry_run: bool,
) -> str:
    """작업 입력 생성 뒤 운영자가 EC2 에서 실행할 명령 안내 문자열을 만든다."""
    site_placeholder = site_id or "<site_id>"
    mode_label = "DRY-RUN (업로드 생략)" if dry_run else "UPLOADED (객체 저장소 업로드 완료)"
    lines = [
        f"[clean_bg_job] 모드: {mode_label}",
        f"[clean_bg_job] 보정 버전: {calibration_version} (카메라 {len(camera_ids)}대: {', '.join(camera_ids)})",
        f"[clean_bg_job] 작업 입력 파일: {output_path}",
        "",
        "주의: LIVE 감시 중(tier0.live 신선)에는 Worker 가 static_reconstruct 작업을 거절(LIVE_EXP_BLOCKED)하므로 LIVE 감시를 멈춘 창에서 실행하세요.",
        "",
        "1) EC2 에 static_reconstruct 작업 넣기:",
        (
            "docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml "
            f"run --rm -T server admin job enqueue --type static_reconstruct --input-file {output_path} "
            f"--timeout-ms {DEFAULT_TIMEOUT_MS} --max-attempts {DEFAULT_MAX_ATTEMPTS}"
        ),
        "",
        "2) 작업 완료 뒤 CANDIDATE 장면 버전 확인:",
        (
            "docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml "
            f"run --rm -T server admin scene list --site {site_placeholder}"
        ),
        "",
        "3) 시각 산출물 확인 뒤 장면 활성화(CANDIDATE -> COMMITTED):",
        (
            "docker compose --env-file /opt/simai/.env -f deploy/ec2/compose.yaml "
            f"run --rm -T server admin scene activate --site {site_placeholder} "
            '--version <scene_version> --actor <관리자> --reason "<사유>"'
        ),
    ]
    return "\n".join(lines)


def _resolve_token_and_purpose(
    *,
    token_file: Path | None,
    purpose_arg: str | None,
    env: dict[str, str],
    upload_calibration: bool = False,
) -> tuple[str, str]:
    """토큰 파일 또는 환경변수에서 인증 토큰을 읽고 서버 UploadPurpose 규칙에 맞게 purpose 를 결정한다."""
    if purpose_arg is not None and purpose_arg not in UPLOAD_PURPOSES:
        raise CleanBgJobError(
            f"지원하지 않는 업로드 purpose 다: {purpose_arg!r} "
            "(사진은 EVENT_FRAME[Tier 0·DEVICE 토큰] 또는 STATIC_RECONSTRUCT_INPUT[워커 토큰]만 허용)"
        )
    if upload_calibration and purpose_arg is not None and purpose_arg != WORKER_PURPOSE:
        raise CleanBgJobError(
            "--upload-calibration 은 보정 문서(application/json)가 STATIC_RECONSTRUCT_INPUT(워커 토큰)만 허용하므로 "
            f"--purpose {purpose_arg} 와 함께 쓸 수 없다(워커 토큰·STATIC_RECONSTRUCT_INPUT 사용 필요)"
        )

    if token_file is not None:
        if not token_file.is_file():
            raise CleanBgJobError(f"토큰 파일이 없다: {token_file}")
        token = token_file.read_text(encoding="utf-8").strip()
        if not token:
            raise CleanBgJobError(f"토큰 파일이 비어 있다: {token_file}")
        return token, (purpose_arg or (WORKER_PURPOSE if upload_calibration else "EVENT_FRAME"))

    upload_tok = (env.get("SIMAI_UPLOAD_TOKEN") or "").strip()
    tier0_tok = (env.get("SIMAI_TIER0_TOKEN") or "").strip()
    device_tok = (env.get("SIMAI_DEVICE_TOKEN") or "").strip()
    worker_tok = (env.get("SIMAI_WORKER_TOKEN") or "").strip()

    if upload_calibration or purpose_arg == WORKER_PURPOSE:
        if worker_tok:
            return worker_tok, WORKER_PURPOSE
        if upload_tok:
            return upload_tok, WORKER_PURPOSE
        if tier0_tok or device_tok:
            raise CleanBgJobError(
                "토큰 종류와 업로드 용도(STATIC_RECONSTRUCT_INPUT)가 맞지 않는다: "
                "보정 문서(application/json)와 STATIC_RECONSTRUCT_INPUT 사진 업로드는 워커 토큰(SIMAI_WORKER_TOKEN)만 허용하며 "
                "SIMAI_TIER0_TOKEN·SIMAI_DEVICE_TOKEN 은 EVENT_FRAME(사진 전용)만 허용한다"
            )
    elif purpose_arg == "EVENT_FRAME":
        if upload_tok:
            return upload_tok, "EVENT_FRAME"
        if tier0_tok:
            return tier0_tok, "EVENT_FRAME"
        if device_tok:
            return device_tok, "EVENT_FRAME"
        if worker_tok:
            raise CleanBgJobError(
                "토큰 종류와 업로드 용도(EVENT_FRAME)가 맞지 않는다: "
                "EVENT_FRAME 업로드는 Tier 0·DEVICE 토큰(SIMAI_TIER0_TOKEN / SIMAI_DEVICE_TOKEN)만 허용하며 "
                "워커 토큰(SIMAI_WORKER_TOKEN)은 STATIC_RECONSTRUCT_INPUT 만 허용한다"
            )
    else:
        if upload_tok:
            return upload_tok, "EVENT_FRAME"
        if tier0_tok:
            return tier0_tok, "EVENT_FRAME"
        if device_tok:
            return device_tok, "EVENT_FRAME"
        if worker_tok:
            return worker_tok, WORKER_PURPOSE

    raise CleanBgJobError(
        "업로드 인증 토큰이 없다(--token-file 또는 SIMAI_UPLOAD_TOKEN / SIMAI_TIER0_TOKEN / "
        "SIMAI_DEVICE_TOKEN / SIMAI_WORKER_TOKEN 환경변수 필요)"
    )


def check_calibration_registered(
    *,
    server_url: str,
    token: str,
    calibration_version: str,
    env: dict[str, str],
    timeout_s: float = 30.0,
) -> None:
    """EC2 `GET /api/calibrations/current` 로 `calibration_version` 이 서버에 등록돼 있는지 확인한다."""
    base = _check_transport_url(server_url, label="서버 주소")
    opener = build_opener(_NoRedirect)
    auth_token = token

    admin_user = (env.get("SIMAI_ADMIN_USER") or "").strip()
    admin_password = env.get("SIMAI_ADMIN_PASSWORD") or ""
    if admin_user and admin_password:
        login_url = f"{base}/api/auth/login"
        login_req = Request(
            login_url,
            data=json.dumps({"username": admin_user, "password": admin_password}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with opener.open(login_req, timeout=timeout_s) as resp:
                login_body = json.loads(resp.read().decode("utf-8", errors="replace"))
            if (
                isinstance(login_body, dict)
                and isinstance(login_body.get("accessToken"), str)
                and login_body["accessToken"].strip()
            ):
                auth_token = login_body["accessToken"].strip()
        except Exception:
            pass

    check_url = f"{base}/api/calibrations/current"
    req = Request(
        check_url,
        headers={
            "Authorization": f"Bearer {auth_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    unregistered_msg = (
        "EC2 에 등록된 보정만 쓸 수 있다(SceneJobListener 가 결과를 거절). "
        f"등록된 보정 문서를 쓰거나 simai_ai.calibration.publish 로 먼저 올린다 (calibration_version={calibration_version})"
    )
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            status = getattr(resp, "status", 200)
            resp_body = resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        if exc.code == 404:
            raise CleanBgJobError(unregistered_msg) from None
        if exc.code in {401, 403}:
            print(
                "[clean_bg_job] 경고: GET /api/calibrations/current 는 사용자 JWT 가 필요해 기계 토큰으로 "
                "EC2 보정 등록 여부를 확인하지 못했다(--skip-calibration-check 또는 SIMAI_ADMIN_USER·SIMAI_ADMIN_PASSWORD 사용 가능). "
                "EC2 에 등록된 보정만 쓸 수 있다(SceneJobListener 가 결과를 거절). "
                "등록된 보정 문서를 쓰거나 simai_ai.calibration.publish 로 먼저 올린다",
                file=sys.stderr,
            )
            return
        raise CleanBgJobError(f"EC2 보정 등록 확인 실패: HTTP {exc.code} ({_sanitize_url(check_url)})") from None
    except URLError as exc:
        raise CleanBgJobError(f"EC2 보정 등록 확인 서버 연결 실패 ({_sanitize_url(check_url)}): {exc.reason}") from None

    if status != 200:
        raise CleanBgJobError(f"EC2 보정 등록 확인 실패: HTTP {status}")

    try:
        current = json.loads(resp_body)
    except Exception as exc:
        raise CleanBgJobError(f"EC2 보정 등록 확인 응답 JSON 파싱 실패: {exc}") from None

    if not isinstance(current, dict):
        raise CleanBgJobError("EC2 보정 등록 확인 응답이 JSON 객체가 아니다")

    known_versions: set[str] = set()
    for field in ("calibration_version", "latest_uploaded_version", "previous_calibration_version"):
        val = current.get(field)
        if isinstance(val, str) and val.strip():
            known_versions.add(val.strip())

    # 서버에는 버전별 조회가 없고 current 는 현재·최근·직전 버전만 준다. 등록돼 있지만 현재가 아닌 보정(9차 266z)을
    # 막지 않도록 여기서는 경고만 한다(등록되지 않았다면 SceneJobListener 가 결과를 거절한다).
    if calibration_version not in known_versions:
        print(
            "[clean_bg_job] 경고: EC2 의 현재·최근 보정이 아니다(" + ", ".join(sorted(known_versions)) + "). "
            + unregistered_msg,
            file=sys.stderr,
        )


def _parse_image_arg(raw: str) -> tuple[str, Path]:
    if "=" not in raw:
        raise CleanBgJobError(f"--image 인자는 <camera_id>=<파일경로> 형식이어야 한다: {raw!r}")
    cid, path_str = raw.split("=", 1)
    cid = cid.strip()
    path_str = path_str.strip()
    if not cid or not path_str:
        raise CleanBgJobError(f"--image 인자가 비어 있다: {raw!r}")
    return cid, Path(path_str)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m simai_ai.scene.clean_bg_job",
        description="Pod 의 사람 지운 배경 묶음(-363)을 올리고 static_reconstruct(-364) 작업 입력 JSON 을 만든다.",
    )
    src_group = parser.add_mutually_exclusive_group(required=True)
    src_group.add_argument(
        "--bundle",
        type=Path,
        help="Tier 0 가 만든 clean-background/ 루트 폴더(status.json 포함) 또는 bundle-<uuid>/ 폴더",
    )
    src_group.add_argument(
        "--images-dir",
        type=Path,
        help="보정 카메라 이름(<camera_id>.jpg/.png)의 배경 이미지가 든 디렉터리",
    )
    src_group.add_argument(
        "--image",
        action="append",
        dest="images",
        metavar="CAMERA_ID=PATH",
        help="개별 카메라 이미지 지정(2~16회 반복, 예: --image cam-1=/path/cam-1.jpg)",
    )

    parser.add_argument("--calibration", type=Path, required=True, help="활성 보정 문서(calibration.json) 경로")
    parser.add_argument(
        "--calibration-ref",
        default=None,
        help="객체 저장소에 올라간 보정 문서 주소(s3://<bucket>/<key>). --upload-calibration 또는 --dry-run 이 아니면 필수",
    )
    parser.add_argument(
        "--upload-calibration",
        action="store_true",
        help="보정 문서도 서명 업로드(STATIC_RECONSTRUCT_INPUT application/json)로 함께 올리고 그 s3:// 주소를 쓴다",
    )
    parser.add_argument("--output", type=Path, required=True, help="생성할 static_reconstruct 작업 입력 JSON 경로")
    parser.add_argument(
        "--server",
        default=None,
        help="EC2 서버 기본 주소(예: https://simai.example.com). 생략 시 SIMAI_EC2_BASE_URL 환경변수 사용",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=None,
        help="Bearer 토큰이 적힌 파일 경로(생략 시 SIMAI_UPLOAD_TOKEN / SIMAI_TIER0_TOKEN 등 환경변수 사용)",
    )
    parser.add_argument(
        "--purpose",
        choices=UPLOAD_PURPOSES,
        default=None,
        help="POST /api/uploads/sign 의 purpose (기본: 토큰 종류에 따라 EVENT_FRAME 또는 STATIC_RECONSTRUCT_INPUT)",
    )
    parser.add_argument(
        "--event-id",
        default=None,
        help="EVENT_FRAME 업로드 시 묶음 식별에 쓸 UUID (생략 시 새 UUID 자동 생성)",
    )
    parser.add_argument(
        "--image-kind",
        choices=sorted(IMAGE_KINDS),
        default="CLEAN_BACKGROUND",
        help="작업 입력 images[].kind 값 (기본: CLEAN_BACKGROUND)",
    )
    parser.add_argument(
        "--frame-source",
        choices=("LIVE", "FILE", "SIM", "DATASET"),
        default="LIVE",
        help="--image / --images-dir 사용 시 input_provenance.frames.source (기본: LIVE)",
    )
    parser.add_argument("--dataset-id", default=None, help="--image / --images-dir 사용 시 dataset_id")
    parser.add_argument("--dataset-hash", default=None, help="--image / --images-dir 사용 시 dataset_hash (sha256:...)")
    parser.add_argument(
        "--align-views",
        choices=("off", "scale", "sim3"),
        default="off",
        help="static_reconstruct options.align_views (기본: off)",
    )
    parser.add_argument(
        "--plane-cleanup",
        action="store_true",
        help="static_reconstruct options.plane_cleanup 활성화 (기본: false)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="네트워크 업로드 없이 로컬 검증과 작업 입력 JSON 생성·명령 출력만 수행한다",
    )
    parser.add_argument(
        "--dry-run-bucket",
        default=DEFAULT_DRY_RUN_BUCKET,
        help=f"--dry-run 시 s3:// 주소에 쓸 버킷 이름 (기본: {DEFAULT_DRY_RUN_BUCKET})",
    )
    parser.add_argument(
        "--skip-calibration-check",
        action="store_true",
        help="EC2 에 calibration_version 이 등록돼 있는지 확인하는 단계를 건너뛴다(경고만 출력)",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP 요청 타임아웃 초 (기본: 30)")
    return parser


def run_cli(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    """CLI 진입점. 성공 시 0, 실패 시 2를 돌려준다."""
    runtime_env = dict(os.environ if env is None else env)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        cal, cal_hash, cal_bytes = load_calibration(args.calibration)
        cal_version = str(cal["calibration_version"])

        if args.bundle is not None:
            prepared = prepare_from_bundle(args.bundle, cal, image_kind=args.image_kind)
        elif args.images_dir is not None:
            pairs = discover_images_in_dir(args.images_dir, cal)
            prepared = prepare_from_images(
                pairs,
                cal,
                image_kind=args.image_kind,
                frame_source=args.frame_source,
                dataset_id=args.dataset_id,
                dataset_hash=args.dataset_hash,
            )
        else:
            pairs = [_parse_image_arg(item) for item in (args.images or [])]
            prepared = prepare_from_images(
                pairs,
                cal,
                image_kind=args.image_kind,
                frame_source=args.frame_source,
                dataset_id=args.dataset_id,
                dataset_hash=args.dataset_hash,
            )

        session_id = (args.event_id or str(uuid.uuid4())).strip().lower()
        if not UUID_RE.match(session_id):
            raise CleanBgJobError(f"--event-id 는 소문자 UUID 형식이어야 한다: {args.event_id!r}")

        image_uris: dict[str, str] = {}
        calibration_ref: str | None = args.calibration_ref

        if args.dry_run:
            bucket = args.dry_run_bucket
            if calibration_ref:
                m = OBJECT_URI_RE.match(calibration_ref)
                if not m:
                    raise CleanBgJobError(f"--calibration-ref 가 유효한 s3:// 주소가 아니다: {calibration_ref!r}")
                bucket = m.group("bucket")
            else:
                calibration_ref = f"s3://{bucket}/calibrations/{cal_version}/calibration.json"
            for img in prepared.images:
                ext = img.path.suffix.lower()
                image_uris[img.camera_id] = (
                    f"s3://{bucket}/clean-backgrounds/{cal_version}/{session_id}/{img.camera_id}{ext}"
                )
        else:
            if not args.upload_calibration:
                if not calibration_ref:
                    raise CleanBgJobError(
                        "실제 업로드 시에는 --calibration-ref s3://<bucket>/<key> 또는 --upload-calibration 이 필요하다"
                    )
                if not OBJECT_URI_RE.match(calibration_ref):
                    raise CleanBgJobError(f"--calibration-ref 가 유효한 s3:// 주소가 아니다: {calibration_ref!r}")
            # 업로드 전에 로컬 계약(출처·옵션·보정 일치)을 먼저 검증해 불필요한 네트워크 호출을 막는다
            preflight_ref = calibration_ref or f"s3://{DEFAULT_DRY_RUN_BUCKET}/calibrations/{cal_version}/calibration.json"
            preflight_bucket = OBJECT_URI_RE.match(preflight_ref).group("bucket")  # type: ignore[union-attr]
            build_job_input(
                prepared=prepared,
                cal=cal,
                calibration_ref=preflight_ref,
                calibration_hash=cal_hash,
                image_uris={
                    img.camera_id: f"s3://{preflight_bucket}/preflight/{img.camera_id}.jpg" for img in prepared.images
                },
                align_views=args.align_views,
                plane_cleanup=args.plane_cleanup,
            )

            server_url = (args.server or runtime_env.get("SIMAI_EC2_BASE_URL") or "").strip()
            if not server_url:
                raise CleanBgJobError("--server 또는 SIMAI_EC2_BASE_URL 환경변수가 필요하다")
            token, purpose = _resolve_token_and_purpose(
                token_file=args.token_file,
                purpose_arg=args.purpose,
                env=runtime_env,
                upload_calibration=args.upload_calibration,
            )
            if args.skip_calibration_check:
                print(
                    "[clean_bg_job] 경고: --skip-calibration-check 로 EC2 보정 등록 확인을 건너뛴다. "
                    "EC2 에 등록된 보정만 쓸 수 있다(SceneJobListener 가 결과를 거절). "
                    "등록된 보정 문서를 쓰거나 simai_ai.calibration.publish 로 먼저 올린다",
                    file=sys.stderr,
                )
            else:
                check_calibration_registered(
                    server_url=server_url,
                    token=token,
                    calibration_version=cal_version,
                    env=runtime_env,
                    timeout_s=args.timeout,
                )

            if args.upload_calibration:
                cal_hex = hashlib.sha256(cal_bytes).hexdigest()
                calibration_ref = sign_and_upload_object(
                    server_url=server_url,
                    token=token,
                    purpose=WORKER_PURPOSE,
                    content_type="application/json",
                    raw_bytes=cal_bytes,
                    sha256_hex=cal_hex,
                    timeout_s=args.timeout,
                    label="calibration.json",
                )

            for img in prepared.images:
                image_uris[img.camera_id] = sign_and_upload_object(
                    server_url=server_url,
                    token=token,
                    purpose=purpose,
                    content_type=img.content_type,
                    raw_bytes=img.raw_bytes,
                    sha256_hex=img.sha256_hex,
                    event_id=session_id if purpose == "EVENT_FRAME" else None,
                    timeout_s=args.timeout,
                    label=f"camera {img.camera_id}",
                )

        job_input = build_job_input(
            prepared=prepared,
            cal=cal,
            calibration_ref=calibration_ref,
            calibration_hash=cal_hash,
            image_uris=image_uris,
            align_views=args.align_views,
            plane_cleanup=args.plane_cleanup,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json(args.output, job_input)

        site_id = (cal.get("coordinate_frame") or {}).get("site_id")
        guide = format_operator_guide(
            output_path=args.output,
            calibration_version=cal_version,
            camera_ids=[img.camera_id for img in prepared.images],
            site_id=site_id if isinstance(site_id, str) and site_id else None,
            dry_run=args.dry_run,
        )
        print(guide)
        return 0
    except CleanBgJobError as exc:
        print(f"[clean_bg_job] 오류: {exc}", file=sys.stderr)
        return 2


def main() -> None:
    raise SystemExit(run_cli())


if __name__ == "__main__":
    main()
