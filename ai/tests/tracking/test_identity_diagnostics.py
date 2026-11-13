"""World 신원 계측(S15P31A101-684, simai_ai.tracking.diagnostics)이 World Track 시간 기준으로 사유·유지·구간·맞바꿈을 센다.

스냅샷을 직접 만들어 넣는다(판정 경로는 test_tracking_diagnostics 의 SIM 장면이 본다).
- 유지만으로 남은 불확실: 10 tick 마다 한 번 사유가 붙어도 1초 유지로 내내 불확실인 경우를 시간으로 분해한다.
- 단독·겹친 사유 시간, 불확실 구간이 끝난 원인(CLEARED·LOST·MERGED_AWAY), 로컬 트랙 이동·맞바꿈.
"""

from __future__ import annotations

from simai_ai.tracking.diagnostics import TrackingDiagnostics, window_log_line
from simai_ai.tracking.world_track import (
    IdentityConfidence as C,
)
from simai_ai.tracking.world_track import (
    IdentityFlag as F,
)
from simai_ai.tracking.world_track import (
    TrackLifecycle as L,
)
from simai_ai.tracking.world_track import (
    WorldTrackFrame,
    WorldTrackSnapshot,
)

T0 = 1_800_000_000_000
TICK = 100


def snap(wid: str, t: int, *, lc: L = L.CONFIRMED, conf: C = C.HIGH, flags=(), raised=(), keys=()) -> WorldTrackSnapshot:
    return WorldTrackSnapshot(
        world_id=wid,
        t=t,
        t_last_valid=t,
        lifecycle=lc,
        identity_confidence=conf,
        identity_revision=0,
        position_xy=(0.0, 0.0),
        position_xy_sigma=(0.1, 0.1),
        velocity=(0.0, 0.0, 0.0),
        identity_flags=tuple(flags),
        identity_flags_raised=tuple(raised),
        local_keys=tuple(keys),
    )


def feed(d: TrackingDiagnostics, ticks: list[tuple[WorldTrackSnapshot, ...]]) -> None:
    for i, tracks in enumerate(ticks):
        d.observe_world(WorldTrackFrame(t=T0 + i * TICK, calibration_version="cal", tracks=tracks))


def test_ten_percent_raised_input_is_counted_as_hold_only_time() -> None:
    """정상 9 tick 마다 사유 1 tick: 유지 때문에 내내 불확실. 불확실 시간 중 90% 가 유지만으로 남은 시간이다."""
    d = TrackingDiagnostics()
    ticks = []
    for i in range(51):
        raised = (F.TWO_VIEW_COMPETITOR_JOINT,) if i % 10 == 0 else ()
        ticks.append((snap("W1", T0 + i * TICK, conf=C.IDENTITY_UNCERTAIN,
                           flags=(F.TWO_VIEW_COMPETITOR_JOINT,), raised=raised),))
    feed(d, ticks)
    ident = d.summary()["identity"]
    total = 50 * TICK
    assert ident["identity_ms"]["CONFIRMED"] == {"IDENTITY_UNCERTAIN": total}
    assert ident["uncertain_sole_ms"] == {"TWO_VIEW_COMPETITOR_JOINT": total}
    # 직전 tick 상태에 시간을 붙인다: 사유가 새로 붙은 tick(0,10,20,30,40) 뒤 5 구간만 유지가 아니다
    assert ident["uncertain_hold_only_ms"] == total - 5 * TICK
    assert ident["uncertain_raised"] == {"TWO_VIEW_COMPETITOR_JOINT": 6}
    assert ident["uncertain_open"] == 1 and ident["uncertain_episodes_ms"] == {}


def test_overlapping_reasons_count_each_but_sole_only_alone() -> None:
    d = TrackingDiagnostics()
    both = (F.CROSS_VIEW_UNCERTAIN, F.TWO_VIEW_COMPETITOR_JOINT)
    ticks = [
        (snap("W1", 0, conf=C.IDENTITY_UNCERTAIN, flags=both, raised=both),),
        (snap("W1", 0, conf=C.IDENTITY_UNCERTAIN, flags=both, raised=both),),
        (snap("W1", 0, conf=C.IDENTITY_UNCERTAIN, flags=(F.CROSS_VIEW_UNCERTAIN,)),),
        (snap("W1", 0, conf=C.HIGH),),
    ]
    feed(d, ticks)
    ident = d.summary()["identity"]
    assert ident["uncertain_reason_ms"] == {"CROSS_VIEW_UNCERTAIN": 3 * TICK, "TWO_VIEW_COMPETITOR_JOINT": 2 * TICK}
    assert ident["uncertain_sole_ms"] == {"CROSS_VIEW_UNCERTAIN": TICK}
    assert ident["uncertain_hold_only_ms"] == TICK
    assert ident["identity_ms"]["CONFIRMED"] == {"IDENTITY_UNCERTAIN": 3 * TICK}
    assert ident["uncertain_episodes_ms"] == {"CLEARED": {"<=300": 1}}
    assert ident["uncertain_open"] == 0


def test_tentative_uncertain_time_is_kept_apart_from_confirmed() -> None:
    d = TrackingDiagnostics()
    flag = (F.POSSIBLE_DUPLICATE,)
    feed(d, [(snap("W1", 0, lc=L.TENTATIVE, conf=C.IDENTITY_UNCERTAIN, flags=flag, raised=flag),)] * 4)
    ident = d.summary()["identity"]
    assert ident["identity_ms"] == {"TENTATIVE": {"IDENTITY_UNCERTAIN": 3 * TICK}}
    assert ident["uncertain_reason_ms"] == {} and ident["uncertain_raised"] == {}
    assert ident["uncertain_open"] == 0


def test_episode_end_causes_lost_and_merged_away() -> None:
    d = TrackingDiagnostics()
    u = dict(conf=C.IDENTITY_UNCERTAIN, flags=(F.LATE_REASSOCIATION,), raised=(F.LATE_REASSOCIATION,))
    ticks = [
        (snap("A", 0, **u), snap("B", 0, **u)),
        (snap("A", 0, **u), snap("B", 0, **u)),
        (snap("A", 0, lc=L.LOST, **u),),  # A 는 LOST, B 는 병합으로 사라짐
    ]
    feed(d, ticks)
    ident = d.summary()["identity"]
    assert ident["uncertain_episodes_ms"] == {"LOST": {"<=300": 1}, "MERGED_AWAY": {"<=300": 1}}
    assert ident["uncertain_open"] == 0


def test_local_key_swap_move_and_merge_handover() -> None:
    d = TrackingDiagnostics()
    ka, kb, kc = ("cam_A", 1), ("cam_B", 2), ("cam_C", 3)
    ticks = [
        (snap("A", 0, keys=(ka,)), snap("B", 0, keys=(kb,)), snap("C", 0, keys=(kc,))),
        (snap("A", 0, keys=(kb,)), snap("B", 0, keys=(ka,)), snap("C", 0, keys=(kc,))),  # A·B 맞바꿈
        (snap("A", 0, keys=(kb, kc)), snap("B", 0, keys=(ka,)), snap("C", 0)),  # C → A 한쪽 이동
        (snap("A", 0, keys=(kb, kc)), snap("D", 0, keys=(ka,))),  # B 는 병합으로 사라지고 D 가 넘겨받음
    ]
    feed(d, ticks)
    ident = d.summary()["identity"]
    assert ident["local_key_swaps"] == 1
    assert ident["local_key_moves"] == 3


def test_window_has_identity_and_log_line() -> None:
    d = TrackingDiagnostics()
    flag = (F.AMBIGUOUS_TRACK_MATCH,)
    feed(d, [(snap("W1", 0, conf=C.IDENTITY_UNCERTAIN, flags=flag, raised=flag),)] * 3)
    w = d.take_window(T0 + 3 * TICK)
    assert w["identity"]["identity_ms"]["CONFIRMED"] == {"IDENTITY_UNCERTAIN": 2 * TICK}
    line = window_log_line(w)
    assert "확정 불확실 비율 1.000" in line
    assert d.take_window(T0 + 3 * TICK)["identity"]["identity_ms"] == {}
