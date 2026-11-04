"""JSON-compatible coverage_guide 1.0.0 messages; no images or keypoint arrays."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .judge import CAMERAS, CoverageJudge, PairMetrics

DEFAULT_STALE_MS = 5000


@dataclass(frozen=True)
class CameraFrame:
    camera_id: str
    frame_ts_ms: int | None
    has_video: bool = True

    def status(self, computed_at_ms: int, stale_ms: int = DEFAULT_STALE_MS) -> str:
        if not self.has_video or self.frame_ts_ms is None:
            return "NO_VIDEO"
        return "STALE" if computed_at_ms - self.frame_ts_ms > stale_ms else "OK"


def build_message(source_id: str, computed_at_ms: int, cameras: list[CameraFrame],
                  pairs: list[PairMetrics], judge: CoverageJudge, *, ttl_ms: int = 10_000,
                  stale_ms: int = DEFAULT_STALE_MS) -> dict:
    if not source_id or computed_at_ms < 0 or ttl_ms <= 0:
        raise ValueError("source_id, nonnegative UTC milliseconds, and positive TTL are required")
    if len(cameras) != 4 or {c.camera_id for c in cameras} != set(CAMERAS):
        raise ValueError("exactly the four configured cameras are required")
    by_id = {c.camera_id: c for c in cameras}
    camera_records = [{"camera_id": c, "status": by_id[c].status(computed_at_ms, stale_ms),
                       "frame_ts_ms": by_id[c].frame_ts_ms} for c in CAMERAS]
    unavailable = {c["camera_id"] for c in camera_records if c["status"] != "OK"}
    available_pairs = [PairMetrics(p.a, p.b) if p.a in unavailable or p.b in unavailable else p
                       for p in pairs]
    accepted, grades, state, components, instructions = judge.evaluate(available_pairs)
    timestamps = [c.frame_ts_ms for c in cameras if c.status(computed_at_ms, stale_ms) == "OK"]
    skew = max(timestamps) - min(timestamps) if len(timestamps) >= 2 else None
    pair_records = []
    for pair in accepted:
        record = asdict(pair)
        record.update(grade=grades[(pair.a, pair.b)], inlier_ratio=pair.inlier_ratio)
        for key in ("centroid_a", "centroid_b"):
            record[key] = list(record[key]) if record[key] is not None else None
        pair_records.append(record)
    return {"schema_version": "1.0.0", "source_id": source_id, "computed_at_ms": computed_at_ms,
            "valid_until_ms": computed_at_ms + ttl_ms, "capture_skew_ms": skew,
            "params": asdict(judge.params), "cameras": camera_records, "pairs": pair_records,
            "connectivity": state, "components": components, "guides": instructions}
