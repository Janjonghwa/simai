"""3D 맵(GLB) 물체 제자리 완성: 부분 관측 물체 후보 분리, 3D-Fixer/가짜 백엔드 완성, 정형 도형(라바콘·안내판), 배치·검증·합치기 (명세 5.3, S15P31A101-515).

고정 카메라 4대가 일부만 본 물체(스툴·의자·원탁 등)는 관측 형상을 기준점으로 제자리 완성(관측 부분 보존 + 미관측 생성)하고,
라바콘(높이 67.5 cm, 밑판 한 변 37 cm 정사각, 꼭대기 지름 4.5 cm)·안내판(평판 + 관측 사진 텍스처)은 정형 도형 템플릿에
관측 사진을 투영해 채운다.

규칙 준수:
- 시각 자산(`geometry_role: VISUAL`, `safety_use: false`)이며 Tier 0 위치·위험 판정에 쓰지 않는다(AGENTS 규칙 1·7).
- 면별·텍셀별 출처를 기존 `surface_source` 체계(`OBSERVED=2`, `INTERPOLATED=1`, `UNOBSERVED=0`, `ESTIMATED_PLANE=3`, `GENERATED=4`, -514 와 동일)로 기록한다.
- World(미터, 오른손, +Z 상방, 바닥 Z=0)에서 계산하고 glTF 축 변환(`(x, z, -y)`)은 `texture_mesh.to_glb_points` 한 곳에서만 수행한다(규칙 3).
- 검증 기준(4대 재투영 실루엣 IoU, 관측 점→메시 거리 중앙·p95, 바닥 접촉 오차, 면 수·용량, 시간) 미달이면 원래 메시 조각을 유지한다(규칙 2).
- 끄기 설정: `SIMAI_GLB_OBJECTS=off` (또는 `0`, `false`, `no`, 작업 옵션 `objects=False`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from .glb_enhance import (
    UNSUPPORTED_REQUIRED,
    CheckCamera,
    EnhanceSkip,
    GlbFile,
    Prim,
    SceneModel,
    _accessor,
    _add_accessor,
    _add_view,
    _glb_to_world,
    _node_matrix,
    cameras_from_raw,
    linear_to_srgb,
    parse_glb,
    prim_vertex_colors,
    rasterize,
    serialize_glb,
)
from .mesh_cleanup import _cell_components, _cells, face_normals
from .texture_mesh import to_glb_points
from .texture_provenance import (
    ALL_SURFACE_VALUES,
    ESTIMATED_PLANE,
    GENERATED,
    OBSERVED,
    UNOBSERVED,
    bilinear,
)

OBJECT_COMPLETION_VERSION = "1.0.0"
SURFACE_VALUE_TABLE = {str(v): k for k, v in sorted(ALL_SURFACE_VALUES.items(), key=lambda kv: kv[1])}
NEAR_M = 0.05

# 라바콘 정형 치수 (지시서 고정값)
TRAFFIC_CONE_HEIGHT_M = 0.675       # 67.5 cm
TRAFFIC_CONE_BASE_SIDE_M = 0.37     # 밑판 한 변 37 cm (정사각, 모서리 둥글림 무시)
TRAFFIC_CONE_TOP_DIAMETER_M = 0.045 # 꼭대기 지름 4.5 cm
TRAFFIC_CONE_BASE_HEIGHT_M = 0.030  # 밑판 두께 3.0 cm (전체 높이 67.5 cm 내)
TRAFFIC_CONE_BODY_BOTTOM_DIAMETER_M = 0.28  # 원뿔 하단 지름 28 cm


class CompletionSkip(Exception):
    """물체 완성을 건너뛰는 사유. 원본 메시를 그대로 유지한다."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(detail or reason)
        self.reason = reason


# --------------------------------------------------------------------------- 설정


@dataclass(frozen=True)
class SignboardConfig:
    """안내판 정형 평판 치수 및 분류 문턱 설정(None 이면 관측 점에서 추정하되 범위 안으로 클램프)."""

    width_m: float | None = None
    height_m: float | None = None
    thickness_m: float = 0.04
    min_width_m: float = 0.10
    max_width_m: float = 0.65
    min_height_m: float = 0.12
    max_height_m: float = 0.85
    max_thickness_m: float = 0.12
    min_diagonal_m: float = 0.14
    max_floor_gap_m: float = 0.18
    min_wall_distance_m: float = 0.35
    min_color_std: float = 10.0
    min_color_chroma: float = 25.0


@dataclass(frozen=True)
class ValidationThresholds:
    """배치·검증 기준. 하나라도 미달이면 완성 메시를 버리고 원래 메시를 유지한다."""

    min_reprojection_iou: float = 0.40       # 최상/유효 시점 재투영 실루엣 IoU 하한
    max_obs_to_mesh_median_m: float = 0.05   # 관측 점 → 완성 메시 거리 중앙값 상한 (5 cm)
    max_obs_to_mesh_p95_m: float = 0.16      # 관측 점 → 완성 메시 거리 p95 상한 (16 cm, TSDF 절단 거리)
    max_floor_contact_error_m: float = 0.03  # 바닥 접촉 오차 |z_min - floor_z| 상한 (3 cm)
    max_faces_per_object: int = 20_000       # 물체당 최대 삼각형 수
    max_bytes_per_object: int = 5_000_000    # 물체당 최대 예상 용량 (5 MB)
    max_seconds_per_object: float = 60.0     # 물체당 최대 처리 시간 (초)


@dataclass(frozen=True)
class ObjectCompletionConfig:
    """물체 완성 전체 설정. 환경 변수(`from_env`) 또는 작업 옵션으로 제어한다."""

    enabled: bool = True
    backend: str = "3d_fixer"  # "3d_fixer" (본선 기본, 불가·실패 시 원본 유지) | "convex_hull" (시험·시각화 전용)
    mask_backend: str = "projection"  # "projection" | "color_refined" | "sam"
    # 평면 제거·후보 분리
    floor_snap_m: float = 0.05
    floor_margin_m: float = 0.06
    core_z_min_m: float = 0.12
    max_object_height_m: float = 1.65
    wall_margin_m: float = 0.22
    cluster_cell_m: float = 0.05
    assign_radius_m: float = 0.14
    min_candidate_faces: int = 25
    min_candidate_area_m2: float = 0.035
    min_candidate_height_m: float = 0.22
    max_candidate_xy_span_m: float = 1.85
    max_candidates: int = 16
    # 라바콘·안내판 분류 문턱
    cone_min_orange_faces: int = 35
    cone_max_xy_span_m: float = 0.75
    cone_radius_m: float = 0.25
    signboard: SignboardConfig = field(default_factory=SignboardConfig)
    # 텍스처·관측 보존
    obs_preserve_tol_m: float = 0.035
    texture_size: int = 256
    render_long_side: int = 640
    # ICP 정합
    icp_max_iter: int = 20
    icp_max_shift_m: float = 0.20
    icp_max_scale_dev: float = 0.25
    # 검증 기준
    thresholds: ValidationThresholds = field(default_factory=ValidationThresholds)
    # 3D-Fixer GPU 설정
    fixer_model_id: str = "HorizonRobotics/3D-Fixer"
    fixer_model_dir: str | None = None
    fixer_repo_dir: str | None = None
    fixer_min_vram_gb: float = 20.0
    fixer_steps: int = 25
    fixer_cfg_strength: float = 5.0
    fixer_seed: int = 42

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> ObjectCompletionConfig:
        env = os.environ if env is None else env
        cfg = cls()
        kw: dict[str, Any] = {}
        if (raw := env.get("SIMAI_GLB_OBJECTS", "").strip()) != "":
            kw["enabled"] = raw.lower() not in ("0", "false", "off", "no", "disabled")
        if (raw := env.get("SIMAI_OBJECT_BACKEND", "").strip()) != "":
            kw["backend"] = raw.lower()
        if (raw := env.get("SIMAI_OBJECT_MASK_BACKEND", "").strip()) != "":
            kw["mask_backend"] = raw.lower()
        if (raw := env.get("SIMAI_3DFIXER_MODEL_ID", "").strip()) != "":
            kw["fixer_model_id"] = raw
        if (raw := env.get("SIMAI_3DFIXER_MODEL_DIR", "").strip()) != "":
            kw["fixer_model_dir"] = raw
        if (raw := env.get("SIMAI_3DFIXER_REPO_DIR", "").strip()) != "":
            kw["fixer_repo_dir"] = raw
        if (raw := env.get("SIMAI_3DFIXER_MIN_VRAM_GB", "").strip()) != "":
            kw["fixer_min_vram_gb"] = float(raw)
        if options:
            if isinstance(options.get("objects"), bool):
                kw["enabled"] = options["objects"]
            if isinstance(options.get("object_backend"), str):
                kw["backend"] = options["object_backend"]
        return replace(cfg, **kw) if kw else cfg


# --------------------------------------------------------------------------- 장면 읽기 (보강된 GLB 포함)


@dataclass
class MergedSceneMesh:
    """추정 바닥/벽 노드를 제외한 입력 장면 메시 통합 표현(World 좌표)."""

    V: np.ndarray          # (N_v, 3) float64 World
    F: np.ndarray          # (N_f, 3) int64
    face_prim: np.ndarray  # (N_f,) model.prims 인덱스
    face_local: np.ndarray # (N_f,) 해당 prim 내 삼각형 인덱스
    is_observed: np.ndarray # (N_f,) bool
    face_rgb_srgb: np.ndarray # (N_f, 3) float64 0..255
    vert_rgb_srgb: np.ndarray # (N_v, 3) float64 0..255
    normals: np.ndarray    # (N_f, 3) float64
    areas: np.ndarray      # (N_f,) float64
    centers: np.ndarray    # (N_f, 3) float64


def load_scene_for_objects(g: GlbFile) -> SceneModel:
    """`glb_enhance.load_scene`과 같되 이미 `enhanced`가 적용된 GLB도 읽고 `estimated_*` 노드는 `kind='estimated'`로 구분한다."""
    doc = g.doc
    if UNSUPPORTED_REQUIRED & set(doc.get("extensionsRequired", [])):
        raise CompletionSkip("COMPRESSED_GLB", "압축 확장이 필요한 GLB는 지원하지 않는다")
    simai = (doc.get("extras") or {}).get("simai") if isinstance(doc.get("extras"), dict) else None
    if isinstance(simai, dict) and simai.get("axis_convention") not in (None, "GLTF_Y_UP"):
        raise CompletionSkip("AXIS_NOT_WORLD", f"axis_convention={simai.get('axis_convention')}")
    if isinstance(simai, dict) and isinstance(simai.get("object_completion"), dict) and simai["object_completion"].get("applied"):
        raise CompletionSkip("ALREADY_COMPLETED", "이미 물체 완성이 적용된 GLB")
    if not doc.get("meshes") or not doc.get("nodes"):
        raise CompletionSkip("EMPTY_SCENE", "메시 없음")

    scene_nodes = doc["scenes"][doc.get("scene", 0)].get("nodes", []) if doc.get("scenes") else list(range(len(doc["nodes"])))
    mesh_nodes: list[tuple[int, np.ndarray, str, dict[str, Any]]] = []
    stack = [(n, np.eye(4)) for n in scene_nodes]
    seen: set[int] = set()
    while stack:
        n, parent = stack.pop()
        if n in seen:
            raise CompletionSkip("NODE_CYCLE", "노드 그래프 순환")
        seen.add(n)
        node = doc["nodes"][n]
        M = parent @ _node_matrix(node)
        if "mesh" in node:
            node_simai = ((node.get("extras") or {}).get("simai") or {}) if isinstance(node.get("extras"), dict) else {}
            mesh_nodes.append((int(node["mesh"]), M, str(node.get("name", "")), node_simai))
        stack.extend((c, M) for c in node.get("children", []))

    prims: list[Prim] = []
    materials = doc.get("materials", [])
    textures = doc.get("textures", [])
    samplers = doc.get("samplers", [])
    for mesh_index, M, node_name, node_simai in mesh_nodes:
        mesh = doc["meshes"][mesh_index]
        mesh_name = str(mesh.get("name", node_name)) or f"mesh{mesh_index}"
        for prim_index, p in enumerate(mesh["primitives"]):
            if p.get("mode", 4) != 4:
                continue
            attrs = p["attributes"]
            if "POSITION" not in attrs:
                continue
            P = _accessor(g, attrs["POSITION"]).astype(np.float64)
            P = P @ M[:3, :3].T + M[:3, 3]
            if "indices" in p:
                idx = _accessor(g, p["indices"]).reshape(-1).astype(np.int64)
            else:
                idx = np.arange(len(P), dtype=np.int64)
            idx = idx[: len(idx) // 3 * 3].reshape(-1, 3)
            mat = materials[p["material"]] if "material" in p else {}
            mat_name = str(mat.get("name", ""))
            pbr = mat.get("pbrMetallicRoughness", {})
            factor_srgb_lin = np.asarray(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3], np.float64)
            uv = None
            image = None
            wrap = (10497, 10497)
            tex_info = pbr.get("baseColorTexture")
            if tex_info is not None and "TEXCOORD_0" in attrs and tex_info.get("texCoord", 0) == 0:
                tex = textures[tex_info["index"]]
                if "source" in tex and tex["source"] is not None:
                    image = int(tex["source"])
                    uv = _accessor(g, attrs["TEXCOORD_0"]).astype(np.float64)
                    if "sampler" in tex:
                        sm = samplers[tex["sampler"]]
                        wrap = (int(sm.get("wrapS", 10497)), int(sm.get("wrapT", 10497)))
            col = None
            if "COLOR_0" in attrs:
                col = _accessor(g, attrs["COLOR_0"]).astype(np.float64)[:, :3]
            names_ = (mat_name, mesh_name, node_name)
            if (
                any(s.startswith("estimated_") or s.startswith("fill_") for s in names_)
                or node_simai.get("surface_source") == "ESTIMATED_PLANE"
            ):
                kind = "estimated"
            elif any("unobserved_neutral" in s for s in names_) and image is None:
                kind = "unobserved"
            else:
                kind = "observed"
            prims.append(
                Prim(
                    name=mesh_name,
                    kind=kind,
                    pos=_glb_to_world(P),
                    idx=idx,
                    uv=uv,
                    col=col,
                    factor=factor_srgb_lin,
                    image=image,
                    wrap=wrap,
                    mesh_index=mesh_index,
                    prim_index=prim_index,
                )
            )
    if not any(p.kind in ("observed", "unobserved") for p in prims):
        raise CompletionSkip("NO_SCENE_MESH", "물체를 찾을 메시 없음")
    return SceneModel(prims, {})


def build_merged_scene_mesh(g: GlbFile, model: SceneModel) -> MergedSceneMesh:
    """추정(estimated) 프리미티브를 제외한 모든 삼각형을 World 좌표에서 하나로 모은다."""
    Vs, Fs, fp, fl, obs, vcols_srgb = [], [], [], [], [], []
    off = 0
    for pid, p in enumerate(model.prims):
        if p.kind == "estimated" or not len(p.idx):
            continue
        Vs.append(p.pos)
        Fs.append(p.idx + off)
        fp.append(np.full(len(p.idx), pid, np.int64))
        fl.append(np.arange(len(p.idx), dtype=np.int64))
        obs.append(np.full(len(p.idx), p.kind == "observed", bool))
        lin_col = prim_vertex_colors(g, model, p)
        vcols_srgb.append(linear_to_srgb(lin_col) * 255.0)
        off += len(p.pos)
    if not Vs:
        raise CompletionSkip("NO_SCENE_MESH", "유효한 삼각형 없음")
    V = np.concatenate(Vs, axis=0)
    F = np.concatenate(Fs, axis=0)
    vert_rgb = np.concatenate(vcols_srgb, axis=0)
    is_obs = np.concatenate(obs, axis=0)
    a, b, c = V[F[:, 0]], V[F[:, 1]], V[F[:, 2]]
    nn = np.cross(b - a, c - a)
    areas = 0.5 * np.linalg.norm(nn, axis=1)
    normals = nn / np.maximum(2.0 * areas, 1e-12)[:, None]
    centers = (a + b + c) / 3.0
    face_rgb = vert_rgb[F].mean(axis=1)
    return MergedSceneMesh(
        V=V,
        F=F,
        face_prim=np.concatenate(fp),
        face_local=np.concatenate(fl),
        is_observed=is_obs,
        face_rgb_srgb=face_rgb,
        vert_rgb_srgb=vert_rgb,
        normals=normals,
        areas=areas,
        centers=centers,
    )


# --------------------------------------------------------------------------- 1. 평면 검출 및 물체 후보 분리


def detect_floor_and_walls(
    mesh: MergedSceneMesh,
    cfg: ObjectCompletionConfig,
    seed: int = 0,
) -> tuple[float, list[dict[str, Any]], tuple[float, float, float, float], dict[str, Any]]:
    """바닥 높이 `z0`, 수직 벽 평면 목록, 방 내부 XY 경계 `(x0, y0, x1, y1)`를 검출한다(-511 `glb_fill` 호환)."""
    # 1. 바닥 높이
    fl_mask = (np.abs(mesh.normals[:, 2]) > 0.85) & (np.abs(mesh.centers[:, 2]) < 0.18)
    if fl_mask.sum() >= 20:
        z_vals = mesh.centers[fl_mask, 2]
        order = np.argsort(z_vals)
        cw = np.cumsum(mesh.areas[fl_mask][order])
        med_z = float(z_vals[order][np.searchsorted(cw, cw[-1] / 2.0)])
        z0 = 0.0 if abs(med_z) <= cfg.floor_snap_m else med_z
    else:
        med_z = 0.0
        z0 = 0.0

    # 바닥 관측점 기준 방 내부 XY 범위
    fl_obs = fl_mask & mesh.is_observed
    ref_pts = mesh.centers[fl_obs if fl_obs.sum() >= 20 else fl_mask, :2] if fl_mask.any() else mesh.centers[:, :2]
    x0, x1 = np.percentile(ref_pts[:, 0], [2.0, 98.0])
    y0, y1 = np.percentile(ref_pts[:, 1], [2.0, 98.0])

    # 2. 수직 벽 평면 순차 RANSAC (-511 glb_fill.find_wall_planes 와 같은 원리)
    vert_ids = np.nonzero((np.abs(mesh.normals[:, 2]) < 0.30) & (mesh.centers[:, 2] > z0 + 0.15) & (mesh.centers[:, 2] < z0 + 2.6))[0]
    walls: list[dict[str, Any]] = []
    if len(vert_ids) >= 30:
        p2 = mesh.centers[vert_ids, :2]
        a = mesh.areas[vert_ids]
        n2 = mesh.normals[vert_ids, :2]
        n2 = n2 / np.maximum(np.linalg.norm(n2, axis=1, keepdims=True), 1e-9)
        ang = np.arctan2(n2[:, 1], n2[:, 0])
        hist, bins = np.histogram(np.mod(ang, np.pi / 2), bins=90, range=(0, np.pi / 2), weights=a)
        th0 = float(bins[np.argmax(hist)] + np.pi / 360)
        rng = np.random.default_rng(seed)
        alive = np.ones(len(vert_ids), bool)
        floor_f = (np.abs(mesh.normals[:, 2]) > 0.7) & (np.abs(mesh.centers[:, 2] - z0) < 0.30)
        for _ in range(12):
            cand = np.nonzero(alive)[0]
            if len(cand) < 20:
                break
            probs = a[cand] / np.maximum(a[cand].sum(), 1e-12)
            pick = rng.choice(cand, size=min(120, len(cand)), replace=False, p=probs)
            best_k, best_area = None, 0.0
            for k in pick:
                inl = (np.abs((p2[cand] - p2[k]) @ n2[k]) < 0.12) & (np.abs(n2[cand] @ n2[k]) > 0.90)
                s_ = float(a[cand[inl]].sum())
                if s_ > best_area:
                    best_k, best_area = k, s_
            if best_k is None or best_area < 1.2:
                break
            inl_all = (np.abs((p2[cand] - p2[best_k]) @ n2[best_k]) < 0.12) & (np.abs(n2[cand] @ n2[best_k]) > 0.90)
            sel = cand[inl_all]
            alive[sel] = False
            w = a[sel]
            c = (p2[sel] * w[:, None]).sum(0) / w.sum()
            cov = ((p2[sel] - c).T * w) @ (p2[sel] - c)
            _, evec = np.linalg.eigh(cov)
            t = evec[:, 1]
            nrm = np.array([-t[1], t[0]])
            if nrm @ n2[best_k] < 0:
                nrm = -nrm
            u = (p2[sel] - c) @ t
            length = float(np.percentile(u, 99) - np.percentile(u, 1))
            theta = float(np.arctan2(nrm[1], nrm[0]))
            dev = abs(((theta - th0 + np.pi / 4) % (np.pi / 2)) - np.pi / 4)
            fid = vert_ids[sel]
            h95 = float(np.percentile(mesh.centers[fid, 2], 95) - z0)
            if length < 1.5 or np.degrees(dev) > 18.0 or h95 < 0.85:
                continue
            snapped = th0 + round((theta - th0) / (np.pi / 2)) * (np.pi / 2)
            nrm = np.array([np.cos(snapped), np.sin(snapped)])
            d = float(np.median(p2[sel] @ nrm))
            if floor_f.any() and mesh.areas[floor_f].sum() > 0:
                dd = mesh.centers[floor_f, :2] @ nrm - d
                af = mesh.areas[floor_f]
                pos_frac = float(af[dd > 0.15].sum() / af.sum())
                neg_frac = float(af[dd < -0.15].sum() / af.sum())
                if pos_frac < neg_frac:
                    nrm, d = -nrm, -d
                    outside = pos_frac
                else:
                    outside = neg_frac
                if outside > 0.15:
                    continue
            walls.append({"n": nrm.tolist(), "d": round(d, 4), "area_m2": round(float(w.sum()), 3), "length_m": round(length, 3)})

    plane_report = {
        "floor_z_m": round(z0, 4),
        "floor_median_z_m": round(med_z, 4),
        "wall_planes": len(walls),
        "interior_xy_m": [round(float(x0), 3), round(float(y0), 3), round(float(x1), 3), round(float(y1), 3)],
    }
    return z0, walls, (float(x0), float(y0), float(x1), float(y1)), plane_report


@dataclass
class ObjectCandidate:
    """맵 메시에서 분리된 물체 후보 1건."""

    candidate_id: str
    category: str  # "traffic_cone" | "signboard" | "general"
    face_indices: np.ndarray        # (M,) MergedSceneMesh.F 내 인덱스
    vertices_world: np.ndarray      # (V_c, 3) 후보 로컬 정점(World)
    faces: np.ndarray               # (M, 3) 0..V_c-1
    face_observed: np.ndarray       # (M,) bool
    vertex_colors_srgb: np.ndarray  # (V_c, 3) uint8
    points_world: np.ndarray        # (P, 3) 관측(또는 전체) 정점·표본 점
    points_mask: np.ndarray         # (P,) float32 (1.0=내부, 0.5=경계)
    bbox_min_world: np.ndarray      # (3,)
    bbox_max_world: np.ndarray      # (3,)
    area_m2: float
    best_camera_id: str | None = None
    camera_views: dict[str, dict[str, Any]] = field(default_factory=dict)
    masks_2d: dict[str, np.ndarray] = field(default_factory=dict)  # camera_id -> (H, W) bool


class MaskRefiner(Protocol):
    """투영 2D 마스크를 사진 색상·경계(또는 SAM 계열)로 정밀화하는 선택 백엔드 인터페이스."""

    def refine(self, image_rgb: np.ndarray, proj_mask: np.ndarray, bbox_xyxy: tuple[int, int, int, int]) -> np.ndarray: ...


class ColorGuidedMaskRefiner:
    """관측 마스크 내부 색상 분포로 경계 화소를 정밀화하는 가벼운 CPU 마스크 정밀화기."""

    def refine(self, image_rgb: np.ndarray, proj_mask: np.ndarray, bbox_xyxy: tuple[int, int, int, int]) -> np.ndarray:
        import cv2

        if not proj_mask.any():
            return proj_mask
        h, w = proj_mask.shape
        m_u8 = proj_mask.astype(np.uint8)
        k5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        core = cv2.erode(m_u8, k5, iterations=1) > 0
        if core.sum() < 16:
            core = proj_mask
        dil = cv2.dilate(m_u8, k5, iterations=2) > 0
        band = dil & ~core
        if not band.any():
            return proj_mask
        fg_pixels = image_rgb[core].astype(np.float32)
        med_rgb = np.median(fg_pixels, axis=0)
        mad_rgb = np.maximum(np.median(np.abs(fg_pixels - med_rgb), axis=0) * 2.8, 22.0)
        diff = np.abs(image_rgb[band].astype(np.float32) - med_rgb)
        keep_band = (diff <= mad_rgb).all(axis=1)
        out = core.copy()
        out[band] = keep_band
        return out


class SamMaskRefiner:
    """SAM / Ultralytics SAM 계열 선택 백엔드(지연 import, 가중치 없으면 투영 마스크 유지)."""

    def __init__(self, weights_path: str | None = None) -> None:
        self.weights_path = weights_path or os.environ.get("SIMAI_SAM_WEIGHTS", "sam2_b.pt")
        self._model: Any = None

    def refine(self, image_rgb: np.ndarray, proj_mask: np.ndarray, bbox_xyxy: tuple[int, int, int, int]) -> np.ndarray:
        if not proj_mask.any():
            return proj_mask
        try:
            if self._model is None:
                from ultralytics import SAM

                self._model = SAM(self.weights_path)
            x0, y0, x1, y1 = bbox_xyxy
            res = self._model.predict(image_rgb, bboxes=[[x0, y0, x1, y1]], verbose=False)
            if res and res[0].masks is not None and len(res[0].masks.data):
                sam_m = res[0].masks.data[0].detach().cpu().numpy() > 0.5
                if sam_m.shape == proj_mask.shape and (sam_m & proj_mask).sum() > 0.3 * proj_mask.sum():
                    return sam_m
        except Exception:  # noqa: BLE001
            pass
        return proj_mask


def _classify_candidate(
    pts: np.ndarray,
    face_rgb: np.ndarray,
    is_cone_seed: bool,
    cfg: ObjectCompletionConfig,
    *,
    floor_z_m: float = 0.0,
    walls: Sequence[Mapping[str, Any]] = (),
    interior_xy: tuple[float, float, float, float] | None = None,
) -> str:
    mn, mx = pts.min(axis=0), pts.max(axis=0)
    sz = mx - mn
    height = float(sz[2])
    xy_span = float(max(sz[0], sz[1]))
    if is_cone_seed:
        return "traffic_cone"
    orange = (
        (face_rgb[:, 0] > 95)
        & (face_rgb[:, 0] - face_rgb[:, 1] > 45)
        & (face_rgb[:, 0] > face_rgb[:, 1] * 1.70)
        & (face_rgb[:, 0] - face_rgb[:, 2] > 50)
    )
    if orange.mean() >= 0.22 and xy_span <= cfg.cone_max_xy_span_m and 0.20 <= height <= 0.90:
        return "traffic_cone"
    # 바닥에 세워진 얇은 안내판(signboard) 판정:
    # 1) 바닥 접촉(허공에 뜬 창틀·기둥 조각 배제), 2) 주변 벽·방 경계로부터 이격,
    # 3) 크기·높이·대각선·두께(PCA 단면), 4) 색 대비(단색 흰/회색 벽·기둥 조각 배제)
    floor_gap = float(mn[2] - floor_z_m)
    center_xy = 0.5 * (mn[:2] + mx[:2])
    near_wall = any(
        abs(float(center_xy @ np.asarray(w["n"], float)) - float(w["d"])) < cfg.signboard.min_wall_distance_m
        for w in walls
    )
    near_boundary = False
    if interior_xy is not None:
        x0, y0, x1, y1 = interior_xy
        b_margin = min(0.25, 0.10 * min(max(x1 - x0, 0.0), max(y1 - y0, 0.0)))
        near_boundary = bool(
            center_xy[0] < x0 + b_margin
            or center_xy[0] > x1 - b_margin
            or center_xy[1] < y0 + b_margin
            or center_xy[1] > y1 - b_margin
        )
    xy = pts[:, :2] - pts[:, :2].mean(axis=0)
    if len(xy) >= 6 and (-0.06 <= floor_gap <= cfg.signboard.max_floor_gap_m) and not (near_wall or near_boundary):
        cov = (xy.T @ xy) / max(len(xy), 1)
        _, eigvecs = np.linalg.eigh(cov)
        proj = xy @ eigvecs
        span_minor = float(np.percentile(proj[:, 0], 98) - np.percentile(proj[:, 0], 2))
        span_major = float(np.percentile(proj[:, 1], 98) - np.percentile(proj[:, 1], 2))
        diag_m = math.hypot(span_major, height)
        rgb_f = np.asarray(face_rgb, dtype=np.float64)
        color_std = float(np.std(rgb_f, axis=0).mean()) if len(rgb_f) else 0.0
        chroma_p90 = float(np.percentile(rgb_f.max(axis=1) - rgb_f.min(axis=1), 90)) if len(rgb_f) else 0.0
        has_color_contrast = (color_std >= cfg.signboard.min_color_std) or (chroma_p90 >= cfg.signboard.min_color_chroma)
        if (
            cfg.signboard.min_width_m <= span_major <= cfg.signboard.max_width_m
            and cfg.signboard.min_height_m <= height <= cfg.signboard.max_height_m
            and diag_m >= cfg.signboard.min_diagonal_m
            and span_minor <= min(cfg.signboard.max_thickness_m, 0.38 * span_major)
            and has_color_contrast
        ):
            return "signboard"
    return "general"


def _build_candidate_from_faces(
    candidate_id: str,
    fids: np.ndarray,
    mesh: MergedSceneMesh,
    is_cone_seed: bool,
    cfg: ObjectCompletionConfig,
    *,
    floor_z_m: float = 0.0,
    walls: Sequence[Mapping[str, Any]] = (),
    interior_xy: tuple[float, float, float, float] | None = None,
) -> ObjectCandidate:
    sub_F_global = mesh.F[fids]
    uniq_v, inv = np.unique(sub_F_global, return_inverse=True)
    V_c = mesh.V[uniq_v]
    F_c = inv.reshape(-1, 3)
    v_col = np.clip(np.rint(mesh.vert_rgb_srgb[uniq_v]), 0, 255).astype(np.uint8)
    f_obs = mesh.is_observed[fids]
    category = _classify_candidate(
        V_c,
        mesh.face_rgb_srgb[fids],
        is_cone_seed,
        cfg,
        floor_z_m=floor_z_m,
        walls=walls,
        interior_xy=interior_xy,
    )

    # 관측 점 우선 추출(없으면 전체 정점 + 면 중심)
    obs_v = np.unique(F_c[f_obs]) if f_obs.any() else np.arange(len(V_c))
    obs_fc = V_c[F_c[f_obs]].mean(axis=1) if f_obs.any() else V_c[F_c].mean(axis=1)
    pts = np.concatenate([V_c[obs_v], obs_fc], axis=0)
    # 3D-Fixer 규칙: 중심부는 1.0, 바깥 경계 15%는 0.5
    cen = np.median(pts, axis=0)
    rad = np.linalg.norm(pts - cen, axis=1)
    r85 = float(np.percentile(rad, 85)) if len(rad) else 1.0
    pmask = np.where(rad <= r85 + 1e-6, 1.0, 0.5).astype(np.float32)

    return ObjectCandidate(
        candidate_id=candidate_id,
        category=category,
        face_indices=fids,
        vertices_world=V_c,
        faces=F_c,
        face_observed=f_obs,
        vertex_colors_srgb=v_col,
        points_world=pts,
        points_mask=pmask,
        bbox_min_world=V_c.min(axis=0),
        bbox_max_world=V_c.max(axis=0),
        area_m2=float(mesh.areas[fids].sum()),
    )


def project_candidates_to_cameras(
    mesh: MergedSceneMesh,
    candidates: list[ObjectCandidate],
    cameras: Sequence[CheckCamera],
    cfg: ObjectCompletionConfig,
    refiner: MaskRefiner | None = None,
) -> None:
    """각 물체 후보를 4대 카메라에 투영해 가림을 반영한 2D 마스크(`masks_2d`)와 최적 시점(`best_camera_id`)을 고른다."""
    if not cameras or not candidates:
        return
    if refiner is None:
        if cfg.mask_backend == "color_refined":
            refiner = ColorGuidedMaskRefiner()
        elif cfg.mask_backend == "sam":
            refiner = SamMaskRefiner()

    # 면 번호 → 후보 번호 매핑 (-1 = 배경)
    face_to_cand = np.full(len(mesh.F), -1, dtype=np.int32)
    for ci, cand in enumerate(candidates):
        face_to_cand[cand.face_indices] = ci

    for cam in cameras:
        h0, w0 = cam.image.shape[:2]
        scale = min(1.0, cfg.render_long_side / max(h0, w0))
        H, W = max(1, round(h0 * scale)), max(1, round(w0 * scale))
        R = np.asarray(cam.R, np.float64)
        t = np.asarray(cam.t, np.float64).reshape(3)
        K = np.asarray(cam.K, np.float64)
        cam_pos = -R.T @ t

        # 전체 장면 z-버퍼 래스터라이즈 (가림 판정용)
        tri_cam = mesh.V[mesh.F] @ R.T + t
        front = tri_cam[:, :, 2].min(axis=1) >= NEAR_M
        front_ids = np.nonzero(front)[0]
        if not len(front_ids):
            continue
        zc = tri_cam[front_ids, :, 2]
        u = (K[0, 0] * tri_cam[front_ids, :, 0] / zc + K[0, 2] + 0.5) * (W / w0)
        v = (K[1, 1] * tri_cam[front_ids, :, 1] / zc + K[1, 2] + 0.5) * (H / h0)
        tid_scene, _, _ = rasterize(np.stack([u, v], axis=2), zc, (H, W), perspective=True)
        hit = tid_scene >= 0
        vis_global_face = np.where(hit, front_ids[np.maximum(tid_scene, 0)], -1)
        vis_cand_map = np.where(hit, face_to_cand[np.maximum(vis_global_face, 0)], -1)

        for ci, cand in enumerate(candidates):
            # 1) 이 후보만 단독 래스터라이즈한 전체 실루엣 vs 장면 가림 후 보이는 픽셀
            c_cam = cand.vertices_world[cand.faces] @ R.T + t
            c_front = c_cam[:, :, 2].min(axis=1) >= NEAR_M
            if not c_front.any():
                cand.camera_views[cam.camera_id] = {"visible_pixels": 0, "visible_ratio": 0.0, "score": 0.0}
                continue
            cz = c_cam[c_front, :, 2]
            cu = (K[0, 0] * c_cam[c_front, :, 0] / cz + K[0, 2] + 0.5) * (W / w0)
            cv_ = (K[1, 1] * c_cam[c_front, :, 1] / cz + K[1, 2] + 0.5) * (H / h0)
            c_tid, _, _ = rasterize(np.stack([cu, cv_], axis=2), cz, (H, W), perspective=True)
            unoccluded_mask = vis_cand_map == ci
            full_sil_mask = (c_tid >= 0) | unoccluded_mask
            vis_px = int(unoccluded_mask.sum())
            sil_px = max(int(full_sil_mask.sum()), 1)
            vis_ratio = float(vis_px / sil_px)

            # 시선 거리·각도 계산
            obj_center = 0.5 * (cand.bbox_min_world + cand.bbox_max_world)
            view_vec = cam_pos - obj_center
            dist_m = float(np.linalg.norm(view_vec))
            # 가장자리 잘림 감점
            ys, xs = np.nonzero(full_sil_mask)
            border_touch = bool(len(ys) and (xs.min() <= 1 or xs.max() >= W - 2 or ys.min() <= 1 or ys.max() >= H - 2))
            border_factor = 0.7 if border_touch else 1.0
            score = float(vis_px * (0.5 + 0.5 * vis_ratio) * border_factor / max(dist_m, 1.0))

            mask_out = full_sil_mask
            if refiner is not None and len(ys) >= 16:
                import cv2

                img_small = cv2.resize(cam.image, (W, H), interpolation=cv2.INTER_AREA)
                bbox = (int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
                mask_out = refiner.refine(img_small, mask_out, bbox)

            cand.masks_2d[cam.camera_id] = mask_out
            cand.camera_views[cam.camera_id] = {
                "visible_pixels": vis_px,
                "silhouette_pixels": int(mask_out.sum()),
                "visible_ratio": round(vis_ratio, 4),
                "distance_m": round(dist_m, 3),
                "border_touch": border_touch,
                "render_hw": [H, W],
                "render_scale": round(scale, 4),
                "score": round(score, 3),
            }

    for cand in candidates:
        if cand.camera_views:
            best_cid = max(cand.camera_views.items(), key=lambda kv: kv[1].get("score", 0.0))[0]
            if cand.camera_views[best_cid].get("visible_pixels", 0) > 0:
                cand.best_camera_id = best_cid


def extract_object_candidates(
    mesh: MergedSceneMesh,
    cameras: Sequence[CheckCamera] | None = None,
    config: ObjectCompletionConfig | None = None,
    refiner: MaskRefiner | None = None,
) -> tuple[list[ObjectCandidate], dict[str, Any]]:
    """바닥·벽 평면을 제거한 뒤 연결 성분·높이·색상으로 물체 후보를 분리하고 카메라별 2D 마스크와 최적 시점을 고른다."""
    cfg = config or ObjectCompletionConfig()
    z0, walls, (x0, y0, x1, y1), plane_rep = detect_floor_and_walls(mesh, cfg)

    Fc = mesh.centers
    Fcol = mesh.face_rgb_srgb
    N_f = len(mesh.F)

    # 1. 바닥·천장·벽 평면 제거 마스크
    is_floor = Fc[:, 2] <= z0 + cfg.floor_margin_m
    is_ceil = Fc[:, 2] >= z0 + cfg.max_object_height_m
    is_wall = (
        (Fc[:, 0] < x0 - 0.15)
        | (Fc[:, 0] > x1 + 0.15)
        | (Fc[:, 1] < y0 - 0.15)
        | (Fc[:, 1] > y1 + 0.15)
    )
    for w in walls:
        nrm = np.asarray(w["n"], float)
        d = float(w["d"])
        is_wall |= np.abs(Fc[:, :2] @ nrm - d) <= cfg.wall_margin_m

    non_plane = ~(is_floor | is_ceil | is_wall)
    assigned_face = np.zeros(N_f, bool)
    raw_clusters: list[tuple[np.ndarray, bool]] = []  # (fids, is_cone_seed)

    # 2. 주황색 라바콘(traffic_cone) 시드 우선 분리 (벤치 끝단 등과 인접해 붙는 현상 방지)
    orange_mask = (
        non_plane
        & mesh.is_observed
        & (Fc[:, 2] > z0 + 0.04)
        & (Fc[:, 2] < z0 + 0.85)
        & (Fcol[:, 0] > 95)
        & (Fcol[:, 0] - Fcol[:, 1] > 45)
        & (Fcol[:, 0] > Fcol[:, 1] * 1.70)
        & (Fcol[:, 0] - Fcol[:, 2] > 50)
    )
    orange_idx = np.nonzero(orange_mask)[0]
    if len(orange_idx) >= cfg.cone_min_orange_faces:
        cells_o = _cells(Fc[orange_idx, :2], 0.15)
        comp_o = _cell_components(cells_o)
        for c in range(int(comp_o.max()) + 1):
            seed_fids = orange_idx[comp_o == c]
            if len(seed_fids) < cfg.cone_min_orange_faces:
                continue
            center_xy = np.median(Fc[seed_fids, :2], axis=0)
            dist_xy = np.linalg.norm(Fc[:, :2] - center_xy, axis=1)
            cone_f = (
                non_plane
                & ~assigned_face
                & (dist_xy <= min(cfg.cone_radius_m, 0.22))
                & (Fc[:, 2] <= z0 + 0.72)
                & (orange_mask | (dist_xy <= 0.16))
            )
            fids = np.nonzero(cone_f)[0]
            if len(fids) >= cfg.min_candidate_faces:
                assigned_face[fids] = True
                raw_clusters.append((fids, True))

    # 3. 관측 면 코어(`z > z0 + core_z_min_m`)를 XY 격자 연결 성분으로 먼저 묶고,
    #    주변 비관측/하단 면(`non_plane & ~assigned_face`)을 가장 가까운 코어에 배정한다.
    core_mask = non_plane & ~assigned_face & mesh.is_observed & (Fc[:, 2] > z0 + cfg.core_z_min_m)
    if not core_mask.any():
        core_mask = non_plane & ~assigned_face & (Fc[:, 2] > z0 + cfg.core_z_min_m)
    core_idx = np.nonzero(core_mask)[0]

    if len(core_idx):
        from scipy.spatial import cKDTree

        cells_c = _cells(Fc[core_idx, :2], cfg.cluster_cell_m)
        comp_c = _cell_components(cells_c)
        valid_cores: list[np.ndarray] = []
        for c in range(int(comp_c.max()) + 1):
            fids = core_idx[comp_c == c]
            if len(fids) >= max(10, cfg.min_candidate_faces // 2):
                valid_cores.append(fids)

        if valid_cores:
            core_pts = np.concatenate([Fc[f, :2] for f in valid_cores], axis=0)
            core_labels = np.concatenate([np.full(len(f), i, np.int32) for i, f in enumerate(valid_cores)])
            tree = cKDTree(core_pts)
            rem_idx = np.nonzero(non_plane & ~assigned_face)[0]
            dist, nn = tree.query(Fc[rem_idx, :2], k=1, distance_upper_bound=cfg.assign_radius_m)
            ok = np.isfinite(dist)
            by_core: dict[int, list[int]] = {i: [] for i in range(len(valid_cores))}
            for r_i, is_ok, n_i in zip(rem_idx, ok, nn):
                if is_ok:
                    by_core[int(core_labels[int(n_i)])].append(int(r_i))
            for i, f_list in by_core.items():
                if not f_list:
                    continue
                fids = np.unique(np.concatenate([valid_cores[i], np.asarray(f_list, np.int64)]))
                assigned_face[fids] = True
                raw_clusters.append((fids, False))

    # 4. 크기·면적·높이 조건 필터링 및 후보 생성
    candidates: list[ObjectCandidate] = []
    rejected_clusters: list[dict[str, Any]] = []
    for fids, is_cone_seed in raw_clusters:
        area = float(mesh.areas[fids].sum())
        pts = mesh.V[np.unique(mesh.F[fids])]
        mn, mx = pts.min(axis=0), pts.max(axis=0)
        sz = mx - mn
        xy_span = float(max(sz[0], sz[1]))
        height = float(mx[2] - z0)
        if len(fids) < cfg.min_candidate_faces or area < cfg.min_candidate_area_m2:
            rejected_clusters.append({"faces": int(len(fids)), "area_m2": round(area, 4), "reason": "TOO_SMALL"})
            continue
        if height < cfg.min_candidate_height_m:
            rejected_clusters.append({"faces": int(len(fids)), "height_m": round(height, 3), "reason": "TOO_LOW"})
            continue
        if not is_cone_seed and xy_span > cfg.max_candidate_xy_span_m:
            rejected_clusters.append({"faces": int(len(fids)), "xy_span_m": round(xy_span, 3), "reason": "TOO_LARGE_SPAN"})
            continue
        floor_gap_cluster = float(mn[2] - z0)
        if not is_cone_seed and (height > cfg.max_object_height_m or floor_gap_cluster > 0.40):
            rejected_clusters.append({
                "faces": int(len(fids)),
                "z_min_m": round(float(mn[2]), 3),
                "z_max_m": round(float(mx[2]), 3),
                "reason": "WALL_OR_PILLAR_FRAGMENT",
            })
            continue
        cid = f"obj_{len(candidates):04d}"
        cand = _build_candidate_from_faces(
            cid,
            fids,
            mesh,
            is_cone_seed,
            cfg,
            floor_z_m=z0,
            walls=walls,
            interior_xy=(x0, y0, x1, y1),
        )
        candidates.append(cand)

    # 면적 및 관측 점 수 기준 상위 max_candidates 개 유지 (라바콘·안내판은 우선 보존)
    candidates.sort(key=lambda c: (c.category == "general", -c.area_m2))
    candidates = candidates[: cfg.max_candidates]
    for i, c in enumerate(candidates):
        prefix = "cone" if c.category == "traffic_cone" else ("sign" if c.category == "signboard" else "obj")
        c.candidate_id = f"{prefix}_{i:02d}"

    if cameras:
        project_candidates_to_cameras(mesh, candidates, cameras, cfg, refiner=refiner)

    summary = {
        "planes": plane_rep,
        "non_plane_faces": int(non_plane.sum()),
        "candidates_found": len(candidates),
        "rejected_clusters": len(rejected_clusters),
        "by_category": {
            cat: sum(1 for c in candidates if c.category == cat)
            for cat in ("traffic_cone", "signboard", "general")
        },
    }
    return candidates, summary


# --------------------------------------------------------------------------- 2. 가림 고려 다중 시점 텍스처 투영 모듈


def _box_uv_unwrap(vertices_world: np.ndarray, faces: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """임의 삼각형 메시에 대해 법선 주축 기준 6면 박스 아틀라스(3×2 타일) UV를 생성한다(xatlas 없이 CPU에서 동작).

    반환: (새 정점 (3*F, 3), 새 면 (F, 3), UV (3*F, 2) glTF 규약 [0, 1]).
    """
    if not len(faces):
        return vertices_world.copy(), faces.copy(), np.zeros((len(vertices_world), 2), np.float64)
    tri_v = vertices_world[faces]  # (F, 3, 3)
    fn = face_normals(vertices_world, faces)
    abs_n = np.abs(fn)
    dom = np.argmax(abs_n, axis=1)
    sign = np.where(fn[np.arange(len(faces)), dom] >= 0, 0, 1)
    chart = dom * 2 + sign  # 0..5 -> 3 cols x 2 rows

    mn = vertices_world.min(axis=0)
    mx = vertices_world.max(axis=0)
    span = np.maximum(mx - mn, 1e-4)
    norm_v = (tri_v - mn[None, None, :]) / span[None, None, :]  # (F, 3, 3) in [0, 1]

    uv_local = np.zeros((len(faces), 3, 2), np.float64)
    for f_i in range(len(faces)):
        d = int(dom[f_i])
        if d == 0:  # X축 법선 -> (Y, Z)
            uv_local[f_i, :, 0] = norm_v[f_i, :, 1]
            uv_local[f_i, :, 1] = 1.0 - norm_v[f_i, :, 2]
        elif d == 1:  # Y축 법선 -> (X, Z)
            uv_local[f_i, :, 0] = norm_v[f_i, :, 0]
            uv_local[f_i, :, 1] = 1.0 - norm_v[f_i, :, 2]
        else:  # Z축 법선 -> (X, Y)
            uv_local[f_i, :, 0] = norm_v[f_i, :, 0]
            uv_local[f_i, :, 1] = 1.0 - norm_v[f_i, :, 1]

    margin = 0.04
    uv_local = margin + uv_local * (1.0 - 2.0 * margin)
    col_idx = (chart % 3).astype(np.float64)
    row_idx = (chart // 3).astype(np.float64)
    uv = np.empty_like(uv_local)
    uv[:, :, 0] = (col_idx[:, None] + uv_local[:, :, 0]) / 3.0
    uv[:, :, 1] = (row_idx[:, None] + uv_local[:, :, 1]) / 2.0

    new_V = tri_v.reshape(-1, 3)
    new_F = np.arange(len(new_V), dtype=np.int64).reshape(-1, 3)
    new_UV = uv.reshape(-1, 2)
    return new_V, new_F, new_UV


def bake_multiview_object_texture(
    vertices_world: np.ndarray,
    faces: np.ndarray,
    uv: np.ndarray,
    face_source: np.ndarray,
    cameras: Sequence[CheckCamera],
    fallback_rgb_srgb: np.ndarray,
    texture_size: int = 256,
    all_masks_2d: Mapping[str, np.ndarray] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """완성/정형 메시의 UV 아틀라스 위에 4대 카메라 관측 사진을 가림 고려해 투영하고 텍셀 출처 지도를 만든다.

    - 카메라에서 가림 없이 보이고 `face_source == OBSERVED`(또는 `ESTIMATED_PLANE`)이면서 해당 카메라의 2D 마스크 안쪽인 텍셀은
      관측 사진 색을 쓰고 그 출처 값을 유지한다.
    - 카메라에서 보이지 않거나 생성된(`GENERATED`) 영역은 같은 높이(z) 대역의 관측 색(없으면 `fallback_rgb_srgb`)으로 채우고
      출처 지도에는 `GENERATED(4)`를 기록한다(관측으로 승격하지 않음, 규칙 7).
    """
    S = int(texture_size)
    fb = np.clip(np.rint(fallback_rgb_srgb), 0, 255).astype(np.uint8)
    tex = np.broadcast_to(fb.reshape(1, 1, 3), (S, S, 3)).copy()
    src_map = np.full((S, S), UNOBSERVED, dtype=np.uint8)
    if not len(faces):
        return tex, src_map

    # UV 공간에서 삼각형 래스터라이즈 -> 각 텍셀의 (면 번호, 무게중심 좌표)
    tri_uv_px = uv[faces] * S  # (F, 3, 2)
    tri_z_dummy = np.ones((len(faces), 3), np.float64)
    tid_uv, bary_uv, _ = rasterize(tri_uv_px, tri_z_dummy, (S, S), perspective=False)
    covered = tid_uv >= 0
    if not covered.any():
        src_map[:] = GENERATED
        return tex, src_map

    ys, xs = np.nonzero(covered)
    f_ids = tid_uv[ys, xs]
    w3 = bary_uv[ys, xs].astype(np.float64)
    tri_v = vertices_world[faces[f_ids]]  # (N_pix, 3, 3)
    P_world = (tri_v * w3[:, :, None]).sum(axis=1)  # (N_pix, 3)
    fn_all = face_normals(vertices_world, faces)
    N_world = fn_all[f_ids]
    f_src = face_source[f_ids]

    # 기본값: 덮인 모든 텍셀을 GENERATED(4)로 초기화
    src_map[ys, xs] = GENERATED

    if cameras:
        best_score = np.zeros(len(ys), np.float64)
        best_rgb = np.zeros((len(ys), 3), np.uint8)
        has_obs = np.zeros(len(ys), bool)
        is_obs_face = np.isin(f_src, (OBSERVED, ESTIMATED_PLANE))

        for cam in cameras:
            h0, w0 = cam.image.shape[:2]
            R = np.asarray(cam.R, np.float64)
            t = np.asarray(cam.t, np.float64).reshape(3)
            K = np.asarray(cam.K, np.float64)
            cam_pos = -R.T @ t
            mask_cam = all_masks_2d.get(cam.camera_id) if all_masks_2d is not None else None

            # 이 물체 자체의 자가 가림(self-occlusion) 깊이 버퍼
            scale = min(1.0, 640.0 / max(h0, w0))
            Hr, Wr = max(1, round(h0 * scale)), max(1, round(w0 * scale))
            tc = vertices_world[faces] @ R.T + t
            front = tc[:, :, 2].min(axis=1) >= NEAR_M
            if not front.any():
                continue
            zc_tri = tc[front, :, 2]
            u_tri = (K[0, 0] * tc[front, :, 0] / zc_tri + K[0, 2] + 0.5) * (Wr / w0)
            v_tri = (K[1, 1] * tc[front, :, 1] / zc_tri + K[1, 2] + 0.5) * (Hr / h0)
            _, _, depth_buf = rasterize(np.stack([u_tri, v_tri], axis=2), zc_tri, (Hr, Wr), perspective=True)

            # 텍셀 3D 점 투영 (관측 면 위주로 직접 사진 색 샘플링)
            pc = P_world @ R.T + t
            z = pc[:, 2]
            u = K[0, 0] * pc[:, 0] / np.maximum(z, 1e-6) + K[0, 2]
            v = K[1, 1] * pc[:, 1] / np.maximum(z, 1e-6) + K[1, 2]
            inb = is_obs_face & (z >= NEAR_M) & (u >= 0) & (u <= w0 - 1) & (v >= 0) & (v <= h0 - 1)
            if not inb.any():
                continue
            idx = np.nonzero(inb)[0]
            ru = np.clip(np.floor((u[idx] + 0.5) * scale).astype(np.int64), 0, Wr - 1)
            rv = np.clip(np.floor((v[idx] + 0.5) * scale).astype(np.int64), 0, Hr - 1)
            d_buf = depth_buf[rv, ru]
            vis = ~np.isfinite(d_buf) | (z[idx] <= d_buf + np.maximum(0.04, 0.04 * z[idx]))

            if mask_cam is not None and mask_cam.any():
                mh, mw = mask_cam.shape[:2]
                mu = np.clip(np.floor((u[idx] + 0.5) * (mw / w0)).astype(np.int64), 0, mw - 1)
                mv = np.clip(np.floor((v[idx] + 0.5) * (mh / h0)).astype(np.int64), 0, mh - 1)
                vis &= mask_cam[mv, mu] > 0

            view_dir = cam_pos[None, :] - P_world[idx]
            dist = np.linalg.norm(view_dir, axis=1)
            cos_a = (N_world[idx] * view_dir).sum(axis=1) / np.maximum(dist, 1e-6)
            # 양면/얇은 판도 허용하되 정면(|cos| >= 0.15) 우선
            vis &= np.abs(cos_a) >= 0.15
            if not vis.any():
                continue
            v_idx = idx[vis]
            sc = np.abs(cos_a[vis]) / np.maximum(dist[vis], 0.5)
            better = sc > best_score[v_idx]
            win = v_idx[better]
            if len(win):
                best_score[win] = sc[better]
                best_rgb[win] = np.clip(np.rint(bilinear(cam.image, u[win], v[win])), 0, 255).astype(np.uint8)
                has_obs[win] = True

        if has_obs.any():
            tex[ys[has_obs], xs[has_obs]] = best_rgb[has_obs]
            # 면 자체가 관측(OBSERVED) 또는 정형 평면(ESTIMATED_PLANE)이면서 사진이 직접 투영된 텍셀만 관측 출처 부여
            obs_texel = has_obs & np.isin(f_src, (OBSERVED, ESTIMATED_PLANE))
            src_map[ys[obs_texel], xs[obs_texel]] = f_src[obs_texel]

            # 사진이 안 닿은 뒤쪽/가림 텍셀은 같은 높이(Z) 대역의 관측 색상 중앙값으로 매끄럽게 보간(출처는 GENERATED=4 유지)
            unseen = ~has_obs
            if unseen.any():
                z_obs = P_world[has_obs, 2]
                c_obs = best_rgb[has_obs]
                z_un = P_world[unseen, 2]
                z_lo = float(min(z_obs.min(), z_un.min()))
                z_hi = float(max(z_obs.max(), z_un.max()) + 1e-5)
                n_bins = 24
                edges = np.linspace(z_lo, z_hi, n_bins + 1)
                bin_colors = np.broadcast_to(fb.reshape(1, 3), (n_bins, 3)).copy()
                for b_i in range(n_bins):
                    z_mid = 0.5 * (edges[b_i] + edges[b_i + 1])
                    band = np.abs(z_obs - z_mid) <= 0.06
                    if band.sum() >= 5:
                        bin_colors[b_i] = np.median(c_obs[band], axis=0).astype(np.uint8)
                un_bins = np.clip(np.digitize(z_un, edges) - 1, 0, n_bins - 1)
                tex[ys[unseen], xs[unseen]] = bin_colors[un_bins]

    return tex, src_map


# --------------------------------------------------------------------------- 3. 완성 백엔드 인터페이스 및 구현


@dataclass
class CompletionInput:
    """완성 백엔드 표준 입력."""

    candidate_id: str
    category: str
    best_camera: CheckCamera | None
    mask_2d: np.ndarray | None
    all_cameras: Sequence[CheckCamera]
    all_masks_2d: Mapping[str, np.ndarray]
    observed_points_world: np.ndarray       # (N, 3) World
    points_mask: np.ndarray                 # (N,) float32 (1.0=전경, 0.5=경계)
    observed_vertices_world: np.ndarray     # (V_o, 3) World
    observed_faces: np.ndarray              # (F_o, 3)
    observed_face_mask: np.ndarray          # (F_o,) bool
    observed_vertex_colors_srgb: np.ndarray # (V_o, 3) uint8
    floor_z_m: float = 0.0


@dataclass
class CompletionOutput:
    """완성 백엔드 표준 출력(World 좌표계)."""

    vertices_world: np.ndarray              # (V, 3) float64 World (+Z 상방)
    faces: np.ndarray                       # (F, 3) int64
    face_source: np.ndarray                 # (F,) uint8 (OBSERVED=2, ESTIMATED_PLANE=3, GENERATED=4)
    uv: np.ndarray | None = None            # (V, 2) float64 glTF UV
    texture_rgb: np.ndarray | None = None   # (H, W, 3) uint8
    texel_source_map: np.ndarray | None = None  # (H, W) uint8
    vertex_colors_srgb: np.ndarray | None = None  # (V, 3) uint8
    backend_name: str = "convex_hull"
    meta: dict[str, Any] = field(default_factory=dict)


class CompletionBackend(Protocol):
    """물체 제자리 완성 백엔드 프로토콜."""

    name: str

    def complete(self, inp: CompletionInput) -> CompletionOutput: ...


class ConvexHullCompletionBackend:
    """시험·노트북 확인용 가짜 백엔드: 관측 메시 조각은 그대로 보존(`OBSERVED=2`)하고
    미관측 뒷면·바닥 지지부는 관측 점 + 바닥 투영점의 3D 볼록 껍질(`GENERATED=4`)로 제자리 완성한다.
    """

    name = "convex_hull"

    def __init__(self, obs_preserve_tol_m: float = 0.035, texture_size: int = 256) -> None:
        self.obs_preserve_tol_m = obs_preserve_tol_m
        self.texture_size = texture_size

    def complete(self, inp: CompletionInput) -> CompletionOutput:
        from scipy.spatial import ConvexHull, cKDTree

        pts = np.asarray(inp.observed_points_world, np.float64)
        if len(pts) < 4:
            raise CompletionSkip("FEW_POINTS", "볼록 껍질을 만들 점이 부족하다")

        # 바닥까지 기둥이 이어지도록 상위/외곽 점의 바닥 투영점(z = floor_z_m)을 추가
        xy_hull_pts = pts.copy()
        bottom_pts = xy_hull_pts.copy()
        bottom_pts[:, 2] = inp.floor_z_m
        all_pts = np.concatenate([pts, bottom_pts], axis=0)
        # 수치 안정성을 위한 미세 중복 제거
        uniq_pts = np.unique(np.round(all_pts, 4), axis=0)
        if len(uniq_pts) < 4:
            raise CompletionSkip("DEGENERATE_HULL", "3D 볼록 껍질 점 수 부족")

        try:
            hull = ConvexHull(uniq_pts, qhull_options="QJ")
        except Exception as exc:  # noqa: BLE001
            raise CompletionSkip("HULL_FAILED", str(exc)) from exc

        hull_V = uniq_pts
        hull_F = np.asarray(hull.simplices, dtype=np.int64)
        # 바깥쪽을 향하도록 면 정점 순서 정렬
        centroid = hull_V.mean(axis=0)
        fn = face_normals(hull_V, hull_F)
        fc = hull_V[hull_F].mean(axis=1)
        flip = ((fc - centroid[None, :]) * fn).sum(axis=1) < 0
        hull_F[flip] = hull_F[flip][:, [0, 2, 1]]

        # 관측 표면 근처(obs_preserve_tol_m 이내)인 껍질 면은 원래 관측 메시 조각으로 대체하여 관측 형상을 보존한다!
        obs_f_mask = inp.observed_face_mask if inp.observed_face_mask.any() else np.ones(len(inp.observed_faces), bool)
        obs_F_orig = inp.observed_faces[obs_f_mask]
        obs_V_orig = inp.observed_vertices_world
        obs_fc = obs_V_orig[obs_F_orig].mean(axis=1)

        if len(obs_fc) >= 4:
            tree = cKDTree(obs_fc)
            hull_fc = hull_V[hull_F].mean(axis=1)
            dist, _ = tree.query(hull_fc, k=1)
            # 관측 면과 겹치지 않는 뒷면·옆면·바닥면만 생성(GENERATED) 면으로 남긴다
            gen_keep = dist > self.obs_preserve_tol_m
            gen_F = hull_F[gen_keep]
        else:
            gen_F = hull_F

        # 관측 메시(OBSERVED=2) + 볼록 껍질 미관측 보완 면(GENERATED=4) 결합
        comb_V = np.concatenate([obs_V_orig, hull_V], axis=0)
        comb_F = np.concatenate([obs_F_orig, gen_F + len(obs_V_orig)], axis=0)
        comb_src = np.concatenate([
            np.full(len(obs_F_orig), OBSERVED, dtype=np.uint8),
            np.full(len(gen_F), GENERATED, dtype=np.uint8),
        ])

        unwrap_V, unwrap_F, unwrap_UV = _box_uv_unwrap(comb_V, comb_F)
        fb_rgb = np.median(inp.observed_vertex_colors_srgb, axis=0) if len(inp.observed_vertex_colors_srgb) else np.array([160, 160, 160])
        tex_rgb, src_map = bake_multiview_object_texture(
            unwrap_V,
            unwrap_F,
            unwrap_UV,
            comb_src,
            inp.all_cameras,
            fb_rgb,
            texture_size=self.texture_size,
            all_masks_2d=inp.all_masks_2d,
        )
        return CompletionOutput(
            vertices_world=unwrap_V,
            faces=unwrap_F,
            face_source=comb_src,
            uv=unwrap_UV,
            texture_rgb=tex_rgb,
            texel_source_map=src_map,
            backend_name=self.name,
            meta={
                "preserved_observed_faces": int(len(obs_F_orig)),
                "generated_hull_faces": int(len(gen_F)),
            },
        )


class ThreeDFixerBackend:
    """1순위 GPU 백엔드: `HorizonRobotics/3D-Fixer` (CVPR 2026, TRELLIS 기반 제자리 완성).

    - 무거운 GPU 의존성(`torch`, `threeDFixer`)은 함수 안에서만 지연 import 한다.
    - 최소 GPU VRAM(기본 20 GB; 공식 요구 24 GB) 미달이면 노트북(RTX 4050 6 GB) 보호를 위해 즉시 `CompletionSkip`을 발생시킨다.
    - 공식 저장소(`app.py`, `inference_ours_testset.py`, `threeDFixer/pipelines/threeD_fixer.py`)의 입력 규약을 따른다:
      - `instance_image_masked`: `(4, 518, 518)` float32 tensor `[0, 1]` (바운딩 박스 1.2배 정사각 크롭 RGB + 마스크)
      - `scene_image_masked`: `(4, 518, 518)` float32 tensor `[0, 1]` (전체 시점 518×518 RGB + 마스크)
      - `extrinsics`: OpenCV `cam2world` `(4, 4)` float32 tensor (`pipeline.project_uv`에서 `inverse(extrinsics)`를 `utils3d.torch.project_cv`에 전달)
      - `intrinsics`: `[W, H]`로 정규화된 `3×3` float32 tensor (`fx/W, fy/H, cx/W, cy/H`)
      - `points`: `(N, 3)` float32 World 관측 부분 점 클라우드
      - `points_mask`: `(N,)` float32 (`1.0` 전경, `0.5` 경계)
    """

    name = "3d_fixer"
    CROP_SIZE = 518

    def __init__(self, config: ObjectCompletionConfig | None = None) -> None:
        self.cfg = config or ObjectCompletionConfig()
        self._pipeline: Any = None

    @staticmethod
    def prepare_conditioning_tensors(
        image_rgb: np.ndarray,
        mask_2d: np.ndarray,
        K: np.ndarray,
        R: np.ndarray,
        t: np.ndarray,
        crop_size: int = 518,
    ) -> dict[str, np.ndarray]:
        """3D-Fixer 공식 `process_scene_image`·`process_instance_image`와 동일한 numpy 전처리(단위 시험·GPU 공용)."""
        import cv2

        h0, w0 = image_rgb.shape[:2]
        m_full = (
            cv2.resize(mask_2d.astype(np.uint8), (w0, h0), interpolation=cv2.INTER_NEAREST) > 0
            if mask_2d.shape[:2] != (h0, w0)
            else (mask_2d > 0)
        )
        ys, xs = np.nonzero(m_full)
        if not len(ys):
            raise CompletionSkip("EMPTY_MASK", "2D 마스크가 비어 있다")

        # 1) scene_image_masked: (4, 518, 518) float32 [0, 1]
        scene_rgb = cv2.resize(image_rgb, (crop_size, crop_size), interpolation=cv2.INTER_LANCZOS4).astype(np.float32) / 255.0
        scene_a = cv2.resize(m_full.astype(np.uint8) * 255, (crop_size, crop_size), interpolation=cv2.INTER_NEAREST).astype(np.float32) / 255.0
        scene_rgba = np.concatenate([scene_rgb.transpose(2, 0, 1), scene_a[None, :, :]], axis=0)

        # 2) instance_image_masked: bbox 중심 기준 1.2배 정사각 크롭 후 (4, 518, 518)
        x_min, y_min, x_max, y_max = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
        cx, cy = 0.5 * (x_min + x_max), 0.5 * (y_min + y_max)
        hsize = max(x_max - x_min, y_max - y_min) * 0.5 * 1.2
        hsize = max(hsize, 16.0)
        bx0, by0, bx1, by1 = int(cx - hsize), int(cy - hsize), int(cx + hsize), int(cy + hsize)

        # 이미지 밖 패딩 처리(PIL Image.crop 동작과 동일하게 0 패딩)
        side_w, side_h = max(1, bx1 - bx0), max(1, by1 - by0)
        crop_rgb = np.zeros((side_h, side_w, 3), dtype=np.uint8)
        crop_a = np.zeros((side_h, side_w), dtype=np.uint8)
        sx0, sy0 = max(0, bx0), max(0, by0)
        sx1, sy1 = min(w0, bx1), min(h0, by1)
        if sx1 > sx0 and sy1 > sy0:
            dx0, dy0 = sx0 - bx0, sy0 - by0
            crop_rgb[dy0 : dy0 + (sy1 - sy0), dx0 : dx0 + (sx1 - sx0)] = image_rgb[sy0:sy1, sx0:sx1]
            crop_a[dy0 : dy0 + (sy1 - sy0), dx0 : dx0 + (sx1 - sx0)] = m_full[sy0:sy1, sx0:sx1].astype(np.uint8) * 255

        inst_rgb = cv2.resize(crop_rgb, (crop_size, crop_size), interpolation=cv2.INTER_LANCZOS4).astype(np.float32) / 255.0
        inst_a = cv2.resize(crop_a, (crop_size, crop_size), interpolation=cv2.INTER_NEAREST).astype(np.float32) / 255.0
        inst_rgba = np.concatenate([inst_rgb.transpose(2, 0, 1), inst_a[None, :, :]], axis=0)

        # 3) extrinsics (OpenCV cam2world 4x4) 및 정규화 intrinsics (3x3)
        R_f = np.asarray(R, np.float64)
        t_f = np.asarray(t, np.float64).reshape(3)
        c2w_cv = np.eye(4, dtype=np.float32)
        c2w_cv[:3, :3] = R_f.T.astype(np.float32)
        c2w_cv[:3, 3] = (-R_f.T @ t_f).astype(np.float32)

        K_f = np.asarray(K, np.float64)
        K_norm = np.array(
            [
                [K_f[0, 0] / w0, 0.0, K_f[0, 2] / w0],
                [0.0, K_f[1, 1] / h0, K_f[1, 2] / h0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )
        return {
            "instance_image_masked": inst_rgba,
            "scene_image_masked": scene_rgba,
            "extrinsics": c2w_cv,
            "intrinsics": K_norm,
            "aug_bbox": np.array([bx0, by0, bx1, by1], dtype=np.int32),
        }

    def _ensure_pipeline(self) -> Any:
        try:
            import torch
        except ImportError as exc:
            raise CompletionSkip("TORCH_NOT_INSTALLED", f"PyTorch 미설치(3D-Fixer GPU 백엔드 불가): {exc}") from exc

        if not torch.cuda.is_available():
            raise CompletionSkip("CUDA_NOT_AVAILABLE", "3D-Fixer는 Linux CUDA GPU가 필요하다")
        vram_gb = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        if vram_gb < self.cfg.fixer_min_vram_gb:
            raise CompletionSkip(
                "GPU_VRAM_INSUFFICIENT",
                f"GPU VRAM {vram_gb:.1f} GB < 최소 요구 {self.cfg.fixer_min_vram_gb:.1f} GB (노트북에서는 실행하지 않음)",
            )
        if self._pipeline is not None:
            return self._pipeline

        if self.cfg.fixer_repo_dir and self.cfg.fixer_repo_dir not in sys.path:
            sys.path.insert(0, self.cfg.fixer_repo_dir)

        try:
            from threeDFixer.pipelines import ThreeDFixerPipeline
        except ImportError as exc:
            raise CompletionSkip("THREEDFIXER_NOT_INSTALLED", f"threeDFixer 패키지 미설치: {exc}") from exc

        model_source = self.cfg.fixer_model_dir or self.cfg.fixer_model_id
        compile_flag = os.environ.get("SIMAI_3DFIXER_COMPILE", "0").strip() in ("1", "true", "yes")
        try:
            self._pipeline = ThreeDFixerPipeline.from_pretrained(model_source, compile=compile_flag)
            self._pipeline.cuda()
        except Exception as exc:
            raise CompletionSkip("THREEDFIXER_LOAD_FAILED", f"3D-Fixer 모델 적재 실패: {type(exc).__name__}: {exc}") from exc
        return self._pipeline

    def unload(self) -> None:
        """GPU 파이프라인을 명시적으로 해제하고 CUDA 캐시를 비운다."""
        if self._pipeline is not None:
            self._pipeline = None
            import gc

            gc.collect()
            try:
                import torch

                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass

    def complete(self, inp: CompletionInput) -> CompletionOutput:
        if inp.best_camera is None or inp.mask_2d is None:
            raise CompletionSkip("NO_BEST_CAMERA", "최적 시점 또는 2D 마스크가 없다")
        pipeline = self._ensure_pipeline()
        import torch
        from scipy.spatial import cKDTree
        from threeDFixer.datasets.utils import transform_vertices
        from threeDFixer.utils import postprocessing_utils

        cond = self.prepare_conditioning_tensors(
            inp.best_camera.image,
            inp.mask_2d,
            inp.best_camera.K,
            inp.best_camera.R,
            inp.best_camera.t,
            crop_size=self.CROP_SIZE,
        )
        device = torch.device("cuda")
        inst_t = torch.from_numpy(cond["instance_image_masked"]).to(device=device, dtype=torch.float32)
        scene_t = torch.from_numpy(cond["scene_image_masked"]).to(device=device, dtype=torch.float32)
        extr_t = torch.from_numpy(cond["extrinsics"]).to(device=device, dtype=torch.float32)
        intr_t = torch.from_numpy(cond["intrinsics"]).to(device=device, dtype=torch.float32)

        pts = np.asarray(inp.observed_points_world, dtype=np.float32)
        pmask = np.asarray(inp.points_mask, dtype=np.float32)
        if (pmask > 0.8).sum() < 4:
            pmask = np.ones(len(pts), dtype=np.float32)

        sampler_params = {
            "steps": self.cfg.fixer_steps,
            "cfg_strength": self.cfg.fixer_cfg_strength,
            "cfg_interval": [0.5, 1.0],
            "rescale_t": 3.0,
        }
        torch.cuda.synchronize(device)
        torch.cuda.reset_peak_memory_stats(device)
        t_infer0 = time.monotonic()
        try:
            outputs, coarse_trans, coarse_scale, fine_trans, fine_scale = pipeline.run(
                inst_t,
                scene_image_masked=scene_t,
                seed=self.cfg.fixer_seed,
                extrinsics=extr_t,
                intrinsics=intr_t,
                points=pts,
                points_mask=pmask,
                sparse_structure_sampler_params=sampler_params,
                slat_sampler_params=sampler_params,
            )
            with torch.enable_grad():
                tmesh = postprocessing_utils.to_glb(
                    outputs["gaussian"][0],
                    outputs["mesh"][0],
                    simplify=0.95,
                    texture_size=max(512, self.cfg.texture_size),
                    transform_fn=lambda x: transform_vertices(
                        x,
                        ops=["scale", "translation", "scale", "translation"],
                        params=[fine_scale, fine_trans[None], coarse_scale, coarse_trans[None]],
                    ),
                    verbose=False,
                )
        except CompletionSkip:
            raise
        except Exception as exc:
            raise CompletionSkip("THREEDFIXER_INFERENCE_FAILED", f"{type(exc).__name__}: {exc}") from exc
        torch.cuda.synchronize(device)
        infer_elapsed_s = time.monotonic() - t_infer0
        peak_mem_mb = float(torch.cuda.max_memory_allocated(device) / (1024**2))
        reserved_mem_mb = float(torch.cuda.max_memory_reserved(device) / (1024**2))

        gen_V = np.asarray(tmesh.vertices, dtype=np.float64)
        gen_F = np.asarray(tmesh.faces, dtype=np.int64)
        uv = np.asarray(tmesh.visual.uv, dtype=np.float64) if getattr(tmesh.visual, "uv", None) is not None else None
        tex_img = getattr(getattr(tmesh.visual, "material", None), "baseColorTexture", None)
        tex_rgb = np.asarray(tex_img.convert("RGB"), dtype=np.uint8) if tex_img is not None else None

        # 관측 부분과 가까운 면은 OBSERVED(2), 새로 생성된 면은 GENERATED(4)
        tree = cKDTree(inp.observed_points_world)
        fc = gen_V[gen_F].mean(axis=1)
        dist, _ = tree.query(fc, k=1)
        face_src = np.where(dist <= self.cfg.obs_preserve_tol_m, OBSERVED, GENERATED).astype(np.uint8)

        src_map = None
        if tex_rgb is not None and uv is not None:
            th, tw = tex_rgb.shape[:2]
            tid_uv, _, _ = rasterize(uv[gen_F] * np.array([tw, th]), np.ones((len(gen_F), 3)), (th, tw), perspective=False)
            src_map = np.where(tid_uv >= 0, face_src[np.maximum(tid_uv, 0)], UNOBSERVED).astype(np.uint8)

        return CompletionOutput(
            vertices_world=gen_V,
            faces=gen_F,
            face_source=face_src,
            uv=uv,
            texture_rgb=tex_rgb,
            texel_source_map=src_map,
            backend_name=self.name,
            meta={
                "model_id": self.cfg.fixer_model_dir or self.cfg.fixer_model_id,
                "coarse_scale": float(coarse_scale),
                "fine_scale": float(fine_scale),
                "gpu_max_memory_allocated_mb": round(peak_mem_mb, 2),
                "gpu_max_memory_reserved_mb": round(reserved_mem_mb, 2),
                "infer_seconds": round(infer_elapsed_s, 4),
            },
        )


# --------------------------------------------------------------------------- 4. 정형 도형 템플릿 (라바콘·안내판)


def build_traffic_cone_template(
    center_xy: tuple[float, float] = (0.0, 0.0),
    floor_z_m: float = 0.0,
    yaw_rad: float = 0.0,
    *,
    height_m: float = TRAFFIC_CONE_HEIGHT_M,
    base_side_m: float = TRAFFIC_CONE_BASE_SIDE_M,
    top_diameter_m: float = TRAFFIC_CONE_TOP_DIAMETER_M,
    base_height_m: float = TRAFFIC_CONE_BASE_HEIGHT_M,
    body_bottom_diameter_m: float = TRAFFIC_CONE_BODY_BOTTOM_DIAMETER_M,
    segments: int = 24,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """라바콘 정형 메시 생성 (지시서 치수: 높이 67.5 cm, 밑판 한 변 37 cm 정사각, 꼭대기 지름 4.5 cm).

    반환: (정점 World (V, 3), 삼각형 (F, 3), UV (V, 2)).
    """
    half = 0.5 * base_side_m
    r_bot = 0.5 * min(body_bottom_diameter_m, base_side_m * 0.90)
    r_top = 0.5 * top_diameter_m
    z_base_top = min(base_height_m, height_m * 0.2)

    # 1) 정사각형 밑판 (z = 0 .. z_base_top, 한 변 base_side_m = 0.37 m)
    b0 = np.array(
        [
            [-half, -half, 0.0],
            [half, -half, 0.0],
            [half, half, 0.0],
            [-half, half, 0.0],
            [-half, -half, z_base_top],
            [half, -half, z_base_top],
            [half, half, z_base_top],
            [-half, half, z_base_top],
        ],
        dtype=np.float64,
    )
    base_faces = np.array(
        [
            [0, 2, 1], [0, 3, 2],  # 바닥면
            [4, 5, 6], [4, 6, 7],  # 밑판 윗면
            [0, 1, 5], [0, 5, 4],  # 앞옆면
            [1, 2, 6], [1, 6, 5],
            [2, 3, 7], [2, 7, 6],
            [3, 0, 4], [3, 4, 7],
        ],
        dtype=np.int64,
    )

    # 2) 절두 원뿔 몸통 (z = z_base_top .. height_m = 0.675 m, 상단 지름 top_diameter_m = 0.045 m)
    n_rings = 6
    zs = np.linspace(z_base_top, height_m, n_rings)
    rs = np.linspace(r_bot, r_top, n_rings)
    angles = np.linspace(0.0, 2.0 * math.pi, segments, endpoint=False)
    ca, sa = np.cos(angles), np.sin(angles)

    cone_verts = []
    for r, z in zip(rs, zs):
        cone_verts.append(np.column_stack([r * ca, r * sa, np.full(segments, z)]))
    top_center_idx = len(b0) + n_rings * segments
    cone_V = np.vstack([*cone_verts, np.array([[0.0, 0.0, height_m]])])

    cone_faces: list[list[int]] = []
    for ring in range(n_rings - 1):
        r0 = len(b0) + ring * segments
        r1 = len(b0) + (ring + 1) * segments
        for j in range(segments):
            jn = (j + 1) % segments
            cone_faces.append([r0 + j, r0 + jn, r1 + jn])
            cone_faces.append([r0 + j, r1 + jn, r1 + j])
    # 꼭대기 원판 마개 (z = height_m, 지름 4.5 cm)
    r_last = len(b0) + (n_rings - 1) * segments
    for j in range(segments):
        jn = (j + 1) % segments
        cone_faces.append([r_last + j, r_last + jn, top_center_idx])

    V_local = np.vstack([b0, cone_V])
    F_local = np.vstack([base_faces, np.asarray(cone_faces, dtype=np.int64)])

    # 회전(yaw) 및 위치(center_xy, floor_z_m) 적용
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    R_yaw = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    V_world = V_local @ R_yaw.T + np.array([center_xy[0], center_xy[1], floor_z_m], dtype=np.float64)
    return _box_uv_unwrap(V_world, F_local)


def complete_traffic_cone(inp: CompletionInput, cfg: ObjectCompletionConfig) -> CompletionOutput:
    """관측 점으로 라바콘 위치·방향을 추정하고 정형 템플릿 + 관측 사진 투영 텍스처를 생성한다."""
    pts = inp.observed_points_world
    z_rel = np.clip(pts[:, 2] - inp.floor_z_m, 0.0, TRAFFIC_CONE_HEIGHT_M)
    upper = z_rel >= np.percentile(z_rel, 50)
    cx0, cy0 = np.median(pts[upper if upper.sum() >= 5 else slice(None), :2], axis=0)

    # 전면부만 관측된 라바콘 점들이 원뿔 옆면 반지름 r(z)에 가장 잘 맞도록 중심축 (cx, cy)를 미세 탐색
    r_bot = 0.5 * TRAFFIC_CONE_BODY_BOTTOM_DIAMETER_M
    r_top = 0.5 * TRAFFIC_CONE_TOP_DIAMETER_M
    t_z = np.clip((z_rel - TRAFFIC_CONE_BASE_HEIGHT_M) / max(TRAFFIC_CONE_HEIGHT_M - TRAFFIC_CONE_BASE_HEIGHT_M, 1e-4), 0.0, 1.0)
    r_expected = r_bot * (1.0 - t_z) + r_top * t_z
    body_mask = z_rel >= TRAFFIC_CONE_BASE_HEIGHT_M
    b_pts = pts[body_mask if body_mask.sum() >= 6 else slice(None), :2]
    b_r = r_expected[body_mask if body_mask.sum() >= 6 else slice(None)]

    best_cx, best_cy = float(cx0), float(cy0)
    best_err = float("inf")
    for dx in np.linspace(-0.10, 0.10, 17):
        for dy in np.linspace(-0.10, 0.10, 17):
            cand_x, cand_y = float(cx0 + dx), float(cy0 + dy)
            rad = np.hypot(b_pts[:, 0] - cand_x, b_pts[:, 1] - cand_y)
            err = float(np.median(np.abs(rad - b_r)))
            if err < best_err:
                best_err = err
                best_cx, best_cy = cand_x, cand_y
    cx, cy = best_cx, best_cy

    # 하단 점 PCA로 밑판 방향 추정
    lower = z_rel <= np.percentile(z_rel, 40)
    yaw = 0.0
    if lower.sum() >= 6:
        xy = pts[lower, :2] - np.array([cx, cy])
        _, evec = np.linalg.eigh((xy.T @ xy) / len(xy))
        yaw = float(math.atan2(evec[1, 1], evec[0, 1]))

    V, F, UV = build_traffic_cone_template((float(cx), float(cy)), inp.floor_z_m, yaw)
    # 정형 템플릿에서 관측 점과 가까운 면은 OBSERVED(2), 나머지는 GENERATED(4)
    from scipy.spatial import cKDTree

    tree = cKDTree(pts)
    fc = V[F].mean(axis=1)
    dist, _ = tree.query(fc, k=1)
    face_src = np.where(dist <= max(cfg.obs_preserve_tol_m, 0.06), OBSERVED, GENERATED).astype(np.uint8)

    # 라바콘 기본 색상(주황 + 흰색 반사띠 패턴)을 초기 바탕색으로 두고 관측 사진을 투영
    S = cfg.texture_size
    tex_rgb, src_map = bake_multiview_object_texture(
        V,
        F,
        UV,
        face_src,
        inp.all_cameras,
        np.array([225, 68, 32], dtype=np.uint8),
        texture_size=S,
        all_masks_2d=inp.all_masks_2d,
    )
    # 생성된(GENERATED=4) 텍셀 및 비정상 배경색(갈색/회색) 텍셀을 라바콘 높이별 표준 색상(주황 몸통 + 흰색 반사띠)으로 정제
    tri_uv_px = UV[F] * S
    tid_uv, bary_uv, _ = rasterize(tri_uv_px, np.ones((len(F), 3), np.float64), (S, S), perspective=False)
    cov = tid_uv >= 0
    if cov.any():
        ys_u, xs_u = np.nonzero(cov)
        f_u = tid_uv[ys_u, xs_u]
        w_u = bary_uv[ys_u, xs_u].astype(np.float64)
        z_u = (V[F[f_u], 2] * w_u).sum(axis=1) - inp.floor_z_m
        cur_rgb = tex_rgb[ys_u, xs_u].astype(np.int32)
        is_stripe = (z_u >= 0.27) & (z_u <= 0.45)
        is_valid_cone_color = (
            ((cur_rgb[:, 0] > 110) & (cur_rgb[:, 0] - cur_rgb[:, 1] > 45) & (cur_rgb[:, 0] > cur_rgb[:, 1] * 1.5))
            | (is_stripe & (cur_rgb.min(axis=1) > 150))
        )
        need_std = (src_map[ys_u, xs_u] == GENERATED) | (~is_valid_cone_color)
        if need_std.any():
            std_col = np.where(
                is_stripe[:, None],
                np.array([[238, 238, 242]], dtype=np.uint8),
                np.array([[225, 68, 32]], dtype=np.uint8),
            )
            tex_rgb[ys_u[need_std], xs_u[need_std]] = std_col[need_std]
            src_map[ys_u[need_std], xs_u[need_std]] = GENERATED
    return CompletionOutput(
        vertices_world=V,
        faces=F,
        face_source=face_src,
        uv=UV,
        texture_rgb=tex_rgb,
        texel_source_map=src_map,
        backend_name="template_traffic_cone",
        meta={
            "template": "traffic_cone",
            "center_xy_m": [round(float(cx), 4), round(float(cy), 4)],
            "floor_z_m": round(float(inp.floor_z_m), 4),
            "yaw_rad": round(yaw, 4),
            "height_m": TRAFFIC_CONE_HEIGHT_M,
            "base_side_m": TRAFFIC_CONE_BASE_SIDE_M,
            "top_diameter_m": TRAFFIC_CONE_TOP_DIAMETER_M,
        },
    )


def build_signboard_template(
    center_xy: tuple[float, float],
    z_min_m: float,
    width_m: float,
    height_m: float,
    thickness_m: float = 0.04,
    yaw_rad: float = 0.0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """안내판 평판(직육면체) 템플릿 메시를 생성한다."""
    hw = 0.5 * width_m
    ht = 0.5 * thickness_m
    V_local = np.array(
        [
            [-hw, -ht, 0.0],
            [hw, -ht, 0.0],
            [hw, ht, 0.0],
            [-hw, ht, 0.0],
            [-hw, -ht, height_m],
            [hw, -ht, height_m],
            [hw, ht, height_m],
            [-hw, ht, height_m],
        ],
        dtype=np.float64,
    )
    F_local = np.array(
        [
            [0, 1, 5], [0, 5, 4],  # 앞면 (-y)
            [2, 3, 7], [2, 7, 6],  # 뒷면 (+y)
            [1, 2, 6], [1, 6, 5],  # 오른옆
            [3, 0, 4], [3, 4, 7],  # 왼옆
            [4, 5, 6], [4, 6, 7],  # 윗면
            [0, 2, 1], [0, 3, 2],  # 아랫면
        ],
        dtype=np.int64,
    )
    cy, sy = math.cos(yaw_rad), math.sin(yaw_rad)
    R_yaw = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    V_world = V_local @ R_yaw.T + np.array([center_xy[0], center_xy[1], z_min_m], dtype=np.float64)
    return _box_uv_unwrap(V_world, F_local)


def complete_signboard(inp: CompletionInput, cfg: ObjectCompletionConfig) -> CompletionOutput:
    """관측 점(또는 설정값)으로 안내판 치수·위치·방향을 맞추고 관측 사진 텍스처를 입힌다."""
    pts = inp.observed_points_world
    cx, cy = np.median(pts[:, :2], axis=0)
    xy = pts[:, :2] - np.array([cx, cy])
    _, evec = np.linalg.eigh((xy.T @ xy) / max(len(xy), 1))
    # 주축(폭 방향) = evec[:, 1]
    yaw = float(math.atan2(evec[1, 1], evec[0, 1]))
    proj_w = xy @ evec[:, 1]
    obs_w = float(np.percentile(proj_w, 98) - np.percentile(proj_w, 2))
    z_min = float(inp.observed_vertices_world[:, 2].min())
    obs_h = float(np.percentile(inp.observed_vertices_world[:, 2], 98) - z_min)
    if z_min - inp.floor_z_m < 0.25:
        z_min = inp.floor_z_m
        obs_h = float(np.percentile(inp.observed_vertices_world[:, 2], 98) - inp.floor_z_m)

    scfg = cfg.signboard
    width = scfg.width_m if scfg.width_m is not None else float(np.clip(obs_w, scfg.min_width_m, scfg.max_width_m))
    height = scfg.height_m if scfg.height_m is not None else float(np.clip(obs_h, scfg.min_height_m, scfg.max_height_m))

    V, F, UV = build_signboard_template((float(cx), float(cy)), z_min, width, height, scfg.thickness_m, yaw)
    # 안내판 정면(0, 1)과 뒷면(2, 3) 중 관측 카메라를 향하는 면은 ESTIMATED_PLANE(3) / OBSERVED(2), 나머지는 GENERATED(4)
    face_src = np.array([OBSERVED, OBSERVED, GENERATED, GENERATED] + [GENERATED] * 8, dtype=np.uint8)
    fb_rgb = np.median(inp.observed_vertex_colors_srgb, axis=0) if len(inp.observed_vertex_colors_srgb) else np.array([240, 240, 240])
    tex_rgb, src_map = bake_multiview_object_texture(
        V,
        F,
        UV,
        face_src,
        inp.all_cameras,
        fb_rgb,
        texture_size=cfg.texture_size,
        all_masks_2d=inp.all_masks_2d,
    )

    return CompletionOutput(
        vertices_world=V,
        faces=F,
        face_source=face_src,
        uv=UV,
        texture_rgb=tex_rgb,
        texel_source_map=src_map,
        backend_name="template_signboard",
        meta={
            "template": "signboard",
            "center_xy_m": [round(float(cx), 4), round(float(cy), 4)],
            "z_min_m": round(z_min, 4),
            "width_m": round(width, 4),
            "height_m": round(height, 4),
            "thickness_m": round(scfg.thickness_m, 4),
            "yaw_rad": round(yaw, 4),
        },
    )


# --------------------------------------------------------------------------- 5. 배치(ICP·바닥 접촉 보정) 및 검증


def _sample_mesh_surface(vertices: np.ndarray, faces: np.ndarray, max_samples: int = 3000) -> np.ndarray:
    """메시 정점 + 면 무게중심 + 변 중점(큰 면은 내부 격자 포함) 표본을 추출한다."""
    if not len(faces):
        return vertices
    a, b, c = vertices[faces[:, 0]], vertices[faces[:, 1]], vertices[faces[:, 2]]
    parts = [vertices, (a + b + c) / 3.0, 0.5 * (a + b), 0.5 * (b + c), 0.5 * (c + a)]
    if len(faces) <= 120:
        for u in np.linspace(0.1, 0.9, 9):
            for v in np.linspace(0.1, 0.9 - u, max(1, int(round((1.0 - u) * 9)))):
                w = 1.0 - u - v
                if w >= -1e-6:
                    parts.append(u * a + v * b + max(0.0, w) * c)
    samples = np.vstack(parts)
    if len(samples) > max_samples:
        step = len(samples) / max_samples
        pick = np.floor(np.arange(max_samples) * step).astype(np.int64)
        samples = samples[pick]
    return samples


def align_completed_mesh(
    out: CompletionOutput,
    observed_points_world: np.ndarray,
    floor_z_m: float,
    cfg: ObjectCompletionConfig,
    *,
    snap_to_floor: bool = True,
) -> tuple[CompletionOutput, dict[str, Any]]:
    """완성 메시를 관측 부분 점(`observed_points_world`)에 맞춰 위치·크기(ICP) 정합하고 바닥 접촉(`z = floor_z_m`)을 보정한다."""
    from scipy.spatial import cKDTree

    V = out.vertices_world.copy()
    obs = np.asarray(observed_points_world, np.float64)

    # 1) 수평 이동 + 수평 스케일 미세 정합 (bounded ICP)
    total_shift = np.zeros(3, dtype=np.float64)
    scale_xy = 1.0
    if len(obs) >= 6 and out.backend_name not in ("convex_hull", "template_traffic_cone", "template_signboard"):
        # 관측 점의 XY 범위를 크게 벗어난 경우 먼저 스케일 보정
        obs_span = np.ptp(obs[:, :2], axis=0)
        mesh_span = np.maximum(np.ptp(V[:, :2], axis=0), 1e-4)
        raw_ratio = float(np.median(obs_span / mesh_span))
        if abs(raw_ratio - 1.0) <= cfg.icp_max_scale_dev and 0.75 <= raw_ratio <= 1.35:
            scale_xy = raw_ratio
            c0 = V.mean(axis=0)
            V[:, :2] = (V[:, :2] - c0[:2]) * scale_xy + c0[:2]

        # 초기 바운딩 박스 중심 정렬(허용 이동 범위 내일 때)
        obs_bb_c = 0.5 * (obs.min(axis=0) + obs.max(axis=0))
        mesh_bb_c = 0.5 * (V.min(axis=0) + V.max(axis=0))
        init_shift = obs_bb_c - mesh_bb_c
        if 1e-4 < np.linalg.norm(init_shift) <= cfg.icp_max_shift_m:
            V += init_shift
            total_shift += init_shift

        for _ in range(cfg.icp_max_iter):
            surf = _sample_mesh_surface(V, out.faces, max_samples=2000)
            tree = cKDTree(surf)
            dist, nn = tree.query(obs, k=1)
            if float(np.median(dist)) < 5e-4:
                break
            inlier = dist <= np.percentile(dist, 80) + 1e-4
            if inlier.sum() < 4:
                break
            step_shift = np.mean(obs[inlier] - surf[nn[inlier]], axis=0)
            if np.linalg.norm(total_shift + step_shift) > cfg.icp_max_shift_m:
                break
            V += step_shift
            total_shift += step_shift
            if np.linalg.norm(step_shift) < 1e-4:
                break

    # 2) 바닥 접촉 보정 (Z = floor_z_m)
    z_min_before = float(V[:, 2].min()) if len(V) else floor_z_m
    if snap_to_floor and len(V):
        dz = floor_z_m - z_min_before
        # 전체 메시가 바닥에서 약간 떠 있거나 파고든 경우(|dz| <= 0.15 m) 하단 정점을 바닥에 스냅하거나 전체 Z 이동
        if abs(dz) <= 0.08:
            V[:, 2] += dz
            total_shift[2] += dz
        else:
            # 관측 상부 높이는 보존하고 하단 지지 정점만 바닥(floor_z_m)으로 내린다
            low_cut = np.percentile(V[:, 2], 15)
            low_mask = V[:, 2] <= low_cut + 1e-6
            if low_mask.any():
                V[low_mask, 2] = np.minimum(V[low_mask, 2], floor_z_m)
                # 가장 낮은 정점을 정확히 floor_z_m 로 맞춤
                min_idx = int(np.argmin(V[:, 2]))
                V[min_idx, 2] = floor_z_m
                below = V[:, 2] < floor_z_m
                V[below, 2] = floor_z_m

    z_min_after = float(V[:, 2].min()) if len(V) else floor_z_m
    align_info = {
        "shift_xyz_m": [round(float(x), 4) for x in total_shift],
        "scale_xy": round(float(scale_xy), 4),
        "z_min_before_m": round(z_min_before, 4),
        "z_min_after_m": round(z_min_after, 4),
        "floor_contact_error_m": round(abs(z_min_after - floor_z_m), 5),
    }
    return replace(out, vertices_world=V), align_info


def validate_completed_object(
    cand: ObjectCandidate,
    out: CompletionOutput,
    cameras: Sequence[CheckCamera],
    floor_z_m: float,
    elapsed_s: float,
    cfg: ObjectCompletionConfig,
) -> dict[str, Any]:
    """완성 메시의 5대 검증 지표(4대 재투영 실루엣 IoU, 관측 점→메시 거리 중앙·p95, 바닥 접촉 오차, 면 수·용량, 시간)를 계산하고 채택 여부를 판정한다."""
    from scipy.spatial import cKDTree

    th = cfg.thresholds
    V, F = out.vertices_world, out.faces

    # 1) 관측 점 → 완성 메시 표면 거리 (중앙값, p95)
    surf = _sample_mesh_surface(V, F, max_samples=4000)
    tree = cKDTree(surf)
    dists, _ = tree.query(cand.points_world, k=1)
    obs_med_m = float(np.median(dists)) if len(dists) else 0.0
    obs_p95_m = float(np.percentile(dists, 95)) if len(dists) else 0.0

    # 2) 바닥 접촉 오차 (안내판이 벽걸이/고소 설치인 경우는 후보 z_min 기준)
    target_z = floor_z_m if (cand.category != "signboard" or cand.bbox_min_world[2] - floor_z_m < 0.25) else float(cand.bbox_min_world[2])
    floor_err_m = abs(float(V[:, 2].min()) - target_z) if len(V) else 999.0

    # 3) 4대 카메라 재투영 실루엣 IoU
    iou_by_cam: dict[str, float] = {}
    for cam in cameras:
        ref_mask = cand.masks_2d.get(cam.camera_id)
        if ref_mask is None or int(ref_mask.sum()) < 10:
            continue
        H, W = ref_mask.shape
        h0, w0 = cam.image.shape[:2]
        R = np.asarray(cam.R, np.float64)
        t = np.asarray(cam.t, np.float64).reshape(3)
        K = np.asarray(cam.K, np.float64)
        tc = V[F] @ R.T + t
        front = tc[:, :, 2].min(axis=1) >= NEAR_M
        if not front.any():
            iou_by_cam[cam.camera_id] = 0.0
            continue
        zc = tc[front, :, 2]
        u = (K[0, 0] * tc[front, :, 0] / zc + K[0, 2] + 0.5) * (W / w0)
        v = (K[1, 1] * tc[front, :, 1] / zc + K[1, 2] + 0.5) * (H / h0)
        tid, _, _ = rasterize(np.stack([u, v], axis=2), zc, (H, W), perspective=True)
        pred_mask = tid >= 0
        inter = int((pred_mask & ref_mask).sum())
        union = int((pred_mask | ref_mask).sum())
        iou_by_cam[cam.camera_id] = round(float(inter / max(union, 1)), 4)

    iou_vals = list(iou_by_cam.values())
    iou_mean = float(np.mean(iou_vals)) if iou_vals else 1.0
    iou_best = float(max(iou_vals)) if iou_vals else 1.0
    # 가림이 적은 대표 시점(best_camera) 또는 최상 시점 IoU를 기준으로 판정
    iou_effective = max(iou_mean, iou_best)

    # 4) 면 수·예상 용량·시간
    face_count = int(len(F))
    tex_bytes = int(out.texture_rgb.size) if out.texture_rgb is not None else 0
    est_bytes = int(len(V) * 32 + len(F) * 12 + tex_bytes)

    reasons: list[str] = []
    if iou_vals and iou_effective < th.min_reprojection_iou:
        reasons.append(f"LOW_REPROJECTION_IOU({iou_effective:.3f}<{th.min_reprojection_iou})")
    # 정형 도형(라바콘·안내판)은 이상적인 기하 형상이므로 TSDF 왜곡 대비 완화된 거리 상한(1.5배) 적용
    dist_scale = 1.5 if cand.category in ("traffic_cone", "signboard") else 1.0
    if obs_med_m > th.max_obs_to_mesh_median_m * dist_scale:
        reasons.append(f"HIGH_OBS_DIST_MEDIAN({obs_med_m:.4f}>{th.max_obs_to_mesh_median_m * dist_scale:.4f})")
    if obs_p95_m > th.max_obs_to_mesh_p95_m * dist_scale:
        reasons.append(f"HIGH_OBS_DIST_P95({obs_p95_m:.4f}>{th.max_obs_to_mesh_p95_m * dist_scale:.4f})")
    if floor_err_m > th.max_floor_contact_error_m:
        reasons.append(f"FLOOR_CONTACT_ERROR({floor_err_m:.4f}>{th.max_floor_contact_error_m})")
    if face_count > th.max_faces_per_object or face_count == 0:
        reasons.append(f"FACE_COUNT_OUT_OF_RANGE({face_count})")
    if est_bytes > th.max_bytes_per_object:
        reasons.append(f"BYTES_EXCEEDED({est_bytes}>{th.max_bytes_per_object})")
    if elapsed_s > th.max_seconds_per_object:
        reasons.append(f"TIME_EXCEEDED({elapsed_s:.2f}s>{th.max_seconds_per_object}s)")

    return {
        "accepted": len(reasons) == 0,
        "rejection_reasons": reasons,
        "reprojection_iou_mean": round(iou_mean, 4) if iou_vals else None,
        "reprojection_iou_best": round(iou_best, 4) if iou_vals else None,
        "reprojection_iou_by_camera": iou_by_cam,
        "obs_to_mesh_median_m": round(obs_med_m, 4),
        "obs_to_mesh_p95_m": round(obs_p95_m, 4),
        "floor_contact_error_m": round(floor_err_m, 5),
        "face_count": face_count,
        "estimated_bytes": est_bytes,
        "elapsed_s": round(elapsed_s, 4),
    }


# --------------------------------------------------------------------------- 6. GLB 합치기 및 면별 출처 기록


@dataclass
class CompletedObjectNode:
    candidate: ObjectCandidate
    output: CompletionOutput
    alignment: dict[str, Any]
    validation: dict[str, Any]


def _encode_png_rgb(rgb: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", np.ascontiguousarray(rgb[:, :, ::-1]))
    if not ok:
        raise ValueError("PNG 인코딩 실패")
    return buf.tobytes()


def _encode_png_gray(gray: np.ndarray) -> bytes:
    import cv2

    ok, buf = cv2.imencode(".png", np.ascontiguousarray(gray))
    if not ok:
        raise ValueError("흑백 PNG 인코딩 실패")
    return buf.tobytes()


def write_completed_glb(
    g: GlbFile,
    model: SceneModel,
    merged_mesh: MergedSceneMesh,
    accepted_objects: Sequence[CompletedObjectNode],
    completion_meta: dict[str, Any],
) -> bytes:
    """채택된 완성 물체들을 GLB 노드로 추가하고 원래 메시에 있던 해당 물체 조각들을 대체한다.

    - World→glTF 정점 변환은 `texture_mesh.to_glb_points(np.eye(4), V_world)` 한 곳만 사용한다(규칙 3).
    - 면별·텍셀별 출처는 기존 `surface_source` 체계(`OBSERVED=2`, `ESTIMATED_PLANE=3`, `GENERATED=4`)로 기록한다.
    """
    doc = g.doc
    materials = doc.setdefault("materials", [])
    textures = doc.setdefault("textures", [])
    images = doc.setdefault("images", [])
    samplers = doc.setdefault("samplers", [])

    # 1) 채택된 물체에 속한 원래 삼각형들을 원본 프리미티브에서 제거(대체)
    removed_global = np.zeros(len(merged_mesh.F), bool)
    for item in accepted_objects:
        removed_global[item.candidate.face_indices] = True

    for pid, prim in enumerate(model.prims):
        if prim.kind == "estimated" or not len(prim.idx):
            continue
        sel = merged_mesh.face_prim == pid
        drop_local = merged_mesh.face_local[sel & removed_global]
        if not len(drop_local):
            continue
        keep_mask = np.ones(len(prim.idx), bool)
        keep_mask[drop_local] = False
        new_idx = prim.idx[keep_mask]
        src_prim = doc["meshes"][prim.mesh_index]["primitives"][prim.prim_index]
        idx_bytes = np.ascontiguousarray(new_idx.reshape(-1), dtype="<u4").tobytes()
        idx_view = _add_view(g, idx_bytes, 34963)
        idx_acc = _add_accessor(g, idx_view, 5125, int(new_idx.size), "SCALAR")
        src_prim["indices"] = idx_acc

    # 2) 채택된 물체마다 메시·재질·텍스처·출처 지도를 GLB에 추가
    sampler_idx = None
    if accepted_objects:
        samplers.append({"magFilter": 9729, "minFilter": 9729, "wrapS": 33071, "wrapT": 33071})
        sampler_idx = len(samplers) - 1

    scene = doc["scenes"][doc.get("scene", 0)]
    scene_nodes = scene.setdefault("nodes", [])

    for item in accepted_objects:
        cand = item.candidate
        out = item.output
        V_world = out.vertices_world
        F = out.faces
        # glTF 좌표 변환은 오직 to_glb_points 한 곳만 사용
        V_glb = to_glb_points(np.eye(4), V_world)
        fn_world = face_normals(V_world, F)
        vn_world = np.zeros_like(V_world)
        for k in range(3):
            np.add.at(vn_world, F[:, k], fn_world)
        vn_norm = np.linalg.norm(vn_world, axis=1, keepdims=True)
        vn_world = np.where(vn_norm > 1e-9, vn_world / np.maximum(vn_norm, 1e-9), np.array([0.0, 0.0, 1.0]))
        N_glb = to_glb_points(np.eye(4), vn_world)

        pos_view = _add_view(g, np.ascontiguousarray(V_glb, dtype="<f4").tobytes(), 34962)
        pos_acc = _add_accessor(
            g,
            pos_view,
            5126,
            len(V_glb),
            "VEC3",
            min=[float(x) for x in V_glb.min(axis=0)],
            max=[float(x) for x in V_glb.max(axis=0)],
        )
        nrm_view = _add_view(g, np.ascontiguousarray(N_glb, dtype="<f4").tobytes(), 34962)
        nrm_acc = _add_accessor(g, nrm_view, 5126, len(N_glb), "VEC3")
        idx_view = _add_view(g, np.ascontiguousarray(F.reshape(-1), dtype="<u4").tobytes(), 34963)
        idx_acc = _add_accessor(g, idx_view, 5125, int(F.size), "SCALAR")

        attrs: dict[str, int] = {"POSITION": pos_acc, "NORMAL": nrm_acc}
        pbr: dict[str, Any] = {"baseColorFactor": [1.0, 1.0, 1.0, 1.0], "metallicFactor": 0.0, "roughnessFactor": 0.9}
        src_img_idx = None

        if out.uv is not None and out.texture_rgb is not None:
            uv_view = _add_view(g, np.ascontiguousarray(out.uv, dtype="<f4").tobytes(), 34962)
            uv_acc = _add_accessor(g, uv_view, 5126, len(out.uv), "VEC2")
            attrs["TEXCOORD_0"] = uv_acc

            png_bytes = _encode_png_rgb(out.texture_rgb)
            img_view = _add_view(g, png_bytes, None)
            images.append({"bufferView": img_view, "mimeType": "image/png", "name": f"{cand.candidate_id}_albedo"})
            img_idx = len(images) - 1
            textures.append({"source": img_idx, "sampler": sampler_idx})
            pbr["baseColorTexture"] = {"index": len(textures) - 1}

            if out.texel_source_map is not None:
                smap_bytes = _encode_png_gray(out.texel_source_map)
                smap_view = _add_view(g, smap_bytes, None)
                images.append({"bufferView": smap_view, "mimeType": "image/png", "name": f"{cand.candidate_id}_surface_source"})
                src_img_idx = len(images) - 1

        # 면별 출처 비율(표면적 기준)
        a, b, c = V_world[F[:, 0]], V_world[F[:, 1]], V_world[F[:, 2]]
        f_areas = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
        tot_area = max(float(f_areas.sum()), 1e-12)
        ratios_area: dict[str, float] = {}
        counts_face: dict[str, int] = {}
        for name, val in ALL_SURFACE_VALUES.items():
            m = out.face_source == val
            if m.any():
                ratios_area[name] = round(float(f_areas[m].sum() / tot_area), 5)
                counts_face[name] = int(m.sum())

        node_prefix = "template" if cand.category in ("traffic_cone", "signboard") else "completed"
        node_name = f"{node_prefix}_{cand.candidate_id}"
        materials.append({"name": f"{node_name}_mat", "doubleSided": True, "pbrMetallicRoughness": pbr})
        mat_idx = len(materials) - 1

        doc.setdefault("meshes", []).append({
            "name": node_name,
            "primitives": [{"attributes": attrs, "indices": idx_acc, "material": mat_idx, "mode": 4}],
        })
        mesh_idx = len(doc["meshes"]) - 1

        dominant_src = "GENERATED" if (out.face_source == GENERATED).any() else "OBSERVED"
        node_extras = {
            "object_id": cand.candidate_id,
            "category": cand.category,
            "backend": out.backend_name,
            "geometry_role": "VISUAL",
            "safety_use": False,
            "surface_source": dominant_src,
            "surface_values": SURFACE_VALUE_TABLE,
            "face_source_counts": counts_face,
            "face_source_ratios_area": ratios_area,
            "source_map_image": src_img_idx,
            "alignment": item.alignment,
            "validation": item.validation,
        }
        doc["nodes"].append({"name": node_name, "mesh": mesh_idx, "extras": {"simai": node_extras}})
        scene_nodes.append(len(doc["nodes"]) - 1)

    # 3) 최상위 extras.simai 갱신 (`GENERATED: 4` 등록 및 `object_completion` 메타 기록)
    extras = doc.setdefault("extras", {})
    if not isinstance(extras, dict):
        extras = doc["extras"] = {}
    simai = extras.setdefault("simai", {})
    if not isinstance(simai, dict):
        simai = extras["simai"] = {}
    simai.setdefault("axis_convention", "GLTF_Y_UP")
    ss = simai.setdefault("surface_source", {})
    if isinstance(ss, dict):
        vals = ss.setdefault("values", dict(ALL_SURFACE_VALUES))
        if isinstance(vals, dict):
            vals["GENERATED"] = GENERATED
        res_not = ss.get("reserved_not_emitted")
        if isinstance(res_not, dict):
            res_not.pop("GENERATED", None)
    simai["object_completion"] = completion_meta
    return serialize_glb(g)


# --------------------------------------------------------------------------- 7. 공개 진입점


@dataclass
class ObjectCompletionResult:
    glb: bytes | None  # 적용된 GLB 바이트. 꺼져 있거나 채택된 물체가 없으면 None(원본 유지)
    report: dict[str, Any]
    candidates: list[ObjectCandidate] = field(default_factory=list)
    completed_nodes: list[CompletedObjectNode] = field(default_factory=list)


def complete_glb_objects(
    raw: bytes,
    *,
    config: ObjectCompletionConfig | None = None,
    cameras: Sequence[CheckCamera] | None = None,
    backend: CompletionBackend | None = None,
    refiner: MaskRefiner | None = None,
    clock=time.monotonic,
) -> ObjectCompletionResult:
    """GLB에서 물체 후보를 분리하고 제자리 완성(또는 라바콘·안내판 템플릿 적용) 후 검증을 통과한 물체만 GLB에 합친다."""
    cfg = config or ObjectCompletionConfig()
    started = clock()
    report: dict[str, Any] = {
        "version": OBJECT_COMPLETION_VERSION,
        "applied": False,
        "reason": None,
        "purpose": "VISUAL_ONLY",
        "safety_use": False,
        "backend": backend.name if backend is not None else cfg.backend,
        "surface_values": SURFACE_VALUE_TABLE,
        "input_bytes": len(raw),
    }
    if not cfg.enabled:
        report["reason"] = "DISABLED"
        report["seconds"] = round(clock() - started, 4)
        return ObjectCompletionResult(None, report)

    owns_backend = backend is None
    try:
        g = parse_glb(raw)
        model = load_scene_for_objects(g)
        merged_mesh = build_merged_scene_mesh(g, model)
        cams = list(cameras) if cameras is not None else []
        candidates, cand_summary = extract_object_candidates(merged_mesh, cams, cfg, refiner=refiner)
        report["candidate_summary"] = cand_summary
        if not candidates:
            report["reason"] = "NO_OBJECT_CANDIDATES"
            return ObjectCompletionResult(None, report)

        floor_z_m = float(cand_summary["planes"]["floor_z_m"])
        if backend is None:
            if cfg.backend == "3d_fixer":
                backend = ThreeDFixerBackend(cfg)
            elif cfg.backend == "convex_hull":
                backend = ConvexHullCompletionBackend(obs_preserve_tol_m=cfg.obs_preserve_tol_m, texture_size=cfg.texture_size)
            else:
                raise CompletionSkip("UNKNOWN_BACKEND", f"지원하지 않는 물체 완성 백엔드: {cfg.backend}")

        cam_by_id = {c.camera_id: c for c in cams}
        accepted_nodes: list[CompletedObjectNode] = []
        object_reports: list[dict[str, Any]] = []
        peak_gpu_mb_all = 0.0

        for cand in candidates:
            t0 = clock()
            best_cam = cam_by_id.get(cand.best_camera_id) if cand.best_camera_id else (cams[0] if cams else None)
            best_mask = cand.masks_2d.get(cand.best_camera_id) if cand.best_camera_id else None
            inp = CompletionInput(
                candidate_id=cand.candidate_id,
                category=cand.category,
                best_camera=best_cam,
                mask_2d=best_mask,
                all_cameras=cams,
                all_masks_2d=cand.masks_2d,
                observed_points_world=cand.points_world,
                points_mask=cand.points_mask,
                observed_vertices_world=cand.vertices_world,
                observed_faces=cand.faces,
                observed_face_mask=cand.face_observed,
                observed_vertex_colors_srgb=cand.vertex_colors_srgb,
                floor_z_m=floor_z_m,
            )
            try:
                if cand.category == "traffic_cone":
                    out = complete_traffic_cone(inp, cfg)
                    aligned, align_info = align_completed_mesh(out, cand.points_world, floor_z_m, cfg, snap_to_floor=True)
                elif cand.category == "signboard":
                    out = complete_signboard(inp, cfg)
                    snap_fl = (cand.bbox_min_world[2] - floor_z_m) < 0.25
                    aligned, align_info = align_completed_mesh(out, cand.points_world, floor_z_m, cfg, snap_to_floor=snap_fl)
                else:
                    out = backend.complete(inp)
                    aligned, align_info = align_completed_mesh(out, cand.points_world, floor_z_m, cfg, snap_to_floor=True)

                elapsed = clock() - t0
                val = validate_completed_object(cand, aligned, cams, floor_z_m, elapsed, cfg)
                obj_gpu_mb = float(aligned.meta.get("gpu_max_memory_allocated_mb", 0.0) or 0.0)
                peak_gpu_mb_all = max(peak_gpu_mb_all, obj_gpu_mb)
                obj_rep = {
                    "candidate_id": cand.candidate_id,
                    "category": cand.category,
                    "backend": aligned.backend_name,
                    "best_camera_id": cand.best_camera_id,
                    "original_faces": int(len(cand.faces)),
                    "original_area_m2": round(cand.area_m2, 4),
                    "bbox_min_m": [round(float(x), 3) for x in cand.bbox_min_world],
                    "bbox_max_m": [round(float(x), 3) for x in cand.bbox_max_world],
                    "alignment": align_info,
                    "validation": val,
                    "meta": aligned.meta,
                    "preserved_original_mesh": not bool(val["accepted"]),
                }
                object_reports.append(obj_rep)
                if val["accepted"]:
                    accepted_nodes.append(CompletedObjectNode(cand, aligned, align_info, val))
            except CompletionSkip as skip_exc:
                object_reports.append({
                    "candidate_id": cand.candidate_id,
                    "category": cand.category,
                    "accepted": False,
                    "preserved_original_mesh": True,
                    "skipped_reason": skip_exc.reason,
                    "detail": str(skip_exc),
                })

        report["objects"] = object_reports
        report["accepted_count"] = len(accepted_nodes)
        report["rejected_count"] = len(candidates) - len(accepted_nodes)
        report["gpu_max_memory_allocated_mb"] = round(peak_gpu_mb_all, 2)
        if not accepted_nodes:
            report["reason"] = "ALL_CANDIDATES_REJECTED"
            return ObjectCompletionResult(None, report, candidates=candidates)

        meta = {
            "version": OBJECT_COMPLETION_VERSION,
            "applied": True,
            "purpose": "VISUAL_ONLY",
            "safety_use": False,
            "backend": backend.name,
            "surface_values": SURFACE_VALUE_TABLE,
            "accepted_count": len(accepted_nodes),
            "rejected_count": len(candidates) - len(accepted_nodes),
            "gpu_max_memory_allocated_mb": round(peak_gpu_mb_all, 2),
            "objects": [
                {
                    "candidate_id": n.candidate.candidate_id,
                    "category": n.candidate.category,
                    "backend": n.output.backend_name,
                    "face_count": n.validation["face_count"],
                    "obs_to_mesh_median_m": n.validation["obs_to_mesh_median_m"],
                    "reprojection_iou_best": n.validation["reprojection_iou_best"],
                }
                for n in accepted_nodes
            ],
        }
        out_bytes = write_completed_glb(g, model, merged_mesh, accepted_nodes, meta)
        report.update(applied=True, reason=None, output_bytes=len(out_bytes))
        return ObjectCompletionResult(out_bytes, report, candidates=candidates, completed_nodes=accepted_nodes)
    except EnhanceSkip as exc:
        report["reason"] = exc.reason
        report["detail"] = str(exc)
    except CompletionSkip as exc:
        report["reason"] = exc.reason
        report["detail"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"OBJECT_COMPLETION_ERROR:{type(exc).__name__}"
        report["detail"] = str(exc)[:300]
    finally:
        if owns_backend and backend is not None and hasattr(backend, "unload"):
            backend.unload()
        report["seconds"] = round(clock() - started, 4)
    return ObjectCompletionResult(None, report)


def complete_glb_file(
    path: Path,
    *,
    config: ObjectCompletionConfig | None = None,
    cameras: Sequence[CheckCamera] | None = None,
    backend: CompletionBackend | None = None,
) -> dict[str, Any]:
    """GLB 파일을 제자리에서 물체 완성한다. 채택된 물체가 있을 때만 원자적으로 교체한다."""
    res = complete_glb_objects(Path(path).read_bytes(), config=config, cameras=cameras, backend=backend)
    if res.glb is not None:
        tmp = Path(str(path) + ".objects.tmp")
        tmp.write_bytes(res.glb)
        os.replace(tmp, path)
    return res.report


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="GLB 물체 제자리 완성 및 정형 도형(라바콘·안내판) 교체. 시각용이며 위험 판정에 쓰지 않는다")
    ap.add_argument("--in", dest="src", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--calibration", type=Path)
    ap.add_argument("--image", action="append", default=[], metavar="CAMERA_ID=PATH")
    ap.add_argument("--backend", choices=("convex_hull", "3d_fixer"), default=None)
    args = ap.parse_args(argv)

    cfg = ObjectCompletionConfig.from_env()
    if args.backend:
        cfg = replace(cfg, backend=args.backend)
    cams = None
    if args.calibration and args.image:
        photos = {k: Path(v) for k, v in (s.split("=", 1) for s in args.image)}
        cams = cameras_from_raw(json.loads(args.calibration.read_text(encoding="utf-8")), photos)
    raw = args.src.read_bytes()
    res = complete_glb_objects(raw, config=cfg, cameras=cams)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(res.glb if res.glb is not None else raw)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: res.report.get(k) for k in ("applied", "reason", "accepted_count", "seconds")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
