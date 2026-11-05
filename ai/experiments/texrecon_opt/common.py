"""texrecon 최적화 실험(S15P31A101-511) 공통: 보정·사진(왜곡 폄), 현재 맵 메시 읽기·단순화, 자유 시점. 실험 전용.
큰 산출물은 저장소 밖 C:/Users/SSAFY/simai-local/texrecon-opt/ 에 둔다."""
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
CAL_DIR = LOCAL / "session12/results/lobby-s12/state/config/bundles"
CAL_VERSION = "cal-lobby-init-f0ab1c6b-jba2-2f6cc097"
BUNDLE = next((LOCAL / f"session12/results/{CAL_VERSION}").glob("bundle-*"))
BASELINE_GLB = LOCAL / "grtmesh-poc/current-scene-textured.glb"
WORK = LOCAL / "texrecon-opt"

# -505 와 같은 자유 시점: 이름 -> (눈, 목표, 수평 시야각)
VIEWS = {"top": ([4.1, 4.2, 9.5], [4.1, 4.15, 0.0], 70.0),
         "obl1": ([4.0, 4.0, 3.6], [0.3, 9.0, 0.3], 80.0),
         "obl2": ([4.0, 4.0, 3.6], [8.2, 1.0, 0.4], 80.0),
         "obl3": ([4.0, 5.0, 3.0], [1.0, 0.2, 0.5], 80.0)}


def load_calibration() -> dict:
    for f in sorted(CAL_DIR.glob("*/calibration.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        p = d.get("payload", d)
        if p.get("calibration_version") == CAL_VERSION:
            return p
    raise SystemExit("calibration not found")


def undistort(image, K, dist):
    """world_frame.prepare.undistort 와 같은 방식(같은 크기, 왜곡 0, 새 K). 렌즈 왜곡(k1≈-0.29)을 편다."""
    h, w = image.shape[:2]
    newK, _ = cv2.getOptimalNewCameraMatrix(K, dist, (w, h), 0, (w, h), centerPrincipalPoint=True)
    mx, my = cv2.initUndistortRectifyMap(K, dist, None, newK, (w, h), cv2.CV_32FC1)
    return cv2.remap(image, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE), newK


def load_cameras() -> list[ge.CheckCamera]:
    """왜곡을 편 사람 지운 사진 + 펴진 K + 유효 마스크. 2304x1296."""
    cal = load_calibration()
    cams = []
    for c in cal["cameras"]:
        cid = c["camera_id"]
        bgr = cv2.imread(str(BUNDLE / f"{cid}.jpg"))
        K = np.asarray(c["K"], float)
        dist = np.asarray(c["distortion"]["coeffs"], float)
        und, newK = undistort(bgr, K, dist)
        vm = cv2.imread(str(BUNDLE / f"{cid}.valid.png"), cv2.IMREAD_GRAYSCALE)
        vu, _ = undistort(vm, K, dist)
        cams.append(ge.CheckCamera(cid, newK, np.asarray(c["R"], float), np.asarray(c["t"], float).reshape(3),
                                   np.ascontiguousarray(und[:, :, ::-1]), vu > 127))
    return cams


def cam2world(cam) -> np.ndarray:
    T = np.eye(4)
    T[:3, :3] = np.asarray(cam.R).T
    T[:3, 3] = -np.asarray(cam.R).T @ np.asarray(cam.t).reshape(3)
    return T


def load_mesh(path: Path = BASELINE_GLB):
    """현재 맵 GLB 에서 메시(관측 + 미관측 면, 기존 추정 바닥 평면 제외)를 하나로 합쳐 (V, F) World m 으로."""
    g = ge.parse_glb(Path(path).read_bytes())
    simai = (g.doc.get("extras") or {}).get("simai")
    if isinstance(simai, dict):
        simai.pop("enhanced", None)  # 읽기 전용: 표식을 메모리에서만 뗀다
    model = ge.load_scene(g)
    Vs, Fs, off = [], [], 0
    for p in model.prims:
        if p.name == "estimated_floor_plane":
            continue
        Vs.append(p.pos)
        Fs.append(p.idx + off)
        off += len(p.pos)
    V, F = np.concatenate(Vs), np.concatenate(Fs)
    key = np.round(V / 1e-5).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    V = V[first]
    F = inv.reshape(-1)[F]
    keep = (F[:, 0] != F[:, 1]) & (F[:, 1] != F[:, 2]) & (F[:, 0] != F[:, 2])
    return V, F[keep]


def simplify(V, F, target: int):
    """Open3D 이차 오차 단순화(운영 fuse_mesh 와 같은 방식)."""
    import open3d as o3d

    if len(F) <= target:
        return V, F
    m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(V), o3d.utility.Vector3iVector(F))
    m = m.simplify_quadric_decimation(int(target))
    for f in (m.remove_degenerate_triangles, m.remove_duplicated_triangles, m.remove_duplicated_vertices,
              m.remove_non_manifold_edges, m.remove_unreferenced_vertices):
        f()
    return np.asarray(m.vertices, float), np.asarray(m.triangles, np.int64)


def lookat_camera(eye, target, W=1280, H=720, hfov_deg=75.0, name="free") -> ge.CheckCamera:
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    f = target - eye
    f /= np.linalg.norm(f)
    up = np.array([0, 0, 1.0])
    if abs(f @ up) > 0.98:
        up = np.array([0, 1.0, 0])
    r = np.cross(f, up)
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    R = np.stack([r, d, f])
    fx = (W / 2) / np.tan(np.radians(hfov_deg) / 2)
    K = np.array([[fx, 0, W / 2], [0, fx, H / 2], [0, 0, 1.0]])
    return ge.CheckCamera(name, K, R, -R @ eye, np.zeros((H, W, 3), np.uint8), None)
