"""VLM 위험구역 초안 생성기: 3D 점군·미리보기 메시 기하 후보 추출 + Cosmos3-Nano 이름·등급·이유 제안 (명세 5.3·8.2, S15P31A101-455).

깨면 안 되는 규칙:
1. 초안은 관리자가 수락해 구성 묶음(config_bundle)으로 발행하기 전까지 Tier 0 판정에 절대 쓰이지 않는다(`safety_use=False`, AGENTS 규칙 1·4).
2. 근거가 없으면 빈 목록을 돌려준다(평평한 바닥만 있으면 후보 0개, AGENTS 규칙 2 "모르면 비운다").
3. 좌표계는 World(m, 오른손, +Z 상방, 바닥 Z=0)다(AGENTS 규칙 3).
4. VLM 입력 이미지는 반드시 `prepare_vlm_frames` 얼굴 처리 관문을 거친 `VlmFrame` 만 받으며 사람 이름을 넣지 않는다(AGENTS 규칙 8).
5. VLM 비가용·시간 초과·형식 오류 시에도 결정적 기하 후보는 `"AI 의견 없음"`(`vlm_status="NO_OPINION"`)으로 돌려주어 세팅 흐름을 막지 않는다.
"""

from __future__ import annotations

import json
import math
import struct
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from .client import VlmClient, VlmError
from .face_blur import VlmFrame

PROMPT_VERSION = "vlm-zone-draft-1.0"
NO_OPINION_TEXT = "AI 의견 없음"

CandidateKind = Literal["ELEVATED_WORK_SURFACE", "OPENING", "FLOOR_EDGE"]
EvalType = Literal["GROUND_FOOTPRINT", "VOLUME_3D"]
DraftSeverity = Literal["CRITICAL", "HIGH"]
VlmOpinionStatus = Literal["VLM", "NO_OPINION"]

SYSTEM_PROMPT = (
    "당신은 산업·교실 안전 관제 플랫폼 simai 의 위험구역 초안 보조 모델이다.\n"
    "입력으로 World 좌표계(단위 m, 오른손, +Z 상방, 바닥 Z=0)에서 추출된 3D 기하 후보 수치와 "
    "얼굴이 비식별 처리된 현장 카메라 영상이 주어진다.\n"
    "역할:\n"
    "1. 기하 후보의 종류와 카메라 화면을 보고 관리자가 구역 편집기에서 검토할 구역 이름(recommended_name), "
    "구역 분류(zone_kind: WORK_SURFACE, OPENING, FLOOR_EDGE 중 하나), "
    "권고 위험 등급(recommended_severity: CRITICAL 또는 HIGH), "
    "1~2 문장의 한국어 추천 이유(rationale)를 JSON 으로만 답한다.\n"
    "2. 사람 이름·신원 추측은 절대 적지 않는다.\n"
    "3. 개구부(OPENING)·깊은 단부(FLOOR_EDGE) 추락 위험은 CRITICAL 을 기본으로, "
    "단상·작업대 상판(ELEVATED_WORK_SURFACE)은 높이와 난간 유무에 따라 HIGH 또는 CRITICAL 로 제안한다."
)

RESPONSE_FORMAT: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vlm_zone_draft_assessment",
        "strict": True,
        "schema": {
            "type": "object",
            "required": [
                "recommended_name",
                "zone_kind",
                "recommended_severity",
                "rationale",
                "confidence",
            ],
            "properties": {
                "recommended_name": {"type": "string", "minLength": 1, "maxLength": 160},
                "zone_kind": {
                    "type": "string",
                    "enum": ["WORK_SURFACE", "OPENING", "FLOOR_EDGE"],
                },
                "recommended_severity": {
                    "type": "string",
                    "enum": ["CRITICAL", "HIGH"],
                },
                "rationale": {"type": "string", "minLength": 1, "maxLength": 500},
                "confidence": {
                    "anyOf": [
                        {"type": "number", "minimum": 0.0, "maximum": 1.0},
                        {"type": "null"},
                    ]
                },
            },
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class ZoneGeometryConfig:
    """3D 점군 → 기하 후보 추출 파라미터(World m, 바닥 Z=0 기준)."""

    grid_res_m: float = 0.10
    floor_z_tol_m: float = 0.08
    min_elevated_z_m: float = 0.25
    max_elevated_z_m: float = 2.20
    elevated_plane_z_band_m: float = 0.10
    elevated_max_z_std_m: float = 0.08
    min_surface_area_m2: float = 0.20
    max_surface_area_m2: float = 12.0
    min_surface_points: int = 20
    min_opening_depth_m: float = 0.15
    min_opening_area_m2: float = 0.12
    max_opening_area_m2: float = 9.0
    min_opening_points: int = 8
    min_floor_edge_length_m: float = 0.60
    floor_edge_strip_width_m: float = 0.30
    max_candidates: int = 16


@dataclass(frozen=True)
class CameraPose:
    """카메라 투영 파라미터(World → Camera: X_c = R @ X_w + t, 픽셀: K @ X_c)."""

    camera_id: str
    K: np.ndarray
    R: np.ndarray
    t: np.ndarray
    image_size: tuple[int, int]


def project_world_points(points_xyz: np.ndarray, pose: CameraPose) -> tuple[np.ndarray, np.ndarray]:
    """World (N, 3) 점을 픽셀 (N, 2) 로 투영하고 카메라 앞(Z_c > 0.05) 여부를 함께 돌려준다."""
    pts = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    cam = pts @ np.asarray(pose.R, dtype=np.float64).T + np.asarray(pose.t, dtype=np.float64).reshape(1, 3)
    valid = cam[:, 2] > 0.05
    z_safe = np.where(valid, cam[:, 2], 1.0)
    k = np.asarray(pose.K, dtype=np.float64).reshape(3, 3)
    u = k[0, 0] * (cam[:, 0] / z_safe) + k[0, 2]
    v = k[1, 1] * (cam[:, 1] / z_safe) + k[1, 2]
    return np.column_stack([u, v]), valid


def cameras_from_calibration(doc: Mapping[str, Any]) -> dict[str, CameraPose]:
    """보정 문서 JSON 에서 카메라별 `CameraPose` 사전을 만든다."""
    out: dict[str, CameraPose] = {}
    for cam in doc.get("cameras") or ():
        if not isinstance(cam, Mapping) or not isinstance(cam.get("camera_id"), str):
            continue
        cam_id = cam["camera_id"]
        k_raw = cam.get("K")
        intr = cam.get("intrinsics")
        if k_raw is not None:
            k_mat = np.asarray(k_raw, dtype=np.float64).reshape(3, 3)
        elif isinstance(intr, Mapping):
            if "K" in intr:
                k_mat = np.asarray(intr["K"], dtype=np.float64).reshape(3, 3)
            else:
                fx = float(intr.get("fx", 500.0))
                fy = float(intr.get("fy", fx))
                cx = float(intr.get("cx", 320.0))
                cy = float(intr.get("cy", 240.0))
                k_mat = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
        else:
            continue
        extr = cam.get("extrinsics") if isinstance(cam.get("extrinsics"), Mapping) else cam
        if "R" not in extr or "t" not in extr:
            continue
        r_mat = np.asarray(extr["R"], dtype=np.float64).reshape(3, 3)
        t_vec = np.asarray(extr["t"], dtype=np.float64).reshape(3)
        size_raw = cam.get("image_size") or (intr.get("image_size") if isinstance(intr, Mapping) else None) or [640, 480]
        w, h = int(size_raw[0]), int(size_raw[1])
        out[cam_id] = CameraPose(camera_id=cam_id, K=k_mat, R=r_mat, t=t_vec, image_size=(w, h))
    return out


@dataclass(frozen=True)
class Obb3D:
    """3D 유향 경계 상자(World m, +Z 축 회전 라디안)."""

    center: tuple[float, float, float]
    size: tuple[float, float, float]
    yaw_rad: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": [round(float(v), 4) for v in self.center],
            "size": [round(float(v), 4) for v in self.size],
            "yaw_rad": round(float(self.yaw_rad), 4),
        }


@dataclass(frozen=True)
class GeometryCandidate:
    """3D 맵에서 결정적으로 뽑은 기하 후보 하나."""

    candidate_id: str
    candidate_kind: CandidateKind
    polygon_xy: tuple[tuple[float, float], ...]
    z_min_m: float
    z_max_m: float
    top_height_m: float | None = None
    obb: Obb3D | None = None
    area_m2: float | None = None
    point_count: int | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> GeometryCandidate:
        obb_raw = raw.get("obb")
        obb = None
        if isinstance(obb_raw, Mapping):
            c = obb_raw["center"]
            s = obb_raw["size"]
            obb = Obb3D(
                center=(float(c[0]), float(c[1]), float(c[2])),
                size=(float(s[0]), float(s[1]), float(s[2])),
                yaw_rad=float(obb_raw["yaw_rad"]),
            )
        top_h = raw.get("top_height_m")
        area = raw.get("area_m2")
        pts = raw.get("point_count")
        return cls(
            candidate_id=str(raw["candidate_id"]),
            candidate_kind=raw["candidate_kind"],
            polygon_xy=tuple((float(p[0]), float(p[1])) for p in raw["polygon_xy"]),
            z_min_m=float(raw["z_min_m"]),
            z_max_m=float(raw["z_max_m"]),
            top_height_m=None if top_h is None else float(top_h),
            obb=obb,
            area_m2=None if area is None else float(area),
            point_count=None if pts is None else int(pts),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "candidate_id": self.candidate_id,
            "candidate_kind": self.candidate_kind,
            "polygon_xy": [[round(float(x), 4), round(float(y), 4)] for x, y in self.polygon_xy],
            "z_min_m": round(float(self.z_min_m), 4),
            "z_max_m": round(float(self.z_max_m), 4),
            "top_height_m": None if self.top_height_m is None else round(float(self.top_height_m), 4),
            "obb": None if self.obb is None else self.obb.to_dict(),
        }
        if self.area_m2 is not None:
            out["area_m2"] = round(float(self.area_m2), 4)
        if self.point_count is not None:
            out["point_count"] = int(self.point_count)
        return out


@dataclass(frozen=True)
class ZoneDraftItem:
    """기하 후보 + VLM(또는 기본 규칙) 이름·등급·이유 초안 항목."""

    candidate_id: str
    candidate_kind: CandidateKind
    zone_kind: str
    eval_type: EvalType
    polygon_xy: tuple[tuple[float, float], ...]
    z_min_m: float
    z_max_m: float
    top_height_m: float | None
    obb: Obb3D | None
    recommended_name: str
    recommended_severity: DraftSeverity
    rationale: str
    vlm_status: VlmOpinionStatus
    evidence_cameras: tuple[str, ...]
    confidence: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "candidate_kind": self.candidate_kind,
            "zone_kind": self.zone_kind,
            "eval_type": self.eval_type,
            "polygon_xy": [[round(float(x), 4), round(float(y), 4)] for x, y in self.polygon_xy],
            "z_min_m": round(float(self.z_min_m), 4),
            "z_max_m": round(float(self.z_max_m), 4),
            "top_height_m": None if self.top_height_m is None else round(float(self.top_height_m), 4),
            "obb": None if self.obb is None else self.obb.to_dict(),
            "recommended_name": self.recommended_name,
            "recommended_severity": self.recommended_severity,
            "rationale": self.rationale,
            "vlm_status": self.vlm_status,
            "evidence_cameras": list(self.evidence_cameras),
            "confidence": None if self.confidence is None else round(float(self.confidence), 4),
        }


def extract_geometry_candidates(
    points_xyz: np.ndarray,
    config: ZoneGeometryConfig | None = None,
) -> list[GeometryCandidate]:
    """World(m, +Z 상방, 바닥 Z=0) 3D 점군에서 위험구역·작업면 기하 후보를 결정적으로 추출한다.

    추출 대상:
    1. `ELEVATED_WORK_SURFACE`: 바닥보다 높은(`min_elevated_z_m` ~ `max_elevated_z_m`) 수평 상판(단상·작업대 OBB 후보)
    2. `OPENING`: 바닥면(`Z≈0`) 내부의 빈 구멍(내부 void) 또는 바닥보다 낮은(`z <= -min_opening_depth_m`) 함몰 영역
    3. `FLOOR_EDGE`: 바닥 외곽에서 깊은 낙차(`z <= -min_opening_depth_m`)와 맞닿은 단부 후보

    근거가 부족하거나 평평한 바닥만 있으면 빈 목록 `[]` 을 돌려준다(AGENTS 규칙 2).
    """
    cfg = config or ZoneGeometryConfig()
    pts = np.asarray(points_xyz, dtype=np.float64)
    if pts.ndim != 2 or pts.shape[1] != 3 or len(pts) == 0:
        return []
    valid_mask = np.isfinite(pts).all(axis=1)
    pts = pts[valid_mask]
    if len(pts) == 0:
        return []

    candidates: list[GeometryCandidate] = []

    # 1. ELEVATED_WORK_SURFACE 후보 추출
    elev_mask = (pts[:, 2] >= cfg.min_elevated_z_m) & (pts[:, 2] <= cfg.max_elevated_z_m)
    elev_pts = pts[elev_mask]
    if len(elev_pts) >= cfg.min_surface_points:
        candidates.extend(_extract_elevated_surfaces(elev_pts, cfg))

    # 2. OPENING 및 FLOOR_EDGE 후보 추출
    openings, edges = _extract_openings_and_edges(pts, cfg, existing_elevated=candidates)
    candidates.extend(openings)
    candidates.extend(edges)

    return candidates[: cfg.max_candidates]


def _extract_elevated_surfaces(
    elev_pts: np.ndarray,
    cfg: ZoneGeometryConfig,
) -> list[GeometryCandidate]:
    res = cfg.grid_res_m
    xmin, ymin = elev_pts[:, 0].min(), elev_pts[:, 1].min()
    gx = np.floor((elev_pts[:, 0] - xmin) / res).astype(np.int32)
    gy = np.floor((elev_pts[:, 1] - ymin) / res).astype(np.int32)
    nx = int(gx.max()) + 1
    ny = int(gy.max()) + 1

    occ = np.zeros((ny, nx), dtype=bool)
    occ[gy, gx] = True
    comps = _connected_components(occ, eight_connected=True)

    results: list[GeometryCandidate] = []
    idx = 1
    for comp_cells in comps:
        if len(comp_cells) * (res * res) < cfg.min_surface_area_m2 * 0.5:
            continue
        cell_set = set(comp_cells)
        in_comp = np.array([(int(y), int(x)) in cell_set for x, y in zip(gx, gy, strict=False)], dtype=bool)
        comp_pts = elev_pts[in_comp]
        if len(comp_pts) < cfg.min_surface_points:
            continue

        # 상판 높이 추정: 상위 구간(85 백분위수) 주변의 수평 평면 점들을 모은다
        top_z_est = float(np.percentile(comp_pts[:, 2], 85))
        band_mask = np.abs(comp_pts[:, 2] - top_z_est) <= cfg.elevated_plane_z_band_m
        top_pts = comp_pts[band_mask]
        if len(top_pts) < cfg.min_surface_points:
            continue
        z_std = float(np.std(top_pts[:, 2]))
        if z_std > cfg.elevated_max_z_std_m:
            continue

        top_height_m = float(np.median(top_pts[:, 2]))
        if top_height_m < cfg.min_elevated_z_m:
            continue

        cx, cy, lx, ly, yaw_rad, corners_xy = _fit_obb_2d(top_pts[:, :2], min_extent_m=res)
        area_m2 = lx * ly
        if area_m2 < cfg.min_surface_area_m2 or area_m2 > cfg.max_surface_area_m2:
            continue

        # 상판 점들이 실제로 OBB 면적을 충분히 채우는지 확인(벽면 선형 점군 배제)
        if min(lx, ly) < max(res * 1.8, 0.25):
            continue

        obb = Obb3D(
            center=(cx, cy, top_height_m / 2.0),
            size=(lx, ly, top_height_m),
            yaw_rad=yaw_rad,
        )
        results.append(
            GeometryCandidate(
                candidate_id=f"cand-elevated-{idx:02d}",
                candidate_kind="ELEVATED_WORK_SURFACE",
                polygon_xy=corners_xy,
                z_min_m=0.0,
                z_max_m=round(top_height_m + 2.0, 4),
                top_height_m=round(top_height_m, 4),
                obb=obb,
                area_m2=round(area_m2, 4),
                point_count=int(len(top_pts)),
            )
        )
        idx += 1
    return results


def _extract_openings_and_edges(
    pts: np.ndarray,
    cfg: ZoneGeometryConfig,
    existing_elevated: Sequence[GeometryCandidate],
) -> tuple[list[GeometryCandidate], list[GeometryCandidate]]:
    res = cfg.grid_res_m
    floor_mask = np.abs(pts[:, 2]) <= cfg.floor_z_tol_m
    deep_mask = pts[:, 2] <= -cfg.min_opening_depth_m
    floor_pts = pts[floor_mask]
    deep_pts = pts[deep_mask]

    if len(floor_pts) < 16 and len(deep_pts) < cfg.min_opening_points:
        return [], []

    ref_pts = pts[floor_mask | deep_mask]
    if len(ref_pts) == 0:
        return [], []

    xmin, ymin = float(ref_pts[:, 0].min()), float(ref_pts[:, 1].min())
    xmax, ymax = float(ref_pts[:, 0].max()), float(ref_pts[:, 1].max())
    nx = int(np.floor((xmax - xmin) / res)) + 1
    ny = int(np.floor((ymax - ymin) / res)) + 1
    if nx < 3 or ny < 3 or nx * ny > 250_000:
        return [], []

    floor_occ = np.zeros((ny, nx), dtype=bool)
    if len(floor_pts) > 0:
        fgx = np.clip(np.floor((floor_pts[:, 0] - xmin) / res).astype(np.int32), 0, nx - 1)
        fgy = np.clip(np.floor((floor_pts[:, 1] - ymin) / res).astype(np.int32), 0, ny - 1)
        floor_occ[fgy, fgx] = True

    deep_occ = np.zeros((ny, nx), dtype=bool)
    if len(deep_pts) >= cfg.min_opening_points:
        dgx = np.clip(np.floor((deep_pts[:, 0] - xmin) / res).astype(np.int32), 0, nx - 1)
        dgy = np.clip(np.floor((deep_pts[:, 1] - ymin) / res).astype(np.int32), 0, ny - 1)
        deep_occ[dgy, dgx] = True

    # 이미 높은 작업면이 있는 자리(작업대 아래 그림자)는 바닥 구멍에서 제외한다
    elev_occ = np.zeros((ny, nx), dtype=bool)
    for cand in existing_elevated:
        if cand.obb is not None:
            ecx, ecy, _ = cand.obb.center
            elx, ely, _ = cand.obb.size
            half_diag = 0.5 * math.hypot(elx, ely)
            x0 = max(0, int(np.floor((ecx - half_diag - xmin) / res)))
            x1 = min(nx - 1, int(np.ceil((ecx + half_diag - xmin) / res)))
            y0 = max(0, int(np.floor((ecy - half_diag - ymin) / res)))
            y1 = min(ny - 1, int(np.ceil((ecy + half_diag - ymin) / res)))
            elev_occ[y0 : y1 + 1, x0 : x1 + 1] = True

    openings: list[GeometryCandidate] = []
    edges: list[GeometryCandidate] = []
    opening_idx = 1
    edge_idx = 1

    # (a) 함몰 점군(z <= -min_opening_depth_m) 기반 개구부 / 단부 후보
    if deep_occ.any():
        for comp_cells in _connected_components(deep_occ & ~elev_occ, eight_connected=True):
            cell_set = set(comp_cells)
            in_comp = np.array(
                [
                    (
                        int(np.clip(np.floor((y - ymin) / res), 0, ny - 1)),
                        int(np.clip(np.floor((x - xmin) / res), 0, nx - 1)),
                    )
                    in cell_set
                    for x, y in zip(deep_pts[:, 0], deep_pts[:, 1], strict=False)
                ],
                dtype=bool,
            )
            comp_pts = deep_pts[in_comp]
            if len(comp_pts) < cfg.min_opening_points:
                continue
            cx, cy, lx, ly, yaw_rad, corners_xy = _fit_obb_2d(comp_pts[:, :2], min_extent_m=res)
            area_m2 = lx * ly
            z_min_m = float(np.min(comp_pts[:, 2]))
            touches_border = any(r == 0 or r == ny - 1 or c == 0 or c == nx - 1 for r, c in comp_cells)
            if touches_border and max(lx, ly) >= cfg.min_floor_edge_length_m and floor_occ.any():
                edges.append(
                    GeometryCandidate(
                        candidate_id=f"cand-edge-{edge_idx:02d}",
                        candidate_kind="FLOOR_EDGE",
                        polygon_xy=corners_xy,
                        z_min_m=round(z_min_m, 4),
                        z_max_m=1.2,
                        top_height_m=None,
                        obb=None,
                        area_m2=round(area_m2, 4),
                        point_count=int(len(comp_pts)),
                    )
                )
                edge_idx += 1
            elif cfg.min_opening_area_m2 <= area_m2 <= cfg.max_opening_area_m2:
                openings.append(
                    GeometryCandidate(
                        candidate_id=f"cand-opening-{opening_idx:02d}",
                        candidate_kind="OPENING",
                        polygon_xy=corners_xy,
                        z_min_m=round(z_min_m, 4),
                        z_max_m=0.2,
                        top_height_m=None,
                        obb=None,
                        area_m2=round(area_m2, 4),
                        point_count=int(len(comp_pts)),
                    )
                )
                opening_idx += 1

    # (b) 바닥(Z≈0) 내부에 완전히 둘러싸인 빈 구멍(interior void) 추출
    occupied = floor_occ | deep_occ | elev_occ
    void_mask = ~occupied
    exterior = _flood_fill_border(void_mask)
    interior_holes = void_mask & ~exterior
    if interior_holes.any():
        for comp_cells in _connected_components(interior_holes, eight_connected=False):
            cell_area = len(comp_cells) * (res * res)
            if not (cfg.min_opening_area_m2 <= cell_area <= cfg.max_opening_area_m2):
                continue
            # 둘러싼 바닥 격자가 실제로 사방에 있는지 확인
            if not _is_enclosed_by(comp_cells, floor_occ):
                continue
            cell_centers = np.array(
                [[xmin + (c + 0.5) * res, ymin + (r + 0.5) * res] for r, c in comp_cells],
                dtype=np.float64,
            )
            _, _, lx, ly, _, corners_xy = _fit_obb_2d(cell_centers, min_extent_m=res, pad_m=res * 0.5)
            area_m2 = lx * ly
            if not (cfg.min_opening_area_m2 <= area_m2 <= cfg.max_opening_area_m2):
                continue
            openings.append(
                GeometryCandidate(
                    candidate_id=f"cand-opening-{opening_idx:02d}",
                    candidate_kind="OPENING",
                    polygon_xy=corners_xy,
                    z_min_m=-0.5,
                    z_max_m=0.2,
                    top_height_m=None,
                    obb=None,
                    area_m2=round(area_m2, 4),
                    point_count=0,
                )
            )
            opening_idx += 1

    return openings, edges


def _connected_components(mask: np.ndarray, *, eight_connected: bool) -> list[list[tuple[int, int]]]:
    ny, nx = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    neighbors = (
        ((-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1))
        if eight_connected
        else ((-1, 0), (1, 0), (0, -1), (0, 1))
    )
    comps: list[list[tuple[int, int]]] = []
    for r in range(ny):
        for c in range(nx):
            if not mask[r, c] or visited[r, c]:
                continue
            comp: list[tuple[int, int]] = []
            q: deque[tuple[int, int]] = deque([(r, c)])
            visited[r, c] = True
            while q:
                cr, cc = q.popleft()
                comp.append((cr, cc))
                for dr, dc in neighbors:
                    nr, nc = cr + dr, cc + dc
                    if 0 <= nr < ny and 0 <= nc < nx and mask[nr, nc] and not visited[nr, nc]:
                        visited[nr, nc] = True
                        q.append((nr, nc))
            comps.append(comp)
    return comps


def _flood_fill_border(void_mask: np.ndarray) -> np.ndarray:
    ny, nx = void_mask.shape
    ext = np.zeros_like(void_mask, dtype=bool)
    q: deque[tuple[int, int]] = deque()
    for r in range(ny):
        for c in (0, nx - 1):
            if void_mask[r, c] and not ext[r, c]:
                ext[r, c] = True
                q.append((r, c))
    for c in range(nx):
        for r in (0, ny - 1):
            if void_mask[r, c] and not ext[r, c]:
                ext[r, c] = True
                q.append((r, c))
    while q:
        cr, cc = q.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = cr + dr, cc + dc
            if 0 <= nr < ny and 0 <= nc < nx and void_mask[nr, nc] and not ext[nr, nc]:
                ext[nr, nc] = True
                q.append((nr, nc))
    return ext


def _is_enclosed_by(comp_cells: Sequence[tuple[int, int]], floor_occ: np.ndarray) -> bool:
    ny, nx = floor_occ.shape
    comp_set = set(comp_cells)
    border_floor = 0
    border_total = 0
    for r, c in comp_cells:
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if (nr, nc) in comp_set:
                continue
            border_total += 1
            if 0 <= nr < ny and 0 <= nc < nx and floor_occ[nr, nc]:
                border_floor += 1
    return border_total > 0 and (border_floor / border_total) >= 0.75


def _fit_obb_2d(
    xy: np.ndarray,
    *,
    min_extent_m: float,
    pad_m: float = 0.0,
) -> tuple[float, float, float, float, float, tuple[tuple[float, float], ...]]:
    """2D 점들의 주축(PCA) 유향 경계 상자(중심, 가로·세로 길이, yaw_rad, 반시계 4꼭짓점)."""
    mean = xy.mean(axis=0)
    centered = xy - mean
    if len(xy) >= 3:
        cov = np.cov(centered, rowvar=False)
        eigvals, eigvecs = np.linalg.eigh(cov)
        # 가장 큰 고유값 방향을 +X 주축으로, 거의 대칭이면 축 정렬(yaw=0)로 고정한다
        if eigvals[1] > 1e-6 and (eigvals[1] - eigvals[0]) / max(eigvals[1], 1e-6) > 0.15:
            axis = eigvecs[:, 1]
            yaw_rad = float(math.atan2(axis[1], axis[0]))
            if yaw_rad > math.pi / 2:
                yaw_rad -= math.pi
            elif yaw_rad <= -math.pi / 2:
                yaw_rad += math.pi
        else:
            yaw_rad = 0.0
    else:
        yaw_rad = 0.0

    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    rot = np.array([[c, s], [-s, c]], dtype=np.float64)
    local = centered @ rot.T
    lmin = local.min(axis=0) - pad_m
    lmax = local.max(axis=0) + pad_m
    lx = max(float(lmax[0] - lmin[0]), min_extent_m)
    ly = max(float(lmax[1] - lmin[1]), min_extent_m)
    local_center = 0.5 * (lmin + lmax)
    world_center = mean + rot.T @ local_center
    cx, cy = float(world_center[0]), float(world_center[1])

    hx, hy = 0.5 * lx, 0.5 * ly
    local_corners = np.array([[-hx, -hy], [hx, -hy], [hx, hy], [-hx, hy]], dtype=np.float64)
    world_corners = world_center + local_corners @ rot
    corners = tuple((float(p[0]), float(p[1])) for p in world_corners)
    return cx, cy, lx, ly, yaw_rad, corners


def load_points_from_bytes(data: bytes, uri: str = "") -> np.ndarray:
    """미리보기 GLB(`.glb`) 또는 점군(`.ply`, `.json`) 바이트에서 World XYZ (N, 3) 점군을 읽는다."""
    if len(data) >= 12 and data[:4] == b"glTF":
        return _load_points_from_glb(data)
    if data.startswith(b"ply"):
        return _load_points_from_ply(data)
    try:
        parsed = json.loads(data.decode("utf-8"))
        if isinstance(parsed, Mapping) and "points_xyz" in parsed:
            arr = np.asarray(parsed["points_xyz"], dtype=np.float64)
        else:
            arr = np.asarray(parsed, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[1] == 3:
            return arr
    except (UnicodeDecodeError, ValueError, TypeError):
        pass
    raise ValueError(f"지원하지 않는 3D 점군/메시 형식이다: {uri or '<bytes>'}")


def _load_points_from_glb(data: bytes) -> np.ndarray:
    from simai_ai.scene.glb_enhance import _node_matrix
    from simai_ai.scene.texture_mesh import GLTF_Y_UP

    magic, version, total_len = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF" or version != 2 or total_len > len(data):
        raise ValueError("손상된 GLB 헤더다")
    offset = 12
    json_chunk: dict[str, Any] | None = None
    bin_chunk = b""
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        chunk_data = data[offset : offset + chunk_len]
        offset += chunk_len
        if chunk_type == 0x4E4F534A:  # JSON
            json_chunk = json.loads(chunk_data.decode("utf-8"))
        elif chunk_type == 0x004E4942:  # BIN
            bin_chunk = chunk_data
    if json_chunk is None:
        raise ValueError("GLB JSON 청크가 없다")

    extras = json_chunk.get("extras")
    simai = extras.get("simai") if isinstance(extras, dict) else None
    if isinstance(simai, dict) and simai.get("axis_convention") not in (None, "GLTF_Y_UP"):
        raise ValueError(f"AXIS_NOT_WORLD: axis_convention={simai.get('axis_convention')}")

    accessors = json_chunk.get("accessors") or []
    buffer_views = json_chunk.get("bufferViews") or []
    meshes = json_chunk.get("meshes") or []
    arrays: list[np.ndarray] = []
    nodes = json_chunk.get("nodes") or []
    instances: list[tuple[int, np.ndarray]] = []
    if nodes:
        scenes = json_chunk.get("scenes") or []
        if scenes:
            roots = scenes[json_chunk.get("scene", 0)].get("nodes", [])
        else:
            children = {child for node in nodes for child in node.get("children", [])}
            roots = [i for i in range(len(nodes)) if i not in children]
        stack = [(i, np.eye(4), frozenset()) for i in reversed(roots)]
        while stack:
            i, parent, ancestors = stack.pop()
            if i in ancestors:
                raise ValueError("GLB 노드 계층에 순환이 있다")
            node = nodes[i]
            matrix = parent @ _node_matrix(node)
            if "mesh" in node:
                instances.append((node["mesh"], matrix))
            stack.extend((child, matrix, ancestors | {i}) for child in reversed(node.get("children", [])))
    else:
        instances = [(i, np.eye(4)) for i in range(len(meshes))]
    for mesh_index, matrix in instances:
        mesh = meshes[mesh_index]
        for prim in mesh.get("primitives") or []:
            pos_idx = (prim.get("attributes") or {}).get("POSITION")
            if pos_idx is None or pos_idx >= len(accessors):
                continue
            acc = accessors[pos_idx]
            if acc.get("componentType") != 5126 or acc.get("type") != "VEC3":
                continue
            bv_idx = acc.get("bufferView")
            if bv_idx is None or bv_idx >= len(buffer_views):
                continue
            bv = buffer_views[bv_idx]
            count = int(acc.get("count", 0))
            byte_offset = int(bv.get("byteOffset", 0)) + int(acc.get("byteOffset", 0))
            byte_stride = int(bv.get("byteStride") or 12)
            if count <= 0:
                continue
            if byte_stride == 12:
                end = byte_offset + count * 12
                if end > len(bin_chunk):
                    raise ValueError("GLB BIN 청크 범위를 벗어났다")
                pts = np.frombuffer(bin_chunk[byte_offset:end], dtype="<f4").reshape(count, 3).astype(np.float64)
            else:
                rows = [
                    struct.unpack_from("<fff", bin_chunk, byte_offset + i * byte_stride)
                    for i in range(count)
                ]
                pts = np.asarray(rows, dtype=np.float64)
            arrays.append((pts @ matrix[:3, :3].T + matrix[:3, 3]) @ GLTF_Y_UP)
    if not arrays:
        return np.zeros((0, 3), dtype=np.float64)
    return np.vstack(arrays)


def _load_points_from_ply(data: bytes) -> np.ndarray:
    """PLY 정점은 이미 World(m, +Z 상방) 좌표라고 가정한다."""
    header_end = data.find(b"end_header\n")
    nl_len = 1
    if header_end < 0:
        header_end = data.find(b"end_header\r\n")
        nl_len = 2
    if header_end < 0:
        raise ValueError("PLY 헤더 끝(end_header)이 없다")
    header = data[:header_end].decode("ascii", errors="replace")
    body = data[header_end + len("end_header") + nl_len :]
    vertex_count = 0
    is_binary = False
    props: list[str] = []
    in_vertex = False
    for line in header.splitlines():
        parts = line.strip().split()
        if not parts:
            continue
        if parts[0] == "format":
            is_binary = "binary_little_endian" in parts[1]
        elif parts[0] == "element":
            in_vertex = parts[1] == "vertex"
            if in_vertex:
                vertex_count = int(parts[2])
        elif parts[0] == "property" and in_vertex:
            props.append(parts[-1])
    if vertex_count <= 0:
        return np.zeros((0, 3), dtype=np.float64)
    if is_binary:
        # float32 x, y, z 앞부분 읽기
        stride = len(props) * 4
        if len(body) < vertex_count * 12:
            raise ValueError("PLY 바이너리 본문이 짧다")
        raw = np.frombuffer(body[: vertex_count * stride], dtype="<f4").reshape(vertex_count, len(props))
        return raw[:, :3].astype(np.float64)
    rows: list[list[float]] = []
    for line in body.decode("ascii", errors="replace").splitlines():
        vals = line.strip().split()
        if len(vals) >= 3:
            rows.append([float(vals[0]), float(vals[1]), float(vals[2])])
            if len(rows) >= vertex_count:
                break
    return np.asarray(rows, dtype=np.float64) if rows else np.zeros((0, 3), dtype=np.float64)


def default_draft_for_candidate(
    candidate: GeometryCandidate,
    index: int,
    *,
    evidence_cameras: Sequence[str] = (),
) -> ZoneDraftItem:
    """VLM 비가용·실패 시 결정적 기하 후보를 'AI 의견 없음' 초안으로 변환한다."""
    if candidate.candidate_kind == "ELEVATED_WORK_SURFACE":
        zone_kind = "WORK_SURFACE"
        eval_type: EvalType = "VOLUME_3D"
        severity: DraftSeverity = "HIGH"
        name = f"고소 작업면 후보 {index}"
    elif candidate.candidate_kind == "OPENING":
        zone_kind = "OPENING"
        eval_type = "GROUND_FOOTPRINT"
        severity = "CRITICAL"
        name = f"바닥 개구부 후보 {index}"
    else:
        zone_kind = "FLOOR_EDGE"
        eval_type = "GROUND_FOOTPRINT"
        severity = "CRITICAL"
        name = f"바닥 단부 후보 {index}"

    return ZoneDraftItem(
        candidate_id=candidate.candidate_id,
        candidate_kind=candidate.candidate_kind,
        zone_kind=zone_kind,
        eval_type=eval_type,
        polygon_xy=candidate.polygon_xy,
        z_min_m=candidate.z_min_m,
        z_max_m=candidate.z_max_m,
        top_height_m=candidate.top_height_m,
        obb=candidate.obb,
        recommended_name=name,
        recommended_severity=severity,
        rationale=NO_OPINION_TEXT,
        vlm_status="NO_OPINION",
        evidence_cameras=tuple(evidence_cameras),
        confidence=None,
    )


def select_candidate_crops(
    candidate: GeometryCandidate,
    frames_by_camera: Mapping[str, VlmFrame],
    cameras_by_id: Mapping[str, CameraPose],
    *,
    max_views: int = 2,
    pad_ratio: float = 0.25,
    min_crop_px: int = 64,
) -> tuple[list[VlmFrame], list[str]]:
    """기하 후보가 보이는 카메라를 고르고 얼굴 처리된 `VlmFrame` 에서 후보 주변 영역을 잘라낸다.

    보정 카메라가 없거나 투영점이 화면 밖이면, 들어온 `frames_by_camera` 순서대로 전체 `VlmFrame` 을 그대로 쓴다.
    """
    z_plane = candidate.top_height_m if candidate.top_height_m is not None else 0.0
    pts_3d = np.array([[x, y, z_plane] for x, y in candidate.polygon_xy], dtype=np.float64)

    scored: list[tuple[int, str, VlmFrame]] = []
    evidence: list[str] = []

    for cam_id, frame in frames_by_camera.items():
        pose = cameras_by_id.get(cam_id)
        if pose is None:
            continue
        h, w = frame.pixels.shape[:2]
        uv, valid = project_world_points(pts_3d, pose)
        in_view = valid & (uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)
        visible_count = int(np.sum(in_view))
        if visible_count < 2:
            continue
        evidence.append(cam_id)
        vis_uv = uv[in_view]
        umin, vmin = float(vis_uv[:, 0].min()), float(vis_uv[:, 1].min())
        umax, vmax = float(vis_uv[:, 0].max()), float(vis_uv[:, 1].max())
        bw = max(umax - umin, float(min_crop_px))
        bh = max(vmax - vmin, float(min_crop_px))
        cx, cy = 0.5 * (umin + umax), 0.5 * (vmin + vmax)
        half_w = 0.5 * bw * (1.0 + 2.0 * pad_ratio)
        half_h = 0.5 * bh * (1.0 + 2.0 * pad_ratio)
        x1 = max(0, int(math.floor(cx - half_w)))
        y1 = max(0, int(math.floor(cy - half_h)))
        x2 = min(w, int(math.ceil(cx + half_w)))
        y2 = min(h, int(math.ceil(cy + half_h)))
        if x2 - x1 >= 16 and y2 - y1 >= 16:
            cropped = frame.with_crop(x1, y1, x2, y2)
        else:
            cropped = frame
        scored.append((visible_count, cam_id, cropped))

    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        chosen = [item[2] for item in scored[:max_views]]
        return chosen, sorted(set(evidence))

    # 투영 카메라 정보가 없으면 얼굴 처리된 원본 프레임을 최대 max_views 장까지 그대로 넘긴다
    fallback_frames = list(frames_by_camera.values())[:max_views]
    fallback_cams = list(frames_by_camera.keys())[:max_views]
    return fallback_frames, fallback_cams


def build_candidate_user_prompt(candidate: GeometryCandidate, evidence_cameras: Sequence[str]) -> str:
    """기하 후보 수치 설명을 한국어 프롬프트로 조립한다(사람 정보 없음)."""
    poly_str = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in candidate.polygon_xy[:8])
    lines = [
        f"기하 후보 ID: {candidate.candidate_id}",
        f"기하 후보 종류(candidate_kind): {candidate.candidate_kind}",
        f"바닥 다각형 꼭짓점 World XY(m): [{poly_str}]",
        f"높이 범위 World Z(m): [{candidate.z_min_m:.2f}, {candidate.z_max_m:.2f}]",
    ]
    if candidate.top_height_m is not None:
        lines.append(f"추정 상판 높이(top_height_m): {candidate.top_height_m:.2f} m")
    if candidate.obb is not None:
        c = candidate.obb.center
        s = candidate.obb.size
        lines.append(
            f"3D OBB 중심=({c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f}) m, "
            f"크기=({s[0]:.2f}, {s[1]:.2f}, {s[2]:.2f}) m, 회전={candidate.obb.yaw_rad:.2f} rad"
        )
    if candidate.area_m2 is not None:
        lines.append(f"추정 면적: {candidate.area_m2:.2f} m^2")
    if evidence_cameras:
        lines.append(f"관측 카메라: {', '.join(evidence_cameras)}")
    lines.append(
        "위 기하 후보와 첨부된 카메라 화면을 바탕으로 recommended_name, zone_kind, "
        "recommended_severity(CRITICAL 또는 HIGH), rationale(1~2문장), confidence 를 JSON 으로 답하라."
    )
    return "\n".join(lines)


def parse_candidate_vlm_response(
    raw_content: str,
    candidate: GeometryCandidate,
    *,
    evidence_cameras: Sequence[str] = (),
) -> ZoneDraftItem:
    """Cosmos3-Nano JSON 응답을 검사해 `ZoneDraftItem` 으로 변환한다. 형식이 어긋나면 `ValueError`."""
    try:
        doc = json.loads(raw_content)
    except ValueError as e:
        raise ValueError(f"VLM 응답이 JSON 이 아니다: {e}") from e
    if not isinstance(doc, Mapping):
        raise ValueError("VLM 응답이 JSON 객체가 아니다")

    name = doc.get("recommended_name")
    zone_kind = doc.get("zone_kind")
    severity = doc.get("recommended_severity")
    rationale = doc.get("rationale")
    confidence = doc.get("confidence")

    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
        raise ValueError(f"유효하지 않은 recommended_name: {name!r}")
    if not isinstance(zone_kind, str) or not zone_kind.strip() or len(zone_kind.strip()) > 64:
        raise ValueError(f"유효하지 않은 zone_kind: {zone_kind!r}")
    if severity not in ("CRITICAL", "HIGH"):
        raise ValueError(f"recommended_severity 는 CRITICAL 또는 HIGH 여야 한다: {severity!r}")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale.strip()) > 500:
        raise ValueError(f"유효하지 않은 rationale: {rationale!r}")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ValueError(f"유효하지 않은 confidence: {confidence!r}")
        conf_val: float | None = float(confidence)
        if not (0.0 <= conf_val <= 1.0):
            raise ValueError(f"confidence 범위(0..1) 위반: {conf_val}")
    else:
        conf_val = None

    eval_type: EvalType = "VOLUME_3D" if candidate.candidate_kind == "ELEVATED_WORK_SURFACE" else "GROUND_FOOTPRINT"
    return ZoneDraftItem(
        candidate_id=candidate.candidate_id,
        candidate_kind=candidate.candidate_kind,
        zone_kind=zone_kind.strip(),
        eval_type=eval_type,
        polygon_xy=candidate.polygon_xy,
        z_min_m=candidate.z_min_m,
        z_max_m=candidate.z_max_m,
        top_height_m=candidate.top_height_m,
        obb=candidate.obb,
        recommended_name=name.strip(),
        recommended_severity=severity,
        rationale=rationale.strip(),
        vlm_status="VLM",
        evidence_cameras=tuple(evidence_cameras),
        confidence=conf_val,
    )


def assess_geometry_candidates(
    candidates: Sequence[GeometryCandidate],
    *,
    vlm_client: Any | None,
    frames_by_camera: Mapping[str, VlmFrame] | None = None,
    cameras_by_id: Mapping[str, CameraPose] | None = None,
) -> tuple[list[ZoneDraftItem], Literal["SUCCEEDED", "NO_OPINION"], str | None]:
    """기하 후보 목록에 대해 Cosmos3-Nano 의견을 붙여 `(drafts, status, model_version)` 을 돌려준다.

    - `vlm_client` 가 없거나(`None`), 얼굴 처리된 프레임이 없거나, VLM 호출·파싱이 실패하면
      해당 후보는 `default_draft_for_candidate(..., rationale="AI 의견 없음", vlm_status="NO_OPINION")` 로 채운다.
    - 후보 중 하나라도 VLM 의견이 반영되었으면 `status="SUCCEEDED"`, 그 외에는 `status="NO_OPINION"`, `model_version=None`.
    """
    frames_map = frames_by_camera or {}
    cams_map = cameras_by_id or {}

    if not candidates:
        model_name = getattr(getattr(vlm_client, "settings", None), "model", None) or getattr(vlm_client, "model", None)
        if vlm_client is not None and isinstance(model_name, str) and model_name:
            return [], "SUCCEEDED", model_name
        return [], "NO_OPINION", None

    drafts: list[ZoneDraftItem] = []
    any_vlm_succeeded = False
    model_version: str | None = None

    for idx, cand in enumerate(candidates, start=1):
        crops, evidence_cams = select_candidate_crops(cand, frames_map, cams_map)
        if vlm_client is None or not crops:
            drafts.append(default_draft_for_candidate(cand, idx, evidence_cameras=evidence_cams))
            continue

        user_prompt = build_candidate_user_prompt(cand, evidence_cams)
        try:
            completion = vlm_client.complete(
                user_prompt,
                crops,
                system=SYSTEM_PROMPT,
                response_format=RESPONSE_FORMAT,
            )
            item = parse_candidate_vlm_response(
                completion.content,
                cand,
                evidence_cameras=evidence_cams,
            )
            drafts.append(item)
            any_vlm_succeeded = True
            model_version = completion.model or getattr(getattr(vlm_client, "settings", None), "model", None) or "cosmos3-nano"
        except (VlmError, ValueError, TypeError):
            drafts.append(default_draft_for_candidate(cand, idx, evidence_cameras=evidence_cams))

    if any_vlm_succeeded and model_version:
        return drafts, "SUCCEEDED", model_version
    return drafts, "NO_OPINION", None
