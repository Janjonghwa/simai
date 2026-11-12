"""세팅 구역별 이전 관제 맵 대비 상면 높이 차이 판정 (명세 5.3·8.2, S15P31A101-630).

이전 관제 COMMITTED Scene GLB 와 이번 세팅 Scene GLB(미리보기 `preview_scene.glb` 또는 정식 `scene.glb`)가
같은 World(`world_anchor.accepted == True`)에 있을 때, 이전 활성 구역 다각형(`polygon_xy`) 바닥 영역을
10 cm(`cell_m=0.10`) 격자로 나누어 칸별 최고 관측 높이 $Z_{\\text{top}}(x, y)$ 를 비교한다.

규칙:
- `glb_enhance` 가 시각용으로 덧붙인 추정 평면(`ESTIMATED_PLANE`, `estimated_floor_plane`, `estimated_*_fill`)은
  실제 관측이 아니므로 제외한다(AGENTS 규칙 2·7).
- 두 맵이 같은 World 가 아니거나(`WORLD_NOT_ANCHORED`), 비교할 새 맵이 없거나(`REUSE_SCENE`),
  이전 맵이 없거나(`NO_PREVIOUS_SCENE`), GLB 를 읽지 못하거나(`SCENE_UNREADABLE`),
  구역 다각형 안에서 두 맵 모두 관측된 격자 비율이 기준 미만이면(`INSUFFICIENT_COVERAGE`)
  판정을 `UNKNOWN` 으로 두고 수치·위치 필드는 모두 `None`(`null`)으로 비운다(AGENTS 규칙 2).
- 판정은 세팅 구역 확인 화면의 정렬·강조·「변화 없음 모두 확인」 권유와 후속 `-631`(VLM 의견·전후 사진)의
  관심 위치(`max_diff_xy`) 입력에만 쓰며, 구역 자동 확인이나 Tier 0 안전 판정에는 쓰지 않는다(AGENTS 규칙 1·4).
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .glb_enhance import (
    UNSUPPORTED_REQUIRED,
    EnhanceSkip,
    GlbFile,
    _accessor,
    _node_matrix,
    parse_glb,
    rasterize,
)
from .texture_mesh import GLTF_Y_UP

STATUS_UNCHANGED = "UNCHANGED"
STATUS_CHANGE_SUSPECTED = "CHANGE_SUSPECTED"
STATUS_UNKNOWN = "UNKNOWN"

REASON_WORLD_NOT_ANCHORED = "WORLD_NOT_ANCHORED"
REASON_REUSE_SCENE = "REUSE_SCENE"
REASON_NO_PREVIOUS_SCENE = "NO_PREVIOUS_SCENE"
REASON_SCENE_UNREADABLE = "SCENE_UNREADABLE"
REASON_INSUFFICIENT_COVERAGE = "INSUFFICIENT_COVERAGE"

MODE_POINTS = 0
MODE_TRIANGLES = 4


@dataclass(frozen=True, slots=True)
class ZoneHeightDiffConfig:
    """구역별 높이 차이 비교 설정값. 초기값은 실제 로비 맵 두 벌로 재측정해 조정할 수 있게 환경 변수로 열어 둔다."""

    cell_m: float = 0.10
    height_diff_m: float = 0.15
    changed_area_ratio: float = 0.10
    min_coverage_ratio: float = 0.30
    min_compared_cells: int = 4
    z_min_clip_m: float = -2.0
    z_max_clip_m: float = 2.20

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> ZoneHeightDiffConfig:
        env = os.environ if env is None else env
        cfg = cls()
        kw: dict[str, Any] = {}
        for env_key, field_name, cast in (
            ("SIMAI_ZONE_HEIGHT_CELL_M", "cell_m", float),
            ("SIMAI_ZONE_HEIGHT_DIFF_M", "height_diff_m", float),
            ("SIMAI_ZONE_CHANGED_AREA_RATIO", "changed_area_ratio", float),
            ("SIMAI_ZONE_MIN_COVERAGE_RATIO", "min_coverage_ratio", float),
            ("SIMAI_ZONE_MIN_COMPARED_CELLS", "min_compared_cells", int),
            ("SIMAI_ZONE_Z_MIN_CLIP_M", "z_min_clip_m", float),
            ("SIMAI_ZONE_Z_MAX_CLIP_M", "z_max_clip_m", float),
        ):
            raw = env.get(env_key, "").strip()
            if raw:
                kw[field_name] = cast(raw)
        return replace(cfg, **kw) if kw else cfg


@dataclass(frozen=True, slots=True)
class GlbSurfaceGeometry:
    """GLB 에서 추출한 World 좌표계(m, +Z 상방) 관측 정점과 삼각형."""

    vertices_xyz: np.ndarray  # (N, 3) float64
    triangles_xyz: np.ndarray  # (T, 3, 3) float64


def _is_estimated_plane_entity(
    node: Mapping[str, Any],
    mesh: Mapping[str, Any],
    material: Mapping[str, Any],
) -> bool:
    """`glb_enhance` 가 덧붙인 추정 평면(`ESTIMATED_PLANE`)인지 확인한다."""
    for obj in (node, mesh):
        extras = obj.get("extras")
        if isinstance(extras, Mapping):
            simai = extras.get("simai")
            if isinstance(simai, Mapping) and simai.get("surface_source") == "ESTIMATED_PLANE":
                return True
    for name in (node.get("name"), mesh.get("name"), material.get("name")):
        if isinstance(name, str) and name.startswith("estimated_"):
            return True
    return False


def extract_observed_surface(
    glb_bytes: bytes,
    *,
    z_min_clip_m: float = -2.0,
    z_max_clip_m: float = 2.20,
) -> GlbSurfaceGeometry:
    """GLB 바이트에서 추정 평면(`ESTIMATED_PLANE`)을 뺀 관측 표면 정점·삼각형을 World 좌표로 뽑는다."""
    g: GlbFile = parse_glb(glb_bytes)
    doc = g.doc
    if UNSUPPORTED_REQUIRED & set(doc.get("extensionsRequired", [])):
        raise EnhanceSkip("COMPRESSED_GLB", "압축 확장이 필요한 GLB 는 읽지 않는다")
    simai = (doc.get("extras") or {}).get("simai") if isinstance(doc.get("extras"), Mapping) else None
    if isinstance(simai, Mapping) and simai.get("axis_convention") not in (None, "GLTF_Y_UP"):
        raise EnhanceSkip("AXIS_NOT_WORLD", f"axis_convention={simai.get('axis_convention')}")
    meshes = doc.get("meshes") or []
    nodes = doc.get("nodes") or []
    if not meshes or not nodes:
        raise EnhanceSkip("EMPTY_SCENE", "GLB 에 메시·노드가 없다")

    scene_nodes = (
        doc["scenes"][doc.get("scene", 0)].get("nodes", [])
        if doc.get("scenes")
        else list(range(len(nodes)))
    )
    stack: list[tuple[int, np.ndarray]] = [(int(n), np.eye(4, dtype=np.float64)) for n in scene_nodes]
    visited: set[int] = set()
    materials = doc.get("materials") or []

    verts_list: list[np.ndarray] = []
    tris_list: list[np.ndarray] = []

    while stack:
        node_idx, parent_mat = stack.pop()
        if node_idx in visited or node_idx < 0 or node_idx >= len(nodes):
            continue
        visited.add(node_idx)
        node = nodes[node_idx]
        mat = parent_mat @ _node_matrix(node)
        for child in node.get("children", []):
            stack.append((int(child), mat))
        if "mesh" not in node:
            continue
        mesh_idx = int(node["mesh"])
        if mesh_idx < 0 or mesh_idx >= len(meshes):
            continue
        mesh = meshes[mesh_idx]
        for prim in mesh.get("primitives", []):
            mode = int(prim.get("mode", MODE_TRIANGLES))
            if mode not in (MODE_POINTS, MODE_TRIANGLES):
                continue
            attrs = prim.get("attributes") or {}
            if "POSITION" not in attrs:
                continue
            mat_idx = prim.get("material")
            material = (
                materials[int(mat_idx)]
                if isinstance(mat_idx, int) and 0 <= int(mat_idx) < len(materials)
                else {}
            )
            if _is_estimated_plane_entity(node, mesh, material):
                continue
            pos_glb = _accessor(g, int(attrs["POSITION"])).astype(np.float64)
            if pos_glb.ndim != 2 or pos_glb.shape[1] != 3 or len(pos_glb) == 0:
                continue
            pos_glb = pos_glb @ mat[:3, :3].T + mat[:3, 3]
            pos_world = pos_glb @ GLTF_Y_UP
            finite_v = np.all(np.isfinite(pos_world), axis=1)
            z_ok = (pos_world[:, 2] >= z_min_clip_m) & (pos_world[:, 2] <= z_max_clip_m)
            valid_v = finite_v & z_ok
            if np.any(valid_v):
                verts_list.append(pos_world[valid_v])

            if mode == MODE_TRIANGLES:
                if "indices" in prim:
                    idx = _accessor(g, int(prim["indices"])).reshape(-1).astype(np.int64)
                else:
                    idx = np.arange(len(pos_world), dtype=np.int64)
                idx = idx[: (len(idx) // 3) * 3].reshape(-1, 3)
                if len(idx) == 0:
                    continue
                in_range = (idx.min(axis=1) >= 0) & (idx.max(axis=1) < len(pos_world))
                idx = idx[in_range]
                if len(idx) == 0:
                    continue
                tri_valid = valid_v[idx[:, 0]] & valid_v[idx[:, 1]] & valid_v[idx[:, 2]]
                idx = idx[tri_valid]
                if len(idx) > 0:
                    tris_list.append(pos_world[idx])

    vertices_xyz = (
        np.concatenate(verts_list, axis=0)
        if verts_list
        else np.zeros((0, 3), dtype=np.float64)
    )
    triangles_xyz = (
        np.concatenate(tris_list, axis=0)
        if tris_list
        else np.zeros((0, 3, 3), dtype=np.float64)
    )
    if len(vertices_xyz) == 0 and len(triangles_xyz) == 0:
        raise EnhanceSkip("EMPTY_OBSERVED_SURFACE", "관측 표면 정점·삼각형이 없다")
    return GlbSurfaceGeometry(vertices_xyz=vertices_xyz, triangles_xyz=triangles_xyz)


def polygon_grid_mask(
    polygon_xy: Sequence[Sequence[float]],
    cell_m: float,
) -> tuple[float, float, np.ndarray]:
    """다각형 `polygon_xy` 의 경계 상자를 `cell_m` 격자에 맞춰 자르고 칸 중심 포함 마스크 `(ny, nx)` 를 돌려준다."""
    poly = np.asarray(polygon_xy, dtype=np.float64)
    if poly.ndim != 2 or poly.shape[0] < 3 or poly.shape[1] != 2 or not np.all(np.isfinite(poly)):
        return 0.0, 0.0, np.zeros((0, 0), dtype=bool)
    xmin, ymin = float(poly[:, 0].min()), float(poly[:, 1].min())
    xmax, ymax = float(poly[:, 0].max()), float(poly[:, 1].max())
    if xmax <= xmin or ymax <= ymin:
        return xmin, ymin, np.zeros((0, 0), dtype=bool)

    x0 = math.floor(xmin / cell_m) * cell_m
    y0 = math.floor(ymin / cell_m) * cell_m
    nx = max(1, math.ceil((xmax - x0) / cell_m))
    ny = max(1, math.ceil((ymax - y0) / cell_m))
    if nx > 2000 or ny > 2000:
        return x0, y0, np.zeros((0, 0), dtype=bool)

    xs = x0 + (np.arange(nx, dtype=np.float64) + 0.5) * cell_m
    ys = y0 + (np.arange(ny, dtype=np.float64) + 0.5) * cell_m
    px, py = np.meshgrid(xs, ys)

    inside = np.zeros((ny, nx), dtype=bool)
    n_verts = len(poly)
    for k in range(n_verts):
        xa, ya = float(poly[k, 0]), float(poly[k, 1])
        xb, yb = float(poly[(k + 1) % n_verts, 0]), float(poly[(k + 1) % n_verts, 1])
        cond = (ya > py) != (yb > py)
        if not np.any(cond):
            continue
        dy = yb - ya
        x_int = (xb - xa) * (py - ya) / (dy if abs(dy) > 1e-15 else 1.0) + xa
        inside ^= cond & (px < x_int)
    return x0, y0, inside


def rasterize_top_height_grid(
    geom: GlbSurfaceGeometry,
    *,
    x0: float,
    y0: float,
    nx: int,
    ny: int,
    cell_m: float,
) -> np.ndarray:
    """격자 `[x0, x0 + nx·cell_m) × [y0, y0 + ny·cell_m)` 의 칸별 최고 관측 높이 `Z_top` `(ny, nx)` 를 계산한다.

    관측이 없는 칸은 `np.nan` 으로 비운다(Z=0 으로 메우지 않는다, AGENTS 규칙 2).
    """
    z_top = np.full((ny, nx), np.nan, dtype=np.float64)
    if nx <= 0 or ny <= 0:
        return z_top

    x1 = x0 + nx * cell_m
    y1 = y0 + ny * cell_m

    tris = geom.triangles_xyz
    if len(tris) > 0:
        tx = tris[:, :, 0]
        ty = tris[:, :, 1]
        overlap = (
            (tx.max(axis=1) >= x0)
            & (tx.min(axis=1) <= x1)
            & (ty.max(axis=1) >= y0)
            & (ty.min(axis=1) <= y1)
        )
        sub_tris = tris[overlap]
        if len(sub_tris) > 0:
            tri_xy = np.stack(
                [
                    (sub_tris[:, :, 0] - x0) / cell_m,
                    (sub_tris[:, :, 1] - y0) / cell_m,
                ],
                axis=2,
            )
            tri_depth = -sub_tris[:, :, 2]  # 위(+Z)일수록 작은 depth 로 선택
            tid, _, depth = rasterize(tri_xy, tri_depth, (ny, nx), perspective=False)
            hit = tid >= 0
            z_top[hit] = -depth[hit]

    verts = geom.vertices_xyz
    if len(verts) > 0:
        vx, vy, vz = verts[:, 0], verts[:, 1], verts[:, 2]
        in_box = (vx >= x0) & (vx < x1) & (vy >= y0) & (vy < y1)
        if np.any(in_box):
            j = np.floor((vx[in_box] - x0) / cell_m).astype(np.int64)
            i = np.floor((vy[in_box] - y0) / cell_m).astype(np.int64)
            valid_ij = (i >= 0) & (i < ny) & (j >= 0) & (j < nx)
            if np.any(valid_ij):
                v_top = np.full((ny, nx), -np.inf, dtype=np.float64)
                np.maximum.at(v_top, (i[valid_ij], j[valid_ij]), vz[in_box][valid_ij])
                has_v = np.isfinite(v_top)
                z_top = np.where(
                    has_v,
                    np.where(np.isfinite(z_top), np.maximum(z_top, v_top), v_top),
                    z_top,
                )

    return z_top


def _unknown_item(
    zone_id: str,
    reason: str,
    *,
    total_cells: int = 0,
    compared_cells: int = 0,
) -> dict[str, Any]:
    return {
        "zone_id": zone_id,
        "status": STATUS_UNKNOWN,
        "reason": reason,
        "changed_area_ratio": None,
        "max_height_diff_m": None,
        "max_diff_xy": None,
        "total_cells": int(max(0, total_cells)),
        "compared_cells": int(max(0, compared_cells)),
        "changed_cells": 0,
    }


def build_unknown_zone_height_changes(
    previous_zones: Sequence[Mapping[str, Any]],
    *,
    reason: str,
    previous_scene_version: str | None,
    compared_scene_version: str | None,
    config: ZoneHeightDiffConfig | None = None,
) -> dict[str, Any]:
    """비교 근거가 없을 때 모든 이전 구역을 `UNKNOWN`(+사유, 수치 null)으로 채운 요약을 만든다."""
    cfg = config or ZoneHeightDiffConfig.from_env()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in previous_zones:
        zone_id = str(raw.get("zone_id") or "").strip()
        if not zone_id or zone_id in seen:
            continue
        seen.add(zone_id)
        poly = raw.get("polygon_xy") or []
        _, _, mask = polygon_grid_mask(poly, cfg.cell_m)
        items.append(_unknown_item(zone_id, reason, total_cells=int(mask.sum())))
    return {
        "previous_scene_version": previous_scene_version,
        "compared_scene_version": compared_scene_version,
        "cell_m": cfg.cell_m,
        "height_diff_threshold_m": cfg.height_diff_m,
        "changed_area_ratio_threshold": cfg.changed_area_ratio,
        "min_coverage_ratio": cfg.min_coverage_ratio,
        "zones": items,
    }


def evaluate_zone_height_changes(
    previous_glb: bytes,
    current_glb: bytes,
    previous_zones: Sequence[Mapping[str, Any]],
    *,
    previous_scene_version: str | None,
    compared_scene_version: str | None,
    config: ZoneHeightDiffConfig | None = None,
) -> dict[str, Any]:
    """이전 관제 GLB 와 이번 세팅 GLB 의 구역별 상면 높이 차이(`site_setup#/$defs/zone_height_changes`)를 계산한다."""
    cfg = config or ZoneHeightDiffConfig.from_env()
    if not previous_zones:
        return {
            "previous_scene_version": previous_scene_version,
            "compared_scene_version": compared_scene_version,
            "cell_m": cfg.cell_m,
            "height_diff_threshold_m": cfg.height_diff_m,
            "changed_area_ratio_threshold": cfg.changed_area_ratio,
            "min_coverage_ratio": cfg.min_coverage_ratio,
            "zones": [],
        }

    try:
        prev_geom = extract_observed_surface(
            previous_glb,
            z_min_clip_m=cfg.z_min_clip_m,
            z_max_clip_m=cfg.z_max_clip_m,
        )
        curr_geom = extract_observed_surface(
            current_glb,
            z_min_clip_m=cfg.z_min_clip_m,
            z_max_clip_m=cfg.z_max_clip_m,
        )
    except (EnhanceSkip, ValueError, KeyError, IndexError, TypeError):
        return build_unknown_zone_height_changes(
            previous_zones,
            reason=REASON_SCENE_UNREADABLE,
            previous_scene_version=previous_scene_version,
            compared_scene_version=compared_scene_version,
            config=cfg,
        )

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in previous_zones:
        zone_id = str(raw.get("zone_id") or "").strip()
        if not zone_id or zone_id in seen:
            continue
        seen.add(zone_id)
        poly = raw.get("polygon_xy") or []
        x0, y0, mask = polygon_grid_mask(poly, cfg.cell_m)
        total_cells = int(mask.sum())
        if total_cells == 0:
            items.append(_unknown_item(zone_id, REASON_INSUFFICIENT_COVERAGE, total_cells=0, compared_cells=0))
            continue

        ny, nx = mask.shape
        prev_z = rasterize_top_height_grid(prev_geom, x0=x0, y0=y0, nx=nx, ny=ny, cell_m=cfg.cell_m)
        curr_z = rasterize_top_height_grid(curr_geom, x0=x0, y0=y0, nx=nx, ny=ny, cell_m=cfg.cell_m)

        both_valid = mask & np.isfinite(prev_z) & np.isfinite(curr_z)
        compared_cells = int(both_valid.sum())
        if (
            compared_cells < cfg.min_compared_cells
            or (compared_cells / total_cells) < cfg.min_coverage_ratio - 1e-9
        ):
            items.append(
                _unknown_item(
                    zone_id,
                    REASON_INSUFFICIENT_COVERAGE,
                    total_cells=total_cells,
                    compared_cells=compared_cells,
                )
            )
            continue

        diffs = np.abs(curr_z[both_valid] - prev_z[both_valid])
        changed_mask = diffs >= cfg.height_diff_m - 1e-9
        changed_cells = int(changed_mask.sum())
        ratio = float(changed_cells / compared_cells)
        max_idx = int(np.argmax(diffs))
        max_diff = float(diffs[max_idx])
        valid_ij = np.argwhere(both_valid)
        i_max, j_max = int(valid_ij[max_idx, 0]), int(valid_ij[max_idx, 1])
        max_xy = [
            round(float(x0 + (j_max + 0.5) * cfg.cell_m), 4),
            round(float(y0 + (i_max + 0.5) * cfg.cell_m), 4),
        ]
        status = (
            STATUS_CHANGE_SUSPECTED
            if ratio >= cfg.changed_area_ratio - 1e-9
            else STATUS_UNCHANGED
        )
        items.append(
            {
                "zone_id": zone_id,
                "status": status,
                "reason": None,
                "changed_area_ratio": round(ratio, 4),
                "max_height_diff_m": round(max_diff, 4),
                "max_diff_xy": max_xy,
                "total_cells": total_cells,
                "compared_cells": compared_cells,
                "changed_cells": changed_cells,
            }
        )

    return {
        "previous_scene_version": previous_scene_version,
        "compared_scene_version": compared_scene_version,
        "cell_m": cfg.cell_m,
        "height_diff_threshold_m": cfg.height_diff_m,
        "changed_area_ratio_threshold": cfg.changed_area_ratio,
        "min_coverage_ratio": cfg.min_coverage_ratio,
        "zones": items,
    }
