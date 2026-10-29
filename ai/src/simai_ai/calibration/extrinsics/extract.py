"""녹화 세션 → 카메라별 YOLO pose keypoint 시퀀스 파일 (명세 4.3 3단계 입력, 11.5 영상 저장 금지).

- 입력은 gateway/recorder 녹화 세션(manifest 를 계약으로 검증, `io.load_recording_manifest`)이다. 카메라·세그먼트마다
  `frames.jsonl` 헤더(촬영 시각·frame_sequence·키프레임·바이트 위치)로 `.h264` 접근 단위를 꺼내 `io.PyAvDecoder` 에
  열쇠와 함께 넣는다. 그림은 그 그림을 만든 헤더의 촬영 시각을 받는다(열쇠 묶기, G3). 파일 메타데이터 fps 로 시각을
  다시 만들지 않는다(카메라 실제 프레임률이 공칭 25 fps 와 달라 몇 분이면 1초 넘게 밀린다).
- 시간 창(`start_ms`·`end_ms`) 앞의 마지막 키프레임부터 디코딩하고, 창 안의 프레임만 `every_n` 장마다 검출기에 넣는다.
  디코딩 실패(DecodeError)는 세고 다음 키프레임까지 그 카메라를 건너뛴다(메운 그림을 관측으로 쓰지 않는다).
- 검출기 결과(박스·신뢰도·keypoint)는 그대로 옮긴다. 영상·이미지·crop 은 디스크에 쓰지 않는다(얼굴 픽셀 포함).
- 출처: 직접 녹화는 frames FILE, dataset_id 는 `recording/<session_id>`(내부 테스트 녹화는 `recording-dev_test/…`,
  `VideoFileFrameSource.from_manifest` 와 같은 규칙). is_test_input 세션은 머리에 그대로 적는다(제품 성능 근거 아님).

검출기는 `PoseDetector`(이미지 목록 → 이미지별 검출 목록)다. 운영 기본은 YOLO26x-pose(`UltralyticsPose`), 노트북 확인은
작은 모델을 써도 된다. 어떤 모델로 뽑았는지는 머리 detector 에 남는다.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from simai_ai.calibration.extrinsics.keypoints import (
    KEYPOINT_FILE_FORMAT,
    KEYPOINT_FILE_VERSION,
)
from simai_ai.io.decoder import DecodeError, Decoder
from simai_ai.io.messages import I0CodecInfo
from simai_ai.io.sources import load_recording_manifest
from simai_ai.providers.provenance import build_input_provenance
from simai_ai.tracking.time_model import require_ms

DEFAULT_IMGSZ = 1280  # services/tier0/yolo_detector.DEFAULT_IMGSZ 와 같다


class PoseDetector(Protocol):
    def describe(self) -> dict[str, Any]: ...

    def detect(self, images: Sequence[Any]) -> list[list[dict[str, Any]]]:
        """이미지마다 [{bbox: [x1,y1,x2,y2], score, keypoints_2d: [[u, v, conf] | None, …]}, …]."""
        ...


class UltralyticsPose:
    """ultralytics YOLO pose(.pt 또는 TensorRT .engine). extras detection 이 필요하다(쓸 때만 import)."""

    def __init__(self, model_path: str, *, imgsz: int = DEFAULT_IMGSZ, device: str | None = None) -> None:
        try:
            import ultralytics
            from ultralytics import YOLO
        except ImportError as e:
            raise RuntimeError("ultralytics 가 없다(uv run --extra detection …)") from e
        self._model = YOLO(model_path, task="pose")
        self._imgsz = imgsz
        self._device = device
        path = Path(model_path)
        self._info = {
            "name": "ultralytics-pose",
            "model": path.name,
            "weights_sha256": _sha256(path) if path.is_file() else None,
            "imgsz": imgsz,
            "device": device,
            "ultralytics_version": str(ultralytics.__version__),
        }

    def describe(self) -> dict[str, Any]:
        return dict(self._info)

    def detect(self, images: Sequence[Any]) -> list[list[dict[str, Any]]]:
        kwargs: dict[str, Any] = {"imgsz": self._imgsz, "verbose": False}
        if self._device is not None:
            kwargs["device"] = self._device
        out = []
        for result in self._model.predict(list(images), **kwargs):
            boxes, keypoints = result.boxes, result.keypoints
            dets = []
            if boxes is not None and len(boxes):
                kp_data = keypoints.data.tolist() if keypoints is not None else [None] * len(boxes)
                for box, score, kps in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), kp_data, strict=True):
                    dets.append(
                        {
                            "bbox": [round(float(v), 2) for v in box],
                            "score": round(float(score), 4),
                            "keypoints_2d": [
                                [round(float(k[0]), 2), round(float(k[1]), 2), round(float(k[2]), 4)]
                                if len(k) >= 3
                                else None
                                for k in (kps or ())
                            ],
                        }
                    )
            out.append(dets)
        return out


def extract_keypoints(
    session_dir: Path,
    detector: PoseDetector,
    *,
    created_at: int,
    decoder_factory: Callable[[], Decoder],
    cameras: Sequence[str] | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    every_n: int = 3,
    batch: int = 8,
) -> tuple[dict[str, Any], Iterator[dict[str, Any]]]:
    """(머리, 프레임 줄 반복자). 머리 cameras[] 의 집계(frames_in_window 등)는 반복자를 끝까지 돈 뒤 채워진다.

    호출자는 줄을 다 모은 뒤 머리와 함께 쓴다(`cli extract`).
    """
    require_ms("created_at", created_at)
    if every_n < 1 or batch < 1:
        raise ValueError(f"every_n·batch 는 1 이상이다: {every_n}, {batch}")
    session_dir = Path(session_dir)
    manifest = load_recording_manifest(session_dir)
    by_id = {c["camera_id"]: c for c in manifest["cameras"]}
    wanted = list(cameras) if cameras else list(by_id)
    missing = sorted(set(wanted) - set(by_id))
    if missing:
        raise ValueError(f"녹화에 없는 카메라: {missing}")
    window = manifest.get("common_capture_window_ms") or [None, None]
    lo = start_ms if start_ms is not None else window[0]
    hi = end_ms if end_ms is not None else window[1]
    purpose = (manifest.get("consent") or {}).get("purpose")
    prefix = "recording-dev_test" if purpose == "dev_test" else "recording"
    frames_prov = {"source": "FILE", "dataset_id": f"{prefix}/{manifest['session_id']}", "camera_ids": wanted}
    header: dict[str, Any] = {
        "format": KEYPOINT_FILE_FORMAT,
        "format_version": KEYPOINT_FILE_VERSION,
        "created_at": created_at,
        "session_id": manifest["session_id"],
        "is_test_input": bool(manifest.get("is_test_input")),
        "consent_purpose": purpose,
        "input_provenance": build_input_provenance(frames_prov),
        "detector": detector.describe(),
        "window_ms": [lo, hi],
        "every_n": every_n,
        "joint_layout": "COCO17",
        "cameras": [
            {
                "camera_id": cid,
                "image_size": [int(by_id[cid]["settings"]["width"]), int(by_id[cid]["settings"]["height"])],
                "timestamp_source": by_id[cid].get("timestamp_source"),
            }
            for cid in wanted
        ],
    }
    stats = {c["camera_id"]: c for c in header["cameras"]}

    def rows() -> Iterator[dict[str, Any]]:
        for cid in wanted:
            st = stats[cid]
            st.update({"frames_in_window": 0, "frames_inferred": 0, "decode_errors": 0, "segments": 0})
            for seg in by_id[cid]["segments"]:
                st["segments"] += 1
                yield from _segment_rows(session_dir, cid, seg, detector, decoder_factory(), lo, hi, every_n, batch, st)

    return header, rows()


def _segment_rows(session_dir, cid, seg, detector, decoder, lo, hi, every_n, batch, st) -> Iterator[dict[str, Any]]:
    headers = [json.loads(ln) for ln in (session_dir / seg["frames"]["path"]).read_text(encoding="utf-8").splitlines() if ln.strip()]
    codec_doc = json.loads((session_dir / seg["codec_info"]["path"]).read_text(encoding="utf-8"))
    codec = I0CodecInfo(
        camera_id=cid,
        stream_epoch=int(codec_doc["stream_epoch"]),
        codec=codec_doc["codec"],
        pts_timebase=codec_doc.get("pts_timebase"),
        sps=base64.b64decode(codec_doc.get("sps") or ""),
        pps=base64.b64decode(codec_doc.get("pps") or ""),
    )
    decoder.configure(codec)
    # 창 시작 앞의 마지막 키프레임부터 넣는다(그 앞 그림은 창 안 그림의 참조가 될 수 없다)
    first = 0
    for i, entry in enumerate(headers):
        h = entry["header"]
        if lo is not None and h["estimated_capture_time_ms"] > lo:
            break
        if h["keyframe"]:
            first = i
    by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    pending: list[tuple[Mapping[str, Any], Any]] = []
    in_window = 0
    waiting_key = True
    with (session_dir / seg["video"]["path"]).open("rb") as video:

        def take(pictures) -> Iterator[dict[str, Any]]:
            nonlocal in_window
            for pic in pictures:
                h = by_key.pop(pic.key, None)
                if h is None or not _inside(h["estimated_capture_time_ms"], lo, hi):
                    continue
                in_window += 1
                st["frames_in_window"] += 1
                if (in_window - 1) % every_n:
                    continue
                pending.append((h, pic.image))
                if len(pending) >= batch:
                    yield from flush()

        def flush() -> Iterator[dict[str, Any]]:
            if not pending:
                return
            results = detector.detect([img for _, img in pending])
            for (h, _), dets in zip(pending, results, strict=True):
                st["frames_inferred"] += 1
                yield _row(cid, h, dets)
            pending.clear()

        for entry in headers[first:]:
            h = entry["header"]
            if hi is not None and h["estimated_capture_time_ms"] > hi:
                break
            if waiting_key and not h["keyframe"]:
                continue
            waiting_key = False
            video.seek(int(entry["offset"]))
            payload = video.read(int(entry["size"]))
            key = (int(h["stream_epoch"]), int(h["frame_sequence"]))
            by_key[key] = h
            try:
                pictures = decoder.decode(payload, key, bool(h["keyframe"]))
            except DecodeError:
                st["decode_errors"] += 1
                by_key.clear()
                decoder.configure(codec)  # CameraIngest 와 같이 디코더를 다시 열고 다음 키프레임까지 건너뛴다
                waiting_key = True
                continue
            yield from take(pictures)
        try:
            yield from take(decoder.flush())
        except DecodeError:
            st["decode_errors"] += 1
        yield from flush()


def _inside(t: int, lo: int | None, hi: int | None) -> bool:
    return (lo is None or t >= lo) and (hi is None or t <= hi)


def _row(cid: str, h: Mapping[str, Any], dets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "camera_id": cid,
        "stream_epoch": int(h["stream_epoch"]),
        "frame_sequence": int(h["frame_sequence"]),
        "estimated_capture_time_ms": int(h["estimated_capture_time_ms"]),
        "timestamp_source": h["timestamp_source"],
        "timestamp_uncertainty_ms": h.get("timestamp_uncertainty_ms"),
        "detections": dets,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
