"""BEV 평면도 시험 공용 fixture: 시뮬레이터 카메라 보정과 바닥 격자 무늬 합성 영상.

- 카메라는 시뮬레이터(`python -m sim.run`)의 basic 시나리오 가상 카메라 4대(Tapo C110 stream1 사양,
  2304x1296, 가로 화각 105°)다. 출력 중 `calibration.json` 만 쓴다(tests/sim 과 같은 실행 방식).
- 합성 영상은 바닥(Z = 0) 평면에 알려진 무늬만 그린 것이다. 사람·실제 영상은 쓰지 않는다.
- 렌더는 BEV 와 반대 방향(원본 픽셀 → 왜곡 제거 → 광선 → 바닥 교점)으로 한다. BEV 는 바닥 점 → 왜곡
  적용 → 픽셀 방향이라 두 경로가 맞아야 격자가 제자리에 온다.
- SIM 입력이므로 결과는 입력이 완벽할 때의 상한이다(is_upper_bound). 제품 성능으로 쓰지 않는다.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from simai_ai.geometry.rig import RigCamera
from simai_ai.providers.base import CalibrationProvider, Frame

AI_DIR = Path(__file__).resolve().parents[2]
ROOM = (9.0, 7.5)
# 사인 격자 주기(m). 위상으로 위치를 되읽으므로 주기의 절반(1 m)보다 작은 오차만 잴 수 있다.
GRATING_PERIOD_M = 2.0
# 실제 광각 렌즈 정도의 배럴 왜곡. 영상 안에서 접히지 않는(단조) 계수다.
BARREL = {"model": "OPENCV_PINHOLE", "coeffs": [-0.12, 0.02, 0.0005, -0.0003, 0.0]}
# 왜곡 영상 렌더는 픽셀마다 반복 왜곡 제거를 하므로 느리다. 화각·비율은 C110 그대로 두고 해상도만 절반으로 한다.
DISTORTED_IMAGE_SIZE = [1152, 648]

Pattern = Callable[[np.ndarray, np.ndarray], np.ndarray]


class DictCalibrationProvider(CalibrationProvider):
    """보정 문서 dict 를 그대로 내는 시험용 Provider."""

    def __init__(self, doc: dict) -> None:
        self._doc = doc

    def current(self) -> dict:
        return self._doc


def run_sim_calibration(workdir: Path, distortion: dict | None = None, image_size: list[int] | None = None) -> dict:
    """basic 시나리오(필요하면 왜곡·해상도만 바꿔서)를 짧게 돌리고 calibration.json 을 읽는다."""
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))
    doc["duration_ms"] = 200
    if distortion is not None:
        doc["name"] = "basic-distorted"
        doc["camera_defaults"]["distortion"] = distortion
    if image_size is not None:
        doc["camera_defaults"]["image_size"] = image_size
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
    return json.loads((out / "calibration.json").read_text(encoding="utf-8"))


def floor_hits(camera: dict, rows: slice) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """원본 영상 행 범위의 픽셀(정수 = 픽셀 중심)마다 광선이 바닥과 만나는 World (x, y)와 적중 여부."""
    cam = RigCamera.from_calibration(camera)
    w, h = cam.image_size
    us = np.arange(w, dtype=float)
    vs = np.arange(h, dtype=float)[rows]
    gu, gv = np.meshgrid(us, vs)
    xn = cam.normalize(np.column_stack([gu.ravel(), gv.ravel()]))
    d = np.column_stack([xn, np.ones(len(xn))]) @ cam.R
    with np.errstate(divide="ignore", invalid="ignore"):
        s = -cam.center[2] / d[:, 2]
    hit = (d[:, 2] < 0) & np.isfinite(s) & (s > 0)
    s = np.where(hit, s, 0.0)
    x = cam.center[0] + s * d[:, 0]
    y = cam.center[1] + s * d[:, 1]
    return x, y, hit


def render_floor(camera: dict, pattern: Pattern, channels: int, dtype=np.float32, chunk_rows: int = 162) -> np.ndarray:
    """바닥 평면 무늬만 있는 카메라 영상 (H, W, C). 바닥에 닿지 않는 픽셀은 0."""
    w, h = camera["image_size"]
    out = np.zeros((h, w, channels), dtype=dtype)
    for r0 in range(0, h, chunk_rows):
        rows = slice(r0, min(h, r0 + chunk_rows))
        x, y, hit = floor_hits(camera, rows)
        values = np.zeros((len(x), channels))
        values[hit] = pattern(x[hit], y[hit])
        out[rows] = values.reshape(rows.stop - rows.start, w, channels).astype(dtype)
    return out


def grating(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """위치를 위상으로 되읽을 수 있는 사인 격자 4채널: sin·cos(2πx/P), sin·cos(2πy/P), [0, 1] 범위."""
    k = 2 * math.pi / GRATING_PERIOD_M
    return 0.5 + 0.5 * np.column_stack([np.sin(k * x), np.cos(k * x), np.sin(k * y), np.cos(k * y)])


def checker(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """눈으로 보는 0.5 m 바둑판(BGR uint8 범위). 방 밖은 어둡게."""
    cell = (np.floor(x / 0.5) + np.floor(y / 0.5)) % 2
    inside = (x >= 0) & (x <= ROOM[0]) & (y >= 0) & (y <= ROOM[1])
    gray = np.where(cell == 0, 220.0, 60.0) * np.where(inside, 1.0, 0.4)
    return np.column_stack([gray, gray * 0.9, gray * 0.8])


def room_floor_mask(camera: dict) -> np.ndarray:
    """광선이 방 바닥 [0, 9] x [0, 7.5] 에 닿는 픽셀 (H, W) bool. 시험용 바닥 마스크."""
    w, h = camera["image_size"]
    x, y, hit = floor_hits(camera, slice(0, h))
    inside = hit & (x >= 0) & (x <= ROOM[0]) & (y >= 0) & (y <= ROOM[1])
    return inside.reshape(h, w)


def make_frames(calibration: dict, images: dict[str, np.ndarray]) -> list[Frame]:
    t0 = calibration["created_at"]
    return [
        Frame(
            camera_id=cid,
            estimated_capture_time=t0,
            timestamp_source="SIM",
            frame_ref={"stream_epoch": 0, "frame_sequence": 0},
            image=img,
        )
        for cid, img in images.items()
    ]


@pytest.fixture(scope="session")
def sim_calibration(tmp_path_factory) -> dict:
    return run_sim_calibration(tmp_path_factory.mktemp("bev-sim"))


@pytest.fixture(scope="session")
def sim_calibration_distorted(tmp_path_factory) -> dict:
    return run_sim_calibration(tmp_path_factory.mktemp("bev-sim-distorted"), BARREL, DISTORTED_IMAGE_SIZE)


@pytest.fixture(scope="session")
def grating_images(sim_calibration) -> dict[str, np.ndarray]:
    return {c["camera_id"]: render_floor(c, grating, 4) for c in sim_calibration["cameras"]}


@pytest.fixture(scope="session")
def grating_images_distorted(sim_calibration_distorted) -> dict[str, np.ndarray]:
    return {c["camera_id"]: render_floor(c, grating, 4) for c in sim_calibration_distorted["cameras"]}


@pytest.fixture(scope="session")
def scene_helpers() -> SimpleNamespace:
    """시험 파일에서 쓰는 렌더 도우미와 상수 (conftest 를 직접 import 하지 않기 위함)."""
    return SimpleNamespace(
        ROOM=ROOM,
        GRATING_PERIOD_M=GRATING_PERIOD_M,
        DictCalibrationProvider=DictCalibrationProvider,
        make_frames=make_frames,
        room_floor_mask=room_floor_mask,
    )


@pytest.fixture(scope="session")
def checker_images(sim_calibration) -> dict[str, np.ndarray]:
    """눈으로 보는 바둑판 uint8 BGR 영상(카메라 4대)."""
    return {c["camera_id"]: render_floor(c, checker, 3, dtype=np.uint8) for c in sim_calibration["cameras"]}
