"""VLM 작업 프레임 관문 (S15P31A101-270): 객체 저장소에서 받은 프레임을 얼굴 처리 뒤에만 돌려주고, 하나도 안 남으면 작업 실패."""

from __future__ import annotations

import numpy as np
import pytest

from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from services.worker.vlm_frames import decode_image, gate_input_frames
from simai_ai.vlm.face_blur import Person

FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))
PERSON = Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)


def _ctx() -> JobContext:
    return JobContext(job_id="j", lease_id="l", job_type="vlm_rationale", attempt=1, input={})


class Store:
    def __init__(self, objects: dict[str, bytes], error: ObjectStoreError | None = None) -> None:
        self.objects, self.error = objects, error

    def get(self, ctx, uri):
        if self.error is not None:
            raise self.error
        return self.objects[uri]

    def put(self, ctx, name, content, content_type):  # pragma: no cover - 쓰지 않는다
        raise AssertionError("흐린 이미지는 저장소에 올리지 않는다")


class Provider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def people(self, image):
        if self.error is not None:
            raise self.error
        return [PERSON]


def _decode(data: bytes) -> np.ndarray:
    if data == b"broken":
        raise ValueError("깨진 JPEG")
    return np.full((320, 240, 3), int(data), np.uint8)


def _frames(*uris_policies):
    return [{"camera_id": "cam1", "frame_uri": u, "face_policy": p} for u, p in uris_policies]


def test_each_frame_uses_its_own_policy():
    store = Store({"s3://a": b"10", "s3://b": b"20"})
    result = gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED"), ("s3://b", "EXCLUDED")), Provider(), decode=_decode)
    assert [(f.source, f.face_policy) for f in result.frames] == [("s3://a", "BLURRED"), ("s3://b", "EXCLUDED")]
    assert (result.frames[1].pixels[100:120, 100:120] == 128).all()


def test_partial_failure_keeps_processed_frames():
    store = Store({"s3://a": b"10", "s3://b": b"broken"})
    result = gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED"), ("s3://b", "BLURRED")), Provider(), decode=_decode)
    assert [f.source for f in result.frames] == ["s3://a"]
    assert [(d.source, d.reason) for d in result.dropped] == [("s3://b", "decode")]


def test_no_frames_left_fails_job_not_retryable_for_decode():
    store = Store({"s3://a": b"broken"})
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and not e.value.retryable


def test_no_frames_left_after_detector_failure_is_retryable():
    store = Store({"s3://a": b"10"})
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(RuntimeError("CUDA OOM")), decode=_decode)
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and e.value.retryable


def test_fetch_failure_fails_job_with_store_retryability():
    store = Store({}, error=ObjectStoreError("서명 만료", retryable=True))
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "FRAME_FETCH_FAILED" and e.value.retryable


def test_decode_image_rejects_garbage():
    pytest.importorskip("cv2")
    with pytest.raises(ValueError):
        decode_image(b"not an image")
