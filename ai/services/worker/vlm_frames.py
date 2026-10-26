"""VLM 작업이 프레임을 VLM 에 넘기기 직전의 관문 (명세 5.4 "VLM 입력 프레임", S15P31A101-270).

vlm_rationale 처리기(S15P31A101-25)는 input.frames[] 를 이 함수로 받고, 돌려받은 `VlmFrame` 만 VLM 에 넣는다.
흐리지 못한 프레임은 빼고, 남는 프레임이 없으면 `JobFailed` 로 끝낸다(흐리지 않은 프레임으로 대신하지 않는다).
incident_summary(S15P31A101-271)는 클립에서 뽑은 프레임 배열을 `simai_ai.vlm.face_blur.prepare_vlm_frames` 에 바로 넣는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from simai_ai.vlm.face_blur import GateResult, KeypointProvider, prepare_vlm_frames

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError


def decode_image(data: bytes) -> np.ndarray:
    """JPEG·PNG 바이트 → BGR uint8. 워커 이미지에는 OpenCV(headless)가 있다."""
    import cv2

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("이미지를 디코드하지 못했다")
    return image


def gate_input_frames(
    ctx: JobContext,
    store: ObjectStore,
    frames: Sequence[Mapping[str, Any]],
    provider: KeypointProvider,
    *,
    decode=decode_image,
) -> GateResult:
    """vlm_rationale input.frames[](frame_uri·face_policy) → 얼굴을 처리한 VLM 프레임. 하나도 안 남으면 JobFailed.

    face_policy 는 계약상 프레임마다 있으므로 프레임마다 그 값으로 처리한다.
    """
    kept, dropped = [], []
    for f in frames:
        try:
            data = store.get(ctx, f["frame_uri"])
        except ObjectStoreError as e:
            raise JobFailed("FRAME_FETCH_FAILED", str(e), retryable=e.retryable) from e
        one = prepare_vlm_frames([(f["frame_uri"], data)], f["face_policy"], provider, decode=decode)
        kept += one.frames
        dropped += one.dropped
    result = GateResult(tuple(kept), tuple(dropped))
    if not result.frames:
        # 검출 실패(일시적일 수 있음, 예: GPU 메모리)가 있으면 다시 해 볼 만하고, 디코드 실패뿐이면 다시 해도 같다
        retryable = any(d.reason == "detect" for d in result.dropped)
        reasons = ", ".join(f"{d.source}: {d.reason}" for d in result.dropped)
        raise JobFailed("NO_FACE_SAFE_FRAMES", f"얼굴을 처리한 프레임이 없다({reasons})", retryable=retryable)
    return result
