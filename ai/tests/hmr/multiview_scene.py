"""다시점 집계 시험용 합성 장면 (S15P31A101-87). 정답 포즈를 카메라 4대에 투영해 카메라별 'HMR 결과'와 Tier 0 world_tracks 를 만든다.

- 교실 8 m × 6 m, 네 모서리 2.5 m 높이에서 가운데를 보는 카메라(K 1000, 1920×1080, 배럴 왜곡 k1 −0.2).
- 사람은 리그 좌표(MHR 자산 좌표 = glTF Y-up, m)의 MHR70 관절로 정의하고 World 로는 R_true = Rz(ψ)·Rx(+π/2) 로 돌린다
  (docs/design/avatar-pose-stream.md '똑바로 선 사람'). 카메라 좌표 = R_k·(R_true·k + p) + t_k.
- 'HMR 결과' 는 자기 카메라 좌표 관절에 오차를 더한 것이다: depth_err 는 몸 크기를 두고 엉덩이 중점만 시선 방향으로 (1+ε) 배
  (2D 가 어긋난다), scale_err 는 카메라 중심 기준 전체 (1+s) 배(체형 크기와 깊이가 함께 틀리고 2D 는 맞다, 단안 HMR 의 실제 모양),
  yaw_err 는 몸 방향, joint_noise 는 관절 잡음.
- 메시는 몸을 감싼 상자(정점 8, 면 12)다. 마스크는 정답 상자를 HMR 그림(K_used = K, 왜곡 편 그림)에 래스터한 것이다.
SIM 상한이며 제품 성능이 아니다(AGENTS 규칙 7).
"""

from __future__ import annotations

import math

import numpy as np

from simai_ai.geometry import distortion
from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.hmr.multiview import HmrCandidate
from simai_ai.hmr.prompt import COCO17_TO_MHR70

T0 = 1_792_552_246_000
CAL = "sim-cal-87"
K = ((1000.0, 0.0, 960.0), (0.0, 1000.0, 540.0), (0.0, 0.0, 1.0))
SIZE = (1920, 1080)
DIST = ("OPENCV_PINHOLE", (-0.2, 0.03, 0.0, 0.0, 0.0))
ROOM_CENTER = (4.0, 3.0, 0.9)
CORNERS = {"cam_A": (0.2, 0.2, 2.5), "cam_B": (7.8, 0.2, 2.5), "cam_C": (7.8, 5.8, 2.5), "cam_D": (0.2, 5.8, 2.5)}
FRAME = {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}
PROVENANCE = {
    "frames": {"source": "SIM", "dataset_id": "sim/hmr-multiview-87"},
    "calibration": {"source": "SIM", "calibration_version": CAL},
    "tracks": {"source": "SIM", "provider_version": "sim-87"},
    "is_upper_bound": True,
}

# 리그 좌표 MHR70 관절(Y-up, 사람 앞 = +Z, 사람 왼쪽 = +X). 엉덩이 중점 높이 0.92 m. 쓰지 않는 관절은 손목 근처에 둔다
_BODY = {
    0: (0.0, 1.62, 0.08), 1: (0.03, 1.66, 0.07), 2: (-0.03, 1.66, 0.07), 3: (0.07, 1.64, 0.0), 4: (-0.07, 1.64, 0.0),
    5: (0.19, 1.42, 0.0), 6: (-0.19, 1.42, 0.0), 7: (0.22, 1.12, 0.02), 8: (-0.22, 1.12, 0.02),
    9: (0.10, 0.92, 0.0), 10: (-0.10, 0.92, 0.0), 11: (0.11, 0.50, 0.03), 12: (-0.11, 0.50, 0.03),
    13: (0.11, 0.08, -0.01), 14: (-0.11, 0.08, -0.01),
    15: (0.13, 0.01, 0.17), 16: (0.07, 0.01, 0.15), 17: (0.11, 0.01, -0.06),
    18: (-0.13, 0.01, 0.17), 19: (-0.07, 0.01, 0.15), 20: (-0.11, 0.01, -0.06),
    41: (-0.23, 0.86, 0.08), 62: (0.23, 0.86, 0.08), 69: (0.0, 1.50, 0.0),
}
BOX_RIG = np.array([[x, y, z] for x in (-0.22, 0.22) for y in (0.0, 1.70) for z in (-0.12, 0.18)])
BOX_FACES = np.array([
    [0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1],
    [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3],
])


def look_at(center, target):
    c, tgt = np.asarray(center, float), np.asarray(target, float)
    f = (tgt - c) / np.linalg.norm(tgt - c)
    r = np.cross(f, [0.0, 0.0, 1.0])
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    R = np.vstack([r, d, f])
    return R, -R @ c


def make_rig(distorted: bool = True, calibration_version: str = CAL) -> CameraRig:
    cams = {}
    for cid, center in CORNERS.items():
        R, t = look_at(center, ROOM_CENTER)
        model, coeffs = DIST if distorted else ("NONE", ())
        cams[cid] = RigCamera(cid, PinholeCamera(K=K, R=R.tolist(), t=t.tolist()), SIZE, model, coeffs)
    return CameraRig(calibration_version, "SIM", "PROVISIONAL", cams, {"source": "SIM", "calibration_version": calibration_version})


def body_rig() -> np.ndarray:
    k = np.zeros((70, 3))
    for i in range(70):
        k[i] = _BODY.get(i, _BODY[41] if i < 42 else _BODY[62])
    return k


def rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])


def rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1.0, 0.0, 0.0], [0.0, c, -s], [0.0, s, c]])


class Person:
    """정답 사람 한 명: 바닥 위치 (x, y), 방향 ψ, 높이 dz(작업면 위면 > 0)."""

    def __init__(self, world_id: str, xy, yaw: float, dz: float = 0.0, identity_revision: int = 0):
        self.world_id = world_id
        self.identity_revision = identity_revision
        self.R_true = rot_z(yaw) @ rot_x(math.pi / 2)
        self.origin = np.array([xy[0], xy[1], dz])
        self.k_rig = body_rig()

    def world(self, points_rig=None) -> np.ndarray:
        pts = self.k_rig if points_rig is None else points_rig
        return pts @ self.R_true.T + self.origin

    @property
    def pelvis(self) -> np.ndarray:
        w = self.world()
        return 0.5 * (w[9] + w[10])


def project_original(cam: RigCamera, X_world: np.ndarray) -> np.ndarray:
    Y = X_world @ cam.R.T + cam.t
    xn = Y[:, :2] / Y[:, 2:3]
    xd = distortion.distort(xn, cam.distortion_model, cam.distortion_coeffs)
    return np.column_stack([xd, np.ones(len(xd))]) @ cam.K.T[:, :2]


def bbox_of(px: np.ndarray, margin: float = 0.08):
    x0, y0 = px.min(axis=0)
    x1, y1 = px.max(axis=0)
    mx, my = (x1 - x0) * margin, (y1 - y0) * margin
    return (float(x0 - mx), float(y0 - my), float(x1 + mx), float(y1 + my))


def track_doc(people, rig: CameraRig, *, t: int = T0, cameras=None, pelvis_known=True, anchor_known=True,
              px_noise: float = 0.0, seed: int = 0) -> dict:
    """world_tracks 문서. cameras = {world_id: [camera_id...]} 로 보인 카메라를 정한다(없으면 4대 모두)."""
    rng = np.random.default_rng(seed)
    tracks = []
    for p in people:
        per_camera = []
        w = p.world()
        for cid in (cameras or {}).get(p.world_id, list(rig.cameras)):
            cam = rig.cameras[cid]
            px = project_original(cam, w)
            coco = px[list(COCO17_TO_MHR70)] + rng.normal(scale=px_noise, size=(17, 2))
            per_camera.append({
                "camera_id": cid, "local_track_id": f"{cid}-{p.world_id}", "bbox": list(bbox_of(project_original(cam, p.world(BOX_RIG)))),
                "joint_layout": "COCO17", "keypoints_2d": [[float(u), float(v), 0.9] for u, v in coco],
                "frame_ref": {"stream_epoch": 0, "frame_sequence": 1}, "estimated_capture_time": t, "timestamp_source": "SIM",
            })
        pelvis = p.pelvis
        anchor = [float(pelvis[0]), float(pelvis[1]), float(p.origin[2])]
        tracks.append({
            "world_id": p.world_id, "t_last_valid": t, "observation_state": "OBSERVED", "track_lifecycle": "CONFIRMED",
            "identity_confidence": "HIGH", "identity_revision": p.identity_revision,
            "position_xy": [float(pelvis[0]), float(pelvis[1])], "position_xy_sigma": [0.05, 0.05],
            "pelvis_xyz": [float(v) for v in pelvis] if pelvis_known else None,
            "ground_anchor_xyz": anchor if (anchor_known and pelvis_known) else None,
            "support_surface_id": "floor" if anchor_known else None,
            "support_surface_validity": "VALID" if anchor_known else "UNKNOWN",
            "contact_validity": "VALID" if anchor_known else "UNAVAILABLE",
            "z_validity": "VALID" if pelvis_known else "UNAVAILABLE",
            "head_z": None, "cameras": [o["camera_id"] for o in per_camera], "per_camera": per_camera,
        })
    return {"schema_version": "1.0.0", "t": t, "provider_source": "SIM", "provider_version": "sim-87", "calibration_version": rig.calibration_version,
            "coordinate_frame": FRAME, "tracks": tracks}


def _with_errors(p: Person, cam: RigCamera, points_rig, depth_err: float, scale_err: float, yaw_err: float) -> np.ndarray:
    X_cam = p.world() @ cam.R.T + cam.t
    hip = 0.5 * (X_cam[9] + X_cam[10])
    R_err = cam.R @ rot_z(yaw_err) @ cam.R.T
    P = p.world(points_rig) @ cam.R.T + cam.t
    return ((P - hip) @ R_err.T + hip * (1.0 + depth_err)) * (1.0 + scale_err)


def hmr_candidate(p: Person, cam: RigCamera, *, depth_err: float = 0.0, scale_err: float = 0.0, joint_noise: float = 0.0,
                  yaw_err: float = 0.0, seed: int = 0, candidate_id: str | None = None, bbox=None, **kw) -> HmrCandidate:
    """정답에서 만든 카메라 한 대의 HMR 후보. yaw_err 는 몸 방향 오차(라디안, 상대 관절을 엉덩이 중심으로 돌림)."""
    rng = np.random.default_rng(seed)
    R_err = cam.R @ rot_z(yaw_err) @ cam.R.T
    kp = _with_errors(p, cam, None, depth_err, scale_err, yaw_err) + rng.normal(scale=joint_noise, size=(70, 3))
    cfr = R_err @ cam.R @ p.R_true
    if bbox is None:
        bbox = bbox_of(project_original(cam, p.world(BOX_RIG)))
    return HmrCandidate(camera_id=cam.camera_id, candidate_id=candidate_id or f"{cam.camera_id}:{p.world_id}", bbox=bbox,
                        keypoints_cam=kp, camera_from_rig=cfr, joint_rotations=tuple([0.0, 0.0, 0.0] for _ in range(127)), **kw)


def hmr_vertices(p: Person, cam: RigCamera, depth_err: float = 0.0, scale_err: float = 0.0, yaw_err: float = 0.0) -> np.ndarray:
    """후보와 같은 오차 모형으로 만든 상자 메시 정점(카메라 좌표)."""
    return _with_errors(p, cam, BOX_RIG, depth_err, scale_err, yaw_err)
