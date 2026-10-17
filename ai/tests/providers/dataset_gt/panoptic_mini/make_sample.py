"""CMU Panoptic 형식을 흉내 낸 합성 표본을 만든다 (실제 데이터에서 가져온 값 없음).

`ai/` 에서 `uv run python tests/providers/dataset_gt/panoptic_mini/make_sample.py` 로 이 디렉터리에 다시 쓴다.
같은 코드면 같은 파일이 나온다.

- 좌표는 Panoptic 돔 규약(센티미터, +Y 아래, 바닥 y = 0), 카메라는 x_cam = R·X + t.
- HD 카메라 3대(00_00~00_02)는 높이 2 m, 반지름 3 m 원 위에서 가운데를 본다.
  HD 00_03 은 머리 쪽을 가까이 당겨 본다(발은 화면 밖): 관절별 2대 규칙 시험용.
  VGA 01_01 은 어댑터가 쓰지 않는 카메라다.
- 프레임 100, 101, 103 (102 없음). 사람 0 은 걷고, 사람 1 은 왼손목(신뢰도 0.05)·오른귀(−1)가 무효다.
  프레임 103 의 사람 2 는 BodyCenter 가 무효라 tracks 에 들어가지 않아야 한다.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

HERE = Path(__file__).resolve().parent

# 서 있는 사람의 joints19 (dx, y, dz) cm. 순서: Neck, Nose, BodyCenter, lShoulder, lElbow, lWrist, lHip, lKnee,
# lAnkle, rShoulder, rElbow, rWrist, rHip, rKnee, rAnkle, lEye, lEar, rEye, rEar
TEMPLATE = [
    (0, -150, 0),
    (0, -162, 8),
    (0, -95, 0),
    (18, -148, 0),
    (22, -120, 0),
    (24, -95, 0),
    (10, -95, 0),
    (11, -50, 0),
    (12, -8, 0),
    (-18, -148, 0),
    (-22, -120, 0),
    (-24, -95, 0),
    (-10, -95, 0),
    (-11, -50, 0),
    (-12, -8, 0),
    (3, -166, 7),
    (7, -164, 0),
    (-3, -166, 7),
    (-7, -164, 0),
]


def _norm(v):
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


def _cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]


def _look_at(center, target):
    """OpenCV 카메라(x 오른쪽, y 아래, z 앞)의 R·t. Panoptic 의 아래는 +Y."""
    z = _norm([t - c for t, c in zip(target, center)])
    down = [0.0, 1.0, 0.0]
    d = sum(a * b for a, b in zip(down, z))
    y = _norm([a - d * b for a, b in zip(down, z)])
    x = _cross(y, z)
    R = [x, y, z]
    t = [-sum(R[i][k] * center[k] for k in range(3)) for i in range(3)]
    return [[round(v, 10) for v in row] for row in R], [[round(v, 8)] for v in t]


def _camera(kind, name, center, target, f, resolution, dist):
    R, t = _look_at(center, target)
    w, h = resolution
    return {
        "name": name,
        "type": kind,
        "resolution": list(resolution),
        "panel": 0 if kind == "hd" else int(name[:2]),
        "node": int(name[3:]),
        "K": [[f, 0, w / 2], [0, f, h / 2], [0, 0, 1]],
        "distCoef": dist,
        "R": R,
        "t": t,
    }


def calibration():
    cams = []
    for i in range(3):
        a = math.radians(90 + 120 * i)
        center = [300 * math.cos(a), -200.0, 300 * math.sin(a)]
        cams.append(
            _camera("hd", f"00_{i:02d}", center, [0, -90, 0], 1100.0, (1920, 1080), [-0.05, 0.01, 0.0002, -0.0001, 0.0])
        )
    cams.append(_camera("hd", "00_03", [0.0, -170.0, 300.0], [0, -150, 0], 2400.0, (1920, 1080), [0, 0, 0, 0, 0]))
    cams.insert(0, _camera("vga", "01_01", [0.0, -250.0, -280.0], [0, -90, 0], 750.0, (640, 480), [-0.3, 0.1, 0, 0, 0]))
    return {"calibDataSource": "synthetic", "cameras": cams}


def _body(body_id, x, z, conf=None):
    conf = conf or {}
    flat = []
    for j, (dx, y, dz) in enumerate(TEMPLATE):
        flat += [round(x + dx, 4), float(y), round(z + dz, 4), conf.get(j, 0.8)]
    return {"id": body_id, "joints19": flat}


def scenes():
    out = {}
    for frame in (100, 101, 103):
        step = frame - 100
        bodies = [_body(0, -30.0 + 4.0 * step, 20.0), _body(1, 80.0, -60.0, {5: 0.05, 18: -1.0})]
        if frame == 103:
            bodies.append(_body(2, -90.0, -40.0, {2: -1.0}))
        out[frame] = {
            "version": 0.7,
            "univTime": round(1000.0 + frame * 1001 / 30, 3),
            "fpsType": "hd_29_97",
            "bodies": bodies,
        }
    return out


def main():
    (HERE / "calibration_mini.json").write_text(json.dumps(calibration(), indent=1) + "\n", encoding="utf-8")
    pose_dir = HERE / "hdPose3d_stage1_coco19"
    pose_dir.mkdir(exist_ok=True)
    for frame, scene in scenes().items():
        (pose_dir / f"body3DScene_{frame:08d}.json").write_text(json.dumps(scene) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
