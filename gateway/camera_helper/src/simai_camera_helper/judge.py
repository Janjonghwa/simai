"""Pure overlap decisions; only the standard library is required.

Thresholds and image-space direction signs follow .agy/helper-interface.md.
This module never estimates positions, changes calibration, or gates LIVE.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from typing import Generic, TypeVar

CAMERAS = ("cam_A", "cam_B", "cam_C", "cam_D")
PAIRS = tuple(combinations(CAMERAS, 2))
SIDE_PAIRS = (("cam_A", "cam_B"), ("cam_B", "cam_C"), ("cam_C", "cam_D"), ("cam_A", "cam_D"))
"""세팅 Stage A 가 맞추는 옆 4쌍(고리 A–B, B–C, C–D, D–A, ai joint_ba_v2.PAIRS). 대각 A–C·B–D 는 Stage A 기준 판정·경고에서 뺀다."""
GRID = (4, 4)
"""인라이어 분포를 세는 격자(가로, 세로). Stage A 공간 홀드아웃 분할(joint_ba_v2 grid [4, 4])과 같다."""
CELL_MIN_INLIERS = 2
"""격자 칸 하나를 「점이 있는 칸」으로 세는 최소 인라이어 수(기하 검증을 통과한 외톨이 한 점은 세지 않는다)."""


@dataclass(frozen=True)
class Params:
    image_width_px: int = 1024
    max_keypoints: int = 1024
    ransac_threshold_px: float = 2.0
    enough_min_inliers: int = 30
    weak_min_inliers: int = 15
    min_inlier_ratio: float = 0.10
    intrinsics_source: str = "STORED"

    def __post_init__(self) -> None:
        if self.image_width_px < 1 or self.max_keypoints < 1:
            raise ValueError("image width and keypoint limit must be positive")
        if not isfinite(self.ransac_threshold_px) or self.ransac_threshold_px <= 0:
            raise ValueError("RANSAC threshold must be positive and finite")
        if not 1 <= self.weak_min_inliers < self.enough_min_inliers:
            raise ValueError("require 1 <= weak_min_inliers < enough_min_inliers")
        if not 0 < self.min_inlier_ratio <= 1:
            raise ValueError("inlier ratio must be in (0, 1]")
        if self.intrinsics_source not in ("STORED", "DEFAULT"):
            raise ValueError("intrinsics_source must be STORED or DEFAULT")


@dataclass(frozen=True)
class PairMetrics:
    a: str
    b: str
    raw_matches: int | None = None
    inliers: int | None = None
    centroid_a: tuple[float, float] | None = None
    centroid_b: tuple[float, float] | None = None
    cells_a: int | None = None
    """카메라 a 분석 영상 GRID 칸 가운데 인라이어가 CELL_MIN_INLIERS 이상인 칸 수(S15P31A101-761). 모르면 None"""
    cells_b: int | None = None

    def __post_init__(self) -> None:
        if (self.a, self.b) not in PAIRS:
            raise ValueError("pair must use the canonical camera order")
        if (self.raw_matches is None) != (self.inliers is None):
            raise ValueError("raw_matches and inliers must both be known or unknown")
        if self.raw_matches is not None and (type(self.raw_matches) is not int or type(self.inliers) is not int):
            raise ValueError("match counts must be integers")
        if self.raw_matches is not None and not 0 <= self.inliers <= self.raw_matches:
            raise ValueError("require 0 <= inliers <= raw_matches")
        for centroid in (self.centroid_a, self.centroid_b):
            if centroid is not None:
                if self.inliers is None or self.inliers < 5:
                    raise ValueError("centroid requires at least five inliers")
                if len(centroid) != 2 or any(not isfinite(x) or not 0 <= x <= 1 for x in centroid):
                    raise ValueError("centroid must be a finite normalized image point")
        for cells in (self.cells_a, self.cells_b):
            if cells is not None:
                if self.inliers is None:
                    raise ValueError("occupied cells require known inliers")
                if type(cells) is not int or not 0 <= cells <= GRID[0] * GRID[1]:
                    raise ValueError("occupied cells must be an integer within the grid")

    @property
    def inlier_ratio(self) -> float | None:
        if self.raw_matches is None:
            return None
        return self.inliers / self.raw_matches if self.raw_matches else 0.0


DEFAULT_PARAMS = Params()


def pair_grade(pair: PairMetrics, params: Params = DEFAULT_PARAMS) -> str:
    if pair.inliers is None:
        return "UNKNOWN"
    if pair.inlier_ratio >= params.min_inlier_ratio:
        if pair.inliers >= params.enough_min_inliers:
            return "ENOUGH"
        if pair.inliers >= params.weak_min_inliers:
            return "WEAK"
    return "LACK"


def occupied_cells(points, grid: tuple[int, int] = GRID, min_per_cell: int = CELL_MIN_INLIERS) -> int:
    """정규화 좌표 [0, 1] 점들이 grid 칸 가운데 몇 칸에 min_per_cell 개 이상 있는지. 한 구석 몰림을 잰다."""
    counts: dict[tuple[int, int], int] = {}
    for x, y in points:
        cell = (min(grid[0] - 1, max(0, int(x * grid[0]))), min(grid[1] - 1, max(0, int(y * grid[1]))))
        counts[cell] = counts.get(cell, 0) + 1
    return sum(1 for n in counts.values() if n >= min_per_cell)


def connectivity(grades: dict[tuple[str, str], str]) -> tuple[str, list[list[str]]]:
    """Known edges form components. Unknown edges yield UNKNOWN only if they could connect them."""
    if set(grades) != set(PAIRS) or any(g not in ("ENOUGH", "WEAK", "LACK", "UNKNOWN")
                                        for g in grades.values()):
        raise ValueError("all six pair grades are required")
    parent = {c: c for c in CAMERAS}

    def find(c: str) -> str:
        while parent[c] != c:
            parent[c] = parent[parent[c]]
            c = parent[c]
        return c

    def union(a: str, b: str) -> None:
        parent[find(b)] = find(a)

    for (a, b), grade in grades.items():
        if grade in ("ENOUGH", "WEAK"):
            union(a, b)
    groups: dict[str, list[str]] = {}
    for c in CAMERAS:
        groups.setdefault(find(c), []).append(c)
    components = list(groups.values())
    if len(components) == 1:
        return "CONNECTED", components
    # If even every unknown edge cannot bridge the known components, disconnection is certain.
    for (a, b), grade in grades.items():
        if grade == "UNKNOWN":
            union(a, b)
    possible = len({find(c) for c in CAMERAS}) == 1
    return ("UNKNOWN" if possible else "DISCONNECTED"), components


def direction(centroid: tuple[float, float] | None) -> str:
    if centroid is None:
        return "NONE"
    x, y = centroid
    if x > 0.65:
        return "RIGHT"
    if x < 0.35:
        return "LEFT"
    if y > 0.68:
        return "DOWN"
    if y < 0.32:
        return "UP"
    return "NONE"


def guides(pairs: list[PairMetrics], grades: dict[tuple[str, str], str], state: str,
           flagged: frozenset[tuple[str, str]] | set[tuple[str, str]] = frozenset()) -> list[dict]:
    """flagged: Stage A 기준 미달·불안정 옆 쌍(S15P31A101-761). 인라이어가 30 이상이어도 가장자리 몰림 방향을 안내한다."""
    result = []
    for pair in pairs:
        grade = grades[(pair.a, pair.b)]
        if grade == "UNKNOWN" or pair.inliers is None:
            continue
        if (pair.a, pair.b) in flagged:
            magnitude = "MORE" if grade == "LACK" and pair.inliers < 30 else "SMALL"
        elif grade not in ("WEAK", "LACK") or pair.inliers >= 30:
            # Low ratio can make a >=30 inlier pair LACK; the interface excludes it from direction advice.
            continue
        else:
            magnitude = "SMALL" if grade == "WEAK" else "MORE"
        if pair.inliers < 5:
            result.append({"camera_id": pair.a, "pair": [pair.a, pair.b], "direction": "NONE",
                           "magnitude": magnitude, "reason_code": "NO_OVERLAP_VISIBLE"})
            continue
        for camera_id, centroid in ((pair.a, pair.centroid_a), (pair.b, pair.centroid_b)):
            turn = direction(centroid)
            if turn != "NONE":
                result.append({"camera_id": camera_id, "pair": [pair.a, pair.b], "direction": turn,
                               "magnitude": magnitude,
                               "reason_code": "OPTIONAL_IMPROVE" if state == "CONNECTED"
                               else "WEAK_PAIR_EDGE_CLUSTER"})
    return result


@dataclass(frozen=True)
class ReadinessParams:
    """「Stage A 를 통과할 만큼 충분」 기준(S15P31A101-761). 근거: docs/design/S15P31A101-761-helper-stage-a-criteria.md"""

    min_inliers: int = 60
    """헬퍼(ALIKED+LightGlue 1024 px) 인라이어. Stage A(전체 해상도 SIFT, Sampson 1 px) 트랙은 이 값의 약 0.5~1.0 배라
    홀드아웃(20 %) 최소 5 트랙(품질 게이트 min_holdout_tracks)을 남기는 값"""
    min_inlier_ratio: float = 0.10
    min_cells: int = 4
    """두 영상 모두 GRID 16칸 가운데 이만큼 칸에 점이 있어야 한다(옆 쌍 겹침 띠 한 열 = 4칸, 한 구석 몰림은 1~3칸)"""
    window_rounds: int = 4
    """연속 충분 라운드 수. 라운드 간격 = 키프레임 약 3 s(GPU 매칭 1.8 s) → 4라운드가 최근 약 10 s"""

    def __post_init__(self) -> None:
        if self.min_inliers < 1 or not 1 <= self.min_cells <= GRID[0] * GRID[1] or self.window_rounds < 2:
            raise ValueError("readiness thresholds must be positive and the window at least two rounds")
        if not 0 < self.min_inlier_ratio <= 1:
            raise ValueError("readiness inlier ratio must be in (0, 1]")


DEFAULT_READINESS = ReadinessParams()
READINESS_WARN = ("SHORT", "UNSTABLE")


def shortfall(pair: PairMetrics, params: ReadinessParams = DEFAULT_READINESS) -> list[str] | None:
    """이 라운드의 Stage A 기준 미달 사유. 측정이 없으면(영상 없음·오래됨) None, 충분하면 빈 목록."""
    if pair.inliers is None:
        return None
    reasons = []
    if pair.inliers < params.min_inliers:
        reasons.append("FEW_INLIERS")
    if pair.inlier_ratio < params.min_inlier_ratio:
        reasons.append("LOW_RATIO")
    if min(pair.cells_a or 0, pair.cells_b or 0) < params.min_cells:
        reasons.append("CLUSTERED")
    return reasons


class StageAReadiness:
    """옆 4쌍마다 최근 window_rounds 라운드의 충분 여부를 들고 상태를 낸다. 대각 쌍은 보지 않는다.

    READY: 최근 window_rounds 라운드 모두 충분 · CONFIRMING: 지금까지 모두 충분하나 아직 창이 차지 않음 ·
    UNSTABLE: 창 안에 충분과 미달이 섞임 · SHORT: 창 안 모두 미달 · UNKNOWN: 이 라운드 측정 없음(창을 비운다).
    """

    def __init__(self, params: ReadinessParams = DEFAULT_READINESS):
        self.params = params
        self._history = {p: deque(maxlen=params.window_rounds) for p in SIDE_PAIRS}

    def update(self, pairs: list[PairMetrics]) -> list[dict]:
        by_key = {(p.a, p.b): p for p in pairs}
        rows = []
        for key in SIDE_PAIRS:
            pair = by_key[key]
            reasons = shortfall(pair, self.params)
            history = self._history[key]
            if reasons is None:
                history.clear()
                status = "UNKNOWN"
            else:
                history.append(not reasons)
                good = sum(history)
                if good == len(history):
                    status = "READY" if len(history) == self.params.window_rounds else "CONFIRMING"
                else:
                    status = "SHORT" if good == 0 else "UNSTABLE"
            rows.append({"a": key[0], "b": key[1], "status": status, "inliers": pair.inliers,
                         "cells_a": None if reasons is None else (pair.cells_a or 0),
                         "cells_b": None if reasons is None else (pair.cells_b or 0),
                         "shortfall": reasons or [], "sufficient_rounds": sum(history),
                         "observed_rounds": len(history)})
        return rows

    def summary(self, rows: list[dict]) -> dict:
        p = self.params
        return {"min_inliers": p.min_inliers, "min_inlier_ratio": p.min_inlier_ratio, "grid": list(GRID),
                "min_cell_inliers": CELL_MIN_INLIERS, "min_cells": p.min_cells, "window_rounds": p.window_rounds,
                "pairs": rows}


T = TypeVar("T")


class TwoRoundConfirmation(Generic[T]):
    """A new value is committed after two consecutive observations, including startup."""

    def __init__(self, initial: T):
        self.value = initial
        self._candidate = initial
        self._count = 0

    def update(self, value: T) -> T:
        if value == self._candidate:
            self._count += 1
        else:
            self._candidate, self._count = value, 1
        if self._count >= 2:
            self.value = value
        return self.value


class CoverageJudge:
    """One instance per source, called once per new four-camera round.

    Hold the last accepted pair evidence while its grade is changing so counts and
    grade describe the same observation. Missing/stale video invalidates evidence
    immediately; reacquisition must pass the two-round confirmation again.
    """

    def __init__(self, params: Params = DEFAULT_PARAMS, readiness: ReadinessParams = DEFAULT_READINESS):
        self.params = params
        self._grades = {p: TwoRoundConfirmation("UNKNOWN") for p in PAIRS}
        self._evidence = {p: PairMetrics(*p) for p in PAIRS}
        self._connection = TwoRoundConfirmation("UNKNOWN")
        self.readiness = StageAReadiness(readiness)
        self.stage_a: list[dict] = []
        """마지막 evaluate 의 옆 4쌍 Stage A 기준 상태(StageAReadiness.update 행)"""

    def evaluate(self, pairs: list[PairMetrics]) -> tuple[list[PairMetrics], dict, str, list, list]:
        if len(pairs) != 6 or {(p.a, p.b) for p in pairs} != set(PAIRS):
            raise ValueError("exactly six unique camera pairs are required")
        raw_grades = {(p.a, p.b): pair_grade(p, self.params) for p in pairs}
        raw_state, _ = connectivity(raw_grades)
        grades = {}
        for pair in pairs:
            key = (pair.a, pair.b)
            candidate = pair_grade(pair, self.params)
            if candidate == "UNKNOWN":
                # Validity overrides smoothing: never claim overlap for NO_VIDEO/STALE.
                self._grades[key] = TwoRoundConfirmation("UNKNOWN")
                self._evidence[key] = PairMetrics(*key)
            accepted = self._grades[key].update(candidate)
            grades[key] = accepted
            if candidate == accepted:
                self._evidence[key] = pair
        accepted_pairs = [self._evidence[p] for p in PAIRS]
        candidate_state, components = connectivity(grades)
        # Confirm the raw graph in parallel with pair grades, avoiding a third-round startup delay.
        state = self._connection.update(raw_state)
        if any(p.inliers is None for p in pairs) and candidate_state == "UNKNOWN":
            self._connection = TwoRoundConfirmation("UNKNOWN")
            state = "UNKNOWN"
        # Stage A 기준은 등급 2회 확인과 따로 최근 창으로 본다(이 라운드 원 측정값)
        self.stage_a = self.readiness.update(pairs)
        flagged = {(r["a"], r["b"]) for r in self.stage_a if r["status"] in READINESS_WARN}
        return accepted_pairs, grades, state, components, guides(accepted_pairs, grades, state, flagged)
