from simai_ai.risk.sound_ladder import SoundLadder


def decide(policy, eid="a", worlds=("w",), zone="z", severity="HIGH", rule="R3", at=1000, live=None):
    return policy.decide(eid, worlds, zone, severity, rule, at, {eid} if live is None else live)


def test_r3_once_and_higher_only():
    p = SoundLadder()
    assert decide(p) == "ONCE"
    assert decide(p, eid="b", rule="R1", live={"a", "b"}) == "SILENT"
    assert decide(p, eid="c", severity="CRITICAL", live={"a", "c"}) == "ONCE"
    assert decide(p, eid="d", worlds=("other",), live={"a", "c", "d"}) == "ONCE"


def test_ack_30_seconds_boundary_zone_and_escalation():
    p = SoundLadder()
    p.acknowledge(("w",), "z", "HIGH", 1000)
    assert decide(p, at=30_999) == "SILENT"
    assert decide(p, severity="CRITICAL", at=30_999) == "ONCE"
    assert decide(p, eid="b", zone="else", at=30_999, live={"b"}) == "ONCE"
    # 위에서 울린 위험(CRITICAL) 때문에 같은 사람·구역·규칙의 경고는 60초 동안 화면만.
    assert decide(p, at=31_000) == "SILENT"
    p = SoundLadder()
    p.acknowledge(("w",), "z", "HIGH", 1000)
    assert decide(p, at=31_000) == "ONCE"


def test_legacy_unknown_people_and_resolved_reset():
    p = SoundLadder()
    assert decide(p, worlds=(), rule="R1") == "REPEAT"
    assert decide(p, eid="b", worlds=(), rule="R1", live={"a", "b"}) == "REPEAT"
    assert decide(p, eid="c", live={"c"}) == "ONCE"
    assert decide(p, eid="d", live={"d"}, at=61_000) == "ONCE"


def test_cooldown_60_seconds_same_person_zone_rule():
    p = SoundLadder()
    assert decide(p, eid="a", rule="R1", at=1000) == "REPEAT"
    # 이벤트가 끝난 뒤 같은 사람·구역·규칙이 다시 걸려도 60초 안이면 화면만.
    assert decide(p, eid="b", rule="R1", at=60_999, live={"b"}) == "SILENT"
    assert decide(p, eid="c", rule="R1", at=61_000, live={"c"}) == "REPEAT"
    # 다른 사람·다른 구역·다른 규칙은 울린다.
    p = SoundLadder()
    assert decide(p, eid="a", rule="R1", at=1000) == "REPEAT"
    assert decide(p, eid="b", worlds=("other",), rule="R1", at=2000, live={"b"}) == "REPEAT"
    assert decide(p, eid="c", zone="else", rule="R1", at=2000, live={"c"}) == "REPEAT"
    assert decide(p, eid="d", rule="R3", at=2000, live={"d"}) == "ONCE"


def test_cooldown_escalation_and_critical_always_ring():
    p = SoundLadder()
    assert decide(p, eid="a", severity="CAUTION", rule="R1", at=1000) == "REPEAT"
    assert decide(p, eid="b", severity="HIGH", rule="R1", at=2000, live={"b"}) == "REPEAT"
    assert decide(p, eid="c", severity="HIGH", rule="R1", at=3000, live={"c"}) == "SILENT"
    p = SoundLadder()
    assert decide(p, eid="a", severity="CRITICAL", rule="R2", at=1000) == "REPEAT"
    assert decide(p, eid="b", severity="CRITICAL", rule="R2", at=2000, live={"b"}) == "REPEAT"
    p.acknowledge(("w",), "z", "CRITICAL", 3000)
    assert decide(p, eid="c", severity="CRITICAL", rule="R2", at=4000, live={"c"}) == "REPEAT"


def test_fall_rings_immediately_over_same_person_alarm():
    # 사용자 결정 2026-11-23(-861): 같은 사람의 진입(R1) 소리가 나는 중이어도 낙상(R4)은 새로 울린다.
    p = SoundLadder()
    assert decide(p, eid="r1", severity="CRITICAL", rule="R1", at=1000) == "REPEAT"
    assert decide(p, eid="r4", severity="CRITICAL", rule="R4", at=2000, live={"r1", "r4"}) == "REPEAT"
    # 같은 사람에게 열린 다른 낙상(ID 갈라짐 등)에도 막히지 않는다(-860 워치와 같은 기준).
    assert decide(p, eid="r4b", severity="CRITICAL", rule="R4", at=2500, live={"r1", "r4", "r4b"}) == "REPEAT"


def test_fall_still_groups_lower_alarms_and_rings_once():
    p = SoundLadder()
    assert decide(p, eid="r4", severity="CRITICAL", rule="R4", at=1000) == "REPEAT"
    # R4 가 열린 동안 같은 사람의 낮은·같은 등급 경보는 그대로 묶인다.
    assert decide(p, eid="r5", severity="CAUTION", rule="R5", at=2000, live={"r4", "r5"}) == "SILENT"
    assert decide(p, eid="r1", severity="CRITICAL", rule="R1", at=2000, live={"r4", "r1"}) == "SILENT"
    # 같은 R4 이벤트를 다시 판단해도 한 번만 울린다.
    assert decide(p, eid="r4", severity="CRITICAL", rule="R4", at=3000, live={"r4"}) == "SILENT"
