"""재동기화 시험용 판정 루프 흉내 (S15P31A101-81): R1 엔진 → LifecycleTracker → 저널(동기 기록).

진입점 조립(S15P31A101-171) 전이라 시험이 직접 잇는다. 판정 스레드와 저널 쓰기 스레드를 나누지 않고 append_step 으로
바로 기록한다(스냅샷 경합은 시험에서 따로 만든다).
"""

from __future__ import annotations

import uuid

from simai_ai.eventlog import EventJournal
from simai_ai.risk import (
    AckActor,
    Clock,
    LifecyclePolicy,
    LifecycleTracker,
    LocalAlert,
    Origin,
    Persistence,
    RiskEngine,
    WorldState,
    Zone,
    ZoneEntryRule,
    build_active_site_alerts,
)

GATEWAY = "tier0-01"
CONFIG = "cfg-12"
INSIDE = (3.5, 5.0)
OUTSIDE = (8.0, 8.0)
ZONE = Zone.from_json(
    {
        "zone_id": "z-04",
        "zone_version": "zone-1",
        "judge_type": "GROUND_FOOTPRINT",
        "polygon_xy": [[3.0, 4.5], [4.0, 4.5], [4.0, 5.5], [3.0, 5.5]],
        "z_range": None,
        "severity": "CRITICAL",
    }
)
# 발생은 1프레임, 해소 유지 300ms, 재개 10s (시험을 짧게)
POLICY = LifecyclePolicy(persistence=Persistence(1, 1), persistence_by_rule={}, clear_hold_ms=300)


def track_json(world_id: str, xy: tuple[float, float], t: int) -> dict:
    return {
        "world_id": world_id,
        "t_last_valid": t,
        "observation_state": "OBSERVED",
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH",
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": [0.05, 0.05],
        "pelvis_xyz": None,
        "ground_anchor_xyz": [xy[0], xy[1], 0.0],
        "support_surface_id": "floor",
        "support_surface_validity": "VALID",
        "contact_validity": "VALID",
        "z_validity": "VALID",
        "head_z": 1.7,
        "cameras": ["cam1", "cam3"],
    }


class Site:
    """판정 루프. step 의 people 은 {world_id: xy}. 빠진 사람은 World 상태에 없다(관측 상실)."""

    def __init__(self, journal: EventJournal, clock: Clock) -> None:
        self.journal = journal
        self.clock = clock
        self.engine = RiskEngine([ZoneEntryRule()], clock)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=clock, policy=POLICY)
        self.local_alerts: list[LocalAlert] = []
        self.signals: list = []
        self.risk_seen: dict[str, tuple[int, int]] = {}  # event_id → (event_sequence, 위험을 VERIFIED 로 본 촬영 시각)

    def step(self, people: dict[str, tuple[float, float]], dt: int = 100):
        self.clock.advance(dt)  # FixedClock
        t_obs = self.clock.now_ms() - 40
        world = WorldState.from_payload(
            {
                "t_latest_global": t_obs,
                "calibration_version": "cal-7",
                "scene_version": "scene-3",
                "tracks": [track_json(w, xy, t_obs) for w, xy in people.items()],
            }
        )
        evaluation = self.engine.evaluate(world, [ZONE], Origin.LIVE)
        step = self.tracker.apply(evaluation, world, config_version=CONFIG, evidence_ref=str(uuid.uuid4()))
        self.journal.append_step(step)
        self.signals.extend(step.local_signals)
        self.local_alerts.extend(s for s in step.local_signals if isinstance(s, LocalAlert))
        # 조립(-171)이 주입할 risk_observed_at 흉내: 이 판정에 발생 판정이 있고 대상이 VERIFIED 인 이벤트의 촬영 시각
        for state in self.tracker.active():
            hit = any(f.rule_id == state.rule_id and f.world_ids == state.world_ids for f in evaluation.findings)
            if hit and str(state.verification) == "VERIFIED":
                self.risk_seen[state.event_id] = (state.event_sequence, evaluation.observation_time)
        return step

    def risk_observed_at(self, event_id: str) -> tuple[int, int] | None:
        return self.risk_seen.get(event_id)

    def site_ack(self, event_id: str):
        """노트북 현장 확인 버튼(site_ack): 확인 전이를 계약 ack_id 와 함께 저널에 남긴다."""
        step = self.tracker.acknowledge(event_id, AckActor.SITE_BUTTON, "laptop-01", self.clock.now_ms(),
                                        channel="SITE_BUTTON")
        assert step is not None
        self.journal.append_step(step, ack_id=str(uuid.uuid4()))
        self.signals.extend(step.local_signals)
        return step

    def event_of(self, world_id: str) -> str:
        [state] = [s for s in self.tracker.active() if s.world_ids == (world_id,)]
        return state.event_id

    def site_alerts(self) -> list[dict]:
        """노트북으로 가는 활성 현장 경보 목록(health.active_site_alerts). EC2 연결과 관계없다."""
        return build_active_site_alerts(self.tracker.active())
