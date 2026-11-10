"""같은 현장 재보정을 이전 채택 보정의 World 에 다시 맞춘다(S15P31A101-584, 설계 docs/design/world-reanchor.md).

세팅마다 World(원점·축)를 새로 정하면 이전에 그린 구역·TBM 배치가 어긋난다. 카메라가 실제로 움직이지 않았으면
새 보정을 이전 World 로 옮겨 기존 구역을 그대로 쓴다.

- 변환은 새 World → 이전 World 의 **Z 축 회전(yaw) + 수평 이동(tx, ty)** 만이다. Z(바닥 Z=0, +Z 상방)와 축척은 새 보정
  그대로다. 새 바닥은 카메라 실측 높이로 기울기를 바로잡은 값(-542)이라 이전 바닥(기울기 보정 전일 수 있음)보다 낫고,
  축척은 새 실측 높이가 정본이다. 추정 축척은 같은 배치인지 확인하는 데만 쓴다(적용하지 않음).
- 추정: 같은 카메라 ID 의 카메라 중심을 바닥에 투영한 2D 점으로 Kabsch yaw·이동(축척 1, 적용값)과 2D Umeyama 축척.
- 채택 조건: 공통 카메라 ≥ min_cameras, 수평 배치 퍼짐 ≥ min_layout_spread_m, 카메라별 배치 잔차(2D 닮음 맞춤 잔차:
  전체 축척 차이를 뺀 "한 대만 움직였는가") ≤ 한도, |축척−1| ≤ 한도, 카메라별 방향 변화(변환 후 이전 R 과의 각) ≤ 한도.
  하나라도 어기면 새 World 를 그대로 두고 사유를 남긴다(카메라가 움직였을 가능성). 실제 적용(축척 1) 뒤 남는 카메라 위치
  차이는 `applied_center_residuals_m` 로 따로 남긴다.
- 운영자가 끄면(`enabled=False`, 워커 SIMAI_SETUP_WORLD_ANCHOR=off) 변환하지 않고 `DISABLED` 로 기록만 남긴다.
- 순수 함수다. 보정 문서를 덮어쓰지 않고 복사본을 돌려준다. 이전 보정이 없으면 기록 없이 그대로 돌려준다(모르면 비운다).
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.calibration.document import content_hash

METHOD = "CAMERA_CENTER_YAW_XY"

ANCHORED = "ANCHORED"
DISABLED = "DISABLED"
PREVIOUS_NOT_USABLE = "PREVIOUS_NOT_USABLE"
SITE_MISMATCH = "SITE_MISMATCH"
INSUFFICIENT_CAMERAS = "INSUFFICIENT_CAMERAS"
DEGENERATE_LAYOUT = "DEGENERATE_LAYOUT"
CENTER_RESIDUAL_EXCEEDED = "CENTER_RESIDUAL_EXCEEDED"
SCALE_OUT_OF_RANGE = "SCALE_OUT_OF_RANGE"
ORIENTATION_CHANGE_EXCEEDED = "ORIENTATION_CHANGE_EXCEEDED"

# 현장 기준이 아니거나 World 가 정해지지 않은 이전 보정에는 맞추지 않는다(규칙 7, 명세 20.4)
_UNUSABLE_SOURCES = frozenset({"DATASET_GT", "SIM"})


@dataclass(frozen=True, slots=True)
class ReanchorConfig:
    """채택 한도. 근거는 docs/design/world-reanchor.md §3."""

    max_center_residual_m: float = 0.15
    max_scale_deviation: float = 0.03
    max_orientation_change_rad: float = math.radians(10.0)
    min_cameras: int = 3
    min_layout_spread_m: float = 0.5

    def limits(self) -> dict[str, float | int]:
        return {
            "max_center_residual_m": self.max_center_residual_m,
            "max_scale_deviation": self.max_scale_deviation,
            "max_orientation_change_rad": round(self.max_orientation_change_rad, 6),
            "min_cameras": self.min_cameras,
        }


DEFAULT_CONFIG = ReanchorConfig()


@dataclass(frozen=True, slots=True)
class ReanchorResult:
    """새 World → 이전 World 변환 추정과 채택 판정. 추정 못 한 값은 None(지어내지 않음)."""

    accepted: bool
    reason: str
    reference_calibration_version: str
    config: ReanchorConfig
    used_cameras: tuple[str, ...] = ()
    yaw_rad: float | None = None
    translation_m: tuple[float, float] | None = None
    scale: float | None = None
    center_residuals_m: Mapping[str, float] = field(default_factory=dict)
    applied_center_residuals_m: Mapping[str, float] = field(default_factory=dict)
    vertical_differences_m: Mapping[str, float] = field(default_factory=dict)
    orientation_changes_rad: Mapping[str, float] = field(default_factory=dict)

    def to_record(self) -> dict[str, Any]:
        """보정 문서 `world_anchor`(계약 calibration 2.5.0)."""
        rec: dict[str, Any] = {
            "accepted": self.accepted,
            "reason": self.reason,
            "reference_calibration_version": self.reference_calibration_version,
            "method": METHOD,
            "limits": self.config.limits(),
            "used_cameras": list(self.used_cameras),
            "yaw_rad": None if self.yaw_rad is None else round(self.yaw_rad, 6),
            "translation_m": None if self.translation_m is None else [round(v, 4) for v in self.translation_m],
            "scale": round(self.scale, 6) if self.scale is not None and self.scale > 0 else None,
            "max_center_residual_m": _max_or_none(self.center_residuals_m, 4),
            "max_orientation_change_rad": _max_or_none(self.orientation_changes_rad, 6),
            "center_residuals_m": {k: round(v, 4) for k, v in self.center_residuals_m.items()},
            "applied_center_residuals_m": {k: round(v, 4) for k, v in self.applied_center_residuals_m.items()},
            "vertical_differences_m": {k: round(v, 4) for k, v in self.vertical_differences_m.items()},
            "orientation_changes_rad": {k: round(v, 6) for k, v in self.orientation_changes_rad.items()},
        }
        return rec

    def yaw_matrix(self) -> np.ndarray:
        c, s = math.cos(self.yaw_rad or 0.0), math.sin(self.yaw_rad or 0.0)
        return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])

    def translation3(self) -> np.ndarray:
        tx, ty = self.translation_m or (0.0, 0.0)
        return np.array([tx, ty, 0.0])


def _max_or_none(values: Mapping[str, float], digits: int) -> float | None:
    return round(max(values.values()), digits) if values else None


def _poses(doc: Mapping[str, Any]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """카메라 ID → (R, 중심 C). 형식이 틀리거나 유한하지 않은 카메라는 뺀다."""
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    cams = doc.get("cameras")
    if not isinstance(cams, Sequence):
        return out
    for cam in cams:
        if not isinstance(cam, Mapping):
            continue
        try:
            R = np.asarray(cam["R"], dtype=np.float64)
            t = np.asarray(cam["t"], dtype=np.float64)
            cid = str(cam["camera_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if R.shape != (3, 3) or t.shape != (3,) or not (np.isfinite(R).all() and np.isfinite(t).all()):
            continue
        out[cid] = (R, -R.T @ t)
    return out


def _rotation_angle(R: np.ndarray) -> float:
    return float(math.acos(max(-1.0, min(1.0, (float(np.trace(R)) - 1.0) / 2.0))))


def _site_id(doc: Mapping[str, Any]) -> str | None:
    frame = doc.get("coordinate_frame")
    sid = frame.get("site_id") if isinstance(frame, Mapping) else None
    return sid if isinstance(sid, str) and sid else None


def estimate_reanchor(
    previous_doc: Mapping[str, Any],
    new_doc: Mapping[str, Any],
    config: ReanchorConfig = DEFAULT_CONFIG,
) -> ReanchorResult:
    """이전 채택 보정과 새 보정(같은 카메라 ID)으로 새 World → 이전 World 의 yaw·수평 이동을 추정하고 채택 여부를 판정한다."""
    ref = str(previous_doc.get("calibration_version") or "")

    def reject(reason: str, **kw: Any) -> ReanchorResult:
        return ReanchorResult(False, reason, ref, config, **kw)

    if previous_doc.get("state") == "UNCALIBRATED" or previous_doc.get("provider_source") in _UNUSABLE_SOURCES:
        return reject(PREVIOUS_NOT_USABLE)
    prev_site, new_site = _site_id(previous_doc), _site_id(new_doc)
    if prev_site is not None and new_site is not None and prev_site != new_site:
        return reject(SITE_MISMATCH)

    prev, new = _poses(previous_doc), _poses(new_doc)
    used = tuple(cid for cid in new if cid in prev)
    if len(used) < max(3, config.min_cameras):
        # 2대면 2D 강체 맞춤이 정확히 정해져 잔차로 검증할 수 없다
        return reject(INSUFFICIENT_CAMERAS, used_cameras=used)

    q = np.array([new[c][1] for c in used])  # 새 World 중심
    p = np.array([prev[c][1] for c in used])  # 이전 World 중심
    qc, pc = q[:, :2] - q[:, :2].mean(0), p[:, :2] - p[:, :2].mean(0)
    spread = float(math.sqrt(np.mean(np.sum(qc * qc, axis=1))))
    if spread < config.min_layout_spread_m:
        return reject(DEGENERATE_LAYOUT, used_cameras=used)

    # 2D Kabsch: argmax Σ p·R(θ)q → θ = atan2(Σ(p_y q_x − p_x q_y), Σ(p_x q_x + p_y q_y)). 축척이 있어도 같은 θ
    dot = float(np.sum(pc * qc))
    cross = float(np.sum(pc[:, 1] * qc[:, 0] - pc[:, 0] * qc[:, 1]))
    yaw = math.atan2(cross, dot)
    c, s = math.cos(yaw), math.sin(yaw)
    R2 = np.array([[c, -s], [s, c]])
    scale = float(math.hypot(dot, cross) / float(np.sum(qc * qc)))  # 2D Umeyama 축척(진단, 적용하지 않음)
    t2 = p[:, :2].mean(0) - R2 @ q[:, :2].mean(0)

    # 배치 잔차(판정): 축척까지 맞춘 닮음 변환 뒤 남는 차이. 적용 잔차(기록): 실제로 쓰는 강체(축척 1) 변환 뒤 차이
    shape_t = p[:, :2].mean(0) - scale * (R2 @ q[:, :2].mean(0))
    shaped = scale * (q[:, :2] @ R2.T) + shape_t
    moved = q[:, :2] @ R2.T + t2
    residuals = {cid: float(np.linalg.norm(shaped[i] - p[i, :2])) for i, cid in enumerate(used)}
    applied = {cid: float(np.linalg.norm(moved[i] - p[i, :2])) for i, cid in enumerate(used)}
    vertical = {cid: float(q[i, 2] - p[i, 2]) for i, cid in enumerate(used)}
    Rz = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    orient = {cid: _rotation_angle(prev[cid][0] @ (new[cid][0] @ Rz.T).T) for cid in used}

    kw: dict[str, Any] = {
        "used_cameras": used, "yaw_rad": yaw, "translation_m": (float(t2[0]), float(t2[1])), "scale": scale,
        "center_residuals_m": residuals, "applied_center_residuals_m": applied,
        "vertical_differences_m": vertical, "orientation_changes_rad": orient,
    }
    if max(residuals.values()) > config.max_center_residual_m:
        return reject(CENTER_RESIDUAL_EXCEEDED, **kw)
    if not math.isfinite(scale) or abs(scale - 1.0) > config.max_scale_deviation:
        return reject(SCALE_OUT_OF_RANGE, **kw)
    if max(orient.values()) > config.max_orientation_change_rad:
        return reject(ORIENTATION_CHANGE_EXCEEDED, **kw)
    return ReanchorResult(True, ANCHORED, ref, config, **kw)


def apply_reanchor(new_doc: Mapping[str, Any], result: ReanchorResult) -> dict[str, Any]:
    """새 보정 문서 복사본에 `world_anchor` 기록을 싣고, 채택이면 모든 카메라 외부 파라미터를 이전 World 로 옮긴다.

    X_prev = Rz·X_new + T (T = [tx, ty, 0]) 이므로 x_cam = R·X_new + t = (R·Rzᵀ)·X_prev + (t − R·Rzᵀ·T).
    카메라 높이(Z)는 변하지 않는다. content_hash 는 다시 계산한다(world_anchor 는 보정 내용에 넣는다).
    """
    doc = copy.deepcopy(dict(new_doc))
    if result.accepted:
        Rz, T = result.yaw_matrix(), result.translation3()
        for cam in doc.get("cameras", []):
            R = np.asarray(cam["R"], dtype=np.float64)
            t = np.asarray(cam["t"], dtype=np.float64)
            R_new = R @ Rz.T
            cam["R"] = R_new.tolist()
            cam["t"] = (t - R_new @ T).tolist()
    doc["world_anchor"] = result.to_record()
    doc["content_hash"] = content_hash(doc)
    return doc


def reanchor_calibration(
    previous_doc: Mapping[str, Any] | None,
    new_doc: Mapping[str, Any],
    config: ReanchorConfig = DEFAULT_CONFIG,
    *,
    enabled: bool = True,
) -> tuple[dict[str, Any], ReanchorResult | None]:
    """이전 채택 보정이 있으면 추정·판정해 기록을 싣고(채택이면 변환), 없으면 새 문서를 그대로(기록 없이) 돌려준다.

    enabled=False 면 추정하지 않고 새 World 그대로 두며 `DISABLED`(accepted=false) 기록만 남긴다.
    """
    if previous_doc is None or not previous_doc.get("calibration_version"):
        return copy.deepcopy(dict(new_doc)), None  # 비교할 이전 채택 보정을 모른다
    if not enabled:
        result = ReanchorResult(False, DISABLED, str(previous_doc["calibration_version"]), config)
        return apply_reanchor(new_doc, result), result
    result = estimate_reanchor(previous_doc, new_doc, config)
    return apply_reanchor(new_doc, result), result
