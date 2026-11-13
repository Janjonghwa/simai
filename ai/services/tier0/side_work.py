"""Tier 0 곁가지 일 프로세스 분리 (S15P31A101-662): 아바타 입력 공유 쓰기. 설정 `offload_side_work`(기본 끔)일 때만 쓴다.

판정에 끼지 않는 일만 옮긴다. 판정 중심(검출 결과 → cross_view → world_track → 판정 → world_state)과 I1 송신 순서·msg_id
(= 그 tick 전이의 evidence_ref)는 그대로 Tier 0 프로세스에 있다. 꺼져 있으면 지금 경로(`LatestChannelWriter`) 그대로다.

나눈 일(`OffloadedChannelWriter`):
- 판정 스레드(`Perception._share_avatar`): 그 tick 의 참조(World 프레임·payload·카메라별 그림·관측·rig·보정 문서)만 잡아
  `offer_deferred` 로 넘긴다. 잠금은 기다리지 않는다(`acquire(blocking=False)`, 바쁘면 버림·계수). 최신 한 건만 둔다.
- 조정 스레드(부모, 판정 lock 밖): 문서 조립(`perception.avatar_metadata`, 꺼짐 경로와 같은 순수 함수)과 크롭 상자·크롭
  복사(`LatestChannelWriter._prepare`)를 하고, 크롭 바이트를 고정 `shared_memory` 칸에 복사한 뒤 작은 메타만 유계 큐에
  `put_nowait` 한다(차면 버림·계수). 크롭은 사람 주변만이라 4대 합계 1~3 MB 다(-650). 원본 프레임은 넘기지 않는다.
- 자식(spawn, `_avatar_writer_main` 최상위 순수 함수): 메타 JSON·`np.savez`·`os.replace`·완료 표식(`LatestChannelWriter._publish`,
  꺼짐 경로와 같은 함수·같은 파일 형식). 공유 채널의 작성자는 자식 하나다(부모는 쓰지 않는다). numpy·avatar_channel 만 불러온다
  (torch·cv2 를 불러오지 않는다: CUDA 문맥을 만들지 않는다. 첫 응답에 torch 적재 여부를 싣고 status 에 남긴다).

시각·순서: 넘기는 문서는 판정 tick 의 `world.t`(t_latest_global)·카메라별 `capture_time_ms`·`frame_ref`(stream_epoch·
frame_sequence)·offer 시각 `produced_at_ms` 를 그대로 싣는다(조립을 늦게 해도 잡아 둔 참조로 만든다). 자식은 `world.t` 가 이미 쓴
것보다 이르면 쓰지 않는다(out_of_order 계수). 그래서 보정 버전이 바뀌는 순간에도 한 문서 안의 calibration_version·보정 문서·
크롭은 같은 tick 의 rig 에서 나온다.

/dev/shm: 칸은 2개, 칸 하나 상한 16 MiB(모자라면 그 건만 버림·oversize). 만들 때 남은 공간 + 여유(4 MiB)를 먼저 보고 Linux 에서는
`posix_fallocate` 로 미리 잡는다(tmpfs 가 차면 쓰는 순간 SIGBUS 로 Tier 0 전체가 죽는 대신 ENOSPC 로 그 건만 버린다, -563 과 같은 함정).

장애: 자식이 죽었거나(종료 코드) 한 칸이 5초 넘게 끝나지 않으면(멈춤) 자식을 내리고 칸·큐를 새로 만들어 다시 띄운다(1초 간격).
판정은 멈추지 않는다. 다시 띄우기가 5번을 넘으면 그 뒤로는 꺼짐 경로(`LatestChannelWriter`, 부모 안 스레드)로 넘어간다(fallback,
자식은 먼저 내린다: 작성자 하나). status() 에 상태·재시작 수·계수를 싣고 Tier 0 status()["avatar_share"]["offload"] 로 나간다.
"""

from __future__ import annotations

import atexit
import logging
import math
import multiprocessing
import os
import queue
import shutil
import threading
import time
import weakref
from collections.abc import Callable, Mapping
from multiprocessing.shared_memory import SharedMemory
from pathlib import Path
from typing import Any

import numpy as np

from simai_ai.io.avatar_channel import DEFAULT_PERIOD_S, LatestChannelWriter

log = logging.getLogger("services.tier0.side_work")

SLOT_COUNT = 2
SLOT_MAX_BYTES = 16 * 1024 * 1024
"""칸 하나 상한. 4대 사람 크롭은 보통 1~3 MB(-650 실측 목표). 넘으면 그 건만 버린다(oversize)"""
SLOT_ROUND_BYTES = 1024 * 1024
SHM_HEADROOM_BYTES = 4 * 1024 * 1024
"""칸을 만들 때 /dev/shm 에 남겨 둘 여유(공유 채널 NPZ·임시 파일 몫, -563 과 같은 값)"""
STUCK_S = 5.0
"""한 칸이 이만큼 넘게 자식에게서 돌아오지 않으면 멈춘 것으로 보고 다시 띄운다"""
RESTART_INTERVAL_S = 1.0
MAX_RESTARTS = 5
"""다시 띄우기 상한. 넘으면 부모 안 쓰기(꺼짐 경로)로 넘어간다"""
CHILD_POLL_S = 1.0
_SHM_DIR = Path("/dev/shm")

PROCESS = "process"
FALLBACK = "fallback"
CLOSED = "closed"


def _shm_free_bytes() -> int | None:
    """Linux tmpfs 남은 바이트. /dev/shm 이 없는 OS(Windows·macOS)는 None(검사 생략)."""
    if not _SHM_DIR.is_dir():
        return None
    try:
        return int(shutil.disk_usage(_SHM_DIR).free)
    except OSError:
        return None


# ---------------------------------------------------------------------------- 자식 프로세스(최상위 순수 함수)


def _avatar_writer_main(path: str, cmd_queue: Any, done_queue: Any) -> None:
    """spawn 자식 진입점: 크롭 배열(공유 메모리)과 메타를 받아 공유 채널 NPZ 를 게시한다. numpy·avatar_channel 만 쓴다."""
    import sys

    if hasattr(os, "nice"):
        try:
            os.nice(10)  # 곁가지 일: Tier 0 판정·검출 스레드보다 뒤
        except OSError:
            pass
    writer = LatestChannelWriter(Path(path), start=False)
    shms: dict[str, SharedMemory] = {}
    last_t: Any = None
    parent = multiprocessing.parent_process()
    done_queue.put(("ready", os.getpid(), "torch" in sys.modules))

    def attach(name: str) -> SharedMemory:
        shm = shms.get(name)
        if shm is None:
            shm = SharedMemory(name=name, create=False)
            if os.name != "nt":
                try:  # 부모가 만들고 지운다. 자식이 끝날 때 자원 추적기가 지우지 않게(-563 과 같음)
                    from multiprocessing import resource_tracker

                    resource_tracker.unregister(shm._name, "shared_memory")  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
            shms[name] = shm
        return shm

    try:
        while True:
            try:
                item = cmd_queue.get(timeout=CHILD_POLL_S)
            except queue.Empty:
                if parent is not None and not parent.is_alive():
                    return  # 부모가 죽었다(SIGKILL 등): 고아로 남지 않는다
                continue
            if item is None:
                return
            if item[0] == "forget":  # 부모가 칸을 새로 만들었다: 옛 이름을 닫는다
                old = shms.pop(item[1], None)
                if old is not None:
                    old.close()
                continue
            _, seq, name, layout, metadata, refs, total = item
            arrays: dict[str, np.ndarray] = {}
            try:
                world = metadata.get("world") if isinstance(metadata, Mapping) else None
                t = world.get("t") if isinstance(world, Mapping) else None
                if isinstance(t, int) and isinstance(last_t, int) and t < last_t:
                    done_queue.put(("done", seq, "out_of_order", None))
                    continue
                for key, offset, shape, dtype in layout:
                    if math.prod(shape) == 0:  # 크롭이 없는 카메라(빈 배열): 공유 메모리를 보지 않는다
                        arrays[key] = np.empty(tuple(shape), dtype=np.dtype(dtype))
                    else:
                        arrays[key] = np.ndarray(tuple(shape), dtype=np.dtype(dtype), buffer=attach(name).buf,
                                                 offset=offset)
                writer._publish(metadata, refs, arrays, total)
                if isinstance(t, int):
                    last_t = t
                done_queue.put(("done", seq, "ok", writer.last_written_bytes))
            except Exception as e:  # noqa: BLE001 - 한 건 실패로 자식을 끝내지 않는다(부모가 계수)
                done_queue.put(("done", seq, "error", f"{type(e).__name__}: {e}"))
            finally:
                arrays.clear()  # 공유 메모리 버퍼를 가리키는 배열을 닫기 전에 놓는다
    finally:
        for shm in shms.values():
            try:
                shm.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------- 부모 쪽


class _Slot:
    __slots__ = ("busy_seq", "busy_since", "shm")

    def __init__(self) -> None:
        self.shm: SharedMemory | None = None
        self.busy_seq: int | None = None
        self.busy_since = 0.0


_LIVE: weakref.WeakSet[OffloadedChannelWriter] = weakref.WeakSet()


def _atexit_close() -> None:
    for writer in list(_LIVE):
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_atexit_close)


class OffloadedChannelWriter:
    """LatestChannelWriter 와 같은 자리(Perception.avatar_writer)에 끼운다. offer·offer_deferred 는 기다리지 않는다."""

    def __init__(self, path: Path, *, period_s: float = DEFAULT_PERIOD_S,
                 clock_ms: Callable[[], int] | None = None,
                 slot_max_bytes: int = SLOT_MAX_BYTES, stuck_s: float = STUCK_S,
                 restart_interval_s: float = RESTART_INTERVAL_S, max_restarts: int = MAX_RESTARTS) -> None:
        self.path = Path(path)
        self.period_s = period_s
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._slot_max = slot_max_bytes
        self._stuck_s = stuck_s
        self._restart_interval_s = restart_interval_s
        self._max_restarts = max_restarts
        self._prep = LatestChannelWriter(self.path, start=False)  # 크롭 준비만(부모는 파일을 쓰지 않는다)
        self.timing = self._prep.timing
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = threading.Lock()
        self._pending: tuple[Callable[[], Mapping[str, Any]], Mapping[str, Any], int] | None = None
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._process: Any = None
        self._cmd: Any = None
        self._done: Any = None
        self._slots = [_Slot() for _ in range(SLOT_COUNT)]
        self._seq = 0
        self._last_restart = -math.inf
        self._fallback: LatestChannelWriter | None = None
        self.mode = PROCESS
        self.pid: int | None = None
        self.torch_in_child: bool | None = None
        self.restarts = 0
        self.offered = self.sent = self.written = 0
        self.dropped = self.errors = 0
        self.oversize = self.shm_full = self.out_of_order = self.queue_full = 0
        self.last_written_bytes = 0
        self.last_error: str | None = None
        self._start_child()
        self._thread = threading.Thread(target=self._run, name="tier0-side-work", daemon=True)
        self._thread.start()
        _LIVE.add(self)

    # --- 넣기(판정 스레드) -------------------------------------------------------------

    def offer(self, metadata: Mapping[str, Any], frames: Mapping[str, Any]) -> bool:
        """이미 조립한 문서를 넣는다(LatestChannelWriter.offer 와 같은 뜻)."""
        return self.offer_deferred(lambda: metadata, frames)

    def offer_deferred(self, build: Callable[[], Mapping[str, Any]], frames: Mapping[str, Any]) -> bool:
        """문서 조립 함수와 그 tick 의 그림 참조를 넣는다. 잠금이 바쁘면 False(버림). produced_at_ms 는 지금 시각이다."""
        fallback = self._fallback
        if fallback is not None:
            return fallback.offer(build(), frames)
        if not self._lock.acquire(blocking=False):
            self.dropped += 1
            return False
        try:
            self.dropped += int(self._pending is not None)
            self._pending = (build, frames, int(self._clock_ms()))
            self.offered += 1
        finally:
            self._lock.release()
        self._wake.set()
        return True

    # --- 조정 스레드 -------------------------------------------------------------------

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.wait(0.2)
                self._wake.clear()
                if self._stop.is_set():
                    break
                self._drain_done()
                if self.mode == PROCESS and not self._child_ok():
                    self._restart()
                    continue
                if self.mode != PROCESS:
                    continue
                with self._lock:
                    item, self._pending = self._pending, None
                if item is None:
                    continue
                started = time.monotonic()
                self._send(*item)
                remaining = self.period_s - (time.monotonic() - started)
                if remaining > 0:
                    self._stop.wait(remaining)  # 최대 5 Hz, 사이에 들어온 것은 최신 한 건으로 합친다(꺼짐 경로와 같다)
        except Exception:  # noqa: BLE001 - 곁가지 실패가 Tier 0 를 멈추지 않는다
            log.exception("곁가지 조정 스레드 실패(판정은 계속한다)")
        finally:
            self._cleanup_child()

    def _drain_done(self) -> None:
        done = self._done
        if done is None:
            return
        while True:
            try:
                msg = done.get_nowait()
            except (queue.Empty, OSError, ValueError, EOFError):
                return
            if msg[0] == "ready":
                self.pid, self.torch_in_child = msg[1], bool(msg[2])
                continue
            _, seq, outcome, detail = msg
            for slot in self._slots:
                if slot.busy_seq == seq:
                    slot.busy_seq = None
            if outcome == "ok":
                self.written += 1
                self.last_written_bytes = int(detail or 0)
            elif outcome == "out_of_order":
                self.out_of_order += 1
            else:
                self.errors += 1
                self.last_error = str(detail)

    def _child_ok(self) -> bool:
        proc = self._process
        if proc is None or not proc.is_alive():
            reason = "없음" if proc is None else f"종료 코드 {proc.exitcode}"
            self.last_error = f"곁가지 자식 프로세스 {reason}"
            return False
        now = time.monotonic()
        if any(s.busy_seq is not None and now - s.busy_since > self._stuck_s for s in self._slots):
            self.last_error = f"곁가지 자식 프로세스 멈춤({self._stuck_s:.0f}초 넘게 응답 없음)"
            return False
        return True

    def _restart(self) -> None:
        now = time.monotonic()
        if now - self._last_restart < self._restart_interval_s:
            return
        self._last_restart = now
        self._cleanup_child()
        if self.restarts >= self._max_restarts:
            log.error("곁가지 자식 프로세스를 %d번 다시 띄웠는데 또 멈췄다(%s): 부모 안 쓰기로 넘어간다(판정은 계속한다)",
                      self.restarts, self.last_error)
            self.mode = FALLBACK
            self._fallback = LatestChannelWriter(self.path, period_s=self.period_s, clock_ms=self._clock_ms)
            return
        self.restarts += 1
        log.warning("곁가지 자식 프로세스 다시 띄움 %d번째 (%s, 판정은 계속한다)", self.restarts, self.last_error)
        try:
            self._start_child()
        except Exception as e:  # noqa: BLE001 - 다음 주기에 다시 본다
            self.last_error = f"곁가지 자식 프로세스 띄우기 실패: {type(e).__name__}: {e}"
            log.warning("%s", self.last_error)

    def _start_child(self) -> None:
        self._cmd = self._ctx.Queue(maxsize=SLOT_COUNT * 2)
        self._done = self._ctx.Queue()
        self.pid = None
        self.torch_in_child = None
        self._process = self._ctx.Process(target=_avatar_writer_main, args=(str(self.path), self._cmd, self._done),
                                          name="tier0-side-work-child", daemon=True)
        self._process.start()
        self.pid = self._process.pid

    def _cleanup_child(self) -> None:
        proc, cmd, done = self._process, self._cmd, self._done
        self._process = self._cmd = self._done = None
        if proc is not None:
            if proc.is_alive() and cmd is not None:
                try:
                    cmd.put_nowait(None)
                except Exception:  # noqa: BLE001
                    pass
                proc.join(0.5)
            if proc.is_alive():
                proc.terminate()
                proc.join(0.5)
            if proc.is_alive():
                proc.kill()
                proc.join(0.5)
        for q in (cmd, done):
            if q is not None:
                try:
                    q.cancel_join_thread()
                    q.close()
                except Exception:  # noqa: BLE001
                    pass
        for slot in self._slots:  # 죽은 자식이 쥐던 칸·큐는 다시 쓰지 않는다
            self._free_shm(slot)
            slot.busy_seq = None

    @staticmethod
    def _free_shm(slot: _Slot) -> None:
        shm, slot.shm = slot.shm, None
        if shm is not None:
            OffloadedChannelWriter._release(shm)

    @staticmethod
    def _release(shm: SharedMemory) -> None:
        try:
            shm.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            shm.unlink()
        except Exception:  # noqa: BLE001
            pass

    def _ensure_capacity(self, slot: _Slot, need: int) -> bool:
        if slot.shm is not None and slot.shm.size >= need:
            return True
        size = max(SLOT_ROUND_BYTES, -(-need // SLOT_ROUND_BYTES) * SLOT_ROUND_BYTES)
        free = _shm_free_bytes()
        if free is not None and free - size < SHM_HEADROOM_BYTES:
            self.shm_full += 1
            return False
        old = slot.shm
        try:
            shm = SharedMemory(create=True, size=size)
        except OSError as e:
            self.shm_full += 1
            self.last_error = f"공유 메모리 칸을 만들지 못했다: {e}"
            return False
        fd = getattr(shm, "_fd", -1)
        if hasattr(os, "posix_fallocate") and fd >= 0:
            try:
                os.posix_fallocate(fd, 0, size)  # tmpfs 가 차면 SIGBUS 대신 여기서 ENOSPC
            except OSError as e:
                self.shm_full += 1
                self.last_error = f"공유 메모리 칸을 잡지 못했다: {e}"
                shm.close()
                shm.unlink()
                return False
        slot.shm = shm
        if old is not None:
            if self._cmd is not None:
                try:
                    self._cmd.put_nowait(("forget", old.name))
                except Exception:  # noqa: BLE001
                    pass
            self._release(old)
        return True

    def _send(self, build: Callable[[], Mapping[str, Any]], frames: Mapping[str, Any], produced_at_ms: int) -> None:
        try:
            metadata = {**build(), "produced_at_ms": produced_at_ms}
            refs, arrays, total = self._prep._prepare(metadata, frames)
        except Exception as e:  # noqa: BLE001 - 조립 실패는 이 건만 버린다
            self.errors += 1
            self.last_error = f"아바타 입력 조립 실패: {type(e).__name__}: {e}"
            return
        need = sum(int(a.nbytes) for a in arrays.values())
        if need > self._slot_max:
            self.oversize += 1
            return
        slot = next((s for s in self._slots if s.busy_seq is None), None)
        if slot is None:
            self.dropped += 1  # 자식이 아직 앞의 두 건을 쓰는 중
            return
        if need and not self._ensure_capacity(slot, need):
            return
        layout = []
        offset = 0
        if need:
            assert slot.shm is not None
            buf = np.ndarray((slot.shm.size,), dtype=np.uint8, buffer=slot.shm.buf)
            for key, arr in arrays.items():
                flat = np.ascontiguousarray(arr).reshape(-1).view(np.uint8)
                buf[offset:offset + flat.nbytes] = flat  # numpy 복사는 GIL 을 놓는다(크롭 1~3 MB)
                layout.append((key, offset, list(arr.shape), arr.dtype.str))
                offset += flat.nbytes
            del buf
        else:
            layout = [(key, 0, list(arr.shape), arr.dtype.str) for key, arr in arrays.items()]
        self._seq += 1
        name = slot.shm.name if slot.shm is not None else ""
        try:
            self._cmd.put_nowait(("write", self._seq, name, layout, metadata, refs, total))
        except (queue.Full, AttributeError, ValueError):
            self.queue_full += 1
            self.dropped += 1
            return
        slot.busy_seq, slot.busy_since = self._seq, time.monotonic()
        self.sent += 1

    # --- 상태·종료 ---------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        proc = self._process
        return {
            "mode": self.mode, "alive": bool(proc is not None and proc.is_alive()), "pid": self.pid,
            "torch_in_child": self.torch_in_child, "restarts": self.restarts,
            "offered": self.offered, "sent": self.sent, "written": self.written, "dropped": self.dropped,
            "errors": self.errors, "oversize": self.oversize, "shm_full": self.shm_full,
            "out_of_order": self.out_of_order, "queue_full": self.queue_full,
            "last_written_bytes": self.last_written_bytes, "last_error": self.last_error,
        }

    def close(self) -> None:
        if self.mode == CLOSED:
            return
        self._stop.set()
        self._wake.set()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(2.0)
        if self._thread.is_alive():
            self._cleanup_child()  # 조정 스레드가 끝나지 않았어도 자식·공유 메모리는 정리한다
        if self._fallback is not None:
            self._fallback.close()
        self.mode = CLOSED
        _LIVE.discard(self)

