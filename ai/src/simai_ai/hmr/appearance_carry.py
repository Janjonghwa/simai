"""아바타 외형 이어받기 판정 (S15P31A101-743, docs/design/S15P31A101-743-avatar-appearance-continuity.md).

**표시 전용이다.** World ID·위치·위험 판정은 Tier 0 가 정본이고(AGENTS 규칙 1) 여기서 만들거나 바꾸지 않는다. 외형 색으로
신원을 정하지 않는다(재식별 아님, 규칙 8): 판정 입력은 world_state 의 위치·시각·키(머리 높이 − 접지 높이)뿐이고, 결과는
"새 World ID 의 아바타 외형을 어떤 누적에서 시작할지"만 정한다. 잘못 이어받으면 다른 사람의 옷 색이 잠깐 보이고(새 관측이
덮는다) 안전 판단에는 영향이 없다.

기록하는 것:
- 아바타 루프가 본 World ID 마다 처음 본 시각·위치, 마지막 측정 시각(t_last_valid)·위치, 기립 키 추정(관측 최댓값).
- 아바타 루프에서 빠진(LOST 또는 world_state 에서 사라짐, 흡수 포함) ID 의 상태(`Departed`, 내용은 호출자 몫이라 보지 않음).

이어받기 조건(모두 맞아야 한다):
1. 받는 쪽: 아바타 루프가 새로 본 World ID(`receiver`)이고 아직 정하지 않았다. 분할로 갈라져 나간 ID(split_from)는 아니다.
2. 시간: 받는 쪽을 처음 본 시각 − 주는 쪽 마지막 측정 시각이 −`overlap_ms` ~ `window_ms` 안.
3. 거리: 처음 위치와 주는 쪽 마지막 위치의 수평 거리가 min(`gate_base_m` + `max_speed_mps` × 공백, `radius_max_m`) 안
   (World Track Manager 의 사람이 갈 수 있는 거리 게이트와 같은 식·값).
4. 그 받는 쪽에 닿는 주는 쪽이 하나, 그 주는 쪽에 닿는 받는 쪽도 하나.
5. 주는 쪽 마지막 위치 `crowd_radius_m` 안에 다른 살아 있는 사람(받는 쪽 제외)과 다른 소실 ID 가 없다.
6. 두 쪽 키를 다 알면 차이가 `height_tolerance_m` 이하.
하나라도 어긋나면 이어받지 않고(새 ID 외형은 처음부터) 사유를 `stats` 에 센다. 표준 라이브러리만 쓴다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class CarryConfig:
    """수치 근거는 설계 문서 '수치'. 거리 게이트 값은 WorldTrackConfig 기본값과 같다(tests 가 같음을 확인)."""

    window_ms: int = 3000
    """주는 쪽 마지막 측정 뒤 받는 쪽이 처음 보이기까지 허용 공백. World Track 의 짧은 재연결 창(lost_after_ms)과 같다"""
    overlap_ms: int = 300
    """받는 쪽이 주는 쪽 마지막 측정보다 이만큼 먼저 보여도 받는다(새 ID 가 옛 ID 의 마지막 측정과 겹쳐 생기는 경우).
    TENTATIVE 최대 공백(tentative_max_gap_ms)과 같다"""
    gate_base_m: float = 0.5
    max_speed_mps: float = 2.0
    radius_max_m: float = 1.5
    crowd_radius_m: float = 1.5
    height_tolerance_m: float = 0.15
    keep_ms: int = 10_000
    """소실 ID 상태를 들고 있는 시간(빠진 뒤 World 시각). 같은 ID 늦은 재연결 창(reconnect_window_ms)과 같다"""
    max_departed: int = 16
    """들고 있는 소실 ID 수 상한(텍스처 누적 사본 메모리 상한). 넘으면 오래된 것부터 버린다"""

    def __post_init__(self):
        if (self.window_ms < 0 or self.overlap_ms < 0 or self.gate_base_m < 0 or self.max_speed_mps < 0
                or self.radius_max_m < self.gate_base_m or self.crowd_radius_m < 0 or self.height_tolerance_m < 0
                or self.keep_ms < self.window_ms or self.max_departed < 1):
            raise ValueError("외형 이어받기 설정 범위 오류")

    def reach_m(self, gap_ms: float) -> float:
        return min(self.gate_base_m + self.max_speed_mps * max(gap_ms, 0) / 1000, self.radius_max_m)


@dataclass
class Seen:
    first_t: int
    first_xy: tuple[float, float]
    last_t: int
    last_xy: tuple[float, float]
    height: float | None = None


@dataclass
class Departed:
    world_id: str
    seen: Seen
    departed_t: int
    payload: Any
    donor: bool
    """새 ID 에 외형을 줄 수 있는가(호출자가 정한다: 보낸 아바타 자산이 있음)"""


def standing_height(track: Mapping[str, Any]) -> float | None:
    """머리 꼭대기 − 접지 높이(m). VALID 머리 높이와 접지점이 모두 있을 때만, 아니면 모른다(None, 지어내지 않음)."""
    head = track.get("head_z")
    validity = track.get("head_z_validity", "VALID" if head is not None else None)
    anchor = track.get("ground_anchor_xyz")
    if validity != "VALID" or not isinstance(head, (int, float)) or not anchor or len(anchor) < 3:
        return None
    height = float(head) - float(anchor[2])
    return height if math.isfinite(height) and height > 0 else None


def _xy(track: Mapping[str, Any]) -> tuple[float, float] | None:
    xy = track.get("position_xy")
    if not xy or len(xy) < 2:
        return None
    x, y = float(xy[0]), float(xy[1])
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class AppearanceCarry:
    def __init__(self, config: CarryConfig | None = None):
        self.config = config or CarryConfig()
        self.seen: dict[str, Seen] = {}
        self.departed: dict[str, Departed] = {}
        self.pending: set[str] = set()
        self.stats: Counter = Counter()

    def observe(self, tracks: Iterable[Mapping[str, Any]], t: int) -> None:
        """LOST 가 아닌 트랙의 처음·마지막 위치와 키 최댓값을 적는다."""
        for tr in tracks:
            if tr.get("track_lifecycle") == "LOST":
                continue
            wid, xy = tr.get("world_id"), _xy(tr)
            if not isinstance(wid, str) or xy is None:
                continue
            last_t = tr.get("t_last_valid")
            last_t = int(last_t) if isinstance(last_t, int) and not isinstance(last_t, bool) else int(t)
            height = standing_height(tr)
            seen = self.seen.get(wid)
            if seen is None:
                self.seen[wid] = Seen(int(t), xy, last_t, xy, height)
                continue
            if last_t >= seen.last_t:
                seen.last_t, seen.last_xy = last_t, xy
            if height is not None and (seen.height is None or height > seen.height):
                seen.height = height

    def depart(self, world_id: str, payload: Any, t: int, *, donor: bool) -> None:
        seen = self.seen.pop(world_id, None)
        self.pending.discard(world_id)
        if seen is None:
            return
        self.departed[world_id] = Departed(world_id, seen, int(t), payload, donor)
        while len(self.departed) > self.config.max_departed:
            oldest = min(self.departed.values(), key=lambda d: d.departed_t)
            del self.departed[oldest.world_id]
            self.stats["dropped_capacity"] += 1

    def take(self, world_id: str) -> Any:
        """병합·같은 ID 재연결 때 그 ID 의 상태를 꺼낸다(이어받기 후보에서 빠진다). 없으면 None."""
        gone = self.departed.pop(world_id, None)
        return None if gone is None else gone.payload

    def receiver(self, world_id: str) -> None:
        self.pending.add(world_id)

    def _reaches(self, seen: Seen, gone: Departed) -> bool:
        gap = seen.first_t - gone.seen.last_t
        if not -self.config.overlap_ms <= gap <= self.config.window_ms:
            return False
        return _dist(seen.first_xy, gone.seen.last_xy) <= self.config.reach_m(gap)

    def decide(self, tracks: Iterable[Mapping[str, Any]], t: int) -> list[tuple[str, Any]]:
        """이번에 이어받기로 정한 (받는 World ID, 주는 쪽 payload). 정한(이어받음·거부) 받는 쪽은 다시 보지 않는다."""
        cfg = self.config
        tracks = [tr for tr in tracks if tr.get("track_lifecycle") != "LOST" and isinstance(tr.get("world_id"), str)]
        live = {tr["world_id"]: _xy(tr) for tr in tracks}
        # 분할로 갈라져 나간 ID 는 원래 ID 외형을 물려받지 않는다(명세 20.3, -661 과 같음)
        for tr in tracks:
            if tr.get("split_from") and tr["world_id"] in self.pending:
                self.pending.discard(tr["world_id"])
                self.stats["rejected_split_child"] += 1
        receivers = sorted(w for w in self.pending if w in live and w in self.seen)
        donors = [d for d in self.departed.values() if d.donor]
        links = {r: [d for d in donors if d.world_id != r and self._reaches(self.seen[r], d)] for r in receivers}
        accepted, decided = [], set()
        for r in receivers:
            cands = links[r]
            if not cands:
                continue   # 주는 쪽이 아직 빠지지 않았을 수 있다(가림 → LOST 는 3 s 뒤): 기한까지 기다린다
            decided.add(r)
            if len(cands) > 1:
                self.stats["rejected_ambiguous_donor"] += 1
                continue
            gone = cands[0]
            if any(gone in links[o] for o in receivers if o != r):
                self.stats["rejected_ambiguous_receiver"] += 1
                continue
            crowd_live = any(o != r and xy is not None and _dist(xy, gone.seen.last_xy) <= cfg.crowd_radius_m
                             for o, xy in live.items())
            crowd_lost = any(d is not gone and abs(d.seen.last_t - gone.seen.last_t) <= cfg.window_ms
                             and _dist(d.seen.last_xy, gone.seen.last_xy) <= cfg.crowd_radius_m
                             for d in self.departed.values())
            if crowd_live or crowd_lost:
                self.stats["rejected_crowd"] += 1
                continue
            mine, theirs = self.seen[r].height, gone.seen.height
            if mine is not None and theirs is not None and abs(mine - theirs) > cfg.height_tolerance_m:
                self.stats["rejected_height"] += 1
                continue
            del self.departed[gone.world_id]
            self.stats["carried"] += 1
            accepted.append((r, gone.payload))
        self.pending -= decided
        # 주는 쪽 마지막 측정은 받는 쪽을 처음 본 뒤 overlap_ms 까지이고, 가림 → LOST 는 그 뒤 lost_after_ms(= window_ms)라
        # 처음 본 뒤 overlap + window (+ 루프 주기 여유 1 s) 안에 주는 쪽이 없으면 처음부터 쌓는다
        horizon = cfg.overlap_ms + cfg.window_ms + 1000
        for r in list(self.pending):
            seen = self.seen.get(r)
            if seen is None or r not in live:
                self.pending.discard(r)
            elif t - seen.first_t > horizon:
                self.pending.discard(r)
                self.stats["no_donor"] += 1
        return accepted

    def expire(self, t: int) -> None:
        for wid in [w for w, d in self.departed.items() if t - d.departed_t > self.config.keep_ms]:
            del self.departed[wid]
