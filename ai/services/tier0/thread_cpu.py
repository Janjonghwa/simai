"""Tier 0 스레드별 CPU 표본 (S15P31A101-662). 판정에 쓰지 않는다.

Linux `/proc/self/task/<tid>/stat` 의 utime+stime 을 두 표본 사이로 나눠 스레드별 CPU% 를 잰다(이름은 threading 이름,
모르는 C 스레드는 `native:<comm>`, 끝 번호를 뗀 묶음). Python 스레드의 CPU 는 GIL 을 놓는 C 구간(디코딩·TensorRT)을 빼면 GIL
점유와 거의 같아 "누가 GIL 을 먹는가"의 근거가 된다. 프로세스 전체 CPU% 는 os.times 로 모든 OS 에서 잰다. 자식 프로세스(배경
누적·회전 복구·곁가지)는 들지 않는다. 정리 주기가 기존 30초 `[tier0] stage_timing` 줄의 `thread_cpu` 에 싣는다.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

MAX_THREAD_ROWS = 32
"""스레드 CPU 표에 싣는 최대 행 수(CPU 큰 순). 로그 한 줄이 커지지 않게"""
_TRAILING_NUMBER = re.compile(r"[_-]\d+$")


def _process_cpu_s() -> float:
    t = os.times()
    return t.user + t.system


def _thread_group(name: str) -> str:
    """같은 일을 하는 스레드 묶음 이름(끝 번호를 뗀다: tier0-event-s3_0 → tier0-event-s3, i0-video-conn-7001 → i0-video-conn)."""
    return _TRAILING_NUMBER.sub("", name)


class ThreadCpuSampler:
    """두 표본 사이의 프로세스·스레드별 CPU%. sample() 첫 호출은 기준만 잡고 None 을 돌려준다.

    task_dir: Linux /proc/self/task. 없으면(Windows·macOS) threads 는 None(모름)이다. 시험은 가짜 디렉터리와 시계를 넣는다.
    """

    def __init__(
        self,
        *,
        task_dir: Path = Path("/proc/self/task"),
        clock: Callable[[], float] = time.monotonic,
        process_cpu_s: Callable[[], float] | None = None,
        threads: Callable[[], Iterable[threading.Thread]] = threading.enumerate,
        ticks_per_s: float | None = None,
    ) -> None:
        self._task_dir = task_dir
        self._clock = clock
        self._process_cpu_s = process_cpu_s or _process_cpu_s
        self._threads = threads
        if ticks_per_s is None:
            try:
                ticks_per_s = float(os.sysconf("SC_CLK_TCK"))  # type: ignore[attr-defined]
            except (AttributeError, ValueError, OSError):
                ticks_per_s = 100.0
        self._ticks_per_s = ticks_per_s
        self._prev: tuple[float, float, dict[int, tuple[str, int]] | None] | None = None

    def _read_tasks(self) -> dict[int, tuple[str, int]] | None:
        try:
            entries = list(self._task_dir.iterdir())
        except OSError:
            return None
        out: dict[int, tuple[str, int]] = {}
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue  # 그 사이 끝난 스레드
            head, sep, rest = raw.rpartition(")")
            if not sep:
                continue
            comm = head.partition("(")[2]
            fields = rest.split()
            # rest[0] 은 3번째 필드(state). utime·stime 은 14·15번째 필드 → rest[11]·rest[12]
            try:
                out[int(entry.name)] = (comm, int(fields[11]) + int(fields[12]))
            except (IndexError, ValueError):
                continue
        return out

    def sample(self) -> dict[str, Any] | None:
        now = self._clock()
        try:
            cpu = float(self._process_cpu_s())
        except Exception:  # noqa: BLE001
            cpu = float("nan")
        tasks = self._read_tasks()
        prev, self._prev = self._prev, (now, cpu, tasks)
        if prev is None:
            return None
        elapsed = now - prev[0]
        if elapsed <= 0:
            return None
        out: dict[str, Any] = {"window_s": round(elapsed, 1)}
        process_pct = (cpu - prev[1]) / elapsed * 100
        out["process_pct"] = None if math.isnan(process_pct) else round(process_pct, 1)  # 못 읽었으면 모름
        if tasks is None or prev[2] is None:
            out["threads"] = None
            return out
        names = {}
        for thread in self._threads():
            native = getattr(thread, "native_id", None)
            if native is not None:
                names[native] = thread.name
        grouped: dict[str, float] = {}
        for tid, (comm, ticks) in tasks.items():
            before = prev[2].get(tid)
            delta = ticks - (before[1] if before is not None else 0)  # 창 안에 생긴 스레드는 그동안 쓴 전부
            if delta <= 0:
                continue
            name = names.get(tid)
            group = _thread_group(name) if name is not None else f"native:{_thread_group(comm)}"
            grouped[group] = grouped.get(group, 0.0) + delta / self._ticks_per_s / elapsed * 100
        rows = sorted(grouped.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_THREAD_ROWS]
        out["threads"] = {name: round(pct, 1) for name, pct in rows}
        return out
