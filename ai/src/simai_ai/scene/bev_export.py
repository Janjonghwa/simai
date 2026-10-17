"""BEV 평면도 저장: PNG(뷰어 바닥 이미지·2D 개요) + JSON(World 대응 정보) (명세 13.1).

표준 라이브러리 zlib 로 8비트 PNG 를 쓴다(opencv·Pillow 없이). 알파 채널은 카메라가 바닥을 본 픽셀(coverage, 정적 3D 층으로 채운 픽셀 포함)이다.
저장은 새 파일로만 한다(이미 있으면 오류). 덮어쓰지 않는다.
"""

from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np

from simai_ai.scene.bev import BevFloorPlan

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# 채널 수 → PNG 색 형식 (0 회색, 4 회색+알파, 2 RGB, 6 RGBA)
_COLOR_TYPES = {1: 0, 2: 4, 3: 2, 4: 6}


def encode_png(image: np.ndarray) -> bytes:
    """uint8 (H, W) 또는 (H, W, 1~4) 배열 → PNG 바이트. 채널 순서는 회색/RGB(A) 로 본다."""
    img = np.asarray(image)
    if img.dtype != np.uint8:
        raise ValueError(f"PNG 는 uint8 영상만 쓴다: {img.dtype}")
    if img.ndim == 2:
        img = img[:, :, None]
    if img.ndim != 3 or img.shape[2] not in _COLOR_TYPES:
        raise ValueError(f"(H, W) 또는 (H, W, 1~4) 영상이어야 한다: {img.shape}")
    h, w, c = img.shape
    raw = np.concatenate([np.zeros((h, 1), dtype=np.uint8), img.reshape(h, w * c)], axis=1).tobytes()
    header = struct.pack(">IIBBBBB", w, h, 8, _COLOR_TYPES[c], 0, 0, 0)
    return _PNG_SIGNATURE + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(raw, 6)) + _chunk(b"IEND", b"")


def save_floorplan(
    plan: BevFloorPlan, directory: str | Path, *, stem: str = "bev_floorplan", channel_order: str = "BGR"
) -> tuple[Path, Path]:
    """`<stem>.png`(알파 = coverage)와 `<stem>.json`(메타데이터)을 쓴다. 둘 중 하나라도 있으면 쓰지 않는다.

    channel_order: 3채널 영상의 순서. 카메라 프레임(OpenCV)은 BGR 이 기본이며 PNG 에는 RGB 로 쓴다.
    """
    if channel_order not in ("BGR", "RGB"):
        raise ValueError(f"channel_order 는 BGR 또는 RGB: {channel_order}")
    directory = Path(directory)
    png_path, json_path = directory / f"{stem}.png", directory / f"{stem}.json"
    for path in (png_path, json_path):
        if path.exists():
            raise FileExistsError(f"덮어쓰지 않는다: {path}")
    image = plan.image
    if image.dtype != np.uint8:
        raise ValueError(f"uint8 BEV 만 PNG 로 쓴다: {image.dtype}")
    if image.shape[2] == 3 and channel_order == "BGR":
        image = image[:, :, ::-1]
    if image.shape[2] not in (1, 3):
        raise ValueError(f"1 또는 3 채널 BEV 만 PNG 로 쓴다: {image.shape[2]}")
    visible = plan.coverage if plan.static_filled is None else plan.coverage | plan.static_filled
    alpha = np.where(visible, 255, 0).astype(np.uint8)[:, :, None]
    data = encode_png(np.concatenate([image, alpha], axis=2))
    directory.mkdir(parents=True, exist_ok=True)
    alpha_meaning = "COVERAGE" if plan.static_filled is None else "COVERAGE_OR_STATIC_FILL"
    meta = dict(plan.metadata, image_file=png_path.name, alpha=alpha_meaning)
    with open(png_path, "xb") as f:
        f.write(data)
    with open(json_path, "x", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    return png_path, json_path


def _chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
