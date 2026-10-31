"""I5 `incident_summary` 처리기: 이벤트 구간 클립 + 구조화 상태 → 구간 요약문·근거 시각 (명세 5.4 용도 2, 12.5, FR-88, S15P31A101-271, S15P31A101-387).

프롬프트·프레임 표본 계획 및 추출·응답 파싱(이벤트 구간 검증)은 `simai_ai.vlm.incident_summary`,
얼굴 흐림 관문은 `simai_ai.vlm.face_blur.prepare_vlm_frames`, VLM 호출은 `simai_ai.vlm.client`.
입력·결과 형식은 `contracts/messages/incident_summary.schema.json`. 설계 `docs/design/incident-summary.md`.

- 입력은 계약으로 먼저 검사하고 이벤트 구간(`window_start_ms` ~ `window_end_ms`)과 클립 촬영 시각 역전을 확인한다(형식 오류는 재시도 불가 `VLM_INPUT_INVALID`).
- 클립은 `ObjectStore.get` 으로 내려받고(실패 시 `CLIP_FETCH_FAILED`, `ObjectStoreError.retryable` 그대로) 프레임을 디코딩한다.
- 클립 전체를 BGR 배열로 만들어 메모리에 쥐지 않는다(`count_clip_frames` 로 프레임 수만 센 뒤 `plan_clip_frames` 로 이벤트 구간 안 표본 인덱스를 먼저 고르고,
  선택된 최대 `DEFAULT_MAX_FRAMES` 장만 BGR 로 변환·보관한다). 디코딩 도중에도 프레임마다 작업 취소(`ctx.cancelled`)와 남은 시간 예산(`ctx.remaining_s`)을 확인한다.
- 표본 프레임은 반드시 `prepare_vlm_frames` 얼굴 흐림 관문을 거친 뒤(`VlmFrame`) VLM 에 넣는다. 남는 프레임이 없으면 `NO_FACE_SAFE_FRAMES`.
- 근거 시각(`evidence_timestamps_ms`) 중 이벤트 구간 밖 시각은 버리고, 구간 안 시각이 하나도 남지 않거나 JSON 형식이 어긋나면
  시간 예산 안에서 한 번 다시 묻고 그래도 어긋나면 재시도 불가 `VLM_ANSWER_INVALID` 로 실패 처리한다.
- 등급(`severity`)·상향 의견(`proposed_severity`)은 절대 내지 않는다.
"""

from __future__ import annotations

import inspect
import io
import json
import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

import numpy as np

from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmError
from simai_ai.vlm.face_blur import KeypointProvider, prepare_vlm_frames
from simai_ai.vlm.incident_summary import (
    DEFAULT_MAX_FRAMES,
    PROMPT,
    RETRY_NOTE,
    PlannedFrame,
    SampledFrame,
    assign_clip_timestamps,
    parse_answer,
    plan_clip_frames,
    resolve_event_window,
)

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError
from .vlm_rationale import _complete_vlm, client_from_env

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/incident_summary.schema.json#/$defs/input"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/incident_summary.schema.json#/$defs/result"}
JOB_BUDGET_MS = 120_000  # job_request REPORT(incident_summary) 기본 timeout_ms

log = logging.getLogger("services.worker")


class ClipDecodeCancelled(Exception):
    """클립 디코딩 도중 작업 취소 신호가 켜졌다."""


class ClipDecodeBudgetExceeded(Exception):
    """클립 디코딩 도중 남은 작업 시간 예산이 소진됐다."""


def _guard_decode_budget(
    cancelled: Callable[[], bool] | None,
    remaining_s: Callable[[], float | None] | None,
) -> None:
    if cancelled is not None and cancelled():
        raise ClipDecodeCancelled("작업 취소로 클립 디코딩을 중단했다")
    if remaining_s is not None and (rem := remaining_s()) is not None and rem <= 0:
        raise ClipDecodeBudgetExceeded(f"남은 시간 예산 부족({rem:.3f}s)")


def count_clip_frames(
    data: bytes,
    *,
    cancelled: Callable[[], bool] | None = None,
    remaining_s: Callable[[], float | None] | None = None,
) -> int:
    """H.264 Annex B(`h264-annexb`) 또는 컨테이너(MP4 등) 클립 바이트의 디코딩 가능한 비디오 프레임 수를 센다.

    BGR 배열 변환(`to_ndarray`)이나 `VideoFrame` 목록 보관을 하지 않아 클립 길이에 비례하는 메모리를 쓰지 않는다.
    """
    if not data:
        raise ValueError("클립 바이트가 비었다")
    _guard_decode_budget(cancelled, remaining_s)
    import av

    for fmt in ("h264", None):
        try:
            count = 0
            with av.open(io.BytesIO(data), format=fmt) as container:
                it = iter(container.decode(video=0))
                while True:
                    _guard_decode_budget(cancelled, remaining_s)
                    try:
                        next(it)
                    except StopIteration:
                        break
                    _guard_decode_budget(cancelled, remaining_s)
                    count += 1
            if count > 0:
                return count
        except (ClipDecodeCancelled, ClipDecodeBudgetExceeded):
            raise
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("클립을 디코딩하지 못했거나 비디오 프레임이 없다")


def decode_clip_frames(
    data: bytes,
    *,
    frame_indices: Collection[int] | None = None,
    cancelled: Callable[[], bool] | None = None,
    remaining_s: Callable[[], float | None] | None = None,
) -> list[np.ndarray]:
    """H.264 Annex B(`h264-annexb`) 또는 컨테이너(MP4 등) 클립 바이트 → BGR uint8 프레임 배열.

    `frame_indices` 가 주어지면 해당 0-기반 프레임 인덱스만 BGR 로 변환·보관하고 마지막 필요 인덱스 뒤에서 디코딩을 멈춘다.
    디코딩 중 매 프레임마다 취소(`cancelled`)와 남은 시간 예산(`remaining_s`)을 확인한다.
    """
    if not data:
        raise ValueError("클립 바이트가 비었다")
    _guard_decode_budget(cancelled, remaining_s)
    ordered: list[int] | None = None
    wanted: set[int] = set()
    last_idx = -1
    if frame_indices is not None:
        ordered = [int(i) for i in frame_indices]
        if any(i < 0 for i in ordered):
            raise ValueError("frame_indices 는 0 이상이어야 한다")
        if not ordered:
            return []
        wanted = set(ordered)
        last_idx = max(wanted)

    import av

    for fmt in ("h264", None):
        try:
            if ordered is None:
                frames: list[np.ndarray] = []
                with av.open(io.BytesIO(data), format=fmt) as container:
                    it = iter(container.decode(video=0))
                    while True:
                        _guard_decode_budget(cancelled, remaining_s)
                        try:
                            frame = next(it)
                        except StopIteration:
                            break
                        _guard_decode_budget(cancelled, remaining_s)
                        frames.append(frame.to_ndarray(format="bgr24"))
                if frames:
                    return frames
            else:
                picked: dict[int, np.ndarray] = {}
                with av.open(io.BytesIO(data), format=fmt) as container:
                    it = iter(container.decode(video=0))
                    idx = 0
                    while True:
                        _guard_decode_budget(cancelled, remaining_s)
                        try:
                            frame = next(it)
                        except StopIteration:
                            break
                        _guard_decode_budget(cancelled, remaining_s)
                        if idx in wanted:
                            picked[idx] = frame.to_ndarray(format="bgr24")
                        if idx >= last_idx:
                            break
                        idx += 1
                if len(picked) == len(wanted):
                    return [picked[i] for i in ordered]
        except (ClipDecodeCancelled, ClipDecodeBudgetExceeded):
            raise
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("클립을 디코딩하지 못했거나 비디오 프레임이 없다")


def _accepts_kwarg(fn: Callable[..., Any], name: str) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _call_with_supported_kwargs(fn: Callable[..., Any], data: bytes, **kwargs: Any) -> Any:
    supported = {k: v for k, v in kwargs.items() if _accepts_kwarg(fn, k)}
    return fn(data, **supported)


def _has_in_window_frame(clip: Mapping[str, Any], frame_count: int, window_start_ms: int, window_end_ms: int) -> bool:
    if frame_count <= 0:
        return False
    timestamps = assign_clip_timestamps(
        int(clip["clip_start_capture_ms"]),
        int(clip["clip_end_capture_ms"]),
        frame_count,
    )
    return any(window_start_ms <= ts <= window_end_ms for ts in timestamps)


class IncidentSummaryHandler:
    def __init__(
        self,
        store: ObjectStore,
        provider: KeypointProvider,
        *,
        client_factory: Callable[[], VlmClient] = client_from_env,
        monotonic: Callable[[], float] = time.monotonic,
        decode_clip: Callable[..., Sequence[np.ndarray]] = decode_clip_frames,
        count_clip: Callable[..., int] | None = None,
        max_frames: int = DEFAULT_MAX_FRAMES,
    ) -> None:
        if max_frames <= 0:
            raise ValueError("max_frames 는 1 이상이어야 한다")
        self._store = store
        self._provider = provider
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._decode_clip = decode_clip
        if count_clip is not None:
            self._count_clip: Callable[..., int] | None = count_clip
        elif decode_clip is decode_clip_frames or _accepts_kwarg(decode_clip, "frame_indices"):
            self._count_clip = count_clip_frames
        else:
            self._count_clip = None
        self._max_frames = max_frames

    def _fetch_clip_bytes(self, ctx: JobContext, clip: Mapping[str, Any]) -> bytes:
        _check_cancelled(ctx)
        rem = ctx.remaining_s()
        if rem is not None and rem <= 0:
            raise JobFailed("CLIP_FETCH_FAILED", "남은 시간 예산이 없어 클립을 받지 않는다", retryable=True)
        try:
            raw = self._store.get(ctx, clip["clip_uri"])
        except ObjectStoreError as e:
            if ctx.cancelled.is_set():
                raise JobFailed("CANCELLED", "작업이 취소됐다") from None
            raise JobFailed("CLIP_FETCH_FAILED", str(e), retryable=e.retryable) from e
        _check_cancelled(ctx)
        return raw

    def _sample_with_selective_decode(
        self,
        ctx: JobContext,
        inp: Mapping[str, Any],
        *,
        window_start_ms: int,
        window_end_ms: int,
    ) -> tuple[list[SampledFrame], list[str]]:
        assert self._count_clip is not None
        event_id = inp["event_id"]
        clips_with_counts: list[tuple[Mapping[str, Any], int]] = []
        raw_by_order: dict[int, bytes] = {}
        decode_drops: list[str] = []

        for clip in inp["clips"]:
            raw = self._fetch_clip_bytes(ctx, clip)
            try:
                _guard_decode_budget(ctx.cancelled.is_set, ctx.remaining_s)
                frame_count = int(
                    _call_with_supported_kwargs(
                        self._count_clip,
                        raw,
                        cancelled=ctx.cancelled.is_set,
                        remaining_s=ctx.remaining_s,
                    )
                )
                _check_cancelled(ctx)
            except ClipDecodeCancelled:
                raise JobFailed("CANCELLED", "작업이 취소됐다") from None
            except ClipDecodeBudgetExceeded:
                log.warning("이벤트 %s: 클립 %s 프레임 수 확인 중 시간 예산 소진", event_id, clip["clip_id"])
                decode_drops.append(f"{clip['clip_id']}: budget")
                return [], decode_drops
            except ValueError as e:
                log.warning("이벤트 %s: 클립 %s 디코드 실패(%s)", event_id, clip["clip_id"], e)
                decode_drops.append(f"{clip['clip_id']}: decode")
                continue
            if frame_count <= 0:
                decode_drops.append(f"{clip['clip_id']}: empty")
                continue
            order = len(clips_with_counts)
            clips_with_counts.append((clip, frame_count))
            if _has_in_window_frame(clip, frame_count, window_start_ms, window_end_ms):
                raw_by_order[order] = raw

        plan = plan_clip_frames(
            clips_with_counts,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            max_frames=self._max_frames,
        )
        if not plan:
            return [], decode_drops

        plans_by_order: dict[int, list[PlannedFrame]] = {}
        for p in plan:
            plans_by_order.setdefault(p.clip_order, []).append(p)

        decoded_by_order: dict[int, dict[int, np.ndarray]] = {}
        for clip_order, clip_plans in plans_by_order.items():
            clip, _ = clips_with_counts[clip_order]
            raw = raw_by_order.pop(clip_order)
            indices = [p.frame_index for p in clip_plans]
            try:
                _guard_decode_budget(ctx.cancelled.is_set, ctx.remaining_s)
                images = list(
                    _call_with_supported_kwargs(
                        self._decode_clip,
                        raw,
                        frame_indices=indices,
                        cancelled=ctx.cancelled.is_set,
                        remaining_s=ctx.remaining_s,
                    )
                )
                _check_cancelled(ctx)
            except ClipDecodeCancelled:
                raise JobFailed("CANCELLED", "작업이 취소됐다") from None
            except ClipDecodeBudgetExceeded:
                log.warning("이벤트 %s: 클립 %s 선택 프레임 디코딩 중 시간 예산 소진", event_id, clip["clip_id"])
                decode_drops.append(f"{clip['clip_id']}: budget")
                return [], decode_drops
            except ValueError as e:
                log.warning("이벤트 %s: 클립 %s 디코드 실패(%s)", event_id, clip["clip_id"], e)
                decode_drops.append(f"{clip['clip_id']}: decode")
                continue
            if len(images) != len(indices):
                decode_drops.append(f"{clip['clip_id']}: decode")
                continue
            decoded_by_order[clip_order] = dict(zip(indices, images, strict=True))

        raw_by_order.clear()
        sampled = [
            SampledFrame(
                source_id=p.source_id,
                clip_id=p.clip_id,
                camera_id=p.camera_id,
                frame_index=p.frame_index,
                capture_time_ms=p.capture_time_ms,
                image=decoded_by_order[p.clip_order][p.frame_index],
            )
            for p in plan
            if p.clip_order in decoded_by_order and p.frame_index in decoded_by_order[p.clip_order]
        ]
        return sampled, decode_drops

    def _sample_with_custom_decoder(
        self,
        ctx: JobContext,
        inp: Mapping[str, Any],
        *,
        window_start_ms: int,
        window_end_ms: int,
    ) -> tuple[list[SampledFrame], list[str]]:
        event_id = inp["event_id"]
        clips_with_counts: list[tuple[Mapping[str, Any], int]] = []
        in_window_by_order: dict[int, dict[int, np.ndarray]] = {}
        decode_drops: list[str] = []
        single_clip = len(inp["clips"]) == 1

        for clip in inp["clips"]:
            raw = self._fetch_clip_bytes(ctx, clip)
            try:
                _guard_decode_budget(ctx.cancelled.is_set, ctx.remaining_s)
                images = list(
                    _call_with_supported_kwargs(
                        self._decode_clip,
                        raw,
                        cancelled=ctx.cancelled.is_set,
                        remaining_s=ctx.remaining_s,
                    )
                )
                _check_cancelled(ctx)
            except ClipDecodeCancelled:
                raise JobFailed("CANCELLED", "작업이 취소됐다") from None
            except ClipDecodeBudgetExceeded:
                log.warning("이벤트 %s: 클립 %s 디코딩 중 시간 예산 소진", event_id, clip["clip_id"])
                decode_drops.append(f"{clip['clip_id']}: budget")
                return [], decode_drops
            except ValueError as e:
                log.warning("이벤트 %s: 클립 %s 디코드 실패(%s)", event_id, clip["clip_id"], e)
                decode_drops.append(f"{clip['clip_id']}: decode")
                continue
            if not images:
                decode_drops.append(f"{clip['clip_id']}: empty")
                continue
            frame_count = len(images)
            order = len(clips_with_counts)
            clips_with_counts.append((clip, frame_count))
            if single_clip:
                plan_one = plan_clip_frames(
                    [(clip, frame_count)],
                    window_start_ms=window_start_ms,
                    window_end_ms=window_end_ms,
                    max_frames=self._max_frames,
                )
                in_window_by_order[order] = {p.frame_index: images[p.frame_index] for p in plan_one}
            else:
                timestamps = assign_clip_timestamps(
                    int(clip["clip_start_capture_ms"]),
                    int(clip["clip_end_capture_ms"]),
                    frame_count,
                )
                in_window_by_order[order] = {
                    idx: img
                    for idx, (ts, img) in enumerate(zip(timestamps, images, strict=True))
                    if window_start_ms <= ts <= window_end_ms
                }
            del images

        plan = plan_clip_frames(
            clips_with_counts,
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            max_frames=self._max_frames,
        )
        sampled = [
            SampledFrame(
                source_id=p.source_id,
                clip_id=p.clip_id,
                camera_id=p.camera_id,
                frame_index=p.frame_index,
                capture_time_ms=p.capture_time_ms,
                image=in_window_by_order[p.clip_order][p.frame_index],
            )
            for p in plan
        ]
        return sampled, decode_drops

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
        if self._count_clip is not None:
            sampled, decode_drops = self._sample_with_selective_decode(
                ctx,
                inp,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )
        else:
            sampled, decode_drops = self._sample_with_custom_decoder(
                ctx,
                inp,
                window_start_ms=window_start_ms,
                window_end_ms=window_end_ms,
            )

        if not sampled:
            _check_cancelled(ctx)
            retryable = any(r.endswith(": budget") for r in decode_drops)
            reasons = ", ".join(decode_drops) if decode_drops else "이벤트 구간 안 프레임 없음"
            raise JobFailed(
                "NO_FACE_SAFE_FRAMES",
                f"얼굴을 처리할 클립 프레임이 없다({reasons})",
                retryable=retryable,
            )

        meta = {
            sf.source_id: {"camera_id": sf.camera_id, "capture_time_ms": sf.capture_time_ms}
            for sf in sampled
        }
        _check_cancelled(ctx)
        gate = prepare_vlm_frames(
            [(sf.source_id, sf.image) for sf in sampled],
            inp["face_policy"],
            self._provider,
            cancelled=ctx.cancelled.is_set,
            remaining_s=ctx.remaining_s,
        )
        del sampled
        _check_cancelled(ctx)
        if gate.dropped:
            log.warning("이벤트 %s: 얼굴 처리 못 한 클립 프레임 %d개를 뺐다", event_id, len(gate.dropped))
        if not gate.frames:
            retryable = any(d.reason in ("detect", "budget") for d in gate.dropped)
            reasons = ", ".join(f"{d.source}: {d.reason}" for d in gate.dropped)
            raise JobFailed("NO_FACE_SAFE_FRAMES", f"얼굴을 처리한 프레임이 없다({reasons})", retryable=retryable)

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
