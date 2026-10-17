"""CMU Panoptic Studio 정답 → CalibrationProvider·WorldTrackProvider (DATASET_GT, 명세 20.4).

입력은 공식 panoptic-toolbox 의 `scripts/getData.sh` 가 내려받는 배치 그대로다.

    <root>/calibration_<seq>.json
    <root>/hdPose3d_stage1_coco19/body3DScene_<HD 프레임 번호 8자리>.json

## 좌표 변환 (데이터셋 → World)

| | Panoptic 돔 좌표 | World (명세 4.1) |
|---|---|---|
| 단위 | 센티미터 | 미터 |
| 손잡이 | 오른손 | 오른손 |
| 상방 | −Y (+Y 가 중력 방향) | +Z |
| 바닥 | y = floor_y_cm (데이터셋에 바닥 정답 없음) | Z = 0 |

    X_world = A · (X_panoptic − (0, floor_y_cm, 0)) / 100,   A = [[1, 0, 0], [0, 0, 1], [0, −1, 0]]

즉 (x, y, z) → (x, z, floor_y_cm − y) / 100 이다. det(A) = +1 이라 손잡이가 유지된다.
Panoptic 카메라도 OpenCV 규약(x_cam = R·X + t, 센티미터)이므로 카메라 축은 그대로 두고
R_w = R·Aᵀ, t_w = (R·(0, floor_y_cm, 0) + t) / 100 (미터)로 바꾼다.

바닥 높이: Panoptic 은 바닥 평면 정답을 주지 않는다. 기본 floor_y_cm = 0 은 171204_pose1 실측 근거다
(발목 관절 y 의 중앙값 −8.2 cm, p95 −7.2 cm 로 발목이 y = 0 위 약 8 cm). 다른 시퀀스는 발목 높이로 확인하고,
다르면 floor_y_cm 를 명시한다. 이 값이 바뀌면 calibration_version 도 바뀐다.

## 관절 (COCO19 → COCO17)

Panoptic joints19 순서: Neck, Nose, BodyCenter(엉덩이 중심), lShoulder, lElbow, lWrist, lHip, lKnee, lAnkle,
rShoulder, rElbow, rWrist, rHip, rKnee, rAnkle, lEye, lEar, rEye, rEar. 각 관절은 [x, y, z, confidence] 다.
COCO17 관절은 모두 있으므로 순서만 바꾼다. Neck 은 쓰지 않고 BodyCenter 는 pelvis_xyz 로 쓴다.
신뢰도가 min_joint_confidence 이하인 관절은 null 이다(지어내지 않음).

## 정답을 Tier 0 와 같은 의미로 옮기는 규칙 (명세 20.4 규칙 1, 5.2)

- 관절은 선택한 카메라 2대 이상의 화면 안(카메라 앞)에 투영될 때만 채운다. 정답에는 카메라별 가림 정보가 없어
  가림은 반영하지 않는다(화면 안이면 보인 것으로 친다).
- 골반(BodyCenter)이 없거나 골반이 화면 안에 투영되는 카메라가 2대 미만인 사람은 그 시각 tracks 에 넣지 않는다.
- 접지·지지 표면 정답이 없으므로 ground_anchor_xyz 는 비운다(support UNKNOWN, contact UNAVAILABLE).
  Z=0 으로 채우지 않는다. head_z 는 머리 꼭대기 관절이 없어 비운다.
- per_camera.bbox 는 그 카메라 화면 안에 투영된 관절의 외접 사각형이다(살·머리 꼭대기 미포함).
  2D keypoint 는 내지 않는다(가림을 모르는 투영값을 관측처럼 보이게 하지 않기 위함).
- 시각: 데이터셋에 UTC 시각이 없으므로 t = epoch_ms + round(HD 프레임 번호 × 1001/30 ms) 다.
  epoch_ms 는 임의 원점이며 실제 촬영 시각이 아니다.

보정은 DATASET_GT 라 CALIBRATED 가 될 수 없고 PROVISIONAL 로 낸다(명세 20.4 규칙 4).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.providers.base import CalibrationDoc, CalibrationProvider, WorldTrackProvider, WorldTracksDoc
from simai_ai.providers.contract import CALIBRATION_SCHEMA_VERSION, MIN_TRIANGULATION_CAMERAS

ADAPTER_VERSION = "panoptic-adapter-0.1.0"
CONTRACTS_VERSION = "1.1.0"
SOURCE = "DATASET_GT"

# 데이터셋에 UTC 시각이 없어 쓰는 임의 시각 원점 (contracts/examples 와 같은 값). 실제 촬영 시각이 아니다.
DEFAULT_EPOCH_MS = 1760342400000
# HD 29.97 fps (fpsType hd_29_97). 171204_pose1 의 univTime 간격 실측 33.366~33.367 ms 와 맞다.
HD_FPS_TYPE = "hd_29_97"
HD_FRAME_MS = 1001 / 30
# 영상 위치(frame_ref.file_uri). 영상은 저장소에 넣지 않고 Network Volume 에 둔다.
DEFAULT_VIDEO_URI_ROOT = "volume://AP-JP-1/datasets/cmu_panoptic"
POSE_DIR = "hdPose3d_stage1_coco19"

CM_PER_M = 100.0
# Panoptic 축 → World 축 (행 우선, det = +1)
PANOPTIC_TO_WORLD_AXES = ((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0))

COCO19_JOINTS = 19
BODY_CENTER = 2
# COCO17 순서의 각 관절이 joints19 의 몇 번째인지
COCO17_FROM_COCO19 = (1, 15, 17, 16, 18, 3, 9, 4, 10, 5, 11, 6, 12, 7, 13, 8, 14)
# 낮은 신뢰도 관절을 버리는 문턱 (초기값)
DEFAULT_MIN_JOINT_CONFIDENCE = 0.1
# 왜곡 다항식은 시야 밖 먼 점을 화면 안으로 접어 넣을 수 있다. 왜곡 전 투영이 화면을 이 비율 넘게 벗어나면 화면 밖이다.
UNDISTORTED_MARGIN = 0.25

ROUND_M = 5
ROUND_PX = 2

_FRAME_FILE = re.compile(r"^body3DScene_(\d+)\.json$")
_CALIBRATION_FILE = re.compile(r"^calibration_(.+)\.json$")

Vec3 = tuple[float, float, float]


def _r(x: float, digits: int) -> float:
    return round(x, digits) + 0.0  # -0.0 을 0.0 으로


def camera_id_of(kind: str, name: str) -> str:
    """Panoptic 카메라(type, name) → 계약의 camera_id. 예: ("hd", "00_03") → "hd_00_03"."""
    return f"{kind}_{name}"


def panoptic_to_world(p_cm: Sequence[float], floor_y_cm: float = 0.0) -> Vec3:
    """Panoptic 돔 좌표(cm, +Y 아래) → World(m, +Z 위, 바닥 Z=0). (x, y, z) → (x, z, floor_y_cm − y) / 100."""
    x, y, z = (float(v) for v in p_cm[:3])
    return (x / CM_PER_M, z / CM_PER_M, (floor_y_cm - y) / CM_PER_M)


def convert_extrinsics(
    R: Sequence[Sequence[float]], t_cm: Sequence[float], floor_y_cm: float = 0.0
) -> tuple[list[list[float]], list[float]]:
    """Panoptic 외부 파라미터(x_cam = R·X + t, cm) → World 기준(R_w = R·Aᵀ, t_w = (R·o + t)/100, m)."""
    A = PANOPTIC_TO_WORLD_AXES
    R_w = [[sum(R[i][k] * A[j][k] for k in range(3)) for j in range(3)] for i in range(3)]
    o = (0.0, floor_y_cm, 0.0)
    t_w = [(sum(R[i][k] * o[k] for k in range(3)) + t_cm[i]) / CM_PER_M for i in range(3)]
    return R_w, t_w


@dataclass(frozen=True)
class _Camera:
    camera_id: str
    pinhole: PinholeCamera
    coeffs: tuple[float, ...]
    width: int
    height: int

    def project(self, p_world: Vec3) -> tuple[float, float] | None:
        """World 점 → 왜곡을 적용한 픽셀. 카메라 뒤이거나 화면 밖이면 None."""
        xc, yc, zc = self.pinhole.world_to_camera(p_world)
        if zc <= 0.0:
            return None
        x, y = xc / zc, yc / zc
        (fx, s, cx), (_, fy, cy), _ = self.pinhole.K
        mw, mh = self.width * UNDISTORTED_MARGIN, self.height * UNDISTORTED_MARGIN
        u0, v0 = fx * x + s * y + cx, fy * y + cy
        if not (-mw <= u0 <= self.width + mw and -mh <= v0 <= self.height + mh):
            return None
        k1, k2, p1, p2, k3 = self.coeffs
        r2 = x * x + y * y
        radial = 1.0 + k1 * r2 + k2 * r2 * r2 + k3 * r2 * r2 * r2
        xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
        yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
        u, v = fx * xd + s * yd + cx, fy * yd + cy
        if not (0.0 <= u < self.width and 0.0 <= v < self.height):
            return None
        return (u, v)


class PanopticCalibrationProvider(CalibrationProvider):
    """`calibration_<seq>.json` 의 HD 카메라를 World 기준 보정 문서로 낸다.

    camera_ids 는 "hd_00_03" 처럼 쓰고, 주지 않으면 HD 카메라 전부다. VGA·Kinect 카메라는 다루지 않는다
    (3D 정답이 HD 프레임 기준이다). calibration_version 은 원본 파일 해시·카메라 목록·바닥 높이·어댑터 버전으로 정한다.
    """

    def __init__(
        self,
        root: str | Path,
        camera_ids: Sequence[str] | None = None,
        *,
        sequence: str | None = None,
        floor_y_cm: float = 0.0,
        epoch_ms: int = DEFAULT_EPOCH_MS,
    ) -> None:
        self.root = Path(root)
        self.sequence, path = _find_calibration(self.root, sequence)
        raw_bytes = path.read_bytes()
        raw = json.loads(raw_bytes)
        hd = {camera_id_of(c["type"], c["name"]): c for c in raw["cameras"] if c.get("type") == "hd"}
        ids = sorted(hd) if camera_ids is None else list(camera_ids)
        unknown = [cid for cid in ids if cid not in hd]
        if unknown or not ids:
            raise ValueError(f"{path}: HD 카메라에 없는 camera_id {unknown} (있는 것: {sorted(hd)})")
        if len(set(ids)) != len(ids):
            raise ValueError(f"camera_ids 중복: {ids}")

        self.floor_y_cm = float(floor_y_cm)
        self.epoch_ms = int(epoch_ms)
        self.source_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        cameras = [self._convert(cid, hd[cid]) for cid in ids]
        key = {"source": self.source_sha256, "cameras": ids, "floor_y_cm": self.floor_y_cm, "adapter": ADAPTER_VERSION}
        digest = hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()
        self._doc: CalibrationDoc = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_version": f"panoptic-{self.sequence}-{digest[:12]}",
            "provider_source": SOURCE,
            "provider_version": ADAPTER_VERSION,
            "state": "PROVISIONAL",
            "created_at": self.epoch_ms,
            "coordinate_frame": {
                "frame": "WORLD",
                "unit": "m",
                "handedness": "right",
                "up_axis": "+Z",
                "site_id": f"cmu_panoptic/{self.sequence}",
            },
            "scale_basis": "DATASET_GT",
            "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
            "cameras": cameras,
        }
        self._cameras = tuple(
            _Camera(
                c["camera_id"],
                PinholeCamera.from_calibration(c),
                tuple(c["distortion"]["coeffs"]),
                c["image_size"][0],
                c["image_size"][1],
            )
            for c in cameras
        )

    def _convert(self, cid: str, cam: Mapping[str, Any]) -> dict[str, Any]:
        if len(cam["distCoef"]) != 5:
            raise ValueError(f"{cid}: distCoef 는 OpenCV 5계수 [k1, k2, p1, p2, k3] 여야 한다 ({cam['distCoef']})")
        t_cm = [float(v[0]) if isinstance(v, list) else float(v) for v in cam["t"]]
        R_w, t_w = convert_extrinsics(cam["R"], t_cm, self.floor_y_cm)
        # 카메라 중심 C = −R_wᵀ·t_w (회전 행렬이므로 전치가 역행렬)
        height = -sum(R_w[i][2] * t_w[i] for i in range(3))
        return {
            "camera_id": cid,
            "image_size": [int(cam["resolution"][0]), int(cam["resolution"][1])],
            "K": [[float(v) for v in row] for row in cam["K"]],
            "distortion": {"model": "OPENCV_PINHOLE", "coeffs": [float(v) for v in cam["distCoef"]]},
            "R": R_w,
            "t": t_w,
            "camera_height_m": _r(height, ROUND_M),
        }

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(c.camera_id for c in self._cameras)

    @property
    def projection_cameras(self) -> tuple[_Camera, ...]:
        """World 점을 왜곡을 적용한 픽셀로 투영하는 카메라 (camera_ids 순서)."""
        return self._cameras

    def current(self) -> CalibrationDoc:
        # 호출한 쪽이 고쳐도 다음 호출 결과가 바뀌지 않게 복사본을 준다.
        return json.loads(json.dumps(self._doc))


class PanopticWorldTrackProvider(WorldTrackProvider):
    """`hdPose3d_stage1_coco19/body3DScene_*.json` 을 HD 프레임 순서로 읽어 World 트랙 문서를 낸다.

    보정(카메라 선택, 바닥 높이, 시각 원점)은 calibration 과 같은 것을 쓴다. 규칙은 모듈 설명을 본다.
    """

    def __init__(
        self,
        calibration: PanopticCalibrationProvider,
        *,
        pose_dir: str | Path | None = None,
        min_joint_confidence: float = DEFAULT_MIN_JOINT_CONFIDENCE,
        video_uri_root: str = DEFAULT_VIDEO_URI_ROOT,
    ) -> None:
        self.calibration = calibration
        self.pose_dir = Path(pose_dir) if pose_dir is not None else calibration.root / POSE_DIR
        self.min_joint_confidence = float(min_joint_confidence)
        self.video_uri_root = video_uri_root.rstrip("/")
        files = []
        for path in self.pose_dir.glob("body3DScene_*.json"):
            m = _FRAME_FILE.match(path.name)
            if m:
                files.append((int(m.group(1)), path))
        if not files:
            raise ValueError(f"{self.pose_dir}: body3DScene_*.json 이 없다")
        self._files = sorted(files)
        cal = calibration.current()
        self._calibration_version = cal["calibration_version"]
        self._coordinate_frame = cal["coordinate_frame"]

    @property
    def source(self) -> str:
        return SOURCE

    @property
    def provider_version(self) -> str:
        return ADAPTER_VERSION

    @property
    def frame_indices(self) -> tuple[int, ...]:
        return tuple(i for i, _ in self._files)

    def time_of(self, frame_index: int) -> int:
        """HD 프레임 번호 → 문서 시각 t (UTC epoch ms, 임의 원점)."""
        return self.calibration.epoch_ms + round(frame_index * HD_FRAME_MS)

    def frames(self) -> Iterator[WorldTracksDoc]:
        for frame_index, path in self._files:
            scene = json.loads(path.read_text(encoding="utf-8"))
            if scene.get("fpsType", HD_FPS_TYPE) != HD_FPS_TYPE:
                raise ValueError(f"{path}: fpsType {scene.get('fpsType')} 은 지원하지 않는다 ({HD_FPS_TYPE} 만)")
            yield self.document(frame_index, scene)

    def document(self, frame_index: int, scene: Mapping[str, Any]) -> WorldTracksDoc:
        """body3DScene 한 개 → world_tracks 문서 한 개."""
        t = self.time_of(frame_index)
        tracks = []
        for body in sorted(scene.get("bodies", []), key=lambda b: b["id"]):
            track = self._track(t, frame_index, body)
            if track is not None:
                tracks.append(track)
        return {
            "schema_version": CONTRACTS_VERSION,
            "t": t,
            "provider_source": SOURCE,
            "provider_version": ADAPTER_VERSION,
            "calibration_version": self._calibration_version,
            "coordinate_frame": dict(self._coordinate_frame),
            "tracks": tracks,
        }

    def _world_joints(self, body: Mapping[str, Any]) -> list[Vec3 | None]:
        flat = body["joints19"]
        if len(flat) != 4 * COCO19_JOINTS:
            raise ValueError(f"body {body.get('id')}: joints19 는 값 {4 * COCO19_JOINTS}개여야 한다 ({len(flat)}개)")
        floor = self.calibration.floor_y_cm
        out: list[Vec3 | None] = []
        for j in range(COCO19_JOINTS):
            x, y, z, c = flat[4 * j : 4 * j + 4]
            out.append(panoptic_to_world((x, y, z), floor) if c > self.min_joint_confidence else None)
        return out

    def _track(self, t: int, frame_index: int, body: Mapping[str, Any]) -> dict[str, Any] | None:
        joints = self._world_joints(body)
        pelvis = joints[BODY_CENTER]
        if pelvis is None:
            return None
        projections = {
            cam.camera_id: [cam.project(p) if p is not None else None for p in joints]
            for cam in self.calibration.projection_cameras
        }
        per_camera = []
        for cam in self.calibration.projection_cameras:
            uv = projections[cam.camera_id]
            if uv[BODY_CENTER] is None:
                continue
            bbox = _bbox([p for p in uv if p is not None])
            if bbox is None:
                continue
            per_camera.append(
                {
                    "camera_id": cam.camera_id,
                    "local_track_id": f"gt-{body['id']}",
                    "bbox": bbox,
                    "frame_ref": {
                        "file_uri": f"{self.video_uri_root}/{self.calibration.sequence}/hdVideos/{cam.camera_id}.mp4",
                        "frame_index": frame_index,
                    },
                    "estimated_capture_time": t,
                    "timestamp_source": "FILE",
                }
            )
        cameras = [pc["camera_id"] for pc in per_camera]
        if len(cameras) < MIN_TRIANGULATION_CAMERAS:
            return None

        coco: list[list[float] | None] = []
        for j19 in COCO17_FROM_COCO19:
            p = joints[j19]
            views = sum(1 for cid in cameras if projections[cid][j19] is not None)
            coco.append([_r(v, ROUND_M) for v in p] if p is not None and views >= MIN_TRIANGULATION_CAMERAS else None)
        seen = [p is not None for p in coco]
        pelvis_xyz = [_r(v, ROUND_M) for v in pelvis]
        return {
            "world_id": f"w-{body['id']:04d}",
            "t_last_valid": t,
            "observation_state": "OBSERVED",
            "track_lifecycle": "CONFIRMED",
            "identity_confidence": "HIGH",
            "identity_revision": 0,
            "position_xy": pelvis_xyz[:2],
            "position_xy_sigma": [0.0, 0.0],
            "pelvis_xyz": pelvis_xyz,
            "ground_anchor_xyz": None,
            "support_surface_id": None,
            "support_surface_validity": "UNKNOWN",
            "contact_validity": "UNAVAILABLE",
            "z_validity": "UNCERTAIN",
            "head_z": None,
            "cameras": cameras,
            "skeleton": {
                "joint_layout": "COCO17",
                "joints": coco,
                "visible": seen,
                "validity": ["VALID" if s else "UNAVAILABLE" for s in seen],
                "joint_source": ["observed"] * len(coco),
            },
            "per_camera": per_camera,
        }


def _bbox(points: Sequence[tuple[float, float]]) -> list[float] | None:
    us, vs = [p[0] for p in points], [p[1] for p in points]
    box = [_r(min(us), ROUND_PX), _r(min(vs), ROUND_PX), _r(max(us), ROUND_PX), _r(max(vs), ROUND_PX)]
    return box if box[0] < box[2] and box[1] < box[3] else None


def _find_calibration(root: Path, sequence: str | None) -> tuple[str, Path]:
    if sequence is not None:
        path = root / f"calibration_{sequence}.json"
        if not path.is_file():
            raise FileNotFoundError(f"{path} 가 없다")
        return sequence, path
    found = [(m.group(1), p) for p in root.glob("calibration_*.json") if (m := _CALIBRATION_FILE.match(p.name))]
    if len(found) != 1:
        raise FileNotFoundError(
            f"{root}: calibration_<seq>.json 이 정확히 하나여야 한다 ({[p.name for _, p in found]})"
        )
    return found[0]
