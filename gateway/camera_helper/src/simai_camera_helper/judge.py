"""Pure overlap decisions; only the standard library is required.

Thresholds and image-space direction signs follow .agy/helper-interface.md.
This module never estimates positions, changes calibration, or gates LIVE.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import isfinite
from typing import Generic, TypeVar

CAMERAS = ("cam_A", "cam_B", "cam_C", "cam_D")
PAIRS = tuple(combinations(CAMERAS, 2))


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

    @property
    def inlier_ratio(self) -> float | None:
        if self.raw_matches is None:
            return None
        return self.inliers / self.raw_matches if self.raw_matches else 0.0


def pair_grade(pair: PairMetrics, params: Params = Params()) -> str:
    if pair.inliers is None:
        return "UNKNOWN"
    if pair.inlier_ratio >= params.min_inlier_ratio:
        if pair.inliers >= params.enough_min_inliers:
            return "ENOUGH"
        if pair.inliers >= params.weak_min_inliers:
            return "WEAK"
    return "LACK"


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


def guides(pairs: list[PairMetrics], grades: dict[tuple[str, str], str], state: str) -> list[dict]:
    result = []
    for pair in pairs:
        grade = grades[(pair.a, pair.b)]
        if grade not in ("WEAK", "LACK") or pair.inliers is None:
            continue
        # Low ratio can make a >=30 inlier pair LACK; the interface excludes it from direction advice.
        if pair.inliers >= 30:
            continue
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

    def __init__(self, params: Params = Params()):
        self.params = params
        self._grades = {p: TwoRoundConfirmation("UNKNOWN") for p in PAIRS}
        self._evidence = {p: PairMetrics(*p) for p in PAIRS}
        self._connection = TwoRoundConfirmation("UNKNOWN")

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
        return accepted_pairs, grades, state, components, guides(accepted_pairs, grades, state)
