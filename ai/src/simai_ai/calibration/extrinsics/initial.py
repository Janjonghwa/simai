"""번들 조정 외부 파라미터 초기값: MapAnything `cameras.json` → World → 카메라 (R, t) (명세 4.2, 4.3 1·2단계).

**cameras.json 규약.** `ai/experiments/static_scene/run_mapanything.py` 의 출력이다. views[] 마다 `cam2world`(4×4, 카메라 →
MapAnything world)이고 카메라 축은 OpenCV(x 오른쪽, y 아래, z 앞), 단위는 모델 추정 미터(미검증)다. 이 모듈은
`convention` 문자열에 cam2world·OpenCV 가 있는지 보고(없으면 오류), 회전이 정규직교·det +1 인지, 카메라 사이 거리가
미터로 그럴듯한지 본다(단위가 mm·정규화면 오류). 변환은 R = Rcw^T, t = −R·c (c 는 cam2world 이동 성분)다.

**MapAnything world → World.** MapAnything world 는 첫 입력 영상 카메라의 OpenCV 축이라 +Z 가 앞이다. World(+Z 상방,
바닥 Z = 0, 벽 방향 축, 모서리 원점)는 4.3 2단계 좌표계 고정(-102)이 정한다. 두 가지 길이 있다.
- `load_world_alignment`: -102 가 낸 닮음 변환(MapAnything world → World, 4×4 [sR | T])을 그대로 쓴다(PROVIDED).
- `alignment_from_people`: -102 전 임시 정렬(INTERIM_PERSON_VERTICAL). 걷는 사람의 발목 중점 → 어깨 중점 방향의
  강건 평균을 상방으로, 서 있는 발목 높이 중앙값 − 발목 접지 높이를 바닥으로 둔다. 벽 방향·원점은 정하지 않는다(MapAnything
  X 축을 수평에 투영한 방향, MapAnything 원점 아래). 이 World 로 낸 보정은 버전으로 제안하지 않는다(`cli`).
스케일은 바꾸지 않는다(INTERIM 은 1). 스케일 근거는 번들 조정의 실측 정물 제약 몫이다.

**출처(AGENTS 7, 명세 20.4 규칙 2·3).** `combine_input_provenance` 가 keypoint 파일·내부 파라미터 기록·World 정렬·실측 정물
입력의 출처를 합친다. 초기 카메라는 MapAnything 이 추정한 값을 다시 쓰므로 calibration 은 MODEL_ESTIMATE 이고, 입력 중
하나라도 SIM·DATASET_GT 면 calibration 을 그 출처로 두어 결과 전체를 상한(is_upper_bound)으로 만든다(SIM 우선).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.bundle.inputs import PersonObservation
from simai_ai.calibration.bundle.skeleton import (
    L_ANKLE,
    L_SHOULDER,
    R_ANKLE,
    R_SHOULDER,
)
from simai_ai.calibration.intrinsics.record import BundleAdjustmentInit
from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_point
from simai_ai.providers.contract import check_input_provenance
from simai_ai.providers.provenance import build_input_provenance

Extrinsics = dict[str, tuple[list[list[float]], list[float]]]

SOURCE_PROVIDED = "PROVIDED"
SOURCE_INTERIM = "INTERIM_PERSON_VERTICAL"
WORLD_ALIGNMENT_FORMAT = "simai.world_alignment"

ORTHONORMAL_TOL = 1e-3
# 카메라 쌍 거리가 이 범위 밖이면 단위가 미터가 아니라고 본다(교실 한 칸, 카메라 4대)
CAMERA_DISTANCE_RANGE_M = (0.3, 60.0)
MIN_UP_SAMPLES = 20
UP_OUTLIER_DEG = 25.0
# 상방 추정: 발목 중점 → 어깨 중점 길이가 이 범위 밖인 표본은 버린다(삼각측량 실패·오대응. MapAnything 미터 기준)
TORSO_LEG_RANGE_M = (0.6, 2.2)


@dataclass(frozen=True)
class MapAnythingCameras:
    extrinsics: Extrinsics  # camera_id → (R, t), MapAnything world → 카메라
    record: Mapping[str, Any]


@dataclass(frozen=True)
class WorldAlignment:
    """X_world = scale · rotation · X_mapanything + translation."""

    rotation: np.ndarray
    translation: np.ndarray
    scale: float
    source: str
    record: Mapping[str, Any] = field(default_factory=dict)

    def apply(self, extrinsics: Mapping[str, tuple[Sequence[Sequence[float]], Sequence[float]]]) -> Extrinsics:
        """MapAnything world 기준 (R, t) → World 기준 (R, t). 투영은 같다(카메라 좌표는 scale 배)."""
        Ra, ta, s = self.rotation, self.translation, self.scale
        out: Extrinsics = {}
        for cid, (R, t) in extrinsics.items():
            R = np.asarray(R, dtype=float) @ Ra.T
            t = s * np.asarray(t, dtype=float) - R @ ta
            out[cid] = (R.tolist(), t.tolist())
        return out


# --------------------------------------------------------------------------- cameras.json


def cam2world_to_extrinsics(T: Sequence[Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    T = np.asarray(T, dtype=float)
    if T.shape != (4, 4) or not np.all(np.isfinite(T)):
        raise ValueError("cam2world 는 유한한 4×4 행렬이어야 한다")
    Rcw = _orthonormal(T[:3, :3], "cam2world 회전")
    R = Rcw.T
    return R, -R @ T[:3, 3]


def extrinsics_to_cam2world(R: Sequence[Sequence[float]], t: Sequence[float]) -> np.ndarray:
    R = np.asarray(R, dtype=float)
    T = np.eye(4)
    T[:3, :3] = R.T
    T[:3, 3] = -R.T @ np.asarray(t, dtype=float)
    return T


def load_mapanything_cameras(path: Path, view_map: Mapping[str, str] | None = None) -> MapAnythingCameras:
    """cameras.json → camera_id → (R, t). view_map(camera_id → view 이름)가 없으면 이름으로 짝짓는다.

    자동 짝짓기는 view 이름이 camera_id 와 같거나 `camera_id_` 로 시작하는 것이다(교실 사진 `cam_A_01` 등). 카메라 하나에
    view 가 둘 이상이거나 하나도 없으면 오류(어느 view 인지 추측하지 않는다).
    """
    path = Path(path)
    raw = path.read_bytes()
    doc = json.loads(raw.decode("utf-8"))
    convention = str(doc.get("convention", ""))
    if "cam2world" not in convention or "opencv" not in convention.lower():
        raise ValueError(f"{path.name}: convention 이 cam2world·OpenCV 가 아니다: {convention!r}")
    views = {v["name"]: v for v in doc.get("views", [])}
    if not views:
        raise ValueError(f"{path.name}: views 가 없다")
    if view_map is None:
        view_map = _auto_view_map(views)
    out: Extrinsics = {}
    for cid, name in sorted(view_map.items()):
        if name not in views:
            raise ValueError(f"{cid}: cameras.json 에 view {name!r} 가 없다")
        R, t = cam2world_to_extrinsics(views[name]["cam2world"])
        out[cid] = (R.tolist(), t.tolist())
    centers = {cid: -np.asarray(R).T @ np.asarray(t) for cid, (R, t) in out.items()}
    _check_units(centers)
    record = {
        "path": path.name,
        "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "run_id": doc.get("run_id"),
        "convention": convention,
        "view_map": dict(view_map),
        "metric_scaling_factor": sorted({v.get("metric_scaling_factor") for v in views.values()} - {None}),
        "camera_centers_m": {cid: [round(float(x), 4) for x in c] for cid, c in centers.items()},
    }
    return MapAnythingCameras(out, record)


def _auto_view_map(views: Mapping[str, Any]) -> dict[str, str]:
    by_camera: dict[str, list[str]] = {}
    for name in views:
        parts = name.split("_")
        # cam_A_01 → cam_A, cam1 → cam1 (뒤의 번호 조각 하나만 뗀다)
        cid = "_".join(parts[:-1]) if len(parts) > 1 and parts[-1].isdigit() else name
        by_camera.setdefault(cid, []).append(name)
    dup = {cid: names for cid, names in by_camera.items() if len(names) > 1}
    if dup:
        raise ValueError(f"카메라 하나에 view 가 여럿이다(--view CAM=NAME 으로 고른다): {dup}")
    return {cid: names[0] for cid, names in by_camera.items()}


def _check_units(centers: Mapping[str, np.ndarray]) -> None:
    ids = sorted(centers)
    lo, hi = CAMERA_DISTANCE_RANGE_M
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            d = float(np.linalg.norm(centers[a] - centers[b]))
            if not lo <= d <= hi:
                raise ValueError(f"{a}–{b} 카메라 거리 {d:.3g} 가 미터로 그럴듯하지 않다({lo}~{hi} m). 단위를 확인한다")


def _orthonormal(M: np.ndarray, what: str) -> np.ndarray:
    if np.linalg.norm(M.T @ M - np.eye(3)) > ORTHONORMAL_TOL or np.linalg.det(M) <= 0:
        raise ValueError(f"{what} 이 정규직교 회전(det +1)이 아니다")
    U, _, Vt = np.linalg.svd(M)
    return U @ Vt


# --------------------------------------------------------------------------- World 정렬


def load_world_alignment(path: Path) -> WorldAlignment:
    """`{"format": "simai.world_alignment", "mapanything_to_world": 4×4 [sR | T]}` (-102 `world_frame.cli fix` 결과).

    status 가 OK 가 아니면(바닥 없음·축 모호) 오류다. cameras_json_sha256·inputs.intrinsics.sha256 이 있으면 record 에 옮겨
    cli 가 같은 MapAnything 실행의 cameras.json·같은 내부 파라미터 기록인지 확인한다(다른 실행의 world 로 정한 변환은 맞지 않는다).
    """
    path = Path(path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("format") != WORLD_ALIGNMENT_FORMAT:
        raise ValueError(f"{path.name}: World 정렬 형식이 아니다: {doc.get('format')!r}")
    if doc.get("status", "OK") != "OK":  # -102 가 바닥·축을 정하지 못한 결과(변환 없음)
        raise ValueError(f"{path.name}: World 고정 실패 결과다({doc.get('reason')}): {doc.get('detail')}")
    M = np.asarray(doc["mapanything_to_world"], dtype=float)
    if M.shape != (4, 4) or not np.all(np.isfinite(M)) or not np.allclose(M[3], [0, 0, 0, 1]):
        raise ValueError(f"{path.name}: mapanything_to_world 는 마지막 행이 [0,0,0,1] 인 4×4 다")
    det = float(np.linalg.det(M[:3, :3]))
    if det <= 0:
        raise ValueError(f"{path.name}: 닮음 변환의 det 가 양수가 아니다(왼손 변환)")
    s = det ** (1 / 3)
    Ra = _orthonormal(M[:3, :3] / s, "World 정렬 회전")
    record = {
        "path": path.name,
        "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        "source": SOURCE_PROVIDED,
        "scale": s,
        "mapanything_to_world": M.tolist(),
        "input_provenance": checked_provenance(doc.get("input_provenance"), path.name),
        "note": doc.get("note"),
        "cameras_json_sha256": doc.get("cameras_json_sha256"),
        "intrinsics_sha256": ((doc.get("inputs") or {}).get("intrinsics") or {}).get("sha256"),
    }
    return WorldAlignment(Ra, M[:3, 3].copy(), float(s), SOURCE_PROVIDED, record)


def alignment_from_people(
    observations: Sequence[PersonObservation],
    intrinsics: BundleAdjustmentInit,
    extrinsics: Mapping[str, tuple[Sequence[Sequence[float]], Sequence[float]]],
    *,
    min_confidence: float = 0.3,
    ankle_contact_height_m: float = 0.08,
) -> WorldAlignment:
    """임시 정렬(모듈 설명). 상방을 정할 표본이 MIN_UP_SAMPLES 보다 적으면 ValueError(지어내지 않는다)."""
    rigs = {}
    for cam in intrinsics.cameras:
        if cam.camera_id in extrinsics:
            R, t = extrinsics[cam.camera_id]
            rigs[cam.camera_id] = RigCamera.from_calibration({**cam.camera, "R": R, "t": t})
    tops, bottoms, ankles = [], [], []
    for obs in observations:
        pts = {}
        for j in (L_SHOULDER, R_SHOULDER, L_ANKLE, R_ANKLE):
            views = {
                cid: kps[j]
                for cid, kps in obs.views.items()
                if cid in rigs and kps[j] is not None and kps[j][2] >= min_confidence
            }
            if len(views) < 2:
                break
            ids = sorted(views)
            X = triangulate_point([rigs[c] for c in ids], np.array([views[c][:2] for c in ids], dtype=float))
            if X is None:
                break
            pts[j] = X
        if len(pts) < 4:
            continue
        top = (pts[L_SHOULDER] + pts[R_SHOULDER]) / 2
        bottom = (pts[L_ANKLE] + pts[R_ANKLE]) / 2
        if TORSO_LEG_RANGE_M[0] <= float(np.linalg.norm(top - bottom)) <= TORSO_LEG_RANGE_M[1]:
            tops.append(top)
            bottoms.append(bottom)
            ankles.append((pts[L_ANKLE], pts[R_ANKLE]))
    if len(tops) < MIN_UP_SAMPLES:
        raise ValueError(f"상방을 정할 사람 표본(어깨·발목이 2대 이상에서 보임)이 {len(tops)} 개다(필요 {MIN_UP_SAMPLES})")
    v = np.array(tops) - np.array(bottoms)
    units = v / np.linalg.norm(v, axis=1, keepdims=True)
    keep = np.ones(len(units), dtype=bool)
    up = units.mean(axis=0)
    for _ in range(3):
        up = units[keep].mean(axis=0)
        up /= np.linalg.norm(up)
        keep = units @ up >= math.cos(math.radians(UP_OUTLIER_DEG))
        if keep.sum() < MIN_UP_SAMPLES:
            raise ValueError(f"상방 방향이 모이지 않는다({UP_OUTLIER_DEG}° 안 표본 {int(keep.sum())} 개)")
    spread = float(np.degrees(np.median(np.arccos(np.clip(units[keep] @ up, -1, 1)))))
    x = np.array([1.0, 0.0, 0.0]) - up[0] * up
    if np.linalg.norm(x) < 0.1:
        x = np.array([0.0, 0.0, 1.0]) - up[2] * up
    x /= np.linalg.norm(x)
    Ra = np.vstack([x, np.cross(up, x), up])
    low = np.array([min((Ra @ a)[2], (Ra @ b)[2]) for (a, b), k in zip(ankles, keep, strict=True) if k])
    floor_z = float(np.median(low)) - ankle_contact_height_m
    record = {
        "source": SOURCE_INTERIM,
        "samples": int(keep.sum()),
        "rejected_samples": int((~keep).sum()),
        "up_in_mapanything": [round(float(c), 5) for c in up],
        "up_spread_median_deg": round(spread, 3),
        "floor_offset_m": round(floor_z, 4),
        "ankle_contact_height_m": ankle_contact_height_m,
        "note": "벽 방향·원점은 정하지 않았다(MapAnything X 축 수평 투영, MapAnything 원점 아래). World 좌표계 고정은 -102",
    }
    return WorldAlignment(Ra, np.array([0.0, 0.0, -floor_z]), 1.0, SOURCE_INTERIM, record)


# --------------------------------------------------------------------------- 출처


def checked_provenance(provenance: Any, where: str) -> dict[str, Any] | None:
    """입력 파일의 선택 input_provenance. 있으면 계약으로 검사한다(없으면 None: 출처를 따로 적지 않은 입력)."""
    if provenance is None:
        return None
    problems = check_input_provenance(provenance)
    if problems:
        raise ValueError(f"{where}: input_provenance 가 계약에 맞지 않는다: {problems}")
    return dict(provenance)


def _upper_source(provenance: Mapping[str, Any] | None) -> str | None:
    """상한 입력이면 그 출처(SIM 우선, 그 밖은 DATASET_GT), 아니면 None."""
    if not provenance:
        return None
    sources = [(provenance.get(k) or {}).get("source") for k in ("frames", "calibration", "tracks")]
    if "SIM" in sources:
        return "SIM"
    if "DATASET_GT" in sources or provenance.get("is_upper_bound"):
        return "DATASET_GT"
    return None


def combine_input_provenance(
    keypoints: Mapping[str, Any],
    others: Mapping[str, Mapping[str, Any] | None],
    initial_version: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(번들 조정에 넘길 input_provenance, 입력별 상한 출처 기록). 가장 보수적인 쪽으로 합친다(모듈 설명).

    keypoints 는 keypoint 파일 머리의 input_provenance(frames·tracks 는 그대로), others 는 입력 이름 → 그 입력의
    input_provenance(없으면 None). initial_version 은 초기 카메라(MapAnything) 식별자다.
    """
    upper = {"keypoints": _upper_source(keypoints), **{k: _upper_source(v) for k, v in others.items()}}
    found = set(upper.values()) - {None}
    calibration = {"source": "MODEL_ESTIMATE", "calibration_version": initial_version}
    if found:
        source = "SIM" if "SIM" in found else "DATASET_GT"
        kp_cal = keypoints.get("calibration") or {}
        version = kp_cal.get("calibration_version") if kp_cal.get("source") == source else None
        calibration = {"source": source, "calibration_version": version or f"{source.lower()}-input"}
    provenance = build_input_provenance(dict(keypoints["frames"]), calibration, keypoints.get("tracks"))
    return provenance, {"upper_bound_inputs": {k: v for k, v in upper.items() if v}, "calibration": calibration}
