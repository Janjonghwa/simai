"""Ultralytics 추적기(TrackTrack·ByteTrack·BoT-SORT)를 프레임 없이 검출 배열만으로 쓰는 백엔드와 YOLO 결과 어댑터.

extras `detection`(ultralytics, lap)이 있어야 import 된다. `local_tracker` 는 이 모듈을 백엔드를 만들 때만 불러온다.

Ultralytics 추적기 계약 (8.4.178 에서 확인):
- `update(results, img=None, feats=None)`. results 는 `conf`, `cls`, `xywh`, `xyxy` 배열을 갖고 boolean 색인과
  `len()` 을 지원하는 객체면 된다(원래는 `Results.boxes.cpu().numpy()`).
- img 는 카메라 움직임 보정(GMC)과 외부 ReID 모델에만 쓴다. GMC 를 끄고(`gmc_method: none`) ReID 를 검출기 특징
  (`model: auto`, feats)으로만 쓰면 영상이 필요 없다. 그래서 img 는 늘 None 으로 넘긴다.
- 반환은 이번 프레임에 갱신된 트랙의 `[x1, y1, x2, y2, track_id, score, cls, idx]` 행이다. idx 는 넣은 검출의 인덱스이고
  박스는 칼만 상태라서 여기서는 (track_id, idx)만 쓴다(관측 값은 검출 원본을 쓴다).
- 트랙 ID 카운터는 추적기 인스턴스마다 따로다(카메라 간 ID 공간이 섞이지 않음).
"""

from __future__ import annotations

from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import numpy as np

from simai_ai.tracking.local_config import TrackerConfig, TrackerKind
from simai_ai.tracking.local_tracker import BBox, Keypoint, PoseDetection

PERSON_CLASS = 0
"""COCO person. 추적기는 클래스를 섞지 않도록 cls 를 그대로 돌려주기만 한다"""


def _tracker_classes() -> dict[TrackerKind, Any]:
    try:
        from ultralytics import trackers
    except ImportError as exc:  # pragma: no cover - 설치 환경 문제
        raise ImportError("Local Tracker 백엔드에는 extras detection(ultralytics, lap)이 필요하다") from exc
    missing = [name for name in ("TRACKTRACK", "BYTETracker", "BOTSORT") if not hasattr(trackers, name)]
    if missing:
        import ultralytics

        raise ImportError(
            f"설치된 ultralytics {ultralytics.__version__} 에 {missing} 가 없다. TrackTrack 이 있는 버전(8.4.178 확인)이 필요하다"
        )
    return {
        TrackerKind.TRACKTRACK: trackers.TRACKTRACK,
        TrackerKind.BYTETRACK: trackers.BYTETracker,
        TrackerKind.BOTSORT: trackers.BOTSORT,
    }


class DetectionArrays:
    """Ultralytics 추적기가 받는 검출 묶음(`Boxes.cpu().numpy()` 대용). 픽셀 xyxy·신뢰도·클래스."""

    def __init__(self, xyxy: np.ndarray, conf: np.ndarray, cls: np.ndarray) -> None:
        self.xyxy = np.asarray(xyxy, dtype=np.float32).reshape(-1, 4)
        self.conf = np.asarray(conf, dtype=np.float32).reshape(-1)
        self.cls = np.asarray(cls, dtype=np.float32).reshape(-1)
        if not len(self.xyxy) == len(self.conf) == len(self.cls):
            raise ValueError("xyxy·conf·cls 길이가 다르다")

    @property
    def xywh(self) -> np.ndarray:
        x1, y1, x2, y2 = self.xyxy.T
        return np.stack([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1], axis=1)

    def __len__(self) -> int:
        return len(self.conf)

    def __getitem__(self, index: Any) -> DetectionArrays:
        return DetectionArrays(self.xyxy[index], self.conf[index], self.cls[index])


class UltralyticsTrackBackend:
    """카메라 하나 전용 Ultralytics 추적기. 설정은 TrackerConfig 의 명시 값만 넘긴다."""

    def __init__(self, config: TrackerConfig) -> None:
        self.config = config
        self._tracker = _tracker_classes()[config.kind](SimpleNamespace(**config.as_dict()))

    def update(
        self,
        boxes: Sequence[BBox],
        scores: Sequence[float],
        features: Sequence[Sequence[float]] | None = None,
    ) -> list[tuple[int, int]]:
        dets = DetectionArrays(
            np.asarray(boxes, dtype=np.float32).reshape(-1, 4),
            np.asarray(scores, dtype=np.float32),
            np.full(len(scores), PERSON_CLASS, dtype=np.float32),
        )
        feats = None
        if features is not None and len(features):
            if not self.config.with_reid:
                raise ValueError("with_reid 가 꺼진 설정에 ReID 특징을 넣었다")
            feats = np.asarray(features, dtype=np.float32)
        out = self._tracker.update(dets, img=None, feats=feats)
        return [(int(row[4]), int(row[7])) for row in np.asarray(out).reshape(-1, 8)]

    def live_track_ids(self) -> tuple[frozenset[int], frozenset[int]]:
        tracked = frozenset(int(t.track_id) for t in self._tracker.tracked_stracks)
        lost = frozenset(int(t.track_id) for t in self._tracker.lost_stracks)
        return tracked, lost


def _to_numpy(value: Any) -> np.ndarray:
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    return np.asarray(value)


def pose_detections_from_result(result: Any) -> tuple[PoseDetection, ...]:
    """YOLO26-pose 결과 하나(`ultralytics.engine.results.Results`, 이미지 한 장)를 PoseDetection 으로 바꾼다.

    박스·신뢰도는 `result.boxes.xyxy`·`.conf`, keypoint 는 `result.keypoints.data`([N, K, 3] = u, v, confidence)를
    그대로 옮긴다. keypoint 가 없는 모델이면 빈 튜플, 값이 유한하지 않은 관절은 None 이다(지어내지 않음).
    사람(cls 0)만 남긴다. 묶음 추론이면 결과 목록의 각 항목에 카메라 ID·프레임 헤더를 붙여 CameraDetections 를 만든다.
    """
    boxes = result.boxes
    if boxes is None or len(boxes) == 0:
        return ()
    xyxy = _to_numpy(boxes.xyxy).reshape(-1, 4)
    conf = _to_numpy(boxes.conf).reshape(-1)
    cls = _to_numpy(boxes.cls).reshape(-1) if getattr(boxes, "cls", None) is not None else np.zeros(len(conf))
    kp_obj = getattr(result, "keypoints", None)
    kp_data = _to_numpy(kp_obj.data) if kp_obj is not None else None
    if kp_data is not None and kp_data.size and kp_data.shape[-1] != 3:
        raise ValueError(f"keypoint 는 [u, v, confidence] 여야 한다 (받은 모양 {kp_data.shape}, 신뢰도 없는 모델)")
    out = []
    for i in range(len(conf)):
        if int(cls[i]) != PERSON_CLASS:
            continue
        kps: tuple[Keypoint, ...] = ()
        if kp_data is not None and len(kp_data) > i:
            # 관절마다 np.isfinite 를 부르지 않고 사람 한 명의 관절 전체를 한 번에 본다(-663). tolist 는 float() 와 같은 값이다
            rows = kp_data[i].reshape(-1, 3)
            finite = np.isfinite(rows).all(axis=1).tolist()
            kps = tuple(
                (float(u), float(v), float(c)) if ok else None
                for (u, v, c), ok in zip(rows.tolist(), finite, strict=True)
            )
        out.append(PoseDetection(bbox=tuple(float(v) for v in xyxy[i]), score=float(conf[i]), keypoints_2d=kps))
    return tuple(out)
