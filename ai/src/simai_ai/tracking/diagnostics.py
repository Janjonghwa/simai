"""Tier 0 추적 계측: 시각 정렬·카메라 간 연결·World Track 생명주기의 사유별 횟수와 분포 (S15P31A101-413).

판정 결과를 바꾸지 않는 관찰자다. `services.tier0.perception.Perception` 이 tick 마다 이미 만든 값(융합 시각에서 뺀 늦은 카메라,
보간 탈락 사유, `AssociationFrame` 의 쌍 근거·클러스터, `WorldTrackFrame`, `WorldTrackManager.diag` 분기 횟수)을 넘기면
누계와 주기 창(기본 30 s, 촬영 시각 기준)으로 센다. 상태 파일(`tier0.live` 의 status)과 주기 요약 로그가 읽는다.
계약(world_state 등)에는 싣지 않는다.

센 것:
- 늦은 카메라: 카메라별 제외 tick 수와 제외 시간(직전 인지 tick 과의 공통 시각 차이를 그 tick 에 붙인다) / 전체 시간 비율.
  기존 로그("늦은 카메라를 융합에서 뺀다")는 제외 집합이 바뀔 때만 찍혀 횟수가 시간 비율이 아니다.
- 보간: 카메라별 보간에 성공한 view 수와 탈락 사유(`geometry.interpolation.DropReason`)별 횟수. 탈락은 (카메라, 로컬 트랙)
  버퍼마다 센다(사라진 트랙의 버퍼가 창 안에 남아 있으면 NO_FRAME_AFTER 로 세어진다).
- 카메라 쌍: 쌍 근거마다 결과(PASS·PASS_MARGINAL·거부 사유) 횟수, 재투영 잔차 분포, "view 마다 상대 카메라의 가장 가까운
  에피폴라 거리" 분포(다른 사람 쌍이 섞인 거부 횟수만으로는 보정 문제와 구별이 안 돼 짝 후보가 있기나 한지를 본다).
- 클러스터: 카메라 수 분포, IDENTITY_UNCERTAIN 사유 횟수.
- World Track: 새로 나타난 ID(appeared: 새 발급 + 병합으로 되살아난 소실 후보. 새 발급만은 manager 의 new_track.created),
  LOST(TENTATIVE·확정 뒤), 병합으로 사라짐, 늦은 재연결, 상태별 누적 시간, LOST 까지 수명 분포,
  위치를 갱신한 Track 의 관측 카메라 수 분포, 그리고 `WorldTrackManager.diag` 의 분기 사유 횟수(재연결 실패 사유 등).
- World 신원(S15P31A101-684): 클러스터 횟수(uncertain_reasons)가 아니라 World Track 시간으로 센다.
  - identity_ms: (CONFIRMED|TENTATIVE 묶음, identity_confidence) 별 Track·시간(ms). CONFIRMED 묶음은 CONFIRMED·OCCLUDED.
  - uncertain_reason_ms / uncertain_sole_ms: 확정 묶음 Track 이 IDENTITY_UNCERTAIN 인 동안 사유별 시간과, 그 사유 하나만
    남아 있던 시간. 사유가 여럿이면 reason_ms 는 겹쳐 세어지므로 합이 불확실 시간보다 클 수 있다. 원인 비중은 sole 로 본다.
  - uncertain_hold_only_ms: 이번 시각에 새로 붙은 사유 없이 uncertain_hold_ms 유지로만 불확실이던 시간.
  - uncertain_raised: 확정 묶음 Track 에 사유가 새로 붙은(갱신된) Track·tick 수.
  - uncertain_episodes: 불확실 구간이 끝난 원인(CLEARED 확신 회복, LOST, MERGED_AWAY 병합으로 사라짐)별 길이 분포.
    끝나지 않은 구간은 summary 의 uncertain_open 으로 본다.
  - local_key_moves / local_key_swaps: (카메라, 로컬 트랙)이 살아 있는 다른 World ID 로 옮겨 간 횟수와, 같은 tick 에 두 ID 가
    서로의 로컬 트랙을 맞바꾼 쌍 수. 정답 없이 보는 swap 대리 지표다(로컬 추적기 자체의 swap 은 잡지 못한다). 병합으로
    사라진 ID 의 로컬 트랙을 넘겨받은 것은 세지 않는다.
  시간은 state_ms 와 같이 직전 tick 의 상태에 그 사이 시간을 붙인다.
- 교차 시점 계산량(S15P31A101-726): `AssociationFrame.work`(view 수, 원시 쌍 수, 에피폴라 통과 수, 2-view SVD 관절 수,
  클러스터 수, 충돌 성분 평가 수·최대 크기, 분할 탐색 방문 수, 클러스터 삼각측량 1차 관절 수·순차 경로 관절 수·재풀이 수,
  VIEW_SPLIT 수)의 tick 평균·최대. 판정 시간(stage_timing)과 같이 보려고 최근 256 tick 의 평균·p95·최대도 낸다(`work_summary`).

현재 시각을 읽지 않는다. 모든 시각은 공통 시각 t(UTC ms)다. 다른 스레드(상태 파일)가 읽으므로 lock 으로 감싼다.
"""

from __future__ import annotations

import bisect
import math
import threading
from collections import Counter, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from simai_ai.geometry.interpolation import FrameDrop
from simai_ai.tracking.cross_view import AssociationFrame, LocalView
from simai_ai.tracking.world_track import IdentityConfidence, TrackLifecycle, WorldTrackFrame

DEFAULT_WINDOW_MS = 30_000
WORK_RECENT_TICKS = 256  # stage_timing 표본 창과 같은 크기
UNCERTAIN_EPISODE_EDGES_MS = (100, 300, 1000, 2000, 5000, 10_000, 30_000, 60_000)
_CONFIRMED_GROUP = (TrackLifecycle.CONFIRMED, TrackLifecycle.OCCLUDED)
RESIDUAL_EDGES_PX = (2.0, 4.0, 6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 30.0)
EPIPOLAR_EDGES_PX = (5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0, 200.0)
LIFESPAN_EDGES_MS = (300, 500, 1000, 2000, 5000, 10_000, 30_000, 60_000, 300_000)


def _bin(value: float, edges: Sequence[float]) -> str:
    """값이 든 구간의 이름: '≤e' 또는 '>마지막'."""
    i = bisect.bisect_left(edges, value)
    return f"<={edges[i]:g}" if i < len(edges) else f">{edges[-1]:g}"


def _pair_name(a: str, b: str) -> str:
    return f"{a}/{b}" if a <= b else f"{b}/{a}"


@dataclass
class _Stats:
    ticks: int = 0
    idle_steps: int = 0
    span_ms: int = 0
    lagging_ticks: Counter = field(default_factory=Counter)
    lagging_ms: Counter = field(default_factory=Counter)
    views: Counter = field(default_factory=Counter)
    drops: Counter = field(default_factory=Counter)  # (camera, reason)
    pair_outcomes: Counter = field(default_factory=Counter)  # (pair, outcome)
    pair_residual: Counter = field(default_factory=Counter)  # (pair, bin)
    pair_best_epipolar: Counter = field(default_factory=Counter)  # (pair, bin)
    cluster_cameras: Counter = field(default_factory=Counter)
    uncertain: Counter = field(default_factory=Counter)
    appeared: int = 0
    lost: Counter = field(default_factory=Counter)  # TENTATIVE / CONFIRMED(확정된 적 있음)
    merged_away: int = 0
    reassociated: int = 0
    revisions: Counter = field(default_factory=Counter)
    state_ms: Counter = field(default_factory=Counter)
    lifespan: Counter = field(default_factory=Counter)  # (TENTATIVE|CONFIRMED, bin)
    updated_cameras: Counter = field(default_factory=Counter)
    manager: Counter = field(default_factory=Counter)
    max_confirmed: int = 0  # 한 tick 의 동시 확정(CONFIRMED·OCCLUDED) Track 최대 (-657)
    max_duplicate_pairs: int = 0  # 한 tick 의 중복 의심 쌍(시점 분할 근거·POSSIBLE_DUPLICATE) 최대 (-657)
    identity_ms: Counter = field(default_factory=Counter)  # (CONFIRMED|TENTATIVE, confidence) (-684)
    uncertain_reason_ms: Counter = field(default_factory=Counter)  # flag
    uncertain_sole_ms: Counter = field(default_factory=Counter)  # flag
    uncertain_hold_only_ms: int = 0
    uncertain_raised: Counter = field(default_factory=Counter)  # flag
    uncertain_episodes: Counter = field(default_factory=Counter)  # (end cause, bin)
    local_key_moves: int = 0
    local_key_swaps: int = 0
    work_ticks: int = 0  # 계산량을 받은 연결 tick 수 (-726)
    work_sum: Counter = field(default_factory=Counter)
    work_max: Counter = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        def nest(c: Counter) -> dict[str, dict[str, int]]:
            out: dict[str, dict[str, int]] = {}
            for (a, b), n in sorted(c.items()):
                out.setdefault(str(a), {})[str(b)] = int(n)
            return out

        span = self.span_ms
        return {
            "ticks": self.ticks,
            "idle_steps": self.idle_steps,
            "span_ms": span,
            "lagging": {
                c: {"ticks": int(self.lagging_ticks[c]), "ms": int(self.lagging_ms[c]),
                    "ratio": round(self.lagging_ms[c] / span, 4) if span else None}
                for c in sorted(set(self.lagging_ticks) | set(self.lagging_ms))
            },
            "views": {c: int(n) for c, n in sorted(self.views.items())},
            "interpolation_drops": nest(self.drops),
            "pair_outcomes": nest(self.pair_outcomes),
            "pair_residual_px": nest(self.pair_residual),
            "pair_best_epipolar_px": nest(self.pair_best_epipolar),
            "cluster_cameras": {str(k): int(v) for k, v in sorted(self.cluster_cameras.items())},
            "uncertain_reasons": {str(k): int(v) for k, v in sorted(self.uncertain.items())},
            "world": {
                "appeared": self.appeared,
                "lost": {str(k): int(v) for k, v in sorted(self.lost.items())},
                "merged_away": self.merged_away,
                "reassociated": self.reassociated,
                "revisions": {str(k): int(v) for k, v in sorted(self.revisions.items())},
                "state_ms": {str(k): int(v) for k, v in sorted(self.state_ms.items())},
                "lifespan_ms": nest(self.lifespan),
                "updated_cameras": {str(k): int(v) for k, v in sorted(self.updated_cameras.items())},
                "manager": {str(k): int(v) for k, v in sorted(self.manager.items())},
                "max_confirmed": self.max_confirmed,
                "max_duplicate_pairs": self.max_duplicate_pairs,
            },
            "identity": {
                "identity_ms": nest(self.identity_ms),
                "uncertain_reason_ms": {str(k): int(v) for k, v in sorted(self.uncertain_reason_ms.items())},
                "uncertain_sole_ms": {str(k): int(v) for k, v in sorted(self.uncertain_sole_ms.items())},
                "uncertain_hold_only_ms": self.uncertain_hold_only_ms,
                "uncertain_raised": {str(k): int(v) for k, v in sorted(self.uncertain_raised.items())},
                "uncertain_episodes_ms": nest(self.uncertain_episodes),
                "local_key_moves": self.local_key_moves,
                "local_key_swaps": self.local_key_swaps,
            },
            "work": {
                "ticks": self.work_ticks,
                "mean": {k: round(v / self.work_ticks, 2) for k, v in self.work_sum.items()} if self.work_ticks else {},
                "max": {k: int(v) for k, v in self.work_max.items()},
            },
        }


@dataclass
class _Alive:
    first_t: int
    lifecycle: TrackLifecycle
    confirmed: bool
    confidence: IdentityConfidence | None = None
    flags: tuple[str, ...] = ()
    raised: tuple[str, ...] = ()
    uncertain_since: int | None = None
    """확정 묶음에서 IDENTITY_UNCERTAIN 이 시작된 시각(구간이 열려 있을 때만)"""


@dataclass
class _IdentityTick:
    """한 tick 의 World 신원 계측(-684). observe_world 가 lock 안에서 만들고 마지막에 두 집계(누계·창)에 더한다."""

    identity_ms: Counter = field(default_factory=Counter)
    reason_ms: Counter = field(default_factory=Counter)
    sole_ms: Counter = field(default_factory=Counter)
    hold_only_ms: int = 0
    raised: Counter = field(default_factory=Counter)
    episodes: Counter = field(default_factory=Counter)
    moves: int = 0
    swaps: int = 0

    def add_time(self, a: _Alive, dt: int) -> None:
        """직전 tick 의 상태(a)에 그 사이 시간 dt 를 붙인다(state_ms 와 같은 규칙)."""
        if dt <= 0 or a.confidence is None:
            return
        if a.lifecycle in _CONFIRMED_GROUP:
            group = "CONFIRMED"
        elif a.lifecycle is TrackLifecycle.TENTATIVE:
            group = "TENTATIVE"
        else:
            return
        self.identity_ms[(group, str(a.confidence))] += dt
        if group != "CONFIRMED" or a.confidence is not IdentityConfidence.IDENTITY_UNCERTAIN:
            return
        for f in a.flags:
            self.reason_ms[f] += dt
        if len(a.flags) == 1:
            self.sole_ms[a.flags[0]] += dt
        if not a.raised:
            self.hold_only_ms += dt

    def update(self, a: _Alive, snap: Any, t: int) -> None:
        """이번 tick 스냅샷으로 a 의 신원 상태를 바꾸고 불확실 구간을 열고 닫는다. LOST 구간 닫기는 호출자가 한다."""
        a.confidence = snap.identity_confidence
        a.flags = tuple(str(f) for f in snap.identity_flags)
        a.raised = tuple(str(f) for f in snap.identity_flags_raised)
        if snap.lifecycle is TrackLifecycle.LOST:
            return
        confirmed = snap.lifecycle in _CONFIRMED_GROUP
        if confirmed:
            for f in a.raised:
                self.raised[f] += 1
        uncertain = confirmed and snap.identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
        if uncertain and a.uncertain_since is None:
            a.uncertain_since = t
        elif not uncertain:
            self.end_episode(a, "CLEARED", t)

    def end_episode(self, a: _Alive, cause: str, t: int) -> None:
        if a.uncertain_since is None:
            return
        self.episodes[(cause, _bin(t - a.uncertain_since, UNCERTAIN_EPISODE_EDGES_MS))] += 1
        a.uncertain_since = None

    def observe_keys(self, frame: WorldTrackFrame, owner: dict[Any, str], present: set[str]) -> None:
        """로컬 트랙이 살아 있는 다른 World ID 로 옮겨 간 것과, 같은 tick 에 두 ID 가 맞바꾼 쌍을 센다."""
        moves: list[tuple[str, str]] = []
        for snap in frame.tracks:
            for k in snap.local_keys:
                prev = owner.get(k)
                if prev is not None and prev != snap.world_id and prev in present:
                    moves.append((prev, snap.world_id))
                owner[k] = snap.world_id
        self.moves += len(moves)
        seen = set(moves)
        self.swaps += len({frozenset(m) for m in moves if (m[1], m[0]) in seen})

    def apply(self, s: _Stats) -> None:
        s.identity_ms.update(self.identity_ms)
        s.uncertain_reason_ms.update(self.reason_ms)
        s.uncertain_sole_ms.update(self.sole_ms)
        s.uncertain_hold_only_ms += self.hold_only_ms
        s.uncertain_raised.update(self.raised)
        s.uncertain_episodes.update(self.episodes)
        s.local_key_moves += self.moves
        s.local_key_swaps += self.swaps


class TrackingDiagnostics:
    """추적 계측 누계와 주기 창. 판정 루프 스레드에서 observe_* 를, 아무 스레드에서 summary 를 부른다."""

    def __init__(self, window_ms: int = DEFAULT_WINDOW_MS) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms 는 양수다")
        self.window_ms = window_ms
        self._lock = threading.Lock()
        self._total = _Stats()
        self._window = _Stats()
        self._window_start: int | None = None
        self._last_t: int | None = None
        self._world_t: int | None = None
        self._alive: dict[str, _Alive] = {}
        self._manager_seen: Counter = Counter()
        self._key_owner: dict[Any, str] = {}
        """(카메라, 로컬 트랙) → 마지막으로 붙은 World ID (살아 있는 ID 만, -684 swap 대리 지표)"""
        self._work_recent: deque[dict[str, int]] = deque(maxlen=WORK_RECENT_TICKS)

    def _both(self) -> tuple[_Stats, _Stats]:
        return self._total, self._window

    # ------------------------------------------------------------------ 관찰

    def observe_idle(self) -> None:
        """새 공통 시각이 없어 인지 결과를 내지 않은 step."""
        with self._lock:
            for s in self._both():
                s.idle_steps += 1

    def observe_tick(self, t: int, lagging: Iterable[str]) -> None:
        """인지 결과를 내는 tick. lagging 은 그 tick 에 늦어서 뺀 카메라."""
        lagging = tuple(lagging)
        with self._lock:
            dt = 0 if self._last_t is None else max(0, t - self._last_t)
            self._last_t = t
            if self._window_start is None:
                self._window_start = t
            for s in self._both():
                s.ticks += 1
                s.span_ms += dt
                for c in lagging:
                    s.lagging_ticks[c] += 1
                    s.lagging_ms[c] += dt

    def observe_views(self, views: Sequence[LocalView], drops: Iterable[FrameDrop]) -> None:
        with self._lock:
            for s in self._both():
                for v in views:
                    s.views[v.camera_id] += 1
                for d in drops:
                    s.drops[(d.camera_id, str(d.reason))] += 1

    def observe_association(self, frame: AssociationFrame) -> None:
        best: dict[tuple[str, str, str], float] = {}  # (view 카메라·트랙, 상대 카메라) → 가장 가까운 에피폴라
        rows: list[tuple[str, str, float | None]] = []
        for p in frame.pairs:
            pair = _pair_name(p.a[0], p.b[0])
            if p.reject is not None:
                outcome = str(p.reject)
            else:
                outcome = "PASS_MARGINAL" if p.marginal else "PASS"
            rows.append((pair, outcome, p.residual_px))
            if p.epipolar_px is not None and math.isfinite(p.epipolar_px):
                for me, other in ((p.a, p.b), (p.b, p.a)):
                    key = (me[0], me[1], other[0])
                    if key not in best or p.epipolar_px < best[key]:
                        best[key] = p.epipolar_px
        work = {str(k): int(v) for k, v in (getattr(frame, "work", None) or {}).items()}
        with self._lock:
            if work:
                self._work_recent.append(work)
            for s in self._both():
                if work:
                    s.work_ticks += 1
                    s.work_sum.update(work)
                    for k, v in work.items():
                        s.work_max[k] = max(s.work_max[k], v)
                for pair, outcome, residual in rows:
                    s.pair_outcomes[(pair, outcome)] += 1
                    if residual is not None and math.isfinite(residual):
                        s.pair_residual[(pair, _bin(residual, RESIDUAL_EDGES_PX))] += 1
                for (cam, _track, other), e in best.items():
                    s.pair_best_epipolar[(_pair_name(cam, other), _bin(e, EPIPOLAR_EDGES_PX))] += 1
                for c in frame.clusters:
                    s.cluster_cameras[len(c.views)] += 1
                    for r in c.uncertain_reasons:
                        s.uncertain[str(r)] += 1

    def observe_world(
        self,
        frame: WorldTrackFrame,
        manager_counts: Mapping[str, int] | None = None,
        duplicate_pairs: int | None = None,
    ) -> None:
        """duplicate_pairs: 이번 tick 의 중복 의심 쌍 수(`WorldTrackManager.duplicate_pair_count`, -657). 모르면 None."""
        t = frame.t
        confirmed_now = sum(
            1 for s in frame.tracks if s.lifecycle in (TrackLifecycle.CONFIRMED, TrackLifecycle.OCCLUDED)
        )
        with self._lock:
            delta: Counter = Counter()
            if manager_counts is not None:
                for k, v in manager_counts.items():
                    d = int(v) - self._manager_seen.get(k, 0)
                    if d:
                        delta[k] = d
                self._manager_seen = Counter(dict(manager_counts))
            dt = 0 if self._world_t is None else max(0, t - self._world_t)
            self._world_t = t
            present = {s.world_id for s in frame.tracks}
            reassociated = set(frame.reassociations)
            appeared = lost_t = lost_c = merged = 0
            lifespans: list[tuple[str, str]] = []
            state_ms: Counter = Counter()
            cams: Counter = Counter()
            identity = _IdentityTick()
            for a in self._alive.values():
                state_ms[str(a.lifecycle)] += dt
                identity.add_time(a, dt)
            for gone in [w for w in self._alive if w not in present]:
                identity.end_episode(self._alive[gone], "MERGED_AWAY", t)
                del self._alive[gone]
                merged += 1  # LOST 를 내지 않고 사라진 ID 는 병합(ID 교정)으로 사라진 것이다
            identity.observe_keys(frame, self._key_owner, present)
            for snap in frame.tracks:
                a = self._alive.get(snap.world_id)
                if a is None:
                    if snap.world_id not in reassociated:
                        appeared += 1
                    a = self._alive[snap.world_id] = _Alive(t, snap.lifecycle, False)
                identity.update(a, snap, t)
                a.lifecycle = snap.lifecycle
                if snap.lifecycle is not TrackLifecycle.TENTATIVE and snap.lifecycle is not TrackLifecycle.LOST:
                    a.confirmed = True
                if snap.updated:
                    cams[len(snap.cameras)] += 1
                if snap.lifecycle is TrackLifecycle.LOST:
                    kind = "CONFIRMED" if a.confirmed else "TENTATIVE"
                    if a.confirmed:
                        lost_c += 1
                    else:
                        lost_t += 1
                    lifespans.append((kind, _bin(t - a.first_t, LIFESPAN_EDGES_MS)))
                    identity.end_episode(a, "LOST", t)
                    del self._alive[snap.world_id]
            alive_ids = set(self._alive)
            for k in [k for k, w in self._key_owner.items() if w not in alive_ids]:
                del self._key_owner[k]
            for s in self._both():
                s.appeared += appeared
                s.lost["TENTATIVE"] += lost_t
                s.lost["CONFIRMED"] += lost_c
                s.merged_away += merged
                s.reassociated += len(reassociated)
                for r in frame.revisions:
                    s.revisions[str(r.kind)] += 1
                s.state_ms.update(state_ms)
                for key in lifespans:
                    s.lifespan[key] += 1
                s.updated_cameras.update(cams)
                s.manager.update(delta)
                s.max_confirmed = max(s.max_confirmed, confirmed_now)
                if duplicate_pairs is not None:
                    s.max_duplicate_pairs = max(s.max_duplicate_pairs, duplicate_pairs)
                identity.apply(s)

    # ------------------------------------------------------------------ 읽기

    def window_due(self, t: int) -> bool:
        with self._lock:
            return self._window_start is not None and t - self._window_start >= self.window_ms

    def take_window(self, t: int | None = None) -> dict[str, Any]:
        """지금 창의 집계를 돌려주고 새 창을 시작한다."""
        with self._lock:
            out = self._window.as_dict()
            out["window_start_t"] = self._window_start
            self._window = _Stats()
            self._window_start = t if t is not None else self._last_t
            return out

    def work_summary(self) -> dict[str, dict[str, float]]:
        """최근 연결 tick(최대 256)의 계산량별 평균·p95·최대 (stage_timing 줄용, 판정에 쓰지 않음)."""
        with self._lock:
            recent = list(self._work_recent)
        out: dict[str, dict[str, float]] = {}
        for key in sorted({k for w in recent for k in w}):
            ordered = sorted(w.get(key, 0) for w in recent)
            out[key] = {
                "mean": round(sum(ordered) / len(ordered), 2),
                "p95": ordered[math.ceil(len(ordered) * 0.95) - 1],
                "max": ordered[-1],
            }
        return out

    def summary(self) -> dict[str, Any]:
        """누계(상태 파일용)."""
        with self._lock:
            out = self._total.as_dict()
            out["alive_tracks"] = len(self._alive)
            out["identity"]["uncertain_open"] = sum(1 for a in self._alive.values() if a.uncertain_since is not None)
            return out


def window_log_line(window: Mapping[str, Any]) -> str:
    """주기 요약 로그 한 줄(사람이 읽는 핵심만). 전체는 상태 파일의 tracking_diagnostics."""
    lag = {c: v["ratio"] for c, v in window.get("lagging", {}).items() if v.get("ticks")}
    drops = {c: dict(r) for c, r in window.get("interpolation_drops", {}).items()}
    passes = {}
    for pair, outcomes in window.get("pair_outcomes", {}).items():
        ok = outcomes.get("PASS", 0) + outcomes.get("PASS_MARGINAL", 0)
        rejected = {k: v for k, v in outcomes.items() if not k.startswith("PASS")}
        passes[pair] = {"pass": ok, **rejected}
    w = window.get("world", {})
    m = w.get("manager", {})
    lost = w.get("lost", {})
    reid_back = m.get("reid.reentry_reconnected", 0) + m.get("reid.reentry_merged", 0)
    geo_back = max(m.get("reconnect.done", 0) - m.get("reid.reentry_reconnected", 0), 0)
    # -657: 갈라짐·재입장 판독 한 줄. 확정승격(TENTATIVE→CONFIRMED), 임시소멸(확정 못 하고 LOST), 동시확정최대(실제 인원과 비교),
    # 중복쌍최대·활성병합(같은 사람 두 ID), 소실/되찾음(확정 소실 수 / 기하 재연결 + Re-ID 재입장·지연 병합)
    split_line = (
        f"확정승격 {m.get('lifecycle.confirmed', 0)}, 임시소멸 {lost.get('TENTATIVE', 0)}, "
        f"동시확정최대 {w.get('max_confirmed', 0)}, 중복쌍최대 {w.get('max_duplicate_pairs', 0)}, "
        f"활성병합 {m.get('revision.active_merge', 0)}, 그림자억제 {m.get('new_track.suppressed_disjoint_shadow', 0)}, "
        f"소실/되찾음 {lost.get('CONFIRMED', 0)}/기하 {geo_back}+ReID {reid_back}, "
        f"키거부 {m.get('reid.stature_rejected', 0)}, 키동률깨기 {m.get('reid.stature_tiebreak', 0)}"
    )
    ident = window.get("identity", {})
    confirmed_ms = ident.get("identity_ms", {}).get("CONFIRMED", {})
    total_ms = sum(confirmed_ms.values())
    unc_ms = confirmed_ms.get("IDENTITY_UNCERTAIN", 0)
    ratio = f"{unc_ms / total_ms:.3f}" if total_ms else "-"
    identity_line = (
        f"확정 불확실 비율 {ratio}(유지만 {ident.get('uncertain_hold_only_ms', 0)}ms), "
        f"단독 사유 {ident.get('uncertain_sole_ms', {})}, 로컬 트랙 이동/맞바꿈 "
        f"{ident.get('local_key_moves', 0)}/{ident.get('local_key_swaps', 0)}"
    )
    work = window.get("work", {})
    work_line = {k: f"{v:g}/{work.get('max', {}).get(k, 0)}" for k, v in work.get("mean", {}).items()}
    return (
        f"추적 계측 {window.get('span_ms', 0) / 1000:.0f}s: tick {window.get('ticks', 0)}, {identity_line}, "
        f"늦은 카메라 비율 {lag}, 보간 view {window.get('views', {})}, 보간 탈락 {drops}, 쌍 {passes}, "
        f"클러스터 카메라 수 {window.get('cluster_cameras', {})}, 갱신 Track 카메라 수 {w.get('updated_cameras', {})}, "
        f"새 ID {w.get('manager', {}).get('new_track.created', 0)}, LOST {w.get('lost', {})}, 병합 {w.get('merged_away', 0)}, "
        f"재연결 {w.get('reassociated', 0)}, {split_line}, 수명 {w.get('lifespan_ms', {})}, 분기 {w.get('manager', {})}, "
        f"교차 시점 계산량(tick 평균/최대) {work_line}"
    )
