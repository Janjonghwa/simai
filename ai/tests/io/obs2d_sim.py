"""시뮬레이터 출력으로 2D 관측 Parquet 기록기를 돌리고 용량을 잰다 (docs/design/tier0-obs2d-parquet.md 의 표).

ai/ 에서 실행한다. pyarrow(dev 그룹 또는 extras io)가 필요하다.

    uv run python tests/io/obs2d_sim.py            # 용량 표 (Markdown)
    uv run python tests/io/obs2d_sim.py --json     # JSON

- 시뮬레이터 `observations/` 의 카메라별 프레임을 촬영 시각 순서로 합치고 카메라마다 한 장씩 묶어
  Local Tracker 출력(Observation2DFrame)으로 만든 뒤 `observation_batch_payload` 로 계약 형식 payload 를 만든다.
  카메라 내 트랙 ID 는 시뮬레이터가 붙인 것을 그대로 쓴다(추적기를 돌리지 않음, 기록기 시험이 목적).
  score 는 보인 관절(conf > 0) 신뢰도 평균이다.
- 용량은 표본 구간(기본 60초, 사람 10명, 카메라 4대)을 실제로 기록한 Parquet 크기에서 1시간·7일·14일로 늘려 계산한다.
  사람 수·관절 신뢰도 분포가 실제와 다르므로 대략값이다. SIM 입력이다(`input_provenance.is_upper_bound = true`).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from simai_ai.io import SimObservationFrameSource
from simai_ai.io.obs2d_layout import HOUR_MS, Obs2DStoreConfig, list_files
from simai_ai.io.obs2d_parquet import Obs2DParquetWriter
from simai_ai.providers.provenance import provenance_from_providers
from simai_ai.tracking.local_tracker import (
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
    observation_batch_payload,
)

AI_DIR = Path(__file__).resolve().parents[2]
BASIC = AI_DIR / "sim" / "scenarios" / "basic.json"
HOUR_BOUNDARY_MS = 1760342400000  # 2025-10-13T08:00:00Z, 정시
CALIBRATION_VERSION = "cal-sim-1"


def scenario(
    *, name: str, start_time_ms: int, duration_ms: int, fps: int, people: int, seed: int = 46162
) -> dict[str, Any]:
    """basic 시나리오(교실·카메라 4대)에 길이·fps·사람 수를 바꾼 시나리오. 사람은 제자리 근처를 원으로 돈다."""
    sc = json.loads(BASIC.read_text(encoding="utf-8"))
    sc.update(name=name, seed=seed, start_time_ms=start_time_ms, duration_ms=duration_ms)
    sc["camera_defaults"]["fps"] = fps
    sc["people"] = []
    step = 2000
    for i in range(people):
        cx, cy = 1.5 + (i % 5) * 1.5, 2.0 + (i // 5 % 2) * 3.0
        sc["people"].append(
            {
                "id": f"p{i}",
                "height_m": 1.60 + 0.02 * (i % 6),
                "waypoints": [
                    {"t_ms": t, "xy": [round(cx + 0.6 * math.cos(t / 5000 + i), 3), round(cy + 0.6 * math.sin(t / 5000 + i), 3)]}
                    for t in range(0, duration_ms + step, step)
                ],
            }
        )
    return sc


def run_sim(sc: dict[str, Any], workdir: Path) -> Path:
    path = workdir / f"{sc['name']}.json"
    path.write_text(json.dumps(sc, ensure_ascii=False), encoding="utf-8")
    out = workdir / f"{sc['name']}-out"
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(path), "--out", str(out)],
        check=False,
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"시뮬레이터 실패: {result.stderr}")
    return out


def _score(keypoints: Sequence[Any]) -> float:
    confs = [kp[2] for kp in keypoints if kp is not None and kp[2] > 0]
    return sum(confs) / len(confs) if confs else 0.0


def observation_frames(out: Path) -> list[Observation2DFrame]:
    """시뮬레이터 출력 → 촬영 시각 순서의 Observation2DFrame (Local Tracker 출력 형식)."""
    source = SimObservationFrameSource(out)
    frames = []
    for f in source.frames():
        dets = tuple(
            TrackedDetection(d.local_track_id, PoseDetection(d.bbox, _score(d.keypoints_2d), d.keypoints_2d))
            for d in f.detections or ()
        )
        frames.append(
            Observation2DFrame(
                camera_id=f.camera_id,
                stream_epoch=f.frame_ref["stream_epoch"],
                frame_sequence=f.frame_ref["frame_sequence"],
                estimated_capture_time_ms=f.estimated_capture_time,
                timestamp_source=f.timestamp_source,
                timestamp_uncertainty_ms=f.timestamp_uncertainty_ms,
                detections=dets,
            )
        )
    return frames


def batches(frames: Sequence[Observation2DFrame]) -> Iterator[list[Observation2DFrame]]:
    """촬영 시각 순서의 프레임을 카메라마다 한 장씩 묶는다 (묶음 추론·observation_batch 흉내)."""
    batch: list[Observation2DFrame] = []
    for f in frames:
        if any(o.camera_id == f.camera_id for o in batch):
            yield batch
            batch = []
        batch.append(f)
    if batch:
        yield batch


def payloads(frames: Sequence[Observation2DFrame]) -> Iterator[dict[str, Any]]:
    for b in batches(frames):
        yield observation_batch_payload(b, calibration_version=CALIBRATION_VERSION)


def provenance(out: Path) -> dict[str, Any]:
    return provenance_from_providers(SimObservationFrameSource(out))


def measure(out: Path, root: Path, store_max_hz: float, session_id: str) -> dict[str, Any]:
    """표본을 기록하고 크기를 잰다."""
    frames = observation_frames(out)
    config = Obs2DStoreConfig(root=root, store_max_hz=store_max_hz)
    with Obs2DParquetWriter(config, session_id=session_id, input_provenance=provenance(out)) as writer:
        for p in payloads(frames):
            writer.write_batch(p)
    stats = writer.stats
    files = list_files(root, session_ids=[session_id])
    t0, t1 = frames[0].estimated_capture_time_ms, frames[-1].estimated_capture_time_ms
    span_ms = t1 - t0
    total = sum(f.size_bytes for f in files)
    per_hour = total * HOUR_MS / span_ms
    cams = sorted({f.camera_id for f in frames})
    stored_hz = stats.frames_written / len(cams) / (span_ms / 1000)
    jsonl = sum((out / "observations" / f"{c}.jsonl").stat().st_size for c in cams)
    return {
        "store_max_hz": store_max_hz,
        "stored_hz_per_camera": round(stored_hz, 2),
        "cameras": len(cams),
        "span_s": round(span_ms / 1000, 2),
        "frames_written": stats.frames_written,
        "rows_written": stats.rows_written,
        "files": len(files),
        "parquet_bytes": total,
        "bytes_per_row": round(total / stats.rows_written, 1),
        "sim_jsonl_bytes_all_frames": jsonl,
        "per_hour_mb": round(per_hour / 1e6, 1),
        "per_8h_day_gb": round(per_hour * 8 / 1e9, 2),
        "days7_8h_gb": round(per_hour * 8 * 7 / 1e9, 1),
        "days14_8h_gb": round(per_hour * 8 * 14 / 1e9, 1),
        "days7_24h_gb": round(per_hour * 24 * 7 / 1e9, 1),
        "days14_24h_gb": round(per_hour * 24 * 14 / 1e9, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--people", type=int, default=10, help="표본 사람 수 (명세 11.4 용량 기준 10명)")
    parser.add_argument("--seconds", type=int, default=60, help="표본 길이")
    parser.add_argument("--fps", type=int, default=25, help="카메라 fps (Tapo C110 기본 25)")
    args = parser.parse_args()
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        sc = scenario(
            name="obs2d-capacity",
            start_time_ms=HOUR_BOUNDARY_MS,
            duration_ms=args.seconds * 1000,
            fps=args.fps,
            people=args.people,
        )
        out = run_sim(sc, Path(tmp))
        for hz in (15.0, 5.0):
            r = measure(out, Path(tmp) / f"store-{hz:g}", hz, f"cap{hz:g}")
            r.update(people=args.people, camera_fps=args.fps)
            results.append(r)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0
    print(f"표본: 카메라 4대 {args.fps} fps, 사람 {args.people}명, {args.seconds}초 (SIM, is_upper_bound = true)\n")
    print("| 저장 상한 | 저장 빈도(카메라당) | 행 | 바이트/행 | 1시간 | 하루 8시간 | 7일(8h/일) | 14일(8h/일) | 7일(24h) | 14일(24h) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in results:
        print(
            f"| {r['store_max_hz']:g} Hz | {r['stored_hz_per_camera']} Hz | {r['rows_written']} | {r['bytes_per_row']} "
            f"| {r['per_hour_mb']} MB | {r['per_8h_day_gb']} GB | {r['days7_8h_gb']} GB | {r['days14_8h_gb']} GB "
            f"| {r['days7_24h_gb']} GB | {r['days14_24h_gb']} GB |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
