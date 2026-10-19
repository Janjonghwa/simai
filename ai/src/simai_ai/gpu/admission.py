"""Tier 0 지연 신호와 분석 워커 작업 접수 문 (명세 3.2: Tier 0 지연이 6.6 목표를 넘으면 분석 워커의 새 작업을 멈추고 대기열에
둔다, EXP-01·02 같은 대형 오프라인 Job 은 LIVE 관제 시간에 실행하지 않는다. 20.5 안전 경로 격리).

신호 경로: Tier 0 → 표시 파일(state/tier0.live, 5초마다 원자적 교체) → 분석 워커. 같은 Pod·같은 볼륨이라 네트워크가 없고,
EC2·I5 계약을 바꾸지 않는다. 표시 파일의 `gpu_priority` 칸(marker_section)이 이 모듈의 형식이다.

Tier 0 쪽(LoadWindow): 정리 주기(약 1초)마다 보정 T_lag(HealthReport.lag_ms, now − t_latest_global: 촬영 → 판정 지연의
상한 쪽 근사, capture_to_risk 와 같은 구간)를 넣고, 표시 파일을 쓸 때 그 사이 표본을 꺼낸다(take). 시계 차이를 몰라 lag 를
모르면 unknown 으로 센다. 표본이 하나도 없으면(정리 주기가 멈춤) samples=0 이다.

분석 워커 쪽(AdmissionGate.check): 새 작업을 받기 전에 부른다. 판정 순서(앞에서 걸리면 그 이유로 끝):
 1. 표시 파일 없음: 기본은 모름 → LIVE 로 보고 받지 않는다. 파일이 없는 것만으로는 '관제 없음'과 'Tier 0 가 표시 파일을 못 씀
    (볼륨 오류)·엔진 준비 중(진입점이 Tier 0 를 띄우기 전)'을 구별할 수 없다. 관제가 없는 개발 시간에는 운영자가
    allow_without_marker(SIMAI_WORKER_DEV_NO_LIVE=1)로 명시한다. 그때만 LIVE 아님 → 모든 유형을 받는다(명세 3.2 '관제가 없는
    개발 시간에는 단독 실행'). 그 모드에서도 마지막으로 LIVE 를 본 뒤 live_linger_s 안이면 받지 않는다(Tier 0 재시작 틈).
 2. 표시 파일을 확인·읽지 못함, max_marker_age_s 보다 오래됨(Tier 0 비정상 종료·멈춤), gpu_priority 칸 없음·형식 틀림 →
    모름 → LIVE 로 보고 받지 않는다.
 3. phase 가 running 이 아님(기동 중), 선확보 상태가 OK·SKIPPED·DISABLED 가 아님 → 받지 않는다.
 4. 지연: samples 0, unknown 1 이상, lag_ms_max 가 예산(lag_budget_ms, 6.6 capture_to_risk p95 600ms) 초과, queue_wait_exceeded
    1 이상 → 받지 않는다. 창 안의 최댓값으로 본다(p95 보다 엄격, Tier 0 우선).
 5. 위가 모두 통과해도 resume_after_s 동안 이어져야 받는다(깜빡임 방지). 한 번이라도 걸리면 처음부터 다시 센다.
 LIVE 중에 받는 유형은 live_blocked_types 를 뺀 것이다(EXP-01 정적 3D, EXP-02 HMR).

시계는 주입받는다(wall_s: 표시 파일 수정 시각과 비교, monotonic: 안정 대기·linger).
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_ai.gpu.reserve import ADMIT_STATES

MARKER_KEY = "gpu_priority"
SECTION_VERSION = 1
RUNNING = "running"
STARTING = "starting"
DEFAULT_LIVE_BLOCKED = frozenset({"static_reconstruct", "hmr_refine", "hmr_appearance_update"})


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(q / 100 * len(s)) - 1))  # nearest-rank
    return s[k]


class LoadWindow:
    """Tier 0 지연 표본 창. add 는 정리 주기, take 는 표시 파일 쓰기에서 부른다(스레드 안전)."""

    def __init__(self, budget_ms: int = 600, max_values: int = 64) -> None:
        if budget_ms <= 0 or max_values <= 0:
            raise ValueError("budget_ms·max_values 는 양수다")
        self.budget_ms = budget_ms
        self._lock = threading.Lock()
        self._values: deque[float] = deque(maxlen=max_values)
        self._reset()

    def _reset(self) -> None:
        self._values.clear()
        self._samples = 0
        self._unknown = 0
        self._over = 0
        self._max: float | None = None
        self._queue_wait_exceeded: int | None = None

    def add(self, lag_ms: float | None, *, queue_wait_exceeded: int | None = None) -> None:
        """lag_ms 는 보정 T_lag(모르면 None). queue_wait_exceeded 는 직전 표본 뒤 대기 상한을 넘겨 버린 프레임 수(모르면 None)."""
        with self._lock:
            self._samples += 1
            if lag_ms is None or isinstance(lag_ms, bool) or not math.isfinite(float(lag_ms)):
                self._unknown += 1
            else:
                v = max(0.0, float(lag_ms))
                self._values.append(v)
                self._max = v if self._max is None else max(self._max, v)
                if v > self.budget_ms:
                    self._over += 1
            if queue_wait_exceeded is not None:
                self._queue_wait_exceeded = (self._queue_wait_exceeded or 0) + max(0, int(queue_wait_exceeded))

    def take(self) -> dict[str, Any]:
        """지난 take 뒤의 표본 요약을 꺼내고 창을 비운다."""
        with self._lock:
            values = list(self._values)
            out = {
                "samples": self._samples,
                "unknown": self._unknown,
                "lag_ms_max": None if self._max is None else round(self._max, 1),
                "lag_ms_p95": None if not values else round(_percentile(values, 95) or 0.0, 1),
                "lag_ms": [round(v, 1) for v in values],
                "over_budget": self._over,
                "budget_ms": self.budget_ms,
                "queue_wait_exceeded": self._queue_wait_exceeded,
            }
            self._reset()
            return out


def marker_section(*, phase: str, reserve: Mapping[str, Any], load: Mapping[str, Any] | None, written_at_ms: int,
                   pid: int) -> dict[str, Any]:
    """표시 파일의 gpu_priority 칸. load 가 None 이면(기동 중) 표본 없음으로 싣는다."""
    return {
        "version": SECTION_VERSION,
        "phase": phase,
        "pid": pid,
        "written_at": written_at_ms,
        "reserve": dict(reserve),
        "load": dict(load) if load is not None else {"samples": 0, "unknown": 0, "lag_ms_max": None, "lag_ms": []},
    }


@dataclass(frozen=True)
class AdmissionPolicy:
    lag_budget_ms: int = 600
    max_marker_age_s: float = 15.0
    resume_after_s: float = 30.0
    live_linger_s: float = 60.0
    live_blocked_types: frozenset[str] = field(default_factory=lambda: DEFAULT_LIVE_BLOCKED)
    allow_without_marker: bool = False  # 표시 파일이 없으면 관제 없음으로 본다(개발 시간 명시 모드). 기본은 모름 → 받지 않음

    def __post_init__(self) -> None:
        if self.lag_budget_ms <= 0 or self.max_marker_age_s <= 0:
            raise ValueError("lag_budget_ms·max_marker_age_s 는 양수다")
        if self.resume_after_s < 0 or self.live_linger_s < 0:
            raise ValueError("resume_after_s·live_linger_s 는 0 이상이다")


@dataclass(frozen=True)
class Admission:
    live: bool  # Tier 0 LIVE 로 본다(모를 때도 True)
    admit: bool  # 새 작업을 받아도 된다(LIVE 면 막힌 유형은 빼고)
    reason: str
    blocked_types: frozenset[str] = frozenset()

    def allowed_types(self, types: Iterable[str]) -> list[str]:
        if not self.admit:
            return []
        return [t for t in types if not (self.live and t in self.blocked_types)]

    def blocks(self, job_type: str) -> bool:
        """LIVE 라서 이 유형을 돌리면 안 된다(지연과 무관)."""
        return self.live and job_type in self.blocked_types


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


class AdmissionGate:
    """분석 워커가 대여 요청 전·작업 시작 전에 부르는 접수 문. check 는 한 스레드에서 부른다고 가정한다."""

    def __init__(
        self,
        marker: Path,
        policy: AdmissionPolicy = AdmissionPolicy(),
        *,
        wall_s: Callable[[], float],
        monotonic: Callable[[], float],
    ) -> None:
        self.marker = Path(marker)
        self.policy = policy
        self._wall_s = wall_s
        self._monotonic = monotonic
        self._ok_since: float | None = None
        self._live_seen: float | None = None

    def check(self) -> Admission:
        now = self._monotonic()
        state, reason = self._evaluate()
        blocked = self.policy.live_blocked_types
        if state == "absent" and not self.policy.allow_without_marker:
            self._ok_since = None
            return Admission(True, False, f"{reason} → Tier 0 상태 모름(표시 파일 쓰기 실패·엔진 준비 중일 수 있다). "
                                          "관제가 없는 개발 시간이면 SIMAI_WORKER_DEV_NO_LIVE=1", blocked)
        if state == "absent":
            self._ok_since = None
            if self._live_seen is not None and now - self._live_seen < self.policy.live_linger_s:
                left = self.policy.live_linger_s - (now - self._live_seen)
                return Admission(True, False, f"{reason}: LIVE 가 끝난 직후라 {left:.0f}s 더 LIVE 로 본다(재시작 틈)", blocked)
            self._live_seen = None
            return Admission(False, True, reason, blocked)
        self._live_seen = now
        if state != "ok":
            self._ok_since = None
            return Admission(True, False, reason, blocked)
        if self._ok_since is None:
            self._ok_since = now
        held = now - self._ok_since
        if held < self.policy.resume_after_s:
            return Admission(True, False, f"{reason}, 안정 대기 {held:.0f}/{self.policy.resume_after_s:.0f}s", blocked)
        return Admission(True, True, reason, blocked)

    def _evaluate(self) -> tuple[str, str]:
        """('absent' | 'refuse' | 'ok', 이유)."""
        try:
            st = os.stat(self.marker)
        except FileNotFoundError:
            return "absent", "표시 파일 없음"
        except OSError as e:
            return "refuse", f"표시 파일을 확인하지 못했다({type(e).__name__}) → 모름"
        age = self._wall_s() - st.st_mtime
        if abs(age) > self.policy.max_marker_age_s:
            return "refuse", f"표시 파일이 {age:.0f}s 전 것이다(상한 {self.policy.max_marker_age_s:.0f}s) → Tier 0 상태 모름"
        try:
            doc = json.loads(self.marker.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return "absent", "Tier 0 LIVE 아님(표시 파일이 방금 지워졌다)"
        except (OSError, ValueError) as e:
            return "refuse", f"표시 파일을 읽지 못했다({type(e).__name__}) → 모름"
        gp = doc.get(MARKER_KEY) if isinstance(doc, dict) else None
        if not isinstance(gp, dict):
            return "refuse", "표시 파일에 지연 신호(gpu_priority)가 없다 → 모름"
        if gp.get("phase") != RUNNING:
            return "refuse", f"Tier 0 가 판정 중이 아니다(phase={gp.get('phase')!r})"
        reserve = gp.get("reserve")
        state = reserve.get("state") if isinstance(reserve, dict) else None
        if state not in ADMIT_STATES:
            return "refuse", f"Tier 0 GPU 선확보 상태 {state!r}"
        load = gp.get("load")
        if not isinstance(load, dict):
            return "refuse", "지연 표본 형식이 틀렸다 → 모름"
        samples, unknown, lag_max = load.get("samples"), load.get("unknown"), load.get("lag_ms_max")
        if not _count(samples) or samples == 0:
            return "refuse", "지연 표본이 없다 → 모름"
        if not _count(unknown) or unknown > 0:
            return "refuse", f"지연을 모르는 표본 {unknown}/{samples} (시계 차이 모름 등) → 모름"
        if not _number(lag_max):
            return "refuse", "지연 최댓값이 없다 → 모름"
        if lag_max > self.policy.lag_budget_ms:
            return "refuse", f"Tier 0 지연 {lag_max:.0f}ms > 예산 {self.policy.lag_budget_ms}ms"
        qwe = load.get("queue_wait_exceeded")
        if qwe is not None:
            if not _count(qwe):
                return "refuse", "queue_wait_exceeded 형식이 틀렸다 → 모름"
            if qwe > 0:
                return "refuse", f"추론 대기 상한을 넘겨 버린 프레임 {qwe}개"
        return "ok", f"Tier 0 지연 {lag_max:.0f}ms ≤ 예산 {self.policy.lag_budget_ms}ms"
