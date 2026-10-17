"""시뮬레이터 고정 시나리오로 시간 보간·삼각측량 관절 오차를 잰다 (docs/design/tier0-triangulation.md 의 표).

ai/ 에서 실행한다. 시뮬레이터는 `python -m sim.run` 으로 돌린다(tests/sim 과 같은 방식).

    uv run python tests/geometry/sim_accuracy.py            # 표 (Markdown)
    uv run python tests/geometry/sim_accuracy.py --json     # 시험(test_sim_accuracy.py)이 읽는 JSON
    uv run python tests/geometry/sim_accuracy.py --baseline # 보간 없이 가장 가까운 프레임을 쓴 비교 행 추가

- 입력은 시뮬레이터 출력의 `observations/` 와 `calibration.json` 만 쓴다. `truth/` 는 오차 계산에만 쓴다.
- 카메라 간 연결(같은 사람 묶기)은 이 티켓 범위가 아니어서 시뮬레이터 정답 연결(world_tracks.jsonl 의 per_camera)을 쓴다.
- 결과는 SIM 입력의 상한 성능이다(`input_provenance.is_upper_bound = true`). 제품 성능으로 쓰지 않는다.

변형:
- clean: 픽셀 잡음 σ=0, 촬영 지터 0, 보고 시각 오차 0. 카메라별 촬영 시각 오프셋(0·7·13·21 ms)은 그대로다.
- noisy: 내장 시나리오 그대로(픽셀 σ=1 px, 촬영 지터 ±3 ms, crossing 은 보고 시각 오차 ±4 ms).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.geometry.interpolation import (
    InterpolatedView,
    InterpolationConfig,
    KeypointSample,
    interpolate_views,
)
from simai_ai.geometry.rig import CameraRig
from simai_ai.geometry.triangulation import JointValidity, TriangulationConfig, triangulate_skeleton
from simai_ai.providers.files import FileCalibrationProvider
from simai_ai.providers.provenance import build_input_provenance

AI_DIR = Path(__file__).resolve().parents[2]
SCENARIOS = ("basic", "podium", "crossing")
VARIANTS = ("clean", "noisy")
# 시뮬레이터 사람은 경유점에서 속도·방향·걸음 흔들림이, 단상을 오르내릴 때 높이(0.2 m)가 한 순간에 바뀐다
# (실제 사람과 달리 불연속). 그 시각 ± 이 값 안의 tick 은 '불연속 경계'로 따로 센다 (World tick 한 칸).
DISCONTINUITY_MARGIN_MS = 100


def scenario_doc(name: str, variant: str) -> dict[str, Any]:
    doc = json.loads((AI_DIR / "sim" / "scenarios" / f"{name}.json").read_text(encoding="utf-8"))
    if variant == "clean":
        doc.setdefault("keypoints", {})["pixel_noise_sigma_px"] = 0.0
        for cam in [doc.setdefault("camera_defaults", {}), *doc["cameras"]]:
            for key in ("capture_jitter_ms", "timestamp_error_ms"):
                if key in cam or cam is doc["camera_defaults"]:
                    cam[key] = 0
    elif variant != "noisy":
        raise ValueError(f"모르는 변형: {variant}")
    return doc


def run_sim(doc: dict[str, Any], workdir: Path) -> Path:
    scenario_path = workdir / f"{doc['name']}.json"
    scenario_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = workdir / "out"
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(scenario_path), "--out", str(out)],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    if result.returncode != 0:
        raise RuntimeError(f"시뮬레이터 실패: {result.stderr}")
    return out


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_samples(out: Path) -> dict[tuple[str, str], list[KeypointSample]]:
    """(camera_id, local_track_id) → 프레임 순서의 KeypointSample."""
    samples: dict[tuple[str, str], list[KeypointSample]] = defaultdict(list)
    for path in sorted((out / "observations").glob("*.jsonl")):
        for row in _jsonl(path):
            for det in row["detections"]:
                samples[(row["camera_id"], det["local_track_id"])].append(
                    KeypointSample(
                        camera_id=row["camera_id"],
                        estimated_capture_time=row["estimated_capture_time_ms"],
                        timestamp_source=row["timestamp_source"],
                        timestamp_uncertainty_ms=row["timestamp_uncertainty_ms"],
                        keypoints=tuple(tuple(kp) if kp is not None else None for kp in det["keypoints_2d"]),
                        frame_ref={"stream_epoch": row["stream_epoch"], "frame_sequence": row["frame_sequence"]},
                    )
                )
    return samples


def load_association(out: Path) -> dict[str, dict[str, str]]:
    """시뮬레이터 정답 연결: world_id → {camera_id: local_track_id}."""
    assoc: dict[str, dict[str, str]] = defaultdict(dict)
    for doc in _jsonl(out / "world_tracks.jsonl"):
        for tr in doc["tracks"]:
            for obs in tr["per_camera"]:
                assoc[tr["world_id"]][obs["camera_id"]] = obs["local_track_id"]
    return assoc


def _pct(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def nearest_views(
    by_camera: dict[str, list[KeypointSample]], t: int, config: InterpolationConfig
) -> tuple[dict[str, InterpolatedView], list]:
    """비교 기준: 보간하지 않고 카메라마다 촬영 시각이 t 에 가장 가까운 프레임을 그대로 쓴다."""
    views = {}
    for cid, samples in by_camera.items():
        ok = [
            s
            for s in samples
            if s.timestamp_uncertainty_ms is not None
            and s.timestamp_uncertainty_ms <= config.max_timestamp_uncertainty_ms
            and abs(s.estimated_capture_time - t) <= config.window_ms
        ]
        if ok:
            s = min(ok, key=lambda s: abs(s.estimated_capture_time - t))
            views[cid] = InterpolatedView(
                cid, t, s.keypoints, s.timestamp_source, s.timestamp_uncertainty_ms, (s.estimated_capture_time,)
            )
    return views, []


def load_discontinuities(out: Path, doc: dict[str, Any]) -> dict[str, list[int]]:
    """world_id → 움직임이 불연속인 절대 시각(UTC ms): 경유점 시각과 지지 표면이 바뀐 tick.

    manifest 의 people 로 시나리오 사람 ID 와 world_id 를 잇는다.
    """
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    world_ids = {p["person_id"]: p["world_id"] for p in manifest["people"]}
    times: dict[str, list[int]] = {
        world_ids[person["id"]]: [doc["start_time_ms"] + w["t_ms"] for w in person["waypoints"]]
        for person in doc["people"]
    }
    previous: dict[str, str] = {}
    for row in _jsonl(out / "truth" / "world_truth.jsonl"):
        for person in row["people"]:
            wid, surface = person["world_id"], person["support_surface_id"]
            if wid in previous and previous[wid] != surface:
                times[wid].append(row["t"])
            previous[wid] = surface
    return times


def evaluate(out: Path, discontinuities: dict[str, list[int]], interpolate: bool = True) -> dict[str, Any]:
    rig = CameraRig.from_provider(FileCalibrationProvider(out / "calibration.json"))
    samples = load_samples(out)
    assoc = load_association(out)
    interp_config, tri_config = InterpolationConfig(), TriangulationConfig()

    errors: list[float] = []
    valid_errors: list[float] = []
    valid_errors_off_boundary: list[float] = []
    z_errors: list[float] = []
    pelvis_xy: list[float] = []
    validity = Counter()
    drops = Counter()
    fabricated = 0
    total_joints = 0
    for truth in _jsonl(out / "truth" / "world_truth.jsonl"):
        t = truth["t"]
        for person in truth["people"]:
            total_joints += len(person["joints"])
            tracks = assoc.get(person["world_id"], {})
            by_camera = {cid: samples[(cid, lid)] for cid, lid in tracks.items()}
            align = interpolate_views if interpolate else nearest_views
            views, frame_drops = align(by_camera, t, interp_config)
            drops.update(str(d.reason) for d in frame_drops)
            if not views:
                validity[str(JointValidity.UNAVAILABLE)] += len(person["joints"])
                continue
            sk = triangulate_skeleton(views, rig, "COCO17", tri_config)
            for est, true_xyz in zip(sk.joints, person["joints"]):
                validity[str(est.validity)] += 1
                if est.xyz is None:
                    continue
                if len(est.camera_ids) < 2:
                    fabricated += 1
                diff = np.subtract(est.xyz, true_xyz)
                err = float(np.linalg.norm(diff))
                errors.append(err)
                z_errors.append(abs(float(diff[2])))
                if est.validity == JointValidity.VALID:
                    valid_errors.append(err)
                    if all(abs(t - d) > DISCONTINUITY_MARGIN_MS for d in discontinuities[person["world_id"]]):
                        valid_errors_off_boundary.append(err)
            pelvis = sk.pelvis_xyz()
            if pelvis is not None:
                pelvis_xy.append(float(np.linalg.norm(np.subtract(pelvis[:2], person["pelvis_xyz"][:2]))))

    estimated = len(errors)
    provenance = build_input_provenance(
        {"source": "SIM", "camera_ids": sorted(rig.cameras)}, calibration=rig.calibration_provenance
    )
    return {
        "input_provenance": provenance,
        "joints_total": total_joints,
        "joints_estimated": estimated,
        "estimated_ratio": estimated / total_joints if total_joints else 0.0,
        "validity": dict(sorted(validity.items())),
        "fabricated_joints": fabricated,
        "joint_error_m": {
            "median": statistics.median(errors) if errors else None,
            "p95": _pct(errors, 95),
            "max": max(errors) if errors else None,
        },
        "valid_joint_error_m": {
            "median": statistics.median(valid_errors) if valid_errors else None,
            "p95": _pct(valid_errors, 95),
            "max": max(valid_errors) if valid_errors else None,
            "max_smooth": max(valid_errors_off_boundary) if valid_errors_off_boundary else None,
        },
        "joint_z_error_m": {"p95": _pct(z_errors, 95)},
        "pelvis_xy_error_m": {
            "median": statistics.median(pelvis_xy) if pelvis_xy else None,
            "p95": _pct(pelvis_xy, 95),
        },
        "frame_drops": dict(sorted(drops.items())),
    }


def run_all(scenarios=SCENARIOS, variants=VARIANTS, baseline: bool = False) -> dict[str, dict[str, Any]]:
    """baseline=True 면 같은 입력을 보간 없이(가장 가까운 프레임) 푼 결과를 `<키>/nearest` 로 함께 낸다."""
    results: dict[str, dict[str, Any]] = {}
    for name in scenarios:
        for variant in variants:
            doc = scenario_doc(name, variant)
            with tempfile.TemporaryDirectory() as tmp:
                out = run_sim(doc, Path(tmp))
                jumps = load_discontinuities(out, doc)
                results[f"{name}/{variant}"] = evaluate(out, jumps)
                if baseline:
                    results[f"{name}/{variant}/nearest"] = evaluate(out, jumps, interpolate=False)
    return results


def _cm(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.2f}"


def markdown(results: dict[str, dict[str, Any]]) -> str:
    lines = [
        "| 시나리오/변형 | 추정 관절 비율 | VALID 비율 | 관절 오차 중앙값 cm | p95 cm | 최대 cm | VALID p95 cm "
        "| VALID 최대 cm | 불연속 밖 VALID 최대 cm | Z p95 cm | 골반 XY p95 cm | 버린 프레임 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for key, r in results.items():
        valid = r["validity"].get("VALID", 0) / r["joints_total"]
        lines.append(
            f"| {key} | {r['estimated_ratio']:.3f} | {valid:.3f} | {_cm(r['joint_error_m']['median'])} "
            f"| {_cm(r['joint_error_m']['p95'])} | {_cm(r['joint_error_m']['max'])} "
            f"| {_cm(r['valid_joint_error_m']['p95'])} | {_cm(r['valid_joint_error_m']['max'])} "
            f"| {_cm(r['valid_joint_error_m']['max_smooth'])} "
            f"| {_cm(r['joint_z_error_m']['p95'])} "
            f"| {_cm(r['pelvis_xy_error_m']['p95'])} | {r['frame_drops'] or '-'} |"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SIM 고정 시나리오 관절 위치 오차 (상한 성능)")
    parser.add_argument("--json", action="store_true", help="JSON 으로 출력")
    parser.add_argument("--baseline", action="store_true", help="보간 없이 가장 가까운 프레임을 쓴 비교 결과도 낸다")
    parser.add_argument("--scenario", action="append", choices=SCENARIOS, help="일부 시나리오만 (여러 번 줄 수 있음)")
    args = parser.parse_args(argv)
    results = run_all(tuple(args.scenario) if args.scenario else SCENARIOS, baseline=args.baseline)
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else markdown(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
