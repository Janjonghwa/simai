"""빠른 미리보기 3D 맵(`preview_scene`) 생성기 (명세 5.3, S15P31A101-451).

MapAnything 점구름·깊이 + 확정 보정·World 변환 위에서 `texrecon`·TSDF 굽기를 기다리지 않고
정점색 단순 메시(또는 점군) glTF 2.0 이진(`.glb`)을 약 10초 안에 먼저 만들어 Scene 후보(`CANDIDATE`)로 낸다.
시각 자산(`geometry_role: "VISUAL"`, `safety_use: False`) 전용이며 Tier 0 안전 기하에 쓰지 않는다.
World → glTF `(+X, +Z, -Y)` 축 변환은 `texture_mesh.to_glb_points` 한 곳에서만 수행한다.
"""

from __future__ import annotations

import json
import struct
import time
from typing import Any

import numpy as np
from scipy.spatial import Delaunay, QhullError

from simai_ai.scene import run_hybrid_depth as hd
from simai_ai.scene.texture_mesh import glb_axis_convention, to_glb_points

DEFAULT_PREVIEW_STRIDE = 2
DEFAULT_MAX_EDGE_M = 0.5

GLTF_MAGIC = b"glTF"
GLTF_VERSION = 2
CHUNK_JSON = b"JSON"
CHUNK_BIN = b"BIN\x00"

MODE_POINTS = 0
MODE_TRIANGLES = 4

COMP_UNSIGNED_BYTE = 5121
COMP_UNSIGNED_INT = 5125
COMP_FLOAT = 5126

TARGET_ARRAY_BUFFER = 34962
TARGET_ELEMENT_ARRAY_BUFFER = 34963


def _normalize_rgba_u8(colors: np.ndarray, n_vertices: int) -> np.ndarray:
    """`(N, 3)` 또는 `(N, 4)` 색 배열을 `(N, 4)` uint8 RGBA 로 정규화한다."""
    c = np.asarray(colors)
    if c.ndim != 2 or c.shape[0] != n_vertices or c.shape[1] not in (3, 4):
        raise ValueError(f"색 배열 모양이 정점 수({n_vertices})와 맞지 않는다: {c.shape}")
    if np.issubdtype(c.dtype, np.floating):
        scale = 255.0 if float(np.nanmax(c)) <= 1.0 + 1e-6 else 1.0
        c_u8 = np.clip(np.rint(c * scale), 0, 255).astype(np.uint8)
    else:
        c_u8 = np.clip(c, 0, 255).astype(np.uint8)
    if c_u8.shape[1] == 3:
        alpha = np.full((n_vertices, 1), 255, dtype=np.uint8)
        c_u8 = np.concatenate([c_u8, alpha], axis=1)
    return np.ascontiguousarray(c_u8, dtype=np.uint8)


def encode_vertex_color_glb(
    V_glb: np.ndarray,
    C_rgb: np.ndarray,
    F: np.ndarray | None = None,
    *,
    extras_simai: dict[str, Any] | None = None,
) -> bytes:
    """정점 좌표(`V_glb`, glTF Y-up), 정점 색(`C_rgb`), 선택 삼각형 면(`F`)을 표준 glTF 2.0 `.glb` 바이트로 묶는다."""
    V_arr = np.asarray(V_glb, dtype=np.float32)
    if V_arr.ndim != 2 or V_arr.shape[1] != 3 or len(V_arr) == 0:
        raise ValueError(f"V_glb 는 비어 있지 않은 (N, 3) 배열이어야 한다: {V_arr.shape}")
    if not np.all(np.isfinite(V_arr)):
        raise ValueError("V_glb 에 유한하지 않은 값(NaN/Inf)이 있다")

    n_verts = int(len(V_arr))
    rgba = _normalize_rgba_u8(C_rgb, n_verts)

    F_arr: np.ndarray | None = None
    if F is not None and len(F) > 0:
        f_tmp = np.asarray(F, dtype=np.int64)
        if f_tmp.ndim != 2 or f_tmp.shape[1] != 3:
            raise ValueError(f"F 는 (M, 3) 배열이어야 한다: {f_tmp.shape}")
        if int(f_tmp.min()) < 0 or int(f_tmp.max()) >= n_verts:
            raise ValueError("F 에 정점 범위를 벗어난 인덱스가 있다")
        F_arr = np.ascontiguousarray(f_tmp, dtype="<u4")

    pos_bytes = np.ascontiguousarray(V_arr, dtype="<f4").tobytes()
    col_bytes = rgba.tobytes()  # N * 4 바이트로 항상 4바이트 정렬됨
    idx_bytes = F_arr.tobytes() if F_arr is not None else b""

    bin_body = pos_bytes + col_bytes + idx_bytes
    bin_pad = (-len(bin_body)) % 4
    if bin_pad:
        bin_body += b"\x00" * bin_pad

    v_min = [float(x) for x in V_arr.min(axis=0)]
    v_max = [float(x) for x in V_arr.max(axis=0)]

    buffer_views: list[dict[str, Any]] = [
        {
            "buffer": 0,
            "byteOffset": 0,
            "byteLength": len(pos_bytes),
            "target": TARGET_ARRAY_BUFFER,
        },
        {
            "buffer": 0,
            "byteOffset": len(pos_bytes),
            "byteLength": len(col_bytes),
            "target": TARGET_ARRAY_BUFFER,
        },
    ]
    accessors: list[dict[str, Any]] = [
        {
            "bufferView": 0,
            "byteOffset": 0,
            "componentType": COMP_FLOAT,
            "count": n_verts,
            "type": "VEC3",
            "min": v_min,
            "max": v_max,
        },
        {
            "bufferView": 1,
            "byteOffset": 0,
            "componentType": COMP_UNSIGNED_BYTE,
            "normalized": True,
            "count": n_verts,
            "type": "VEC4",
        },
    ]

    primitive: dict[str, Any] = {
        "attributes": {"POSITION": 0, "COLOR_0": 1},
        "material": 0,
    }
    if F_arr is not None:
        buffer_views.append(
            {
                "buffer": 0,
                "byteOffset": len(pos_bytes) + len(col_bytes),
                "byteLength": len(idx_bytes),
                "target": TARGET_ELEMENT_ARRAY_BUFFER,
            }
        )
        accessors.append(
            {
                "bufferView": 2,
                "byteOffset": 0,
                "componentType": COMP_UNSIGNED_INT,
                "count": int(F_arr.size),
                "type": "SCALAR",
            }
        )
        primitive["indices"] = 2
        primitive["mode"] = MODE_TRIANGLES
    else:
        primitive["mode"] = MODE_POINTS

    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "simai_ai.scene.preview_scene"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0, "name": "preview_scene"}],
        "meshes": [{"name": "preview_scene", "primitives": [primitive]}],
        "materials": [
            {
                "name": "preview_vertex_color",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "metallicFactor": 0.0,
                    "roughnessFactor": 1.0,
                },
                "doubleSided": True,
            }
        ],
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(bin_body)}],
    }
    if extras_simai is not None:
        doc["extras"] = {"simai": extras_simai}

    json_bytes = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_pad = (-len(json_bytes)) % 4
    if json_pad:
        json_bytes += b" " * json_pad

    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_body)
    return (
        struct.pack("<4sII", GLTF_MAGIC, GLTF_VERSION, total_len)
        + struct.pack("<I4s", len(json_bytes), CHUNK_JSON)
        + json_bytes
        + struct.pack("<I4s", len(bin_body), CHUNK_BIN)
        + bin_body
    )


def triangulate_pointcloud_xy(
    points_xyz: np.ndarray,
    *,
    max_edge_m: float = DEFAULT_MAX_EDGE_M,
) -> np.ndarray:
    """비구조 3D 점구름 `(N, 3)`을 주평면 2D Delaunay 삼각분할 + 최대 변 길이(`max_edge_m`) 필터로 단순 메시 면 `(M, 3)`으로 잇는다.

    점이 3개 미만이거나 일직선(퇴화)이면 빈 `(0, 3)` 배열을 돌려줘 호출자가 점군(`POINTS`) 모드로 폴백하게 한다.
    """
    pts = np.asarray(points_xyz, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) < 3:
        return np.zeros((0, 3), dtype=np.int64)

    centered = pts - pts.mean(axis=0, keepdims=True)
    # 기본은 수평(XY) 투영을 쓰되, 수직 벽처럼 XY 분산이 퇴화한 경우에는 SVD 주성분 2축으로 투영한다.
    xy = centered[:, :2]
    if np.linalg.matrix_rank(xy, tol=1e-6) < 2:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        xy = centered @ vt[:2].T

    try:
        tri = Delaunay(xy)
    except (QhullError, ValueError):
        return np.zeros((0, 3), dtype=np.int64)

    simplices = np.asarray(tri.simplices, dtype=np.int64)
    if len(simplices) == 0:
        return np.zeros((0, 3), dtype=np.int64)

    p0 = pts[simplices[:, 0]]
    p1 = pts[simplices[:, 1]]
    p2 = pts[simplices[:, 2]]
    e01 = np.linalg.norm(p1 - p0, axis=1)
    e12 = np.linalg.norm(p2 - p1, axis=1)
    e20 = np.linalg.norm(p0 - p2, axis=1)
    area2 = np.linalg.norm(np.cross(p1 - p0, p2 - p0), axis=1)

    keep = (area2 > 1e-10) & (
        np.maximum(np.maximum(e01, e12), e20) <= float(max_edge_m)
    )
    return np.ascontiguousarray(simplices[keep], dtype=np.int64)


def make_preview_extras(
    *,
    M: np.ndarray | None,
    vertex_count: int,
    triangle_count: int,
    stride: int,
    primitive_mode: str,
) -> dict[str, Any]:
    """미리보기 glTF 의 `extras.simai` 메타데이터 사전을 만든다."""
    return {
        "schema_version": "1.0.0",
        "scene_tier": "PREVIEW",
        "preview_scene": True,
        "geometry_role": "VISUAL",
        "safety_use": False,
        "frame": "WORLD" if M is not None else "MAPANYTHING_WORLD",
        "axis_convention": glb_axis_convention(M),
        "primitive_mode": primitive_mode,
        "surface_source": {
            "encoding": "VERTEX_COLOR_0",
            "default": "OBSERVED_DEPTH_VERTEX_COLOR",
            "ratios_by_area": {
                "OBSERVED": 1.0,
                "INTERPOLATED": 0.0,
                "UNOBSERVED": 0.0,
                "ESTIMATED_PLANE": 0.0,
            },
        },
        "vertex_count": int(vertex_count),
        "triangle_count": int(triangle_count),
        "preview_stride": int(stride),
    }


def build_preview_from_pointcloud(
    points_xyz: np.ndarray,
    colors_rgb: np.ndarray,
    *,
    M: np.ndarray | None = None,
    faces: np.ndarray | None = None,
    max_edge_m: float = DEFAULT_MAX_EDGE_M,
    triangulate: bool = True,
    stride: int = 1,
) -> tuple[bytes, dict[str, Any]]:
    """점구름 `(points_xyz, colors_rgb)` 과 선택 World 정렬 행렬 `M` 에서 미리보기 `.glb` 바이트와 요약 기록을 만든다."""
    t0 = time.perf_counter()
    if stride < 1:
        raise ValueError(f"stride 는 1 이상이어야 한다: {stride}")

    pts = np.asarray(points_xyz, dtype=np.float64)
    cols = np.asarray(colors_rgb)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) == 0:
        raise ValueError(f"points_xyz 는 비어 있지 않은 (N, 3) 배열이어야 한다: {pts.shape}")
    if len(cols) != len(pts):
        raise ValueError(f"colors_rgb 길이({len(cols)})가 points_xyz({len(pts)})와 다르다")

    if stride > 1 and faces is None:
        pts = pts[::stride]
        cols = cols[::stride]

    W = hd.transform_points(M, pts)
    if faces is not None:
        F = np.asarray(faces, dtype=np.int64)
    elif triangulate:
        F = triangulate_pointcloud_xy(W, max_edge_m=max_edge_m)
    else:
        F = np.zeros((0, 3), dtype=np.int64)

    # World → glTF (+X, +Z, -Y) 변환은 to_glb_points 한 곳에서만 수행한다.
    V_glb = to_glb_points(M, pts)
    prim_mode = "TRIANGLES" if len(F) > 0 else "POINTS"
    extras = make_preview_extras(
        M=M,
        vertex_count=len(V_glb),
        triangle_count=len(F),
        stride=stride,
        primitive_mode=prim_mode,
    )
    glb_bytes = encode_vertex_color_glb(V_glb, cols, F, extras_simai=extras)
    build_s = round(time.perf_counter() - t0, 4)

    floor_band = W[np.abs(W[:, 2]) < 0.15, 2]
    floor_rms = (
        round(float(np.sqrt(np.mean(floor_band**2))), 4)
        if len(floor_band) >= 10
        else 0.0
    )

    meta: dict[str, Any] = {
        "scene_tier": "PREVIEW",
        "preview_scene": True,
        "primitive_mode": prim_mode,
        "vertex_count": int(len(V_glb)),
        "triangle_count": int(len(F)),
        "preview_stride": int(stride),
        "axis_convention": glb_axis_convention(M),
        "floor_flatness_rms_m": floor_rms,
        "build_s": build_s,
        "size_bytes": len(glb_bytes),
    }
    return glb_bytes, meta


def build_preview_from_views(
    views: list[hd.FuseView],
    *,
    M: np.ndarray | None = None,
    stride: int = DEFAULT_PREVIEW_STRIDE,
    edge_tol: float = hd.EDGE_TOL,
) -> tuple[bytes, dict[str, Any]]:
    """MapAnything 시점별 깊이·색·마스크(`hd.FuseView` 목록)에서 정점색 격자 메시(또는 점군 폴백) `.glb`를 만든다."""
    t0 = time.perf_counter()
    if stride < 1:
        raise ValueError(f"stride 는 1 이상이어야 한다: {stride}")
    if not views:
        raise ValueError("views 가 비어 있다")

    vs: list[np.ndarray] = []
    fs: list[np.ndarray] = []
    cs: list[np.ndarray] = []
    offset = 0
    per_view: dict[str, dict[str, int]] = {}

    for v in views:
        V_v, F_v, C_v = hd.grid_mesh(v, stride, None, edge_tol=edge_tol)
        per_view[v.name] = {"vertices": int(len(V_v)), "faces": int(len(F_v))}
        if len(F_v) == 0:
            continue
        vs.append(V_v)
        fs.append(F_v + offset)
        if C_v is not None:
            cs.append(C_v[:, :3])
        else:
            cs.append(np.full((len(V_v), 3), 180, dtype=np.uint8))
        offset += len(V_v)

    if vs:
        V_all = np.concatenate(vs, axis=0)
        F_all = np.concatenate(fs, axis=0)
        C_all = np.concatenate(cs, axis=0)
        glb_bytes, meta = build_preview_from_pointcloud(
            V_all,
            C_all,
            M=M,
            faces=F_all,
            triangulate=False,
            stride=stride,
        )
    else:
        # 격자 삼각형이 하나도 안 나오는 희소 깊이 입력이면 유효 픽셀 점구름으로 폴백한다.
        pts_list: list[np.ndarray] = []
        col_list: list[np.ndarray] = []
        for v in views:
            d_sub = v.depth[::stride, ::stride]
            ok = v.valid[::stride, ::stride] & np.isfinite(d_sub)
            if not np.any(ok):
                continue
            vv, uu = np.nonzero(ok)
            pts_v = v.backproject(vv * stride, uu * stride)
            if v.colors is not None:
                col_v = v.colors[::stride, ::stride][ok]
            else:
                col_v = np.full((len(pts_v), 3), 180, dtype=np.uint8)
            pts_list.append(pts_v)
            col_list.append(col_v)
        if not pts_list:
            raise ValueError("모든 시점에 유효한 깊이 픽셀이 없다")
        glb_bytes, meta = build_preview_from_pointcloud(
            np.concatenate(pts_list, axis=0),
            np.concatenate(col_list, axis=0),
            M=M,
            triangulate=True,
            stride=stride,
        )

    meta["views"] = per_view
    meta["build_s"] = round(time.perf_counter() - t0, 4)
    return glb_bytes, meta
