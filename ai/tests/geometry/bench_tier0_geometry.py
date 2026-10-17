"""Tier 0 기하 단계(왜곡 제거·삼각측량·관절 게이트·Cross-view)의 처리 시간을 잰다 (docs/design/tier0-geometry-budget.md).

ai/ 에서 실행한다. 기본 설치(dev 그룹)로 돈다. GPU 를 쓰지 않는다(이 단계는 CPU numpy 다).

    uv run python tests/geometry/bench_tier0_geometry.py                    # 4대 × 1·5·10명, 표 (Markdown)
    uv run python tests/geometry/bench_tier0_geometry.py --people 10 --ticks 50
    uv run python tests/geometry/bench_tier0_geometry.py --json out.json    # 기록용 JSON 도 남긴다

장면: 교실(9 × 7.5 m) 모서리 높이 2.7 m 카메라 4대, 2304 × 1296, 수평 화각 105°, 광각 배럴 왜곡
(OPENCV_PINHOLE k1 −0.30, k2 0.09). 사람은 서로 0.6 m 이상 떨어진 무작위 위치·방향에 서 있고(일부는 0.4 m 단 위),
tick(10 Hz)마다 조금씩 걷는다. keypoint 는 왜곡까지 넣어 투영하고 픽셀 잡음 σ 1 px, 신뢰도 0.4~1.0, 관절의 10% 는
신뢰도 기준 미만, 3% 는 검출 없음(None)이다. 영상 밖·카메라 뒤 관절은 None 이다.

단계 (tick 마다, 사람 N 명·view 4N 개). 각 단계는 따로 잰다(앞 단계 결과를 넘기지 않는다):
- undistort: view 4N 개 전체 왜곡 제거. 카메라마다 그 카메라 view 를 한 번에 푼다(`RigCamera.undistort_many`).
  묶음 API 가 없는 이전 구현에서는 view 마다 `normalize_with_mask` 한 번.
- triangulate: 정답 연결로 사람마다 `triangulate_skeleton` (자체 왜곡 제거 포함).
- triangulate_batch: 같은 입력을 `triangulate_skeletons` 한 번으로 (묶음 API 가 없는 이전 구현에서는 triangulate 와 같다).
- joint_gate: 정답 연결로 사람마다 `JointGate.update` (자체 왜곡 제거·삼각측량 포함, 다른 사람 검출을 others 로 넘김).
- cross_view: `CrossViewAssociator.associate` (view 4N 개, 왜곡 제거·클러스터 최종 삼각측량 포함).
- pipeline: World Track 갱신 한 번의 기하 시간. cross_view 뒤에 클러스터마다 `JointGate.update` 를 부른다(others 는 다른
  클러스터의 view). 묶음 API 가 있으면 클러스터의 왜곡 제거·삼각측량 결과(`ViewCluster.undistorted`, `ViewCluster.skeleton`)를
  undistorted·others_undistorted·skeleton 으로 넘겨 다시 계산하지 않는다.

이전 구현과 비교하려면 이전 커밋의 `ai/src` 를 PYTHONPATH 앞에 두고 같은 스크립트를 돌린다(docs/design/tier0-geometry-budget.md).

결과는 SIM 합성 입력의 처리 시간이다. 정확도 지표가 아니다. 운영 판정은 Pod(H100 노드) CPU 측정으로 한다(명세 6.6).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.geometry import distortion, triangulation
from simai_ai.geometry.interpolation import InterpolatedView
from simai_ai.geometry.joint_gate import JointGate
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.providers.base import CalibrationProvider
from simai_ai.tracking.cross_view import CrossViewAssociator, LocalView

T0 = 1_760_342_400_000
TICK_MS = 100
SIZE = (2304, 1296)
HFOV_DEG = 105.0
WIDE_COEFFS = (-0.30, 0.09, 0.0, 0.0, 0.0)
CORNERS = {"cam1": (0.3, 0.3, 2.7), "cam2": (8.7, 0.3, 2.7), "cam3": (8.7, 7.2, 2.7), "cam4": (0.3, 7.2, 2.7)}
ROOM = ((0.8, 8.2), (0.8, 6.7))
MIN_SEPARATION_M = 0.6
# COCO17 서 있는 자세 오프셋 (dx 오른쪽, dz 위, 미터)
OFFSETS = (
    (0.0, 1.60), (-0.03, 1.63), (0.03, 1.63), (-0.08, 1.60), (0.08, 1.60),
    (-0.20, 1.40), (0.20, 1.40), (-0.25, 1.10), (0.25, 1.10), (-0.25, 0.85), (0.25, 0.85),
    (-0.12, 0.95), (0.12, 0.95), (-0.12, 0.50), (0.12, 0.50), (-0.12, 0.08), (0.12, 0.08),
)  # fmt: skip


class _DictProvider(CalibrationProvider):
    def __init__(self, doc: dict[str, Any]) -> None:
        self._doc = doc

    def current(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._doc))


def _look_at(position: tuple[float, ...], target: tuple[float, ...]) -> tuple[np.ndarray, np.ndarray]:
    z = np.subtract(target, position)
    z = z / np.linalg.norm(z)
    x = np.cross(z, [0.0, 0.0, 1.0])
    x = x / np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.vstack([x, y, z])
    return R, -R @ np.asarray(position, dtype=float)


def calibration_doc(
    coeffs: tuple[float, ...] = WIDE_COEFFS, state: str = "PROVISIONAL", model: str | None = None
) -> dict[str, Any]:
    """카메라 4대 보정 문서. model 이 없으면 계수가 있을 때 OPENCV_PINHOLE, 없을 때 NONE 이다."""
    if model is None:
        model = "OPENCV_PINHOLE" if coeffs else "NONE"
    f = (SIZE[0] / 2) / math.tan(math.radians(HFOV_DEG) / 2)
    cameras = []
    for cid, pos in CORNERS.items():
        R, t = _look_at(pos, (4.5, 3.75, 0.9))
        cameras.append(
            {
                "camera_id": cid,
                "image_size": list(SIZE),
                "K": [[f, 0.0, SIZE[0] / 2], [0.0, f, SIZE[1] / 2], [0.0, 0.0, 1.0]],
                "distortion": {"model": model, "coeffs": list(coeffs)},
                "R": R.tolist(),
                "t": t.tolist(),
            }
        )
    return {
        "schema_version": "1.1.0",
        "calibration_version": "bench-rig-1",
        "provider_source": "SIM",
        "state": state,
        "created_at": T0,
        "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
        "scale_basis": "DATASET_GT",
        "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
        "cameras": cameras,
    }


def make_rig(
    coeffs: tuple[float, ...] = WIDE_COEFFS, state: str = "PROVISIONAL", model: str | None = None
) -> CameraRig:
    return CameraRig.from_provider(_DictProvider(calibration_doc(coeffs, state, model)))


@dataclass
class Person:
    xy: np.ndarray
    yaw: float
    surface_z: float
    velocity: np.ndarray
    arm: np.ndarray
    """관절별 추가 오프셋 (17, 3). 팔·다리 자세 다양성"""

    def joints(self) -> np.ndarray:
        c, s = math.cos(self.yaw), math.sin(self.yaw)
        right = np.array([s, -c, 0.0])  # 보는 방향 (c, s) 의 오른쪽
        base = np.array([self.xy[0], self.xy[1], self.surface_z])
        return np.array([base + dx * right + np.array([0.0, 0.0, dz]) for dx, dz in OFFSETS]) + self.arm


def make_people(n: int, rng: np.random.Generator) -> list[Person]:
    people: list[Person] = []
    while len(people) < n:
        xy = np.array([rng.uniform(*ROOM[0]), rng.uniform(*ROOM[1])])
        if any(np.linalg.norm(xy - p.xy) < MIN_SEPARATION_M for p in people):
            continue
        arm = rng.normal(0.0, 0.04, (len(OFFSETS), 3))
        arm[:5] *= 0.3  # 얼굴 관절은 덜 흔든다
        people.append(
            Person(
                xy=xy,
                yaw=float(rng.uniform(-math.pi, math.pi)),
                surface_z=0.4 if rng.random() < 0.2 else 0.0,
                velocity=rng.normal(0.0, 0.5, 2),
                arm=arm,
            )
        )
    return people


def step_people(people: list[Person], rng: np.random.Generator, dt: float = TICK_MS / 1000.0) -> None:
    for p in people:
        p.xy = p.xy + p.velocity * dt
        for k, (lo, hi) in enumerate(ROOM):
            if not lo <= p.xy[k] <= hi:
                p.velocity[k] = -p.velocity[k]
                p.xy[k] = min(max(p.xy[k], lo), hi)
        p.arm = p.arm + rng.normal(0.0, 0.005, p.arm.shape)


def project(cam: RigCamera, joints: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """World 관절 (J, 3) → 왜곡까지 넣은 원본 픽셀 (J, 2) 와 영상 안·카메라 앞 여부 (J,)."""
    xc = cam.to_camera(joints)
    front = xc[:, 2] > 0.1
    xn = xc[:, :2] / np.where(front, xc[:, 2], 1.0)[:, None]
    uv = cam.ideal_pixels(distortion.distort(xn, cam.distortion_model, cam.distortion_coeffs))
    inside = front & (uv[:, 0] >= 0) & (uv[:, 0] < SIZE[0]) & (uv[:, 1] >= 0) & (uv[:, 1] < SIZE[1])
    return uv, inside


def keypoints_of(
    cam: RigCamera,
    joints: np.ndarray,
    rng: np.random.Generator,
    noise_px: float = 1.0,
    outlier_rate: float = 0.0,
    nan_rate: float = 0.0,
    corner_rate: float = 0.0,
) -> tuple[tuple[float, float, float] | None, ...]:
    """한 카메라의 COCO17 keypoint. 영상 밖·카메라 뒤는 None, 10% 는 신뢰도 기준 미만, 3% 는 None.

    outlier_rate: 이 비율의 관절을 30~80 px 옮긴다(이상치 관측). nan_rate: 이 비율의 관절에 NaN 값을 넣는다.
    corner_rate: 이 비율의 관절을 영상 모서리 40 px 안의 임의 픽셀로 바꾼다(왜곡 식이 접히는 렌즈에서 왜곡 제거 실패).
    """
    uv, inside = project(cam, joints)
    out: list[tuple[float, float, float] | None] = []
    for j in range(len(joints)):
        r = rng.random()
        if not inside[j] or r < 0.03:
            out.append(None)
            continue
        u, v = uv[j] + rng.normal(0.0, noise_px, 2)
        conf = float(rng.uniform(0.0, 0.3)) if r < 0.13 else float(rng.uniform(0.4, 1.0))
        if rng.random() < outlier_rate:
            ang = rng.uniform(0, 2 * math.pi)
            mag = rng.uniform(30.0, 80.0)
            u, v = u + mag * math.cos(ang), v + mag * math.sin(ang)
        if rng.random() < corner_rate:
            u = rng.uniform(0, 40) if rng.random() < 0.5 else SIZE[0] - rng.uniform(0, 40)
            v = rng.uniform(0, 40) if rng.random() < 0.5 else SIZE[1] - rng.uniform(0, 40)
        if rng.random() < nan_rate:
            which = int(rng.integers(0, 3))
            vals = [float(u), float(v), conf]
            vals[which] = math.nan
            out.append(tuple(vals))  # type: ignore[arg-type]
            continue
        out.append((float(u), float(v), conf))
    return tuple(out)


@dataclass
class TickInput:
    t: int
    per_person: list[dict[str, InterpolatedView]]
    """사람마다 카메라 → view (정답 연결)"""
    local_views: list[LocalView]


def tick_input(
    rig: CameraRig,
    people: list[Person],
    t: int,
    rng: np.random.Generator,
    camera_drop_rate: float = 0.0,
    **kwargs: float,
) -> TickInput:
    """camera_drop_rate: 사람·카메라마다 이 비율로 view 를 통째로 뺀다(가림, 2대 관측 관절을 만든다)."""
    per_person: list[dict[str, InterpolatedView]] = []
    local_views: list[LocalView] = []
    for i, p in enumerate(people):
        joints = p.joints()
        views = {}
        for cid, cam in rig.cameras.items():
            kps = keypoints_of(cam, joints, rng, **kwargs)
            if rng.random() < camera_drop_rate:
                continue
            if all(k is None for k in kps):
                continue
            view = InterpolatedView(cid, t, kps, "SIM", 0.0, (t,))
            views[cid] = view
            local_views.append(LocalView(cid, f"{cid}-p{i}", view))
        per_person.append(views)
    return TickInput(t, per_person, local_views)


def others_for(per_person: list[dict[str, InterpolatedView]], i: int) -> dict[str, list[tuple[Any, ...]]]:
    """사람 i 를 뺀 다른 사람들의 카메라별 keypoint (JointGate.update 의 others)."""
    out: dict[str, list[tuple[Any, ...]]] = {}
    for k, views in enumerate(per_person):
        if k == i:
            continue
        for cid, v in views.items():
            out.setdefault(cid, []).append(v.keypoints)
    return out


def _undistort_all(rig: CameraRig, inp: TickInput) -> None:
    by_camera: dict[str, list[Any]] = {}
    for lv in inp.local_views:
        by_camera.setdefault(lv.camera_id, []).append(lv.view.keypoints)
    for cid, keypoints in by_camera.items():
        cam = rig.cameras[cid]
        if hasattr(cam, "undistort_many"):
            cam.undistort_many(keypoints)
        else:  # 묶음 API 이전 구현 비교용: view 마다 한 번
            for kps in keypoints:
                cam.normalize_with_mask(np.array([kp[:2] if kp is not None else (0.0, 0.0) for kp in kps], dtype=float))


def _gate_clusters(frame: Any, rig: CameraRig, gates: dict[str, JointGate]) -> None:
    """Cross-view 클러스터마다 관절 게이트 (World Track Manager 가 할 일의 근사). 게이트는 association_id 마다 둔다."""
    clusters = [c for c in frame.clusters if c.skeleton is not None]
    reuse = all(hasattr(c, "undistorted") for c in frame.clusters)
    for c in clusters:
        others: dict[str, list[Any]] = {}
        others_undistorted: dict[str, list[Any]] = {}
        for o in frame.clusters:
            if o is not c:
                for lv in o.views:
                    others.setdefault(lv.camera_id, []).append(lv.view.keypoints)
                    if reuse:
                        others_undistorted.setdefault(lv.camera_id, []).append(o.undistorted[lv.camera_id])
        gate = gates.setdefault(c.association_id, JointGate())
        views = [lv.view for lv in c.views]
        if reuse:
            reuse_kwargs = {"undistorted": c.undistorted, "others_undistorted": others_undistorted, "skeleton": c.skeleton}
            gate.update(views, rig, others, **reuse_kwargs)
        else:  # 묶음 API 이전 구현 비교용
            gate.update(views, rig, others)


def bench(n_people: int, ticks: int, seed: int, warmup: int = 2) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    rig = make_rig()
    people = make_people(n_people, rng)
    gates = [JointGate() for _ in people]
    associator = CrossViewAssociator(rig)
    pipeline_associator = CrossViewAssociator(rig)
    cluster_gates: dict[str, JointGate] = {}
    stages = ("undistort", "triangulate", "triangulate_batch", "joint_gate", "cross_view", "pipeline")
    times: dict[str, list[float]] = {k: [] for k in stages}
    for k in range(warmup + ticks):
        inp = tick_input(rig, people, T0 + k * TICK_MS, rng)
        step_people(people, rng)

        s = time.perf_counter()
        _undistort_all(rig, inp)
        undistort = time.perf_counter() - s

        s = time.perf_counter()
        for views in inp.per_person:
            if len(views) >= 2:
                triangulate_skeleton(views, rig)
        triangulate = time.perf_counter() - s

        s = time.perf_counter()
        people_views = [views for views in inp.per_person if len(views) >= 2]
        if hasattr(triangulation, "triangulate_skeletons"):
            triangulation.triangulate_skeletons(people_views, rig)
        else:  # 묶음 API 이전 구현 비교용
            for views in people_views:
                triangulate_skeleton(views, rig)
        triangulate_batch = time.perf_counter() - s

        s = time.perf_counter()
        for i, views in enumerate(inp.per_person):
            if views:
                gates[i].update(views, rig, others_for(inp.per_person, i))
        gate = time.perf_counter() - s

        s = time.perf_counter()
        associator.associate(inp.t, inp.local_views)
        cross = time.perf_counter() - s

        s = time.perf_counter()
        frame = pipeline_associator.associate(inp.t, inp.local_views)
        _gate_clusters(frame, rig, cluster_gates)
        pipeline = time.perf_counter() - s

        if k >= warmup:
            for name, value in (
                ("undistort", undistort),
                ("triangulate", triangulate),
                ("triangulate_batch", triangulate_batch),
                ("joint_gate", gate),
                ("cross_view", cross),
                ("pipeline", pipeline),
            ):
                times[name].append(value * 1000.0)
    return {
        "people": n_people,
        "views": 4 * n_people,
        "ticks": ticks,
        "stages_ms": {
            name: {
                "mean": statistics.fmean(v),
                "p95": float(np.percentile(v, 95)),
                "max": max(v),
            }
            for name, v in times.items()
        },
    }


def environment() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "python": sys.version.split()[0],
        "numpy": np.__version__,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--people", type=int, nargs="+", default=[1, 5, 10])
    parser.add_argument("--ticks", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", type=Path, help="결과 JSON 경로")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    env = environment()
    results = [bench(n, args.ticks, args.seed + n) for n in args.people]
    print(
        f"환경: {env['platform']}, {env['processor']}, CPU {env['cpu_count']}, "
        f"Python {env['python']}, numpy {env['numpy']}"
    )
    print()
    print("| 사람 | view | 단계 | 평균 ms | p95 ms | 최대 ms |")
    print("|---:|---:|---|---:|---:|---:|")
    for r in results:
        for name, s in r["stages_ms"].items():
            print(f"| {r['people']} | {r['views']} | {name} | {s['mean']:.1f} | {s['p95']:.1f} | {s['max']:.1f} |")
    if args.json:
        args.json.write_text(json.dumps({"environment": env, "results": results}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
