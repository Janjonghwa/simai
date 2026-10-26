"""HMR 프롬프트 구성: 사람 박스 + YOLO26-seg 마스크 + 안정 관절 keypoint (명세 5.6, S15P31A101-87, docs/design/hmr-multiview-aggregation.md).

입력은 카메라 한 대·한 시각의 사람 관측(Tier 0 per_camera 또는 같은 YOLO26x-pose 검출: 원본 픽셀 bbox·COCO17 keypoint)과,
HMR 에 넣는 그림(왜곡을 편 그림, `HmrImageMap`)에서 YOLO26-seg 가 낸 사람 인스턴스다. 출력은 사람마다 HMR 입력 그림 좌표의
bbox·마스크(맞는 것이 있을 때만)·keypoint 프롬프트다. 추론은 하지 않는다(GPU 쪽 `ai/experiments/hmr/run_multiview_hmr.py`, -227 워커).

- keypoint 프롬프트는 몸통·고관절(COCO 어깨 5·6, 엉덩이 11·12)만 쓴다. 손목·손은 넣지 않는다(명세 5.6).
  신뢰도가 기준 미만이거나 HMR 그림으로 옮길 수 없거나(왜곡 제거 실패) bbox 밖이면 그 관절은 넣지 않는다(지어내지 않음).
- 마스크는 사람 하나에 인스턴스 하나, 인스턴스 하나는 사람 하나에만 준다(박스 IoU 큰 순서, 기준 미만은 마스크 없음).
  마스크가 없으면 bbox 만으로 돈다(다른 사람 마스크를 빌리지 않음).
- 같은 카메라에서 같은 World ID 의 관측이 둘이면 둘 다 프롬프트를 만들지 않는다(어느 쪽이 그 사람인지 모름, 명세 5.2).
- keypoint 인덱스는 MHR70(sam_3d_body/metadata/mhr70.py) 순서다. COCO17 → MHR70 대응은 `COCO17_TO_MHR70`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from simai_ai.geometry.coordinates import PinholeCamera
from simai_ai.geometry.rig import RigCamera

# COCO17 관절 j 의 MHR70 인덱스 (mhr70: 0~8 은 COCO 와 같고, 손목 왼 62·오른 41, 엉덩이 9·10, 무릎 11·12, 발목 13·14)
COCO17_TO_MHR70 = (0, 1, 2, 3, 4, 5, 6, 7, 8, 62, 41, 9, 10, 11, 12, 13, 14)
# 몸통·고관절 = 어깨 2 + 엉덩이 2 (COCO17 번호). 손목·손 제외 (명세 5.6)
STABLE_COCO17 = (5, 6, 11, 12)
DEFAULT_MIN_KEYPOINT_CONFIDENCE = 0.5
DEFAULT_MIN_MASK_IOU = 0.5
# SAM 3D Body keypoint 프롬프트의 빈 칸 표시(sam3d_body.py 의 dummy prompt 라벨)
DUMMY_PROMPT_LABEL = -2
BBOX_EDGE_SAMPLES = 9  # 원본 bbox 를 펴진 그림으로 옮길 때 변마다 찍는 점 수(배럴 왜곡은 변을 굽힌다)

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PersonObservation:
    """카메라 한 대에서 본 사람 한 명(원본 픽셀). world_id 가 없으면 Tier 0 연결 전 검출이다."""

    camera_id: str
    bbox: Box
    keypoints: tuple[Sequence[float] | None, ...] = ()  # COCO17 (u, v, 신뢰도), 원본 픽셀
    world_id: str | None = None
    identity_revision: int | None = None
    local_track_id: str | None = None
    score: float = 1.0


@dataclass(frozen=True, eq=False)
class SegInstance:
    """HMR 입력 그림에서 YOLO26-seg 가 낸 사람 인스턴스 하나."""

    bbox: Box
    score: float
    mask: np.ndarray  # (H, W) bool, HMR 입력 그림 좌표


@dataclass(frozen=True, eq=False)
class HmrPrompt:
    """사람 한 명의 HMR 프롬프트(HMR 입력 그림 좌표)."""

    observation: PersonObservation
    bbox: Box
    mask: np.ndarray | None
    mask_index: int | None  # instances 목록의 순서, 없으면 None
    mask_iou: float | None
    keypoints: tuple[tuple[int, float, float], ...]  # (MHR70 인덱스, u, v)


@dataclass(frozen=True)
class SkippedObservation:
    observation: PersonObservation
    reason: str


@dataclass(frozen=True, eq=False)
class HmrImageMap:
    """원본 픽셀 → HMR 입력 그림 픽셀. 왜곡을 편 그림이면 K_used(펴진 그림의 K)로, 아니면 그대로다.

    `ai/experiments/hmr/fs3db_common.prepare_image` 가 `cv2.undistort(img, K, D, None, K_used)` 로 편 그림과 같은 사상이다
    (K_used · 왜곡 제거 정규화 좌표). 왜곡 제거가 실패한 점은 NaN 이다(`RigCamera.normalize`).
    """

    camera: RigCamera
    K_used: np.ndarray | None = None
    image_size: tuple[int, int] | None = field(default=None)  # HMR 입력 그림 (W, H). None 이면 보정 image_size

    @classmethod
    def from_intrinsics(
        cls,
        camera_id: str,
        K: Sequence[Sequence[float]],
        image_size: Sequence[int],
        distortion_model: str = "NONE",
        distortion_coeffs: Sequence[float] = (),
        K_used: Sequence[Sequence[float]] | None = None,
    ) -> HmrImageMap:
        """내부 파라미터만으로 만든다(외부 파라미터는 쓰지 않으므로 R = I, t = 0)."""
        cam = RigCamera(
            camera_id=camera_id,
            pinhole=PinholeCamera(K=K, R=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)), t=(0.0, 0.0, 0.0)),
            image_size=(int(image_size[0]), int(image_size[1])),
            distortion_model=distortion_model,
            distortion_coeffs=tuple(float(c) for c in distortion_coeffs),
        )
        return cls(cam, None if K_used is None else np.asarray(K_used, dtype=float))

    @property
    def size(self) -> tuple[int, int]:
        return self.image_size or self.camera.image_size

    def points(self, uv: np.ndarray) -> np.ndarray:
        uv = np.asarray(uv, dtype=float).reshape(-1, 2)
        if self.K_used is None:
            return uv.copy()
        xn = self.camera.normalize(uv)
        homo = np.column_stack([xn, np.ones(len(xn))]) @ self.K_used.T
        return homo[:, :2] / homo[:, 2:3]

    def bbox(self, box: Box) -> Box | None:
        """bbox 의 변을 따라 찍은 점을 옮겨 외접 사각형을 낸다. 옮길 수 없는 점이 있으면 None(지어내지 않음)."""
        x0, y0, x1, y1 = (float(v) for v in box)
        s = np.linspace(0.0, 1.0, BBOX_EDGE_SAMPLES)
        xs, ys = x0 + (x1 - x0) * s, y0 + (y1 - y0) * s
        edge = np.vstack([
            np.column_stack([xs, np.full_like(xs, y0)]),
            np.column_stack([xs, np.full_like(xs, y1)]),
            np.column_stack([np.full_like(ys, x0), ys]),
            np.column_stack([np.full_like(ys, x1), ys]),
        ])
        p = self.points(edge)
        if not np.isfinite(p).all():
            return None
        w, h = self.size
        out = (max(0.0, float(p[:, 0].min())), max(0.0, float(p[:, 1].min())),
               min(float(w), float(p[:, 0].max())), min(float(h), float(p[:, 1].max())))
        return out if out[2] > out[0] and out[3] > out[1] else None


def box_iou(a: Box, b: Box) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def valid_box(box: Sequence[float]) -> bool:
    return len(box) == 4 and all(math.isfinite(float(v)) for v in box) and box[2] > box[0] and box[3] > box[1]


def observations_from_world_tracks(doc: Mapping[str, Any], camera_id: str) -> list[PersonObservation]:
    """world_tracks 문서(contracts/providers/world_tracks) 한 시각에서 그 카메라의 관측을 꺼낸다."""
    out = []
    for track in doc["tracks"]:
        for obs in track.get("per_camera") or ():
            if obs["camera_id"] != camera_id:
                continue
            out.append(PersonObservation(
                camera_id=camera_id,
                bbox=tuple(float(v) for v in obs["bbox"]),
                keypoints=tuple(obs.get("keypoints_2d") or ()) if obs.get("joint_layout", "COCO17") == "COCO17" else (),
                world_id=track["world_id"],
                identity_revision=int(track["identity_revision"]),
                local_track_id=obs.get("local_track_id"),
            ))
    return out


def stable_keypoint_prompts(
    observation: PersonObservation, image_map: HmrImageMap, bbox: Box, min_confidence: float = DEFAULT_MIN_KEYPOINT_CONFIDENCE
) -> tuple[tuple[int, float, float], ...]:
    """몸통·고관절 keypoint → (MHR70 인덱스, u, v) HMR 그림 좌표. 쓸 수 없는 관절은 넣지 않는다."""
    picks, pixels = [], []
    for j in STABLE_COCO17:
        kp = observation.keypoints[j] if j < len(observation.keypoints) else None
        if kp is None or len(kp) < 3:
            continue
        u, v, c = (float(x) for x in kp[:3])
        if not (math.isfinite(u) and math.isfinite(v) and math.isfinite(c)) or c < min_confidence:
            continue
        picks.append(j)
        pixels.append((u, v))
    if not picks:
        return ()
    mapped = image_map.points(np.asarray(pixels))
    out = []
    for j, (u, v) in zip(picks, mapped):
        if math.isfinite(u) and math.isfinite(v) and bbox[0] <= u <= bbox[2] and bbox[1] <= v <= bbox[3]:
            out.append((COCO17_TO_MHR70[j], float(u), float(v)))
    return tuple(out)


def build_prompts(
    observations: Sequence[PersonObservation],
    instances: Sequence[SegInstance],
    image_map: HmrImageMap,
    *,
    min_mask_iou: float = DEFAULT_MIN_MASK_IOU,
    min_keypoint_confidence: float = DEFAULT_MIN_KEYPOINT_CONFIDENCE,
) -> tuple[list[HmrPrompt], list[SkippedObservation]]:
    """카메라 한 대·한 시각의 관측 → HMR 프롬프트. 결과 순서는 관측 순서(건너뛴 것은 빠짐)."""
    cams = {o.camera_id for o in observations}
    if len(cams) > 1:
        raise ValueError(f"카메라 한 대의 관측만 받는다: {sorted(cams)}")
    if cams and image_map.camera.camera_id not in cams:
        raise ValueError(f"관측 카메라 {sorted(cams)} 와 그림 사상 카메라 {image_map.camera.camera_id} 가 다르다")
    skipped: list[SkippedObservation] = []
    seen: dict[str, int] = {}
    for o in observations:
        if o.world_id is not None:
            seen[o.world_id] = seen.get(o.world_id, 0) + 1
    usable: list[tuple[PersonObservation, Box]] = []
    for o in observations:
        if o.world_id is not None and seen[o.world_id] > 1:
            skipped.append(SkippedObservation(o, "DUPLICATE_WORLD_ID_IN_CAMERA"))
            continue
        if not valid_box(o.bbox):
            skipped.append(SkippedObservation(o, "INVALID_BBOX"))
            continue
        mapped = image_map.bbox(o.bbox)
        if mapped is None:
            skipped.append(SkippedObservation(o, "BBOX_NOT_MAPPABLE"))
            continue
        usable.append((o, mapped))

    pairs = []
    for i, (_o, box) in enumerate(usable):
        for k, inst in enumerate(instances):
            if valid_box(inst.bbox):
                iou = box_iou(box, inst.bbox)
                if iou >= min_mask_iou:
                    pairs.append((-iou, i, k))
    pairs.sort()
    person_mask: dict[int, tuple[int, float]] = {}
    taken: set[int] = set()
    for neg_iou, i, k in pairs:
        if i in person_mask or k in taken:
            continue
        person_mask[i] = (k, -neg_iou)
        taken.add(k)

    prompts = []
    for i, (o, box) in enumerate(usable):
        k, iou = person_mask.get(i, (None, None))
        prompts.append(HmrPrompt(
            observation=o,
            bbox=box,
            mask=None if k is None else np.asarray(instances[k].mask, dtype=bool),
            mask_index=k,
            mask_iou=None if iou is None else round(iou, 4),
            keypoints=stable_keypoint_prompts(o, image_map, box, min_keypoint_confidence),
        ))
    return prompts, skipped


def keypoint_prompt_array(prompt: HmrPrompt, slots: int = len(STABLE_COCO17)) -> np.ndarray:
    """SAM 3D Body keypoint 프롬프트 모양 (slots, 3) = [u, v, MHR70 인덱스], 빈 칸은 [0, 0, −2] (HMR 그림 픽셀).

    모델의 crop 정규화(`_full_to_crop`)는 GPU 쪽에서 한다(`ai/experiments/hmr/run_multiview_hmr.py`).
    """
    out = np.zeros((slots, 3), dtype=np.float32)
    out[:, 2] = DUMMY_PROMPT_LABEL
    for row, (idx, u, v) in enumerate(prompt.keypoints[:slots]):
        out[row] = (u, v, idx)
    return out
