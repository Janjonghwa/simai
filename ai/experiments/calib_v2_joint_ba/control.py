"""Dependency-free experiment policy, split, and statistics (no native DLLs)."""

from __future__ import annotations

import hashlib
import math
from collections import defaultdict

CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")
PAIRS = ((0, 1), (1, 2), (2, 3), (3, 0))


def spatial_split(uv, size, grid=(4, 4), fraction=0.2, seed=449):
    """Exact rounded quota, allocated proportionally across occupied source cells.

    This is stratified sampling, not a claim of spatially independent regions.
    Stable hash ordering avoids dependence on feature detector ordering within cells.
    """
    if not 0 < fraction < 1 or min(size) <= 0 or min(grid) <= 0:
        raise ValueError("Invalid split parameters")
    cells = defaultdict(list)
    for i, (x, y) in enumerate(uv):
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("Nonfinite pixel")
        cell = (min(grid[0] - 1, max(0, int(x * grid[0] / size[0]))),
                min(grid[1] - 1, max(0, int(y * grid[1] / size[1]))))
        cells[cell].append(i)
    quota = {cell: math.floor(len(ids) * fraction) for cell, ids in cells.items()}
    target = round(len(uv) * fraction)
    remainder_order = sorted(cells, key=lambda c: (-(len(cells[c]) * fraction - quota[c]), c))
    for cell in remainder_order[:target - sum(quota.values())]:
        quota[cell] += 1
    held = set()
    for cell, ids in cells.items():
        ordered = sorted(ids, key=lambda i: hashlib.sha256(
            f"{seed}:{float(uv[i][0]):.8f}:{float(uv[i][1]):.8f}:{i}".encode()).digest())
        held.update(ordered[:quota[cell]])
    return [i for i in range(len(uv)) if i not in held], sorted(held)


def quarantine_shared_keypoints(tracks, merge_radius_px=0.0):
    """A held-out keypoint must not re-enter BA through another adjacent edge or window."""
    held_nodes = {tuple(node) for t in tracks if t["split"] == "holdout" for node in t["nodes"]}
    if merge_radius_px <= 0:
        return [t for t in tracks if t["split"] == "holdout"
                or not any(tuple(n) in held_nodes for n in t["nodes"])]
    held_pixels = defaultdict(list)
    for t in tracks:
        if t["split"] == "holdout":
            for c, px in zip(t.get("pair", []), t.get("uv", [])):
                held_pixels[c].append(px)
    r2 = merge_radius_px ** 2
    def near_held(track):
        if any(tuple(n) in held_nodes for n in track["nodes"]):
            return True
        for c, px in zip(track.get("pair", []), track.get("uv", [])):
            if any((px[0] - hx[0]) ** 2 + (px[1] - hx[1]) ** 2 <= r2 for hx in held_pixels[c]):
                return True
        return False
    return [t for t in tracks if t["split"] == "holdout" or not near_held(t)]


def deduplicate_multiwindow_matches(candidates, merge_radius_px=2.0):
    """Merge multi-window matches that refer to the same physical 2-view feature."""
    r2 = merge_radius_px ** 2
    kept = []
    for cand in candidates:
        ua, ub = cand["uv"]
        if any((ua[0] - k["uv"][0][0]) ** 2 + (ua[1] - k["uv"][0][1]) ** 2 <= r2
               and (ub[0] - k["uv"][1][0]) ** 2 + (ub[1] - k["uv"][1][1]) ** 2 <= r2
               for k in kept):
            continue
        kept.append(cand)
    return kept


def percentile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def statistics(values):
    """Do not silently improve metrics by dropping invalid projections."""
    finite = [float(x) for x in values if math.isfinite(x)]
    valid = len(finite) == len(values) and bool(values)
    return {"n": len(values), "invalid": len(values) - len(finite),
            "median": percentile(finite, 0.5) if valid else None,
            "p90": percentile(finite, 0.9) if valid else None,
            "p95": percentile(finite, 0.95) if valid else None}


def worsened(before, after):
    return any(after[k] is None or before[k] is None or after[k] > before[k]
               for k in ("median", "p90"))


def select_anchor(v0):
    runs = [r for r in v0["runs"] if r["feature"] == "aliked"]
    errors = runs[0]["reproj_current_cal"]
    return min(CAMS, key=lambda c: errors[c]["2view"]["median"])


def huber_terms(z, delta):
    """rho(z), rho'(z), rho''(z), z = squared unscaled pixel residual."""
    if z <= delta * delta:
        return z, 1.0, 0.0
    root = math.sqrt(z)
    return 2 * delta * root - delta * delta, delta / root, -delta / (2 * z * root)
