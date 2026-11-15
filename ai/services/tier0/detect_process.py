"""Tier 0 검출·판정 프로세스 분리 (S15P31A101-728). 설정 `process_split`(기본 끔)일 때만 쓴다.

설계: docs/design/S15P31A101-728-tier0-process-split.md. 꺼져 있으면 이 모듈은 쓰이지 않고 지금 단일 프로세스 경로 그대로다.

- 검출 프로세스(spawn 자식, `_detector_child_main` 최상위 함수): 영상 수신(LIVE 는 I0 영상 포트, file 은 판정 프로세스가 보낸 녹화
  메시지) → 디코딩 → 최신성 대기열(`I0Ingest`) → 검출 → `ResultGate` → 카메라별 Local Tracker. 지금과 같은 `DetectorFeed` 를 그대로
  돌리고 나온 `Observation2DFrame` 만 보낸다. GPU 선확보와 아바타 입력 공유 쓰기(그림이 필요)도 여기서 한다.
- 판정 프로세스(지금 Tier 0 프로세스): `DetectorProcess` 가 자식을 띄우고 감독하며, `SplitDetectorFeed` 가 판정 루프의 입력(FrameFeed)이
  된다. World Track·Risk Engine·저널·이벤트 순번·I1·I0 제어 채널·표시 파일은 모두 판정 프로세스만 갖는다(단일 소유).

경계 큐: 검출 → 판정은 관측 묶음을 유계 큐로 보낸다(차면 버리고 센다). 판정 쪽은 카메라별 유계 deque(지금 대기열과 같은 깊이,
넘치면 가장 오래된 것을 버림)에 넣고, 카메라별 촬영 시각이 늘지 않은 관측(중복·역순)은 버리고 센다. 카메라별 최신 1건만 남기지
않는다(보간에 t 앞뒤 관측이 필요하다).

file 입력은 lockstep 이다: 판정 스레드가 녹화 메시지를 명령으로 보내고, 관측 수거 때 `step` 을 보내 답을 기다린다(명령은 받은 순서대로
처리). 명령마다 판정 시계 값을 실어 보내 검출 프로세스의 `I0Ingest` 가 그 값을 쓴다(`_MirrorClock`). 그래서 최대 속도 재생은 단일
프로세스와 같은 결과를 낸다.

장애: LIVE 중 자식이 죽거나 하트비트가 끊기면 그때부터 모든 카메라를 연결 아님으로 보고(모르면 비움, 관측을 지어내지 않음) 백오프로
새 세대를 띄운다. 새 세대의 로컬 트랙 ID 에는 `-g<세대>` 를 붙여 옛 World Track 과 섞이지 않게 한다. 짧은 시간에 너무 자주 죽으면
`fatal_reason` 을 세워 판정 프로세스가 종료 코드 4 로 끝나게 한다(bootstrap 이 Tier 0 전체를 다시 띄움). file 입력은 다시 띄우지 않고
재생 실패로 둔다. 판정 프로세스가 죽으면 자식은 1초 안에 부모가 없는 것을 보고 끝난다.
"""

from __future__ import annotations

import atexit
from collections import deque
from collections.abc import Callable, Mapping, Sequence
import dataclasses
from dataclasses import dataclass, field
import logging
import multiprocessing
import multiprocessing.util  # noqa: F401 - 아래 atexit 가 multiprocessing 의 자식 join 보다 먼저 돌게 먼저 등록시킨다
import os
from pathlib import Path
import pickle
import queue
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
import weakref

from simai_ai.calibration.drift import CameraObservation
from simai_ai.gpu.admission import LoadWindow
from simai_ai.tracking.local_tracker import Observation2DFrame

log = logging.getLogger("services.tier0.detect_process")

STATUS_EVERY_S = 0.5
"""검출 프로세스 하트비트(상태) 주기"""
STALE_S = 3.0
"""하트비트가 이만큼 없으면 카메라를 연결 아님으로 보고 검출 처리 시간을 모름으로 센다"""
STUCK_S = 10.0
"""하트비트가 이만큼 없으면 멈춘 것으로 보고 자식을 내린 뒤 다시 띄운다"""
BUILD_TIMEOUT_S = 300.0
"""기동(GPU 선확보·엔진 적재·디코더) 대기 상한"""
VIDEO_START_TIMEOUT_S = 30.0
RPC_TIMEOUT_S = 60.0
"""file 입력 lockstep 한 단계 응답 대기 상한"""
IPC_QUEUE_MAX = 256
RESTART_BACKOFF_S = 1.0
RESTART_BACKOFF_MAX_S = 5.0
MAX_RESTARTS = 3
RESTART_WINDOW_S = 60.0
PARENT_POLL_S = 1.0
STOP_JOIN_S = 7.0
CHILD_THREAD_CPU_EVERY_S = 30.0
PROCESSING_MAX_VALUES = 256  # simai_ai.gpu.processing.DEFAULT_PROCESSING_MAX_VALUES 와 같다
_CPU_LIST = re.compile(r"\d+(-\d+)?(,\d+(-\d+)?)*")


class DetectorProcessError(RuntimeError):
    """검출 프로세스를 시작하지 못했다. exit_code 는 단일 프로세스 경로와 같은 규칙(2 설정, 4 입력·검출기)."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class DetectorProcessSpec:
    """검출 프로세스에 넘기는 설정(pickle 된다). 팩토리는 모듈 최상위 이름이어야 한다(spawn)."""

    mode: str  # "live" | "file"
    camera_ids: tuple[str, ...]
    decoder: str = "cpu"
    queue_depth: int = 1
    max_queue_wait_ms: int = 150
    detect_tick_ms: int = 50
    detect_queue_depth: int = 8
    engine_path: str | None = None
    weights_path: str | None = None
    imgsz: int = 1280
    cuda_stream_priority: str = "normal"
    gpu_reserve_mb: int = 0
    gpu_device: int = 0
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    avatar_channel: str | None = None
    offload_side_work: bool = False
    detect_cpus: str = "none"
    log_level: int = logging.INFO
    detector_factory: Callable[[], Any] | None = None
    tracker_backend_factory: Any = None
    input_factory: Callable[[DetectorProcessSpec, Any], Any] | None = None
    """시험: LIVE 입력 대역(next_batch·connected·start·stop·snapshot). None 이면 I0 영상 포트(LiveInput)"""
    clock_factory: Callable[[], Any] | None = None
    """LIVE 시계(시험 주입). None 이면 진입점이 OS 시계로 만든다"""


def detect_cpu_set(value: str | None, current: Sequence[int]) -> set[int] | None:
    """검출 프로세스 CPU 묶음. none → None(물려받음), auto → 지금 묶음의 앞쪽 절반(최소 1), 목록 → 그 CPU(지금 묶음의 부분)."""
    v = (value or "none").strip().lower()
    ordered = sorted(set(current))
    if v == "none" or not ordered:
        return None
    if v == "auto":
        return set(ordered[: max(1, len(ordered) // 2)])
    if not _CPU_LIST.fullmatch(v):
        raise ValueError(f"CPU 목록 형식 오류: {value!r} (예: auto, none, 0-3,6)")
    cpus: set[int] = set()
    for part in v.split(","):
        lo, _, hi = part.partition("-")
        cpus.update(range(int(lo), int(hi or lo) + 1))
    if not cpus <= set(ordered):
        raise ValueError(f"CPU {sorted(cpus - set(ordered))} 는 지금 쓸 수 있는 CPU 가 아니다")
    return cpus


# ---------------------------------------------------------------------------- 검출 프로세스(자식)


class _MirrorClock:
    """file 입력: 판정 프로세스가 명령에 실어 보낸 판정 시계 값을 그대로 낸다(검출 프로세스는 재생 시계를 갖지 않는다)."""

    def __init__(self) -> None:
        self._now = 0

    def set(self, now_ms: int) -> None:
        self._now = int(now_ms)

    def now_ms(self) -> int:
        return self._now


class _SystemClock:
    """LIVE: 판정 프로세스의 기본 시계와 같은 OS 시계로 만든 판정 시각(앞으로만 감). 최신성 정책에만 쓴다(관측 시각은 헤더 값)."""

    def __init__(self, monotonic_ns: Callable[[], int], wall_ms: Callable[[], int]) -> None:
        from simai_ai.tracking.clock_sync import Tier0Clock
        from simai_ai.tracking.decision_time import DecisionClock

        self._clock = DecisionClock(Tier0Clock(monotonic_ns, wall_ms))
        self._lock = threading.Lock()

    def now_ms(self) -> int:
        with self._lock:
            return self._clock.now_ms()


class _OffsetTracker:
    """재시작한 세대의 로컬 트랙 ID 에 `-g<세대>` 를 붙인다(옛 World Track 의 local_keys 와 겹치지 않게). 세대 0 은 그대로."""

    def __init__(self, tracker: Any, generation: int) -> None:
        self._tracker = tracker
        self._suffix = f"-g{generation}" if generation else ""

    def update_batch(self, detections: Sequence[Any]) -> list[Observation2DFrame]:
        out = self._tracker.update_batch(detections)
        if not self._suffix:
            return out
        return [dataclasses.replace(o, detections=tuple(
            dataclasses.replace(d, local_track_id=f"{d.local_track_id}{self._suffix}") for d in o.detections))
            for o in out]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._tracker, name)


class _LiveChildInput:
    """LIVE: I0 영상 포트 + I0Ingest(live_input.LiveInput 그대로)."""

    def __init__(self, live: Any) -> None:
        self.live = live
        self.ingest = live.ingest
        self.camera_ids = tuple(live.camera_ids)

    def next_batch(self) -> list[Any]:
        return self.ingest.next_batch()

    def connected(self, camera_id: str) -> bool:
        return self.live.connected(camera_id)

    def start(self) -> None:
        self.live.start()

    def stop(self, timeout_s: float) -> list[str]:
        return self.live.stop(timeout_s)

    def snapshot(self) -> dict[str, Any]:
        live = self.live
        return {
            "connected": {c: live.connected(c) for c in self.camera_ids},
            "waiting_keyframe": {c: live.waiting_keyframe(c) for c in self.camera_ids},
            "stream_epoch": {c: live.server.streams[c].stream_epoch for c in self.camera_ids},
            "frame_totals": live.frame_totals(),
            "counts": {c: live.ingest.log.counts(c) for c in self.camera_ids},
            "live_status": live.status(),
            "input_timing": live.bridge.timing.summary(),
        }


class _FileChildInput:
    """file: 판정 프로세스가 보낸 녹화 I0 메시지를 받는 I0Ingest."""

    def __init__(self, ingest: Any) -> None:
        self.ingest = ingest
        self.camera_ids = tuple(ingest.camera_ids)
        self.errors = 0

    def next_batch(self) -> list[Any]:
        return self.ingest.next_batch()

    def connected(self, camera_id: str) -> bool:
        return self.ingest.camera(camera_id).ready

    def start(self) -> None:
        return None

    def stop(self, timeout_s: float) -> list[str]:
        return []

    def snapshot(self) -> dict[str, Any]:
        ingest = self.ingest
        return {
            "ready": {c: ingest.camera(c).ready for c in self.camera_ids},
            "pending": {c: ingest.scheduler.pending(c) for c in self.camera_ids},
            "counts": {c: ingest.log.counts(c) for c in self.camera_ids},
            "ingest_errors": self.errors,
        }


class _ChildAvatar:
    """아바타 입력 공유(검출 프로세스 쪽). 판정 tick 의 재료로 지금과 같은 순수 함수(avatar_metadata)·작성기로 쓴다."""

    def __init__(self, path: str, feed: Any, offload: bool) -> None:
        self._wall = 0
        self.feed = feed
        clock = lambda: self._wall  # noqa: E731 - 판정 프로세스가 요청에 실은 벽시계(재생이면 재생 시계)
        if offload:
            from services.tier0.side_work import OffloadedChannelWriter

            self.writer: Any = OffloadedChannelWriter(Path(path), clock_ms=clock)
        else:
            from simai_ai.io.avatar_channel import LatestChannelWriter

            self.writer = LatestChannelWriter(Path(path), clock_ms=clock)
        self.received = 0
        self.offered = 0
        self.errors = 0

    def offer(self, wall_ms: int, data: bytes) -> None:
        from services.tier0.perception import avatar_metadata

        self.received += 1
        self._wall = int(wall_ms)
        try:
            request = pickle.loads(data)
            images = dict(self.feed.avatar_frames)
            observations = self.feed.avatar_observations
            frame = SimpleNamespace(tracks=[SimpleNamespace(local_keys=keys, avatar_update_hold=hold)
                                            for keys, hold in request["tracks"]])
            rig = SimpleNamespace(calibration_version=request["calibration_version"])

            def build() -> dict[str, Any]:
                return avatar_metadata(frame, request["payload"], request["t"], images, observations, rig,
                                       request["calibration"])

            offer_deferred = getattr(self.writer, "offer_deferred", None)
            offered = offer_deferred(build, images) if offer_deferred is not None else self.writer.offer(build(), images)
            if offered is not False:
                self.offered += 1
        except Exception:  # noqa: BLE001 - 보조 출력 실패가 검출을 멈추지 않는다
            self.errors += 1
            log.warning("아바타 입력 공유 실패(검출 프로세스)", exc_info=self.errors == 1)

    def status(self) -> dict[str, Any]:
        w = self.writer
        return {"received": self.received, "offered": self.offered, "errors": self.errors + int(getattr(w, "errors", 0)),
                "dropped": int(getattr(w, "dropped", 0)), "written": getattr(w, "written", None),
                "offload": w.status() if hasattr(w, "status") else None}

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            log.debug("아바타 작성기 닫기 실패", exc_info=True)


def _decoder_factory(kind: str) -> Callable[[str], Any]:
    """카메라마다 디코더 하나(service.Tier0Service._decoder_factory 와 같다). nvdec 은 CPU 로 몰래 떨어지지 않는다."""
    from simai_ai.io import NvdecDecoder, PyAvDecoder

    if kind == "nvdec":
        return lambda _camera_id: NvdecDecoder()
    return lambda _camera_id: PyAvDecoder()


def _build_detector(spec: DetectorProcessSpec) -> Any:
    """service.Tier0Service._build_detector 와 같은 순서·종료 코드(엔진 → 가중치 → 없으면 4)."""
    if spec.detector_factory is not None:
        return spec.detector_factory()
    from services.tier0.yolo_detector import DetectorUnavailable, UltralyticsPoseDetector

    label = spec.mode.upper()
    model = spec.engine_path if spec.engine_path and Path(spec.engine_path).exists() else None
    if model is None and spec.weights_path and Path(spec.weights_path).exists():
        log.warning("TensorRT 엔진이 없어(SIMAI_TIER0_ENGINE=%r) 가중치 %s 를 ultralytics 로 연다(느리다, 운영 기준 아님)",
                    spec.engine_path, spec.weights_path)
        model = spec.weights_path
    if model is None:
        raise DetectorProcessError(f"{label} 입력: 검출기가 없다(SIMAI_TIER0_ENGINE={spec.engine_path!r}, "
                                   f"SIMAI_TIER0_WEIGHTS={spec.weights_path!r} 둘 다 없음)", 4)
    try:
        return UltralyticsPoseDetector(model, imgsz=spec.imgsz, high_priority_stream=spec.cuda_stream_priority == "high")
    except DetectorUnavailable as e:
        raise DetectorProcessError(f"{label} 입력: {e}", 4) from e
    except Exception as e:  # noqa: BLE001 - 엔진·가중치를 읽지 못함(손상·GPU 불일치)
        raise DetectorProcessError(f"{label} 입력: 검출기 {model} 를 열지 못했다 ({type(e).__name__}: {e})", 4) from e


def _build_input(spec: DetectorProcessSpec, clock: Any) -> Any:
    from simai_ai.io import DecoderUnavailable, FreshnessConfig, I0Ingest

    if spec.mode == "live" and spec.input_factory is not None:
        return spec.input_factory(spec, clock)
    config = FreshnessConfig(queue_depth=spec.queue_depth, max_queue_wait_ms=spec.max_queue_wait_ms, reorder_window_ms=0)
    try:
        ingest = I0Ingest(spec.camera_ids, _decoder_factory(spec.decoder), clock, config)
    except DecoderUnavailable as e:
        raise DetectorProcessError(f"{spec.mode.upper()} 입력: 디코더({spec.decoder})를 열지 못했다: {e}", 4) from e
    if spec.mode == "file":
        return _FileChildInput(ingest)
    from services.tier0.live_input import LiveInput
    from services.tier0.video_ingest import build_server

    try:
        live = LiveInput(ingest, lambda consumer: build_server(spec.env, consumer))
    except (ValueError, OSError) as e:
        raise DetectorProcessError(f"LIVE 입력: I0 영상 수신 설정 오류: {e}", 2) from e
    return _LiveChildInput(live)


def _pin_cpus(value: str) -> None:
    if not hasattr(os, "sched_getaffinity"):
        return
    try:
        cpus = detect_cpu_set(value, sorted(os.sched_getaffinity(0)))
        if cpus is not None:
            os.sched_setaffinity(0, cpus)
            log.info("검출 프로세스 CPU 고정: %s", sorted(cpus))
    except (OSError, ValueError) as e:
        log.warning("검출 프로세스 CPU 고정 실패(물려받은 묶음 그대로): %s", e)


class _ChildRunner:
    """검출 프로세스 본체. 주 스레드는 명령을 받고, LIVE 면 검출 루프·상태 스레드를 따로 돌린다."""

    def __init__(self, spec: DetectorProcessSpec, generation: int, cmd_q: Any, out_q: Any, *, feed: Any, child_input: Any,
                 clock: Any, reserve: Any, avatar: _ChildAvatar | None) -> None:
        from services.tier0.thread_cpu import ThreadCpuSampler
        from simai_ai.tracking.stage_timing import StageTiming

        self.spec = spec
        self.generation = generation
        self.cmd_q = cmd_q
        self.out_q = out_q
        self.feed = feed
        self.input = child_input
        self.clock = clock
        self.reserve = reserve
        self.avatar = avatar
        self.stop = threading.Event()
        self.loop_timing = StageTiming()
        self.cpu_sampler = ThreadCpuSampler()
        self.thread_cpu: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._processing: list[float] = []
        self._in_progress: float | None = None
        self.ipc_dropped = 0
        self.steps = 0
        self._threads: list[threading.Thread] = []
        self._last_timing = 0.0

    # --- 상태 ---------------------------------------------------------------------

    def snapshot(self, *, timing: bool = True) -> dict[str, Any]:
        snap: dict[str, Any] = {}
        try:
            snap.update(self.input.snapshot())
        except Exception:  # noqa: BLE001 - 상태 실패가 검출을 멈추지 않는다
            log.debug("입력 상태 실패", exc_info=True)
        feed = self.feed
        with self._lock:
            processing, self._processing = self._processing[-PROCESSING_MAX_VALUES:], []
            in_progress = None if self._in_progress is None else (time.perf_counter() - self._in_progress) * 1000
        snap.update(
            pid=os.getpid(), generation=self.generation, steps=self.steps,
            detector_stats=dict(vars(feed.stats)),
            camera_streams={c: list(o.image_size) for c, o in feed.camera_streams.items()},
            gpu_reserve=self.reserve.status(),
            processing=processing, in_progress_ms=in_progress,
            avatar=None if self.avatar is None else self.avatar.status(),
            ipc_dropped=self.ipc_dropped,
            loops=self.loop_timing.summary(), thread_cpu=self.thread_cpu,
        )
        if timing:
            snap["detector_timing"] = feed.timing.summary()
        return snap

    def _put(self, item: Any) -> bool:
        try:
            self.out_q.put_nowait(item)
            return True
        except queue.Full:
            return False

    # --- LIVE 루프 ------------------------------------------------------------------

    def _detect_loop(self) -> None:
        """service.Tier0Service._run_detect 와 같은 주기·쉬기. 묶음마다 나온 관측을 보낸다(차면 버리고 센다)."""
        feed = self.feed
        period = self.spec.detect_tick_ms / 1000.0
        idle_wait = min(period / 4.0, 0.005)
        next_at = time.monotonic()
        while not self.stop.is_set():
            started = time.perf_counter()
            with self._lock:
                self._in_progress = started
            ok = False
            try:
                processed = feed.detect_once()
                ok = True
            except Exception:  # noqa: BLE001 - 한 묶음 실패로 검출 루프를 멈추지 않는다
                log.exception("검출 루프 단계 실패")
                processed = 0
            elapsed = (time.perf_counter() - started) * 1000
            with self._lock:
                self._in_progress = None
                self._processing.append(elapsed if ok else float("nan"))
            observations = feed.poll(0)
            if observations and not self._put(("obs", observations)):
                self.ipc_dropped += len(observations)
            if processed <= 0:
                self._wait("detect_wake_lag", idle_wait)
                next_at = time.monotonic()
                continue
            next_at += period
            delay = next_at - time.monotonic()
            if delay < 0:
                next_at = time.monotonic()
                continue
            self._wait("detect_wake_lag", delay)

    def _wait(self, stage: str, delay: float) -> None:
        target = time.perf_counter() + delay
        if not self.stop.wait(delay):
            self.loop_timing.record(stage, None, max(0.0, (time.perf_counter() - target) * 1000))

    def _status_loop(self) -> None:
        last_cpu = time.monotonic()
        try:
            self.cpu_sampler.sample()
        except Exception:  # noqa: BLE001
            pass
        while not self.stop.wait(STATUS_EVERY_S):
            if time.monotonic() - last_cpu >= CHILD_THREAD_CPU_EVERY_S:
                last_cpu = time.monotonic()
                try:
                    self.thread_cpu = self.cpu_sampler.sample()
                except Exception:  # noqa: BLE001
                    self.thread_cpu = None
            self._put(("status", self.snapshot()))

    def _spawn(self, name: str, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, name=name, daemon=True)
        self._threads.append(t)
        t.start()

    # --- 명령 ---------------------------------------------------------------------

    def run(self) -> None:
        parent = multiprocessing.parent_process()
        try:
            while not self.stop.is_set():
                try:
                    cmd = self.cmd_q.get(timeout=PARENT_POLL_S)
                except queue.Empty:
                    if parent is not None and not parent.is_alive():
                        log.error("판정 프로세스가 없다: 검출 프로세스를 끝낸다")
                        return
                    continue
                except (EOFError, OSError):
                    return
                if cmd is None or cmd[0] == "stop":
                    return
                self._dispatch(cmd)
        finally:
            self.close()

    def _dispatch(self, cmd: tuple) -> None:
        kind = cmd[0]
        if kind == "ingest":
            self.clock.set(cmd[1])
            try:
                self.input.ingest.receive(cmd[2])
            except Exception as e:  # noqa: BLE001 - FileInput.pump 와 같이 한 프레임 오류가 재생을 멈추지 않는다
                self.input.errors += 1
                if self.input.errors == 1 or self.input.errors % 100 == 0:
                    log.warning("재생 입력 오류 %d회째 (%s: %s)", self.input.errors, type(e).__name__, e)
        elif kind == "lost":
            self.clock.set(cmd[1])
            try:
                self.input.ingest.connection_lost(cmd[2])
            except Exception as e:  # noqa: BLE001
                self.input.errors += 1
                log.warning("재생 연결 끊김 처리 오류 (%s: %s)", type(e).__name__, e)
        elif kind == "close_ingest":
            self.clock.set(cmd[1])
            error = None
            try:
                self.input.ingest.close()
            except Exception as e:  # noqa: BLE001 - 판정 프로세스의 FileInput 이 재생 실패로 남긴다
                error = f"{type(e).__name__}: {e}"
            self._put_blocking(("close_ingest", cmd[2], error, self.snapshot(timing=False)))
        elif kind == "step":
            self.clock.set(cmd[1])
            # 단일 경로 판정 tick 과 같은 순서: JudgmentLoop.tick 의 detect_once → Perception.step 의 poll
            error = None
            observations: list[Any] = []
            try:
                self.feed.detect_once()
                observations = self.feed.poll(cmd[1])
            except Exception as e:  # noqa: BLE001 - 단일 경로처럼 그 tick 만 실패로 돌려준다
                log.exception("검출 단계 실패")
                error = f"{type(e).__name__}: {e}"
            self.steps += 1
            now = time.monotonic()
            timing = now - self._last_timing >= 1.0
            if timing:
                self._last_timing = now
            self._put_blocking(("step", cmd[2], observations, self.snapshot(timing=timing), error))
        elif kind == "start_video":
            try:
                self.input.start()
            except OSError as e:
                self._put_blocking(("video", False, 4, f"I0 영상 포트를 열지 못했다 ({type(e).__name__}: {e})"))
                return
            except Exception as e:  # noqa: BLE001
                self._put_blocking(("video", False, 4, f"영상 수신 시작 실패 ({type(e).__name__}: {e})"))
                return
            self._put_blocking(("video", True, 0, ""))
            self._spawn("tier0-detect", self._detect_loop)
            self._spawn("tier0-detect-status", self._status_loop)
        elif kind == "avatar":
            if self.avatar is not None:
                self.avatar.offer(cmd[1], cmd[2])
        else:
            log.warning("모르는 명령: %r", kind)

    def _put_blocking(self, item: Any) -> None:
        try:
            self.out_q.put(item, timeout=RPC_TIMEOUT_S)
        except queue.Full:
            log.error("판정 프로세스가 응답을 받지 않는다(%s)", item[0])

    def close(self) -> None:
        self.stop.set()
        # 판정 프로세스가 더 받지 않을 때 남은 송신 버퍼 때문에 종료가 막히지 않게
        self.out_q.cancel_join_thread()
        for t in self._threads:
            t.join(2.0)
        try:
            self.input.stop(STOP_JOIN_S)
        except Exception:  # noqa: BLE001
            log.debug("입력 정지 실패", exc_info=True)
        if self.avatar is not None:
            self.avatar.close()
        try:
            self.feed.close()
        except Exception:  # noqa: BLE001
            log.debug("검출 입력 닫기 실패", exc_info=True)


def _detector_child_main(spec: DetectorProcessSpec, generation: int, cmd_q: Any, out_q: Any) -> None:
    """spawn 자식 진입점(조립 지점). 기동 결과를 ("built", ...) 또는 ("failed", 종료 코드, 사유) 로 알린 뒤 명령을 받는다."""
    logging.basicConfig(level=spec.log_level,
                        format="%(asctime)s %(levelname)s [detect] %(threadName)s %(name)s %(message)s")
    _pin_cpus(spec.detect_cpus)
    try:
        from simai_ai.gpu.cv_threads import limit_cv2_threads

        limit_cv2_threads(os.environ)
    except Exception:  # noqa: BLE001 - 부모가 이미 같은 값을 검증했다
        pass
    clock: Any
    if spec.mode == "file":
        clock = _MirrorClock()
    elif spec.clock_factory is not None:
        clock = spec.clock_factory()
    else:
        clock = _SystemClock(time.monotonic_ns, lambda: time.time_ns() // 1_000_000)
    try:
        from services.tier0.perception import DetectorFeed
        from simai_ai.gpu.reserve import GpuReservation
        from simai_ai.tracking.local_tracker import MultiCameraLocalTracker

        reserve = GpuReservation(spec.gpu_reserve_mb, device=spec.gpu_device)
        reserve.acquire()  # 실패해도 예외를 내지 않는다(상태로 남김)
        detector = _build_detector(spec)
        child_input = _build_input(spec, clock)
        kwargs: dict[str, Any] = {"camera_ids": spec.camera_ids}
        if spec.tracker_backend_factory is not None:
            kwargs["backend_factory"] = spec.tracker_backend_factory
        tracker = _OffsetTracker(MultiCameraLocalTracker(**kwargs), generation)
        feed = DetectorFeed(camera_ids=spec.camera_ids, tickets=child_input.next_batch, detector=detector,
                            tracker=tracker, connected=child_input.connected, source=spec.mode.upper(),
                            max_queued_per_camera=spec.detect_queue_depth, async_mode=spec.mode == "live")
        avatar = None
        if spec.avatar_channel is not None:
            feed.avatar_enabled = True
            avatar = _ChildAvatar(spec.avatar_channel, feed, spec.offload_side_work)
    except DetectorProcessError as e:
        out_q.put(("failed", e.exit_code, str(e)))
        return
    except Exception as e:  # noqa: BLE001
        log.exception("검출 프로세스 기동 실패")
        out_q.put(("failed", 4, f"검출 프로세스 기동 실패 ({type(e).__name__}: {e})"))
        return
    out_q.put(("built", os.getpid(), reserve.status(), "torch" in sys.modules))
    _ChildRunner(spec, generation, cmd_q, out_q, feed=feed, child_input=child_input, clock=clock, reserve=reserve,
                 avatar=avatar).run()


# ---------------------------------------------------------------------------- 판정 프로세스(부모)


@dataclass
class _Gen:
    generation: int
    process: Any
    cmd_q: Any
    out_q: Any
    pid: int | None = None


_LIVE_PROCESSES: weakref.WeakSet[DetectorProcess] = weakref.WeakSet()


def _kill_children_at_exit() -> None:
    """인터프리터 종료 때 남은 자식을 내린다(비데몬 자식을 multiprocessing 이 끝없이 기다리지 않게)."""
    for proc in list(_LIVE_PROCESSES):
        proc._kill_now()


atexit.register(_kill_children_at_exit)


class DetectorProcess:
    """검출 프로세스 하나(세대가 바뀌어도 같은 객체). 판정 프로세스 쪽 감독·경계 큐·상태 사본."""

    def __init__(
        self,
        spec: DetectorProcessSpec,
        *,
        now_ms: Callable[[], int],
        mono_s: Callable[[], float],
        on_gpu_status: Callable[[dict[str, Any]], None] | None = None,
        on_processing: Callable[[Sequence[float]], None] | None = None,
        max_restarts: int = MAX_RESTARTS,
        restart_window_s: float = RESTART_WINDOW_S,
        backoff_s: float = RESTART_BACKOFF_S,
        backoff_max_s: float = RESTART_BACKOFF_MAX_S,
        build_timeout_s: float = BUILD_TIMEOUT_S,
        rpc_timeout_s: float = RPC_TIMEOUT_S,
    ) -> None:
        if spec.mode not in ("live", "file"):
            raise ValueError(f"mode 는 live 또는 file 이다: {spec.mode!r}")
        self.spec = spec
        self.camera_ids = tuple(spec.camera_ids)
        self._camera_order = {c: i for i, c in enumerate(self.camera_ids)}
        self._now_ms = now_ms
        self._mono_s = mono_s
        self._on_gpu_status = on_gpu_status
        self._on_processing = on_processing
        self.max_restarts = max_restarts
        self.restart_window_s = restart_window_s
        self.backoff_s = backoff_s
        self.backoff_max_s = backoff_max_s
        self.build_timeout_s = build_timeout_s
        self.rpc_timeout_s = rpc_timeout_s
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._gen: _Gen | None = None
        self._snapshot: dict[str, Any] = {}
        self._status_at: float | None = None
        self._queues: dict[str, deque[Observation2DFrame]] = {c: deque() for c in self.camera_ids}
        self._last_capture: dict[str, int] = {}
        self._seq = 0
        self._stopping = threading.Event()
        self._reader: threading.Thread | None = None
        self._restart_times: deque[float] = deque()
        self.depth = spec.detect_queue_depth
        self.batches = 0
        self.received = 0
        self.dropped_observations = 0
        self.out_of_order = 0
        self.unknown_camera = 0
        self.restarts = 0
        self.deaths = 0
        self.cmd_dropped = 0
        self._ipc_dropped_base = 0
        self.last_death: str | None = None
        self.fatal_reason: str | None = None
        """LIVE: 짧은 시간에 너무 자주 죽었다. 판정 프로세스는 종료 코드 4 로 끝낸다"""
        self.failed_reason: str | None = None
        """file: 검출 프로세스가 죽었거나 응답하지 않았다. 재생 실패로 끝낸다"""
        self.torch_in_child: bool | None = None
        _LIVE_PROCESSES.add(self)

    # --- 기동 ---------------------------------------------------------------------

    def start(self) -> None:
        """검출 프로세스를 띄우고 기동 결과를 기다린다(GPU 선확보·검출기·디코더·영상 포트 설정). 실패면 DetectorProcessError."""
        self._gen = self._launch(0)

    def _launch(self, generation: int) -> _Gen:
        cmd_q = self._ctx.Queue(IPC_QUEUE_MAX)
        out_q = self._ctx.Queue(IPC_QUEUE_MAX)
        # 비데몬: 자식(아바타 곁가지 자식 등)을 다시 띄울 수 있어야 한다. 종료는 stop()·atexit 가 맡는다
        process = self._ctx.Process(target=_detector_child_main, args=(self.spec, generation, cmd_q, out_q),
                                    name=f"tier0-detect-g{generation}", daemon=False)
        process.start()
        gen = _Gen(generation, process, cmd_q, out_q)
        msg = self._await(gen, ("built", "failed"), self.build_timeout_s)
        if msg is None or msg[0] == "failed":
            self._discard(gen)
            if msg is None:
                why = (f"검출 프로세스가 기동 중 끝났다(종료 코드 {process.exitcode})" if process.exitcode is not None
                       else f"검출 프로세스가 {self.build_timeout_s:.0f}초 안에 준비되지 않았다")
                raise DetectorProcessError(why, 4)
            raise DetectorProcessError(msg[2], int(msg[1]))
        gen.pid = int(msg[1])
        self.torch_in_child = bool(msg[3])
        if self._on_gpu_status is not None:
            self._on_gpu_status(dict(msg[2]))
        log.info("검출 프로세스 준비(세대 %d, pid %d, GPU 선확보 %s)", generation, gen.pid, msg[2].get("state"))
        return gen

    def start_streaming(self, timeout_s: float = VIDEO_START_TIMEOUT_S) -> None:
        """LIVE: 영상 포트를 열게 하고(실패면 DetectorProcessError), 관측 받기 스레드를 시작한다."""
        gen = self._require_gen()
        self._open_video(gen, timeout_s)
        self._reader = threading.Thread(target=self._read_loop, name="tier0-detect-ipc", daemon=True)
        self._reader.start()

    def _open_video(self, gen: _Gen, timeout_s: float) -> None:
        gen.cmd_q.put(("start_video",), timeout=timeout_s)
        msg = self._await(gen, ("video",), timeout_s)
        if msg is None:
            raise DetectorProcessError("검출 프로세스가 영상 포트 열기에 답하지 않았다", 4)
        if not msg[1]:
            raise DetectorProcessError(msg[3], int(msg[2]))
        with self._lock:
            self._status_at = self._mono_s()  # 첫 하트비트 전까지의 신선도 기준

    def _require_gen(self) -> _Gen:
        if self._gen is None:
            raise RuntimeError("검출 프로세스를 시작하지 않았다")
        return self._gen

    # --- 받기 ---------------------------------------------------------------------

    def _await(self, gen: _Gen, kinds: tuple[str, ...], timeout_s: float, seq: int | None = None) -> tuple | None:
        """gen 의 응답을 기다린다(다른 메시지는 처리). 시간 초과·자식 종료면 None."""
        deadline = time.monotonic() + timeout_s
        while not self._stopping.is_set():
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            try:
                msg = gen.out_q.get(timeout=min(left, 0.2))
            except queue.Empty:
                if not gen.process.is_alive():
                    return None
                continue
            except (EOFError, OSError):
                return None
            if msg[0] in kinds and (seq is None or msg[1] == seq):
                return msg
            self._handle(msg)
        return None

    def _handle(self, msg: tuple) -> None:
        kind = msg[0]
        if kind == "obs":
            self._accept(msg[1])
        elif kind == "status":
            self._update(msg[1])
        elif kind == "failed":
            log.error("검출 프로세스 실패 알림: %s", msg[2])

    def _accept(self, observations: Sequence[Observation2DFrame]) -> None:
        """경계 큐에 넣는다: 카메라별 촬영 시각이 늘지 않은 관측(중복·역순)은 버리고, 카메라별 깊이를 넘치면 가장 오래된 것을 버린다."""
        with self._lock:
            self.batches += 1
            for obs in observations:
                cam = obs.camera_id
                q = self._queues.get(cam)
                if q is None:
                    self.unknown_camera += 1
                    continue
                last = self._last_capture.get(cam)
                if last is not None and obs.estimated_capture_time_ms <= last:
                    self.out_of_order += 1
                    continue
                self._last_capture[cam] = obs.estimated_capture_time_ms
                if len(q) >= self.depth:
                    q.popleft()
                    self.dropped_observations += 1
                q.append(obs)
                self.received += 1

    def _update(self, snap: dict[str, Any]) -> None:
        with self._lock:
            # 빠진 키(file 의 detector_timing 은 1초마다만 온다)는 직전 값을 잇는다. 죽으면 _on_death 가 비운다
            self._snapshot = {**self._snapshot, **snap}
            self._status_at = self._mono_s()
        if self._on_gpu_status is not None and isinstance(snap.get("gpu_reserve"), dict):
            self._on_gpu_status(dict(snap["gpu_reserve"]))
        if self._on_processing is not None and snap.get("processing") is not None:
            values = [v for v in snap["processing"] if v == v]  # nan(검출 예외) 은 실패라 창에 모름으로 넣지 않고 뺀다
            failed = len(snap["processing"]) - len(values)
            self._on_processing(values + [None] * failed)  # type: ignore[list-item]

    def _read_loop(self) -> None:
        """LIVE 받기 스레드: 관측·상태를 받고, 자식 종료·멈춤을 보면 다시 띄운다(상한을 넘으면 fatal)."""
        while not self._stopping.is_set():
            gen = self._gen
            if gen is None:
                return
            try:
                msg = gen.out_q.get(timeout=0.2)
            except queue.Empty:
                msg = None
            except (EOFError, OSError):
                msg = None
            if msg is not None:
                self._handle(msg)
                continue
            if self._stopping.is_set():
                return
            if not gen.process.is_alive():
                self._on_death(gen, f"종료 코드 {gen.process.exitcode}")
            elif self._stale_s() is not None and self._stale_s() > STUCK_S:  # type: ignore[operator]
                log.error("검출 프로세스가 %.0f초 넘게 상태를 보내지 않는다: 내리고 다시 띄운다", STUCK_S)
                self._kill(gen)
                self._on_death(gen, "응답 없음")
            if self.fatal_reason is not None:
                return

    def _on_death(self, gen: _Gen, why: str) -> None:
        self.deaths += 1
        self.last_death = why
        with self._lock:
            self._ipc_dropped_base += int(self._snapshot.get("ipc_dropped") or 0)
            self._snapshot = {}  # 모르면 비움: 카메라 연결 아님, 처리 시간 모름
            self._status_at = None
        if self._on_gpu_status is not None:
            self._on_gpu_status(_pending_reserve(self.spec, "검출 프로세스 재시작 중"))
        self._discard(gen)
        log.error("검출 프로세스가 끝났다(세대 %d, %s): 카메라를 연결 아님으로 본다", gen.generation, why)
        if self.spec.mode == "file":
            self.failed_reason = f"검출 프로세스가 끝났다({why})"
            return
        next_generation = gen.generation + 1
        while not self._stopping.is_set():
            now = self._mono_s()
            self._restart_times.append(now)
            while self._restart_times and now - self._restart_times[0] > self.restart_window_s:
                self._restart_times.popleft()
            if len(self._restart_times) > self.max_restarts:
                self.fatal_reason = (f"검출 프로세스가 {self.restart_window_s:.0f}초 안에 {len(self._restart_times)}번 "
                                     f"끝났다(상한 {self.max_restarts}번, 마지막: {why})")
                log.error("%s: Tier 0 를 종료 코드 4 로 끝낸다(bootstrap 이 다시 띄움)", self.fatal_reason)
                return
            delay = min(self.backoff_max_s, self.backoff_s * 2 ** (len(self._restart_times) - 1))
            if self._stopping.wait(delay):
                return
            try:
                new = self._launch(next_generation)
                try:
                    self._open_video(new, VIDEO_START_TIMEOUT_S)
                except DetectorProcessError:
                    self._discard(new)
                    raise
            except DetectorProcessError as e:
                why = str(e)
                next_generation += 1
                log.error("검출 프로세스 다시 띄우기 실패: %s", why)
                continue
            self._gen = new
            self.restarts += 1
            log.warning("검출 프로세스를 다시 띄웠다(세대 %d, 재시작 %d번째)", new.generation, self.restarts)
            return

    # --- file 입력(lockstep) -------------------------------------------------------

    def send_ordered(self, cmd: tuple) -> None:
        """file: 녹화 메시지 등 순서가 결과에 들어가는 명령(버리지 않고 기다려 넣는다). 넣지 못하면 재생 실패."""
        if self.failed_reason is not None:
            return
        gen = self._require_gen()
        try:
            gen.cmd_q.put((cmd[0], self._now_ms(), *cmd[1:]), timeout=self.rpc_timeout_s)
        except queue.Full:
            self._fail_file(gen, "검출 프로세스가 명령을 받지 않는다")

    def step(self, now_ms: int) -> list[Observation2DFrame]:
        """file: 한 판정 tick 의 검출·로컬 추적을 시키고 관측을 받아 경계 큐에 넣는다."""
        reply = self._rpc("step", now_ms)
        if reply is None:
            return []
        _, _, observations, snap, error = reply
        self._update(snap)
        if error is not None:
            raise RuntimeError(f"검출 프로세스 검출 단계 실패: {error}")
        self._accept(observations)
        return observations

    def close_ingest(self) -> str | None:
        """file: 녹화를 다 넣었다. 디코더를 비우게 하고 결과(오류 문자열 또는 None)를 돌려준다."""
        reply = self._rpc("close_ingest", self._now_ms())
        if reply is None:
            return self.failed_reason or "검출 프로세스가 응답하지 않는다"
        self._update(reply[3])
        return reply[2]

    def _rpc(self, kind: str, now_ms: int) -> tuple | None:
        if self.failed_reason is not None:
            return None
        gen = self._require_gen()
        self._seq += 1
        seq = self._seq
        try:
            gen.cmd_q.put((kind, int(now_ms), seq), timeout=self.rpc_timeout_s)
        except queue.Full:
            self._fail_file(gen, "검출 프로세스가 명령을 받지 않는다")
            return None
        reply = self._await(gen, (kind,), self.rpc_timeout_s, seq=seq)
        if reply is None and not self._stopping.is_set():
            alive = gen.process.is_alive()
            self._fail_file(gen, f"검출 프로세스가 {self.rpc_timeout_s:.0f}초 안에 답하지 않았다" if alive
                            else f"검출 프로세스가 끝났다(종료 코드 {gen.process.exitcode})")
        return reply

    def _fail_file(self, gen: _Gen, why: str) -> None:
        if self.failed_reason is None:
            self.failed_reason = why
            self.deaths += 1
            self.last_death = why
            log.error("녹화 재생: %s", why)
        with self._lock:
            self._snapshot = {}
        self._kill(gen)

    # --- 판정 쪽 조회 -----------------------------------------------------------------

    def send_nowait(self, cmd: tuple) -> bool:
        """LIVE·곁가지 명령(아바타 요청). 차거나 자식이 없으면 버리고 센다."""
        gen = self._gen
        if gen is None or gen.cmd_q is None or self._stopping.is_set():
            self.cmd_dropped += 1
            return False
        try:
            gen.cmd_q.put_nowait(cmd)
            return True
        except (queue.Full, ValueError, OSError):
            self.cmd_dropped += 1
            return False

    def take(self) -> list[Observation2DFrame]:
        """경계 큐에 쌓인 관측을 모두 꺼낸다((촬영 시각, 카메라 순, frame_sequence) 순, DetectorFeed.poll 과 같다)."""
        with self._lock:
            out: list[Observation2DFrame] = []
            for q in self._queues.values():
                out.extend(q)
                q.clear()
        if len(out) > 1:
            out.sort(key=lambda o: (o.estimated_capture_time_ms, self._camera_order.get(o.camera_id, len(self._camera_order)),
                                    o.frame_sequence))
        return out

    def pending(self) -> int:
        with self._lock:
            return sum(len(q) for q in self._queues.values())

    def _stale_s(self) -> float | None:
        """마지막 하트비트 뒤 지난 초(판정 프로세스 단조 시계). 하트비트를 받은 적이 없으면 None."""
        with self._lock:
            at = self._status_at
        return None if at is None else max(0.0, self._mono_s() - at)

    def healthy(self) -> bool:
        """LIVE: 자식이 살아 있고 하트비트가 신선하다. file 은 실패하지 않았으면 True."""
        gen = self._gen
        if gen is None or gen.process is None or not gen.process.is_alive():
            return False
        if self.spec.mode == "file":
            return self.failed_reason is None
        stale = self._stale_s()
        return stale is not None and stale <= STALE_S

    def snapshot(self) -> dict[str, Any]:
        """마지막으로 받은 검출 프로세스 상태. 건강하지 않으면 빈 dict(모르면 비움)."""
        if not self.healthy():
            return {}
        with self._lock:
            return self._snapshot

    def connected(self, camera_id: str) -> bool:
        return bool(self.snapshot().get("connected", {}).get(camera_id, False))

    def in_progress_ms(self) -> float | None:
        """검출 프로세스가 마지막 하트비트 때 진행 중이던 검출의 경과(그 뒤 흐른 시간 포함). 없으면 None."""
        with self._lock:
            value = self._snapshot.get("in_progress_ms")
            at = self._status_at
        if value is None or at is None:
            return None
        return float(value) + max(0.0, self._mono_s() - at) * 1000

    def status(self) -> dict[str, Any]:
        gen = self._gen
        with self._lock:
            snap = self._snapshot
            stale = None if self._status_at is None else round(max(0.0, self._mono_s() - self._status_at), 3)
        return {
            "mode": self.spec.mode, "pid": None if gen is None else gen.pid,
            "generation": None if gen is None else gen.generation,
            "alive": bool(gen is not None and gen.process is not None and gen.process.is_alive()),
            "healthy": self.healthy(), "status_age_s": stale,
            "restarts": self.restarts, "deaths": self.deaths, "last_death": self.last_death,
            "fatal": self.fatal_reason, "failed": self.failed_reason,
            "batches": self.batches, "received": self.received, "pending": self.pending(),
            "dropped_observations": self.dropped_observations, "out_of_order": self.out_of_order,
            "unknown_camera": self.unknown_camera, "cmd_dropped": self.cmd_dropped,
            "ipc_dropped": self._ipc_dropped_base + int(snap.get("ipc_dropped") or 0),
            "torch_in_child": self.torch_in_child,
            "steps": snap.get("steps"),
            "detect_loops": snap.get("loops"), "detect_thread_cpu": snap.get("thread_cpu"),
            "detect_cpus": self.spec.detect_cpus,
        }

    # --- 정지 ---------------------------------------------------------------------

    def stop(self, timeout_s: float = STOP_JOIN_S) -> list[str]:
        """멈춘다(여러 번 불러도 된다). 제한 시간 안에 끝나지 않으면 kill 하고 이름을 돌려준다."""
        first = not self._stopping.is_set()
        self._stopping.set()
        failures: list[str] = []
        gen = self._gen
        if gen is not None and gen.process is not None and first:
            try:
                gen.cmd_q.put_nowait(("stop",))
            except (queue.Full, ValueError, OSError):
                pass
            gen.process.join(max(0.1, timeout_s))
            if gen.process.is_alive():
                failures.append("tier0-detect-process")  # service._shutdown 이 "thread:" 를 붙인다
                log.error("검출 프로세스가 %.1f초 안에 끝나지 않아 내린다", timeout_s)
            self._discard(gen)
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(2.0)
        _LIVE_PROCESSES.discard(self)
        return failures

    def _kill(self, gen: _Gen) -> None:
        if gen.process is not None and gen.process.is_alive():
            gen.process.kill()
            gen.process.join(2.0)

    def _kill_now(self) -> None:
        self._stopping.set()
        gen = self._gen
        if gen is not None:
            self._kill(gen)

    def _discard(self, gen: _Gen) -> None:
        """그 세대의 자식을 내리고 큐를 닫는다(자식이 남긴 데이터 때문에 종료가 막히지 않게)."""
        self._kill(gen)
        for q in (gen.cmd_q, gen.out_q):
            if q is None:
                continue
            try:
                q.cancel_join_thread()
                q.close()
            except (ValueError, OSError):
                pass


def _pending_reserve(spec: DetectorProcessSpec, reason: str) -> dict[str, Any]:
    from simai_ai.gpu.reserve import PENDING

    return {"state": PENDING, "target_mb": spec.gpu_reserve_mb, "reserved_mb": None, "device": spec.gpu_device,
            "reason": reason}


# ---------------------------------------------------------------------------- 판정 쪽 대리 객체


class SplitDetectorFeed:
    """판정 루프의 입력(FrameFeed). 관측은 검출 프로세스에서 온다. LIVE 는 받기 스레드가 쌓은 경계 큐를 꺼내고, file 은 lockstep."""

    reid_collector = None  # 1차 MR: Re-ID 는 분리 경로에서 끈다(설계 5절)

    def __init__(self, process: DetectorProcess, *, connected: Callable[[str], bool], source: str, async_mode: bool,
                 provenance: Mapping[str, Any] | None = None) -> None:
        self.process = process
        self._connected = connected
        self._source = source
        self.async_mode = async_mode
        self._provenance = None if provenance is None else dict(provenance)
        self.avatar_frames: dict[str, Any] = {}  # 그림은 검출 프로세스에 있다
        self.avatar_observations: dict[str, Any] = {}
        self.observe_decoded = None
        self.timing = SimpleNamespace(summary=lambda: process.snapshot().get("detector_timing") or {})

    @property
    def source(self) -> str:
        return self._source

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return self.process.camera_ids

    @property
    def stats(self) -> SimpleNamespace:
        """DetectorFeedStats 와 같은 키(검출 프로세스 값) + 경계 큐 계수. 모르면 빈 값."""
        child = dict(self.process.snapshot().get("detector_stats") or {})
        p = self.process
        child["dropped_observations"] = int(child.get("dropped_observations", 0)) + p.dropped_observations
        child.update(out_of_order=p.out_of_order, ipc_received=p.received)
        return SimpleNamespace(**child)

    @property
    def camera_streams(self) -> dict[str, CameraObservation]:
        streams = self.process.snapshot().get("camera_streams") or {}
        return {c: CameraObservation(c, image_size=(int(s[0]), int(s[1]))) for c, s in streams.items()}

    @property
    def failed(self) -> str | None:
        return self.process.failed_reason

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        if not self.async_mode:
            self.process.step(now_ms)
        return self.process.take()

    def pending_observations(self) -> int:
        return self.process.pending()

    def drained(self) -> bool:
        return self.process.pending() == 0

    def camera_connected(self, camera_id: str) -> bool:
        return self._connected(camera_id)

    def provenance(self) -> dict[str, Any]:
        if self._provenance is not None:
            return dict(self._provenance)
        return {"source": self._source, "camera_ids": list(self.camera_ids)}

    def close(self) -> None:
        self.process.stop()


class _RemoteLog:
    """I0Ingest.log.counts 대역(검출 프로세스 보고값). 모르면 빈 집계."""

    def __init__(self, process: DetectorProcess) -> None:
        self._process = process

    def counts(self, camera_id: str) -> dict[str, Any]:
        counts = (self._process.snapshot().get("counts") or {}).get(camera_id)
        if counts is not None:
            return counts
        return {"received": 0, "sequenced": 0, "decoded": 0, "inferred": 0, "network_loss": 0, "decode_failed": 0,
                "skipped_by_reason": {}, "frame_age_ms_p95": None, "queue_wait_ms_max": None}


class _RemoteBridge:
    def __init__(self, process: DetectorProcess) -> None:
        self.timing = SimpleNamespace(summary=lambda: process.snapshot().get("input_timing") or {})
        self._warned = False

    def attach_clips(self, _receive: Callable[[Any], None]) -> None:
        if not self._warned:
            self._warned = True
            log.warning("프로세스 분리 1단계: 이벤트 클립 링 버퍼는 검출 프로세스로 옮기지 않아 클립이 없다(설계 9절)")


class RemoteLiveInput:
    """live_input.LiveInput 대역(판정 프로세스 쪽). health·status·연결은 검출 프로세스 보고값으로 만든다."""

    def __init__(self, process: DetectorProcess) -> None:
        self._process = process
        self.camera_ids = process.camera_ids
        self.ingest = SimpleNamespace(log=_RemoteLog(process))
        self.bridge = _RemoteBridge(process)
        self._report_lock = threading.Lock()
        self._reported: dict[str, dict[str, dict[str, int]]] = {}
        self.started = False

    def start(self) -> None:
        try:
            self._process.start_streaming()
        except DetectorProcessError as e:
            raise OSError(str(e)) from e
        self.started = True

    def stop(self, timeout_s: float = 0.0) -> list[str]:
        self.started = False
        return self._process.stop(max(0.1, timeout_s))

    def connected(self, camera_id: str) -> bool:
        return self._process.connected(camera_id)

    def waiting_keyframe(self, camera_id: str) -> bool:
        return bool(self._process.snapshot().get("waiting_keyframe", {}).get(camera_id, False))

    def frame_totals(self) -> dict[str, dict[str, int]]:
        from services.tier0.live_input import FRAME_FIELDS

        totals = self._process.snapshot().get("frame_totals") or {}
        return {c: dict(totals.get(c) or dict.fromkeys(FRAME_FIELDS, 0)) for c in self.camera_ids}

    def take_frames(self, consumer: str) -> dict[str, dict[str, int]]:
        """LiveInput.take_frames 와 같다. 검출 프로세스가 다시 뜨면 누계가 0 부터라 음수 증가분은 0 으로 둔다."""
        totals = self.frame_totals()
        with self._report_lock:
            prev = self._reported.get(consumer, {})
            self._reported[consumer] = totals
        return {cam: {k: max(0, v - prev.get(cam, {}).get(k, 0)) for k, v in t.items()} for cam, t in totals.items()}

    def merge_health(self, payload: dict[str, Any] | None, consumer: str) -> dict[str, Any] | None:
        """LiveInput.merge_health 와 같은 규칙."""
        if payload is None:
            return None
        frames = self.take_frames(consumer)
        epochs = self._process.snapshot().get("stream_epoch") or {}
        for entry in payload.get("cameras", ()):
            cam = entry.get("id")
            if cam not in frames:
                continue
            entry["frames"] = frames[cam]
            epoch = epochs.get(cam)
            if epoch is not None:
                entry["stream_epoch"] = epoch
            if entry.get("status") != "SYNC_INVALID" and self.waiting_keyframe(cam):
                entry["status"] = "WAITING_KEYFRAME"
        return payload

    def status(self) -> dict[str, Any]:
        live = self._process.snapshot().get("live_status") or {"started": False, "cameras": {}}
        return {**live, "started": self.started and bool(live.get("started")), "process": self._process.status()}


class RemoteIngest:
    """file 입력의 FileInput 이 쓰는 I0Ingest 대역. 메시지는 검출 프로세스로 보내고, 상태는 마지막 응답 값이다."""

    def __init__(self, process: DetectorProcess) -> None:
        self._process = process
        self.camera_ids = process.camera_ids
        self.log = _RemoteLog(process)
        self.scheduler = SimpleNamespace(pending=lambda c: int((self._snap().get("pending") or {}).get(c, 0)))

    def _snap(self) -> dict[str, Any]:
        with self._process._lock:
            return self._process._snapshot

    def receive(self, message: Any) -> None:
        self._process.send_ordered(("ingest", message))

    def connection_lost(self, camera_id: str) -> None:
        self._process.send_ordered(("lost", camera_id))

    def close(self) -> None:
        error = self._process.close_ingest()
        if error is not None:
            raise RuntimeError(error)

    def camera(self, camera_id: str) -> SimpleNamespace:
        return SimpleNamespace(ready=bool((self._snap().get("ready") or {}).get(camera_id, False)))


class RemoteAvatarWriter:
    """판정 쪽 아바타 공유 작성기 대역. 판정 tick 의 재료만 pickle 해 보내고, 조립·쓰기는 그림이 있는 검출 프로세스가 한다."""

    def __init__(self, process: DetectorProcess, *, wall_ms: Callable[[], int], period_s: float | None = None) -> None:
        from simai_ai.io.avatar_channel import DEFAULT_PERIOD_S

        self._process = process
        self._wall_ms = wall_ms
        self.period_s = DEFAULT_PERIOD_S if period_s is None else period_s
        self.sent = 0
        self.send_dropped = 0

    def offer_tick(self, frame: Any, payload: Mapping[str, Any], t_latest: int, rig: Any,
                   calibration: Mapping[str, Any]) -> bool:
        request = {
            "t": t_latest,
            "tracks": [(tuple(tuple(k) for k in snap.local_keys), bool(getattr(snap, "avatar_update_hold", False)))
                       for snap in frame.tracks],
            "payload": payload, "calibration_version": rig.calibration_version, "calibration": calibration,
        }
        # 판정 스레드에서 바로 직렬화한다(큐 feeder 가 나중에 읽는 동안 dict 가 바뀌지 않게)
        ok = self._process.send_nowait(("avatar", self._wall_ms(), pickle.dumps(request, protocol=pickle.HIGHEST_PROTOCOL)))
        if ok:
            self.sent += 1
        else:
            self.send_dropped += 1
        return ok

    def _child(self) -> dict[str, Any]:
        return self._process.snapshot().get("avatar") or {}

    @property
    def dropped(self) -> int:
        return int(self._child().get("dropped", 0)) + self.send_dropped

    @property
    def errors(self) -> int:
        return int(self._child().get("errors", 0))

    def status(self) -> dict[str, Any]:
        return {"mode": "detect_process", "sent": self.sent, "send_dropped": self.send_dropped, "child": self._child()}

    def close(self) -> None:
        return None  # 작성기는 검출 프로세스가 닫는다


class RemoteGpuReservation:
    """GPU 선확보 상태 대역: 선확보는 CUDA 를 쓰는 검출 프로세스가 하고, 판정 프로세스는 보고값을 표시 파일에 싣는다."""

    def __init__(self, target_mb: int, *, device: int = 0) -> None:
        from simai_ai.gpu.reserve import DISABLED, PENDING

        self._lock = threading.Lock()
        state, reason = (DISABLED, "target_mb=0") if target_mb == 0 else (PENDING, "검출 프로세스가 확보한다")
        self._status: dict[str, Any] = {"state": state, "target_mb": int(target_mb), "reserved_mb": None,
                                        "device": int(device), "reason": reason}

    def acquire(self) -> None:
        log.info("프로세스 분리: GPU 메모리 선확보는 검출 프로세스가 한다(목표 %d MB)", self._status["target_mb"])

    def update(self, status: Mapping[str, Any]) -> None:
        with self._lock:
            self._status = dict(status)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)


class RemoteProcessingWindow:
    """표시 파일 processing.detect 창(ProcessingWindow 와 같은 형식). 표본은 검출 프로세스가 잰 처리 시간이다.

    take 때 검출 프로세스가 건강하지 않으면(하트비트 끊김·종료) 모름을 넣어 아바타 관문이 멈추게 한다(모르면 멈춤). 진행 중이던 검출이
    있으면 그 경과를 넣는다(ProcessingWindow 와 같이 멈춘 작업을 정상으로 보지 않는다)."""

    def __init__(self, budget_ms: int) -> None:
        self._window = LoadWindow(budget_ms, max_values=PROCESSING_MAX_VALUES)
        self._lock = threading.Lock()
        self.process: DetectorProcess | None = None

    def add_samples(self, values: Sequence[float | None]) -> None:
        with self._lock:
            for v in values:
                self._window.add(v)

    def take(self) -> dict[str, Any]:
        with self._lock:
            p = self.process
            if p is not None and p.spec.mode == "live":
                if not p.healthy():
                    self._window.add(None)
                else:
                    running = p.in_progress_ms()
                    if running is not None:
                        self._window.add(running)
            return self._window.take()
