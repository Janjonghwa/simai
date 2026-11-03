"""2D 관측 캐시로 Tier 0 추적·융합만 다시 돌린다 (S15P31A101-413).

`detect_cache.py` 가 남긴 검출(카메라·프레임별 박스·관절)을 운영과 같은 경로에 넣는다:
캐시 → (카메라별 TrackTrack, `MultiCameraLocalTracker`) → `services.tier0.perception.Perception`(공통 시각·늦은 카메라 제외·
보간·Cross-view·World Track·접지). 삼각측량·추적을 따로 만들지 않고(ai/CONVENTION.md) 운영 객체에 설정만 바꿔 끼운다.

시각 모형(녹화 재생의 한계는 docs/design/tier0-track-id-replay.md):
- 프레임은 녹화 헤더의 수신 시각(received_at_ms) + `infer_latency_ms` 에 "검출 결과가 나온" 것으로 본다(운영은 검출 스레드가
  카메라당 약 12 fps 로 latest-frame-wins). 캐시는 12.5 fps(stride 2)라 프레임을 모두 쓴다.
- 판정 tick 은 `tick_ms`(100) 마다 `Perception.step(now)`. 시계는 녹화 시각(실제 시간을 기다리지 않는다, 결정적).
- 실험용 교란: 카메라별 촬영 시각 이동(`feed.camera_offset_ms`), 수신 지연 추가(`feed.extra_arrival_ms`), 불확실성 덮어쓰기.

설정은 JSON 하나(`--config`): {"cross_view": {...CrossViewConfig}, "interpolation": {...}, "time": {...TimeModelConfig},
"world": {...WorldTrackConfig}, "tracker": {...TrackTrack 키}, "feed": {...}}. 빠진 값은 운영 기본값이다.

출력(`--out`, 저장소 밖): metrics.json(지표·계측 누계), tracks.jsonl.gz(tick 별 Track 요약: 위치·카메라 수·생명주기).
사람 위치가 든 dev_test 파생물이라 커밋하지 않는다.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

from services.tier0.perception import Perception
from simai_ai.contact import SurfaceSet
from simai_ai.geometry.interpolation import InterpolationConfig
from simai_ai.geometry.rig import CameraRig
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.tracking.cross_view import CrossViewAssociator, CrossViewConfig, LocalTrackletBuffer
from simai_ai.tracking.local_config import tracker_config
from simai_ai.tracking.local_tracker import CameraDetections, MultiCameraLocalTracker, Observation2DFrame, PoseDetection
from simai_ai.tracking.time_model import TimeModelConfig
from simai_ai.tracking.world_track import WorldTrackConfig

DEFAULT_ZONE = [[2.55, 0.4], [4.9, 0.4], [4.9, 2.95], [2.55, 2.95]]  # 10차 구성 묶음 cfg-7·8 z-94ffd421 (266z World)


# ----------------------------------------------------------------------------- 입력


def load_cache(cache_dir: Path, cameras: Sequence[str] | None = None) -> dict[str, list[tuple[dict, list]]]:
    out: dict[str, list[tuple[dict, list]]] = {}
    for path in sorted(cache_dir.glob("*.jsonl.gz")):
        cam = path.name.split(".")[0]
        if cameras and cam not in cameras:
            continue
        rows = []
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                if r.get("dets") is None:
                    continue  # 디코딩 실패·손상 프레임: 관측이 없다(지어내지 않음)
                rows.append((r["h"], r["dets"]))
        rows.sort(key=lambda x: x[0]["frame_sequence"])
        out[cam] = rows
    return out


class CachedFeed:
    """캐시 → 카메라별 Local Tracker → Observation2DFrame. `perception.FrameFeed` 를 따른다."""

    def __init__(self, cache: Mapping[str, list[tuple[dict, list]]], tracker: MultiCameraLocalTracker, cfg: Mapping[str, Any]):
        self._tracker = tracker
        self._cams = tuple(sorted(cache))
        infer = int(cfg.get("infer_latency_ms", 40))
        offsets = {c: int(v) for c, v in (cfg.get("camera_offset_ms") or {}).items()}
        extra = {c: int(v) for c, v in (cfg.get("extra_arrival_ms") or {}).items()}
        unc_override = cfg.get("uncertainty_ms")
        min_score = float(cfg.get("min_score", 0.0))
        events = []
        for cam, rows in cache.items():
            for h, dets in rows:
                cap = int(h["estimated_capture_time_ms"]) + offsets.get(cam, 0)
                recv = int(h.get("received_at_ms", h["estimated_capture_time_ms"]))
                arrive = max(recv, cap) + infer + extra.get(cam, 0)
                unc = h.get("timestamp_uncertainty_ms") if unc_override is None else unc_override
                pd = tuple(
                    PoseDetection(tuple(b), float(s), tuple(tuple(k) if k is not None else None for k in kps))
                    for b, s, kps in dets
                    if float(s) >= min_score
                )
                cd = CameraDetections(
                    camera_id=cam,
                    stream_epoch=int(h["stream_epoch"]),
                    frame_sequence=int(h["frame_sequence"]),
                    estimated_capture_time_ms=cap,
                    timestamp_source=h["timestamp_source"],
                    timestamp_uncertainty_ms=None if unc is None else float(unc),
                    detections=pd,
                )
                events.append((arrive, cam, int(h["frame_sequence"]), cd))
        events.sort(key=lambda e: (e[0], e[1], e[2]))
        self._events = events
        self._i = 0
        self.first_arrival = events[0][0]
        self.last_arrival = events[-1][0]
        self.capture_start = min(e[3].estimated_capture_time_ms for e in events)

    @property
    def source(self) -> str:
        return "FILE"

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self._cams

    @property
    def finished(self) -> bool:
        return self._i >= len(self._events)

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        batch = []
        while self._i < len(self._events) and self._events[self._i][0] <= now_ms:
            batch.append(self._events[self._i][3])
            self._i += 1
        obs = self._tracker.update_batch(batch) if batch else []
        obs.sort(key=lambda o: (o.estimated_capture_time_ms, o.camera_id, o.frame_sequence))
        return obs

    def camera_connected(self, camera_id: str) -> bool:
        return camera_id in self._cams

    def provenance(self) -> dict[str, Any]:
        return {"source": "FILE", "camera_ids": list(self._cams)}

    def close(self) -> None:
        return None


def _only(cls: Any, values: Mapping[str, Any] | None) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    values = dict(values or {})
    unknown = sorted(set(values) - names)
    if unknown:
        raise ValueError(f"{cls.__name__} 에 없는 설정: {unknown}")
    return {k: (tuple(v) if isinstance(v, list) else v) for k, v in values.items()}


# ----------------------------------------------------------------------------- 실행


BONES = {  # COCO17 관절 쌍과 성인 길이 범위(m). 사람 둘이 한 클러스터로 묶이면 크게 벗어난다(부작용 지표, 근사)
    "shoulder": ((5, 6), (0.20, 0.60)),
    "hip": ((11, 12), (0.12, 0.50)),
    "femur_l": ((11, 13), (0.25, 0.65)),
    "femur_r": ((12, 14), (0.25, 0.65)),
    "shin_l": ((13, 15), (0.25, 0.60)),
    "shin_r": ((14, 16), (0.25, 0.60)),
}


def _bone_flags(skeleton: Any) -> tuple[int, int]:
    """(잰 뼈 수, 범위를 벗어난 뼈 수)."""
    measured = bad = 0
    for (a, b), (lo, hi) in BONES.values():
        ja, jb = skeleton.joints[a].xyz, skeleton.joints[b].xyz
        if ja is None or jb is None:
            continue
        d = math.dist(ja, jb)
        measured += 1
        bad += not (lo <= d <= hi)
    return measured, bad


def run(cache: Mapping[str, list[tuple[dict, list]]], calibration: Path, cfg: Mapping[str, Any], out: Path | None) -> dict:
    if cfg.get("experimental"):
        raise ValueError("experimental 설정은 폐기했다. world.split_suppression_* 운영 설정을 쓴다")
    rig = CameraRig.from_provider(FileCalibrationProvider(calibration))
    tracker = MultiCameraLocalTracker(tracker_config(**(cfg.get("tracker") or {})), camera_ids=sorted(cache))
    feed = CachedFeed(cache, tracker, cfg.get("feed") or {})
    time_cfg = TimeModelConfig(**_only(TimeModelConfig, cfg.get("time")))
    world_cfg = WorldTrackConfig.from_time_model(time_cfg, **_only(WorldTrackConfig, cfg.get("world")))
    perception = Perception(
        rig, feed, session="replay413", scene_version="replay", input_provenance=feed.provenance(),
        time_config=time_cfg, world_config=world_cfg,
    )
    perception.associator = CrossViewAssociator(rig, CrossViewConfig(**_only(CrossViewConfig, cfg.get("cross_view"))))
    perception.buffer = LocalTrackletBuffer(InterpolationConfig(**_only(InterpolationConfig, cfg.get("interpolation"))))
    tick = int(cfg.get("tick_ms", 100))
    surfaces = SurfaceSet()
    now = feed.first_arrival
    end = feed.last_arrival + 1000
    rows = []
    review_events = []
    last_seen: dict[str, tuple[int, tuple]] = {}
    wall = time.monotonic()
    while now <= end:
        p = perception.step(now, surfaces)
        if p is not None:
            tr = []
            for s in p.frame.tracks:
                m = b = 0
                if s.updated and s.skeleton is not None:
                    m, b = _bone_flags(s.skeleton)
                if s.updated:
                    previous = last_seen.get(s.world_id)
                    kinds = []
                    if len(s.cameras) == 2:
                        kinds.append("two_view")
                    if previous and s.t_last_valid - previous[0] > world_cfg.occluded_after_ms:
                        kinds.append("reconnection")
                    for kind in kinds:
                        review_events.append({
                            "event_id": f"{kind}:{p.t}:{s.world_id}", "kind": kind, "t": p.t,
                            "world_id": s.world_id, "position_xy": list(s.position_xy),
                            "local_keys": [list(k) for k in s.local_keys],
                            "previous_local_keys": [list(k) for k in previous[1]] if previous else [],
                            "gap_ms": s.t_last_valid - previous[0] if previous else None,
                            "false_connection": None,
                        })
                    last_seen[s.world_id] = (s.t_last_valid, s.local_keys)
                tr.append([s.world_id, str(s.lifecycle)[0], int(s.updated), len(s.cameras),
                           round(s.position_xy[0], 3), round(s.position_xy[1], 3), str(s.identity_confidence)[0], m, b,
                           "".join(c[-1] for c in s.cameras)])
            rows.append({"t": p.t, "now": now, "lag": list(p.lagging), "tracks": tr})
        now += tick
    elapsed = time.monotonic() - wall
    metrics = compute_metrics(rows, cfg, feed.capture_start)
    labels = json.loads(Path(cfg["review_file"]).read_text(encoding="utf-8")) if cfg.get("review_file") else []
    metrics["association_review"] = review_metrics(review_events, labels)
    metrics["diagnostics"] = perception.diagnostics.summary()
    metrics["local_tracker"] = {c: {"issued_ids": s.issued_ids, "resets": s.resets, "frames": s.frames_processed}
                                for c, s in tracker.states().items()}
    metrics["run"] = {"elapsed_s": round(elapsed, 1), "ticks": len(rows), "config": cfg,
                      "calibration_version": rig.calibration_version}
    if out is not None:
        out.mkdir(parents=True, exist_ok=True)
        (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1), encoding="utf-8")
        with gzip.open(out / "tracks.jsonl.gz", "wt", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        with gzip.open(out / "association_review.jsonl.gz", "wt", encoding="utf-8") as fh:
            for event in review_events:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return metrics


# ----------------------------------------------------------------------------- 지표


def review_metrics(events: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """수동 확인한 오연결만 센다. 미확인은 0건으로 보고하지 않는다. labels 는 실험별 JSON 배열이다."""
    by_id = {e["event_id"]: e for e in events}
    reviewed = {}
    for label in labels:
        key = label["event_id"]
        if key not in by_id or key in reviewed or not isinstance(label["false_connection"], bool):
            raise ValueError(f"알 수 없거나 중복된 event_id, 또는 bool 이 아닌 판정: {key}")
        reviewed[key] = label["false_connection"]
    result = {}
    for kind in ("two_view", "reconnection"):
        ids = [key for key, event in by_id.items() if event["kind"] == kind]
        values = [reviewed[key] for key in ids if key in reviewed]
        result[kind] = {"candidates": len(ids), "reviewed": len(values),
                        "false_connections": sum(values) if values else None,
                        "reviewed_false_ratio": sum(values) / len(values) if values else None,
                        "coverage": len(values) / len(ids) if ids else None}
    return result


def _inside(x: float, y: float, poly: Sequence[Sequence[float]]) -> bool:
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def segment_metrics(rows: Sequence[Mapping[str, Any]], t0: int, t1: int, people: int,
                    first_seen: Mapping[str, int]) -> dict[str, Any]:
    """[t0, t1) 공통 시각 구간, 그 구간에 보이는 사람 수 people(녹화에서 센 값).

    - new_ids: 구간 안에서 처음 나타난 World ID 수(사람이 바뀌지 않으면 0 이 이상). per_person_min 은 사람·분당.
    - count_exact/over/under: 위치를 갱신한 Track 수가 people 과 같은·많은(한 사람 두 ID)·적은(놓침·합쳐짐) tick 비율.
    - id_changes: tick 마다 갱신된 Track 의 ID 집합이 바뀐 횟수 중 개수는 그대로인 것(ID 교체).
    """
    ticks = [r for r in rows if t0 <= r["t"] < t1]
    if not ticks:
        return {"ticks": 0}
    exact = over = under = 0
    cams = Counter()
    changes = 0
    prev: frozenset[str] | None = None
    for r in ticks:
        upd = [tr for tr in r["tracks"] if tr[2]]
        for tr in upd:
            cams[tr[3]] += 1
        n = len(upd)
        exact += n == people
        over += n > people
        under += n < people
        ids = frozenset(tr[0] for tr in upd)
        if prev is not None and n == people and len(prev) == people and ids != prev:
            changes += 1
        if n == people:
            prev = ids
    new_ids = sum(1 for w, t in first_seen.items() if t0 <= t < t1)
    minutes = (t1 - t0) / 60000
    k = len(ticks)
    return {
        "ticks": k,
        "people": people,
        "new_ids": new_ids,
        "new_ids_per_person_min": round(new_ids / (people * minutes), 2) if people and minutes else None,
        "count_exact": round(exact / k, 3),
        "count_over": round(over / k, 3),
        "count_under": round(under / k, 3),
        "id_changes": changes,
        "updated_cameras": dict(sorted(cams.items())),
    }


def compute_metrics(rows: Sequence[Mapping[str, Any]], cfg: Mapping[str, Any], capture_start: int) -> dict[str, Any]:
    first: dict[str, int] = {}
    last: dict[str, int] = {}
    confirmed: set[str] = set()
    cams = Counter()
    bones_measured = bones_bad = tracks_bad = tracks_measured = 0
    zone = cfg.get("zone_xy", DEFAULT_ZONE)  # 다른 World 좌표계에서는 [] 로 구역 근사를 끈다
    inside_ticks: dict[str, int] = defaultdict(int)
    occupied = []
    for r in rows:
        any_in = False
        for wid, lc, upd, ncam, x, y, _conf, m, b, _c in r["tracks"]:
            first.setdefault(wid, r["t"])
            last[wid] = r["t"]
            if lc in ("C", "O"):
                confirmed.add(wid)
            if upd:
                cams[ncam] += 1
                if m:
                    tracks_measured += 1
                    bones_measured += m
                    bones_bad += b
                    tracks_bad += b > 0
                if _inside(x, y, zone):
                    inside_ticks[wid] += 1
                    any_in = True
        occupied.append((r["t"], any_in))
    span_min = (rows[-1]["t"] - rows[0]["t"]) / 60000 if len(rows) > 1 else 0.0
    life = [(last[w] - first[w]) / 1000 for w in first]
    tick_ms = int(cfg.get("tick_ms", 100))
    dwell = int(cfg.get("zone_dwell_ms", 500)) // tick_ms
    entries = sum(1 for n in inside_ticks.values() if n >= dwell)
    episodes, run_in, gap = 0, 0, 10**9
    for _t, inside in occupied:  # 1 s 넘게 빈 뒤 다시 찬 것만 새 진입으로 본다
        if inside:
            if gap * tick_ms > 1000 and run_in == 0:
                episodes += 1
            run_in += 1
            gap = 0
        else:
            gap += 1
            if gap * tick_ms > 1000:
                run_in = 0
    total_upd = sum(cams.values())
    segments = {}
    for name, (s0, s1, people) in (cfg.get("segments_s") or {}).items():
        segments[name] = segment_metrics(rows, capture_start + int(s0 * 1000), capture_start + int(s1 * 1000),
                                         int(people), first)
    return {
        "span_min": round(span_min, 2),
        "ids_total": len(first),
        "ids_per_min": round(len(first) / span_min, 2) if span_min else None,
        "ids_confirmed": len(confirmed),
        "lifespan_s": {"p50": _pct(life, 0.5), "p90": _pct(life, 0.9), "max": max(life) if life else None,
                       "under_6s_ratio": round(sum(1 for v in life if v < 6) / len(life), 3) if life else None},
        "updated_cameras_ratio": {str(k): round(v / total_upd, 3) for k, v in sorted(cams.items())} if total_upd else {},
        "updated_cameras_mean": round(sum(k * v for k, v in cams.items()) / total_upd, 2) if total_upd else None,
        "updated_track_ticks": total_upd,
        "bones": {"tracks_measured": tracks_measured, "tracks_with_bad_bone_ratio":
                  round(tracks_bad / tracks_measured, 4) if tracks_measured else None,
                  "bad_bone_ratio": round(bones_bad / bones_measured, 4) if bones_measured else None},
        "zone_proxy": {"id_entries": entries if zone else None, "occupancy_episodes": episodes if zone else None,
                       "duplicates": entries - episodes if zone else None},
        "segments": segments,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--calibration", required=True, type=Path)
    ap.add_argument("--config", type=Path, help="설정 JSON (없으면 운영 기본값)")
    ap.add_argument("--set", action="append", default=[], help="설정 한 값 덮기: 경로=JSON값 (예: cross_view.residual_gate_px=16)")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--cameras", nargs="*")
    args = ap.parse_args(argv)
    cfg: dict[str, Any] = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    for item in args.set:
        path, _, raw = item.partition("=")
        node = cfg
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = json.loads(raw)
    cache = load_cache(args.cache, args.cameras)
    m = run(cache, args.calibration, cfg, args.out)
    brief = {k: m[k] for k in ("ids_total", "ids_per_min", "ids_confirmed", "lifespan_s", "updated_cameras_ratio",
                                "zone_proxy", "segments")}
    print(json.dumps(brief, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
