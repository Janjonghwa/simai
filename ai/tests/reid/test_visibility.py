"""COCO 17관절 기반 신체 부위 가시성과 공가시성(co-visibility) 신뢰도 시험 (S15P31A101-463)."""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.reid import (
    PartVisibility,
    PartWeights,
    VisibilityConfig,
    co_visibility_confidence,
    co_visible_parts,
    estimate_part_visibility,
    score_appearance_pair,
)


def _make_coco17(
    *,
    head_conf: float = 0.9,
    torso_conf: float = 0.9,
    legs_conf: float = 0.9,
) -> list[list[float]]:
    kps: list[list[float]] = []
    for i in range(17):
        if i <= 4:
            c = head_conf
            y = 20.0 + i * 2.0
        elif i <= 12:
            c = torso_conf
            y = 60.0 + (i - 5) * 8.0
        else:
            c = legs_conf
            y = 150.0 + (i - 13) * 15.0
        kps.append([50.0, y, c])
    return kps


def test_estimate_part_visibility_full_and_partial() -> None:
    full_kps = _make_coco17(head_conf=0.9, torso_conf=0.85, legs_conf=0.8)
    vis_full = estimate_part_visibility(full_kps, bbox_xyxy=(0.0, 0.0, 100.0, 240.0))
    assert vis_full.head is True
    assert vis_full.torso is True
    assert vis_full.legs is True
    assert vis_full.visible_parts == ("head", "torso", "legs")
    assert vis_full.visibility_ratio() == pytest.approx(1.0)

    # 하체가 책상·난간에 가려진 경우 (legs_conf < min_joint_conf)
    upper_kps = _make_coco17(head_conf=0.9, torso_conf=0.85, legs_conf=0.1)
    vis_upper = estimate_part_visibility(upper_kps)
    assert vis_upper.head is True
    assert vis_upper.torso is True
    assert vis_upper.legs is False
    assert vis_upper.visible_parts == ("head", "torso")
    assert vis_upper.visibility_ratio() == pytest.approx(0.65)

    # 관절이 없거나 부족한 경우
    assert estimate_part_visibility(None) == PartVisibility.none_visible()
    assert estimate_part_visibility([[10.0, 20.0, 0.9]] * 5) == PartVisibility.none_visible()


def test_estimate_part_visibility_respects_bbox_bounds() -> None:
    kps = _make_coco17(head_conf=0.9, torso_conf=0.9, legs_conf=0.9)
    # 다리 관절이 bbox 바깥 멀리 찍힌 경우
    for idx in (13, 14, 15, 16):
        kps[idx] = [50.0, 500.0, 0.95]

    vis = estimate_part_visibility(
        kps,
        bbox_xyxy=(0.0, 0.0, 100.0, 140.0),
        config=VisibilityConfig(bbox_margin_ratio=0.05),
    )
    assert vis.head is True
    assert vis.torso is True
    assert vis.legs is False


def test_co_visibility_confidence_and_appearance_scoring() -> None:
    weights = PartWeights(head=0.15, torso=0.50, legs=0.35)
    vis_full = PartVisibility(head=True, torso=True, legs=True)
    vis_upper = PartVisibility(head=True, torso=True, legs=False)
    vis_legs_only = PartVisibility(head=False, torso=False, legs=True)

    assert co_visibility_confidence(vis_full, vis_full, weights) == pytest.approx(1.0)
    assert co_visibility_confidence(vis_full, vis_upper, weights) == pytest.approx(0.65)
    assert co_visible_parts(vis_full, vis_upper) == ("head", "torso")

    # 상체만 보이는 관측과 다리만 보이는 관측은 겹치는 부위가 없어 신뢰도 0
    assert co_visibility_confidence(vis_upper, vis_legs_only, weights) == pytest.approx(0.0)
    assert co_visible_parts(vis_upper, vis_legs_only) == ()

    emb_a = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    emb_b = np.array([0.8, 0.6, 0.0], dtype=np.float32)

    score_ok = score_appearance_pair(emb_a, emb_b, vis_full, vis_upper, weights=weights, min_co_visibility=0.5)
    assert score_ok.raw_similarity == pytest.approx(0.8, abs=1e-5)
    assert score_ok.co_visibility == pytest.approx(0.65, abs=1e-5)
    assert score_ok.effective_similarity == pytest.approx(0.8 * 0.65, abs=1e-5)
    assert score_ok.reliable is True

    score_unreliable = score_appearance_pair(emb_a, emb_b, vis_upper, vis_legs_only, weights=weights, min_co_visibility=0.35)
    assert score_unreliable.co_visibility == 0.0
    assert score_unreliable.effective_similarity == 0.0
    assert score_unreliable.reliable is False
