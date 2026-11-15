"""Tier 0 비동기 Re-ID 특징 수집기 및 로컬 트랙 특징 캐시 (S15P31A101-463, 명세 5.2).

규칙:
1. YOLO26x-pose 추론·삼각측량·위험 판정 루프(10 Hz)를 절대 막지 않는다.
   - `submit_observation` 은 디코딩된 프레임에서 품질 게이트(최소 검출 신뢰도, 최소 bbox 크기, 최소 신체 가시성)와
     로컬 트랙별 갱신 주기(새/불확실 트랙 750 ms, 혼자 안정적인 확정 트랙 3000 ms)를 통과한
     프레임 참조·박스만 유계 대기열에 넣는다. 자르기·복사·색변환은 작업 스레드에서 한다.
   - 별도 데몬 스레드(`tier0-reid`)가 대기열에서 최대 `batch_size`(기본 8)개씩 꺼내 COPE 배치 추론을 수행하고
     `LocalTrackFeatureCache` 에 `(camera_id, local_track_id)` 별 최신 임베딩·가시성·품질 점수를 갱신한다.
   - 원본 프레임·사람 crop 이미지는 추론 직후 메모리에서 버리고 디스크에 절대 저장하지 않는다(명세 11.5).
2. 설정 스위치(`enabled=False`), GPU 미사용 환경, 또는 COPE 저장소·가중치 파일 부재 시 예외를 던지지 않고
   조용히 `state="disabled"`(사유 `reason` 기록)로 전환하여 기존 기하 추적과 완전히 동일하게 동작한다.
3. 오프라인 재생(`track_replay`)에서는 한 번 추출해 저장한 `.npz` 특징 캐시(임베딩·가시성·메타데이터만 포함,
   이미지 없음)를 `LocalTrackFeatureCache.load_npz` 로 불러와 시각 `t` 기준으로 결정론적 조회를 수행한다.
"""

from __future__ import annotations

import bisect
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any

import numpy as np

from simai_ai.tracking.stage_timing import StageTiming

from simai_ai.reid.embedder import COPEConfig, COPEEmbedder, Embedder
from simai_ai.reid.face import (
    FaceConfig,
    FaceEmbedder,
    create_face_embedder,
    estimate_face_region_from_pose,
)
from simai_ai.reid.visibility import PartVisibility, PartWeights, estimate_part_visibility

log = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 8
DEFAULT_UPDATE_INTERVAL_MS = 750
DEFAULT_MIN_DET_SCORE = 0.40
DEFAULT_MIN_BBOX_WIDTH_PX = 28
DEFAULT_MIN_BBOX_HEIGHT_PX = 56
DEFAULT_MIN_VISIBILITY_RATIO = 0.45
DEFAULT_MAX_QUEUE_SIZE = 32
DEFAULT_FEATURE_TTL_MS = 30_000


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    if arr.size == 0 or not np.all(np.isfinite(arr)):
        raise ValueError("특징 벡터는 유한한 1차원 배열이어야 합니다.")
    norm = float(np.linalg.norm(arr))
    if norm <= 1e-12:
        raise ValueError("영벡터는 정규화할 수 없습니다.")
    return arr / norm


def estimate_orientation_bin_2d(
    keypoints_2d: Sequence[Sequence[float] | None] | np.ndarray | None,
    bbox_xyxy: Sequence[float] | None,
    *,
    num_bins: int = 8,
    image_width_px: float = 2304.0,
) -> int | None:
    """2D 박스·관절로부터 카메라 내 방향/시야 슬롯 bin(`0 .. num_bins - 1`)을 추정한다.

    1단계 평가(`reid_eval`)와 동일한 수평 시야 분할(`cx // (width / num_bins)`)을 기본 슬롯 기준으로 사용하여
    카메라 내 위치·시선 각도에 따른 다중 슬롯 다양성을 보장한다.
    """
    if num_bins < 1:
        return None
    if bbox_xyxy is not None and len(bbox_xyxy) >= 4:
        x1, _, x2, _ = (float(bbox_xyxy[0]), float(bbox_xyxy[1]), float(bbox_xyxy[2]), float(bbox_xyxy[3]))
        if np.isfinite(x1) and np.isfinite(x2) and x2 > x1:
            cx = 0.5 * (x1 + x2)
            step = max(1.0, image_width_px / float(num_bins))
            return int(max(0.0, cx) // step) % num_bins
    if keypoints_2d is not None and len(keypoints_2d) >= 7:
        l_sh, r_sh = keypoints_2d[5], keypoints_2d[6]
        if l_sh is not None and r_sh is not None and len(l_sh) >= 2 and len(r_sh) >= 2:
            cx = 0.5 * (float(l_sh[0]) + float(r_sh[0]))
            if np.isfinite(cx):
                step = max(1.0, image_width_px / float(num_bins))
                return int(max(0.0, cx) // step) % num_bins
    return 0


@dataclass(frozen=True)
class LocalTrackFeature:
    """`(camera_id, local_track_id)` 의 단일 시점 COPE 외형 특징 및 가시성 메타데이터."""

    camera_id: str
    local_track_id: str
    frame_sequence: int
    capture_time_ms: int
    embedding: np.ndarray
    visibility: PartVisibility = field(default_factory=PartVisibility.all_visible)
    quality_score: float = 1.0
    orientation_bin: int | None = None
    face_embedding: np.ndarray | None = None
    bbox_xyxy: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if not self.camera_id or not self.local_track_id:
            raise ValueError("camera_id 와 local_track_id 는 비어 있을 수 없습니다.")
        object.__setattr__(self, "embedding", _l2_normalize(self.embedding))
        if self.face_embedding is not None:
            object.__setattr__(self, "face_embedding", _l2_normalize(self.face_embedding))
        q = float(np.clip(self.quality_score, 0.0, 1.0)) if np.isfinite(self.quality_score) else 0.0
        object.__setattr__(self, "quality_score", q)


@dataclass(frozen=True)
class ReidCollectorConfig:
    """비동기 Re-ID 특징 수집기 설정."""

    enabled: bool = True
    cope_root: str | Path | None = None
    weights_path: str | Path | None = None
    clip_model_path: str | Path | None = None
    device: str = "cuda"
    fp16: bool = True
    batch_size: int = DEFAULT_BATCH_SIZE
    update_interval_ms: int = DEFAULT_UPDATE_INTERVAL_MS
    stable_update_interval_ms: int = 3000
    max_crops_per_second: int = 8
    min_det_score: float = DEFAULT_MIN_DET_SCORE
    min_bbox_width_px: int = DEFAULT_MIN_BBOX_WIDTH_PX
    min_bbox_height_px: int = DEFAULT_MIN_BBOX_HEIGHT_PX
    min_visibility_ratio: float = DEFAULT_MIN_VISIBILITY_RATIO
    max_queue_size: int = DEFAULT_MAX_QUEUE_SIZE
    feature_ttl_ms: int = DEFAULT_FEATURE_TTL_MS
    use_face_aux: bool = False
    part_weights: PartWeights = field(default_factory=PartWeights)

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise ValueError("batch_size 는 1 이상이어야 합니다.")
        if self.stable_update_interval_ms < self.update_interval_ms:
            raise ValueError("stable_update_interval_ms 는 update_interval_ms 이상이어야 합니다.")
        if self.max_crops_per_second < 1:
            raise ValueError("max_crops_per_second 는 1 이상이어야 합니다.")
        if self.update_interval_ms < 0:
            raise ValueError("update_interval_ms 는 0 이상이어야 합니다.")
        if not 0.0 <= self.min_det_score <= 1.0:
            raise ValueError("min_det_score 는 0.0~1.0 범위여야 합니다.")
        if self.min_bbox_width_px < 1 or self.min_bbox_height_px < 1:
            raise ValueError("최소 bbox 크기는 1 픽셀 이상이어야 합니다.")
        if not 0.0 <= self.min_visibility_ratio <= 1.0:
            raise ValueError("min_visibility_ratio 는 0.0~1.0 범위여야 합니다.")
        if self.max_queue_size < 1:
            raise ValueError("max_queue_size 는 1 이상이어야 합니다.")
        if self.feature_ttl_ms < 100:
            raise ValueError("feature_ttl_ms 는 100 ms 이상이어야 합니다.")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> ReidCollectorConfig:
        src = os.environ if env is None else env
        enabled_raw = src.get("SIMAI_REID_ENABLED", "1").strip().lower()
        enabled = enabled_raw not in {"0", "false", "no", "off"}
        kwargs: dict[str, Any] = {
            "enabled": enabled,
            "cope_root": src.get("SIMAI_COPE_ROOT") or None,
            "weights_path": src.get("SIMAI_COPE_WEIGHTS") or None,
            "clip_model_path": src.get("SIMAI_CLIP_MODEL_PATH") or None,
            "device": src.get("SIMAI_REID_DEVICE", "cuda"),
        }
        if "SIMAI_REID_BATCH_SIZE" in src:
            kwargs["batch_size"] = int(src["SIMAI_REID_BATCH_SIZE"])
        if "SIMAI_REID_UPDATE_INTERVAL_MS" in src:
            kwargs["update_interval_ms"] = int(src["SIMAI_REID_UPDATE_INTERVAL_MS"])
        for key in ("stable_update_interval_ms", "max_crops_per_second"):
            env_key = "SIMAI_REID_" + key.upper()
            if env_key in src:
                kwargs[key] = int(src[env_key])
        kwargs.update(overrides)
        return cls(**kwargs)


class LocalTrackFeatureCache:
    """스레드 안전한 `(camera_id, local_track_id)` 특징 캐시.

    실시간 모드에서는 최신 특징을 O(1) 로 조회하고, 오프라인 재생 모드에서는 시각 `now_ms` 이하의
    가장 최근 특징을 이진 탐색으로 결정론적으로 돌려준다.
    """

    def __init__(self, feature_ttl_ms: int = DEFAULT_FEATURE_TTL_MS) -> None:
        self.feature_ttl_ms = int(feature_ttl_ms)
        self._lock = threading.Lock()
        self._latest: dict[tuple[str, str], LocalTrackFeature] = {}
        self._timeline: dict[tuple[str, str], list[tuple[int, LocalTrackFeature]]] = {}
        self._frame_lookup: dict[tuple[str, int], list[LocalTrackFeature]] = {}

    def __len__(self) -> int:
        with self._lock:
            return len(self._latest)

    def clear(self) -> None:
        with self._lock:
            self._latest.clear()
            self._timeline.clear()
            self._frame_lookup.clear()

    def put(self, feature: LocalTrackFeature, *, keep_timeline: bool = False) -> None:
        key = (feature.camera_id, str(feature.local_track_id))
        with self._lock:
            prev = self._latest.get(key)
            if prev is None or feature.capture_time_ms >= prev.capture_time_ms:
                self._latest[key] = feature
            if keep_timeline:
                seq = self._timeline.setdefault(key, [])
                if not seq or feature.capture_time_ms >= seq[-1][0]:
                    seq.append((feature.capture_time_ms, feature))
                else:
                    idx = bisect.bisect_right([item[0] for item in seq], feature.capture_time_ms)
                    seq.insert(idx, (feature.capture_time_ms, feature))
                f_key = (feature.camera_id, int(feature.frame_sequence))
                self._frame_lookup.setdefault(f_key, []).append(feature)

    def lookup(
        self,
        camera_id: str,
        local_track_id: str,
        now_ms: int | None = None,
        *,
        max_age_ms: int | None = None,
    ) -> LocalTrackFeature | None:
        ttl = self.feature_ttl_ms if max_age_ms is None else int(max_age_ms)
        key = (camera_id, str(local_track_id))
        with self._lock:
            if now_ms is not None and key in self._timeline:
                seq = self._timeline[key]
                times = [item[0] for item in seq]
                idx = bisect.bisect_right(times, now_ms) - 1
                if idx < 0:
                    return None
                feat = seq[idx][1]
                if now_ms - feat.capture_time_ms > ttl:
                    return None
                return feat
            feat = self._latest.get(key)
            if feat is None:
                return None
            if now_ms is not None:
                age = now_ms - feat.capture_time_ms
                if age < 0 or age > ttl:
                    return None
            return feat

    def lookup_by_frame_bbox(
        self,
        camera_id: str,
        frame_sequence: int,
        bbox_xyxy: Sequence[float],
        *,
        min_iou: float = 0.5,
    ) -> LocalTrackFeature | None:
        """`(camera_id, frame_sequence)` 와 bbox IoU 로 특징을 찾는다 (로컬 트랙 설정이 바뀐 재생 실험용)."""
        with self._lock:
            candidates = self._frame_lookup.get((camera_id, int(frame_sequence)))
            if not candidates:
                return None
            best: LocalTrackFeature | None = None
            best_iou = min_iou
            for cand in candidates:
                iou = _bbox_iou(bbox_xyxy, cand.bbox_xyxy)
                if iou > best_iou:
                    best_iou = iou
                    best = cand
            return best

    def prune(self, now_ms: int) -> int:
        removed = 0
        with self._lock:
            expired = [
                k for k, feat in self._latest.items() if now_ms - feat.capture_time_ms > self.feature_ttl_ms
            ]
            for k in expired:
                del self._latest[k]
                self._timeline.pop(k, None)
                removed += 1
        return removed

    def save_npz(self, path: str | Path) -> Path:
        """캐시된 모든 특징을 압축 `.npz` 로 저장한다 (원본 이미지 없음, 임베딩·메타데이터만)."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            all_features: list[LocalTrackFeature] = []
            if self._timeline:
                for seq in self._timeline.values():
                    all_features.extend(feat for _, feat in seq)
            else:
                all_features.extend(self._latest.values())

        all_features.sort(key=lambda f: (f.capture_time_ms, f.camera_id, f.frame_sequence, f.local_track_id))
        n = len(all_features)
        dim = int(all_features[0].embedding.shape[0]) if n > 0 else 1280

        camera_ids = np.array([f.camera_id for f in all_features], dtype="<U16")
        local_track_ids = np.array([f.local_track_id for f in all_features], dtype="<U64")
        frame_sequences = np.array([f.frame_sequence for f in all_features], dtype=np.int64)
        capture_times = np.array([f.capture_time_ms for f in all_features], dtype=np.int64)
        embeddings = (
            np.stack([f.embedding for f in all_features], axis=0).astype(np.float16)
            if n > 0
            else np.zeros((0, dim), dtype=np.float16)
        )
        vis_scores = (
            np.array(
                [
                    [
                        1.0 if f.visibility.head else 0.0,
                        1.0 if f.visibility.torso else 0.0,
                        1.0 if f.visibility.legs else 0.0,
                        float(f.visibility.head_score),
                        float(f.visibility.torso_score),
                        float(f.visibility.legs_score),
                    ]
                    for f in all_features
                ],
                dtype=np.float32,
            )
            if n > 0
            else np.zeros((0, 6), dtype=np.float32)
        )
        quality_scores = np.array([f.quality_score for f in all_features], dtype=np.float32)
        orient_bins = np.array(
            [-1 if f.orientation_bin is None else int(f.orientation_bin) for f in all_features],
            dtype=np.int32,
        )
        bboxes = (
            np.array([f.bbox_xyxy for f in all_features], dtype=np.float32)
            if n > 0
            else np.zeros((0, 4), dtype=np.float32)
        )

        np.savez_compressed(
            target,
            camera_ids=camera_ids,
            local_track_ids=local_track_ids,
            frame_sequences=frame_sequences,
            capture_times=capture_times,
            embeddings=embeddings,
            vis_scores=vis_scores,
            quality_scores=quality_scores,
            orient_bins=orient_bins,
            bboxes=bboxes,
        )
        return target

    @classmethod
    def load_npz(
        cls,
        path: str | Path,
        *,
        feature_ttl_ms: int = DEFAULT_FEATURE_TTL_MS,
    ) -> LocalTrackFeatureCache:
        """저장된 `.npz` 특징 캐시를 불러와 시간순 타임라인 캐시를 구성한다."""
        cache = cls(feature_ttl_ms=feature_ttl_ms)
        with np.load(Path(path), allow_pickle=False) as data:
            camera_ids = data["camera_ids"]
            local_track_ids = data["local_track_ids"]
            frame_sequences = data["frame_sequences"]
            capture_times = data["capture_times"]
            embeddings = data["embeddings"].astype(np.float32)
            vis_scores = data["vis_scores"].astype(np.float32)
            quality_scores = data["quality_scores"].astype(np.float32)
            orient_bins = data["orient_bins"].astype(np.int32)
            bboxes = data["bboxes"].astype(np.float32)

        for i in range(len(camera_ids)):
            v = vis_scores[i]
            vis = PartVisibility(
                head=bool(v[0] >= 0.5),
                torso=bool(v[1] >= 0.5),
                legs=bool(v[2] >= 0.5),
                head_score=float(v[3]),
                torso_score=float(v[4]),
                legs_score=float(v[5]),
            )
            obin = None if int(orient_bins[i]) < 0 else int(orient_bins[i])
            box = bboxes[i]
            feat = LocalTrackFeature(
                camera_id=str(camera_ids[i]),
                local_track_id=str(local_track_ids[i]),
                frame_sequence=int(frame_sequences[i]),
                capture_time_ms=int(capture_times[i]),
                embedding=embeddings[i],
                visibility=vis,
                quality_score=float(quality_scores[i]),
                orientation_bin=obin,
                bbox_xyxy=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
            )
            cache.put(feat, keep_timeline=True)
        return cache


@dataclass(frozen=True)
class _CropTask:
    camera_id: str
    local_track_id: str
    frame_sequence: int
    capture_time_ms: int
    people: int
    image_bgr: np.ndarray
    crop_bounds: tuple[int, int, int, int]
    keypoints: Any
    visibility: PartVisibility
    det_score: float
    orientation_bin: int | None
    bbox_xyxy: tuple[float, float, float, float]


def _bbox_iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    x1 = max(float(box_a[0]), float(box_b[0]))
    y1 = max(float(box_a[1]), float(box_b[1]))
    x2 = min(float(box_a[2]), float(box_b[2]))
    y2 = min(float(box_a[3]), float(box_b[3]))
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0.0:
        return 0.0
    area_a = max(0.0, float(box_a[2]) - float(box_a[0])) * max(0.0, float(box_a[3]) - float(box_a[1]))
    area_b = max(0.0, float(box_b[2]) - float(box_b[0])) * max(0.0, float(box_b[3]) - float(box_b[1]))
    denom = area_a + area_b - inter
    return inter / denom if denom > 0.0 else 0.0


def sampling_snapshot(frame: Any) -> tuple[int, int, frozenset[tuple[str, str]]]:
    """World 정본 스냅샷에서 수집 주기 정책 입력을 만든다: (사람 수, 시각, 느린 주기로 둘 로컬 키). 읽기만 한다.

    새 키·가림·근접(1 m)·경쟁·오래된 상태는 빠른 주기 유지(stable 에 넣지 않는다)."""
    tracks = [tr for tr in frame.tracks if str(tr.lifecycle) != "LOST"]
    stable: set[tuple[str, str]] = set()
    for tr in tracks:
        if (str(tr.lifecycle) != "CONFIRMED" or tr.identity_flags
                or str(tr.identity_confidence) not in {"HIGH", "MEDIUM"}
                or tr.t_last_valid != frame.t):
            continue
        if any(other.world_id != tr.world_id and
               np.linalg.norm(np.subtract(other.position_xy, tr.position_xy)) <= 1.0 for other in tracks):
            continue
        stable.update(tuple(k) for k in tr.local_keys)
    return len(tracks), int(frame.t), frozenset(stable)


class AsyncReidCollector:
    """비동기 로컬 트랙 crop 수집 및 COPE 배치 추론기."""

    def __init__(
        self,
        config: ReidCollectorConfig | None = None,
        *,
        embedder: Embedder | None = None,
        face_embedder: FaceEmbedder | None = None,
        cache: LocalTrackFeatureCache | None = None,
        start_worker: bool = True,
        monotonic: Any = time.monotonic,
    ) -> None:
        self.config = config or ReidCollectorConfig()
        self.timing = StageTiming()
        self._monotonic = monotonic
        self._rate_times: deque[float] = deque()
        self._sampling = (-1, frozenset())
        self.people = 0
        self.dropped_rate_limit = 0
        self.dropped_stable_cooldown = 0
        # 빈 캐시도 len 0 이라 거짓이다: `or` 로 고르면 넘긴 캐시를 버린다(-744)
        self.cache = cache if cache is not None else LocalTrackFeatureCache(feature_ttl_ms=self.config.feature_ttl_ms)
        self._embedder: Embedder | None = None
        self._face_embedder: FaceEmbedder | None = face_embedder
        self.state: str = "disabled"
        self.reason: str | None = None

        self._cond = threading.Condition()
        self._queue: deque[_CropTask] = deque()
        self._last_queued_ms: dict[tuple[str, str], int] = {}
        self._closed = False
        self._busy = False
        self._worker: threading.Thread | None = None

        # 계측 카운터
        self.submitted_crops = 0
        self.embedded_crops = 0
        self.dropped_low_quality = 0
        self.dropped_cooldown = 0
        self.dropped_queue_full = 0
        self.batches = 0
        self.errors = 0
        self.last_batch_ms: float | None = None

        self._init_backend(embedder)
        if self.active and start_worker:
            self._worker = threading.Thread(target=self._worker_loop, name="tier0-reid", daemon=True)
            self._worker.start()

    def _cope_config(self) -> COPEConfig:
        """수집기 설정 → COPE 설정. 이름이 다른 필드(clip_model_path → clip_path)를 맞추고, 수집기가 넘기는 crop 은
        RGB 라 input_format="rgb" 로 둔다(기본 bgr 이면 채널이 한 번 더 뒤집힌다). fp16 은 COPE 설정에 없다(S15P31A101-472)."""
        cfg = self.config
        kwargs: dict[str, Any] = {"device": cfg.device, "batch_size": cfg.batch_size, "input_format": "rgb"}
        if cfg.cope_root is not None:
            kwargs["cope_root"] = Path(cfg.cope_root)
        if cfg.weights_path is not None:
            kwargs["weights_path"] = Path(cfg.weights_path)
        if cfg.clip_model_path is not None:
            kwargs["clip_path"] = Path(cfg.clip_model_path)
        return COPEConfig(**kwargs)

    def _init_face_backend(self) -> None:
        """얼굴 보조 점수(선택). 실패해도 전신 Re-ID 는 그대로 두고 얼굴만 뺀다(S15P31A101-471)."""
        if not self.config.use_face_aux or self._face_embedder is not None:
            return
        try:
            self._face_embedder = create_face_embedder(FaceConfig.from_env())  # SIMAI_FACE_WEIGHTS_DIR(buffalo_l), 없으면 NoOp
        except Exception as exc:  # noqa: BLE001 - 얼굴 가중치·onnxruntime 부재
            log.warning("얼굴 보조 점수 비활성화: %s", exc)
            self._face_embedder = None

    def _init_backend(self, injected_embedder: Embedder | None) -> None:
        cfg = self.config
        if not cfg.enabled:
            self.state = "disabled"
            self.reason = "config_disabled"
            return

        if injected_embedder is not None:
            self._embedder = injected_embedder
            self.state = "active"
            self.reason = None
            self._init_face_backend()
            return

        if cfg.device.startswith("cuda"):
            try:
                import torch

                if not torch.cuda.is_available():
                    self.state = "disabled"
                    self.reason = "cuda_unavailable"
                    log.info("Re-ID 비활성화: CUDA 장치를 사용할 수 없다")
                    return
            except ImportError:
                self.state = "disabled"
                self.reason = "torch_unavailable"
                return

        try:
            cope = COPEEmbedder(self._cope_config())
            if hasattr(cope, "load"):
                cope.load()
            self._embedder = cope
            self._init_face_backend()
            self.state = "active"
            self.reason = None
        except Exception as exc:  # noqa: BLE001 - 가중치·저장소 부재 시 조용히 비활성화
            self._embedder = None
            self.state = "disabled"
            self.reason = f"{type(exc).__name__}: {exc}"
            log.info("Re-ID 비활성화(기존 기하 추적으로 동작): %s", self.reason)

    @property
    def active(self) -> bool:
        return self.state == "active" and self._embedder is not None

    def lookup(
        self,
        camera_id: str,
        local_track_id: str,
        now_ms: int | None = None,
    ) -> LocalTrackFeature | None:
        if not self.config.enabled:
            return None
        with self.timing.measure("reid_lookup", self.people):
            return self.cache.lookup(camera_id, local_track_id, now_ms)

    def update_sampling(self, frame: Any) -> None:
        """World 정본이 만든 불변 스냅샷만 읽는다. 새 키·가림·근접·경쟁·오래된 상태는 빠른 주기 유지.

        DORMANT 재입장으로 새 로컬 키가 생기면 stable 집합에 없으므로 첫 임베딩을 생략하지 않는다.
        갤러리/위치/ID를 변경하지 않는다. 다음 제출부터 적용한다.
        """
        people, t, stable = sampling_snapshot(frame)
        self.set_sampling(t, stable, people)

    def set_sampling(self, t: int, stable: frozenset[tuple[str, str]], people: int) -> None:
        """update_sampling 의 결과를 그대로 넣는다(프로세스 분리 -744: 판정 프로세스가 계산해 검출 프로세스로 보낸다)."""
        self.people = int(people)
        self._sampling = (int(t), frozenset(stable))

    def submit_observation(self, frame: Any, obs: Any) -> int:
        """디코딩된 `Frame` 과 `Observation2DFrame` 에서 유효한 crop 을 비동기 큐에 넣는다 (비차단)."""
        if not self.active:
            return 0
        image = getattr(frame, "image", None)
        if image is None or not isinstance(image, np.ndarray) or image.ndim != 3:
            return 0
        h_img, w_img = image.shape[:2]
        cam_id = str(obs.camera_id)
        fseq = int(obs.frame_sequence)
        cap_ms = int(obs.estimated_capture_time_ms)
        cfg = self.config
        enqueued = 0

        snapshot_t, stable = self._sampling
        # 카메라 epoch 변경/긴 입력 정지 뒤에는 이전 정책을 재사용하지 않는다.
        stable = stable if 0 <= cap_ms - snapshot_t <= 500 else frozenset()
        tasks_to_add: list[_CropTask] = []
        with self._cond:
            if self._closed:
                return 0
            expired = [key for key, t in self._last_queued_ms.items() if cap_ms - t > cfg.feature_ttl_ms]
            for key in expired:
                self._last_queued_ms.pop(key, None)
            for td in getattr(obs, "detections", ()):
                ltid = str(td.local_track_id)
                key = (cam_id, ltid)
                last_ms = self._last_queued_ms.get(key)
                interval = cfg.update_interval_ms
                if key in stable and self.cache.lookup(cam_id, ltid, cap_ms) is not None:
                    interval = cfg.stable_update_interval_ms
                if last_ms is not None and 0 <= cap_ms - last_ms < interval:
                    if interval > cfg.update_interval_ms:
                        self.dropped_stable_cooldown += 1
                    self.dropped_cooldown += 1
                    continue
                det = td.detection
                score = float(getattr(det, "score", 0.0))
                if score < cfg.min_det_score:
                    self.dropped_low_quality += 1
                    continue
                bbox = getattr(det, "bbox", None)
                if bbox is None or len(bbox) < 4:
                    self.dropped_low_quality += 1
                    continue
                x1 = max(0, min(w_img - 1, int(round(float(bbox[0])))))
                y1 = max(0, min(h_img - 1, int(round(float(bbox[1])))))
                x2 = max(x1 + 1, min(w_img, int(round(float(bbox[2])))))
                y2 = max(y1 + 1, min(h_img, int(round(float(bbox[3])))))
                if (x2 - x1) < cfg.min_bbox_width_px or (y2 - y1) < cfg.min_bbox_height_px:
                    self.dropped_low_quality += 1
                    continue
                kps = getattr(det, "keypoints_2d", None)
                vis = estimate_part_visibility(kps, bbox_xyxy=bbox)
                if vis.visible_ratio(cfg.part_weights) < cfg.min_visibility_ratio:
                    self.dropped_low_quality += 1
                    continue

                orient_bin = estimate_orientation_bin_2d(kps, bbox, image_width_px=float(w_img))
                tasks_to_add.append(
                    _CropTask(
                        camera_id=cam_id,
                        local_track_id=ltid,
                        frame_sequence=fseq,
                        capture_time_ms=cap_ms,
                        people=len(obs.detections),
                        image_bgr=image,
                        crop_bounds=(x1, y1, x2, y2),
                        keypoints=kps,
                        visibility=vis,
                        det_score=score,
                        orientation_bin=orient_bin,
                        bbox_xyxy=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
                    )
                )
                self._last_queued_ms[key] = cap_ms

            for task in tasks_to_add:
                if len(self._queue) >= cfg.max_queue_size:
                    self._forget_task(self._queue.popleft())
                    self.dropped_queue_full += 1
                self._queue.append(task)
                self.submitted_crops += 1
                enqueued += 1
            if enqueued > 0:
                self._cond.notify()
        return enqueued

    def process_pending_once(self) -> int:
        """대기열에서 최대 `batch_size` 개를 꺼내 한 번 추론한다 (워커 스레드 및 동기 시험용)."""
        if not self.active or self._embedder is None:
            return 0
        with self._cond:
            if not self._queue:
                return 0
            batch = self._take_batch()
            self._busy = True
        try:
            return self._run_batch(batch)
        finally:
            with self._cond:
                self._busy = False
                self._cond.notify_all()

    def flush(self, timeout_s: float = 2.0) -> bool:
        """대기 중인 모든 작업이 처리될 때까지 기다린다 (단위 시험용)."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            while (self._queue or self._busy) and not self._closed:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(timeout=min(0.05, remaining))
            return not self._queue and not self._busy

    def _forget_task(self, task: _CropTask) -> None:
        key = (task.camera_id, task.local_track_id)
        if self._last_queued_ms.get(key) == task.capture_time_ms:
            self._last_queued_ms.pop(key, None)

    def _take_batch(self) -> list[_CropTask]:
        """cond 안: 최근 1초 임베딩 시작 수 제한. 넘친 오래된 대기 작업부터 버린다."""
        now = self._monotonic()
        while self._rate_times and now - self._rate_times[0] >= 1.0:
            self._rate_times.popleft()
        available = max(0, self.config.max_crops_per_second - len(self._rate_times))
        while len(self._queue) > available:
            self._forget_task(self._queue.popleft())
            self.dropped_rate_limit += 1
        batch = [self._queue.popleft() for _ in range(min(len(self._queue), self.config.batch_size))]
        self._rate_times.extend([now] * len(batch))
        return batch

    def _prepare_crops(self, batch: list[_CropTask]):
        """프레임 참조가 수명을 보장한다. 잘라내기/색변환/얼굴 복사는 모델 작업 스레드에서만 실행."""
        import cv2

        crops, faces = [], []
        for task in batch:
            x1, y1, x2, y2 = task.crop_bounds
            crops.append(cv2.cvtColor(task.image_bgr[y1:y2, x1:x2], cv2.COLOR_BGR2RGB))
            face = None
            if self._face_embedder is not None and task.keypoints is not None:
                h, w = task.image_bgr.shape[:2]
                candidate = estimate_face_region_from_pose(task.keypoints, person_bbox_xyxy=task.bbox_xyxy,
                                                          image_shape_hw=(h, w), config=self._face_embedder.config)
                if candidate is not None and candidate.passes_size_gate:
                    fx1, fy1, fx2, fy2 = (int(round(v)) for v in candidate.bbox_xyxy)
                    fx1, fy1 = max(0, fx1), max(0, fy1)
                    fx2, fy2 = min(w, max(fx1 + 1, fx2)), min(h, max(fy1 + 1, fy2))
                    if fx2 - fx1 >= 16 and fy2 - fy1 >= 16:
                        face = task.image_bgr[fy1:fy2, fx1:fx2].copy()
            faces.append(face)
        return crops, faces

    def _run_batch(self, batch: list[_CropTask]) -> int:
        assert self._embedder is not None
        t0 = time.perf_counter()
        if not batch:
            return 0
        people = max(task.people for task in batch)
        with self.timing.measure("reid_crop", people):
            crops, faces = self._prepare_crops(batch)
        try:
            embed_with_scores = getattr(self._embedder, "embed_with_scores", None)
            if callable(embed_with_scores):
                with self.timing.measure("reid_embedding", people):
                    embeddings, p_scores = embed_with_scores(crops)
            else:
                with self.timing.measure("reid_embedding", people):
                    embeddings = self._embedder.embed(crops)
                p_scores = np.ones((len(batch),), dtype=np.float32)
        except Exception:  # noqa: BLE001 - Re-ID 추론 오류가 Tier 0 를 멈추지 않는다
            with self._cond:
                self.errors += 1
            log.warning("Re-ID 배치 추론 실패(이번 배치 %d개 건너뜀)", len(batch), exc_info=True)
            return 0

        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        for i, task in enumerate(batch):
            vis_ratio = task.visibility.visible_ratio(self.config.part_weights)
            q_score = float(np.clip(0.5 * float(p_scores[i]) + 0.3 * vis_ratio + 0.2 * task.det_score, 0.0, 1.0))
            face_emb: np.ndarray | None = None
            face_crop = faces[i]
            if face_crop is not None and self._face_embedder is not None:
                try:
                    face_emb = self._face_embedder.embed_single(face_crop)
                except AttributeError:
                    res = self._face_embedder.embed([face_crop])
                    if res is not None and len(res) > 0:
                        face_emb = res[0]
                except Exception:  # noqa: BLE001
                    face_emb = None
            feat = LocalTrackFeature(
                camera_id=task.camera_id,
                local_track_id=task.local_track_id,
                frame_sequence=task.frame_sequence,
                capture_time_ms=task.capture_time_ms,
                embedding=embeddings[i],
                visibility=task.visibility,
                quality_score=q_score,
                orientation_bin=task.orientation_bin,
                face_embedding=face_emb,
                bbox_xyxy=task.bbox_xyxy,
            )
            self.cache.put(feat)

        with self._cond:
            self.batches += 1
            self.embedded_crops += len(batch)
            self.last_batch_ms = round(elapsed_ms, 3)
        return len(batch)

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait(timeout=0.5)
                if self._closed:
                    return
                batch = self._take_batch()
                self._busy = True
            try:
                if batch:
                    self._run_batch(batch)
            except Exception:  # noqa: BLE001 - crop/특징 형식 실패도 보조 작업만 버린다
                with self._cond:
                    self.errors += 1
                log.warning("Re-ID 작업 실패(기하 판정은 계속한다)", exc_info=True)
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()

    def close(self, timeout_s: float = 1.0) -> None:
        with self._cond:
            self._closed = True
            self._queue.clear()
            self._cond.notify_all()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=timeout_s)

    def status(self) -> dict[str, Any]:
        with self._cond:
            queued = len(self._queue)
            return {
                "enabled": self.config.enabled,
                "state": self.state,
                "reason": self.reason,
                "batch_size": self.config.batch_size,
                "update_interval_ms": self.config.update_interval_ms,
                "stable_update_interval_ms": self.config.stable_update_interval_ms,
                "max_crops_per_second": self.config.max_crops_per_second,
                "dropped_rate_limit": self.dropped_rate_limit,
                "dropped_stable_cooldown": self.dropped_stable_cooldown,
                "timing": self.timing.summary(),
                "submitted_crops": self.submitted_crops,
                "embedded_crops": self.embedded_crops,
                "dropped_low_quality": self.dropped_low_quality,
                "dropped_cooldown": self.dropped_cooldown,
                "dropped_queue_full": self.dropped_queue_full,
                "queued_crops": queued,
                "cached_tracks": len(self.cache),
                "batches": self.batches,
                "errors": self.errors,
                "last_batch_ms": self.last_batch_ms,
            }


def __getattr__(name: str) -> Any:
    if name == "cv2":
        import cv2

        globals()["cv2"] = cv2
        return cv2
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
