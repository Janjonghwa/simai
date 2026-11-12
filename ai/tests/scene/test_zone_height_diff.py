"""세팅 구역별 상면 높이 차이 판정(`simai_ai.scene.zone_height_diff`) 단위 시험 (S15P31A101-630)."""

from __future__ import annotations

import json
import struct
from typing import Any

import numpy as np

from simai_ai.providers.contract import schema_errors
from simai_ai.scene.preview_scene import encode_vertex_color_glb
from simai_ai.scene.texture_mesh import GLTF_Y_UP, to_glb_points
from simai_ai.scene.zone_height_diff import (
    REASON_INSUFFICIENT_COVERAGE,
    REASON_SCENE_UNREADABLE,
    REASON_WORLD_NOT_ANCHORED,
    STATUS_CHANGE_SUSPECTED,
    STATUS_UNCHANGED,
    STATUS_UNKNOWN,
    ZoneHeightDiffConfig,
    build_unknown_zone_height_changes,
    evaluate_zone_height_changes,
)

ZONE_HEIGHT_CHANGES_SCHEMA = {
    "$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/zone_height_changes"
}


def _make_grid_glb(
    x_range: tuple[float, float],
    y_range: tuple[float, float],
    *,
    step_m: float = 0.05,
    z_fn: Any = None,
    extra_tris_world: tuple[np.ndarray, np.ndarray] | None = None,
) -> bytes:
    """World XY 사각 격자 정점·삼각형으로 표준 glTF Y-up GLB 바이트를 만든다."""
    xs = np.arange(x_range[0], x_range[1] + 1e-9, step_m, dtype=np.float64)
    ys = np.arange(y_range[0], y_range[1] + 1e-9, step_m, dtype=np.float64)
    gx, gy = np.meshgrid(xs, ys)
    gz = np.zeros_like(gx) if z_fn is None else z_fn(gx, gy)
    pts_world = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)

    ny, nx = gx.shape
    tris: list[list[int]] = []
    for r in range(ny - 1):
        for c in range(nx - 1):
            i0 = r * nx + c
            i1 = i0 + 1
            i2 = i0 + nx
            i3 = i2 + 1
            tris.append([i0, i1, i3])
            tris.append([i0, i3, i2])
    faces = np.asarray(tris, dtype=np.int64)

    if extra_tris_world is not None:
        extra_v, extra_f = extra_tris_world
        offset = len(pts_world)
        pts_world = np.concatenate([pts_world, extra_v], axis=0)
        faces = np.concatenate([faces, extra_f + offset], axis=0)

    v_glb = to_glb_points(np.eye(4, dtype=np.float64), pts_world)
    colors = np.full((len(v_glb), 3), 180, dtype=np.uint8)
    return encode_vertex_color_glb(
        v_glb,
        colors,
        faces,
        extras_simai={"axis_convention": "GLTF_Y_UP", "geometry_role": "VISUAL", "safety_use": False},
    )


def _append_estimated_floor_node(glb_bytes: bytes, rect: tuple[float, float, float, float], z: float = 0.0) -> bytes:
    """`glb_enhance` 가 덧붙이는 `estimated_floor_plane`(`surface_source: ESTIMATED_PLANE`) 노드를 모사한다."""
    off = 12
    length, _ = struct.unpack_from("<I4s", glb_bytes, off)
    doc = json.loads(glb_bytes[off + 8 : off + 8 + length].decode("utf-8"))
    off2 = off + 8 + length
    bin_len, _ = struct.unpack_from("<I4s", glb_bytes, off2)
    blob = bytearray(glb_bytes[off2 + 8 : off2 + 8 + bin_len])

    x0, y0, x1, y1 = rect
    pos_world = np.array([[x0, y0, z], [x1, y0, z], [x1, y1, z], [x0, y1, z]], dtype=np.float64)
    pos_glb = (pos_world @ GLTF_Y_UP.T).astype("<f4")
    idx = np.array([0, 1, 2, 0, 2, 3], dtype="<u2")

    blob += b"\x00" * (-len(blob) % 4)
    pos_off = len(blob)
    pos_bytes = pos_glb.tobytes()
    blob += pos_bytes
    idx_off = len(blob)
    idx_bytes = idx.tobytes()
    blob += idx_bytes
    blob += b"\x00" * (-len(blob) % 4)

    doc["bufferViews"].append({"buffer": 0, "byteOffset": pos_off, "byteLength": len(pos_bytes), "target": 34962})
    pos_view = len(doc["bufferViews"]) - 1
    doc["bufferViews"].append({"buffer": 0, "byteOffset": idx_off, "byteLength": len(idx_bytes), "target": 34963})
    idx_view = len(doc["bufferViews"]) - 1

    doc["accessors"].append(
        {
            "bufferView": pos_view,
            "byteOffset": 0,
            "componentType": 5126,
            "count": 4,
            "type": "VEC3",
            "min": [float(v) for v in pos_glb.min(axis=0)],
            "max": [float(v) for v in pos_glb.max(axis=0)],
        }
    )
    pos_acc = len(doc["accessors"]) - 1
    doc["accessors"].append(
        {"bufferView": idx_view, "byteOffset": 0, "componentType": 5123, "count": 6, "type": "SCALAR"}
    )
    idx_acc = len(doc["accessors"]) - 1

    doc["materials"].append({"name": "estimated_floor_plane", "doubleSided": True})
    mat_idx = len(doc["materials"]) - 1
    doc["meshes"].append(
        {
            "name": "estimated_floor_plane",
            "primitives": [{"attributes": {"POSITION": pos_acc}, "indices": idx_acc, "material": mat_idx, "mode": 4}],
        }
    )
    mesh_idx = len(doc["meshes"]) - 1
    doc["nodes"].append(
        {
            "name": "estimated_floor_plane",
            "mesh": mesh_idx,
            "extras": {"simai": {"surface_source": "ESTIMATED_PLANE", "geometry_role": "VISUAL", "safety_use": False}},
        }
    )
    doc["scenes"][0]["nodes"].append(len(doc["nodes"]) - 1)
    doc["buffers"][0]["byteLength"] = len(blob)

    js = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(blob), b"BIN\x00")
        + bytes(blob)
    )


def test_zone_height_diff_unchanged_changed_and_insufficient_coverage() -> None:
    """변화 없음(UNCHANGED), 변경 의심(CHANGE_SUSPECTED + 비율·최대차·위치), 관측 부족(UNKNOWN)을 각각 판정한다."""
    prev_glb = _make_grid_glb((0.0, 3.0), (0.0, 1.0), z_fn=lambda x, y: np.zeros_like(x))

    # 새 맵: [0.2, 0.8] x [0.2, 0.8] 영역(36% 면적)에 높이 0.55 m 적재물이 생김, [2.0, 3.0] 구역은 그대로(노이즈 2 cm)
    def curr_z(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        obstacle = (x >= 0.2) & (x <= 0.8) & (y >= 0.2) & (y <= 0.8)
        return np.where(obstacle, 0.55, 0.02)

    curr_glb = _make_grid_glb((0.0, 3.0), (0.0, 1.0), z_fn=curr_z)

    zones = [
        {
            "zone_id": "zone-changed",
            "polygon_xy": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
        },
        {
            "zone_id": "zone-unchanged",
            "polygon_xy": [[2.0, 0.0], [3.0, 0.0], [3.0, 1.0], [2.0, 1.0]],
        },
        {
            "zone_id": "zone-unobserved",
            "polygon_xy": [[5.0, 5.0], [6.0, 5.0], [6.0, 6.0], [5.0, 6.0]],
        },
    ]

    summary = evaluate_zone_height_changes(
        prev_glb,
        curr_glb,
        zones,
        previous_scene_version="scene-prev-1",
        compared_scene_version="scene-preview-2",
    )
    assert schema_errors(summary, ZONE_HEIGHT_CHANGES_SCHEMA) == []

    by_id = {z["zone_id"]: z for z in summary["zones"]}
    changed = by_id["zone-changed"]
    assert changed["status"] == STATUS_CHANGE_SUSPECTED
    assert changed["reason"] is None
    assert changed["total_cells"] == 100
    assert changed["compared_cells"] == 100
    assert changed["changed_area_ratio"] >= 0.30
    assert abs(changed["max_height_diff_m"] - 0.55) < 1e-3
    assert 0.2 <= changed["max_diff_xy"][0] <= 0.8
    assert 0.2 <= changed["max_diff_xy"][1] <= 0.8

    unchanged = by_id["zone-unchanged"]
    assert unchanged["status"] == STATUS_UNCHANGED
    assert unchanged["reason"] is None
    assert unchanged["changed_area_ratio"] == 0.0
    assert abs(unchanged["max_height_diff_m"] - 0.02) < 1e-3
    assert unchanged["max_diff_xy"] is not None

    unobserved = by_id["zone-unobserved"]
    assert unobserved["status"] == STATUS_UNKNOWN
    assert unobserved["reason"] == REASON_INSUFFICIENT_COVERAGE
    assert unobserved["changed_area_ratio"] is None
    assert unobserved["max_height_diff_m"] is None
    assert unobserved["max_diff_xy"] is None
    assert unobserved["compared_cells"] == 0


def test_estimated_floor_plane_is_excluded_from_observed_surface() -> None:
    """`glb_enhance` 가 채운 추정 바닥(`ESTIMATED_PLANE`)은 관측 표면으로 세지 않는다(AGENTS 규칙 2·7)."""
    # 실제 관측은 [0, 1] x [0, 1] 에만 있고, [2, 3] x [0, 1] 은 추정 바닥 평면만 있다
    base_glb = _make_grid_glb((0.0, 1.0), (0.0, 1.0))
    enhanced_prev = _append_estimated_floor_node(base_glb, (0.0, 0.0, 4.0, 2.0), z=-0.02)
    enhanced_curr = _append_estimated_floor_node(base_glb, (0.0, 0.0, 4.0, 2.0), z=-0.02)

    zones = [
        {"zone_id": "zone-on-estimated-only", "polygon_xy": [[2.0, 0.0], [3.0, 0.0], [3.0, 1.0], [2.0, 1.0]]},
    ]
    summary = evaluate_zone_height_changes(
        enhanced_prev,
        enhanced_curr,
        zones,
        previous_scene_version="scene-prev",
        compared_scene_version="scene-curr",
    )
    assert schema_errors(summary, ZONE_HEIGHT_CHANGES_SCHEMA) == []
    item = summary["zones"][0]
    assert item["status"] == STATUS_UNKNOWN
    assert item["reason"] == REASON_INSUFFICIENT_COVERAGE
    assert item["compared_cells"] == 0


def test_ceiling_triangles_above_clip_do_not_hide_floor_changes() -> None:
    """천장(z > z_max_clip_m=2.20 m) 삼각형이 위를 덮어도 바닥·단상 높이 변화가 가려지지 않는다."""
    ceiling_v = np.array([[0.0, 0.0, 2.6], [1.0, 0.0, 2.6], [1.0, 1.0, 2.6], [0.0, 1.0, 2.6]], dtype=np.float64)
    ceiling_f = np.array([[0, 1, 2], [0, 2, 3]], dtype=np.int64)
    prev_glb = _make_grid_glb((0.0, 1.0), (0.0, 1.0), z_fn=lambda x, y: np.zeros_like(x), extra_tris_world=(ceiling_v, ceiling_f))
    curr_glb = _make_grid_glb(
        (0.0, 1.0),
        (0.0, 1.0),
        z_fn=lambda x, y: np.full_like(x, 0.40),
        extra_tris_world=(ceiling_v, ceiling_f),
    )
    summary = evaluate_zone_height_changes(
        prev_glb,
        curr_glb,
        [{"zone_id": "zone-1", "polygon_xy": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]}],
        previous_scene_version="scene-prev",
        compared_scene_version="scene-curr",
        config=ZoneHeightDiffConfig(),
    )
    assert summary["zones"][0]["status"] == STATUS_CHANGE_SUSPECTED
    assert abs(summary["zones"][0]["max_height_diff_m"] - 0.40) < 1e-3


def test_corrupt_glb_or_unanchored_world_returns_unknown_without_raising() -> None:
    zones = [{"zone_id": "zone-1", "polygon_xy": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]}]
    corrupt = evaluate_zone_height_changes(
        b"not-a-glb",
        b"also-not-a-glb",
        zones,
        previous_scene_version="scene-prev",
        compared_scene_version="scene-curr",
    )
    assert schema_errors(corrupt, ZONE_HEIGHT_CHANGES_SCHEMA) == []
    assert corrupt["zones"][0]["status"] == STATUS_UNKNOWN
    assert corrupt["zones"][0]["reason"] == REASON_SCENE_UNREADABLE

    unanchored = build_unknown_zone_height_changes(
        zones,
        reason=REASON_WORLD_NOT_ANCHORED,
        previous_scene_version="scene-prev",
        compared_scene_version="scene-curr",
    )
    assert schema_errors(unanchored, ZONE_HEIGHT_CHANGES_SCHEMA) == []
    assert unanchored["zones"][0]["status"] == STATUS_UNKNOWN
    assert unanchored["zones"][0]["reason"] == REASON_WORLD_NOT_ANCHORED
