"""I5 `vlm_rationale` 처리기: 이벤트 시점 프레임 + 직전 3~5초 + 구조화 상태 → 근거 문장·상향 의견 (명세 5.4 빠른 경로, 12.5, G7).

프롬프트·응답 형식·상향 강제는 `simai_ai.vlm.rationale`, 얼굴 처리 관문은 `vlm_frames.gate_input_frames`, VLM 호출은
`simai_ai.vlm.client`. 입력·결과 형식은 `contracts/messages/vlm_rationale.schema.json`. 설계 `docs/design/vlm-rationale.md`.

- 입력은 계약으로 먼저 검사한다(형식 오류는 재시도 불가 VLM_INPUT_INVALID).
- 프레임은 촬영 시각 순으로 놓고 observation_time 뒤 프레임은 뺀다(사후 프레임·클립을 기다리거나 찾지 않는다).
- VLM 에는 관문을 거친 VlmFrame 만 넣는다. 남는 프레임이 없으면 관문의 JobFailed(NO_FACE_SAFE_FRAMES 등) 그대로.
- VLM 오류는 VlmError 의 code·retryable 그대로. 응답 형식이 어긋나면 남은 시간이 있을 때 한 번 다시 묻고, 그래도 어긋나면 재시도
  불가 VLM_ANSWER_INVALID(같은 입력에 GPU 만 더 쓰지 않게). 응답 원문은 로그에 남기지 않는다.
- 상향만(S15P31A101-273): 입력 이벤트의 현재 severity 보다 높은 proposed_severity 만 남기고, 같거나 낮은 의견은 null 로 바꾸며
  버린 사유 코드(NOT_UPWARD)를 로그에 남긴다. 결과의 severity 는 입력 그대로다. clause_ids 는 워커에 조항 검색이 없어 늘 [] 다.
- 시간 제한은 러너가 timeout_ms(VLM_FAST 5000)로 취소한다. 처리기는 단계 사이마다 ctx.cancelled 를 본다.
- 같은 Pod 로컬 사본(S15P31A101-408): local_frames 가 있으면 프레임의 local_copy 를 먼저 읽고 못 쓰면 S3(vlm_frames).
  성공하면 단계 시간(받기·얼굴 처리, VLM)을 한 줄 남긴다(다음 GPU 세션 측정용, 근거 본문·주소 없음).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any
from pathlib import Path

from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmError, VlmSettings, VlmTimeout
from simai_ai.vlm.face_blur import KeypointProvider
from simai_ai.vlm.overlay import overlay
from simai_ai.vlm.rationale import PROPOSAL_DISCARD_REASON, PROMPT, RETRY_NOTE, check_proposal, parse_answer, upward_only

from .handlers import JobContext, JobFailed
from .objects import ObjectStore
from .vlm_frames import DEFAULT_FETCH_WORKERS, LocalFrames, decode_image, gate_input_frames

INPUT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/vlm_rationale.schema.json#/$defs/input"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/vlm_rationale.schema.json#/$defs/result"}
JOB_BUDGET_MS = 5000  # job_request VLM_FAST timeout_ms. 다시 묻기는 첫 호출만큼 시간이 남을 때만

log = logging.getLogger("services.worker")


def client_from_env(env: Mapping[str, str] = os.environ) -> VlmClient:
    return VlmClient(VlmSettings.from_env(env))


def _complete_vlm(
    client: Any,
    prompt: str,
    frames: Any,
    *,
    system: str | None,
    response_format: Mapping[str, Any] | None,
    timeout_s: float | None,
) -> Any:
    """남은 시간 예산(timeout_s)이 있으면 client.complete 에 전달한다(timeout_s 인자가 없는 시험용 가짜 클라이언트는 그대로 호출)."""
    if timeout_s is not None:
        if timeout_s <= 0:
            raise VlmTimeout("VLM 호출에 남은 시간 예산이 없다")
        params = inspect.signature(client.complete).parameters
        if "timeout_s" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return client.complete(
                prompt,
                frames,
                system=system,
                response_format=response_format,
                timeout_s=timeout_s,
            )
    return client.complete(prompt, frames, system=system, response_format=response_format)


class VlmRationaleHandler:
    def __init__(
        self,
        store: ObjectStore,
        provider: KeypointProvider,
        *,
        client_factory: Callable[[], VlmClient] = client_from_env,
        monotonic: Callable[[], float] = time.monotonic,
        decode: Callable[[bytes], Any] = decode_image,
        max_fetch_workers: int = DEFAULT_FETCH_WORKERS,
        local_frames: LocalFrames | None = None,
        env: Mapping[str, str] = os.environ,
    ) -> None:
        self._store = store
        self._local_frames = local_frames
        self._overlay_enabled = env.get("SIMAI_VLM_OVERLAY", "1").strip().lower() not in ("0", "false", "off", "no")
        self._state_dir = LocalFrames.from_env(env).state_dir
        self._provider = provider
        self._client_factory = client_factory
        self._monotonic = monotonic
        self._decode = decode
        self._max_fetch_workers = max_fetch_workers

    def __call__(self, ctx: JobContext) -> Mapping[str, Any]:
        custom_clock = self._monotonic if self._monotonic is not time.monotonic else None
        t0 = ctx.bind_budget(JOB_BUDGET_MS, monotonic=custom_clock)
        inp = json.loads(json.dumps(ctx.input))
        if errors := schema_errors(inp, INPUT_SCHEMA):
            raise JobFailed("VLM_INPUT_INVALID", "; ".join(errors))
        event_id = inp["event_id"]
        frames = sorted(inp["frames"], key=lambda f: f["estimated_capture_time_ms"])
        late = [f["frame_uri"] for f in frames if f["estimated_capture_time_ms"] > inp["observation_time"]]
        if late:
            log.warning("이벤트 %s: observation_time 뒤 프레임 %d개를 뺐다", event_id, len(late))
            frames = [f for f in frames if f["estimated_capture_time_ms"] <= inp["observation_time"]]
        if not frames:
            raise JobFailed("VLM_INPUT_INVALID", "이벤트 시점까지의 프레임이 없다")
        try:
            client = self._client_factory()
        except VlmError as e:
            raise JobFailed(e.code, str(e), retryable=e.retryable) from None

        _check_cancelled(ctx)
        t_gate = ctx.monotonic()
        gate = gate_input_frames(
            ctx, self._store, frames, self._provider, decode=self._decode, max_workers=self._max_fetch_workers,
            local=self._local_frames,
        )
        t_vlm = ctx.monotonic()
        if gate.dropped:
            log.warning("이벤트 %s: 얼굴 처리 못 한 프레임 %d개를 뺐다", event_id, len(gate.dropped))
        meta = {f["frame_uri"]: f for f in frames}
        vlm_frames = gate.frames
        applied = False
        if self._overlay_enabled:
            cameras = _calibration_cameras(self._state_dir, inp["structured_state"].get("calibration_version"))
            state = {**inp["structured_state"], "event_zone_id": inp.get("zone_id")}
            rendered = [overlay(f, meta[f.source], state, inp["world_ids"], cameras.get(meta[f.source]["camera_id"]))
                        for f in gate.frames]
            vlm_frames = tuple(f for f, _ in rendered)
            applied = any(drawn for _, drawn in rendered)
        parts = PROMPT.build({**inp, "frames": [meta[f.source] for f in gate.frames], "overlay_applied": applied})
        ctx.report_progress(0.3)

        for retry in (False, True):
            _check_cancelled(ctx)
            rem_s = ctx.remaining_s()
            try:
                r = _complete_vlm(
                    client,
                    parts.text + RETRY_NOTE if retry else parts.text,
                    vlm_frames,
                    system=parts.system,
                    response_format=parts.response_format,
                    timeout_s=rem_s,
                )
            except VlmError as e:
                raise JobFailed(e.code, str(e), retryable=e.retryable) from None
            try:
                if r.finish_reason == "length":  # max_tokens 에서 잘렸다. 파싱되더라도 쓰지 않는다
                    raise ValueError("max_tokens 에서 잘렸다")
                answer = parse_answer(r.content)
                break
            except ValueError as e:
                rem_after_s = ctx.remaining_s()
                rem_after_ms = (
                    rem_after_s * 1000
                    if rem_after_s is not None
                    else (JOB_BUDGET_MS - (ctx.monotonic() - t0) * 1000)
                )
                log.warning("이벤트 %s: VLM 응답 형식 오류(%s, finish_reason=%s)", event_id, e, r.finish_reason)
                if retry or rem_after_ms < r.latency_ms:
                    raise JobFailed("VLM_ANSWER_INVALID", f"VLM 응답이 형식에 맞지 않는다: {e}") from None

        _check_cancelled(ctx)
        proposed, discard_reason = check_proposal(inp["severity"], answer.proposed_severity)
        if discard_reason is not None:
            log.warning(
                "이벤트 %s: 상향이 아닌 의견을 버렸다 (reason=%s, severity=%s, proposed_severity=%s)",
                event_id,
                discard_reason,
                inp["severity"],
                answer.proposed_severity,
            )
        result = {
            "event_id": event_id,
            "event_sequence": inp["event_sequence"],
            "severity": inp["severity"],
            "rationale": answer.rationale,
            "proposed_severity": proposed,
            "clause_ids": [],
            "model_version": r.model,
            "latency_ms": round((ctx.monotonic() - t0) * 1000),
        }
        if errors := schema_errors(result, RESULT_SCHEMA):
            raise JobFailed("VLM_RESULT_INVALID", "; ".join(errors))
        log.info("이벤트 %s: 근거 완료 frames=%d gate_ms=%d vlm_ms=%d total_ms=%d", event_id, len(gate.frames),
                 round((t_vlm - t_gate) * 1000), round((ctx.monotonic() - t_vlm) * 1000), result["latency_ms"])
        ctx.report_progress(1.0)
        return result


def _check_cancelled(ctx: JobContext) -> None:
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")


def _calibration_cameras(state_dir: Path, version: Any) -> dict[str, Mapping[str, Any]]:
    """이벤트가 사용한 버전과 로컬 저장소의 문서 해시가 일치할 때만 읽는다."""
    if not isinstance(version, str) or not version:
        return {}
    try:
        root = state_dir / "calibrations"
        head = json.loads((root / "active.json").read_text(encoding="utf-8"))
        path = root / (hashlib.sha256(version.encode("utf-8")).hexdigest() + ".json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(json.dumps(doc, sort_keys=True, allow_nan=False).encode("utf-8")).hexdigest()
        if version not in head["versions"] or doc["calibration_version"] != version or head["document_hashes"][version] != digest:
            return {}
        return {c["camera_id"]: c for c in doc["cameras"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
