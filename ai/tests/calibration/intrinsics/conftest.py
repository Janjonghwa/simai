"""무마커 내부 파라미터 시험 공용 fixture: 시뮬레이터 카메라와 왜곡 있는 합성 교실 영상.

- 카메라는 시뮬레이터(`python -m sim.run`)의 basic 시나리오 가상 카메라 4대(Tapo C110 stream1 사양, 가로 화각
  105°)이고 출력 중 `calibration.json` 만 쓴다(tests/scene 과 같은 실행 방식). 렌더가 느리므로 화각·비율은 그대로
  두고 해상도만 절반(1152x648)으로 한다.
- 합성 영상은 9 x 7.5 x 3 m 상자 방의 바닥·벽·천장에 직선(바닥 타일 줄, 천장 격자, 벽 가로·세로 줄, 걸레받이)만
  그린 것이다. 사람·실제 영상은 쓰지 않는다.
- 렌더는 원본 픽셀 → 왜곡 제거(뉴턴법, `undistort_normalized`) → 광선 → 방 표면 교점 순서다. 선은 표면 위
  픽셀 크기로 경계를 부드럽게 해서(상자 필터 근사) 서브픽셀 위치가 영상에 남게 한다.
- SIM 입력이므로 결과는 입력이 완벽할 때의 상한이다(is_upper_bound). 제품 성능으로 쓰지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from simai_ai.calibration.intrinsics.estimate import distort_normalized, undistort_normalized, undistort_pixels
from simai_ai.calibration.intrinsics.plumbline import PlumbLineVPEstimator

AI_DIR = Path(__file__).resolve().parents[3]
ROOM = (9.0, 7.5, 3.0)
HALF_SIZE = [1152, 648]
# 광각 렌즈 정도의 강한 배럴 왜곡(방사만). 영상 모서리까지 단조다
WIDE = {"model": "OPENCV_PINHOLE", "coeffs": [-0.30, 0.09, 0.0, 0.0, 0.0]}
# tests/scene 의 BARREL 과 같은 약한 배럴 + 접선 왜곡(이 추정기는 접선을 모델링하지 않는다)
BARREL_TANGENTIAL = {"model": "OPENCV_PINHOLE", "coeffs": [-0.12, 0.02, 0.0005, -0.0003, 0.0]}
NO_DISTORTION = {"model": "NONE", "coeffs": []}
# 다른 렌즈(좁은 화각) 카메라: 이상치 시험용
NARROW_HFOV_RAD = 1.40


def run_sim_calibration(workdir: Path, distortion: dict, image_size=None, camera_overrides=None, cameras=None) -> dict:
    """basic 시나리오를 짧게 돌리고 calibration.json 을 읽는다. camera_overrides = {camera_id: {키: 값}}."""
    doc = json.loads((AI_DIR / "sim" / "scenarios" / "basic.json").read_text(encoding="utf-8"))
    doc["duration_ms"] = 200
    doc["camera_defaults"]["distortion"] = distortion
    doc["camera_defaults"]["image_size"] = image_size or HALF_SIZE
    if cameras is not None:
        doc["cameras"] = cameras
    for cam in doc["cameras"]:
        cam.update((camera_overrides or {}).get(cam["id"], {}))
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


def _line_cover(coord, spacing, width, footprint, offset=0.0):
    """주기 spacing 의 폭 width 선이 픽셀을 덮는 비율(상자 필터 근사)."""
    d = np.abs(np.mod(coord - offset + spacing / 2, spacing) - spacing / 2)
    return np.clip(0.5 + (width / 2 - d) / footprint, 0.0, 1.0)


def _single_line_cover(coord, at, width, footprint):
    return np.clip(0.5 + (width / 2 - np.abs(coord - at)) / footprint, 0.0, 1.0)


def _band_cover(coord, lo, hi, footprint):
    return np.clip(0.5 + np.minimum(coord - lo, hi - coord) / footprint, 0.0, 1.0)


def camera_rays(camera: dict, uv: np.ndarray) -> np.ndarray:
    """원본 픽셀 → World 단위 광선 방향 (N, 3)."""
    K = np.asarray(camera["K"], dtype=float)
    R = np.asarray(camera["R"], dtype=float)
    xd = np.column_stack([(uv[:, 0] - K[0, 2]) / K[0, 0], (uv[:, 1] - K[1, 2]) / K[1, 1]])
    coeffs = camera["distortion"]["coeffs"] if camera["distortion"]["model"] != "NONE" else []
    xn = undistort_normalized(xd, coeffs) if coeffs else xd
    d = np.column_stack([xn, np.ones(len(xn))]) @ R
    return d / np.linalg.norm(d, axis=1, keepdims=True)


def camera_center(camera: dict) -> np.ndarray:
    R = np.asarray(camera["R"], dtype=float)
    return -R.T @ np.asarray(camera["t"], dtype=float)


def render_room(camera: dict, seed: int = 0, chunk_rows: int = 162) -> np.ndarray:
    """직선만 있는 교실 합성 영상 (H, W) float32, 값 범위 약 [0, 1]."""
    w, h = camera["image_size"]
    f = float(camera["K"][0][0])
    C = camera_center(camera)
    out = np.zeros((h, w), dtype=np.float32)
    for r0 in range(0, h, chunk_rows):
        vs = np.arange(r0, min(h, r0 + chunk_rows), dtype=float)
        gu, gv = np.meshgrid(np.arange(w, dtype=float), vs)
        d = camera_rays(camera, np.column_stack([gu.ravel(), gv.ravel()]))
        ts = []
        for ax, size in enumerate(ROOM):
            with np.errstate(divide="ignore", invalid="ignore"):
                t = np.where(d[:, ax] > 0, (size - C[ax]) / d[:, ax], -C[ax] / d[:, ax])
            ts.append(np.where(np.isfinite(t) & (t > 0), t, np.inf))
        ts = np.column_stack(ts)
        ax = np.argmin(ts, axis=1)
        rows = np.arange(len(ax))
        t = ts[rows, ax]
        P = C + d * t[:, None]
        # 표면 위 한 픽셀의 크기(m): 거리 / 초점, 비스듬히 볼수록 커진다
        footprint = t / f / np.maximum(np.abs(d[rows, ax]), 0.05)
        x, y, z = P[:, 0], P[:, 1], P[:, 2]
        floor = (ax == 2) & (d[:, 2] < 0)
        ceiling = (ax == 2) & (d[:, 2] > 0)
        wall_x = ax == 0
        wall_y = ax == 1
        base = np.select(
            [floor, ceiling, wall_x & (d[:, 0] < 0), wall_x, wall_y & (d[:, 1] < 0), wall_y],
            [0.55, 0.85, 0.72, 0.66, 0.76, 0.62],
        )
        floor_lines = np.maximum(_line_cover(x, 1.0, 0.03, footprint), _line_cover(y, 1.0, 0.03, footprint))
        ceiling_lines = np.maximum(
            _line_cover(x, 1.5, 0.03, footprint, 0.75), _line_cover(y, 1.5, 0.03, footprint, 0.75)
        )
        wall_horizontal = np.maximum(
            _band_cover(z, -1.0, 0.1, footprint),
            np.maximum(_single_line_cover(z, 1.0, 0.04, footprint), _single_line_cover(z, 2.2, 0.04, footprint)),
        )
        dark = np.select(
            [floor, ceiling, wall_x, wall_y],
            [
                floor_lines,
                ceiling_lines,
                np.maximum(wall_horizontal, _line_cover(y, 1.5, 0.04, footprint, 0.75)),
                np.maximum(wall_horizontal, _line_cover(x, 1.5, 0.04, footprint, 0.75)),
            ],
        )
        out[r0 : r0 + len(vs)] = (base * (1 - dark) + 0.12 * dark).reshape(len(vs), w)
    rng = np.random.default_rng(seed)
    return out + rng.normal(0.0, 0.005, out.shape).astype(np.float32)


def world_line_points(camera: dict, samples: int = 400) -> list[np.ndarray]:
    """방의 직선(바닥 타일 줄, 벽 세로줄, 벽-바닥 경계)을 정답 카메라로 투영한 원본 픽셀 점 목록.

    검출기와 무관한 직선성 지표용이다. 영상 안에 보이는 점이 40개 이상인 직선만 낸다.
    """
    lines = []
    for k in np.arange(1.0, ROOM[0], 1.0):
        lines.append(((k, 0.0, 0.0), (k, ROOM[1], 0.0)))
    for k in np.arange(1.0, ROOM[1], 1.0):
        lines.append(((0.0, k, 0.0), (ROOM[0], k, 0.0)))
    for k in np.arange(0.75, ROOM[0], 1.5):
        lines.append(((k, 0.0, 0.0), (k, 0.0, ROOM[2])))
        lines.append(((k, ROOM[1], 0.0), (k, ROOM[1], ROOM[2])))
    for k in np.arange(0.75, ROOM[1], 1.5):
        lines.append(((0.0, k, 0.0), (0.0, k, ROOM[2])))
        lines.append(((ROOM[0], k, 0.0), (ROOM[0], k, ROOM[2])))
    for z in (0.0, 1.0, 2.2):
        lines.append(((0.0, 0.0, z), (ROOM[0], 0.0, z)))
        lines.append(((0.0, ROOM[1], z), (ROOM[0], ROOM[1], z)))
        lines.append(((0.0, 0.0, z), (0.0, ROOM[1], z)))
        lines.append(((ROOM[0], 0.0, z), (ROOM[0], ROOM[1], z)))
    K = np.asarray(camera["K"], dtype=float)
    R = np.asarray(camera["R"], dtype=float)
    t = np.asarray(camera["t"], dtype=float)
    coeffs = camera["distortion"]["coeffs"] if camera["distortion"]["model"] != "NONE" else []
    w, h = camera["image_size"]
    out = []
    for a, b in lines:
        s = np.linspace(0.0, 1.0, samples)[:, None]
        P = np.asarray(a) * (1 - s) + np.asarray(b) * s
        Xc = P @ R.T + t
        front = Xc[:, 2] > 0.3
        xn = Xc[front, :2] / Xc[front, 2:3]
        xd = distort_normalized(xn, coeffs) if coeffs else xn
        uv = np.column_stack([K[0, 0] * xd[:, 0] + K[0, 2], K[1, 1] * xd[:, 1] + K[1, 2]])
        inside = (uv[:, 0] >= 0) & (uv[:, 0] <= w - 1) & (uv[:, 1] >= 0) & (uv[:, 1] <= h - 1)
        # 영상 안이라도 정답 왜곡의 정의역 밖(접힘)은 뺀다
        inside &= np.hypot(xn[:, 0], xn[:, 1]) < 3.0
        if inside.sum() >= 40:
            out.append(uv[inside])
    return out


def straightness_rms_px(lines_uv: list[np.ndarray], focal_px: float | None, coeffs, principal_point) -> float:
    """직선 점들을 (focal, coeffs) 로 펴서 직선 맞춤 수직 거리 RMS(펴진 이상적 핀홀 픽셀). focal None 이면 그대로."""
    sq = []
    for uv in lines_uv:
        if focal_px is None:
            pts = uv
        else:
            pts = undistort_pixels(uv, focal_px, coeffs, principal_point) * focal_px
        c = pts - pts.mean(axis=0)
        _, vecs = np.linalg.eigh(c.T @ c)
        sq.append((c @ vecs[:, 0]) ** 2)
    return float(np.sqrt(np.mean(np.concatenate(sq))))


class SimCase:
    """시나리오 하나의 보정·영상·추정을 지연 계산으로 들고 있는다(세션 공유)."""

    def __init__(self, calibration: dict) -> None:
        self.calibration = calibration
        self.cameras = {c["camera_id"]: c for c in calibration["cameras"]}
        self._images: dict[str, np.ndarray] = {}
        self._estimates: dict[str, object] = {}

    def image(self, camera_id: str) -> np.ndarray:
        if camera_id not in self._images:
            self._images[camera_id] = render_room(self.cameras[camera_id])
        return self._images[camera_id]

    def estimate(self, camera_id: str):
        if camera_id not in self._estimates:
            self._estimates[camera_id] = PlumbLineVPEstimator().estimate(self.image(camera_id), camera_id=camera_id)
        return self._estimates[camera_id]


@pytest.fixture(scope="session")
def wide_case(tmp_path_factory) -> SimCase:
    return SimCase(run_sim_calibration(tmp_path_factory.mktemp("intr-wide"), WIDE))


@pytest.fixture(scope="session")
def narrow_case(tmp_path_factory) -> SimCase:
    """cam4 만 다른 렌즈(좁은 화각)인 시나리오. 왜곡 계수는 같다."""
    return SimCase(
        run_sim_calibration(
            tmp_path_factory.mktemp("intr-narrow"), WIDE, camera_overrides={"cam4": {"hfov_rad": NARROW_HFOV_RAD}}
        )
    )


@pytest.fixture(scope="session")
def barrel_case(tmp_path_factory) -> SimCase:
    return SimCase(run_sim_calibration(tmp_path_factory.mktemp("intr-barrel"), BARREL_TANGENTIAL))


@pytest.fixture(scope="session")
def pinhole_case(tmp_path_factory) -> SimCase:
    return SimCase(run_sim_calibration(tmp_path_factory.mktemp("intr-pinhole"), NO_DISTORTION))


@pytest.fixture(scope="session")
def frontal_case(tmp_path_factory) -> SimCase:
    """벽을 정면·수평으로 보는 카메라 1대: 가로·세로 직선의 소실점이 무한대라 초점이 정해지지 않는다."""
    cameras = [{"id": "front", "position": [4.5, 0.3, 1.5], "look_at": [4.5, 7.5, 1.5], "time_offset_ms": 0}]
    return SimCase(run_sim_calibration(tmp_path_factory.mktemp("intr-front"), WIDE, cameras=cameras))


@pytest.fixture(scope="session")
def intr() -> SimpleNamespace:
    """시험 파일에서 쓰는 도우미와 상수 (conftest 를 직접 import 하지 않기 위함)."""
    return SimpleNamespace(
        WIDE=WIDE,
        BARREL_TANGENTIAL=BARREL_TANGENTIAL,
        HALF_SIZE=HALF_SIZE,
        world_line_points=world_line_points,
        straightness_rms_px=straightness_rms_px,
        render_room=render_room,
    )
