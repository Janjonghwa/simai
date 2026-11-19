"""아바타 회차별 seg 배치·프롬프트. 실패하면 한 번 알리고 프로세스 재시작까지 bbox로 돈다."""

from dataclasses import dataclass
import logging
from pathlib import Path
import time
from typing import Protocol

import numpy as np

from simai_ai.calibration.intrinsics.estimate import WeightsRef
from simai_ai.hmr.prompt import HmrImageMap, HmrPrompt, PersonObservation, SegInstance, build_prompts

log = logging.getLogger(__name__)


def _is_cuda_device(device) -> bool:
    if device is None:
        return False
    if getattr(device, "type", None) is not None:
        return str(device.type).lower() == "cuda"
    return str(device).lower().startswith("cuda")


class Segmenter(Protocol):
    def predict(self, images: list[np.ndarray]) -> list[list[SegInstance]]: ...


class YoloPersonSegmenter:
    def __init__(self, weights, *, device="cuda", half=None):
        self.weights, self.device = weights, device
        is_cuda = _is_cuda_device(device)
        # CUDA에서는 FP16(half=True)을 기본으로 써 GPU 점유를 줄인다. CPU는 항상 FP32.
        self.half = is_cuda if half is None else (bool(half) and is_cuda)
        self._model = None

    def predict(self, images):
        if self._model is None:
            # 경로·해시부터 검증해 ultralytics의 자동 다운로드를 막는다.
            path = self.weights.verify()
            from ultralytics import YOLO

            self._model = YOLO(str(path), task="segment")
        results = list(self._model.predict(
            images,
            classes=[0],
            conf=0.25,
            imgsz=1280,
            retina_masks=True,
            half=self.half,
            verbose=False,
            device=self.device,
        ))
        if len(results) != len(images):
            raise ValueError("seg 배치와 결과 카메라 수가 다르다")
        batch = []
        for image, result in zip(images, results):
            instances = []
            if result.masks is not None:
                masks = result.masks.data.cpu().numpy() > 0.5
                boxes = result.boxes.xyxy.cpu().numpy()
                scores = result.boxes.conf.cpu().numpy()
                classes = result.boxes.cls.cpu().numpy()
                if not (len(masks) == len(boxes) == len(scores) == len(classes)):
                    raise ValueError("seg 박스와 마스크 수가 다르다")
                for box, score, cls, mask in zip(boxes, scores, classes, masks):
                    if cls != 0:
                        continue
                    # retina_masks=True는 letterbox를 제거한 원래 입력 크기다.
                    # 다른 크기를 단순 resize하면 패딩까지 사람 영역으로 옮기므로 거부한다.
                    if mask.shape != image.shape[:2]:
                        raise ValueError("seg retina 마스크 해상도가 다르다")
                    instances.append(SegInstance(tuple(float(x) for x in box), float(score), mask))
            batch.append(instances)
        return batch


@dataclass
class PreparedPerson:
    image: np.ndarray
    prompt: HmrPrompt
    original_mask: np.ndarray | None


class AvatarSegmentation:
    def __init__(self, segmenter: Segmenter, *, device="cpu", synchronize=None, timer=time.perf_counter,
                 sync_timing=False):
        self.segmenter, self.device = segmenter, device
        self.synchronize = synchronize
        self.sync_timing = bool(sync_timing)
        self.timer = timer
        self.failed = False
        self._calibration_version = None
        self._maps = {}

    def prepare(self, jobs, images, rig):
        """(track, observation) 목록 → (World ID, camera ID)별 준비 입력. 상류 객체는 수정하지 않는다."""
        if self.failed or not jobs:
            return {}
        try:
            if self._calibration_version != rig.calibration_version:
                self._maps.clear()
                self._calibration_version = rig.calibration_version
            grouped = {}
            for track, obs in jobs:
                cid = obs["camera_id"]
                grouped.setdefault(cid, []).append(PersonObservation(
                    camera_id=cid, bbox=tuple(obs["bbox"]), world_id=track["world_id"],
                    identity_revision=track["identity_revision"], local_track_id=obs.get("local_track_id")))
            prepared = {}
            for cid in grouped:
                if cid not in self._maps:
                    camera = rig.cameras[cid]
                    self._maps[cid] = HmrImageMap(camera, camera.K)
                prepared[cid] = self._maps[cid].prepare_image(images[cid])
            if self.sync_timing and self.synchronize is not None:
                self.synchronize()
            start = self.timer()
            batch = self.segmenter.predict(list(prepared.values()))
            if self.sync_timing and self.synchronize is not None:
                self.synchronize()
            elapsed = (self.timer() - start) * 1000
            if len(batch) != len(grouped):
                raise ValueError("seg 배치와 결과 카메라 수가 다르다")
            out = {}
            for (cid, observations), instances in zip(grouped.items(), batch):
                imap = self._maps[cid]
                if any(np.asarray(inst.mask).shape != imap.size[::-1] for inst in instances):
                    raise ValueError("seg 마스크와 HMR 그림 해상도가 다르다")
                prompts, _ = build_prompts(observations, instances, imap)
                for prompt in prompts:
                    mask = None if prompt.mask is None else imap.mask_to_original(prompt.mask)
                    out[(prompt.observation.world_id, cid)] = PreparedPerson(prepared[cid], prompt, mask)
            log.info("아바타 seg batch cameras=%s instances=%s matched=%s elapsed_ms=%.3f device=%s half=%s",
                     len(grouped), sum(len(items) for items in batch),
                     sum(p.prompt.mask is not None for p in out.values()), elapsed, self.device,
                     getattr(self.segmenter, "half", False))
            return out
        except Exception as error:
            self.failed = True
            self.segmenter = None  # 실패한 YOLO의 GPU 가중치를 붙잡아 HMR 폴백을 막지 않는다.
            # 예외 본문에는 경로/외부 라이브러리 설정이 있을 수 있어 종류만 남긴다.
            log.warning("아바타 seg 끔: %s; bbox만 사용 (재시작 전까지)", type(error).__name__)
            return {}


def segmentation_from_env(env, *, device=None, cuda_available=None):
    enabled = env.get("SIMAI_AVATAR_SEG", "auto")
    if enabled not in {"auto", "0", "1"}:
        raise ValueError("SIMAI_AVATAR_SEG는 auto, 0, 1")
    if enabled == "0":
        return None
    half_env = env.get("SIMAI_AVATAR_SEG_HALF", "auto")
    if half_env not in {"auto", "0", "1"}:
        raise ValueError("SIMAI_AVATAR_SEG_HALF는 auto, 0, 1")
    sync_env = env.get("SIMAI_AVATAR_SEG_SYNC_TIMING", "0")
    if sync_env not in {"0", "1"}:
        raise ValueError("SIMAI_AVATAR_SEG_SYNC_TIMING는 0 또는 1")
    if cuda_available is None:
        import torch

        cuda_available = torch.cuda.is_available()
    if enabled == "auto" and not cuda_available:
        return None
    device = device if device is not None else ("cuda" if cuda_available else "cpu")
    path = env.get("SIMAI_AVATAR_SEG_WEIGHTS") or str(
        Path(env.get("SIMAI_ROOT") or "/workspace/simai") / "weights/yolo/yolo26x-seg.pt")
    digest = env.get("SIMAI_AVATAR_SEG_WEIGHTS_SHA256") or \
        "92b3de0065766a17180d6219858717dc9d03cdce8a3ca9576c97fd75aabb64f3"
    try:
        weights = WeightsRef(path, "sha256:" + digest.removeprefix("sha256:"))
    except ValueError:
        log.warning("아바타 seg 끔: 잘못된 가중치 해시 설정; bbox만 사용 (재시작 전까지)")
        return None
    half = None if half_env == "auto" else (half_env == "1")
    sync_timing = sync_env == "1"
    # 기본 경로에서는 torch.cuda.synchronize를 부르지 않아 Tier 0 CUDA 스트림과의 불필요한 전체 동기화를 막는다.
    if sync_timing and cuda_available and _is_cuda_device(device):
        def synchronize():
            import torch

            torch.cuda.synchronize(device)
    else:
        synchronize = None
    return AvatarSegmentation(
        YoloPersonSegmenter(weights, device=device, half=half),
        device=device,
        synchronize=synchronize,
        sync_timing=sync_timing,
    )
