"""방 외곽 넓히기·볼록 껍질과 BEV 저장(PNG + JSON) 시험."""

from __future__ import annotations

import json
import struct
import zlib

import numpy as np
import pytest

from simai_ai.scene.bev import BevConfig, BevRaster, generate_bev
from simai_ai.scene.bev_export import encode_png, save_floorplan
from simai_ai.scene.room_outline import convex_hull, convex_outline_from_mask, expand_outline


def _decode_png(data: bytes) -> tuple[int, int, int, np.ndarray]:
    """시험용 최소 PNG 해독(필터 0, 8비트). (width, height, color_type, 픽셀 배열)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, header = 8, b"", None
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    w, h, depth, color, *_ = header
    assert depth == 8
    channels = {0: 1, 4: 2, 2: 3, 6: 4}[color]
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(h, 1 + w * channels)
    assert (raw[:, 0] == 0).all()
    return w, h, color, raw[:, 1:].reshape(h, w, channels)


def test_convex_hull_ccw_without_interior_points():
    pts = np.array([[0, 0], [2, 0], [2, 1], [0, 1], [1, 0.5], [1, 0]], dtype=float)
    hull = convex_hull(pts)
    assert {tuple(p) for p in hull} == {(0, 0), (2, 0), (2, 1), (0, 1)}
    area2 = np.dot(hull[:, 0], np.roll(hull[:, 1], -1)) - np.dot(hull[:, 1], np.roll(hull[:, 0], -1))
    assert area2 > 0  # 반시계


def test_outline_from_mask_covers_true_pixels():
    raster = BevRaster(min_x=0.0, max_y=1.0, width=10, height=10, meters_per_pixel=0.1)
    mask = np.zeros((10, 10), dtype=bool)
    mask[2:5, 3:8] = True  # 행 2~4 → y 0.5~0.8, 열 3~7 → x 0.3~0.8
    outline = convex_outline_from_mask(mask, raster, basis="CAMERA_FOOTPRINT")
    poly = np.array(outline["polygon_xy"])
    np.testing.assert_allclose(poly.min(axis=0), [0.3, 0.5])
    np.testing.assert_allclose(poly.max(axis=0), [0.8, 0.8])
    assert outline["source"] == "inferred"
    with pytest.raises(ValueError):
        convex_outline_from_mask(mask, raster, basis="SURVEY")
    with pytest.raises(ValueError):
        convex_outline_from_mask(np.zeros((10, 10), dtype=bool), raster, basis="FLOOR_MASK")


def test_expand_outline_is_at_least_margin_outside():
    square = [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0], [0.0, 3.0]]
    grown = np.array(expand_outline(square, 0.5))
    # 모든 변이 적어도 0.5 m 바깥으로 나갔다(외접 근사라 조금 더 넓을 수 있다)
    assert grown[:, 0].min() <= -0.5 + 1e-9 and grown[:, 0].max() >= 4.5 - 1e-9
    assert grown[:, 1].min() <= -0.5 + 1e-9 and grown[:, 1].max() >= 3.5 - 1e-9
    # 원래 꼭짓점에서 넓힌 다각형의 각 변까지 거리가 margin 이상
    for i in range(len(grown)):
        a, b = grown[i], grown[(i + 1) % len(grown)]
        edge = b - a
        normal = np.array([edge[1], -edge[0]]) / np.linalg.norm(edge)  # 반시계 다각형의 바깥 법선
        dist = (np.array(square) - a) @ normal
        assert (dist <= -0.5 + 1e-9).all()
    np.testing.assert_allclose(sorted(map(tuple, expand_outline(square, 0.0))), sorted(map(tuple, square)))
    with pytest.raises(ValueError):
        expand_outline(square, -0.1)


def test_encode_png_roundtrip():
    img = (np.arange(4 * 5 * 3) % 256).astype(np.uint8).reshape(4, 5, 3)
    w, h, color, pixels = _decode_png(encode_png(img))
    assert (w, h, color) == (5, 4, 2)
    np.testing.assert_array_equal(pixels, img)
    with pytest.raises(ValueError):
        encode_png(img.astype(np.float32))


def test_save_floorplan_writes_png_and_json_without_overwrite(scene_helpers, sim_calibration, checker_images, tmp_path):
    h = scene_helpers
    config = BevConfig(world_bounds=((-0.5, -0.5), (9.5, 8.0)), meters_per_pixel=0.05, allowed_states=("PROVISIONAL",))
    plan = generate_bev(
        h.DictCalibrationProvider(sim_calibration),
        h.make_frames(sim_calibration, checker_images),
        frame_source="SIM",
        created_at_ms=1_760_342_500_000,
        config=config,
    )
    png_path, json_path = save_floorplan(plan, tmp_path)
    w, h_px, color, pixels = _decode_png(png_path.read_bytes())
    assert (w, h_px, color) == (plan.raster.width, plan.raster.height, 6)
    np.testing.assert_array_equal(pixels[..., :3], plan.image[..., ::-1])  # BGR → RGB
    np.testing.assert_array_equal(pixels[..., 3] == 255, plan.coverage)
    meta = json.loads(json_path.read_text(encoding="utf-8"))
    assert meta["calibration_version"] == sim_calibration["calibration_version"]
    assert meta["image_file"] == png_path.name and meta["alpha"] == "COVERAGE"
    assert meta["raster"] == plan.metadata["raster"]
    with pytest.raises(FileExistsError):
        save_floorplan(plan, tmp_path)
