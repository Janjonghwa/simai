"""COCO 17관절 기반 신체 부위(머리·몸통·다리) 가시성 판정 및 공가시성(co-visibility) 신뢰도 (S15P31A101-463).

VGOSL 아이디어를 단순화해, 두 2D 관측에서 공통으로 보이는 신체 부위(head, torso, legs)의
가중치 비중을 외형 유사도의 신뢰도로 사용한다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

# COCO 17 관절 인덱스:
# 0: 코, 1: 왼눈, 2: 오른눈, 3: 왼귀, 4: 오른귀
HEAD_JOINT_INDICES: tuple[int, ...] = (0, 1, 2, 3, 4)
# 5: 왼어깨, 6: 오른어깨, 11: 왼엉덩이, 12: 오른엉덩이
TORSO_JOINT_INDICES: tuple[int, ...] = (5, 6, 11, 12)
# 13: 왼무릎, 14: 오른무릎, 15: 왼발목, 16: 오른발목
LEG_JOINT_INDICES: tuple[int, ...] = (13, 14, 15, 16)


@dataclass(frozen=True)
class PartWeights:
    """부위별 외형 정보 비중 가중치."""

    head: float = 0.15
    torso: float = 0.50
    legs: float = 0.35

    def __post_init__(self) -> None:
        if self.head < 0 or self.torso < 0 or self.legs < 0:
            raise ValueError("부위 가중치는 음수일 수 없습니다.")
        if self.head + self.torso + self.legs <= 0:
            raise ValueError("부위 가중치 합은 0보다 커야 합니다.")

    @property
    def total(self) -> float:
        return float(self.head + self.torso + self.legs)

    def as_dict(self) -> dict[str, float]:
        t = self.total
        return {
            "head": self.head / t,
            "torso": self.torso / t,
            "legs": self.legs / t,
        }


@dataclass(frozen=True)
class VisibilityConfig:
    """2D 관절 기반 부위 가시성 판정 설정."""

    min_joint_conf: float = 0.30
    min_head_joints: int = 2
    min_torso_joints: int = 2
    min_leg_joints: int = 2
    bbox_margin_ratio: float = 0.10
    weights: PartWeights = field(default_factory=PartWeights)


@dataclass(frozen=True)
class PartVisibility:
    """단일 관측의 부위별 가시성 판정 결과."""

    head: bool
    torso: bool
    legs: bool
    head_score: float = 0.0
    torso_score: float = 0.0
    legs_score: float = 0.0

    @classmethod
    def all_visible(cls) -> PartVisibility:
        return cls(
            head=True,
            torso=True,
            legs=True,
            head_score=1.0,
            torso_score=1.0,
            legs_score=1.0,
        )

    @classmethod
    def none_visible(cls) -> PartVisibility:
        return cls(
            head=False,
            torso=False,
            legs=False,
            head_score=0.0,
            torso_score=0.0,
            legs_score=0.0,
        )

    @property
    def visible_parts(self) -> tuple[str, ...]:
        parts: list[str] = []
        if self.head:
            parts.append("head")
        if self.torso:
            parts.append("torso")
        if self.legs:
            parts.append("legs")
        return tuple(parts)

    @property
    def any_visible(self) -> bool:
        return bool(self.head or self.torso or self.legs)

    def visibility_ratio(self, weights: PartWeights | None = None) -> float:
        """단일 관측에서 보이는 부위의 가중치 비율 [0.0, 1.0]."""
        w = (weights or PartWeights()).as_dict()
        score = 0.0
        if self.head:
            score += w["head"]
        if self.torso:
            score += w["torso"]
        if self.legs:
            score += w["legs"]
        return round(float(score), 6)


def _joint_is_valid(
    kp: Any,
    min_conf: float,
    bounds: tuple[float, float, float, float] | None,
) -> tuple[bool, float]:
    if kp is None:
        return False, 0.0
    try:
        if len(kp) < 2:
            return False, 0.0
        x = float(kp[0])
        y = float(kp[1])
        conf = float(kp[2]) if len(kp) >= 3 else 1.0
    except (TypeError, ValueError):
        return False, 0.0

    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(conf)):
        return False, 0.0
    if x == 0.0 and y == 0.0 and conf == 0.0:
        return False, 0.0
    if conf < min_conf:
        return False, 0.0
    if bounds is not None:
        x1, y1, x2, y2 = bounds
        if not (x1 <= x <= x2 and y1 <= y <= y2):
            return False, 0.0
    return True, min(1.0, max(0.0, conf))


def _expanded_bounds(
    bbox_xyxy: Sequence[float] | None,
    margin_ratio: float,
) -> tuple[float, float, float, float] | None:
    if bbox_xyxy is None or len(bbox_xyxy) < 4:
        return None
    x1, y1, x2, y2 = (float(bbox_xyxy[0]), float(bbox_xyxy[1]), float(bbox_xyxy[2]), float(bbox_xyxy[3]))
    w = max(0.0, x2 - x1)
    h = max(0.0, y2 - y1)
    if w <= 0.0 or h <= 0.0:
        return None
    mx = w * max(0.0, margin_ratio)
    my = h * max(0.0, margin_ratio)
    return (x1 - mx, y1 - my, x2 + mx, y2 + my)


def estimate_part_visibility(
    keypoints_coco17: Sequence[Any] | None,
    bbox_xyxy: Sequence[float] | None = None,
    config: VisibilityConfig | None = None,
) -> PartVisibility:
    """COCO 17관절(또는 23관절 앞 17개)에서 머리·몸통·다리 가시성을 판정한다."""
    cfg = config or VisibilityConfig()
    if keypoints_coco17 is None or len(keypoints_coco17) < 17:
        return PartVisibility.none_visible()

    bounds = _expanded_bounds(bbox_xyxy, cfg.bbox_margin_ratio)

    def _eval_group(indices: tuple[int, ...], min_count: int) -> tuple[bool, float]:
        confs: list[float] = []
        for idx in indices:
            ok, c = _joint_is_valid(keypoints_coco17[idx], cfg.min_joint_conf, bounds)
            if ok:
                confs.append(c)
        visible = len(confs) >= min_count
        score = sum(confs) / len(indices) if confs else 0.0
        return visible, round(float(score), 4)

    head_ok, head_score = _eval_group(HEAD_JOINT_INDICES, cfg.min_head_joints)
    torso_ok, torso_score = _eval_group(TORSO_JOINT_INDICES, cfg.min_torso_joints)
    legs_ok, legs_score = _eval_group(LEG_JOINT_INDICES, cfg.min_leg_joints)

    return PartVisibility(
        head=head_ok,
        torso=torso_ok,
        legs=legs_ok,
        head_score=head_score,
        torso_score=torso_score,
        legs_score=legs_score,
    )


def co_visible_parts(vis_a: PartVisibility, vis_b: PartVisibility) -> tuple[str, ...]:
    """두 관측 모두에서 보이는 신체 부위 이름 목록을 반환한다."""
    shared: list[str] = []
    if vis_a.head and vis_b.head:
        shared.append("head")
    if vis_a.torso and vis_b.torso:
        shared.append("torso")
    if vis_a.legs and vis_b.legs:
        shared.append("legs")
    return tuple(shared)


def co_visibility_confidence(
    vis_a: PartVisibility,
    vis_b: PartVisibility,
    weights: PartWeights | None = None,
) -> float:
    """두 관측에서 공통으로 보이는 부위의 가중치 비중 [0.0, 1.0]을 계산한다."""
    w = (weights or PartWeights()).as_dict()
    conf = 0.0
    if vis_a.head and vis_b.head:
        conf += w["head"]
    if vis_a.torso and vis_b.torso:
        conf += w["torso"]
    if vis_a.legs and vis_b.legs:
        conf += w["legs"]
    return round(float(conf), 6)


@dataclass(frozen=True)
class AppearanceMatchScore:
    """공가시성 신뢰도를 반영한 두 관측 간 외형 유사도 결과."""

    raw_similarity: float
    co_visibility: float
    effective_similarity: float
    co_visible_parts: tuple[str, ...]
    reliable: bool


def score_appearance_pair(
    emb_a: Any,
    emb_b: Any,
    vis_a: PartVisibility | None = None,
    vis_b: PartVisibility | None = None,
    *,
    weights: PartWeights | None = None,
    min_co_visibility: float = 0.35,
) -> AppearanceMatchScore:
    """두 L2 정규화 임베딩과 부위 가시성으로 코사인 유사도 및 공가시성 신뢰도를 계산한다."""
    import numpy as np

    va = np.asarray(emb_a, dtype=np.float32).reshape(-1)
    vb = np.asarray(emb_b, dtype=np.float32).reshape(-1)
    if va.shape != vb.shape or va.size == 0:
        raise ValueError(f"임베딩 차원이 맞지 않습니다: {va.shape} vs {vb.shape}")

    na = float(np.linalg.norm(va))
    nb = float(np.linalg.norm(vb))
    if na <= 1e-12 or nb <= 1e-12:
        raw_sim = 0.0
    else:
        raw_sim = float(np.dot(va / na, vb / nb))
        raw_sim = max(-1.0, min(1.0, raw_sim))

    if vis_a is None or vis_b is None:
        co_vis = 1.0
        shared_parts: tuple[str, ...] = ("head", "torso", "legs")
    else:
        co_vis = co_visibility_confidence(vis_a, vis_b, weights=weights)
        shared_parts = co_visible_parts(vis_a, vis_b)

    reliable = co_vis >= min_co_visibility and (na > 1e-12 and nb > 1e-12)
    effective = raw_sim * co_vis
    return AppearanceMatchScore(
        raw_similarity=raw_sim,
        co_visibility=co_vis,
        effective_similarity=effective,
        co_visible_parts=shared_parts,
        reliable=reliable,
    )
