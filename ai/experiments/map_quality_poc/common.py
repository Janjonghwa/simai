"""map-quality PoC 공통(grtmesh PoC common 에서 복사): 보정·사진 읽기, GLB/OBJ 를 Prim 으로, 깊이 렌더. glb_enhance 의 렌더러를 그대로 쓴다(PoC 전용, 운영 코드 무변경)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

AI_SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(AI_SRC))
from simai_ai.scene import glb_enhance as ge  # noqa: E402

LOCAL = Path("C:/Users/SSAFY/simai-local")
BUNDLE = next((LOCAL / "session12/results/cal-lobby-init-f0ab1c6b-jba2-2f6cc097").glob("bundle-*"))
CAL_DIR = LOCAL / "session12/results/lobby-s12/state/config/bundles"
CAL_VERSION = "cal-lobby-init-f0ab1c6b-jba2-2f6cc097"
WORK = LOCAL / "map-quality-poc"
BASELINE_GLB = LOCAL / "grtmesh-poc/current-scene-textured.glb"


def load_calibration() -> dict:
    for f in sorted(CAL_DIR.glob("*/calibration.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        p = d.get("payload", d)
        if p.get("calibration_version") == CAL_VERSION:
            return p
    raise SystemExit("calibration not found")


def undistort(image, K, dist):
    """prepare.undistort 와 같은 방식(같은 크기, 왜곡 0, 새 K)."""
    h, w = image.shape[:2]
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), 0, (w, h), centerPrincipalPoint=True)
    mx, my = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_32FC1)
    return cv2.remap(image, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE), newK


def load_cameras() -> list[ge.CheckCamera]:
    """왜곡을 편 사람 지운 사진 + 펴진 K + 유효 마스크(사진도 같이 펴서). 2304x1296."""
    cal = load_calibration()
    cams = []
    for c in cal["cameras"]:
        cid = c["camera_id"]  # cam_A
        bgr = cv2.imread(str(BUNDLE / f"{cid}.jpg"))
        K = np.asarray(c["K"], float)
        dist = np.asarray(c["distortion"]["coeffs"], float)
        und, newK = undistort(bgr, K, dist)
        vm = cv2.imread(str(BUNDLE / f"{cid}.valid.png"), cv2.IMREAD_GRAYSCALE)
        vu, _ = undistort(vm, K, dist)
        cams.append(ge.CheckCamera(cid, newK, np.asarray(c["R"], float), np.asarray(c["t"], float).reshape(3),
                                   np.ascontiguousarray(und[:, :, ::-1]), vu > 127))
    return cams


def load_glb(path: Path):
    g = ge.parse_glb(Path(path).read_bytes())
    simai = (g.doc.get('extras') or {}).get('simai')
    if isinstance(simai, dict):
        simai.pop('enhanced', None)  # 현재 운영 GLB 는 이미 보강본이다. 읽기만 하므로 표식을 메모리에서만 뗀다.
    model = ge.load_scene(g)
    return g, model


def load_obj_prim(obj: Path, tex: Path):
    """grtmesh OBJ(+jpg 아틀라스) -> (g, model, prims). v 는 아래→위라 1-v 로 glTF 규약에 맞춘다. 삼각형별 uv 라 정점을 분리한다."""
    V, VT, F, FT = [], [], [], []
    for line in Path(obj).read_text().splitlines():
        if line.startswith("v "):
            V.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("vt "):
            VT.append([float(x) for x in line.split()[1:3]])
        elif line.startswith("f "):
            parts = [p.split("/") for p in line.split()[1:]]
            for k in range(1, len(parts) - 1):
                tri = [parts[0], parts[k], parts[k + 1]]
                F.append([int(p[0]) - 1 for p in tri])
                FT.append([int(p[1]) - 1 for p in tri])
    V, VT, F, FT = np.array(V), np.array(VT), np.array(F), np.array(FT)
    pos = V[F].reshape(-1, 3)
    uv = VT[FT].reshape(-1, 2).copy()
    uv[:, 1] = 1.0 - uv[:, 1]
    idx = np.arange(len(pos)).reshape(-1, 3)
    img = cv2.imread(str(tex))
    prim = ge.Prim("grtmesh", "observed", pos, idx, uv=uv, image=0)
    model = ge.SceneModel([prim], {0: np.ascontiguousarray(img[:, :, ::-1])})
    return None, model, len(F), img.shape


def render_depth(prims, cam, shape_hw, K_scaled, near=0.05):
    """카메라 z 깊이(m) float32, 덮이지 않은 곳 0."""
    H, W = shape_hw
    pos, uv, col, pid = ge._flatten(prims)
    R, t = np.asarray(cam.R, float), np.asarray(cam.t, float).reshape(3)
    for _ in range(7):
        z = pos @ R[2] + t[2]
        crossing = (z.min(1) < near) & (z.max(1) >= near)
        if not crossing.any():
            break
        keep = ~crossing
        n_pos, n_uv, n_col, n_pid = ge._split4(pos[crossing], uv[crossing], col[crossing], pid[crossing])
        pos, uv, col, pid = (np.concatenate([a[keep], b]) for a, b in ((pos, n_pos), (uv, n_uv), (col, n_col), (pid, n_pid)))
    cp = pos @ R.T + t
    front = cp[:, :, 2].min(1) >= near
    cp = cp[front]
    zc = cp[:, :, 2]
    u = K_scaled[0, 0] * cp[:, :, 0] / zc + K_scaled[0, 2] + 0.5
    v = K_scaled[1, 1] * cp[:, :, 1] / zc + K_scaled[1, 2] + 0.5
    tid, lam, depth = ge.rasterize(np.stack([u, v], axis=2), zc, (H, W), perspective=True)
    depth = np.where(tid >= 0, depth, 0.0).astype(np.float32)
    return depth
