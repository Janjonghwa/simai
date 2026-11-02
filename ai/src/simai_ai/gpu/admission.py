"""Tier 0 지연 신호와 분석 워커·아바타 접수 문 (명세 3.2: Tier 0 지연이 6.6 목표를 넘거나 모르면 분석 워커의 새 작업과
상시 아바타 추론을 멈춘다, EXP-01·02 같은 대형 오프라인 Job 은 LIVE 관제 시간에 실행하지 않는다. 20.5 안전 경로 격리).

신호 경로: Tier 0 → 표시 파일(state/tier0.live, 5초마다 원자적 교체) → 분석 워커·아바타. 같은 Pod·같은 볼륨이라 네트워크가 없고,
EC2·I5 계약을 바꾸지 않는다. 표시 파일의 `gpu_priority` 칸(marker_section)이 이 모듈의 형식이다.

Tier 0 쪽(LoadWindow): 정리 주기(약 1초)마다 보정 T_lag(HealthReport.lag_ms, now − t_latest_global: 촬영 → 판정 지연의
상한 쪽 근사, capture_to_risk 와 같은 구간)를 넣고, 표시 파일을 쓸 때 그 사이 표본을 꺼낸다(take). 시계 차이를 몰라 lag 를
모르면 unknown 으로 센다. 표본이 하나도 없으면(정리 주기가 멈춤) samples=0 이다. 표시 파일 하나가 5초 창 하나다.

접수 쪽(AdmissionGate.check): 분석 워커는 새 작업을 받기 전, 아바타 루프는 추론 전마다 부른다. 판정 순서(앞에서 걸리면 그 이유로
끝, S15P31A101-412 사유별 규칙):
 1. 표시 파일 없음: 기본은 모름 → LIVE 로 보고 받지 않는다. 파일이 없는 것만으로는 '관제 없음'과 'Tier 0 가 표시 파일을 못 씀
    (볼륨 오류)·엔진 준비 중(진입점이 Tier 0 를 띄우기 전)'을 구별할 수 없다. 단, Tier 0 가 정상 종료하며 표시 파일 옆에 남긴
    정상 종료 기록(clean_stop_path, `<표시 파일>.stopped`, S15P31A101-403)이 있으면 '정상 종료로 끝남' → LIVE 아님이다. 이 기록은
    Tier 0 시작(simai-bootstrap 시작 잠금·Tier0Service.start)이 표시 파일을 쓰기 전에 지우므로 다음 기동·엔진 준비 중에는 남지 않는다.
    기록을 읽지 못하거나 형식이 틀리면 모름 → LIVE. 관제가 없는 개발 시간에는 운영자가 allow_without_marker
    (SIMAI_WORKER_DEV_NO_LIVE=1)로 명시할 수도 있다. 그때도 LIVE 아님 → 모든 유형을 받는다(명세 3.2 '관제가 없는 개발 시간에는
    단독 실행'). 두 경우 모두 마지막으로 LIVE 를 본 뒤 live_linger_s 안이면 받지 않는다(Tier 0 재시작 틈).
 2. 표시 파일을 확인·읽지 못함, max_marker_age_s 보다 오래됨(Tier 0 비정상 종료·멈춤), gpu_priority 칸 없음·형식 틀림 →
    모름 → 즉시 멈춘다.
 3. phase 가 running 이 아님(기동 중), 선확보 상태가 OK·SKIPPED·DISABLED 가 아님 → 즉시 멈춘다.
 4. 지연 모름(samples 0, unknown 1 이상, 최댓값 없음) → 즉시 멈춘다. 창 최댓값 lag_ms_max 가 예산(lag_budget_ms, 6.6
    capture_to_risk p95 600ms)을 넘으면 단 한 창이라도 즉시 멈춘다(허용 표본 없음).
 5. 추론 대기 상한 초과로 버린 프레임(queue_wait_exceeded): 한 창에서 max_queue_wait_exceeded(기본 2) 이하면 통과로 넘기고
    (안정 시계를 지우지 않는다), 넘으면 멈춘다. 지연 초과·모름과 함께 오면 4 에서 이미 멈춘다.
 6. 멈춘 뒤에는 통과 상태가 resume_after_s(기본 10초) 이어져야 다시 받는다(깜빡임 방지). 멈춤 사유가 나오면 처음부터 센다.
 LIVE 중에 받는 유형은 live_blocked_types 를 뺀 것이다(EXP-01 정적 3D, EXP-02 배치 HMR). 이 차단은 지연 규칙과 따로 늘 걸린다.

계측(판정에 쓰지 않음): 차단 사유별 check 횟수(blocked), 열림·안정 대기가 지워진 횟수(resets), 마지막 초기화 시각·사유,
넘긴 큐 대기 초과 창·프레임 수. stats() 로 읽는다.

시계는 주입받는다(wall_s: 표시 파일 수정 시각과 비교·계측 시각, monotonic: 안정 대기·linger).
"""

from __future__ import annotations

import json
import math
import os
import threading
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from simai_ai.gpu.reserve import ADMIT_STATES

MARKER_KEY = "gpu_priority"
SECTION_VERSION = 1
RUNNING = "running"
STARTING = "starting"
DEFAULT_LAG_BUDGET_MS = 600
DEFAULT_RESUME_AFTER_S = 10.0
DEFAULT_MAX_QUEUE_WAIT_EXCEEDED = 2
DEFAULT_LIVE_BLOCKED = frozenset({"static_reconstruct", "hmr_refine", "hmr_appearance_update"})
CLEAN_STOP_PHASE = "stopped_clean"
CLEAN_STOP_VERSION = 1


def clean_stop_path(marker: Path) -> Path:
    """Tier 0 정상 종료 기록 경로: 표시 파일 옆 `<표시 파일 이름>.stopped`(S15P31A101-403).

    표시 파일 자체에 남기지 않는 까닭: 표시 파일이 있으면 LIVE 로 보는 소비자(GPU 실험 큐 live_guard, simai-bootstrap 장면 모드,
    Tier 0 다음 시작의 비정상 종료 판단)가 이미 있다. 따로 두면 그들의 판단은 그대로다.
    """
    return Path(marker).with_name(Path(marker).name + ".stopped")


def clean_stop_record(*, stopped_at_ms: int, pid: int) -> dict[str, Any]:
    """Tier 0 가 모든 종료 단계(저널 비우기·마지막 체크포인트 포함)를 마친 뒤 표시 파일을 지우고 남기는 기록."""
    return {"version": CLEAN_STOP_VERSION, "phase": CLEAN_STOP_PHASE, "stopped_at": int(stopped_at_ms), "pid": int(pid)}

# 판정 사유 코드(계측·로그용)
OK = "ok"
NOT_LIVE = "not_live"
STABILIZING = "stabilizing"
LIVE_LINGER = "live_linger"
MARKER_ABSENT = "marker_absent"
MARKER_STALE = "marker_stale"
MARKER_UNREADABLE = "marker_unreadable"
NOT_RUNNING = "not_running"
RESERVE_NOT_OK = "reserve_not_ok"
NO_SAMPLES = "no_samples"
LAG_UNKNOWN = "lag_unknown"
OVER_BUDGET = "over_budget"
QUEUE_WAIT_EXCEEDED = "queue_wait_exceeded"
TIER0_STOPPED = "tier0_stopped"  # 정상 종료 기록으로 LIVE 아님(S15P31A101-403)


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(q / 100 * len(s)) - 1))  # nearest-rank
    return s[k]


class LoadWindow:
    """Tier 0 지연 표본 창. add 는 정리 주기, take 는 표시 파일 쓰기에서 부른다(스레드 안전)."""

    def __init__(self, budget_ms: int = DEFAULT_LAG_BUDGET_MS, max_values: int = 64) -> None:
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


def _number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _count(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


@dataclass(frozen=True)
class AdmissionPolicy:
    lag_budget_ms: int = DEFAULT_LAG_BUDGET_MS
    max_marker_age_s: float = 15.0
    resume_after_s: float = DEFAULT_RESUME_AFTER_S  # 멈춘 뒤 이만큼 통과가 이어져야 다시 받음(근거: 10차 실측, 설계 문서)
    live_linger_s: float = 60.0
    # 한 창(표시 파일 하나, 5초)에서 넘기는 큐 대기 초과 프레임 수. 이보다 많으면 멈춘다
    max_queue_wait_exceeded: int = DEFAULT_MAX_QUEUE_WAIT_EXCEEDED
    live_blocked_types: frozenset[str] = field(default_factory=lambda: DEFAULT_LIVE_BLOCKED)
    allow_without_marker: bool = False  # 표시 파일이 없으면 관제 없음으로 본다(개발 시간 명시 모드). 기본은 모름 → 받지 않음

    def __post_init__(self) -> None:
        if self.lag_budget_ms <= 0 or self.max_marker_age_s <= 0:
            raise ValueError("lag_budget_ms·max_marker_age_s 는 양수다")
        if self.resume_after_s < 0 or self.live_linger_s < 0:
            raise ValueError("resume_after_s·live_linger_s 는 0 이상이다")
        if not _count(self.max_queue_wait_exceeded):
            raise ValueError("max_queue_wait_exceeded 는 0 이상의 정수다")


@dataclass(frozen=True)
class Admission:
    live: bool  # Tier 0 LIVE 로 본다(모를 때도 True)
    admit: bool  # 새 작업을 받아도 된다(LIVE 면 막힌 유형은 빼고)
    reason: str
    blocked_types: frozenset[str] = frozenset()
    code: str = ""  # 판정 사유 코드(계측용, 위 상수)

    def allowed_types(self, types: Iterable[str]) -> list[str]:
        if not self.admit:
            return []
        return [t for t in types if not (self.live and t in self.blocked_types)]

    def blocks(self, job_type: str) -> bool:
        """LIVE 라서 이 유형을 돌리면 안 된다(지연과 무관)."""
        return self.live and job_type in self.blocked_types


class AdmissionGate:
    """분석 워커·아바타 루프가 부르는 접수 문. check·stats 는 한 스레드에서 부른다고 가정한다."""

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
        self._opened = False
        self._last_marker_id: tuple[int, bytes] | None = None
        # 계측(판정에 쓰지 않음)
        self.blocked: Counter[str] = Counter()  # 받지 않은 check 횟수, 사유 코드별
        self.resets: Counter[str] = Counter()  # 열림·안정 대기를 지운 횟수, 사유 코드별
        self.opened_count = 0
        self.last_reset_wall_ms: int | None = None
        self.last_reset_code: str | None = None
        self.last_reset_reason: str | None = None
        self.queue_wait_tolerated_windows = 0
        self.queue_wait_tolerated_frames = 0

    def stats(self) -> dict[str, Any]:
        """계측 요약(로그·상태 표시용). 판정에 쓰지 않는다."""
        return {
            "blocked": dict(self.blocked),
            "resets": dict(self.resets),
            "opened": self.opened_count,
            "last_reset_ms": self.last_reset_wall_ms,
            "last_reset_code": self.last_reset_code,
            "last_reset_reason": self.last_reset_reason,
            "queue_wait_tolerated_windows": self.queue_wait_tolerated_windows,
            "queue_wait_tolerated_frames": self.queue_wait_tolerated_frames,
        }

    def _close(self, code: str, reason: str) -> None:
        """멈춤: 안정 시계를 지운다. 열려 있었거나 안정 대기 중이었으면 초기화로 센다."""
        if self._ok_since is not None or self._opened:
            self.resets[code] += 1
            self.last_reset_wall_ms = int(self._wall_s() * 1000)
            self.last_reset_code, self.last_reset_reason = code, reason
        self._ok_since = None
        self._opened = False

    def _refuse(self, reason: str, code: str) -> Admission:
        self.blocked[code] += 1
        return Admission(True, False, reason, self.policy.live_blocked_types, code)

    def check(self) -> Admission:
        now = self._monotonic()
        state, code, reason, marker_id, qwe = self._evaluate()
        blocked = self.policy.live_blocked_types
        if state == "absent" and not self.policy.allow_without_marker:
            self._close(code, reason)
            return self._refuse(f"{reason}·정상 종료 기록 없음 → Tier 0 상태 모름(표시 파일 쓰기 실패·엔진 준비 중일 수 있다). "
                                "관제가 없는 개발 시간이면 SIMAI_WORKER_DEV_NO_LIVE=1", code)
        if state in ("absent", "stopped"):  # stopped: 정상 종료 기록(S15P31A101-403), absent: 개발 모드(allow_without_marker)
            self._close(code, reason)
            if self._live_seen is not None and now - self._live_seen < self.policy.live_linger_s:
                left = self.policy.live_linger_s - (now - self._live_seen)
                return self._refuse(f"{reason}: LIVE 가 끝난 직후라 {left:.0f}s 더 LIVE 로 본다(재시작 틈)", LIVE_LINGER)
            self._live_seen = None
            return Admission(False, True, reason, blocked, NOT_LIVE)
        self._live_seen = now
        if state == "refuse":
            self._close(code, reason)
            return self._refuse(reason, code)
        if qwe:
            if marker_id != self._last_marker_id:  # 같은 표시 파일을 여러 번 읽어도 한 창으로 센다
                self.queue_wait_tolerated_windows += 1
                self.queue_wait_tolerated_frames += qwe
            reason = f"{reason}, 큐 대기 초과 {qwe}건은 넘김(창당 {self.policy.max_queue_wait_exceeded}건까지)"
        self._last_marker_id = marker_id
        if self._ok_since is None:
            self._ok_since = now
        held = now - self._ok_since
        if held < self.policy.resume_after_s:
            return self._refuse(f"{reason}, 안정 대기 {held:.0f}/{self.policy.resume_after_s:.0f}s", STABILIZING)
        if not self._opened:
            self._opened = True
            self.opened_count += 1
        return Admission(True, True, reason, blocked, OK)

    def _without_marker(self, why: str) -> tuple[str, str, str, None, int]:
        """표시 파일이 없을 때: 정상 종료 기록이 있으면 stopped, 없으면 absent(기본 모름), 기록을 못 읽거나 틀리면 refuse(모름)."""
        path = clean_stop_path(self.marker)
        try:
            doc = json.loads(path.read_bytes().decode("utf-8"))
        except FileNotFoundError:
            return "absent", MARKER_ABSENT, why, None, 0
        except (OSError, ValueError) as e:
            return ("refuse", MARKER_UNREADABLE,
                    f"{why}, 정상 종료 기록 {path.name} 을 읽지 못했다({type(e).__name__}) → 모름", None, 0)
        if (not isinstance(doc, dict) or doc.get("phase") != CLEAN_STOP_PHASE or not _count(doc.get("stopped_at"))
                or doc.get("version") != CLEAN_STOP_VERSION):
            return "refuse", MARKER_UNREADABLE, f"{why}, 정상 종료 기록 {path.name} 형식이 틀렸다 → 모름", None, 0
        return ("stopped", TIER0_STOPPED,
                f"Tier 0 가 정상 종료했다({path.name}, stopped_at {doc['stopped_at']}) → LIVE 아님", None, 0)

    def _evaluate(self) -> tuple[str, str, str, tuple[int, bytes] | None, int]:
        """('absent' | 'stopped' | 'refuse' | 'ok', 사유 코드, 이유, 표시 파일 식별자, 넘긴 큐 대기 초과 프레임 수)."""
        try:
            st = os.stat(self.marker)
        except FileNotFoundError:
            return self._without_marker("표시 파일 없음")
        except OSError as e:
            return "refuse", MARKER_UNREADABLE, f"표시 파일을 확인하지 못했다({type(e).__name__}) → 모름", None, 0
        age = self._wall_s() - st.st_mtime
        if abs(age) > self.policy.max_marker_age_s:
            return ("refuse", MARKER_STALE,
                    f"표시 파일이 {age:.0f}s 전 것이다(상한 {self.policy.max_marker_age_s:.0f}s) → Tier 0 상태 모름", None, 0)
        try:
            raw_bytes = self.marker.read_bytes()
            doc = json.loads(raw_bytes.decode("utf-8"))
        except FileNotFoundError:
            return self._without_marker("표시 파일이 방금 지워졌다")
        except (OSError, ValueError) as e:
            return "refuse", MARKER_UNREADABLE, f"표시 파일을 읽지 못했다({type(e).__name__}) → 모름", None, 0
        marker_id = (st.st_mtime_ns, raw_bytes)
        gp = doc.get(MARKER_KEY) if isinstance(doc, dict) else None
        if not isinstance(gp, dict):
            return "refuse", MARKER_UNREADABLE, "표시 파일에 지연 신호(gpu_priority)가 없다 → 모름", marker_id, 0
        if gp.get("phase") != RUNNING:
            return "refuse", NOT_RUNNING, f"Tier 0 가 판정 중이 아니다(phase={gp.get('phase')!r})", marker_id, 0
        reserve = gp.get("reserve")
        state = reserve.get("state") if isinstance(reserve, dict) else None
        if state not in ADMIT_STATES:
            return "refuse", RESERVE_NOT_OK, f"Tier 0 GPU 선확보 상태 {state!r}", marker_id, 0
        load = gp.get("load")
        if not isinstance(load, dict):
            return "refuse", MARKER_UNREADABLE, "지연 표본 형식이 틀렸다 → 모름", marker_id, 0
        samples, unknown, lag_max = load.get("samples"), load.get("unknown"), load.get("lag_ms_max")
        if not _count(samples) or samples == 0:
            return "refuse", NO_SAMPLES, "지연 표본이 없다 → 모름", marker_id, 0
        if not _count(unknown) or unknown > 0:
            return "refuse", LAG_UNKNOWN, f"지연을 모르는 표본 {unknown}/{samples} (시계 차이 모름 등) → 모름", marker_id, 0
        if not _number(lag_max):
            return "refuse", LAG_UNKNOWN, "지연 최댓값이 없다 → 모름", marker_id, 0
        qwe = load.get("queue_wait_exceeded")
        if qwe is not None and not _count(qwe):
            return "refuse", MARKER_UNREADABLE, "queue_wait_exceeded 형식이 틀렸다 → 모름", marker_id, 0
        if lag_max > self.policy.lag_budget_ms:
            return ("refuse", OVER_BUDGET, f"Tier 0 지연 {lag_max:.0f}ms > 예산 {self.policy.lag_budget_ms}ms",
                    marker_id, 0)
        qwe = qwe or 0
        if qwe > self.policy.max_queue_wait_exceeded:
            return ("refuse", QUEUE_WAIT_EXCEEDED,
                    f"추론 대기 상한을 넘겨 버린 프레임 {qwe}개(창당 {self.policy.max_queue_wait_exceeded}개 초과)",
                    marker_id, 0)
        return "ok", OK, f"Tier 0 지연 {lag_max:.0f}ms ≤ 예산 {self.policy.lag_budget_ms}ms", marker_id, qwe
