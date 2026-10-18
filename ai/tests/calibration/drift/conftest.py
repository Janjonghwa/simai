"""틀어짐 감시 SIM 시험 공용 fixture: 모서리 카메라 4대 + 약 1° 돌린 cam2 사본, 교실을 오래 걷는 세 사람.

- 시뮬레이터(`python -m sim.run`)를 한 번 돌린다. cam2 와 같은 위치에서 수평으로 1° 돌린 가상 카메라 `cam2r` 를 함께
  넣어, 회전 시각 뒤에는 cam2 관측 대신 cam2r 관측을 cam2 이름으로 넣는다(15.1 틀어짐 재현: 5m 벽 점이 약 8.7cm 이동).
- 사람 대응은 world_tracks.jsonl 의 정답 per_camera 를 쓴다(Cross-view 연결 결과 대신). 카메라별 시각 차이는
  `geometry.interpolation.interpolate_views` 로 공통 시각에 맞춘다.
- 픽셀 잡음 σ 1.5px 에 더해, 보인 keypoint 의 3% 를 20~60px 튀게 해 검출 이상치를 흉내 낸다(결정적 시드).
- SIM 입력이므로 감지 시간·오경보 수는 상한이다(input_provenance.is_upper_bound). 제품 성능으로 쓰지 않는다.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from simai_ai.calibration import (
    CalibrationEvidence,
    CalibrationState,
    CalibrationVersionStore,
    CameraCalibration,
    HeightCheckpoint,
    ScaleBasis,
    ScaleEstimate,
)
from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.monitor import BaselineRecord, baseline_from_observations
from simai_ai.calibration.recalibration import RecalibrationOutcome, run_recalibration
from simai_ai.calibration.watch import CalibrationDriftWatch, WatchReport
from simai_ai.geometry.interpolation import KeypointSample, interpolate_views

AI_DIR = Path(__file__).resolve().parents[3]
DURATION_MS = 600_000
SAMPLE_PERIOD_MS = 1000
ROTATION_DEG = 1.0
LENS = {"model": "OPENCV_PINHOLE", "coeffs": [-0.06, 0.012, 0.0, 0.0, 0.0]}
CAM2 = {"id": "cam2", "position": [8.7, 0.3, 2.6], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 7}
CAMERAS = [
    {"id": "cam1", "position": [0.3, 0.3, 2.7], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 0},
    CAM2,
    {"id": "cam3", "position": [8.7, 7.2, 2.8], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 13},
    {"id": "cam4", "position": [0.3, 7.2, 2.5], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 21},
]
OUTLIER_RATE = 0.03


def _panned(camera: dict, deg: float, new_id: str) -> dict:
    """같은 위치에서 수평으로 deg 만큼 돌린 카메라."""
    px, py, _ = camera["position"]
    lx, ly, lz = camera["look_at"]
    a = math.radians(deg)
    dx, dy = lx - px, ly - py
    rx, ry = dx * math.cos(a) - dy * math.sin(a), dx * math.sin(a) + dy * math.cos(a)
    return {**camera, "id": new_id, "look_at": [px + rx, py + ry, lz]}


def _walkers(seed: int = 5, count: int = 3, speed_mps: float = 1.0) -> list[dict]:
    rng = np.random.default_rng(seed)
    people = []
    for k in range(count):
        t, xy = 0, [float(rng.uniform(1.5, 7.5)), float(rng.uniform(1.5, 6.0))]
        waypoints = [{"t_ms": 0, "xy": xy}]
        while t < DURATION_MS:
            nxt = [float(rng.uniform(1.0, 8.0)), float(rng.uniform(1.0, 6.5))]
            dist = math.hypot(nxt[0] - xy[0], nxt[1] - xy[1])
            t += max(500, int(dist / speed_mps * 1000))
            # 가끔 멈춰 선다(교실 안 대기)
            if rng.uniform() < 0.2:
                waypoints.append({"t_ms": t, "xy": nxt})
                t += int(rng.uniform(2000, 8000))
            waypoints.append({"t_ms": t, "xy": nxt})
            xy = nxt
        people.append({"id": f"p{k + 1}", "height_m": round(float(rng.uniform(1.55, 1.85)), 2), "waypoints": waypoints})
    return people


def run_sim(workdir: Path) -> Path:
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))
    doc.update({"name": "drift", "duration_ms": DURATION_MS, "people": _walkers()})
    doc["cameras"] = [*CAMERAS, _panned(CAM2, ROTATION_DEG, "cam2r")]
    doc["camera_defaults"]["distortion"] = LENS
    doc["camera_defaults"]["fps"] = 10
    doc["world_rate_hz"] = 1  # world_tracks 는 정답 사람 대응(per_camera)에만 쓴다(local_track_id 는 사람마다 고정)
    doc["keypoints"] = {"pixel_noise_sigma_px": 1.5}
    scenario = workdir / "scenario.json"
    scenario.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    out = workdir / "out"
    result = subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(scenario), "--out", str(out)],
        cwd=AI_DIR,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0, result.stderr
    return out


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DriftSim:
    """시뮬레이터 출력 하나. frames(t) 는 공통 시각 t 의 사람별 관측(PersonObservation) 목록."""

    def __init__(self, out: Path):
        self.out = out
        self.manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.calibration = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
        self.truth_cameras = {c["camera_id"]: c for c in self.calibration["cameras"]}
        self.input_provenance = self.manifest["input_provenance"]
        self.start = self.manifest["start_time_ms"]
        self.times = list(range(self.start + 200, self.start + DURATION_MS - 200, SAMPLE_PERIOD_MS))
        self._nominal, self._rotated = self._build()

    def _build(self) -> tuple[dict[int, list[PersonObservation]], dict[int, list[PersonObservation]]]:
        rng = np.random.default_rng(23)
        samples: dict[tuple[str, str], list[KeypointSample]] = defaultdict(list)
        for path in sorted((self.out / "observations").glob("*.jsonl")):
            for row in _jsonl(path):
                for det in row["detections"]:
                    kps = []
                    for kp in det["keypoints_2d"]:
                        if kp is not None and kp[2] > 0 and rng.uniform() < OUTLIER_RATE:
                            ang, mag = rng.uniform(0, 2 * math.pi), rng.uniform(20, 60)
                            kp = [kp[0] + mag * math.cos(ang), kp[1] + mag * math.sin(ang), kp[2]]
                        kps.append(tuple(kp) if kp is not None else None)
                    samples[(row["camera_id"], det["local_track_id"])].append(
                        KeypointSample(
                            camera_id=row["camera_id"],
                            estimated_capture_time=row["estimated_capture_time_ms"],
                            timestamp_source=row["timestamp_source"],
                            timestamp_uncertainty_ms=row["timestamp_uncertainty_ms"],
                            keypoints=tuple(kps),
                        )
                    )
        for frames in samples.values():
            frames.sort(key=lambda s: s.estimated_capture_time)
        assoc: dict[str, dict[str, str]] = defaultdict(dict)
        for doc in _jsonl(self.out / "world_tracks.jsonl"):
            for tr in doc["tracks"]:
                for obs in tr["per_camera"]:
                    assoc[tr["world_id"]][obs["camera_id"]] = obs["local_track_id"]
        nominal: dict[int, list[PersonObservation]] = defaultdict(list)
        rotated: dict[int, list[PersonObservation]] = defaultdict(list)
        for world_id, per_cam in sorted(assoc.items()):
            by_camera = {cid: samples[(cid, ltid)] for cid, ltid in per_cam.items() if (cid, ltid) in samples}
            stamps = {cid: [s.estimated_capture_time for s in v] for cid, v in by_camera.items()}
            for t in self.times:
                # interpolate_views 는 호출마다 프레임 전체를 정렬하므로 t 주변 프레임만 넘긴다(결과는 같다)
                near = {}
                for cid, frames in by_camera.items():
                    k = bisect.bisect_right(stamps[cid], t)
                    near[cid] = frames[max(0, k - 2) : k + 2]
                views, _ = interpolate_views(near, t)
                kps = {cid: v.keypoints for cid, v in views.items()}
                base = {cid: k for cid, k in kps.items() if cid != "cam2r"}
                if len(base) >= 2:
                    nominal[t].append(PersonObservation(world_id, t, base))
                turned = {cid: k for cid, k in kps.items() if cid not in ("cam2", "cam2r")}
                if "cam2r" in kps:
                    turned["cam2"] = kps["cam2r"]
                if len(turned) >= 2:
                    rotated[t].append(PersonObservation(world_id, t, turned))
        return nominal, rotated

    def frames(self, t: int, rotated: bool = False) -> list[PersonObservation]:
        return (self._rotated if rotated else self._nominal).get(t, [])


@pytest.fixture(scope="session")
def drift_sim(tmp_path_factory) -> DriftSim:
    return DriftSim(run_sim(tmp_path_factory.mktemp("drift-sim")))


@pytest.fixture(scope="session")
def dsim() -> SimpleNamespace:
    return SimpleNamespace(
        DURATION_MS=DURATION_MS,
        SAMPLE_PERIOD_MS=SAMPLE_PERIOD_MS,
        ROTATION_DEG=ROTATION_DEG,
        ROTATION_AT_S=ROTATION_AT_S,
        calibrated_store=calibrated_store,
        run_watch=run_watch,
        Run=Run,
    )


# --------------------------------------------------------------------------- 감시 실행


INSTALLED = ("cam1", "cam2", "cam3", "cam4")
ROTATION_AT_S = 180


def passing_evidence() -> CalibrationEvidence:
    """CALIBRATED 기준을 통과하는 시험용 근거(tests/calibration/conftest.py 와 같은 값)."""
    return CalibrationEvidence(
        floor_coverage_ratio=0.85,
        floor_plane_disagreement_m=0.02,
        floor_plane_camera_ids=INSTALLED,
        scale_estimates=(
            ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 1.004, label="안전봉"),
            ScaleEstimate(ScaleBasis.MEASURED_OBJECT, 0.996, label="라바콘"),
            ScaleEstimate(ScaleBasis.KNOWN_PERSON_HEIGHT, 1.012, label="팀원 A"),
        ),
        checkpoints=(
            HeightCheckpoint("p1", 1.75, 1.81, 0.08),
            HeightCheckpoint("p2", 1.75, 1.69, 0.11),
            HeightCheckpoint("p3", 1.75, 1.78, 0.06),
        ),
    )


def calibrated_store(sim: DriftSim) -> CalibrationVersionStore:
    """정답 카메라 4대를 현장 보정(G1_AUTO)처럼 넣은 CALIBRATED 저장소. 시험용 상태이며 지표는 SIM 상한이다."""
    store = CalibrationVersionStore(site_id="sim-classroom", camera_ids=INSTALLED)
    cameras = [
        CameraCalibration(
            camera_id=cid,
            image_size=tuple(sim.truth_cameras[cid]["image_size"]),
            K=sim.truth_cameras[cid]["K"],
            R=sim.truth_cameras[cid]["R"],
            t=sim.truth_cameras[cid]["t"],
            distortion_model=sim.truth_cameras[cid]["distortion"]["model"],
            distortion_coeffs=sim.truth_cameras[cid]["distortion"]["coeffs"],
            reprojection_median_px=0.8,
            reprojection_p95_px=2.0,
        )
        for cid in INSTALLED
    ]
    result = store.propose(
        provider_source="G1_AUTO",
        provider_version="test",
        cameras=cameras,
        evidence=passing_evidence(),
        at_ms=sim.start,
    )
    assert result.adopted and store.state is CalibrationState.CALIBRATED, result
    return store


@dataclass
class Run:
    """감시 실행 하나의 기록. reports 는 갱신마다의 WatchReport."""

    store: CalibrationVersionStore
    watch: CalibrationDriftWatch
    reports: list[WatchReport] = field(default_factory=list)
    outcomes: list[tuple[int, RecalibrationOutcome]] = field(default_factory=list)

    def first_marked(self) -> WatchReport | None:
        return next((r for r in self.reports if r.drift_marked), None)


_WALK_BASELINE: dict[str, BaselineRecord] = {}


def calibration_walk_baseline(sim: DriftSim, store: CalibrationVersionStore) -> BaselineRecord:
    """첫 60초 관측을 보정 걷기로 보고 잰 검증된 기준선(보정 입력 관측과 같은 통계, SIM 상한)."""
    key = str(sim.out)
    if key not in _WALK_BASELINE:
        walk = [p.views for t in sim.times if t < sim.start + 60_000 for p in sim.frames(t)]
        _WALK_BASELINE[key] = baseline_from_observations(store.snapshot().doc(), walk)
    return _WALK_BASELINE[key]


def run_watch(sim: DriftSim, frames, until_ms: int, streams=lambda t: (), run: Run | None = None) -> Run:
    """frames(t) 의 관측으로 감시를 돌린다. 재보정 요청이 나오면 그 자리에서 실행한다(시험용 동기 실행)."""
    if run is None:
        store = calibrated_store(sim)
        baselines = {"cal-1": calibration_walk_baseline(sim, store)}
        run = Run(store, CalibrationDriftWatch(store, sim.input_provenance, baselines=baselines))
    start = run.reports[-1].t_ms if run.reports else -1
    for t in sim.times:
        if t <= start:
            continue
        if t > until_ms:
            break
        report = run.watch.update(t, frames(t), streams(t))
        run.reports.append(report)
        if report.recalibration_request is not None:
            request = report.recalibration_request
            outcome = run_recalibration(request, run.store, at_ms=t, is_valid=run.watch.request_still_valid)
            run.watch.recalibration_finished(outcome, t)
            run.outcomes.append((t, outcome))
    return run


@pytest.fixture(scope="session")
def nominal_run(drift_sim) -> Run:
    """회전 없이 10분."""
    return run_watch(drift_sim, drift_sim.frames, drift_sim.start + DURATION_MS)


@pytest.fixture(scope="session")
def rotation_run(drift_sim) -> Run:
    """ROTATION_AT_S 초에 cam2 가 1° 돌아간다. 감지·재보정 뒤 새 버전 감시까지 본다."""
    t_rot = drift_sim.start + ROTATION_AT_S * 1000
    return run_watch(drift_sim, lambda t: drift_sim.frames(t, rotated=t >= t_rot), t_rot + 330_000)
