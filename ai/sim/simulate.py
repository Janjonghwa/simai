"""시나리오 하나를 돌려 출력 파일 내용(경로 → 바이트)을 만든다.

출력 (경로는 출력 디렉터리 기준):
- calibration.json             가상 카메라 보정. contracts/providers/calibration.schema.json, provider_source = SIM
- observations/<camera>.jsonl  카메라별 프레임마다 2D keypoint와 촬영 시각 (센서가 내는 값만)
- world_tracks.jsonl           tick마다 WorldTrackProvider 형식. contracts/providers/world_tracks.schema.json
- truth/world_truth.jsonl      tick마다 사람별 정답 관절 3D (가림과 무관한 전부)
- truth/camera_frames.jsonl    프레임별 실제 촬영 시각과 보고한 촬영 시각
- manifest.json                시나리오 해시, 시드, input_provenance, 파일 해시

정답(truth/)은 평가용이다. 정답·시뮬레이터 입력으로 얻은 지표는 상한(is_upper_bound = true)이며 제품 성능이 아니다.
"""

import bisect
import random
from dataclasses import dataclass

from sim import __version__, camera
from sim.body import COCO17, FACE_JOINTS, FOOT_TIPS, HEAD_JOINTS, L_ANKLE, L_HIP, R_ANKLE, R_HIP, BodyState, Person
from sim.output import CONF, MAT, PX, M, canonical_sha256, json_bytes, jsonl_bytes, r, rv, sha256
from sim.scenario import Scenario
from sim.visibility import Box, Cylinder, face_turned_away, is_occluded

CONTRACTS_VERSION = "1.1.0"
# calibration 문서의 계약 버전 (contracts/providers/calibration.schema.json 2.0.0: v1.3 제외 보정 출처·스케일 근거 값 삭제)
CALIBRATION_SCHEMA_VERSION = "2.0.0"
PROVIDER_VERSION = f"simai-sim-{__version__}"
STREAM_EPOCH = 0

VISIBLE, OCCLUDED, OUT_OF_FRAME, BEHIND = "visible", "occluded", "out_of_frame", "behind"


@dataclass(frozen=True)
class Detection:
    person_index: int
    local_track_id: str
    bbox: list[float]
    keypoints_2d: list[list[float] | None]
    foot_tips_2d: list[list[float] | None]
    visible: tuple[bool, ...]
    """COCO17 관절별로 이 프레임에서 보였는지 (conf > 0)"""


@dataclass(frozen=True)
class Frame:
    camera_id: str
    frame_sequence: int
    true_capture_ms: int
    estimated_capture_ms: int
    uncertainty_ms: int
    detections: tuple[Detection, ...]


class Simulation:
    def __init__(self, sc: Scenario):
        self.sc = sc
        self.cameras = [camera.build(c) for c in sc.cameras]
        self.people = [Person(p, sc.surfaces) for p in sc.people]
        self.boxes = [Box(s.box_min, s.box_max) for s in sc.surfaces]
        self.world_ids = [f"w-{i + 1:04d}" for i in range(len(self.people))]
        self._states: dict[int, list[BodyState]] = {}

    def states(self, t_abs_ms: int) -> list[BodyState]:
        if t_abs_ms not in self._states:
            self._states[t_abs_ms] = [p.state(t_abs_ms - self.sc.start_time_ms) for p in self.people]
        return self._states[t_abs_ms]

    def run(self) -> dict[str, bytes]:
        frames = {cam.camera_id: self._camera_frames(cam) for cam in self.cameras}
        calibration = self._calibration()
        files: dict[str, bytes] = {"calibration.json": json_bytes(calibration)}
        for cam in self.cameras:
            files[f"observations/{cam.camera_id}.jsonl"] = jsonl_bytes(
                [self._observation_row(f) for f in frames[cam.camera_id]]
            )
        ticks = self._ticks()
        files["world_tracks.jsonl"] = jsonl_bytes([self._world_tracks_row(t, frames, calibration) for t in ticks])
        files["truth/world_truth.jsonl"] = jsonl_bytes([self._truth_row(t) for t in ticks])
        files["truth/camera_frames.jsonl"] = jsonl_bytes(
            [
                {
                    "camera_id": f.camera_id,
                    "frame_sequence": f.frame_sequence,
                    "true_capture_time_ms": f.true_capture_ms,
                    "estimated_capture_time_ms": f.estimated_capture_ms,
                }
                for cam in self.cameras
                for f in frames[cam.camera_id]
            ]
        )
        files["manifest.json"] = json_bytes(self._manifest(files, calibration["calibration_version"]))
        return files

    # --- 카메라 프레임 ---

    def _camera_frames(self, cam: camera.VirtualCamera) -> list[Frame]:
        spec, sc = cam.spec, self.sc
        time_rng = random.Random(f"{sc.seed}:time:{spec.camera_id}")
        kp_rng = random.Random(f"{sc.seed}:keypoints:{spec.camera_id}")
        period_ms = 1000.0 / spec.fps
        frames: list[Frame] = []
        k = 0
        while (nominal := spec.time_offset_ms + round(k * period_ms)) < sc.duration_ms:
            jitter = time_rng.randint(-spec.capture_jitter_ms, spec.capture_jitter_ms) if spec.capture_jitter_ms else 0
            error = (
                time_rng.randint(-spec.timestamp_error_ms, spec.timestamp_error_ms) if spec.timestamp_error_ms else 0
            )
            true_ms = sc.start_time_ms + nominal + jitter
            frames.append(
                Frame(
                    camera_id=spec.camera_id,
                    frame_sequence=k,
                    true_capture_ms=true_ms,
                    estimated_capture_ms=true_ms + error,
                    uncertainty_ms=spec.timestamp_error_ms,
                    detections=self._detect(cam, true_ms, kp_rng),
                )
            )
            k += 1
        return frames

    def _detect(self, cam: camera.VirtualCamera, t_abs_ms: int, rng: random.Random) -> tuple[Detection, ...]:
        kp = self.sc.keypoints
        w, h = cam.spec.image_size
        states = self.states(t_abs_ms)
        cylinders = [
            Cylinder(st.position_xy, p.spec.radius_m, st.support_z, st.support_z + p.spec.height_m)
            for st, p in zip(states, self.people)
        ]
        detections = []
        for i, st in enumerate(states):
            others = cylinders[:i] + cylinders[i + 1 :]
            points = [*st.joints, *st.foot_tips, st.head_top]
            away = face_turned_away(st.heading_rad, st.head_top, cam.center)
            observed = []
            for j, p in enumerate(points):
                uv = cam.project(p)
                if uv is None:
                    observed.append((BEHIND, None))
                elif not cam.in_image(uv):
                    observed.append((OUT_OF_FRAME, uv))
                elif (away and j in FACE_JOINTS) or is_occluded(cam.center, p, others, self.boxes):
                    observed.append((OCCLUDED, uv))
                else:
                    observed.append((VISIBLE, uv))
            visible = tuple(status == VISIBLE for status, _ in observed[: len(COCO17)])
            if sum(visible) < kp.min_visible_joints:
                continue  # 검출 누락: 보이는 관절이 너무 적다
            encoded = [self._encode(status, uv, w, h, rng) for status, uv in observed]
            seen = [uv for status, uv in observed if status == VISIBLE]
            us, vs = [uv[0] for uv in seen], [uv[1] for uv in seen]
            pad = 2.0 + 0.05 * (max(vs) - min(vs))
            bbox = [
                max(0.0, min(us) - pad),
                max(0.0, min(vs) - pad),
                min(float(w), max(us) + pad),
                min(float(h), max(vs) + pad),
            ]
            detections.append(
                Detection(
                    person_index=i,
                    local_track_id=f"{cam.camera_id}-{i + 1}",
                    bbox=rv(bbox, PX),
                    keypoints_2d=encoded[: len(COCO17)],
                    foot_tips_2d=encoded[len(COCO17) : len(COCO17) + len(FOOT_TIPS)],
                    visible=visible,
                )
            )
        return tuple(detections)

    def _encode(
        self, status: str, uv: tuple[float, float] | None, w: int, h: int, rng: random.Random
    ) -> list[float] | None:
        kp = self.sc.keypoints
        if status == VISIBLE:
            assert uv is not None
            u = min(max(uv[0] + rng.gauss(0.0, kp.pixel_noise_sigma_px), 0.0), w - 1e-3)
            v = min(max(uv[1] + rng.gauss(0.0, kp.pixel_noise_sigma_px), 0.0), h - 1e-3)
            conf = rng.uniform(*kp.confidence_range)
            return [r(u, PX), r(v, PX), r(conf, CONF)]
        policy = {OCCLUDED: kp.occluded, OUT_OF_FRAME: kp.out_of_frame, BEHIND: "null"}[status]
        if policy == "null" or uv is None:
            return None
        return [r(uv[0], PX), r(uv[1], PX), 0.0]

    def _observation_row(self, f: Frame) -> dict:
        return {
            "camera_id": f.camera_id,
            "stream_epoch": STREAM_EPOCH,
            "frame_sequence": f.frame_sequence,
            "estimated_capture_time_ms": f.estimated_capture_ms,
            "timestamp_source": "SIM",
            "timestamp_uncertainty_ms": f.uncertainty_ms,
            "detections": [
                {
                    "local_track_id": d.local_track_id,
                    "bbox": d.bbox,
                    "joint_layout": "COCO17",
                    "keypoints_2d": d.keypoints_2d,
                    "foot_tips_2d": d.foot_tips_2d,
                }
                for d in f.detections
            ],
        }

    # --- 보정 ---

    def _calibration(self) -> dict:
        cams = [
            {
                "camera_id": c.camera_id,
                "image_size": list(c.spec.image_size),
                "K": [rv(row, MAT) for row in c.K],
                "distortion": {"model": c.spec.distortion_model, "coeffs": list(c.spec.distortion_coeffs)},
                "R": [rv(row, MAT) for row in c.R],
                "t": rv(c.t, MAT),
                "camera_height_m": r(c.center[2], M),
            }
            for c in self.cameras
        ]
        doc = {
            "schema_version": CALIBRATION_SCHEMA_VERSION,
            "calibration_version": f"sim-{self.sc.name}-{canonical_sha256(cams)[7:15]}",
            "provider_source": "SIM",
            "provider_version": PROVIDER_VERSION,
            # SIM 보정은 현장 보정이 아니므로 CALIBRATED 가 될 수 없다 (명세 20.4 규칙 4).
            "state": "PROVISIONAL",
            "created_at": self.sc.start_time_ms,
            "coordinate_frame": self._frame(),
            # 계약의 scale_basis 에 SIM 이 없어 정답 기반 값인 DATASET_GT 를 쓴다. SIM 값이 계약에 생기면 교체한다.
            "scale_basis": "DATASET_GT",
            "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
            "cameras": cams,
        }
        # content_hash = content_hash 를 뺀 문서의 canonical sha256 (키 정렬, 공백 없음)
        doc["content_hash"] = canonical_sha256(doc)
        return doc

    def _frame(self) -> dict:
        return {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z", "site_id": self.sc.site_id}

    # --- World tick ---

    def _ticks(self) -> list[int]:
        period_ms = 1000.0 / self.sc.world_rate_hz
        ticks = []
        i = 0
        while (t_rel := round(i * period_ms)) < self.sc.duration_ms:
            ticks.append(self.sc.start_time_ms + t_rel)
            i += 1
        return ticks

    def _truth_row(self, t: int) -> dict:
        people = []
        for i, st in enumerate(self.states(t)):
            spec = self.people[i].spec
            people.append(
                {
                    "person_id": spec.person_id,
                    "world_id": self.world_ids[i],
                    "height_m": spec.height_m,
                    "position_xy": rv(st.position_xy, M),
                    "heading_rad": r(st.heading_rad, MAT),
                    "velocity": rv(st.velocity, M),
                    "support_surface_id": st.support_surface_id,
                    "support_z": r(st.support_z, M),
                    "pelvis_xyz": rv(st.pelvis, M),
                    "ground_anchor_xyz": rv(st.ground_anchor, M),
                    "head_top_xyz": rv(st.head_top, M),
                    "joint_layout": "COCO17",
                    "joints": [rv(j, M) for j in st.joints],
                    "foot_tips": [rv(j, M) for j in st.foot_tips],
                }
            )
        return {"t": t, "people": people}

    def _world_tracks_row(self, t: int, frames: dict[str, list[Frame]], calibration: dict) -> dict:
        """Tier 0 출력과 같은 의미로 정답을 가린다 (명세 20.4 규칙 1).

        카메라마다 촬영 시각이 t 에 가장 가까운 프레임을 쓰고, 2대 이상에서 보인 관절만 채운다(나머지 null).
        골반은 양쪽 엉덩이, 접지점은 발목 하나 이상이 2대 이상에서 보여야 채운다. 접지를 모르면 비운다(Z=0 대체 금지).
        골반도 접지점도 없는 사람은 tracks 에 넣지 않는다.
        """
        nearest = {cid: _nearest(fs, t) for cid, fs in frames.items()}
        tracks = []
        for i, st in enumerate(self.states(t)):
            per_camera = []
            counts = [0] * len(COCO17)
            for cid in (c.camera_id for c in self.cameras):
                f = nearest[cid]
                det = next((d for d in f.detections if d.person_index == i), None)
                if det is None:
                    continue
                counts = [n + v for n, v in zip(counts, det.visible)]
                per_camera.append(
                    {
                        "camera_id": cid,
                        "local_track_id": det.local_track_id,
                        "bbox": det.bbox,
                        "keypoints_2d": det.keypoints_2d,
                        "joint_layout": "COCO17",
                        "frame_ref": {"stream_epoch": STREAM_EPOCH, "frame_sequence": f.frame_sequence},
                        "estimated_capture_time": f.estimated_capture_ms,
                        "timestamp_source": "SIM",
                        "timestamp_uncertainty_ms": f.uncertainty_ms,
                    }
                )
            seen = [n >= 2 for n in counts]
            pelvis = rv(st.pelvis, M) if seen[L_HIP] and seen[R_HIP] else None
            contact = seen[L_ANKLE] or seen[R_ANKLE]
            anchor = rv(st.ground_anchor, M) if contact else None
            if pelvis is None and anchor is None:
                continue
            xy = pelvis[:2] if pelvis is not None else anchor[:2]  # type: ignore[index]
            head_seen = any(seen[j] for j in HEAD_JOINTS)
            tracks.append(
                {
                    "world_id": self.world_ids[i],
                    "t_last_valid": t,
                    "observation_state": "OBSERVED",
                    "track_lifecycle": "CONFIRMED",
                    "identity_confidence": "HIGH",
                    "identity_revision": 0,
                    "position_xy": xy,
                    "position_xy_sigma": [0.0, 0.0],
                    "pelvis_xyz": pelvis,
                    "ground_anchor_xyz": anchor,
                    "support_surface_id": st.support_surface_id if contact else None,
                    "support_surface_validity": "VALID" if contact else "UNKNOWN",
                    "contact_validity": "VALID" if contact else "UNAVAILABLE",
                    "z_validity": "VALID" if contact else "UNCERTAIN",
                    "head_z": r(st.head_top[2], M) if head_seen else None,
                    "velocity": rv(st.velocity, M),
                    "cameras": [pc["camera_id"] for pc in per_camera],
                    "skeleton": {
                        "joint_layout": "COCO17",
                        "joints": [rv(j, M) if s else None for j, s in zip(st.joints, seen)],
                        "visible": seen,
                        "validity": ["VALID" if s else "UNAVAILABLE" for s in seen],
                        "joint_source": ["observed"] * len(COCO17),
                    },
                    "per_camera": per_camera,
                }
            )
        return {
            "schema_version": CONTRACTS_VERSION,
            "t": t,
            "provider_source": "SIM",
            "provider_version": PROVIDER_VERSION,
            "calibration_version": calibration["calibration_version"],
            "coordinate_frame": self._frame(),
            "tracks": tracks,
        }

    # --- manifest ---

    def _manifest(self, files: dict[str, bytes], calibration_version: str) -> dict:
        sc = self.sc
        observations = b"".join(files[f"observations/{c.camera_id}.jsonl"] for c in self.cameras)
        return {
            "generator": "simai-sim",
            "generator_version": __version__,
            "contracts_version": CONTRACTS_VERSION,
            "scenario": {"name": sc.name, "description": sc.description, "sha256": canonical_sha256(sc.canonical)},
            "seed": sc.seed,
            "start_time_ms": sc.start_time_ms,
            "duration_ms": sc.duration_ms,
            "world_rate_hz": sc.world_rate_hz,
            "calibration_version": calibration_version,
            "joint_layout": "COCO17",
            "foot_tips": list(FOOT_TIPS),
            "people": [{"person_id": p.spec.person_id, "world_id": w} for p, w in zip(self.people, self.world_ids)],
            "input_provenance": {
                "frames": {
                    "source": "SIM",
                    "dataset_id": f"sim/{sc.name}",
                    "dataset_hash": sha256(observations),
                    "camera_ids": [c.camera_id for c in self.cameras],
                },
                "calibration": {"source": "SIM", "calibration_version": calibration_version},
                "tracks": {"source": "SIM", "provider_version": PROVIDER_VERSION},
                "is_upper_bound": True,
            },
            "files": [
                {"path": path, "sha256": sha256(data), "bytes": len(data)} for path, data in sorted(files.items())
            ],
        }


def _nearest(frames: list[Frame], t: int) -> Frame:
    """보고한 촬영 시각이 t 에 가장 가까운 프레임. 같으면 앞 프레임."""
    times = [f.estimated_capture_ms for f in frames]
    k = bisect.bisect_left(times, t)
    if k == 0:
        return frames[0]
    if k == len(frames):
        return frames[-1]
    return frames[k] if times[k] - t < t - times[k - 1] else frames[k - 1]


def simulate(sc: Scenario) -> dict[str, bytes]:
    return Simulation(sc).run()
