"""VLM 작업이 프레임을 VLM 에 넘기기 직전의 관문 (명세 5.4 "VLM 입력 프레임", S15P31A101-270, S15P31A101-384).

vlm_rationale 처리기(S15P31A101-25)는 input.frames[] 를 이 함수로 받고, 돌려받은 `VlmFrame` 만 VLM 에 넣는다.
- 프레임 다운로드는 제한된 병렬 스레드(기본 최대 4개)로 동시에 수행하되, 결과 순서는 입력 프레임 순서를 유지한다.
- 서명·GET·얼굴 처리는 모두 작업의 남은 시간 예산(ctx.remaining_s)과 취소(ctx.cancelled) 안에서만 수행한다.
- 흐리지 못한 프레임은 빼고, 남는 프레임이 없으면 `JobFailed` 로 끝낸다(흐리지 않은 프레임으로 대신하지 않는다).
incident_summary(S15P31A101-271)는 클립에서 뽑은 프레임 배열을 `simai_ai.vlm.face_blur.prepare_vlm_frames` 에 바로 넣는다.

같은 Pod 로컬 사본(S15P31A101-408): 프레임에 local_copy 가 있고 `LocalFrames` 가 주어지면 Tier 0 상태 폴더의 사본을 먼저 읽는다.
상태 폴더의 event-frames 밖(심볼릭 링크 포함)·크기·sha256 이 어긋나면 쓰지 않고 frame_uri(S3)로 받는다. S3 객체가 아직 올라오는
중이면(403·404·일시 오류) 남은 시간 안에서 짧게 다시 시도한다. 사본이든 S3 든 받은 바이트는 같은 얼굴 처리 관문을 거친다.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.vlm.face_blur import DroppedFrame, GateResult, KeypointProvider, VlmFrame, prepare_vlm_frames

from .handlers import JobContext, JobFailed
from .objects import ObjectStore, ObjectStoreError

DEFAULT_FETCH_WORKERS = 4
# 계약 pod_local_frame.path 와 같은 꼴(상태 폴더 기준 상대 경로, `..`·절대 경로 불가)
LOCAL_PATH = re.compile(r"^event-frames/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/[A-Za-z0-9_-]{1,96}\.jpg$")
LOCAL_MAX_BYTES = 16 * 1024 * 1024  # 계약 pod_local_frame.size_bytes 상한(업로드 목적 EVENT_FRAME 과 같음)
S3_TRIES = 4  # 사본이 없을 때 S3 받기 시도 수(첫 시도 포함). Tier 0 가 업로드를 마치기 전에 알릴 수 있다(-408)
S3_RETRY_DELAY_S = 0.25
NOT_YET_UPLOADED = (403, 404)  # 아직 없는 객체: S3 는 ListBucket 권한이 없으면 404 대신 403 을 준다

log = logging.getLogger("services.worker")


class LocalFrames:
    """같은 Pod 의 Tier 0 상태 폴더(state_dir)에서 이벤트 사진 사본을 읽는다(-408). 맞지 않으면 None → S3."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LocalFrames:
        """SIMAI_STATE_DIR, 없으면 Tier 0 표시 파일(SIMAI_TIER0_MARKER = state_dir/tier0.live)의 폴더, 없으면 $SIMAI_ROOT/state."""
        explicit = (env.get("SIMAI_STATE_DIR") or "").strip()
        if explicit:
            return cls(Path(explicit))
        marker = (env.get("SIMAI_TIER0_MARKER") or "").strip()
        if marker:
            return cls(Path(marker).parent)
        return cls(Path(env.get("SIMAI_ROOT") or "/workspace/simai") / "state")

    def read(self, copy: Any) -> bytes | None:
        """local_copy 하나 → 바이트. 형식·경로 탈출·크기·sha256 이 어긋나거나 파일이 없으면 None."""
        if not isinstance(copy, Mapping):
            return _reject("form")
        rel, expected, size = copy.get("path"), copy.get("sha256"), copy.get("size_bytes")
        if (not isinstance(rel, str) or not LOCAL_PATH.fullmatch(rel) or not isinstance(expected, str)
                or isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= LOCAL_MAX_BYTES):
            return _reject("form")
        try:
            base = (self.state_dir / "event-frames").resolve(strict=True)
            target = (self.state_dir / rel).resolve(strict=True)  # 심볼릭 링크를 따라간 실제 위치로 본다
        except OSError:
            # 다른 Pod 에서 돌거나 Tier 0 가 지웠다: 예비 경로(S3)로 간다
            log.info("이벤트 사진 로컬 사본이 없다, 객체 저장소에서 받는다")
            return None
        if not target.is_relative_to(base):
            return _reject("escape")
        try:
            info = os.stat(target)
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                return _reject("size")
            with open(target, "rb") as handle:
                data = handle.read(LOCAL_MAX_BYTES + 1)
        except OSError:
            return _reject("io")
        if len(data) != size:
            return _reject("size")
        if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
            return _reject("sha256")
        return data


def _reject(reason: str) -> None:
    log.warning("이벤트 사진 로컬 사본을 쓰지 않는다(reason=%s), 객체 저장소에서 받는다", reason)
    return None


def decode_image(data: bytes) -> np.ndarray:
    """JPEG·PNG 바이트 → BGR uint8. 워커 이미지에는 OpenCV(headless)가 있다."""
    import cv2

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("이미지를 디코드하지 못했다")
    return image


def _get_s3(ctx: JobContext, store: ObjectStore, uri: str) -> bytes:
    """S3 에서 받는다. 아직 올라오지 않은 객체(403·404)·일시 오류는 남은 시간 안에서 S3_TRIES 까지 다시 해 본다."""
    for attempt in range(1, S3_TRIES + 1):
        try:
            return store.get(ctx, uri)
        except ObjectStoreError as e:
            if ctx.cancelled.is_set() or attempt == S3_TRIES or not (e.status in NOT_YET_UPLOADED or e.retryable):
                raise
            rem = ctx.remaining_s()
            if rem is not None and rem <= S3_RETRY_DELAY_S:
                raise
            if ctx.cancelled.wait(S3_RETRY_DELAY_S):
                raise
    raise AssertionError("도달하지 않는다")  # pragma: no cover


def _fetch_frames_parallel(
    ctx: JobContext,
    store: ObjectStore,
    frames: Sequence[Mapping[str, Any]],
    *,
    max_workers: int,
    local: LocalFrames | None = None,
) -> list[bytes]:
    if not frames:
        return []
    found: dict[int, bytes] = {}
    if local is not None:
        for i, f in enumerate(frames):
            if "local_copy" in f and (data := local.read(f["local_copy"])) is not None:
                found[i] = data
    if found:
        log.info("이벤트 사진 %d장 중 %d장을 같은 Pod 로컬 사본으로 읽었다", len(frames), len(found))
    rest = [i for i in range(len(frames)) if i not in found]
    remote = _fetch_remote(ctx, store, [frames[i] for i in rest], max_workers=max_workers)
    found.update(zip(rest, remote, strict=True))
    return [found[i] for i in range(len(frames))]


def _fetch_remote(
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
                out.append(_get_s3(ctx, store, f["frame_uri"]))
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
            fetched[idx] = _get_s3(ctx, store, frames[idx]["frame_uri"])
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
    local: LocalFrames | None = None,
) -> GateResult:
    """vlm_rationale input.frames[](frame_uri·face_policy) → 얼굴을 처리한 VLM 프레임. 하나도 안 남으면 JobFailed.

    face_policy 는 계약상 프레임마다 있으므로 프레임마다 그 값으로 처리한다.
    다운로드는 제한된 병렬(max_workers)로 수행하고, 얼굴 처리는 남은 시간 예산 안에서 순서대로 수행한다.
    local 이 있으면 같은 Pod 로컬 사본(local_copy)을 먼저 읽고, 못 쓰는 장만 객체 저장소에서 받는다(-408).
    """
    if max_workers <= 0:
        raise ValueError("max_workers 는 1 이상이어야 한다")
    if ctx.cancelled.is_set():
        raise JobFailed("CANCELLED", "작업이 취소됐다")
    raw_frames = _fetch_frames_parallel(ctx, store, frames, max_workers=max_workers, local=local)
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
