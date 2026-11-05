"""자유 시점 카메라 만들기(World +Z 상방, OpenCV 축)."""
from __future__ import annotations
import numpy as np
import common as C

ge = C.ge


def lookat_camera(eye, target, W=1280, H=720, hfov_deg=75.0, name="free") -> "ge.CheckCamera":
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    f = (target - eye); f /= np.linalg.norm(f)
    up = np.array([0, 0, 1.0])
    if abs(f @ up) > 0.98:
        up = np.array([0, 1.0, 0])  # 거의 수직으로 내려다볼 때
    r = np.cross(f, up); r /= np.linalg.norm(r)
    d = np.cross(f, r)  # OpenCV y = 아래
    R = np.stack([r, d, f])
    t = -R @ eye
    fx = (W / 2) / np.tan(np.radians(hfov_deg) / 2)
    K = np.array([[fx, 0, W / 2], [0, fx, H / 2], [0, 0, 1.0]])
    return ge.CheckCamera(name, K, R, t, np.zeros((H, W, 3), np.uint8), None)
