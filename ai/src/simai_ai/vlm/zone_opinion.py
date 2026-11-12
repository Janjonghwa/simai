"""변경 의심 구역 VLM 의견: 이전·이번 세팅의 같은 카메라 사진 크롭을 Cosmos3-Nano 가 비교해 유지·수정·삭제를 권고한다 (S15P31A101-631).

높이 비교(S15P31A101-630)가 `CHANGE_SUSPECTED` 로 판정한 이어 쓴 구역마다
1. 구역 3D 상자(바닥 다각형 × 높이 범위)를 이전·이번 보정에 각각 투영해 둘 다에서 잘 보이는 카메라 하나를 고르고(`select_opinion_view`),
2. 그 카메라의 이전·이번 프레임에서 구역 주변을 잘라(정규화 크롭 박스, 여백 포함) VLM 에 두 장을 넣어
3. `recommended_action`(KEEP·MODIFY·REMOVE)·`rationale`(한국어 한 줄)·`confidence` JSON 을 받는다(`assess_zone_opinions`).

깨면 안 되는 규칙:
1. 의견은 화면 표시용 제안이다. 구역 목록·확인 필요·관제 구역을 바꾸지 않고 Tier 0 판정에 들어가지 않는다(AGENTS 규칙 1·4).
2. 의견을 못 내면 `NO_OPINION` 과 사유만 적는다. 권고·이유를 지어내지 않는다(AGENTS 규칙 2).
3. VLM 입력 이미지는 얼굴 처리 관문을 거친 `VlmFrame` 의 크롭만 쓴다. 사진은 같은 Pod 루프백 vLLM 밖으로 나가지 않는다(AGENTS 규칙 8).
4. 좌표는 World(m, 오른손, +Z 상방, 바닥 Z=0)다(AGENTS 규칙 3). 투영은 초안과 같은 핀홀(왜곡 계수 생략)이다.

크롭 박스는 프레임 너비·높이로 나눈 정규화 좌표라 해상도와 무관하다. 그래서 카메라 고르기는 보정(`CameraPose.image_size`)만으로 하고,
VLM 에 넣을 때 실제 프레임 크기로 픽셀 영역을 다시 계산한다. 화면도 같은 프레임(CAMERA_FRAME)을 이 박스로 잘라 보인다.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np

from .client import VlmError
from .face_blur import VlmFrame
from .zone_draft import CameraPose, project_world_points

PROMPT_VERSION = "vlm-zone-opinion-1.0"
MAX_RATIONALE_CHARS = 200

OpinionStatus = Literal["OPINION", "NO_OPINION"]
RecommendedAction = Literal["KEEP", "MODIFY", "REMOVE"]
NoOpinionReason = Literal[
    "NO_PREVIOUS_FRAMES",
    "PREVIOUS_FRAMES_UNAVAILABLE",
    "CURRENT_FRAMES_UNAVAILABLE",
    "CALIBRATION_UNAVAILABLE",
    "NOT_VISIBLE",
    "VLM_UNAVAILABLE",
    "VLM_FAILED",
    "TIME_BUDGET",
    "ZONE_LIMIT",
]
_ACTIONS: tuple[RecommendedAction, ...] = ("KEEP", "MODIFY", "REMOVE")

SYSTEM_PROMPT = (
    "당신은 산업·교실 안전 관제 플랫폼 simai 의 위험구역 점검 보조 모델이다.\n"
    "같은 고정 카메라로 찍은 두 사진이 주어진다. 첫 번째는 이전 세팅 때, 두 번째는 이번 세팅 때 같은 위험구역 주변을 잘라낸 사진이다. "
    "얼굴은 비식별 처리되어 있다.\n"
    "역할:\n"
    "1. 두 사진에서 구역 안 물체(단상·작업대·적치물·개구부 덮개 등)가 무엇이 바뀌었는지 비교한다.\n"
    "2. 기존 위험구역을 그대로 둘지(KEEP), 범위·높이를 고칠지(MODIFY), 위험 요인이 사라져 지울지(REMOVE) 권고한다. "
    "확실하지 않으면 KEEP 이나 MODIFY 를 고르고 confidence 를 낮게 적는다.\n"
    "3. rationale 은 무엇이 바뀌었는지와 권고 이유를 한국어 한 문장(100자 안팎)으로 적는다.\n"
    "4. 사람 이름·신원·행동 추측은 적지 않는다. 사람은 비교 대상이 아니다.\n"
    "5. JSON 으로만 답한다."
)

RESPONSE_FORMAT: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vlm_zone_opinion",
        "strict": True,
        "schema": {
            "type": "object",
            "required": ["recommended_action", "rationale", "confidence"],
            "properties": {
                "recommended_action": {"type": "string", "enum": list(_ACTIONS)},
                "rationale": {"type": "string", "minLength": 1, "maxLength": 300},
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
class ZoneOpinionConfig:
    """카메라 고르기·크롭·시간 예산 파라미터. 근거는 docs/design/S15P31A101-631-zone-vlm-opinion.md."""

    # 크롭 여백: 투영 상자 양쪽에 상자 크기의 25%씩(초안 크롭 select_candidate_crops 와 같다)
    pad_ratio: float = 0.25
    # 크롭 최소 변(px, 1280 px 프레임 기준). 작은 구역도 주변 맥락이 보이게 한다
    min_crop_px: int = 96
    # 구역 상자 높이 범위. 바닥 구역도 위에 놓인 물체가 보이게 최소 0.3 m, 천장까지 잡혀 크롭이 커지지 않게 최대 2.0 m
    min_box_height_m: float = 0.3
    max_box_height_m: float = 2.0
    default_box_height_m: float = 1.0
    # 상자 꼭짓점 중 이 비율 이상이 카메라 앞이어야 그 카메라를 본다
    min_in_front_ratio: float = 0.5
    # 투영 상자 중 화면 안에 든 면적 비율 하한(잘려 나간 구역은 비교가 어렵다)
    min_visible_ratio: float = 0.4
    # 화면 안 투영 상자의 최소 변(px)
    min_side_px: float = 16.0
    # 한 작업에서 의견을 낼 구역 수 상한(서버도 같은 수만 싣는다)
    max_zones: int = 8
    # VLM 한 번 부르기 전에 남아 있어야 하는 작업 시간(초). 모자라면 TIME_BUDGET
    min_call_budget_s: float = 8.0
    # 호출 시간 상한을 남은 시간보다 이만큼 짧게 둔다(결과 제출 여유)
    call_reserve_s: float = 2.0


@dataclass(frozen=True)
class SuspectedZone:
    """변경 의심 구역 하나(입력 `suspected_zones[]`)."""

    zone_id: str
    polygon_xy: tuple[tuple[float, float], ...]
    name: str | None = None
    zone_kind: str | None = None
    z_min_m: float | None = None
    z_max_m: float | None = None
    changed_area_ratio: float | None = None
    max_height_diff_m: float | None = None
    max_diff_xy: tuple[float, float] | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> SuspectedZone:
        def num(key: str) -> float | None:
            value = raw.get(key)
            return (
                float(value)
                if isinstance(value, int | float) and not isinstance(value, bool)
                else None
            )

        xy = raw.get("max_diff_xy")
        return cls(
            zone_id=str(raw["zone_id"]),
            polygon_xy=tuple((float(p[0]), float(p[1])) for p in raw["polygon_xy"]),
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
            zone_kind=raw.get("zone_kind")
            if isinstance(raw.get("zone_kind"), str)
            else None,
            z_min_m=num("z_min_m"),
            z_max_m=num("z_max_m"),
            changed_area_ratio=num("changed_area_ratio"),
            max_height_diff_m=num("max_height_diff_m"),
            max_diff_xy=(float(xy[0]), float(xy[1]))
            if isinstance(xy, list | tuple) and len(xy) == 2
            else None,
        )

    def box_z_range(self, cfg: ZoneOpinionConfig) -> tuple[float, float]:
        """투영할 상자의 높이 범위(World m). 구역 높이를 쓰되 바닥 위 최소·최대 높이로 자른다."""
        lo = self.z_min_m if self.z_min_m is not None else 0.0
        hi = self.z_max_m if self.z_max_m is not None else lo + cfg.default_box_height_m
        base = max(lo, 0.0)
        hi = min(max(hi, base + cfg.min_box_height_m), base + cfg.max_box_height_m)
        return lo, hi


def zone_box_corners(zone: SuspectedZone, cfg: ZoneOpinionConfig) -> np.ndarray:
    """구역 상자 꼭짓점 (2N, 3): 바닥 다각형을 높이 범위 아래·위에 놓는다."""
    lo, hi = zone.box_z_range(cfg)
    poly = np.asarray(zone.polygon_xy, dtype=np.float64).reshape(-1, 2)
    bottom = np.column_stack([poly, np.full(len(poly), lo)])
    top = np.column_stack([poly, np.full(len(poly), hi)])
    return np.vstack([bottom, top])


@dataclass(frozen=True)
class ProjectedBox:
    """한 카메라에 투영한 구역 상자. box_px 는 화면 안으로 자른 경계 상자(x1, y1, x2, y2)."""

    camera_id: str
    box_px: tuple[float, float, float, float]
    image_size: tuple[int, int]
    visible_ratio: float
    area_ratio: float

    @property
    def score(self) -> float:
        return self.visible_ratio * self.area_ratio


def project_zone_box(
    zone: SuspectedZone, pose: CameraPose, cfg: ZoneOpinionConfig
) -> ProjectedBox | None:
    """구역 상자를 카메라에 투영한다. 잘 보이지 않으면(카메라 뒤·화면 밖·너무 작음) None."""
    w, h = int(pose.image_size[0]), int(pose.image_size[1])
    if w <= 0 or h <= 0:
        return None
    corners = zone_box_corners(zone, cfg)
    uv, in_front = project_world_points(corners, pose)
    if not len(corners) or float(np.mean(in_front)) < cfg.min_in_front_ratio:
        return None
    front = uv[in_front]
    if not np.all(np.isfinite(front)):
        return None
    ux1, vy1 = float(front[:, 0].min()), float(front[:, 1].min())
    ux2, vy2 = float(front[:, 0].max()), float(front[:, 1].max())
    full_area = max(ux2 - ux1, 1e-6) * max(vy2 - vy1, 1e-6)
    cx1, cy1 = max(ux1, 0.0), max(vy1, 0.0)
    cx2, cy2 = min(ux2, float(w)), min(vy2, float(h))
    if cx2 - cx1 < cfg.min_side_px or cy2 - cy1 < cfg.min_side_px:
        return None
    clipped_area = (cx2 - cx1) * (cy2 - cy1)
    visible_ratio = min(1.0, clipped_area / full_area)
    if visible_ratio < cfg.min_visible_ratio:
        return None
    return ProjectedBox(
        camera_id=pose.camera_id,
        box_px=(cx1, cy1, cx2, cy2),
        image_size=(w, h),
        visible_ratio=visible_ratio,
        area_ratio=clipped_area / float(w * h),
    )


def crop_box_norm(
    box: ProjectedBox, cfg: ZoneOpinionConfig
) -> tuple[float, float, float, float]:
    """투영 상자에 여백을 붙이고 최소 변을 맞춘 크롭 영역을 정규화 좌표 [u1, v1, u2, v2] 로 돌려준다."""
    w, h = box.image_size
    x1, y1, x2, y2 = box.box_px
    bw = max(x2 - x1, float(cfg.min_crop_px))
    bh = max(y2 - y1, float(cfg.min_crop_px))
    cx, cy = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
    half_w = 0.5 * bw * (1.0 + 2.0 * cfg.pad_ratio)
    half_h = 0.5 * bh * (1.0 + 2.0 * cfg.pad_ratio)
    u1 = max(0.0, (cx - half_w) / w)
    v1 = max(0.0, (cy - half_h) / h)
    u2 = min(1.0, (cx + half_w) / w)
    v2 = min(1.0, (cy + half_h) / h)
    return (round(u1, 4), round(v1, 4), round(u2, 4), round(v2, 4))


def norm_to_pixel_rect(
    norm: Sequence[float], width: int, height: int
) -> tuple[int, int, int, int]:
    """정규화 크롭 박스를 프레임 픽셀 영역(x1, y1, x2, y2)으로 바꾼다(최소 1 px)."""
    x1 = max(0, min(width - 1, math.floor(norm[0] * width)))
    y1 = max(0, min(height - 1, math.floor(norm[1] * height)))
    x2 = max(x1 + 1, min(width, math.ceil(norm[2] * width)))
    y2 = max(y1 + 1, min(height, math.ceil(norm[3] * height)))
    return x1, y1, x2, y2


@dataclass(frozen=True)
class OpinionView:
    """전·후 모두에서 구역이 잘 보이는 카메라 하나와 각 프레임의 정규화 크롭 박스."""

    camera_id: str
    before_box_norm: tuple[float, float, float, float]
    after_box_norm: tuple[float, float, float, float]
    score: float


def select_opinion_view(
    zone: SuspectedZone,
    before_cameras: Mapping[str, CameraPose],
    after_cameras: Mapping[str, CameraPose],
    *,
    allowed_cameras: set[str] | None = None,
    cfg: ZoneOpinionConfig | None = None,
) -> OpinionView | None:
    """이전·이번 보정 모두에서 구역이 잘 보이는 같은 카메라를 고른다. 점수(화면 안 비율 × 화면 면적 비율)의 작은 쪽이 가장 큰 카메라.

    고정 카메라라 전·후 같은 카메라를 비교해야 VLM·관리자가 같은 시점으로 본다. 그런 카메라가 없으면 None(NOT_VISIBLE).
    """
    cfg = cfg or ZoneOpinionConfig()
    best: OpinionView | None = None
    for cam_id in sorted(set(before_cameras) & set(after_cameras)):
        if allowed_cameras is not None and cam_id not in allowed_cameras:
            continue
        before = project_zone_box(zone, before_cameras[cam_id], cfg)
        after = project_zone_box(zone, after_cameras[cam_id], cfg)
        if before is None or after is None:
            continue
        score = min(before.score, after.score)
        if best is None or score > best.score:
            best = OpinionView(
                cam_id, crop_box_norm(before, cfg), crop_box_norm(after, cfg), score
            )
    return best


@dataclass(frozen=True)
class ZoneOpinion:
    """구역 하나의 의견(결과 `zone_opinions[]` 항목)."""

    zone_id: str
    status: OpinionStatus
    reason: NoOpinionReason | None
    recommended_action: RecommendedAction | None
    rationale: str | None
    confidence: float | None
    camera_id: str | None
    before_crop_box_norm: tuple[float, float, float, float] | None
    after_crop_box_norm: tuple[float, float, float, float] | None
    model_version: str | None
    prompt_version: str = PROMPT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "status": self.status,
            "reason": self.reason,
            "recommended_action": self.recommended_action,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "camera_id": self.camera_id,
            "before_crop_box_norm": list(self.before_crop_box_norm)
            if self.before_crop_box_norm
            else None,
            "after_crop_box_norm": list(self.after_crop_box_norm)
            if self.after_crop_box_norm
            else None,
            "model_version": self.model_version,
            "prompt_version": self.prompt_version,
        }


def no_opinion(
    zone_id: str, reason: NoOpinionReason, view: OpinionView | None = None
) -> ZoneOpinion:
    """의견 없음. 카메라를 골랐으면 화면이 전·후 사진은 보일 수 있게 크롭 박스를 남긴다."""
    return ZoneOpinion(
        zone_id=zone_id,
        status="NO_OPINION",
        reason=reason,
        recommended_action=None,
        rationale=None,
        confidence=None,
        camera_id=view.camera_id if view else None,
        before_crop_box_norm=view.before_box_norm if view else None,
        after_crop_box_norm=view.after_box_norm if view else None,
        model_version=None,
    )


def build_opinion_prompt(
    zone: SuspectedZone, camera_id: str, cfg: ZoneOpinionConfig | None = None
) -> str:
    """구역 수치와 높이 비교 결과를 한국어 프롬프트로 조립한다(사람 정보 없음)."""
    cfg = cfg or ZoneOpinionConfig()
    xs = [p[0] for p in zone.polygon_xy]
    ys = [p[1] for p in zone.polygon_xy]
    lines = [f"구역 ID: {zone.zone_id}"]
    if zone.name:
        lines.append(f"구역 이름: {zone.name}")
    if zone.zone_kind:
        lines.append(f"구역 종류: {zone.zone_kind}")
    lines.append(
        f"바닥 범위 World XY(m): 가로 {max(xs) - min(xs):.2f} × 세로 {max(ys) - min(ys):.2f}"
    )
    lo, hi = zone.box_z_range(cfg)
    lines.append(f"살펴볼 높이 범위 World Z(m): [{lo:.2f}, {hi:.2f}]")
    if zone.max_height_diff_m is not None:
        lines.append(
            f"3D 맵 비교: 구역 바닥 상면 높이가 최대 {zone.max_height_diff_m:.2f} m 달라졌다"
        )
    if zone.changed_area_ratio is not None:
        lines.append(
            f"3D 맵 비교: 구역 면적의 {zone.changed_area_ratio * 100:.0f}% 에서 높이가 바뀌었다"
        )
    lines.append(f"카메라: {camera_id} (두 사진 모두 이 카메라)")
    lines.append("첫 번째 사진은 이전 세팅, 두 번째 사진은 이번 세팅이다.")
    lines.append(
        "무엇이 바뀌었는지 비교해 recommended_action(KEEP·MODIFY·REMOVE), rationale(한국어 한 문장), confidence 를 JSON 으로 답하라."
    )
    return "\n".join(lines)


def parse_opinion_response(
    raw_content: str,
    zone_id: str,
    view: OpinionView,
    model_version: str,
) -> ZoneOpinion:
    """VLM JSON 응답을 검사해 `ZoneOpinion` 으로 바꾼다. 형식이 어긋나면 `ValueError`·`TypeError`(호출하는 쪽이 NO_OPINION 으로 바꾼다)."""
    try:
        doc = json.loads(raw_content)
    except ValueError as e:
        raise ValueError(f"VLM 응답이 JSON 이 아니다: {e}") from e
    if not isinstance(doc, Mapping):
        raise TypeError("VLM 응답이 JSON 객체가 아니다")
    action = doc.get("recommended_action")
    if action not in _ACTIONS:
        raise ValueError(f"recommended_action 은 KEEP·MODIFY·REMOVE 다: {action!r}")
    rationale = doc.get("rationale")
    if not isinstance(rationale, str):
        raise TypeError(f"유효하지 않은 rationale: {rationale!r}")
    # 한 줄만 쓴다. 길면 잘라 화면 한 줄 길이를 지킨다
    line = next((part.strip() for part in rationale.splitlines() if part.strip()), "")
    if not line:
        raise ValueError("rationale 이 비었다")
    if len(line) > MAX_RATIONALE_CHARS:
        line = line[: MAX_RATIONALE_CHARS - 1].rstrip() + "…"
    confidence = doc.get("confidence")
    conf_val: float | None = None
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise ValueError(f"유효하지 않은 confidence: {confidence!r}")
        conf_val = float(confidence)
        if not (0.0 <= conf_val <= 1.0):
            raise ValueError(f"confidence 범위(0..1) 위반: {conf_val}")
    return ZoneOpinion(
        zone_id=zone_id,
        status="OPINION",
        reason=None,
        recommended_action=action,
        rationale=line,
        confidence=conf_val,
        camera_id=view.camera_id,
        before_crop_box_norm=view.before_box_norm,
        after_crop_box_norm=view.after_box_norm,
        model_version=model_version,
    )


def _crop(frame: VlmFrame, norm: Sequence[float]) -> VlmFrame:
    h, w = frame.pixels.shape[:2]
    return frame.with_crop(*norm_to_pixel_rect(norm, w, h))


def _model_name(vlm_client: Any, completion: Any) -> str:
    return (
        getattr(completion, "model", None)
        or getattr(getattr(vlm_client, "settings", None), "model", None)
        or getattr(vlm_client, "model", None)
        or "cosmos3-nano"
    )


def assess_zone_opinions(
    zones: Sequence[SuspectedZone],
    *,
    vlm_client: Any | None,
    before_cameras: Mapping[str, CameraPose],
    after_cameras: Mapping[str, CameraPose],
    before_frames: Mapping[str, VlmFrame]
    | Callable[[set[str]], Mapping[str, VlmFrame]],
    after_frames: Mapping[str, VlmFrame],
    allowed_cameras: set[str] | None = None,
    blocked_reason: NoOpinionReason | None = None,
    remaining_s: Callable[[], float | None] | None = None,
    cfg: ZoneOpinionConfig | None = None,
) -> list[ZoneOpinion]:
    """변경 의심 구역마다 의견을 낸다. 입력 순서대로 하나씩, 실패는 그 구역만 `NO_OPINION`.

    - `blocked_reason` 이 있으면(이전 사진·보정을 못 찾음 등) 모든 구역이 그 사유의 NO_OPINION 이다.
    - 카메라는 보정만으로 고르므로 VLM 이 없어도 크롭 박스는 남는다(화면이 전·후 사진을 보인다).
    - `before_frames` 가 함수면 고른 카메라 집합으로 한 번 불러 그 카메라의 이전 프레임만 얼굴 처리한다(필요 없는 장 처리 시간 절약).
    """
    cfg = cfg or ZoneOpinionConfig()
    if blocked_reason is not None:
        return [no_opinion(z.zone_id, blocked_reason) for z in zones]

    views: list[OpinionView | None] = [
        select_opinion_view(
            z, before_cameras, after_cameras, allowed_cameras=allowed_cameras, cfg=cfg
        )
        for z in zones
    ]
    wanted = {
        v.camera_id for i, v in enumerate(views) if v is not None and i < cfg.max_zones
    }
    if vlm_client is not None and wanted and callable(before_frames):
        before_map: Mapping[str, VlmFrame] = before_frames(wanted)
    elif isinstance(before_frames, Mapping):
        before_map = before_frames
    else:
        before_map = {}

    out: list[ZoneOpinion] = []
    for idx, (zone, view) in enumerate(zip(zones, views, strict=True)):
        if idx >= cfg.max_zones:
            out.append(no_opinion(zone.zone_id, "ZONE_LIMIT", view))
            continue
        if view is None:
            out.append(no_opinion(zone.zone_id, "NOT_VISIBLE"))
            continue
        if vlm_client is None:
            out.append(no_opinion(zone.zone_id, "VLM_UNAVAILABLE", view))
            continue
        before = before_map.get(view.camera_id)
        if before is None:
            out.append(no_opinion(zone.zone_id, "PREVIOUS_FRAMES_UNAVAILABLE", view))
            continue
        after = after_frames.get(view.camera_id)
        if after is None:
            out.append(no_opinion(zone.zone_id, "CURRENT_FRAMES_UNAVAILABLE", view))
            continue
        left = remaining_s() if remaining_s is not None else None
        if left is not None and left < cfg.min_call_budget_s:
            out.append(no_opinion(zone.zone_id, "TIME_BUDGET", view))
            continue
        kwargs: dict[str, Any] = {
            "system": SYSTEM_PROMPT,
            "response_format": RESPONSE_FORMAT,
        }
        if left is not None:
            kwargs["timeout_s"] = max(1.0, left - cfg.call_reserve_s)
        try:
            crops = [
                _crop(before, view.before_box_norm),
                _crop(after, view.after_box_norm),
            ]
            completion = vlm_client.complete(
                build_opinion_prompt(zone, view.camera_id, cfg), crops, **kwargs
            )
            out.append(
                parse_opinion_response(
                    completion.content,
                    zone.zone_id,
                    view,
                    _model_name(vlm_client, completion),
                )
            )
        except (VlmError, ValueError, TypeError):
            out.append(no_opinion(zone.zone_id, "VLM_FAILED", view))
    return out
