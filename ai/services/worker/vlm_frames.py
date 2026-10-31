"""VLM 작업이 프레임을 VLM 에 넘기기 직전의 관문 (명세 5.4 "VLM 입력 프레임", S15P31A101-270, S15P31A101-384).

vlm_rationale 처리기(S15P31A101-25)는 input.frames[] 를 이 함수로 받고, 돌려받은 `VlmFrame` 만 VLM 에 넣는다.
- 프레임 다운로드는 제한된 병렬 스레드(기본 최대 4개)로 동시에 수행하되, 결과 순서는 입력 프레임 순서를 유지한다.
- 서명·GET·얼굴 처리는 모두 작업의 남은 시간 예산(ctx.remaining_s)과 취소(ctx.cancelled) 안에서만 수행한다.
- 흐리지 못한 프레임은 빼고, 남는 프레임이 없으면 `JobFailed` 로 끝낸다(흐리지 않은 프레임으로 대신하지 않는다).
incident_summary(S15P31A101-271)는 클립에서 뽑은 프레임 배열을 `simai_ai.vlm.face_blur.prepare_vlm_frames` 에 바로 넣는다.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from simai_ai.vlm.face_blur import DroppedFrame, GateResult, KeypointProvider, VlmFrame, prepare_vlm_frames

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError

DEFAULT_FETCH_WORKERS = 4


def decode_image(data: bytes) -> np.ndarray:
    """JPEG·PNG 바이트 → BGR uint8. 워커 이미지에는 OpenCV(headless)가 있다."""
    import cv2

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("이미지를 디코드하지 못했다")
    return image


def _fetch_frames_parallel(
    ctx: JobContext,
    store: ObjectStore,
    frames: Sequence[Mapping[str, Any]],
    *,
    max_workers: int,
) -> list[bytes]:
    if not frames:
        return []
    n = len(frames)
    workers = min(max_workers, n)
    if workers <= 1:
        out: list[bytes] = []
        for f in frames:
            if ctx.cancelled.is_set():
                raise JobFailed("CANCELLED", "작업이 취소됐다")
            rem = ctx.remaining_s()
            if rem is not None and rem <= 0:
                raise JobFailed("FRAME_FETCH_FAILED", "남은 시간 예산이 없어 프레임을 받지 않는다", retryable=True)
            try:
                out.append(store.get(ctx, f["frame_uri"]))
            except ObjectStoreError as e:
                if ctx.cancelled.is_set():
                    raise JobFailed("CANCELLED", "작업이 취소됐다") from None
                raise JobFailed("FRAME_FETCH_FAILED", str(e), retryable=e.retryable) from e
        return out

    fetched: list[bytes | None] = [None] * n
    errors: list[BaseException | None] = [None] * n
    abort = threading.Event()

    def fetch_one(idx: int) -> None:
        if abort.is_set() or ctx.cancelled.is_set():
            return
        rem = ctx.remaining_s()
        if rem is not None and rem <= 0:
            abort.set()
            errors[idx] = ObjectStoreError("남은 시간 예산이 없어 프레임을 받지 않는다", retryable=True)
            return
        try:
            fetched[idx] = store.get(ctx, frames[idx]["frame_uri"])
        except BaseException as e:  # noqa: BLE001
            abort.set()
            errors[idx] = e

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="vlm-fetch") as pool:
        futures = [pool.submit(fetch_one, i) for i in range(n)]
        for fut in futures:
            fut.result()

    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")
    for err in errors:
        if err is None:
            continue
        if isinstance(err, ObjectStoreError):
            raise JobFailed("FRAME_FETCH_FAILED", str(err), retryable=err.retryable) from err
        raise err
    for i, data in enumerate(fetched):
        if data is None:
            raise JobFailed("FRAME_FETCH_FAILED", f"프레임 {frames[i]['frame_uri']} 을 받지 못했다", retryable=True)
    return [d for d in fetched if d is not None]


def gate_input_frames(
    ctx: JobContext,
    store: ObjectStore,
    frames: Sequence[Mapping[str, Any]],
    provider: KeypointProvider,
    *,
    decode=decode_image,
    max_workers: int = DEFAULT_FETCH_WORKERS,
) -> GateResult:
    """vlm_rationale input.frames[](frame_uri·face_policy) → 얼굴을 처리한 VLM 프레임. 하나도 안 남으면 JobFailed.

    face_policy 는 계약상 프레임마다 있으므로 프레임마다 그 값으로 처리한다.
    다운로드는 제한된 병렬(max_workers)로 수행하고, 얼굴 처리는 남은 시간 예산 안에서 순서대로 수행한다.
    """
    if max_workers <= 0:
        raise ValueError("max_workers 는 1 이상이어야 한다")
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")
    raw_frames = _fetch_frames_parallel(ctx, store, frames, max_workers=max_workers)
    kept: list[VlmFrame] = []
    dropped: list[DroppedFrame] = []
    for f, data in zip(frames, raw_frames, strict=True):
        if ctx.cancelled.is_set():
            raise JobFailed("CANCELLED", "작업이 취소됐다")
        one = prepare_vlm_frames(
            [(f["frame_uri"], data)],
            f["face_policy"],
            provider,
            decode=decode,
            cancelled=ctx.cancelled.is_set,
            remaining_s=ctx.remaining_s,
        )
        if ctx.cancelled.is_set():
            raise JobFailed("CANCELLED", "작업이 취소됐다")
        kept.extend(one.frames)
        dropped.extend(one.dropped)
    result = GateResult(tuple(kept), tuple(dropped))
    if not result.frames:
        # 검출 실패·시간 예산 소진(일시적일 수 있음, 예: GPU 메모리·네트워크 지연)이 있으면 다시 해 볼 만하고, 디코드 실패뿐이면 다시 해도 같다
        retryable = any(d.reason in ("detect", "budget") for d in result.dropped)
        reasons = ", ".join(f"{d.source}: {d.reason}" for d in result.dropped)
        raise JobFailed("NO_FACE_SAFE_FRAMES", f"얼굴을 처리한 프레임이 없다({reasons})", retryable=retryable)
    return result
