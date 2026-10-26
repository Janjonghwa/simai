"""카메라별 HMR 결과 → 사람별 대표 포즈 하나 (EXP-02 다시점 집계, 명세 5.6·20.3·20.4, S15P31A101-87).

설계·가정·수치 근거는 docs/design/hmr-multiview-aggregation.md. -227 워커가 그대로 부르는 순수 함수다(상태 없음, 시계 안 읽음).

입력: 한 시각의 카메라별 HMR 후보(`HmrCandidate`), 같은 시각 Tier 0 world_tracks 문서(contracts/providers/world_tracks:
world_state 트랙 + 카메라별 bbox·keypoint), 그 문서와 같은 판의 보정(`CameraRig`).

1. 연결(카메라당 최대 1개): 후보 bbox 와 그 카메라 Tier 0 관측 bbox 의 IoU 가 기준 이상인 쌍만 후보로 두고, IoU 큰 순서로
   한 후보는 한 World ID 에만, 한 World ID 는 카메라마다 한 후보만 받는다(명세 5.2 같은 카메라 두 검출 금지). Tier 0 pelvis 가
   있으면 후보의 엉덩이 중점이 그 카메라에 투영한 pelvis 와 bbox 높이 × 기준 안에 있고, 깊이 비가 기준 안이어야 한다
   (겹친 박스·같은 시선 위 앞뒤 사람 오연결 방지. 단안 깊이는 부정확해 넉넉한 비만 본다).
   프롬프트를 만든 World ID·identity_revision 이 함께 오면 지금 문서와 같아야 한다(재등장·ID 교정 뒤 낡은 결과를 붙이지 않음).
   Tier 0 가 그 카메라에서 그 사람을 보지 않았으면 붙이지 않는다(HMR 이 사람 정체성을 정하지 않음, 20.3).
2. 대표 포즈: 후보마다 자기 3D 관절을 World 방향으로 돌려(보정 R) 엉덩이 중점 기준 상대 위치를 만들고, Tier 0 pelvis 에 놓아
   다른 카메라의 Tier 0 2D keypoint 에 재투영한 오차(중앙값)가 가장 작은 후보를 고른다(CROSS_VIEW). 그것이 안 되면
   후보 3개 이상은 상대 관절 거리 합이 가장 작은 후보(MEDOID), 2개면 검출 점수(SCORE), 1개면 그대로(SINGLE).
   관절 회전은 대표 후보 하나의 것을 그대로 쓴다(카메라마다 다른 관절을 섞지 않음).
3. root: 위치는 Tier 0 pelvis_xyz 그대로다. HMR translation 은 버린다. pelvis 가 null 이면 World 관절·root 오차를 비운다
   (지어내지 않음). 접지(ground_anchor_xyz)도 Tier 0 값 그대로이고 모르면 null 이다(Z=0 대체 금지).
   진단용 `hmr_root_offset_m` 은 대표 상대 관절을 고정한 채 위치만 다시점 Tier 0 2D keypoint 로 푼 값(명세 5.6 "translation 만
   다시점 재투영으로 보정")과 Tier 0 pelvis 의 거리다. root 로 쓰지 않는다.
4. 결과 관절은 모두 inferred 다. 위험 판정에 쓰지 않는다(AGENTS 규칙 1·4·7).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import numpy as np

from simai_ai.geometry.rig import CameraRig, RigCamera
from simai_ai.hmr.pose import POSE_DECIMALS, world_root_rotation
from simai_ai.hmr.prompt import COCO17_TO_MHR70, box_iou, valid_box

MHR70_COUNT = 70
MHR70_L_HIP, MHR70_R_HIP = 9, 10
MHR70_FEET = (15, 16, 17, 18, 19, 20)  # 엄지·새끼발가락 끝, 발뒤꿈치 (좌 15~17, 우 18~20)
COCO17_INDEX = np.asarray(COCO17_TO_MHR70)
POSE_FRAME_SCHEMA_VERSION = "2.0.0"  # contracts/experiments/human_mesh_output
NATIVE_POSE_FORMAT = "mhr_pose@avatar_pose_batch-1.0.0"  # params = 스트림 mhr_pose 와 같은 모양(root_rotation, joint_rotations)


class Selection(StrEnum):
    CROSS_VIEW = "CROSS_VIEW"
    MEDOID = "MEDOID"
    SCORE = "SCORE"
    SINGLE = "SINGLE"


class Reject(StrEnum):
    UNKNOWN_CAMERA = "UNKNOWN_CAMERA"
    INVALID_BBOX = "INVALID_BBOX"
    NON_FINITE_KEYPOINTS = "NON_FINITE_KEYPOINTS"
    PROMPT_WORLD_ID_NOT_IN_FRAME = "PROMPT_WORLD_ID_NOT_IN_FRAME"
    IDENTITY_REVISION_CHANGED = "IDENTITY_REVISION_CHANGED"
    NO_TRACK_MATCH = "NO_TRACK_MATCH"
    CAMERA_SLOT_TAKEN = "CAMERA_SLOT_TAKEN"


@dataclass(frozen=True)
class AggregationConfig:
    """초기값. 근거는 설계 문서 '명세에 없어 정한 수치'."""

    min_bbox_iou: float = 0.5
    pelvis_gate_bbox_ratio: float = 0.5  # 후보 엉덩이 중점 ↔ Tier 0 pelvis 투영 거리 / bbox 높이
    max_depth_ratio: float = 1.5  # 후보 엉덩이 깊이 ↔ Tier 0 pelvis 깊이(그 카메라 z) 비, 같은 시선 위 앞뒤 사람 구분
    min_keypoint_confidence: float = 0.3  # 다시점 선택·위치 진단에 쓰는 Tier 0 2D keypoint
    min_translation_views: int = 2
    min_translation_points: int = 6


@dataclass(frozen=True, eq=False)
class HmrCandidate:
    """카메라 한 대 HMR 결과의 사람 한 명.

    keypoints_cam: MHR70 3D 관절의 그 카메라 좌표(m, OpenCV) = `pred_keypoints_3d + pred_cam_t`. 왜곡을 편 그림(K_used)으로
    추론해도 카메라 회전은 같으므로 보정 R 로 World 방향을 만든다. camera_from_rig·joint_rotations 는 `pose.pose_from_hmr`·
    `pose_from_model_params` 결과(HmrPose)다. bbox 는 원본 픽셀의 프롬프트 박스다(Tier 0 per_camera bbox 와 같은 공간).
    """

    camera_id: str
    candidate_id: str
    bbox: tuple[float, float, float, float]
    keypoints_cam: np.ndarray
    camera_from_rig: np.ndarray | None = None
    joint_rotations: tuple[Sequence[float] | None, ...] | None = None
    score: float = 1.0
    prompt_world_id: str | None = None
    prompt_identity_revision: int | None = None


@dataclass(frozen=True)
class Rejected:
    camera_id: str
    candidate_id: str
    reason: Reject
    world_id: str | None = None


@dataclass(frozen=True, eq=False)
class PersonAggregate:
    """사람 한 명의 대표 포즈. 위치는 Tier 0 값만 싣는다."""

    world_id: str
    identity_revision: int
    identity_confidence: str
    observation_state: str
    t: int
    candidates: Mapping[str, HmrCandidate]  # camera_id → 후보 (카메라당 하나)
    representative_camera: str
    selection: Selection
    cross_view_error_px: Mapping[str, float | None]
    relative_keypoints: np.ndarray  # (70, 3) World 방향, 엉덩이 중점 기준. 위치 없음
    root_rotation: list[float] | None
    joint_rotations: tuple[Sequence[float] | None, ...] | None
    pelvis_xyz: tuple[float, float, float] | None
    ground_anchor_xyz: tuple[float, float, float] | None
    hmr_root_offset_m: float | None
    joint_source: str = field(default="inferred")

    @property
    def world_keypoints(self) -> np.ndarray | None:
        """(70, 3) World 관절 = 상대 관절 + Tier 0 pelvis. pelvis 를 모르면 None."""
        if self.pelvis_xyz is None:
            return None
        return self.relative_keypoints + np.asarray(self.pelvis_xyz, dtype=float)


@dataclass(frozen=True, eq=False)
class FrameAggregation:
    t: int
    calibration_version: str
    people: tuple[PersonAggregate, ...]
    rejected: tuple[Rejected, ...]


def hip_mid(keypoints: np.ndarray) -> np.ndarray:
    k = np.asarray(keypoints, dtype=float)
    return 0.5 * (k[MHR70_L_HIP] + k[MHR70_R_HIP])


def relative_world_keypoints(candidate: HmrCandidate, camera: RigCamera) -> np.ndarray:
    """후보 관절 → World 방향 상대 관절 (70, 3): R_wc · (X_cam − 엉덩이 중점). 행 벡터라 (·) @ R 이다."""
    k = np.asarray(candidate.keypoints_cam, dtype=float)
    return (k - hip_mid(k)) @ camera.R


def single_view_world_keypoints(candidate: HmrCandidate, camera: RigCamera) -> np.ndarray:
    """후보 관절을 HMR 자기 translation 그대로 World 에 놓는다 (기준선 '원본'): R_wc · (X_cam − t)."""
    return (np.asarray(candidate.keypoints_cam, dtype=float) - camera.t) @ camera.R


def project_ideal(camera: RigCamera, points_world: np.ndarray) -> np.ndarray:
    """World 점 (N, 3) → 이상적 핀홀 픽셀 (N, 2). 카메라 앞이 아니거나 유한하지 않으면 NaN."""
    Y = np.asarray(points_world, dtype=float).reshape(-1, 3) @ camera.R.T + camera.t
    out = np.full((len(Y), 2), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        ok = np.isfinite(Y).all(axis=1) & (Y[:, 2] > 1e-6)
        if ok.any():
            uvw = (Y[ok] / Y[ok, 2:3]) @ camera.K.T
            out[ok] = uvw[:, :2]
    return out


def observed_coco17(camera: RigCamera, keypoints: Sequence[Sequence[float] | None], min_confidence: float) -> tuple[np.ndarray, np.ndarray]:
    """Tier 0 COCO17 keypoint → (이상적 픽셀 (17, 2), 정규화 좌표 (17, 2)). 쓸 수 없는 관절은 NaN."""
    kps = list(keypoints)[:17] + [None] * max(0, 17 - len(keypoints))
    und = camera.undistort_keypoints(kps)
    ok = und.usable(min_confidence)
    ideal = np.where(ok[:, None], und.ideal_pixels, np.nan)
    norm = np.where(ok[:, None], und.normalized, np.nan)
    return ideal, norm


def reprojection_px(camera: RigCamera, points_coco17_world: np.ndarray, observed_ideal: np.ndarray) -> np.ndarray:
    """(17,) 관절별 재투영 오차(이상적 픽셀). 어느 쪽이든 없으면 NaN."""
    proj = project_ideal(camera, points_coco17_world)
    return np.linalg.norm(proj - observed_ideal, axis=1)


def solve_root_translation(
    relative_coco17: np.ndarray, views: Sequence[tuple[RigCamera, np.ndarray, np.ndarray]]
) -> np.ndarray | None:
    """상대 관절을 고정하고 위치 T 만 푼다(선형 최소제곱, 대수 오차). views = (카메라, 정규화 좌표 (17, 2), 가중치 (17,)).

    X_i = rel_i + T 를 카메라에 넣은 (R·X + t) 의 x − x_n·z = 0, y − y_n·z = 0 이 T 에 선형이다. 해가 카메라 뒤면 None.
    shortcut: 대수 오차 해만 쓴다(가우스-뉴턴 정밀화 없음), 진단값이라 충분. 위치로 쓰게 되면 정밀화를 더한다.
    """
    rows, rhs, w = [], [], []
    for cam, norm, weight in views:
        for i in range(len(relative_coco17)):
            xn = norm[i]
            if not (np.isfinite(xn).all() and np.isfinite(relative_coco17[i]).all() and weight[i] > 0):
                continue
            base = cam.R @ relative_coco17[i] + cam.t
            for axis in (0, 1):
                a = cam.R[axis] - xn[axis] * cam.R[2]
                rows.append(a)
                rhs.append(-(base[axis] - xn[axis] * base[2]))
                w.append(math.sqrt(float(weight[i])))
    if len(rows) < 6:
        return None
    A = np.asarray(rows) * np.asarray(w)[:, None]
    b = np.asarray(rhs) * np.asarray(w)
    if np.linalg.matrix_rank(A) < 3:
        return None
    T, *_ = np.linalg.lstsq(A, b, rcond=None)
    for cam, _norm, _w in views:
        if (cam.R @ T + cam.t)[2] <= 0:
            return None
    return T


def _per_camera(track: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {obs["camera_id"]: obs for obs in track.get("per_camera") or ()}


def _xyz(v: Any) -> tuple[float, float, float] | None:
    return None if v is None else (float(v[0]), float(v[1]), float(v[2]))


def _candidate_ok(c: HmrCandidate, rig: CameraRig) -> Reject | None:
    if c.camera_id not in rig.cameras:
        return Reject.UNKNOWN_CAMERA
    if not valid_box(c.bbox):
        return Reject.INVALID_BBOX
    k = np.asarray(c.keypoints_cam, dtype=float)
    if k.shape != (MHR70_COUNT, 3) or not np.isfinite(k[COCO17_INDEX]).all():
        return Reject.NON_FINITE_KEYPOINTS
    return None


def associate(
    candidates: Iterable[HmrCandidate], doc: Mapping[str, Any], rig: CameraRig, config: AggregationConfig = AggregationConfig()
) -> tuple[dict[str, dict[str, HmrCandidate]], list[Rejected]]:
    """후보 → World ID 연결(카메라당 최대 1개). 반환: {world_id: {camera_id: 후보}}, 버린 후보와 이유."""
    tracks = {t["world_id"]: t for t in doc["tracks"]}
    rejected: list[Rejected] = []
    pairs: list[tuple[float, str, str, str, HmrCandidate]] = []
    eligible: set[int] = set()
    cands = list(candidates)
    seen_ids: set[tuple[str, str]] = set()
    for c in cands:
        if (c.camera_id, c.candidate_id) in seen_ids:
            raise ValueError(f"같은 카메라에 같은 candidate_id 가 둘: {c.camera_id}/{c.candidate_id}")
        seen_ids.add((c.camera_id, c.candidate_id))
        why = _candidate_ok(c, rig)
        if why is not None:
            rejected.append(Rejected(c.camera_id, c.candidate_id, why))
            continue
        if c.prompt_world_id is not None and c.prompt_world_id not in tracks:
            rejected.append(Rejected(c.camera_id, c.candidate_id, Reject.PROMPT_WORLD_ID_NOT_IN_FRAME, c.prompt_world_id))
            continue
        if c.prompt_world_id is not None and c.prompt_identity_revision is not None:
            if int(tracks[c.prompt_world_id]["identity_revision"]) != c.prompt_identity_revision:
                rejected.append(Rejected(c.camera_id, c.candidate_id, Reject.IDENTITY_REVISION_CHANGED, c.prompt_world_id))
                continue
        cam = rig.cameras[c.camera_id]
        hip_cam = hip_mid(c.keypoints_cam)
        hip_px = (cam.K @ (hip_cam / hip_cam[2]))[:2] if hip_cam[2] > 1e-6 else np.full(2, np.nan)
        height = c.bbox[3] - c.bbox[1]
        for wid, track in tracks.items():
            if c.prompt_world_id is not None and wid != c.prompt_world_id:
                continue
            obs = _per_camera(track).get(c.camera_id)
            if obs is None or not valid_box(obs["bbox"]):
                continue
            iou = box_iou(tuple(float(v) for v in c.bbox), tuple(float(v) for v in obs["bbox"]))
            if iou < config.min_bbox_iou:
                continue
            pelvis = track.get("pelvis_xyz")
            if pelvis is not None:
                pel_cam = cam.R @ np.asarray(pelvis, dtype=float) + cam.t
                p = project_ideal(cam, np.asarray([pelvis], dtype=float))[0]
                if not (np.isfinite(p).all() and np.isfinite(hip_px).all() and pel_cam[2] > 1e-6):
                    continue
                if np.linalg.norm(p - hip_px) > config.pelvis_gate_bbox_ratio * height:
                    continue
                ratio = hip_cam[2] / pel_cam[2]
                if not (1.0 / config.max_depth_ratio <= ratio <= config.max_depth_ratio):
                    continue
            pairs.append((-iou, c.camera_id, c.candidate_id, wid, c))
            eligible.add(id(c))
    pairs.sort(key=lambda p: (p[0], p[1], p[2], p[3]))
    clusters: dict[str, dict[str, HmrCandidate]] = {}
    used: set[int] = set()
    for _neg, cam_id, _cid, wid, c in pairs:
        if id(c) in used or cam_id in clusters.get(wid, {}):
            continue
        clusters.setdefault(wid, {})[cam_id] = c
        used.add(id(c))
    already = {(r.camera_id, r.candidate_id) for r in rejected}
    for c in cands:
        if id(c) in used or (c.camera_id, c.candidate_id) in already:
            continue
        reason = Reject.CAMERA_SLOT_TAKEN if id(c) in eligible else Reject.NO_TRACK_MATCH
        rejected.append(Rejected(c.camera_id, c.candidate_id, reason, c.prompt_world_id))
    return clusters, rejected


def _cross_view_errors(
    rel: Mapping[str, np.ndarray], pelvis: np.ndarray, obs_ideal: Mapping[str, np.ndarray], rig: CameraRig
) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for cam_k, r in rel.items():
        world = r[COCO17_INDEX] + pelvis
        per_view = []
        for cam_j, ideal in obs_ideal.items():
            if cam_j == cam_k:
                continue
            e = reprojection_px(rig.cameras[cam_j], world, ideal)
            e = e[np.isfinite(e)]
            if len(e):
                per_view.append(float(np.median(e)))
        out[cam_k] = float(np.median(per_view)) if per_view else None
    return out


def _select(
    cands: Mapping[str, HmrCandidate], rel: Mapping[str, np.ndarray], cross: Mapping[str, float | None]
) -> tuple[str, Selection]:
    order = sorted(cands)
    if len(order) == 1:
        return order[0], Selection.SINGLE
    scored = [(e, cid) for cid, e in cross.items() if e is not None]
    if scored:
        return min(scored)[1], Selection.CROSS_VIEW
    if len(order) >= 3:
        cost = {
            a: sum(float(np.nanmean(np.linalg.norm(rel[a][COCO17_INDEX] - rel[b][COCO17_INDEX], axis=1))) for b in order if b != a)
            for a in order
        }
        return min(order, key=lambda a: (cost[a], a)), Selection.MEDOID
    return min(order, key=lambda a: (-cands[a].score, a)), Selection.SCORE


def aggregate_person(
    track: Mapping[str, Any],
    t: int,
    candidates: Mapping[str, HmrCandidate],
    rig: CameraRig,
    config: AggregationConfig = AggregationConfig(),
) -> PersonAggregate:
    """연결된 후보(카메라당 하나)로 사람 한 명의 대표 포즈를 만든다. 위치·접지는 track(Tier 0) 값 그대로다."""
    if not candidates:
        raise ValueError(f"{track['world_id']}: 후보가 없다")
    for cid, c in candidates.items():
        if cid != c.camera_id:
            raise ValueError(f"카메라 키 {cid} 와 후보 카메라 {c.camera_id} 가 다르다")
    pelvis = _xyz(track.get("pelvis_xyz"))
    anchor = _xyz(track.get("ground_anchor_xyz"))
    rel = {cid: relative_world_keypoints(c, rig.cameras[cid]) for cid, c in candidates.items()}
    per_cam = _per_camera(track)
    obs_ideal: dict[str, np.ndarray] = {}
    obs_norm: dict[str, np.ndarray] = {}
    for cid in candidates:
        kps = per_cam.get(cid, {}).get("keypoints_2d")
        if kps and per_cam[cid].get("joint_layout", "COCO17") == "COCO17":
            obs_ideal[cid], obs_norm[cid] = observed_coco17(rig.cameras[cid], kps, config.min_keypoint_confidence)
    cross: dict[str, float | None] = {cid: None for cid in candidates}
    if pelvis is not None:
        cross = _cross_view_errors(rel, np.asarray(pelvis), obs_ideal, rig)
    rep, how = _select(candidates, rel, cross)
    c = candidates[rep]
    cam = rig.cameras[rep]
    root_rotation = None if c.camera_from_rig is None else world_root_rotation(np.asarray(c.camera_from_rig), cam.R)
    offset = None
    if pelvis is not None:
        views = [(rig.cameras[cid], obs_norm[cid], np.isfinite(obs_norm[cid]).all(axis=1).astype(float)) for cid in obs_norm]
        views = [v for v in views if v[2].sum() > 0]
        if len(views) >= config.min_translation_views and sum(int(v[2].sum()) for v in views) >= config.min_translation_points:
            T = solve_root_translation(rel[rep][COCO17_INDEX], views)
            if T is not None:
                offset = round(float(np.linalg.norm(T - np.asarray(pelvis))), POSE_DECIMALS)
    return PersonAggregate(
        world_id=track["world_id"],
        identity_revision=int(track["identity_revision"]),
        identity_confidence=track["identity_confidence"],
        observation_state=track["observation_state"],
        t=int(t),
        candidates=dict(sorted(candidates.items())),
        representative_camera=rep,
        selection=how,
        cross_view_error_px={k: (None if v is None else round(v, 3)) for k, v in sorted(cross.items())},
        relative_keypoints=rel[rep],
        root_rotation=root_rotation,
        joint_rotations=c.joint_rotations,
        pelvis_xyz=pelvis,
        ground_anchor_xyz=anchor,
        hmr_root_offset_m=offset,
    )


def aggregate_frame(
    candidates: Iterable[HmrCandidate],
    doc: Mapping[str, Any],
    rig: CameraRig,
    config: AggregationConfig = AggregationConfig(),
) -> FrameAggregation:
    """한 시각의 카메라별 HMR 후보 → 사람별 대표 포즈. doc·rig 의 보정 판이 다르면 섞지 않고 ValueError."""
    if doc["calibration_version"] != rig.calibration_version:
        raise ValueError(f"world_tracks 보정 {doc['calibration_version']} 과 rig 보정 {rig.calibration_version} 이 다르다")
    frame = doc.get("coordinate_frame") or {}
    if (frame.get("frame"), frame.get("unit"), frame.get("up_axis")) != ("WORLD", "m", "+Z"):
        raise ValueError(f"world_tracks 좌표계가 World(m, +Z)가 아니다: {frame}")
    clusters, rejected = associate(candidates, doc, rig, config)
    tracks = {t["world_id"]: t for t in doc["tracks"]}
    people = tuple(aggregate_person(tracks[wid], int(doc["t"]), cl, rig, config) for wid, cl in sorted(clusters.items()))
    return FrameAggregation(int(doc["t"]), doc["calibration_version"], people, tuple(rejected))


def _rounded_xyz(v: np.ndarray) -> list[float] | None:
    if not np.isfinite(v).all():
        return None
    return [float(round(float(x), POSE_DECIMALS)) + 0.0 for x in v]


def pose_frame_record(
    person: PersonAggregate,
    doc: Mapping[str, Any],
    *,
    run_id: str,
    model_version: str,
    input_hash: str,
    input_provenance: Mapping[str, Any],
    hmr_latency_ms: float,
    lag_vs_tier0_ms: float,
    fallback: bool,
    appearance_version: int = 0,
    quality: Mapping[str, float | None] | None = None,
    experiment_id: str = "EXP-02",
) -> dict[str, Any] | None:
    """`human_mesh_output.pose_frame`(2.0.0) 기록 하나. 몸 방향(root_rotation)을 모르면 None(포즈를 지어내지 않음).

    canonical_motion 은 COCO17 위치(평가용)이고 관절 출처는 모두 inferred 다. pelvis 를 모르면 위치는 모두 null 이다.
    """
    if person.root_rotation is None or person.joint_rotations is None:
        return None
    if "tracks" not in input_provenance:
        raise ValueError("pose_frame 의 input_provenance 에는 tracks 가 필요하다")
    world = person.world_keypoints
    joints = [None] * 17 if world is None else [_rounded_xyz(world[i]) for i in COCO17_INDEX]
    track = next(t for t in doc["tracks"] if t["world_id"] == person.world_id)
    per_cam = _per_camera(track)
    sources = []
    for cid in person.candidates:
        obs = per_cam.get(cid)
        if obs is None:
            continue
        used = ["POSE"] if cid == person.representative_camera else []
        src = {"camera_id": cid, "local_track_id": str(obs["local_track_id"]),
               "estimated_capture_time": int(obs["estimated_capture_time"]), "timestamp_source": obs["timestamp_source"]}
        if used:
            src["used_for"] = used
        sources.append(src)
    rec: dict[str, Any] = {
        "schema_version": POSE_FRAME_SCHEMA_VERSION,
        "record_type": "pose_frame",
        "world_id": person.world_id,
        "experiment_id": experiment_id,
        "calibration_version": doc["calibration_version"],
        "coordinate_frame": {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"},
        "input_provenance": dict(input_provenance),
        "reproducibility": {"run_id": run_id, "model_version": model_version, "input_hash": input_hash},
        "t": person.t,
        "appearance_version": int(appearance_version),
        "root": {
            "source": "TIER0",
            "tier0_pelvis_xyz": None if person.pelvis_xyz is None else list(person.pelvis_xyz),
            "tier0_ground_anchor_xyz": None if person.ground_anchor_xyz is None else list(person.ground_anchor_xyz),
            "tier0_t": int(doc["t"]),
            "hmr_root_offset_m": person.hmr_root_offset_m,
        },
        "native_pose": {
            "representation": "MHR",
            "format_version": NATIVE_POSE_FORMAT,
            "params": {"root_rotation": list(person.root_rotation),
                       "joint_rotations": [None if r is None else [float(x) for x in r] for r in person.joint_rotations]},
        },
        "canonical_motion": {"joint_layout": "COCO17", "joints": joints, "joint_source": ["inferred"] * 17},
        "observation_state": person.observation_state,
        "per_camera_sources": sources,
        "timing": {"hmr_latency_ms": float(hmr_latency_ms), "lag_vs_tier0_ms": float(lag_vs_tier0_ms), "fallback": bool(fallback)},
    }
    if "scene_version" in doc:
        rec["scene_version"] = doc["scene_version"]
    if quality:
        rec["quality"] = {k: (None if v is None else float(v)) for k, v in quality.items()}
    return rec
