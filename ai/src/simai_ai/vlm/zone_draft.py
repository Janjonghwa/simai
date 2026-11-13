"""VLM 위험구역 초안 생성기: 3D 점군·미리보기 메시 기하 후보 추출 + Cosmos3-Nano 이름·등급·이유 제안 (명세 5.3·8.2, S15P31A101-455).

깨면 안 되는 규칙:
1. 초안은 관리자가 수락해 구성 묶음(config_bundle)으로 발행하기 전까지 Tier 0 판정에 절대 쓰이지 않는다(`safety_use=False`, AGENTS 규칙 1·4).
2. 근거가 없으면 빈 목록을 돌려준다(평평한 바닥만 있으면 후보 0개, AGENTS 규칙 2 "모르면 비운다").
3. 좌표계는 World(m, 오른손, +Z 상방, 바닥 Z=0)다(AGENTS 규칙 3).
4. VLM 입력 이미지는 반드시 `prepare_vlm_frames` 얼굴 처리 관문을 거친 `VlmFrame` 만 받으며 사람 이름을 넣지 않는다(AGENTS 규칙 8).
5. VLM 비가용·시간 초과·형식 오류 시에도 결정적 기하 후보는 `"AI 의견 없음"`(`vlm_status="NO_OPINION"`)으로 돌려주어 세팅 흐름을 막지 않는다.
6. 시연 개구부 스위치(S15P31A101-671, `ZoneDraftOptions.opening_only`, 기본 켬): 후보를 `OPENING` 으로 제한하고, 단상 상판에 둘러싸인
   낮은·빈 자리 후보·가려진 바닥 빈 칸 거르기·겹친 후보 합치기·노랑·검정 줄무늬 화면 근거·개구부 확인 프롬프트를 쓴다. 끄면 이전 동작 그대로다.
   미리 만든 물체 도형은 넣지 않는다(후보는 관측 점군에서만 나온다). 근거는 docs/design/S15P31A101-671-zone-draft-demo-opening.md.
"""

from __future__ import annotations

import json
import math
import struct
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal

import numpy as np

from .client import VlmClient, VlmError
from .face_blur import VlmFrame
from .hazard_stripes import (
    StripeConfig,
    convex_hull,
    convex_polygon_mask,
    hazard_stripe_mask,
)

PROMPT_VERSION = "vlm-zone-draft-1.0"
NO_OPINION_TEXT = "AI 의견 없음"

CandidateKind = Literal["ELEVATED_WORK_SURFACE", "OPENING", "FLOOR_EDGE"]
EvalType = Literal["GROUND_FOOTPRINT", "VOLUME_3D"]
DraftSeverity = Literal["CRITICAL", "HIGH"]
VlmOpinionStatus = Literal["VLM", "NO_OPINION"]

# 'AI 의견 없음' 초안의 종류 이름. 관제 구역 편집기의 자동 이름(zoneEditModel.ts AUTO_ZONE_NAME)과 같은 말을 쓴다.
DEFAULT_NAME_BY_KIND: Mapping[CandidateKind, str] = {
    "ELEVATED_WORK_SURFACE": "고소작업면",
    "OPENING": "개구부",
    "FLOOR_EDGE": "바닥 단부",
}
_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

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
    # 주축이 방 축(World +X·+Y, 세팅 때 벽에 맞춘다)과 이 각도(도)보다 덜 틀어졌으면 축 정렬(yaw=0)로 맞춘다(S15P31A101-651).
    # 미리보기 점구름의 PCA 주축은 상판 반사·가림으로 수 도씩 흔들리는데, 회전한 상자는 구역 편집기에서 손잡이로 고칠 수 없다.
    # 10° 는 14차 로비 실측(작업대 81.9° = 축에서 8.1°)을 넣고 실제로 비스듬히 놓인 물체(보통 15° 이상)는 남기는 값이다.
    axis_snap_max_deg: float = 10.0
    # ---- S15P31A101-671 시연 개구부. 아래 기본값은 모두 '끔'(이전 동작)이고, `ZoneDraftOptions.geometry_config` 가 켠다.
    # 낼 후보 종류(None 이면 전부). 시연은 ("OPENING",)
    allowed_kinds: tuple[CandidateKind, ...] | None = None
    # 단상 상판으로 둘러싸인 낮은·빈 자리 개구부 후보(`_extract_platform_openings`)
    platform_openings: bool = False
    # 단상 상판으로 볼 칸 높이 상한(m). 이보다 높은 칸(벽·기둥·난간 기둥)은 단상이 아니다. 14차 로비 단상 ≈0.45 m, 탁자 0.75 m
    platform_max_top_z_m: float = 0.90
    # 단상 상판 덩어리 최소 면적(m²)·최소 폭(m). 둥근 탁자(지름 0.6~0.8 m, ≈0.4 m²)·의자는 빠지고 단상(≈1.2 m × 2.5 m)은 남는다
    platform_min_area_m2: float = 0.60
    platform_min_width_m: float = 0.40
    # 둘러싼 상판보다 이만큼(m) 이상 낮으면 '낮은 자리'. 바닥 함몰 기준(min_opening_depth_m)과 같은 0.15 m. 방석(상판과 비슷한 높이)은 빠진다
    platform_opening_min_drop_m: float = 0.15
    # 마주 보는 두 상판 사이 폭 상한(m). 사람이 빠질 수 있는 틈(0.3 m~)부터 두 단상 사이 통로 정도까지만 개구부로 본다
    platform_opening_max_width_m: float = 1.60
    platform_opening_max_area_m2: float = 4.0
    # 바닥 안 빈 칸 개구부((b))를 그 칸을 가림 없이 보는 카메라가 이 수 이상일 때만 낸다(0 이면 거르지 않음). 카메라 정보가 없으면 모두 버린다
    floor_void_min_visible_cameras: int = 0
    # 겹친 후보 합치기: 바닥 다각형 IoU 이상이거나 중심 거리(m) 이하면 하나만 남긴다(0 이면 끔)
    dedup_iou: float = 0.0
    dedup_center_m: float = 0.0


@dataclass(frozen=True)
class CameraPose:
    """카메라 투영 파라미터(World → Camera: X_c = R @ X_w + t, 픽셀: K @ X_c)."""

    camera_id: str
    K: np.ndarray
    R: np.ndarray
    t: np.ndarray
    image_size: tuple[int, int]
    # OpenCV 방사·접선 왜곡 계수(k1, k2, p1, p2, k3). 없으면 None. 줄무늬 근거 투영만 쓴다(기존 크롭 투영은 핀홀 그대로, S15P31A101-671)
    distortion: tuple[float, float, float, float, float] | None = None


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
        out[cam_id] = CameraPose(
            camera_id=cam_id, K=k_mat, R=r_mat, t=t_vec, image_size=(w, h), distortion=_distortion_coeffs(cam.get("distortion"))
        )
    return out


def _distortion_coeffs(raw: Any) -> tuple[float, float, float, float, float] | None:
    """보정 문서 `distortion`(`{"model": "OPENCV_PINHOLE", "coeffs": [k1, k2, p1, p2, k3]}`) → 5개 계수. 모르는 모델이면 None."""
    if not isinstance(raw, Mapping) or raw.get("model") not in (None, "OPENCV_PINHOLE", "OPENCV"):
        return None
    coeffs = raw.get("coeffs")
    if not isinstance(coeffs, Sequence) or isinstance(coeffs, str | bytes) or not coeffs:
        return None
    try:
        vals = [float(v) for v in list(coeffs)[:5]]
    except (TypeError, ValueError):
        return None
    vals += [0.0] * (5 - len(vals))
    if not all(math.isfinite(v) for v in vals) or not any(vals):
        return None
    return (vals[0], vals[1], vals[2], vals[3], vals[4])


def _distortion_max_r2(coeffs: Sequence[float]) -> float:
    """왜곡 다항식이 단조 증가하는 정규화 반경² 상한(넘으면 투영이 접혀 믿을 수 없다)."""
    k1, k2, _, _, k3 = coeffs
    for r in np.arange(0.05, 3.0, 0.01):
        r2 = r * r
        # d/dr [r (1 + k1 r² + k2 r⁴ + k3 r⁶)]
        if 1.0 + 3.0 * k1 * r2 + 5.0 * k2 * r2 * r2 + 7.0 * k3 * r2**3 <= 0.0:
            return float((r - 0.01) ** 2)
    return 9.0


def project_world_points_distorted(points_xyz: np.ndarray, pose: CameraPose) -> tuple[np.ndarray, np.ndarray]:
    """World (N, 3) 점을 왜곡 포함 픽셀 (N, 2) 로 투영한다. 카메라 앞이고 왜곡 모델이 접히지 않는 점만 유효(S15P31A101-671)."""
    if pose.distortion is None:
        return project_world_points(points_xyz, pose)
    pts = np.asarray(points_xyz, dtype=np.float64).reshape(-1, 3)
    cam = pts @ np.asarray(pose.R, dtype=np.float64).T + np.asarray(pose.t, dtype=np.float64).reshape(1, 3)
    valid = cam[:, 2] > 0.05
    z_safe = np.where(valid, cam[:, 2], 1.0)
    x = cam[:, 0] / z_safe
    y = cam[:, 1] / z_safe
    k1, k2, p1, p2, k3 = pose.distortion
    r2 = x * x + y * y
    valid &= r2 <= _distortion_max_r2(pose.distortion)
    radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2**3
    xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
    yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
    k = np.asarray(pose.K, dtype=np.float64).reshape(3, 3)
    return np.column_stack([k[0, 0] * xd + k[0, 2], k[1, 1] * yd + k[1, 2]]), valid


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
    # ---- S15P31A101-671 근거 수치(계약 밖, to_dict 에 싣지 않는다). 프롬프트·순위·이유 문구에만 쓴다
    # 출처: PLATFORM_GAP(단상 사이 낮은·빈 자리), FLOOR_DEPRESSION(바닥보다 낮은 점), FLOOR_VOID(바닥 안 빈 칸). 모르면 None
    source: str | None = None
    # 단상 상판이 막아 선 방향 수(±X·±Y 중, 2~4)
    enclosed_directions: int | None = None
    # 둘러싼 상판 높이 중앙값(m)과, 관측된 칸의 상판 대비 낮은 정도(m, 관측 칸이 없으면 None)
    surround_top_m: float | None = None
    drop_m: float | None = None
    # 후보 칸 중 점이 하나도 없는 칸 비율
    empty_ratio: float | None = None

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
    *,
    cameras: Mapping[str, CameraPose] | None = None,
) -> list[GeometryCandidate]:
    """World(m, +Z 상방, 바닥 Z=0) 3D 점군에서 위험구역·작업면 기하 후보를 결정적으로 추출한다.

    추출 대상:
    1. `ELEVATED_WORK_SURFACE`: 바닥보다 높은(`min_elevated_z_m` ~ `max_elevated_z_m`) 수평 상판(단상·작업대 OBB 후보)
    2. `OPENING`: 바닥면(`Z≈0`) 내부의 빈 구멍(내부 void) 또는 바닥보다 낮은(`z <= -min_opening_depth_m`) 함몰 영역
    3. `FLOOR_EDGE`: 바닥 외곽에서 깊은 낙차(`z <= -min_opening_depth_m`)와 맞닿은 단부 후보

    근거가 부족하거나 평평한 바닥만 있으면 빈 목록 `[]` 을 돌려준다(AGENTS 규칙 2).

    S15P31A101-671 설정(기본 끔): `platform_openings` 면 단상 상판에 둘러싸인 낮은·빈 자리도 `OPENING` 으로 내고,
    `floor_void_min_visible_cameras` 면 (2) 의 바닥 빈 칸을 `cameras` 로 가림 없이 본 칸만 남기며, `allowed_kinds`·`dedup_*` 로
    종류를 거르고 겹친 후보를 합친 뒤 `max_candidates` 로 자른다.
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
    openings, edges = _extract_openings_and_edges(pts, cfg, existing_elevated=candidates, cameras=cameras)
    candidates.extend(openings)
    candidates.extend(edges)

    # 3. 단상 상판으로 둘러싸인 낮은·빈 자리(S15P31A101-671)
    if cfg.platform_openings:
        candidates.extend(_extract_platform_openings(pts, cfg))

    candidates = filter_and_dedup_candidates(candidates, cfg)
    return candidates[: cfg.max_candidates]


def filter_and_dedup_candidates(
    candidates: Sequence[GeometryCandidate], cfg: ZoneGeometryConfig
) -> list[GeometryCandidate]:
    """`allowed_kinds` 밖 후보를 빼고 겹친 후보를 하나로 합친다(S15P31A101-671). 설정이 꺼져 있으면 그대로 돌려준다.

    합칠 때는 새 도형을 만들지 않고 우선순위가 높은 후보 하나를 남긴다(단상 사이 > 바닥 함몰 > 바닥 빈 칸 > 그 밖, 같으면 넓은 쪽).
    """
    out = [c for c in candidates if cfg.allowed_kinds is None or c.candidate_kind in cfg.allowed_kinds]
    if cfg.dedup_iou <= 0.0 and cfg.dedup_center_m <= 0.0:
        return out
    order = sorted(
        range(len(out)),
        key=lambda i: (_SOURCE_PRIORITY.get(out[i].source or "", 9), -_polygon_area(out[i].polygon_xy), i),
    )
    kept: list[int] = []
    for i in order:
        if any(_same_place(out[i], out[j], cfg) for j in kept):
            continue
        kept.append(i)
    return [out[i] for i in sorted(kept)]


_SOURCE_PRIORITY: Mapping[str, int] = {"PLATFORM_GAP": 0, "FLOOR_DEPRESSION": 1, "FLOOR_VOID": 2}


def _polygon_area(poly: Sequence[tuple[float, float]]) -> float:
    if len(poly) < 3:
        return 0.0
    xy = np.asarray(poly, dtype=np.float64)
    x, y = xy[:, 0], xy[:, 1]
    return float(0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _same_place(a: GeometryCandidate, b: GeometryCandidate, cfg: ZoneGeometryConfig) -> bool:
    """같은 종류이고 바닥 다각형이 IoU `dedup_iou` 이상 겹치거나 중심이 `dedup_center_m` 안이면 같은 자리다."""
    if a.candidate_kind != b.candidate_kind:
        return False
    pa = np.asarray(a.polygon_xy, dtype=np.float64)
    pb = np.asarray(b.polygon_xy, dtype=np.float64)
    if cfg.dedup_center_m > 0.0 and float(np.linalg.norm(pa.mean(axis=0) - pb.mean(axis=0))) <= cfg.dedup_center_m:
        return True
    return cfg.dedup_iou > 0.0 and polygon_iou(pa, pb) >= cfg.dedup_iou


def polygon_iou(a: np.ndarray, b: np.ndarray, cell_m: float = 0.05) -> float:
    """두 바닥 다각형(볼록 껍질로 근사)의 IoU 를 `cell_m` 격자로 잰다."""
    ha = convex_hull(a)
    hb = convex_hull(b)
    if len(ha) < 3 or len(hb) < 3:
        return 0.0
    lo = np.minimum(ha.min(axis=0), hb.min(axis=0))
    hi = np.maximum(ha.max(axis=0), hb.max(axis=0))
    shape = (int(np.ceil((hi[1] - lo[1]) / cell_m)) + 1, int(np.ceil((hi[0] - lo[0]) / cell_m)) + 1)
    if shape[0] * shape[1] > 4_000_000:
        return 0.0
    ma = convex_polygon_mask((ha - lo) / cell_m, shape)
    mb = convex_polygon_mask((hb - lo) / cell_m, shape)
    union = int(np.count_nonzero(ma | mb))
    return 0.0 if union == 0 else float(np.count_nonzero(ma & mb)) / union


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

        cx, cy, lx, ly, yaw_rad, corners_xy = _fit_obb_2d(
            top_pts[:, :2], min_extent_m=res, axis_snap_rad=math.radians(cfg.axis_snap_max_deg)
        )
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
    cameras: Mapping[str, CameraPose] | None = None,
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
            cx, cy, lx, ly, yaw_rad, corners_xy = _fit_obb_2d(
                comp_pts[:, :2], min_extent_m=res, axis_snap_rad=math.radians(cfg.axis_snap_max_deg)
            )
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
                        source="FLOOR_DEPRESSION",
                    )
                )
                opening_idx += 1

    # (b) 바닥(Z≈0) 내부에 완전히 둘러싸인 빈 구멍(interior void) 추출
    occupied = floor_occ | deep_occ | elev_occ
    void_mask = ~occupied
    exterior = _flood_fill_border(void_mask)
    interior_holes = void_mask & ~exterior
    # 가려진 바닥(탁자·의자 아래, 물체 뒤)은 점이 없어 빈 칸처럼 보인다. 켜져 있으면 카메라가 가림 없이 본 칸만 남긴다(S15P31A101-671)
    top_map: np.ndarray | None = None
    if cfg.floor_void_min_visible_cameras > 0 and interior_holes.any():
        top_map, _ = _cell_top_map(pts[pts[:, 2] <= cfg.max_elevated_z_m], xmin, ymin, nx, ny, res)
    if interior_holes.any():
        for comp_cells in _connected_components(interior_holes, eight_connected=False):
            cell_area = len(comp_cells) * (res * res)
            if not (cfg.min_opening_area_m2 <= cell_area <= cfg.max_opening_area_m2):
                continue
            # 둘러싼 바닥 격자가 실제로 사방에 있는지 확인
            if not _is_enclosed_by(comp_cells, floor_occ):
                continue
            if top_map is not None and not _void_seen_by_cameras(comp_cells, top_map, xmin, ymin, res, cameras, cfg):
                continue
            cell_centers = np.array(
                [[xmin + (c + 0.5) * res, ymin + (r + 0.5) * res] for r, c in comp_cells],
                dtype=np.float64,
            )
            _, _, lx, ly, _, corners_xy = _fit_obb_2d(
                cell_centers,
                min_extent_m=res,
                pad_m=res * 0.5,
                axis_snap_rad=math.radians(cfg.axis_snap_max_deg),
            )
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
                    source="FLOOR_VOID",
                    empty_ratio=1.0,
                )
            )
            opening_idx += 1

    return openings, edges


def _cell_top_map(
    pts: np.ndarray, xmin: float, ymin: float, nx: int, ny: int, res: float, pct: float = 0.9
) -> tuple[np.ndarray, np.ndarray]:
    """칸마다 점 높이의 `pct` 백분위(점이 없으면 nan)와 점 수. 한두 점 잡음에 덜 흔들리게 최댓값 대신 상위 백분위를 쓴다."""
    top = np.full(nx * ny, np.nan, dtype=np.float64)
    counts = np.zeros(nx * ny, dtype=np.int64)
    if len(pts) == 0:
        return top.reshape(ny, nx), counts.reshape(ny, nx)
    gx = np.floor((pts[:, 0] - xmin) / res).astype(np.int64)
    gy = np.floor((pts[:, 1] - ymin) / res).astype(np.int64)
    inb = (gx >= 0) & (gx < nx) & (gy >= 0) & (gy < ny)
    idx = gy[inb] * nx + gx[inb]
    z = pts[inb, 2]
    if len(idx) == 0:
        return top.reshape(ny, nx), counts.reshape(ny, nx)
    order = np.lexsort((z, idx))
    idx_s, z_s = idx[order], z[order]
    counts = np.bincount(idx_s, minlength=nx * ny).astype(np.int64)
    starts = np.concatenate([[0], np.cumsum(counts)[:-1]])
    has = counts > 0
    top[has] = z_s[starts[has] + np.floor(pct * (counts[has] - 1)).astype(np.int64)]
    return top.reshape(ny, nx), counts.reshape(ny, nx)


def _camera_center(pose: CameraPose) -> np.ndarray:
    r = np.asarray(pose.R, dtype=np.float64).reshape(3, 3)
    return -r.T @ np.asarray(pose.t, dtype=np.float64).reshape(3)


def _visible_camera_count(
    point_xyz: np.ndarray,
    top_map: np.ndarray,
    xmin: float,
    ymin: float,
    res: float,
    cameras: Mapping[str, CameraPose],
    tol_m: float = 0.05,
) -> int:
    """World 점 하나를 화면 안에 담고, 그 점과 카메라 사이를 높이 지도(칸 상판)가 막지 않는 카메라 수."""
    ny, nx = top_map.shape
    count = 0
    for pose in cameras.values():
        uv, ok = project_world_points(point_xyz.reshape(1, 3), pose)
        w, h = pose.image_size
        if not (ok[0] and 0 <= uv[0, 0] < w and 0 <= uv[0, 1] < h):
            continue
        seg = _camera_center(pose) - point_xyz
        steps = max(2, int(np.ceil(np.linalg.norm(seg[:2]) / (res * 0.5))))
        ts = np.linspace(0.0, 1.0, steps + 1)[1:]
        samples = point_xyz[None, :] + ts[:, None] * seg[None, :]
        gx = np.floor((samples[:, 0] - xmin) / res).astype(np.int64)
        gy = np.floor((samples[:, 1] - ymin) / res).astype(np.int64)
        inb = (gx >= 0) & (gx < nx) & (gy >= 0) & (gy < ny)
        tops = np.full(len(samples), np.nan)
        tops[inb] = top_map[gy[inb], gx[inb]]
        if not (np.isfinite(tops) & (tops > samples[:, 2] + tol_m)).any():
            count += 1
    return count


def _void_seen_by_cameras(
    comp_cells: Sequence[tuple[int, int]],
    top_map: np.ndarray,
    xmin: float,
    ymin: float,
    res: float,
    cameras: Mapping[str, CameraPose] | None,
    cfg: ZoneGeometryConfig,
) -> bool:
    """빈 칸 덩어리의 칸 절반 이상을 `floor_void_min_visible_cameras` 대 이상이 가림 없이 보면 True. 카메라가 없으면 모른다 → False."""
    if not cameras:
        return False
    seen = 0
    for r, c in comp_cells:
        p = np.array([xmin + (c + 0.5) * res, ymin + (r + 0.5) * res, 0.02], dtype=np.float64)
        if _visible_camera_count(p, top_map, xmin, ymin, res, cameras) >= cfg.floor_void_min_visible_cameras:
            seen += 1
    return seen * 2 >= len(comp_cells)


def _nearest_platform_east(plat: np.ndarray, top: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """칸마다 +열 방향으로 가장 가까운 상판 칸까지 칸 수(없으면 inf)와 그 칸 상판 높이."""
    ny, nx = plat.shape
    dist = np.full((ny, nx), np.inf)
    height = np.full((ny, nx), np.nan)
    for c in range(nx - 2, -1, -1):
        nxt = plat[:, c + 1]
        dist[:, c] = np.where(nxt, 1.0, dist[:, c + 1] + 1.0)
        height[:, c] = np.where(nxt, top[:, c + 1], height[:, c + 1])
    return dist, height


def _nearest_platform_4(plat: np.ndarray, top: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
    """동(+열)·서(-열)·북(+행)·남(-행) 네 방향 가장 가까운 상판(칸 수·높이)."""
    east = _nearest_platform_east(plat, top)
    fw = _nearest_platform_east(np.flip(plat, axis=1), np.flip(top, axis=1))
    west = (np.flip(fw[0], axis=1), np.flip(fw[1], axis=1))
    tn = _nearest_platform_east(plat.T, top.T)
    north = (tn[0].T, tn[1].T)
    ts = _nearest_platform_east(np.flip(plat, axis=0).T, np.flip(top, axis=0).T)
    south = (np.flip(ts[0].T, axis=0), np.flip(ts[1].T, axis=0))
    return [east, west, north, south]


def _extract_platform_openings(pts: np.ndarray, cfg: ZoneGeometryConfig) -> list[GeometryCandidate]:
    """단상 상판 칸이 마주 보는 두 방향(±X 또는 ±Y, U자면 셋 이상)을 막은 안쪽의 낮은·빈 칸 덩어리를 `OPENING` 으로 낸다.

    - 상판 칸: 칸 높이(상위 90 백분위)가 `min_elevated_z_m`~`platform_max_top_z_m` 이고, 8-이웃 덩어리가 `platform_min_area_m2`·
      `platform_min_width_m` 이상이며 칸 높이 60% 이상이 중앙값 ±`elevated_plane_z_band_m` 안(평평한 상판)인 칸.
    - 안쪽 칸: 상판이 아니고 키 큰 칸(벽·기둥)도 아니며, 마주 보는 두 상판 사이 폭이 `platform_opening_max_width_m` 이하이고,
      점이 없거나 둘러싼 상판보다 `platform_opening_min_drop_m` 이상 낮은 칸. 방석처럼 상판과 비슷한 높이는 빠진다.
    - 덩어리 면적이 `min_opening_area_m2`~`platform_opening_max_area_m2` 인 것만. 관측 점군 칸에서만 만들고 정해 둔 도형은 쓰지 않는다.
    """
    res = cfg.grid_res_m
    sel = (pts[:, 2] >= -1.0) & (pts[:, 2] <= cfg.platform_max_top_z_m + 0.30)
    p = pts[sel]
    if len(p) < cfg.min_surface_points:
        return []
    xmin = float(p[:, 0].min()) - res
    ymin = float(p[:, 1].min()) - res
    nx = int(np.floor((float(p[:, 0].max()) - xmin) / res)) + 2
    ny = int(np.floor((float(p[:, 1].max()) - ymin) / res)) + 2
    if nx < 3 or ny < 3 or nx * ny > 250_000:
        return []
    top, counts = _cell_top_map(p, xmin, ymin, nx, ny, res)
    finite = np.isfinite(top)
    top_safe = np.where(finite, top, -np.inf)
    is_top = finite & (top_safe >= cfg.min_elevated_z_m) & (top_safe <= cfg.platform_max_top_z_m)
    is_tall = finite & (top_safe > cfg.platform_max_top_z_m)

    plat = np.zeros((ny, nx), dtype=bool)
    for comp in _connected_components(is_top, eight_connected=True):
        if len(comp) * res * res < cfg.platform_min_area_m2:
            continue
        rows = np.array([rc[0] for rc in comp])
        cols = np.array([rc[1] for rc in comp])
        centers = np.column_stack([xmin + (cols + 0.5) * res, ymin + (rows + 0.5) * res])
        _, _, lx, ly, _, _ = _fit_obb_2d(centers, min_extent_m=res, pad_m=res * 0.5)
        if min(lx, ly) < cfg.platform_min_width_m:
            continue
        heights = top[rows, cols]
        if float(np.mean(np.abs(heights - np.median(heights)) <= cfg.elevated_plane_z_band_m)) < 0.6:
            continue
        plat[rows, cols] = True
    if not plat.any():
        return []

    (d_e, h_e), (d_w, h_w), (d_n, h_n), (d_s, h_s) = _nearest_platform_4(plat, top)
    max_cells = cfg.platform_opening_max_width_m / res
    pair_x = np.isfinite(d_e) & np.isfinite(d_w) & (d_e + d_w - 1.0 <= max_cells)
    pair_y = np.isfinite(d_n) & np.isfinite(d_s) & (d_n + d_s - 1.0 <= max_cells)
    surround = np.full((ny, nx), np.inf)
    surround = np.where(pair_x, np.minimum(surround, np.fmin(h_e, h_w)), surround)
    surround = np.where(pair_y, np.minimum(surround, np.fmin(h_n, h_s)), surround)
    n_dirs = sum((np.isfinite(d) & (d <= max_cells)).astype(np.int64) for d in (d_e, d_w, d_n, d_s))
    empty = counts == 0
    low = empty | (finite & (top_safe <= surround - cfg.platform_opening_min_drop_m))
    valid = (pair_x | pair_y) & low & ~plat & ~is_tall

    results: list[GeometryCandidate] = []
    idx = 1
    for comp in _connected_components(valid, eight_connected=False):
        area = len(comp) * res * res
        if not (cfg.min_opening_area_m2 <= area <= cfg.platform_opening_max_area_m2):
            continue
        rows = np.array([rc[0] for rc in comp])
        cols = np.array([rc[1] for rc in comp])
        centers = np.column_stack([xmin + (cols + 0.5) * res, ymin + (rows + 0.5) * res])
        _, _, lx, ly, _, corners_xy = _fit_obb_2d(
            centers, min_extent_m=res, pad_m=res * 0.5, axis_snap_rad=math.radians(cfg.axis_snap_max_deg)
        )
        surround_top = float(np.median(surround[rows, cols]))
        observed = ~empty[rows, cols]
        obs_tops = top[rows, cols][observed]
        drop = None if len(obs_tops) == 0 else round(surround_top - float(np.median(obs_tops)), 4)
        z_min = min(0.0, float(obs_tops.min())) if len(obs_tops) else 0.0
        results.append(
            GeometryCandidate(
                candidate_id=f"cand-platform-opening-{idx:02d}",
                candidate_kind="OPENING",
                polygon_xy=corners_xy,
                z_min_m=round(z_min, 4),
                z_max_m=round(surround_top + 0.2, 4),
                top_height_m=None,
                obb=None,
                area_m2=round(lx * ly, 4),
                point_count=int(counts[rows, cols].sum()),
                source="PLATFORM_GAP",
                enclosed_directions=int(np.max(n_dirs[rows, cols])),
                surround_top_m=round(surround_top, 4),
                drop_m=drop,
                empty_ratio=round(float(np.mean(~observed)), 4),
            )
        )
        idx += 1
    return results


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


def _axis_offset_rad(yaw_rad: float) -> float:
    """(-π/2, π/2] 의 yaw 가 가장 가까운 World 축 방향(0 또는 ±π/2)에서 틀어진 각(rad)."""
    a = abs(yaw_rad)
    return min(a, abs(math.pi / 2 - a))


def _fit_obb_2d(
    xy: np.ndarray,
    *,
    min_extent_m: float,
    pad_m: float = 0.0,
    axis_snap_rad: float = 0.0,
) -> tuple[float, float, float, float, float, tuple[tuple[float, float], ...]]:
    """2D 점들의 주축(PCA) 유향 경계 상자(중심, 가로·세로 길이, yaw_rad, 반시계 4꼭짓점).

    주축이 World 축(0·±π/2)에서 `axis_snap_rad` 보다 덜 틀어졌으면 yaw=0 으로 두고 World 축 기준 경계 상자를 잰다
    (점을 모두 덮으므로 회전 상자보다 조금 크다).
    """
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
            if _axis_offset_rad(yaw_rad) < axis_snap_rad:
                yaw_rad = 0.0
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
    """VLM 비가용·실패 시 결정적 기하 후보를 'AI 의견 없음' 초안으로 변환한다.

    이름은 종류 이름(구분자 없음)이고, `assess_geometry_candidates` 가 끝에 `unique_draft_names` 로 구분자를 붙인다.
    `index` 는 예전 호출과 맞추려고 남긴 인자다.
    """
    if candidate.candidate_kind == "ELEVATED_WORK_SURFACE":
        zone_kind = "WORK_SURFACE"
        eval_type: EvalType = "VOLUME_3D"
        severity: DraftSeverity = "HIGH"
        name = DEFAULT_NAME_BY_KIND["ELEVATED_WORK_SURFACE"]
    elif candidate.candidate_kind == "OPENING":
        zone_kind = "OPENING"
        eval_type = "GROUND_FOOTPRINT"
        severity = "CRITICAL"
        name = DEFAULT_NAME_BY_KIND["OPENING"]
    else:
        zone_kind = "FLOOR_EDGE"
        eval_type = "GROUND_FOOTPRINT"
        severity = "CRITICAL"
        name = DEFAULT_NAME_BY_KIND["FLOOR_EDGE"]

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

    drafts = unique_draft_names(drafts)
    if any_vlm_succeeded and model_version:
        return drafts, "SUCCEEDED", model_version
    return drafts, "NO_OPINION", None


def _separator(kind: CandidateKind, n: int) -> str:
    """n(1부터)번째 구분자. 고소 작업면은 A·B·…(26개 넘으면 27·28…), 그 밖은 1·2·…."""
    if kind == "ELEVATED_WORK_SURFACE" and n <= len(_LETTERS):
        return _LETTERS[n - 1]
    return str(n)


def unique_draft_names(drafts: Sequence[ZoneDraftItem]) -> list[ZoneDraftItem]:
    """초안 이름에 구분자를 붙여 한 번의 초안 안에서 겹치지 않게 한다(S15P31A101-651).

    - 'AI 의견 없음' 초안(종류 이름)은 늘 `종류 + 구분자`(고소작업면 A, 개구부 1)다.
    - VLM 이 준 이름은 같은 이름이 둘 이상일 때만 구분자를 붙인다(작업대 상판 A·B). 하나뿐이면 그대로 둔다.
    - 이미 쓴 이름(다른 초안의 이름이나 앞서 붙인 이름)은 건너뛴다. 관제의 활성 구역과 겹치는지는 수락하는 화면이 본다.
    """
    counts: dict[str, int] = {}
    for d in drafts:
        counts[d.recommended_name] = counts.get(d.recommended_name, 0) + 1
    used = {d.recommended_name for d in drafts if not _needs_separator(d, counts)}
    next_n: dict[str, int] = {}
    out: list[ZoneDraftItem] = []
    for d in drafts:
        base = d.recommended_name
        if not _needs_separator(d, counts):
            out.append(d)
            continue
        n = next_n.get(base, 1)
        name = f"{base} {_separator(d.candidate_kind, n)}"
        while name in used:
            n += 1
            name = f"{base} {_separator(d.candidate_kind, n)}"
        next_n[base] = n + 1
        used.add(name)
        out.append(replace(d, recommended_name=name))
    return out


def _needs_separator(draft: ZoneDraftItem, counts: Mapping[str, int]) -> bool:
    return draft.vlm_status == "NO_OPINION" or counts[draft.recommended_name] > 1


# ------------------------------------------------------------------------------------------------------------------
# S15P31A101-671 시연 개구부: 후보 종류 제한·줄무늬 근거·개구부 확인 프롬프트
# ------------------------------------------------------------------------------------------------------------------

OPENING_PROMPT_VERSION = "vlm-zone-draft-opening-1.1"
PLATFORM_OPENING_NAME = "단상 개구부"
_RATIONALE_MAX = 500

OPENING_SYSTEM_PROMPT = (
    "당신은 산업·교실 안전 관제 플랫폼 simai 의 위험구역 초안 확인 보조 모델이다.\n"
    "입력으로 World 좌표계(단위 m, 오른손, +Z 상방, 바닥 Z=0)의 개구부 후보 수치(둘러싼 단상 상판 높이, 상판보다 낮은 정도, "
    "빈 칸 비율, 카메라별 노랑·검정 줄무늬 화면 비율)와 얼굴이 비식별 처리된 현장 카메라 크롭이 주어진다.\n"
    "질문: 이 자리가 노란·검은 줄무늬 난간(문틀형 가설 난간), 단상·바닥 둘레의 노란·검은 줄무늬 테이프, 안내판으로 표시된 "
    "바닥 또는 단상 개구부(사람이 빠지거나 떨어질 수 있는 낮거나 빈 자리)인가?\n"
    "규칙:\n"
    "1. 이 자리 둘레에 줄무늬 난간·줄무늬 테이프·안내판 중 하나 이상이 실제로 보이고 그 안쪽이 주변 단상·바닥보다 낮거나 비어 있으면 "
    "is_marked_opening=true 다. 표시가 보이지 않거나 평범한 바닥·탁자·의자·방석 자리면 false 로 답해 이 후보를 버리게 한다.\n"
    "2. markings_seen 에는 화면에서 실제로 본 표시만 적는다(RAILING: 줄무늬 난간, TAPE: 줄무늬 테이프, SIGN: 안내판). 보지 못한 것은 적지 않는다.\n"
    "3. true 이면 recommended_name 은 짧은 한국어 이름(예: 단상 개구부), recommended_severity 는 CRITICAL, "
    "rationale 은 보이는 표시와 위치 근거를 1~2 문장 한국어로 적는다. false 여도 형식을 지켜 이유를 적는다.\n"
    "4. 사람 이름·신원 추측은 절대 적지 않는다. JSON 으로만 답한다."
)

OPENING_RESPONSE_FORMAT: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vlm_zone_draft_opening_check",
        "strict": True,
        "schema": {
            "type": "object",
            "required": [
                "is_marked_opening",
                "markings_seen",
                "recommended_name",
                "recommended_severity",
                "rationale",
                "confidence",
            ],
            "properties": {
                "is_marked_opening": {"type": "boolean"},
                "markings_seen": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["RAILING", "TAPE", "SIGN"]},
                    "maxItems": 3,
                },
                "recommended_name": {"type": "string", "minLength": 1, "maxLength": 160},
                "recommended_severity": {"type": "string", "enum": ["CRITICAL", "HIGH"]},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 400},
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

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


def _env_bool(env: Mapping[str, str], key: str, default: bool) -> bool:
    raw = (env.get(key) or "").strip().lower()
    if raw in _TRUE_WORDS:
        return True
    if raw in _FALSE_WORDS:
        return False
    return default


def _env_number(env: Mapping[str, str], key: str, default: float, cast: type) -> Any:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        val = cast(raw)
    except ValueError:
        return default
    return val if val >= 0 else default


@dataclass(frozen=True)
class ZoneDraftOptions:
    """AI 구역 초안 동작 스위치(S15P31A101-671). 근거는 docs/design/S15P31A101-671-zone-draft-demo-opening.md.

    `opening_only`(환경 변수 `SIMAI_ZONE_DRAFT_OPENING_ONLY`, 기본 켬): 시연 개구부 하나를 확실히 잡는 모드.
    끄면(`0`·`false`·`no`·`off`) 기하 후보·프롬프트·결과가 이전(-455·-651) 그대로다.
    """

    opening_only: bool = True
    # 개구부 초안 수 상한(`SIMAI_ZONE_DRAFT_MAX_OPENINGS`). 시연 현장은 개구부 하나라 1
    max_openings: int = 1
    # 개구부 확인 VLM 호출 상한(순위 순서). 거절된 후보 다음 순위에 기회를 주되 세팅 시간을 늘리지 않는다
    max_vlm_calls: int = 4
    stripe: StripeConfig = field(default_factory=StripeConfig)
    # 줄무늬 근거 상자: 후보 바닥 다각형을 둘레로 이만큼(m) 넓히고(테이프가 단상 가장자리를 두른다), 높이는 후보 위로 이만큼(m) 올린다(난간·안내판)
    stripe_ring_margin_m: float = 0.30
    stripe_marker_height_m: float = 1.0
    # 두 번째 카메라 상자 안 줄무늬 픽셀 비율이 이 값이면 줄무늬 점수 1. 14차 로비 사진에서 개구부 자리가 0.023~0.028(설계 메모)
    stripe_full_ratio: float = 0.025
    # 이유 문구·프롬프트에 카메라 줄무늬 비율을 적는 하한
    stripe_report_ratio: float = 0.005
    # 순위 점수 = stripe_weight × 줄무늬 점수 + (1 − stripe_weight) × 기하 점수. 줄무늬가 없어도 기하 후보는 남는다(점수만 낮다)
    stripe_weight: float = 0.6
    platform_opening_min_drop_m: float = 0.15
    floor_void_min_visible_cameras: int = 2

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ZoneDraftOptions:
        base = cls()
        return replace(
            base,
            opening_only=_env_bool(env, "SIMAI_ZONE_DRAFT_OPENING_ONLY", base.opening_only),
            max_openings=max(1, _env_number(env, "SIMAI_ZONE_DRAFT_MAX_OPENINGS", base.max_openings, int)),
            platform_opening_min_drop_m=_env_number(
                env, "SIMAI_ZONE_DRAFT_PLATFORM_DROP_M", base.platform_opening_min_drop_m, float
            ),
            floor_void_min_visible_cameras=_env_number(
                env, "SIMAI_ZONE_DRAFT_FLOOR_VOID_MIN_CAMERAS", base.floor_void_min_visible_cameras, int
            ),
        )

    @property
    def prompt_version(self) -> str:
        return OPENING_PROMPT_VERSION if self.opening_only else PROMPT_VERSION

    def geometry_config(self, base: ZoneGeometryConfig | None = None) -> ZoneGeometryConfig:
        """스위치가 켜져 있으면 개구부만·단상 사이 후보·가림 거르기·겹침 합치기를 켠 설정, 꺼져 있으면 `base` 그대로."""
        cfg = base or ZoneGeometryConfig()
        if not self.opening_only:
            return cfg
        return replace(
            cfg,
            allowed_kinds=("OPENING",),
            platform_openings=True,
            platform_opening_min_drop_m=self.platform_opening_min_drop_m,
            floor_void_min_visible_cameras=self.floor_void_min_visible_cameras,
            # 같은 자리를 두 갈래(단상 사이·바닥 빈 칸)로 잡으면 겹침이 크다. 0.3 은 절반 크기 상자가 안에 든 경우까지 합치는 값
            dedup_iou=0.3,
            dedup_center_m=0.5,
        )


@dataclass(frozen=True)
class CameraStripeEvidence:
    """카메라 하나에서 후보 상자 안·둘레의 줄무늬 픽셀 비율(0~1)."""

    camera_id: str
    inside_ratio: float
    ring_ratio: float
    region_ratio: float
    region_px: int


@dataclass(frozen=True)
class StripeEvidence:
    """후보 하나의 줄무늬 화면 근거. 카메라가 없거나 아무 데서도 안 보이면 빈 근거(점수 0)."""

    cameras: tuple[CameraStripeEvidence, ...] = ()

    @property
    def best_ratio(self) -> float:
        return max((c.region_ratio for c in self.cameras), default=0.0)

    @property
    def consensus_ratio(self) -> float:
        """두 번째로 큰 카메라 비율(한 대만 보면 그 절반). 키 큰 상자가 먼 카메라에서 단상 쪽 화면에 겹쳐 한 대만 높게 나오는
        자리를 누른다(14차 사진: 개구부 자리 0.023~0.028, 단상과 먼 자리 0.001~0.005, 단상 옆 바닥 0.017)."""
        ratios = sorted((c.region_ratio for c in self.cameras), reverse=True)
        if len(ratios) >= 2:
            return ratios[1]
        return ratios[0] * 0.5 if ratios else 0.0

    def score(self, full_ratio: float) -> float:
        if full_ratio <= 0.0:
            return 0.0
        return min(1.0, self.consensus_ratio / full_ratio)

    def reported(self, min_ratio: float) -> list[CameraStripeEvidence]:
        return sorted(
            (c for c in self.cameras if c.region_ratio >= min_ratio), key=lambda c: (-c.region_ratio, c.camera_id)
        )


def _candidate_prism(candidate: GeometryCandidate, margin_m: float, marker_height_m: float) -> np.ndarray:
    """후보 바닥 다각형(둘레로 `margin_m` 넓힘)을 바닥~(후보 위 + 표시 높이)까지 세운 기둥의 꼭짓점 (N, 3)."""
    poly = np.asarray(candidate.polygon_xy, dtype=np.float64).reshape(-1, 2)
    if margin_m > 0.0:
        offsets = np.array([[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]]) * margin_m
        poly = (poly[:, None, :] + offsets[None, :, :]).reshape(-1, 2)
    z_lo = 0.0
    z_hi = max(float(candidate.z_max_m), float(candidate.top_height_m or 0.0), 0.2) + marker_height_m
    return np.vstack([np.column_stack([poly, np.full(len(poly), z)]) for z in (z_lo, z_hi)])


def _projected_hull(points_xyz: np.ndarray, pose: CameraPose) -> np.ndarray | None:
    uv, ok = project_world_points_distorted(points_xyz, pose)
    if not bool(np.all(ok)):
        return None  # 꼭짓점 하나라도 카메라 뒤·왜곡 범위 밖이면 상자 모양을 믿을 수 없다
    hull = convex_hull(uv)
    return hull if len(hull) >= 3 else None


def candidate_stripe_evidence(
    candidate: GeometryCandidate,
    stripe_masks: Mapping[str, np.ndarray],
    cameras_by_id: Mapping[str, CameraPose],
    options: ZoneDraftOptions | None = None,
    *,
    min_region_px: int = 64,
) -> StripeEvidence:
    """후보 상자를 각 카메라 프레임에 (왜곡 포함) 투영해 안·둘레의 노랑·검정 줄무늬 비율을 잰다.

    `stripe_masks` 는 얼굴 처리된 프레임에서 만든 `hazard_stripe_mask` 결과다. 화면에 거의 안 보이는 카메라는 뺀다.
    """
    opts = options or ZoneDraftOptions()
    inner_pts = _candidate_prism(candidate, 0.0, opts.stripe_marker_height_m)
    outer_pts = _candidate_prism(candidate, opts.stripe_ring_margin_m, opts.stripe_marker_height_m)
    out: list[CameraStripeEvidence] = []
    for cam_id in sorted(stripe_masks):
        pose = cameras_by_id.get(cam_id)
        mask = stripe_masks[cam_id]
        if pose is None or mask.ndim != 2:
            continue
        inner_h = _projected_hull(inner_pts, pose)
        outer_h = _projected_hull(outer_pts, pose)
        if inner_h is None or outer_h is None:
            continue
        inner = convex_polygon_mask(inner_h, mask.shape)
        outer = convex_polygon_mask(outer_h, mask.shape) | inner
        ring = outer & ~inner
        region_px = int(np.count_nonzero(outer))
        if region_px < min_region_px:
            continue
        inner_px = int(np.count_nonzero(inner))
        ring_px = int(np.count_nonzero(ring))
        out.append(
            CameraStripeEvidence(
                camera_id=cam_id,
                inside_ratio=float(np.count_nonzero(mask & inner)) / inner_px if inner_px else 0.0,
                ring_ratio=float(np.count_nonzero(mask & ring)) / ring_px if ring_px else 0.0,
                region_ratio=float(np.count_nonzero(mask & outer)) / region_px,
                region_px=region_px,
            )
        )
    return StripeEvidence(tuple(out))


def _geometry_score(candidate: GeometryCandidate) -> float:
    """기하 근거 점수(0~1). 단상 사이 자리는 막힌 방향이 많고 넓을수록, 관측 점이 바닥 아래인 함몰은 중간, 빈 칸은 낮게."""
    if candidate.source == "PLATFORM_GAP":
        dirs = candidate.enclosed_directions or 2
        area = candidate.area_m2 or 0.0
        return 0.5 + 0.25 * min(1.0, max(0, dirs - 2) / 2.0) + 0.25 * min(1.0, area / 1.0)
    if candidate.source == "FLOOR_DEPRESSION":
        return 0.6
    if candidate.source == "FLOOR_VOID":
        return 0.3
    return 0.4


def _evidence_text(candidate: GeometryCandidate, stripes: StripeEvidence, opts: ZoneDraftOptions) -> str:
    parts: list[str] = []
    if candidate.source == "PLATFORM_GAP":
        geo = f"단상 상판이 {candidate.enclosed_directions or 2}방향에서 둘러쌈"
        if candidate.drop_m is not None:
            geo += f", 상판보다 {candidate.drop_m:.2f} m 낮음"
        if candidate.empty_ratio:
            geo += f", 빈 칸 {candidate.empty_ratio * 100:.0f}%"
        parts.append(geo)
    elif candidate.source == "FLOOR_DEPRESSION":
        parts.append("바닥보다 낮은 점 관측")
    elif candidate.source == "FLOOR_VOID":
        parts.append("카메라가 가림 없이 본 바닥 안 빈 칸")
    reported = stripes.reported(opts.stripe_report_ratio)
    if reported:
        parts.append("노랑·검정 줄무늬 " + "·".join(f"{c.camera_id} {c.region_ratio * 100:.1f}%" for c in reported[:4]))
    else:
        parts.append("노랑·검정 줄무늬 근거 없음")
    return "근거: " + "; ".join(parts)


def _join_rationale(head: str, evidence: str) -> str:
    """`head (evidence)` 를 500자 안으로. 넘치면 head 를 줄인다."""
    tail = f" ({evidence})"
    room = _RATIONALE_MAX - len(tail)
    if room < 10:
        return head[:_RATIONALE_MAX]
    return (head if len(head) <= room else head[: room - 1] + "…") + tail


def default_opening_draft(
    candidate: GeometryCandidate,
    *,
    evidence_cameras: Sequence[str],
    evidence_text: str,
) -> ZoneDraftItem:
    """VLM 의견 없이 내는 개구부 초안('AI 의견 없음' + 기하·줄무늬 근거). 단상 사이 자리는 이름이 '단상 개구부'다."""
    base = default_draft_for_candidate(candidate, 0, evidence_cameras=evidence_cameras)
    name = PLATFORM_OPENING_NAME if candidate.source == "PLATFORM_GAP" else base.recommended_name
    return replace(base, recommended_name=name, rationale=_join_rationale(NO_OPINION_TEXT, evidence_text))


def select_opening_crops(
    candidate: GeometryCandidate,
    frames_by_camera: Mapping[str, VlmFrame],
    cameras_by_id: Mapping[str, CameraPose],
    stripes: StripeEvidence,
    options: ZoneDraftOptions | None = None,
    *,
    max_views: int = 2,
    pad_ratio: float = 0.15,
    min_crop_px: int = 96,
) -> tuple[list[VlmFrame], list[str]]:
    """후보 기둥(바닥~난간 높이, 둘레 포함)이 보이는 카메라에서 크롭한다. 줄무늬 비율이 큰 카메라부터 고른다.

    보정·투영이 안 되면 기존 `select_candidate_crops`(바닥 평면 투영, 그것도 안 되면 전체 프레임)를 쓴다.
    """
    opts = options or ZoneDraftOptions()
    pts = _candidate_prism(candidate, opts.stripe_ring_margin_m, opts.stripe_marker_height_m)
    ratio_by_cam = {c.camera_id: c.region_ratio for c in stripes.cameras}
    scored: list[tuple[float, float, str, VlmFrame]] = []
    for cam_id, frame in frames_by_camera.items():
        pose = cameras_by_id.get(cam_id)
        if pose is None:
            continue
        hull = _projected_hull(pts, pose)
        if hull is None:
            continue
        h, w = frame.pixels.shape[:2]
        umin, vmin = hull.min(axis=0)
        umax, vmax = hull.max(axis=0)
        bw = max(float(umax - umin), float(min_crop_px))
        bh = max(float(vmax - vmin), float(min_crop_px))
        cx, cy = 0.5 * (umin + umax), 0.5 * (vmin + vmax)
        x1 = max(0, math.floor(cx - 0.5 * bw * (1 + 2 * pad_ratio)))
        y1 = max(0, math.floor(cy - 0.5 * bh * (1 + 2 * pad_ratio)))
        x2 = min(w, math.ceil(cx + 0.5 * bw * (1 + 2 * pad_ratio)))
        y2 = min(h, math.ceil(cy + 0.5 * bh * (1 + 2 * pad_ratio)))
        if x2 - x1 < 16 or y2 - y1 < 16:
            continue
        visible_area = float((x2 - x1) * (y2 - y1))
        scored.append((ratio_by_cam.get(cam_id, 0.0), visible_area, cam_id, frame.with_crop(x1, y1, x2, y2)))
    if not scored:
        return select_candidate_crops(candidate, frames_by_camera, cameras_by_id, max_views=max_views)
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in scored[:max_views]], sorted(item[2] for item in scored)


def build_opening_user_prompt(
    candidate: GeometryCandidate,
    evidence_cameras: Sequence[str],
    stripes: StripeEvidence,
) -> str:
    """개구부 확인 질문(수치 근거 포함, 사람 정보 없음)."""
    poly_str = ", ".join(f"({x:.2f}, {y:.2f})" for x, y in candidate.polygon_xy[:8])
    source_text = {
        "PLATFORM_GAP": "단상 상판들 사이의 낮거나 빈 자리",
        "FLOOR_DEPRESSION": "바닥보다 낮은 함몰",
        "FLOOR_VOID": "바닥 안 빈 칸",
    }.get(candidate.source or "", "기하 후보")
    lines = [
        f"개구부 후보 ID: {candidate.candidate_id}",
        f"후보 출처: {source_text}",
        f"바닥 다각형 꼭짓점 World XY(m): [{poly_str}]",
    ]
    if candidate.area_m2 is not None:
        lines.append(f"추정 면적: {candidate.area_m2:.2f} m^2")
    if candidate.surround_top_m is not None:
        lines.append(
            f"둘러싼 단상 상판 높이: {candidate.surround_top_m:.2f} m, 막힌 방향 수: {candidate.enclosed_directions or 2}"
        )
    if candidate.drop_m is not None:
        lines.append(f"상판보다 낮은 정도: {candidate.drop_m:.2f} m")
    if candidate.empty_ratio is not None:
        lines.append(f"점이 없는 칸 비율: {candidate.empty_ratio * 100:.0f}%")
    if stripes.cameras:
        lines.append(
            "카메라별 노랑·검정 줄무늬 화면 비율(후보 안 / 둘레): "
            + ", ".join(f"{c.camera_id} {c.inside_ratio * 100:.1f}% / {c.ring_ratio * 100:.1f}%" for c in stripes.cameras)
        )
    else:
        lines.append("카메라별 노랑·검정 줄무늬 화면 비율: 측정 못 함(근거 없음)")
    if evidence_cameras:
        lines.append(f"첨부 크롭 카메라 후보: {', '.join(evidence_cameras)}")
    lines.append(
        "질문: 이 자리가 노란·검은 줄무늬 난간·테이프·안내판으로 표시된 바닥 또는 단상 개구부인가? "
        "is_marked_opening, markings_seen, recommended_name, recommended_severity, rationale(1~2문장), confidence 를 JSON 으로 답하라."
    )
    return "\n".join(lines)


def parse_opening_vlm_response(
    raw_content: str,
    candidate: GeometryCandidate,
    *,
    evidence_cameras: Sequence[str] = (),
    evidence_text: str = "",
) -> ZoneDraftItem | None:
    """개구부 확인 응답을 검사한다. 표시된 개구부가 아니라고 하면 None(후보를 버린다), 형식이 어긋나면 `ValueError`·`TypeError`.

    등급은 CRITICAL 로 둔다: 개구부 기본 등급이 CRITICAL 이고 VLM 은 경보를 낮추지 못한다(AGENTS 규칙 4).
    """
    try:
        doc = json.loads(raw_content)
    except ValueError as e:
        raise ValueError(f"VLM 응답이 JSON 이 아니다: {e}") from e
    if not isinstance(doc, Mapping):
        raise TypeError("VLM 응답이 JSON 객체가 아니다")
    verdict = doc.get("is_marked_opening")
    if not isinstance(verdict, bool):
        raise TypeError(f"유효하지 않은 is_marked_opening: {verdict!r}")
    markings = doc.get("markings_seen", [])
    if not isinstance(markings, list) or any(m not in ("RAILING", "TAPE", "SIGN") for m in markings):
        raise ValueError(f"유효하지 않은 markings_seen: {markings!r}")
    if not verdict:
        return None
    name = doc.get("recommended_name")
    severity = doc.get("recommended_severity")
    rationale = doc.get("rationale")
    confidence = doc.get("confidence")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 160:
        raise ValueError(f"유효하지 않은 recommended_name: {name!r}")
    if severity not in ("CRITICAL", "HIGH"):
        raise ValueError(f"recommended_severity 는 CRITICAL 또는 HIGH 여야 한다: {severity!r}")
    if not isinstance(rationale, str) or not rationale.strip():
        raise ValueError(f"유효하지 않은 rationale: {rationale!r}")
    conf_val: float | None = None
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, int | float) or not 0.0 <= float(confidence) <= 1.0:
            raise ValueError(f"유효하지 않은 confidence: {confidence!r}")
        conf_val = float(confidence)
    head = rationale.strip()
    if markings:
        seen = {"RAILING": "줄무늬 난간", "TAPE": "줄무늬 테이프", "SIGN": "안내판"}
        head += " [본 표시: " + "·".join(seen[m] for m in dict.fromkeys(markings)) + "]"
    return ZoneDraftItem(
        candidate_id=candidate.candidate_id,
        candidate_kind="OPENING",
        zone_kind="OPENING",
        eval_type="GROUND_FOOTPRINT",
        polygon_xy=candidate.polygon_xy,
        z_min_m=candidate.z_min_m,
        z_max_m=candidate.z_max_m,
        top_height_m=candidate.top_height_m,
        obb=candidate.obb,
        recommended_name=name.strip(),
        recommended_severity="CRITICAL",
        rationale=_join_rationale(head, evidence_text) if evidence_text else head[:_RATIONALE_MAX],
        vlm_status="VLM",
        evidence_cameras=tuple(evidence_cameras),
        confidence=conf_val,
    )


@dataclass(frozen=True)
class RankedOpening:
    """순위를 매긴 개구부 후보와 근거(로그·시험용)."""

    candidate: GeometryCandidate
    score: float
    stripes: StripeEvidence


def rank_opening_candidates(
    candidates: Sequence[GeometryCandidate],
    frames_by_camera: Mapping[str, VlmFrame] | None,
    cameras_by_id: Mapping[str, CameraPose] | None,
    options: ZoneDraftOptions | None = None,
) -> list[RankedOpening]:
    """`OPENING` 후보만 줄무늬·기하 점수로 정렬한다(높은 순). 프레임·보정이 없으면 줄무늬 점수 0(지어내지 않는다)."""
    opts = options or ZoneDraftOptions()
    frames_map = frames_by_camera or {}
    cams_map = cameras_by_id or {}
    masks = {cid: hazard_stripe_mask(f.pixels, opts.stripe) for cid, f in frames_map.items() if cid in cams_map}
    ranked: list[RankedOpening] = []
    for cand in candidates:
        if cand.candidate_kind != "OPENING":
            continue
        stripes = candidate_stripe_evidence(cand, masks, cams_map, opts)
        score = opts.stripe_weight * stripes.score(opts.stripe_full_ratio) + (1.0 - opts.stripe_weight) * _geometry_score(cand)
        ranked.append(RankedOpening(cand, round(score, 4), stripes))
    ranked.sort(key=lambda r: (-r.score, r.candidate.candidate_id))
    return ranked


def assess_opening_candidates(
    candidates: Sequence[GeometryCandidate],
    *,
    vlm_client: Any | None,
    frames_by_camera: Mapping[str, VlmFrame] | None = None,
    cameras_by_id: Mapping[str, CameraPose] | None = None,
    options: ZoneDraftOptions | None = None,
) -> tuple[list[ZoneDraftItem], Literal["SUCCEEDED", "NO_OPINION"], str | None]:
    """시연 개구부 모드의 초안: 순위 순서로 VLM 에 '표시된 개구부인가'를 묻고, 아니라고 하면 버리며 `max_openings` 개까지 낸다.

    - VLM 이 없거나 실패하면 그 후보는 'AI 의견 없음'(기하·줄무늬 근거 문구)으로 남는다(세팅을 막지 않는다).
    - 결과 상태 규칙은 `assess_geometry_candidates` 와 같다.
    """
    opts = options or ZoneDraftOptions()
    frames_map = frames_by_camera or {}
    cams_map = cameras_by_id or {}
    ranked = rank_opening_candidates(candidates, frames_map, cams_map, opts)
    if not ranked:
        model_name = getattr(getattr(vlm_client, "settings", None), "model", None) or getattr(vlm_client, "model", None)
        if vlm_client is not None and isinstance(model_name, str) and model_name:
            return [], "SUCCEEDED", model_name
        return [], "NO_OPINION", None

    drafts: list[ZoneDraftItem] = []
    any_vlm_succeeded = False
    model_version: str | None = None
    calls = 0
    for r in ranked:
        if len(drafts) >= opts.max_openings:
            break
        cand = r.candidate
        evidence = _evidence_text(cand, r.stripes, opts)
        crops, evidence_cams = select_opening_crops(cand, frames_map, cams_map, r.stripes, opts)
        if vlm_client is None or not crops or calls >= opts.max_vlm_calls:
            drafts.append(default_opening_draft(cand, evidence_cameras=evidence_cams, evidence_text=evidence))
            continue
        calls += 1
        try:
            completion = vlm_client.complete(
                build_opening_user_prompt(cand, evidence_cams, r.stripes),
                crops,
                system=OPENING_SYSTEM_PROMPT,
                response_format=OPENING_RESPONSE_FORMAT,
            )
            item = parse_opening_vlm_response(
                completion.content, cand, evidence_cameras=evidence_cams, evidence_text=evidence
            )
        except (VlmError, ValueError, TypeError):
            drafts.append(default_opening_draft(cand, evidence_cameras=evidence_cams, evidence_text=evidence))
            continue
        any_vlm_succeeded = True
        model_version = completion.model or getattr(getattr(vlm_client, "settings", None), "model", None) or "cosmos3-nano"
        if item is not None:
            drafts.append(item)

    drafts = unique_draft_names(drafts)
    if any_vlm_succeeded and model_version:
        return drafts, "SUCCEEDED", model_version
    return drafts, "NO_OPINION", None
