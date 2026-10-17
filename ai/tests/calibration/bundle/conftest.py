"""번들 조정 시험 공용 fixture: 시뮬레이터 4대 카메라 + 걷는 사람의 합성 keypoint.

- 시뮬레이터는 `python -m sim.run` 으로 돌린다(tests/sim 과 같은 방식). 출력 중 `observations/`(픽셀 잡음 σ 1px,
  가림 conf 0, 화면 밖 null), `world_tracks.jsonl` 의 정답 사람 대응(per_camera), `manifest.json` 의 input_provenance,
  `calibration.json`(정답 카메라: 교란해 초기값으로 쓰고 평가에 쓴다)만 쓴다.
- 카메라별 촬영 시각 차이는 `geometry.interpolation.interpolate_views` 로 공통 시각(약 3.3 Hz)에 맞춘다. keypoint 는
  COCO17 에 발 끝점(foot_tips_2d) 2개를 붙여 19개로 넣는다.
- 초기값은 정답을 교란한 값이다(MapAnything 초기 추정 대신): 회전 2°, 카메라 중심 축마다 σ 0.15 m, 초점 +4%,
  k1 +0.03. 같은 렌즈 공유 초기값처럼 4대에 같은 내부 파라미터를 준다.
- SIM 입력이므로 모든 지표는 상한이다(is_upper_bound). 제품 성능으로 쓰지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from simai_ai.calibration.bundle import PersonObservation
from simai_ai.calibration.intrinsics import BundleAdjustmentInit, CameraIntrinsicsInit
from simai_ai.geometry.interpolation import KeypointSample, interpolate_views

AI_DIR = Path(__file__).resolve().parents[3]
T0 = 1_760_342_400_000
CORNER_CAMERAS = [
    {"id": "cam1", "position": [0.3, 0.3, 2.7], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 0},
    {"id": "cam2", "position": [8.7, 0.3, 2.6], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 7},
    {"id": "cam3", "position": [8.7, 7.2, 2.8], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 13},
    {"id": "cam4", "position": [0.3, 7.2, 2.5], "look_at": [4.5, 3.75, 0.9], "time_offset_ms": 21},
]
# 시험용 약한 배럴 왜곡(방사 2계수). 번들 조정이 다시 추정한다
LENS = {"model": "OPENCV_PINHOLE", "coeffs": [-0.06, 0.012, 0.0, 0.0, 0.0]}
# 교실을 한 바퀴 돌고 대각선으로 가로지르는 사람 + 반대 방향으로 도는 사람
LOOP_WALKERS = [
    {
        "id": "walker",
        "height_m": 1.74,
        "waypoints": [
            {"t_ms": 0, "xy": [1.5, 1.5]},
            {"t_ms": 5000, "xy": [7.5, 1.5]},
            {"t_ms": 9000, "xy": [7.5, 6.0]},
            {"t_ms": 14000, "xy": [1.5, 6.0]},
            {"t_ms": 18000, "xy": [1.5, 1.5]},
            {"t_ms": 24000, "xy": [7.5, 6.0]},
        ],
    },
    {
        "id": "walker2",
        "height_m": 1.62,
        "waypoints": [
            {"t_ms": 0, "xy": [3.0, 5.0]},
            {"t_ms": 4000, "xy": [3.0, 2.5]},
            {"t_ms": 9000, "xy": [6.0, 2.5]},
            {"t_ms": 13000, "xy": [6.0, 5.0]},
            {"t_ms": 17000, "xy": [3.0, 5.0]},
            {"t_ms": 24000, "xy": [6.0, 2.5]},
        ],
    },
]
STRAIGHT_WALKER = [
    {"id": "walker", "height_m": 1.74, "waypoints": [{"t_ms": 0, "xy": [1.5, 3.75]}, {"t_ms": 6000, "xy": [7.5, 3.75]}]}
]
SAMPLE_PERIOD_MS = 300
PERTURB = {"rotation_deg": 2.0, "center_sigma_m": 0.15, "focal_rel": 0.04, "k1": 0.03}


def run_sim(workdir: Path, people: list[dict], duration_ms: int, cameras: list[dict] | None = None) -> Path:
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))
    doc.update({"name": "bundle", "duration_ms": duration_ms, "people": people})
    doc["cameras"] = cameras or CORNER_CAMERAS
    doc["camera_defaults"]["distortion"] = LENS
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


class SimCase:
    """시뮬레이터 출력 하나. observations 는 공통 시각 사람별 관측(PersonObservation)."""

    def __init__(self, out: Path):
        self.out = out
        self.manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        self.calibration = json.loads((out / "calibration.json").read_text(encoding="utf-8"))
        self.truth_cameras = {c["camera_id"]: c for c in self.calibration["cameras"]}
        self.input_provenance = self.manifest["input_provenance"]
        self.observations = self._observations()

    def _observations(self) -> list[PersonObservation]:
        samples: dict[tuple[str, str], list[KeypointSample]] = defaultdict(list)
        for path in sorted((self.out / "observations").glob("*.jsonl")):
            for row in _jsonl(path):
                for det in row["detections"]:
                    kps = [*det["keypoints_2d"], *det["foot_tips_2d"]]
                    samples[(row["camera_id"], det["local_track_id"])].append(
                        KeypointSample(
                            camera_id=row["camera_id"],
                            estimated_capture_time=row["estimated_capture_time_ms"],
                            timestamp_source=row["timestamp_source"],
                            timestamp_uncertainty_ms=row["timestamp_uncertainty_ms"],
                            keypoints=tuple(tuple(kp) if kp is not None else None for kp in kps),
                        )
                    )
        assoc: dict[str, dict[str, str]] = defaultdict(dict)
        for doc in _jsonl(self.out / "world_tracks.jsonl"):
            for tr in doc["tracks"]:
                for obs in tr["per_camera"]:
                    assoc[tr["world_id"]][obs["camera_id"]] = obs["local_track_id"]
        start = self.manifest["start_time_ms"]
        end = start + self.manifest["duration_ms"]
        out: list[PersonObservation] = []
        for world_id, per_cam in sorted(assoc.items()):
            by_camera = {cid: samples[(cid, ltid)] for cid, ltid in per_cam.items() if (cid, ltid) in samples}
            for t in range(start + 100, end - 100, SAMPLE_PERIOD_MS):
                views, _ = interpolate_views(by_camera, t)
                if len(views) >= 2:
                    out.append(PersonObservation(world_id, t, {cid: v.keypoints for cid, v in views.items()}))
        return out

    def initial(self, seed: int = 7, perturb: dict | None = None, camera_ids=None):
        """(BundleAdjustmentInit, initial_extrinsics): 정답을 교란한 초기값."""
        p = dict(PERTURB, **(perturb or {}))
        rng = np.random.default_rng(seed)
        ids = sorted(camera_ids or self.truth_cameras)
        first = self.truth_cameras[ids[0]]
        f = first["K"][0][0] * (1 + p["focal_rel"])
        k1, k2 = first["distortion"]["coeffs"][:2]
        cams = []
        extr = {}
        for cid in ids:
            gt = self.truth_cameras[cid]
            K = [[f, 0.0, gt["K"][0][2]], [0.0, f, gt["K"][1][2]], [0.0, 0.0, 1.0]]
            camera = {
                "camera_id": cid,
                "image_size": gt["image_size"],
                "K": K,
                "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [k1 + p["k1"], k2, 0.0, 0.0, 0.0]},
            }
            cams.append(
                CameraIntrinsicsInit(
                    camera_id=cid,
                    camera=camera,
                    focal_prior_std_px=0.05 * f,
                    k_prior_std=(0.05, 0.03),
                    principal_point_fixed=True,
                )
            )
            R = np.array(gt["R"])
            C = -R.T @ np.array(gt["t"])
            axis = rng.normal(size=3)
            dR = Rotation.from_rotvec(axis / np.linalg.norm(axis) * np.radians(p["rotation_deg"])).as_matrix()
            R2 = R @ dR
            C2 = C + rng.normal(scale=p["center_sigma_m"], size=3)
            extr[cid] = (R2.tolist(), (-R2 @ C2).tolist())
        return BundleAdjustmentInit(tuple(cams), {"format": "test"}), extr

    def truth_center(self, camera_id: str) -> np.ndarray:
        cam = self.truth_cameras[camera_id]
        return -np.array(cam["R"]).T @ np.array(cam["t"])


@pytest.fixture(scope="session")
def loop_case(tmp_path_factory) -> SimCase:
    return SimCase(run_sim(tmp_path_factory.mktemp("ba-loop"), LOOP_WALKERS, 24000))


@pytest.fixture(scope="session")
def straight_case(tmp_path_factory) -> SimCase:
    return SimCase(run_sim(tmp_path_factory.mktemp("ba-straight"), STRAIGHT_WALKER, 6000))


@pytest.fixture(scope="session")
def ba() -> SimpleNamespace:
    """시험 파일에서 쓰는 상수 (conftest 를 직접 import 하지 않기 위함)."""
    return SimpleNamespace(T0=T0, LENS=LENS, PERTURB=PERTURB, run_sim=run_sim, SimCase=SimCase)
