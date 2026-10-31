"""I5 `incident_summary` 처리기: 이벤트 구간 클립 + 구조화 상태 → 구간 요약문·근거 시각 (명세 5.4 용도 2, 12.5, FR-88, S15P31A101-271).

프롬프트·프레임 표본 추출·응답 파싱(이벤트 구간 검증)은 `simai_ai.vlm.incident_summary`,
얼굴 흐림 관문은 `simai_ai.vlm.face_blur.prepare_vlm_frames`, VLM 호출은 `simai_ai.vlm.client`.
입력·결과 형식은 `contracts/messages/incident_summary.schema.json`. 설계 `docs/design/incident-summary.md`.

- 입력은 계약으로 먼저 검사하고 이벤트 구간(`window_start_ms` ~ `window_end_ms`)과 클립 촬영 시각 역전을 확인한다(형식 오류는 재시도 불가 `VLM_INPUT_INVALID`).
- 클립은 `ObjectStore.get` 으로 내려받고(실패 시 `CLIP_FETCH_FAILED`, `ObjectStoreError.retryable` 그대로) 프레임을 디코딩한다.
- 디코딩된 프레임에 클립 촬영 시각을 보간해 붙이고, 이벤트 구간 안 프레임을 장수 상한(`DEFAULT_MAX_FRAMES`)으로 균등 표본 추출한다.
- 표본 프레임은 반드시 `prepare_vlm_frames` 얼굴 흐림 관문을 거친 뒤(`VlmFrame`) VLM 에 넣는다. 남는 프레임이 없으면 `NO_FACE_SAFE_FRAMES`.
- 근거 시각(`evidence_timestamps_ms`) 중 이벤트 구간 밖 시각은 버리고, 구간 안 시각이 하나도 남지 않거나 JSON 형식이 어긋나면
  시간 예산 안에서 한 번 다시 묻고 그래도 어긋나면 재시도 불가 `VLM_ANSWER_INVALID` 로 실패 처리한다.
- 등급(`severity`)·상향 의견(`proposed_severity`)은 절대 내지 않는다.
"""

from __future__ import annotations

import io
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np

from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmError
from simai_ai.vlm.face_blur import KeypointProvider, prepare_vlm_frames
from simai_ai.vlm.incident_summary import (
    DEFAULT_MAX_FRAMES,
    PROMPT,
    RETRY_NOTE,
    parse_answer,
    resolve_event_window,
    sample_clip_frames,
)

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .vlm_rationale import _complete_vlm, client_from_env

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/incident_summary.schema.json#/$defs/input"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/incident_summary.schema.json#/$defs/result"}
JOB_BUDGET_MS = 120_000  # job_request REPORT(incident_summary) 기본 timeout_ms

log = logging.getLogger("services.worker")


def decode_clip_frames(data: bytes) -> list[np.ndarray]:
    """H.264 Annex B(`h264-annexb`) 또는 컨테이너(MP4 등) 클립 바이트 → BGR uint8 프레임 배열."""
    if not data:
        raise ValueError("클립 바이트가 비었다")
    import av

    frames: list[np.ndarray] = []
    for fmt in ("h264", None):
        try:
            with av.open(io.BytesIO(data), format=fmt) as container:
                frames = [frame.to_ndarray(format="bgr24") for frame in container.decode(video=0)]
            if frames:
                return frames
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("클립을 디코딩하지 못했거나 비디오 프레임이 없다")


class IncidentSummaryHandler:
    def __init__(
        self,
        store: ObjectStore,
        provider: KeypointProvider,
        *,
        client_factory: Callable[[], VlmClient] = client_from_env,
        monotonic: Callable[[], float] = time.monotonic,
        decode_clip: Callable[[bytes], Sequence[np.ndarray]] = decode_clip_frames,
        max_frames: int = DEFAULT_MAX_FRAMES,
    ) -> None:
        if max_frames <= 0:
            raise ValueError("max_frames 는 1 이상이어야 한다")
        self._store = store
        self._provider = provider
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._decode_clip = decode_clip
        self._max_frames = max_frames

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        custom_clock = self._monotonic if self._monotonic is not time.monotonic else None
        t0 = ctx.bind_budget(JOB_BUDGET_MS, monotonic=custom_clock)
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("VLM_INPUT_INVALID", "; ".join(errors))
        try:
            window_start_ms, window_end_ms = resolve_event_window(inp)
        except ValueError as e:
            raise JobFailed("VLM_INPUT_INVALID", str(e)) from None
        try:
            client = self._client_factory()
        except VlmError as e:
            raise JobFailed(e.code, str(e), retryable=e.retryable) from None

        event_id = inp["event_id"]
        clips_with_images: list[tuple[Mapping[str, Any], Sequence[np.ndarray]]] = []
        decode_drops: list[str] = []
        for clip in inp["clips"]:
            _check_cancelled(ctx)
            try:
                raw = self._store.get(ctx, clip["clip_uri"])
            except ObjectStoreError as e:
                if ctx.cancelled.is_set():
                    raise JobFailed("CANCELLED", "작업이 취소됐다") from None
                raise JobFailed("CLIP_FETCH_FAILED", str(e), retryable=e.retryable) from e
            try:
                images = list(self._decode_clip(raw))
            except ValueError as e:
                log.warning("이벤트 %s: 클립 %s 디코드 실패(%s)", event_id, clip["clip_id"], e)
                decode_drops.append(f"{clip['clip_id']}: decode")
                continue
            if not images:
                decode_drops.append(f"{clip['clip_id']}: empty")
                continue
            clips_with_images.append((clip, images))

        sampled = sample_clip_frames(
            clips_with_images,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            max_frames=self._max_frames,
        )
        if not sampled:
            reasons = ", ".join(decode_drops) if decode_drops else "이벤트 구간 안 프레임 없음"
            raise JobFailed("NO_FACE_SAFE_FRAMES", f"얼굴을 처리할 클립 프레임이 없다({reasons})", retryable=False)

        _check_cancelled(ctx)
        gate = prepare_vlm_frames(
            [(sf.source_id, sf.image) for sf in sampled],
            inp["face_policy"],
            self._provider,
            cancelled=ctx.cancelled.is_set,
            remaining_s=ctx.remaining_s,
        )
        _check_cancelled(ctx)
        if gate.dropped:
            log.warning("이벤트 %s: 얼굴 처리 못 한 클립 프레임 %d개를 뺐다", event_id, len(gate.dropped))
        if not gate.frames:
            retryable = any(d.reason in ("detect", "budget") for d in gate.dropped)
            reasons = ", ".join(f"{d.source}: {d.reason}" for d in gate.dropped)
            raise JobFailed("NO_FACE_SAFE_FRAMES", f"얼굴을 처리한 프레임이 없다({reasons})", retryable=retryable)

        meta = {
            sf.source_id: {"camera_id": sf.camera_id, "capture_time_ms": sf.capture_time_ms}
            for sf in sampled
        }
        parts = PROMPT.build(
            inp,
            [meta[f.source] for f in gate.frames],
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
        )
        ctx.report_progress(0.4)

        for retry in (False, True):
            _check_cancelled(ctx)
            rem_s = ctx.remaining_s()
            try:
                r = _complete_vlm(
                    client,
                    parts.text + RETRY_NOTE if retry else parts.text,
                    gate.frames,
                    system=parts.system,
                    response_format=parts.response_format,
                    timeout_s=rem_s,
                )
            except VlmError as e:
                raise JobFailed(e.code, str(e), retryable=e.retryable) from None
            try:
                if r.finish_reason == "length":
                    raise ValueError("max_tokens 에서 잘렸다")
                answer = parse_answer(
                    r.content,
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                )
                break
            except ValueError as e:
                rem_after_s = ctx.remaining_s()
                rem_after_ms = (
                    rem_after_s * 1000
                    if rem_after_s is not None
                    else (JOB_BUDGET_MS - (ctx.monotonic() - t0) * 1000)
                )
                log.warning("이벤트 %s: VLM 요약 응답 형식 오류(%s, finish_reason=%s)", event_id, e, r.finish_reason)
                if retry or rem_after_ms < r.latency_ms:
                    raise JobFailed("VLM_ANSWER_INVALID", f"VLM 응답이 형식에 맞지 않는다: {e}") from None

        _check_cancelled(ctx)
        if answer.dropped_timestamps_ms:
            log.warning("이벤트 %s: 이벤트 구간 밖 근거 시각 %d개를 버렸다", event_id, len(answer.dropped_timestamps_ms))

        result = {
            "event_id": event_id,
            "event_sequence": inp["event_sequence"],
            "summary": answer.summary,
            "evidence_timestamps_ms": list(answer.evidence_timestamps_ms),
            "window_start_ms": window_start_ms,
            "window_end_ms": window_end_ms,
            "frames_used": len(gate.frames),
            "model_version": r.model,
            "latency_ms": round((ctx.monotonic() - t0) * 1000),
        }
        if errors := schema_errors(result, RESULT_SCHEMA):
            raise JobFailed("VLM_RESULT_INVALID", "; ".join(errors))
        ctx.report_progress(1.0)
        return result


def _check_cancelled(ctx: JobContext) -> None:
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")
