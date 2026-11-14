"""관제 중 카메라별로 사람 지운 배경을 누적하는 모듈 (MapAnything 입력용, 명세 3.2·4.3 1단계, S15P31A101-358).

- 관제 중에는 MapAnything 을 돌리지 않고(GPU 여유 확보), Tier 0 가 이미 내는 원본 해상도 프레임(BGR)과 사람 박스 목록을
  받아 카메라별 고리 버퍼에 표본을 쌓는다. 이 모듈은 검출기를 직접 부르지 않는다.
- 표본 간격(`sample_interval_ms`), 카메라별 최대 표본 수(`max_samples_per_camera`), 전체 메모리 상한(`max_bytes`)을
  두고 원본 해상도로 저장하되, 상한을 넘으면 오래된 표본부터 버린다.
- 사람 박스는 좌우 `pad_x_ratio`(기본 0.15), 상하 `pad_y_ratio`(기본 0.10) 만큼 넓혀 가린다(`.agy/clean_bg.py` 기준).
- 배경 합성은 가려지지 않은 표본 값의 픽셀별 시간 중앙값(`nanmedian`, 행 묶음 계산)을 쓰고, 끝까지 한 번도 드러나지 않은
  자리는 전체 표본 중앙값으로 채우되 "못 채운 비율"(`never_revealed_ratio`)로 보고한다.
- 특정 카메라가 틀어졌을 때(`DRIFTED`)는 `reset_camera(camera_id)` 로 그 카메라 누적만 비운다.
- 카메라마다 표본 수 ≥ `min_samples_per_camera` 이고 못 채운 비율 < `max_unrevealed_ratio`(기본 0.02 = 2%)이면
  "충분히 찼다"(`is_camera_ready` / `is_ready`)로 판단한다.
- 내보내기(`export` / `export_prepared`)는 카메라별 배경 사진(`<camera_id>.jpg`), 메타 JSON(`clean_backgrounds.json`),
  그리고 `calibration.world_frame.prepare.prepare_inputs` 와 바로 이어지는 이미지 경로 사전을 낸다.
- 상태·합성·내보내기에 `since_ms` 를 주면 그 시각 이후 표본만 쓴다(세팅 시작 뒤 새로 찍은 표본만, S15P31A101-686).
  이때 준비 판단의 최소 표본 수는 `min_samples` 로 바꿀 수 있다(못 채운 비율 상한은 같다).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.calibration.world_frame.prepare import PersonSegmenter, prepare_inputs
from simai_ai.tracking.local_tracker import (
    BBox,
    CameraDetections,
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
    require_camera_id,
)
from simai_ai.tracking.time_model import require_ms

CLEAN_BACKGROUND_FORMAT = "simai.clean_backgrounds"
CLEAN_BACKGROUND_VERSION = "0.1.0"


def _finite_non_negative(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}: 0 이상 유한한 수여야 한다 (받은 값 {value!r})")
    return float(value)


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name}: 1 이상 정수여야 한다 (받은 값 {value!r})")
    return value


@dataclass(frozen=True)
class CleanBackgroundConfig:
    """카메라별 사람 지운 배경 누적기 설정."""

    sample_interval_ms: int = 3_000
    """같은 카메라에서 표본을 뽑는 최소 시간 간격 (기본 3,000 ms = 3초). 걷는 사람이 한 자리를 지나가는 시간보다 길게 둔다."""
    min_samples_per_camera: int = 20
    """'충분히 찼다' 판단에 필요한 카메라당 최소 표본 수 (3초 간격 기준 약 1분 분량)."""
    max_samples_per_camera: int = 60
    """카메라당 최대 보관 표본 수 (10/29 녹화 시험 `.agy/clean_bg.py` 기준 60장)."""
    max_bytes: int = 2_200 * 1024 * 1024
    """전체 카메라 표본 + 마스크 메모리 상한 (기본 약 2.2 GiB: 2304×1296 BGR+mask 60장×4대 ≈ 2.26 GB 수용)."""
    pad_x_ratio: float = 0.15
    """사람 박스 좌우 확장 비율 (박스 폭 대비, 기본 15%). 팔·그림자·검출 경계 여유."""
    pad_y_ratio: float = 0.10
    """사람 박스 상하 확장 비율 (박스 높이 대비, 기본 10%). 머리·발끝 여유."""
    max_unrevealed_ratio: float = 0.02
    """'충분히 찼다' 판단의 카메라별 못 채운 픽셀 비율 상한 (`never_revealed_ratio < max_unrevealed_ratio`, 기본 2%)."""
    rows_per_chunk: int = 64
    """중앙값 합성 시 한 번에 float32 로 펼치는 행 수 (메모리 피크 억제)."""
    jpeg_quality: int = 95
    """배경 사진 저장 JPEG 품질 (`world_frame.prepare` 와 같은 95)."""

    def __post_init__(self) -> None:
        if isinstance(self.sample_interval_ms, bool) or not isinstance(self.sample_interval_ms, int) or self.sample_interval_ms < 0:
            raise ValueError("sample_interval_ms 는 0 이상 정수여야 한다")
        _positive_int("min_samples_per_camera", self.min_samples_per_camera)
        _positive_int("max_samples_per_camera", self.max_samples_per_camera)
        if self.min_samples_per_camera > self.max_samples_per_camera:
            raise ValueError("min_samples_per_camera 는 max_samples_per_camera 이하여야 한다")
        _positive_int("max_bytes", self.max_bytes)
        _finite_non_negative("pad_x_ratio", self.pad_x_ratio)
        _finite_non_negative("pad_y_ratio", self.pad_y_ratio)
        unrev = _finite_non_negative("max_unrevealed_ratio", self.max_unrevealed_ratio)
        if unrev <= 0 or unrev > 1:
            raise ValueError("max_unrevealed_ratio 는 (0, 1] 범위여야 한다")
        _positive_int("rows_per_chunk", self.rows_per_chunk)
        if isinstance(self.jpeg_quality, bool) or not isinstance(self.jpeg_quality, int) or not (1 <= self.jpeg_quality <= 100):
            raise ValueError("jpeg_quality 는 1~100 정수여야 한다")


@dataclass(frozen=True)
class _Sample:
    capture_time_ms: int
    frame_bgr: np.ndarray
    reveal_mask: np.ndarray  # True = 사람이 안 가린 배경 픽셀, False = 사람 박스 영역
    person_cover_ratio: float
    byte_size: int


@dataclass(frozen=True)
class CameraBackgroundStatus:
    """카메라 한 대의 배경 누적 상태 보고."""

    camera_id: str
    sample_count: int
    time_range_ms: tuple[int, int] | None
    never_revealed_ratio: float | None
    mean_person_cover_ratio: float | None
    image_size: tuple[int, int] | None
    bytes_used: int
    ready: bool
    last_person_cover_ratio: float | None = None
    """마지막 표본의 사람 가림 비율(0 이면 사람 없음). 표본이 없으면 None (S15P31A101-686 세팅 촬영 안내용)."""

    def to_record(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "sample_count": self.sample_count,
            "time_range_ms": list(self.time_range_ms) if self.time_range_ms is not None else None,
            "never_revealed_ratio": round(self.never_revealed_ratio, 6) if self.never_revealed_ratio is not None else None,
            "mean_person_cover_ratio": round(self.mean_person_cover_ratio, 6) if self.mean_person_cover_ratio is not None else None,
            "image_size": list(self.image_size) if self.image_size is not None else None,
            "bytes_used": self.bytes_used,
            "ready": self.ready,
            "last_person_cover_ratio": (
                round(self.last_person_cover_ratio, 6) if self.last_person_cover_ratio is not None else None
            ),
        }


@dataclass(frozen=True)
class CleanBackgroundExport:
    """내보내기 결과: 카메라별 배경 사진 경로(`prepare_inputs` 의 `images` 인자 형식)와 메타 문서."""

    out_dir: Path
    meta_path: Path
    images: dict[str, Path]
    metadata: dict[str, Any]


def extract_person_bboxes(
    detections: Observation2DFrame | CameraDetections | Sequence[BBox | PoseDetection | TrackedDetection | Sequence[float]],
) -> tuple[BBox, ...]:
    """Tier 0 검출(`Observation2DFrame`, `CameraDetections`, `PoseDetection`, `TrackedDetection`, `[x1, y1, x2, y2]`)에서
    사람 박스 목록만 뽑는다."""
    if isinstance(detections, (Observation2DFrame, CameraDetections)):
        items: Sequence[Any] = detections.detections
    else:
        items = detections
    boxes: list[BBox] = []
    for item in items:
        if isinstance(item, TrackedDetection):
            raw = item.detection.bbox
        elif isinstance(item, PoseDetection):
            raw = item.bbox
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 4:
            raw = item
        else:
            raise ValueError(f"지원하지 않는 사람 박스 형식: {item!r}")
        x1, y1, x2, y2 = (float(v) for v in raw)
        if not (math.isfinite(x1) and math.isfinite(y1) and math.isfinite(x2) and math.isfinite(y2)):
            raise ValueError(f"bbox 좌표는 유한한 수여야 한다: {raw!r}")
        if x2 < x1 or y2 < y1:
            raise ValueError(f"bbox 는 x2 >= x1, y2 >= y1 이어야 한다: {raw!r}")
        boxes.append((x1, y1, x2, y2))
    return tuple(boxes)


def build_reveal_mask(
    shape_hw: tuple[int, int],
    person_boxes: Sequence[BBox],
    *,
    pad_x_ratio: float = 0.15,
    pad_y_ratio: float = 0.10,
) -> np.ndarray:
    """원본 해상도 (H, W) 에서 사람 박스를 넓혀 가린 bool 마스크(True = 드러난 배경, False = 사람 가림)를 만든다."""
    h, w = int(shape_hw[0]), int(shape_hw[1])
    mask = np.ones((h, w), dtype=bool)
    for x1, y1, x2, y2 in person_boxes:
        bw, bh = x2 - x1, y2 - y1
        px1 = max(0, int(math.floor(x1 - pad_x_ratio * bw)))
        px2 = min(w, int(math.ceil(x2 + pad_x_ratio * bw)) + 1)
        py1 = max(0, int(math.floor(y1 - pad_y_ratio * bh)))
        py2 = min(h, int(math.ceil(y2 + pad_y_ratio * bh)) + 1)
        if px2 > px1 and py2 > py1:
            mask[py1:py2, px1:px2] = False
    return mask


def compute_masked_median_background(
    frames: Sequence[np.ndarray],
    reveal_masks: Sequence[np.ndarray],
    *,
    rows_per_chunk: int = 64,
) -> tuple[np.ndarray, np.ndarray]:
    """가려지지 않은 픽셀들의 시간 중앙값 배경(uint8 BGR)과 끝까지 안 드러난 픽셀 마스크(bool H×W)를 계산한다.

    한 번도 드러나지 않은 픽셀(`never_revealed`)은 전체 표본의 단순 시간 중앙값으로 채워 빈 구멍(NaN/검은 점)을 남기지 않고,
    호출자가 `never_revealed_ratio` 로 보고·판단할 수 있게 마스크를 함께 돌려준다.
    """
    if not frames or len(frames) != len(reveal_masks):
        raise ValueError("프레임과 마스크 수가 맞지 않거나 비어 있다")
    first = np.asarray(frames[0])
    h, w = first.shape[:2]
    out = np.empty_like(first, dtype=np.uint8)
    never_revealed = np.zeros((h, w), dtype=bool)

    for r0 in range(0, h, rows_per_chunk):
        r1 = min(h, r0 + rows_per_chunk)
        chunk_frames = np.stack([np.asarray(f)[r0:r1] for f in frames], axis=0)  # (N, ch_h, W, 3) uint8
        chunk_masks = np.stack([np.asarray(m)[r0:r1] for m in reveal_masks], axis=0)  # (N, ch_h, W) bool
        ever_revealed = chunk_masks.any(axis=0)  # (ch_h, W)
        never_chunk = ~ever_revealed
        never_revealed[r0:r1] = never_chunk

        if chunk_masks.all():
            med = np.median(chunk_frames, axis=0)
            out[r0:r1] = np.clip(np.rint(med), 0, 255).astype(np.uint8)
            continue

        masked = chunk_frames.astype(np.float32)
        masked[~chunk_masks] = np.nan
        with np.errstate(all="ignore"):
            bg = np.nanmedian(masked, axis=0)
        if never_chunk.any():
            plain = np.median(chunk_frames, axis=0)
            bg[never_chunk] = plain[never_chunk]
        out[r0:r1] = np.clip(np.rint(bg), 0, 255).astype(np.uint8)

    return out, never_revealed


class CleanBackgroundAccumulator:
    """관제 중 카메라별로 사람 지운 배경을 누적하는 고리 버퍼."""

    def __init__(
        self,
        config: CleanBackgroundConfig | None = None,
        *,
        camera_ids: Sequence[str] | None = None,
    ) -> None:
        self.config = config or CleanBackgroundConfig()
        self._fixed_cameras = camera_ids is not None
        self._samples: dict[str, deque[_Sample]] = {}
        self._image_sizes: dict[str, tuple[int, int]] = {}
        for cid in camera_ids or ():
            norm = require_camera_id(cid)
            if norm in self._samples:
                raise ValueError(f"camera_ids 에 {norm} 가 중복되었다")
            self._samples[norm] = deque()

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._samples)

    @property
    def total_bytes(self) -> int:
        return sum(s.byte_size for q in self._samples.values() for s in q)

    def _ensure_camera(self, camera_id: str) -> deque[_Sample]:
        cid = require_camera_id(camera_id)
        q = self._samples.get(cid)
        if q is None:
            if self._fixed_cameras:
                raise KeyError(f"등록되지 않은 카메라: {cid}")
            q = self._samples[cid] = deque()
        return q

    def add_frame(
        self,
        camera_id: str,
        capture_time_ms: int,
        frame_bgr: np.ndarray,
        person_boxes: Observation2DFrame | CameraDetections | Sequence[BBox | PoseDetection | TrackedDetection | Sequence[float]] = (),
    ) -> bool:
        """프레임 하나와 그 프레임의 사람 박스 목록을 넣는다. 표본으로 채택되면 True, 간격 미달·역순이면 False."""
        q = self._ensure_camera(camera_id)
        require_ms("capture_time_ms", capture_time_ms)
        img = np.asarray(frame_bgr)
        if img.ndim != 3 or img.shape[2] != 3 or img.dtype != np.uint8:
            raise ValueError(f"{camera_id}: 프레임은 (H, W, 3) uint8 BGR 배열이어야 한다 (받은 shape={img.shape}, dtype={img.dtype})")
        h, w = int(img.shape[0]), int(img.shape[1])
        if h < 1 or w < 1:
            raise ValueError(f"{camera_id}: 프레임 크기가 비어 있다: {(w, h)}")

        expected_size = self._image_sizes.get(camera_id)
        if expected_size is not None and expected_size != (w, h):
            raise ValueError(
                f"{camera_id}: 프레임 해상도 {(w, h)} 가 기존 누적 해상도 {expected_size} 와 다르다 "
                "(해상도가 바뀌었으면 reset_camera 후 다시 쌓는다)"
            )

        if q:
            last_ms = q[-1].capture_time_ms
            if capture_time_ms < last_ms:
                return False
            if capture_time_ms - last_ms < self.config.sample_interval_ms:
                return False

        boxes = extract_person_bboxes(person_boxes)
        reveal_mask = build_reveal_mask(
            (h, w),
            boxes,
            pad_x_ratio=self.config.pad_x_ratio,
            pad_y_ratio=self.config.pad_y_ratio,
        )
        frame_copy = np.array(img, dtype=np.uint8, copy=True)
        byte_size = int(frame_copy.nbytes + reveal_mask.nbytes)
        if byte_size > self.config.max_bytes:
            raise ValueError(
                f"{camera_id}: 표본 한 장 크기({byte_size} bytes)가 max_bytes({self.config.max_bytes} bytes)를 넘는다"
            )

        cover_ratio = float(1.0 - reveal_mask.mean())
        sample = _Sample(
            capture_time_ms=capture_time_ms,
            frame_bgr=frame_copy,
            reveal_mask=reveal_mask,
            person_cover_ratio=cover_ratio,
            byte_size=byte_size,
        )
        self._image_sizes[camera_id] = (w, h)
        q.append(sample)
        while len(q) > self.config.max_samples_per_camera:
            q.popleft()
        self._evict_to_budget()
        return True

    def _evict_to_budget(self) -> None:
        """전체 메모리 상한(`max_bytes`)을 넘으면 가장 오래된 표본(카메라 중 표본이 가장 많은 쪽 우선, 동률이면 가장 옛 시각)부터 버린다."""
        while self.total_bytes > self.config.max_bytes:
            non_empty = [cid for cid, q in self._samples.items() if q]
            if not non_empty:
                break
            # 특정 카메라만 아사하지 않도록 가장 표본이 많은 카메라 중 가장 오래된 표본을 먼저 비운다.
            victim = min(non_empty, key=lambda cid: (-len(self._samples[cid]), self._samples[cid][0].capture_time_ms, cid))
            self._samples[victim].popleft()
            if not self._samples[victim]:
                self._image_sizes.pop(victim, None)

    def reset_camera(self, camera_id: str) -> None:
        """DRIFTED 등으로 특정 카메라의 누적 배경 표본을 비운다."""
        cid = require_camera_id(camera_id)
        if cid in self._samples:
            self._samples[cid].clear()
        self._image_sizes.pop(cid, None)

    def _window(self, camera_id: str, since_ms: int | None) -> list[_Sample]:
        """카메라 표본 중 `since_ms` 이후(포함) 것만. None 이면 전부."""
        q = self._ensure_camera(camera_id)
        if since_ms is None:
            return list(q)
        require_ms("since_ms", since_ms)
        return [s for s in q if s.capture_time_ms >= since_ms]

    def _required(self, min_samples: int | None) -> int:
        return self.config.min_samples_per_camera if min_samples is None else _positive_int("min_samples", min_samples)

    def camera_status(
        self,
        camera_id: str,
        *,
        since_ms: int | None = None,
        min_samples: int | None = None,
    ) -> CameraBackgroundStatus:
        """카메라 한 대의 누적 상태(표본 수, 시각 범위, 못 채운 비율, 사람 가림 평균 비율, 준비 여부)를 돌려준다.

        `since_ms` 를 주면 그 시각 이후 표본만 센다. `min_samples` 는 준비 판단의 최소 표본 수(기본 설정값)다.
        """
        q = self._window(camera_id, since_ms)
        required = self._required(min_samples)
        if not q:
            return CameraBackgroundStatus(
                camera_id=camera_id,
                sample_count=0,
                time_range_ms=None,
                never_revealed_ratio=None,
                mean_person_cover_ratio=None,
                image_size=None,
                bytes_used=0,
                ready=False,
            )
        ever_revealed = np.logical_or.reduce([s.reveal_mask for s in q])
        never_ratio = float(1.0 - ever_revealed.mean())
        mean_cover = float(np.mean([s.person_cover_ratio for s in q]))
        count = len(q)
        ready = count >= required and never_ratio < self.config.max_unrevealed_ratio
        return CameraBackgroundStatus(
            camera_id=camera_id,
            sample_count=count,
            time_range_ms=(q[0].capture_time_ms, q[-1].capture_time_ms),
            never_revealed_ratio=never_ratio,
            mean_person_cover_ratio=mean_cover,
            image_size=self._image_sizes[camera_id],
            bytes_used=sum(s.byte_size for s in q),
            ready=ready,
            last_person_cover_ratio=q[-1].person_cover_ratio,
        )

    def status(self) -> dict[str, CameraBackgroundStatus]:
        """등록된 모든 카메라의 상태를 돌려준다."""
        return {cid: self.camera_status(cid) for cid in sorted(self._samples)}

    def is_camera_ready(self, camera_id: str) -> bool:
        """해당 카메라가 최소 표본 수를 넘고 못 채운 비율 < max_unrevealed_ratio(기본 2%) 인지 돌려준다."""
        return self.camera_status(camera_id).ready

    def is_ready(self, camera_ids: Sequence[str] | None = None) -> bool:
        """대상 카메라(생략하면 누적기에 있는 전체 카메라)가 모두 준비되었는지 돌려준다."""
        targets = tuple(camera_ids) if camera_ids is not None else tuple(self._samples)
        if not targets:
            return False
        return all(self.is_camera_ready(cid) for cid in targets)

    def synthesize_camera(
        self,
        camera_id: str,
        *,
        since_ms: int | None = None,
        min_samples: int | None = None,
    ) -> tuple[np.ndarray, CameraBackgroundStatus]:
        """카메라 한 대의 사람 지운 배경 영상(BGR uint8)과 상태를 만든다(`since_ms` 면 그 뒤 표본만)."""
        q = self._window(camera_id, since_ms)
        required = self._required(min_samples)
        if not q:
            raise ValueError(f"{camera_id}: 누적된 표본이 없다")
        bg, never_mask = compute_masked_median_background(
            [s.frame_bgr for s in q],
            [s.reveal_mask for s in q],
            rows_per_chunk=self.config.rows_per_chunk,
        )
        never_ratio = float(never_mask.mean())
        mean_cover = float(np.mean([s.person_cover_ratio for s in q]))
        count = len(q)
        ready = count >= required and never_ratio < self.config.max_unrevealed_ratio
        st = CameraBackgroundStatus(
            camera_id=camera_id,
            sample_count=count,
            time_range_ms=(q[0].capture_time_ms, q[-1].capture_time_ms),
            never_revealed_ratio=never_ratio,
            mean_person_cover_ratio=mean_cover,
            image_size=self._image_sizes[camera_id],
            bytes_used=sum(s.byte_size for s in q),
            ready=ready,
            last_person_cover_ratio=q[-1].person_cover_ratio,
        )
        return bg, st

    def export(
        self,
        out_dir: Path,
        *,
        calibration_version: str,
        camera_ids: Sequence[str] | None = None,
        require_ready: bool = True,
        since_ms: int | None = None,
        min_samples: int | None = None,
    ) -> CleanBackgroundExport:
        """카메라별 사람 지운 배경 사진(`<camera_id>.jpg`)과 메타 JSON(`clean_backgrounds.json`)을 새 폴더에 내보낸다.

        `since_ms` 를 주면 그 시각 이후 표본만으로 만들고 메타 카메라 항목에 `sample_since_ms` 로 남긴다.

        반환된 `export.images`(`dict[str, Path]`)는 `calibration.world_frame.prepare.prepare_inputs(images, ...)` 의
        첫 인자로 바로 넘길 수 있다.
        """
        import cv2

        if not isinstance(calibration_version, str) or not calibration_version.strip():
            raise ValueError("calibration_version 은 비어 있지 않은 문자열이어야 한다")
        out_dir = Path(out_dir)
        if out_dir.exists():
            raise FileExistsError(f"{out_dir} 가 이미 있다(덮어쓰지 않는다)")

        targets = sorted(camera_ids if camera_ids is not None else self._samples)
        if not targets:
            raise ValueError("내보낼 카메라가 없다")

        synthesized: list[tuple[str, np.ndarray, CameraBackgroundStatus]] = []
        for cid in targets:
            bg, st = self.synthesize_camera(cid, since_ms=since_ms, min_samples=min_samples)
            if require_ready and not st.ready:
                raise ValueError(
                    f"{cid}: 배경 누적이 아직 충분하지 않다 "
                    f"(samples={st.sample_count}/{self._required(min_samples)}, "
                    f"never_revealed_ratio={st.never_revealed_ratio:.4f}, limit={self.config.max_unrevealed_ratio})"
                )
            synthesized.append((cid, bg, st))

        out_dir.mkdir(parents=True)
        images: dict[str, Path] = {}
        cameras_meta: dict[str, Any] = {}
        for cid, bg, st in synthesized:
            img_path = out_dir / f"{cid}.jpg"
            ok = cv2.imwrite(str(img_path), bg, [cv2.IMWRITE_JPEG_QUALITY, self.config.jpeg_quality])
            if not ok:  # pragma: no cover
                raise OSError(f"{cid}: 배경 사진을 쓰지 못함 ({img_path})")
            images[cid] = img_path
            assert st.image_size is not None and st.time_range_ms is not None
            assert st.never_revealed_ratio is not None and st.mean_person_cover_ratio is not None
            cameras_meta[cid] = {
                "camera_id": cid,
                "image": img_path.name,
                "image_sha256": "sha256:" + hashlib.sha256(img_path.read_bytes()).hexdigest(),
                "image_size": list(st.image_size),
                "sample_count": st.sample_count,
                "time_range_ms": list(st.time_range_ms),
                "never_revealed_ratio": round(st.never_revealed_ratio, 6),
                "mean_person_cover_ratio": round(st.mean_person_cover_ratio, 6),
                "ready": st.ready,
                "calibration_version": calibration_version,
            }
            if since_ms is not None:
                cameras_meta[cid]["sample_since_ms"] = since_ms

        meta_doc: dict[str, Any] = {
            "format": CLEAN_BACKGROUND_FORMAT,
            "format_version": CLEAN_BACKGROUND_VERSION,
            "calibration_version": calibration_version,
            "config": {
                "sample_interval_ms": self.config.sample_interval_ms,
                "min_samples_per_camera": self.config.min_samples_per_camera,
                "max_samples_per_camera": self.config.max_samples_per_camera,
                "max_bytes": self.config.max_bytes,
                "pad_x_ratio": self.config.pad_x_ratio,
                "pad_y_ratio": self.config.pad_y_ratio,
                "max_unrevealed_ratio": self.config.max_unrevealed_ratio,
            },
            "ready": all(st.ready for _, _, st in synthesized),
            "cameras": cameras_meta,
        }
        meta_path = out_dir / "clean_backgrounds.json"
        meta_path.write_text(json.dumps(meta_doc, ensure_ascii=False, indent=2), encoding="utf-8")
        return CleanBackgroundExport(out_dir=out_dir, meta_path=meta_path, images=images, metadata=meta_doc)

    def export_prepared(
        self,
        backgrounds_dir: Path,
        prepared_dir: Path,
        intrinsics_record: Mapping[str, Any],
        *,
        calibration_version: str,
        input_provenance: Mapping[str, Any],
        segmenter: PersonSegmenter | None = None,
        dilate_px: int = 15,
        intrinsics_ref: Mapping[str, Any] | None = None,
        camera_ids: Sequence[str] | None = None,
        require_ready: bool = True,
    ) -> tuple[CleanBackgroundExport, dict[str, Any]]:
        """배경 사진·메타를 내보내고 이어서 `world_frame.prepare.prepare_inputs` 를 호출해 MapAnything 입력 폴더를 만든다."""
        exported = self.export(
            backgrounds_dir,
            calibration_version=calibration_version,
            camera_ids=camera_ids,
            require_ready=require_ready,
        )
        prepared_doc = prepare_inputs(
            exported.images,
            intrinsics_record,
            prepared_dir,
            segmenter=segmenter,
            input_provenance=input_provenance,
            dilate_px=dilate_px,
            intrinsics_ref=intrinsics_ref,
        )
        return exported, prepared_doc
