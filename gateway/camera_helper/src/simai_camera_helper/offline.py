"""Bounded-memory keyframe replay and CPU measurement CLI.

Raw H.264 contains no UTC capture timestamps. Messages use an explicitly labelled,
frozen batch replay clock, not the recording's original capture times. Never upload
these offline messages as LIVE coverage guides.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import platform
import subprocess
import sys
import threading
import time
from contextlib import ExitStack
from pathlib import Path

from .judge import CAMERAS, CoverageJudge, Params
from .message import CameraFrame, build_message

DEFAULT_RECORDING = Path("C:/Users/SSAFY/simai-local/session11/rec-calwalk")
DEFAULT_INTRINSICS = Path("C:/Users/SSAFY/simai-local/session11/lobby-bundle-rotfix/calibration.json")
DEFAULT_WEIGHTS = Path("C:/Users/SSAFY/.cache/torch/hub/checkpoints")


def selected_keyframes(path: Path, *, stride: int = 1, start: int = 0):
    """Decode I frames only, keep no history, convert only selected frames to BGR."""
    import av

    if stride < 1 or start < 0:
        raise ValueError("stride must be positive and start nonnegative")
    with av.open(str(path), format="h264") as container:
        stream = container.streams.video[0]
        stream.codec_context.skip_frame = "NONKEY"
        # Keep decoder threading bounded as well as the number of retained images.
        stream.codec_context.thread_count = 1
        for index, frame in enumerate(container.decode(stream)):
            if frame.key_frame and index >= start and (index - start) % stride == 0:
                yield frame.to_ndarray(format="bgr24")


class PeakMemory:
    """Process RSS sampled every 20 ms, plus OS lifetime peak when available (Windows/Linux)."""

    def __init__(self):
        self.peak = 0
        self._stop = threading.Event()

    def __enter__(self):
        import psutil

        self._process = psutil.Process()
        self._sample()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._thread.start()
        return self

    def _sample(self):
        memory = self._process.memory_info()
        self.peak = max(self.peak, memory.rss, getattr(memory, "peak_wset", 0))

    def _watch(self):
        while not self._stop.wait(0.02):
            self._sample()

    def __exit__(self, *_):
        self._stop.set()
        self._thread.join()
        self._sample()
        if sys.platform != "win32":
            import resource

            scale = 1 if sys.platform == "darwin" else 1024
            self.peak = max(self.peak, resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale)


def percentile(values: list[float], q: float) -> float:
    """Linear interpolation, matching numpy.percentile's default, without importing numpy."""
    if not values:
        raise ValueError("cannot summarize an empty timing sample")
    ordered = sorted(values)
    index = (len(ordered) - 1) * q
    lo = int(index)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (index - lo)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def run_record(args, started_at: int, status: str, metrics: dict, failure: str | None = None) -> dict:
    root = Path(__file__).resolve().parents[4]

    def git(*parts):
        return subprocess.check_output(["git", "-C", str(root), *parts], text=True,
                                       stderr=subprocess.DEVNULL).strip()

    report = args.out.resolve() / "summary.json"
    result = {
        "schema_version": "2.2.0", "run_id": f"camera-helper-{started_at}-w{args.width}-k{args.keypoints}",
        "experiment_id": "VAL-01",
        "jira_key": "S15P31A101-445", "owner": "S15P31A101-445 Owner (Lead 확인 필요)",
        "git_commit": git("rev-parse", "HEAD"), "branch": git("branch", "--show-current"),
        "dirty_worktree": True, "env_hash": "", "model_version": "ALIKED-n16+LightGlue-5a650c4",
        "config": {"path": "gateway/camera_helper/pyproject.toml",
                   "sha256": digest(root / "gateway/camera_helper/pyproject.toml"),
                   "overrides": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()}},
        "input_provenance": {"frames": {"source": "FILE", "camera_ids": list(CAMERAS),
                                        "dataset_id": "session11/rec-calwalk"}, "is_upper_bound": False},
        "gpu": {"model": "NONE (CPU only)", "driver": "N/A", "cuda": "disabled", "provider": "LOCAL",
                "shared_with_tier0": False},
        "started_at": started_at, "finished_at": time.time_ns() // 1_000_000,
        "status": status, "experiment_state": "BENCHMARK", "metrics": metrics,
        "results": [{"kind": "REPORT", "uri": report.as_uri(), "sha256": digest(report)}],
        "notes": "No Docker image digest available; env_hash empty. Uncommitted work; not review evidence. "
                 "Only K/distortion read. No extrinsics. Offline synthetic batch clock. "
                 "Run is a CPU helper tool check, not a product calibration accuracy metric.",
    }
    if failure:
        result["failure_reason"] = failure
    return result


def run(args) -> int:
    if (args.out / "run_record.json").exists() or (args.out / "summary.json").exists():
        raise FileExistsError(f"run output already exists; use a new --out directory: {args.out}")
    args.out.mkdir(parents=True, exist_ok=True)
    started_at = time.time_ns() // 1_000_000
    summary = {"image_width_px": args.width, "max_keypoints": args.keypoints,
               "requested_rounds": args.rounds, "warmup_rounds": args.warmup, "cpu_threads": args.threads,
               "device": args.device, "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
               "platform": platform.platform(), "processor": platform.processor(),
               "logical_cpus": os.cpu_count(), "status": "FAILED"}
    metrics = {}
    try:
        # Imported lazily: --help and failure recording work even in an uninstalled environment.
        from .matcher import CameraMatcher, load_intrinsics

        with PeakMemory() as memory:
            init_start = time.perf_counter()
            intrinsics = load_intrinsics(args.intrinsics) if args.intrinsics else None
            params = Params(image_width_px=args.width, max_keypoints=args.keypoints,
                            intrinsics_source="STORED" if intrinsics is not None else "DEFAULT",
                            ransac_threshold_px=2.0 if intrinsics is not None else 3.0)
            matcher = CameraMatcher(args.aliked_weights, args.lightglue_weights, params, intrinsics,
                                    cpu_threads=args.threads, device=args.device)
            summary["device_used"] = matcher.device
            summary["model_init_ms"] = (time.perf_counter() - init_start) * 1000
            summary["weights_sha256"] = {"aliked": digest(args.aliked_weights),
                                         "lightglue": digest(args.lightglue_weights)}
            import av
            import cv2
            import numpy as np
            import torch

            summary["versions"] = {"torch": torch.__version__, "av": av.__version__,
                                   "numpy": np.__version__, "opencv": cv2.__version__}
            judge = CoverageJudge(params)
            round_times, total_times, grade_counts = [], [], {}
            with ExitStack() as stack, (args.out / "rounds.jsonl").open("w", encoding="utf-8") as log:
                streams = {}
                for cid in CAMERAS:
                    generator = selected_keyframes(args.recording / cid / "epoch_0000.h264",
                                                   stride=args.stride, start=args.start)
                    stack.callback(generator.close)
                    streams[cid] = generator
                for index in range(args.rounds + args.warmup):
                    total_start = time.perf_counter()
                    # Advancing a generator releases its previous frame; only this four-image batch is kept.
                    images = {cid: next(streams[cid], None) for cid in CAMERAS}
                    if any(image is None for image in images.values()):
                        raise ValueError(f"not enough keyframes for requested rounds (stopped at {index})")
                    # A frozen offline clock makes round results independent of inference runtime.
                    replay_clock = started_at + index * 2000
                    compute_start = time.perf_counter()
                    pairs = matcher.match(images)
                    message = build_message(args.source_id, replay_clock,
                                            [CameraFrame(cid, replay_clock) for cid in CAMERAS], pairs, judge)
                    elapsed = (time.perf_counter() - compute_start) * 1000
                    total_elapsed = (time.perf_counter() - total_start) * 1000
                    warmup = index < args.warmup
                    if not warmup:
                        round_times.append(elapsed)
                        total_times.append(total_elapsed)
                        for p in message["pairs"]:
                            key = f"{p['a']}-{p['b']}:{p['grade']}"
                            grade_counts[key] = grade_counts.get(key, 0) + 1
                    record = {"round": index, "warmup": warmup, "round_ms": elapsed,
                              "decode_and_round_ms": total_elapsed,
                              "timestamp_basis": "SYNTHETIC_FROZEN_BATCH_REPLAY_NOT_ORIGINAL_CAPTURE",
                              "recorded_capture_skew_ms": None, "message": message}
                    log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                    log.flush()
                    print(json.dumps(record, ensure_ascii=False, allow_nan=False), flush=True)
                    del images
            metrics = {"round_p50_ms": percentile(round_times, 0.5),
                       "round_p95_ms": percentile(round_times, 0.95), "rounds": len(round_times),
                       "decode_and_round_p50_ms": percentile(total_times, 0.5),
                       "decode_and_round_p95_ms": percentile(total_times, 0.95)}
            summary.update(metrics, pair_grade_counts=grade_counts, status="SUCCEEDED")
        metrics["peak_ram_mib"] = memory.peak / 1024**2
        summary["peak_ram_mib"] = metrics["peak_ram_mib"]
    except Exception as exc:  # noqa: BLE001
        summary["failure_reason"] = f"{type(exc).__name__}: {exc}"
        print(summary["failure_reason"], file=sys.stderr, flush=True)
    write_json(args.out / "summary.json", summary)
    write_json(args.out / "run_record.json",
               run_record(args, started_at, summary["status"], metrics, summary.get("failure_reason")))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0 if summary["status"] == "SUCCEEDED" else 1


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--recording", type=Path, default=DEFAULT_RECORDING)
    ap.add_argument("--intrinsics", type=Path, default=DEFAULT_INTRINSICS)
    ap.add_argument("--default-intrinsics", action="store_true", help="90-degree FOV approximation")
    ap.add_argument("--aliked-weights", type=Path, default=DEFAULT_WEIGHTS / "aliked-n16.pth")
    ap.add_argument("--lightglue-weights", type=Path, default=DEFAULT_WEIGHTS / "aliked_lightglue_v0-1_arxiv.pth")
    ap.add_argument("--source-id", default="gw-lobby")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--keypoints", type=int, default=1024)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--warmup", type=int, default=1)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto")
    ap.add_argument("--stride", type=int, default=1, help="keep every Nth keyframe")
    ap.add_argument("--start", type=int, default=0, help="first keyframe ordinal")
    ap.add_argument("--matrix", action="store_true", help="960/1024 px x 1024/2048 points, fresh child processes")
    return ap


def main() -> None:
    # CPU mode is enforced both by hiding CUDA and by explicit .to('cpu') in CameraMatcher.
    ap = parser()
    args = ap.parse_args()
    if args.device == "cpu":
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    if min(args.rounds, args.threads, args.stride, args.width, args.keypoints) < 1:
        ap.error("rounds/threads/stride/width/keypoints must be positive")
    if args.warmup < 0 or args.start < 0:
        ap.error("warmup/start must be nonnegative")
    if args.default_intrinsics:
        args.intrinsics = None
    if not args.matrix:
        raise SystemExit(run(args))
    if args.out.exists() and any(args.out.iterdir()):
        ap.error(f"matrix output must be a new or empty directory: {args.out}")
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    # Sequential, one fresh process per configuration; never four simultaneous model copies.
    for width, keypoints in itertools.product((960, 1024), (1024, 2048)):
        out = args.out / f"w{width}-k{keypoints}"
        cmd = [sys.executable, "-m", "simai_camera_helper.offline", "--out", str(out)]
        values = vars(args) | {"width": width, "keypoints": keypoints}
        for key in ("recording", "intrinsics", "aliked_weights", "lightglue_weights", "source_id",
                    "width", "keypoints", "rounds", "warmup", "threads", "device", "stride", "start"):
            if values[key] is not None:
                cmd.extend(["--" + key.replace("_", "-"), str(values[key])])
        if args.default_intrinsics:
            cmd.append("--default-intrinsics")
        completed = subprocess.run(cmd, check=False)
        if (out / "summary.json").is_file():
            rows.append(json.loads((out / "summary.json").read_text(encoding="utf-8")))
        else:
            rows.append({"image_width_px": width, "max_keypoints": keypoints,
                         "status": "FAILED", "failure_reason": f"child exit {completed.returncode}"})
    write_json(args.out / "benchmark.json", rows)
    raise SystemExit(0 if all(row["status"] == "SUCCEEDED" for row in rows) else 1)


if __name__ == "__main__":
    main()
