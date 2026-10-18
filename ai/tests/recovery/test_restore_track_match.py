"""재시작 뒤 복원 Track 매칭과 이벤트 연결 (명세 3.2 재시작 복구·부록 C, 부록 D-G3-09, 15.1 G3, S15P31A101-54).

완료 조건: 재시작할 때 (1) 같은 위험의 이벤트가 연결 없이 다시 생기는 경우, (2) 활성 이벤트 자동 해소, (3) 복원 위치를
OBSERVED 로 처리하는 경우가 모두 0건이다. 재시작 전 Tier 0(세션 pre)에서 World Track → world_state → Risk 생명주기로 R1
이벤트를 만들고, 그 Track·이벤트를 체크포인트처럼 넘겨 재시작 뒤 Tier 0(세션 post)를 돌린다. 마지막 시험은 실제
저널·체크포인트·recover 를 거친다. 모든 입력은 손으로 만든 SIM 이다.
"""

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from simai_ai.recovery import RestoredEventLinker
from simai_ai.risk import (
    AlertClear,
    Evaluation,
    Finding,
    FixedClock,
    IdentityCorrection,
    Lifecycle,
    LifecycleTracker,
    Origin,
    Severity,
    Verification,
    WorldState,
)
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import IdentityConfidence, IdentityFlag, WorldIdIssuer, WorldTrackManager

_spec = importlib.util.spec_from_file_location(
    "_wt_helpers_relink", Path(__file__).parents[1] / "tracking" / "test_world_track.py"
)
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT = wt.T, wt.DT
GATEWAY = "tier0-01"
CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


@pytest.fixture(scope="module")
def schema_errors():
    registry = Registry()
    schemas = {}
    for p in sorted(CONTRACTS.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(s["$id"], Resource.from_contents(s))
        schemas[p.name.removesuffix(".schema.json")] = s

    def errors(name, doc):
        return [
            f"{list(e.path)}: {e.message}"
            for e in Draft202012Validator(schemas[name], registry=registry).iter_errors(doc)
        ]

    return errors


def in_zone(tr):
    """구역 z-01 = x < 3.5 m (골반 XY, 위치를 갱신한 Track 만)."""
    return "z-01" if tr.updated and tr.position_xy[0] < 3.5 else None


class Tier0:
    """World Track Manager → world_state → LifecycleTracker. zone_of(track) 가 구역을 돌려주면 R1 HIGH 판정을 낸다."""

    def __init__(self, session, clock, *, tracker=None, zone_of=in_zone, linker=None):
        self.clock = clock
        self.manager = WorldTrackManager(WorldIdIssuer(session))
        self.tracker = tracker or LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=clock)
        self.zone_of = zone_of
        self.linker = linker
        self.transitions = []
        self.signals = []
        self.frames = []
        self.payloads = []

    def tick(self, rig, t, people):
        """people: (이름, x, y) 들. 모두 네 카메라에 보인다(로컬 트랙 ID 는 이름으로)."""
        clusters = [wt._cluster(rig, name, t, x, y, prefix=name) for name, x, y in people]
        frame = self.manager.update(wt._frame(rig, t, clusters), rig)
        for rev in frame.revisions:
            self.transitions += self.tracker.correct_identity(rev.to_correction()).transitions
        payload = world_state_payload(frame, frame.t, "scene-1")
        world = WorldState.from_payload(payload)
        findings = []
        for tr in frame.tracks:
            zone = self.zone_of(tr)
            if zone is not None:
                findings.append(
                    Finding(
                        rule_id="R1",
                        rule_version=1,
                        severity=Severity.HIGH,
                        world_ids=(tr.world_id,),
                        zone_id=zone,
                        reasons=("xy_inside_polygon",),
                    )
                )
        self.clock.set(frame.t + 50)
        evaluation = Evaluation(
            origin=Origin.LIVE,
            evaluated_at=self.clock.now_ms(),
            observation_time=world.t_latest_global,
            calibration_version=world.calibration_version,
            scene_version=world.scene_version,
            rules=(),
            zones=(),
            findings=tuple(sorted(findings, key=lambda f: f.sort_key)),
        )
        related = None if self.linker is None else self.linker.for_frame(frame, self.manager.restore_status)
        step = self.tracker.apply(evaluation, world, config_version="cfg-1", evidence_ref=MSG_ID, related=related)
        self.transitions += step.transitions
        self.signals += step.local_signals
        self.frames.append(frame)
        self.payloads.append(payload)
        return frame, step

    def raised(self):
        return [t for t in self.transitions if t.transition is Lifecycle.RAISED]

    def resolved(self):
        return [t for t in self.transitions if t.transition is Lifecycle.RESOLVED]

    def clears(self):
        return [s for s in self.signals if isinstance(s, AlertClear)]


def before_restart(rig, people, ticks=10):
    """재시작 전 Tier 0: people 를 ticks 동안 보고 R1 이벤트를 연다. (Track 체크포인트, tracks_t, 활성 이벤트)."""
    pre = Tier0("pre", FixedClock(T))
    for k in range(ticks):
        pre.tick(rig, T + k * DT, people)
    events = pre.tracker.active()
    assert events and all(e.local_alerted for e in events)
    by_name = {}
    for name, x, y in people:
        (tr,) = [
            s for s in pre.frames[-1].tracks if abs(s.position_xy[0] - x) < 0.05 and abs(s.position_xy[1] - y) < 0.05
        ]
        by_name[name] = tr.world_id
    return pre.manager.export_tracks(), pre.manager.last_t, events, by_name


def restart(checkpoint_tracks, tracks_t, events, *, zone_of=in_zone):
    """재시작 뒤 Tier 0: 활성 이벤트를 UNVERIFIABLE 로 복원하고 Track 을 후보로 넣는다(-171 배선 순서)."""
    clock = FixedClock(tracks_t + 10)
    tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=clock)
    tracker.restore(events)
    post = Tier0("post", clock, tracker=tracker, zone_of=zone_of, linker=RestoredEventLinker(tracker.active()))
    post.manager.restore(checkpoint_tracks, tracks_t)
    return post


def event_of(events, world_id):
    (e,) = [e for e in events if e.world_ids == (world_id,)]
    return e


# ---------------------------------------------------------------- 같은 위험이 재시작 직후 다시 생김


def test_same_risk_right_after_restart_links_previous_event(rig, schema_errors):
    """A 가 구역 안에서 R1 경보 중 Tier 0 가 재시작(5 s)했다. A 가 같은 자리에서 새 ID 로 재관측되고 R1 이 다시 걸리면
    새 이벤트에 이전 이벤트 연결(related_event_id)을 남긴다. 이전 이벤트는 UNVERIFIABLE 로 남고 해소되지 않는다."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 5000
    for k in range(40):
        post.tick(rig, start + k * DT, [("a2", 3.05, 3.0)])
    (raised,) = post.raised()
    assert raised.state.related_event_id == previous.event_id
    assert raised.state.world_ids[0].startswith("w-post-")
    payload = raised.to_payload()
    assert payload["related_event_id"] == previous.event_id
    payload = {k: v for k, v in payload.items() if not (k == "ack" and v is None)}  # outbox 처럼 빈 선택 필드를 뺀다
    message = {
        "schema_version": "1.4.0",
        "msg_type": "risk_event_transition",
        "msg_id": MSG_ID,
        "source_id": GATEWAY,
        "sent_at": start + 1000,
        "payload": payload | {"journal_offset": 7, "resent": False},
    }
    assert schema_errors("risk_event_transition", message) == []
    kept = post.tracker.get(previous.event_id)
    assert kept is not None and kept.verification is Verification.UNVERIFIABLE
    assert post.resolved() == [] and post.clears() == []


def test_world_state_after_restart_never_contains_restored_position(rig, schema_errors):
    """복원 위치가 OBSERVED 로 새는 경로 0건: 재시작 뒤 world_state 에 재시작 전 World ID 는 없고, 모든 Track 의 위치는
    재시작 뒤 자기 관측이다(t_last_valid ≥ 첫 프레임)."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 2000
    for k in range(20):
        post.tick(rig, start + k * DT, [("a2", 3.0, 3.0)] if k < 8 else [])
    for payload in post.payloads:
        assert ids["a"] not in {d["world_id"] for d in payload["tracks"]}
        for d in payload["tracks"]:
            assert d["t_last_valid"] >= start
            if d["observation_state"] == "OBSERVED":
                assert payload["t_latest_global"] - d["t_last_valid"] <= 200
        message = world_state_message(payload, msg_id=MSG_ID, sent_at=payload["t_latest_global"] + 20)
        assert schema_errors("world_state", message) == []
    assert post.payloads[-1]["tracks"][0]["observation_state"] == "UNKNOWN"  # 가려진 뒤 복원 위치로 채우지 않는다


# ---------------------------------------------------------------- 다른 사람이 같은 자리에 나타남


def _other_person_scene(rig, wrong_link):
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 1000
    frames = []
    for k in range(600):  # 60 s, B 는 구역 밖(3.7, 3.0) 에 계속 보인다
        frame, _ = post.tick(rig, start + k * DT, [("b", 3.7, 3.0)])
        frames.append(frame)
        if wrong_link and k == 0:
            # 음성 대조: 위치만으로 B 를 A 로 이었다면(이벤트를 B 로 옮김)
            b = frame.tracks[0].world_id
            post.transitions += post.tracker.correct_identity(IdentityCorrection(ids["a"], b, 1)).transitions
    return post, previous, frames


def test_other_person_at_restored_spot_does_not_clear_previous_alarm(rig):
    """재시작 뒤 A 의 자리 옆(0.7 m, 구역 밖)에 다른 사람 B 가 나타나 60 s 동안 보인다. B 는 새 ID 이고 A 의 경보는
    해소되지 않는다(AlertClear 없음). B 는 A 의 복원 Track 이 남는 동안 IDENTITY_UNCERTAIN 이다."""
    post, previous, frames = _other_person_scene(rig, wrong_link=False)
    assert post.resolved() == [] and post.clears() == []
    kept = post.tracker.get(previous.event_id)
    assert kept is not None and kept.world_ids == previous.world_ids and kept.verification is Verification.UNVERIFIABLE
    b = frames[0].tracks[0].world_id
    assert b != previous.world_ids[0] and post.raised() == []
    assert IdentityFlag.RESTORED_TRACK_CANDIDATE in frames[0].track(b).identity_flags
    assert frames[0].track(b).identity_confidence is IdentityConfidence.IDENTITY_UNCERTAIN
    assert frames[-1].track(b).identity_confidence is IdentityConfidence.HIGH  # 후보가 끝난 뒤


def test_other_person_scene_would_clear_alarm_if_linked_by_position(rig):
    """음성 대조: 같은 장면에서 B 를 위치만으로 A 로 이으면 A 의 경보가 B 의 관측으로 해소된다. 위 시험이 '잇지 않음'을
    실제로 검사한다는 뜻이다."""
    post, previous, _ = _other_person_scene(rig, wrong_link=True)
    assert [t.event_id for t in post.resolved()] == [previous.event_id]
    assert [s.event_id for s in post.clears()] == [previous.event_id]


# ---------------------------------------------------------------- 30초 규칙


@pytest.mark.parametrize("gap_ms, track_based", [(29_900, True), (30_100, False)])
def test_checkpoint_age_boundary_keeps_link_without_track_restore(rig, gap_ms, track_based):
    """29.9 s 체크포인트는 복원 Track 후보로, 30.1 s 체크포인트는 Track 없이(같은 규칙·같은 구역) 이전 이벤트와 잇는다.
    어느 쪽도 이전 이벤트를 해소하지 않는다. 30.1 s 면 새 사람은 복원 Track 후보가 아니다(IDENTITY_UNCERTAIN 아님)."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + gap_ms
    frames = [post.tick(rig, start + k * DT, [("a2", 3.0, 3.0)])[0] for k in range(10)]
    (raised,) = post.raised()
    assert raised.state.related_event_id == previous.event_id
    first = frames[0].tracks[0]
    assert (first.restored_candidates == (ids["a"],)) is track_based
    assert (IdentityFlag.RESTORED_TRACK_CANDIDATE in first.identity_flags) is track_based
    assert post.resolved() == [] and post.tracker.get(previous.event_id) is not None


def test_link_window_closes_30s_after_first_frame(rig):
    """연결은 재시작 뒤 첫 프레임부터 30 s 까지만 한다. 그 뒤 같은 구역의 새 위험은 다른 일로 본다(연결 없음)."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 1000
    post.tick(rig, start, [])  # 첫 프레임 (아무도 없음)
    for k in range(1, 10):
        post.tick(rig, start + 30_000 + k * DT, [("c", 3.0, 3.0)])
    (raised,) = post.raised()
    assert raised.state.related_event_id is None
    assert post.tracker.get(previous.event_id) is not None and post.resolved() == []


def test_unreachable_person_is_not_linked_even_in_same_zone(rig):
    """짧은 재시작(0.3 s)에서 A 의 복원 Track 이 갈 수 없는 곳(2 m)에 처음 나타난 사람은 A 가 아니다. 같은 구역이어도
    잇지 않는다(위치를 모를 때의 같은 규칙·구역 연결은 복원 Track 이 없을 때만)."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 300
    frames = [post.tick(rig, start + k * DT, [("c", 1.0, 3.0)])[0] for k in range(10)]
    assert frames[0].tracks[0].restored_excluded == (ids["a"],)
    (raised,) = post.raised()
    assert raised.state.related_event_id is None


# ---------------------------------------------------------------- 두 복원 Track 이 한 사람을 다툼


def test_two_restored_tracks_contest_one_person(rig):
    """A(3.0, 3.0)·C(3.0, 3.8) 둘 다 R1 경보 중 재시작. 새 사람 N 이 (3.05, 3.1) 에 나타나 R1 이 걸리면 가까운 A 의
    이벤트와 잇는다. 어느 이전 이벤트도 옮기거나 해소하지 않는다. 뒤이어 C 자리의 M 은 C 의 이벤트와 잇는다."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0), ("c", 3.0, 3.8)])
    ea, ec = event_of(events, ids["a"]), event_of(events, ids["c"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 1000
    frames = [post.tick(rig, start + k * DT, [("n", 3.05, 3.1)])[0] for k in range(10)]
    assert frames[0].tracks[0].restored_candidates == (ids["a"], ids["c"])
    for k in range(10, 20):
        post.tick(rig, start + k * DT, [("n", 3.05, 3.1), ("m", 3.0, 3.9)])
    related = {t.state.world_ids[0]: t.state.related_event_id for t in post.raised()}
    n = frames[0].tracks[0].world_id
    (m,) = set(related) - {n}
    assert related == {n: ea.event_id, m: ec.event_id}
    assert {e.event_id for e in post.tracker.active()} >= {ea.event_id, ec.event_id}
    assert post.tracker.get(ea.event_id).world_ids == ea.world_ids
    assert post.tracker.get(ec.event_id).world_ids == ec.world_ids
    assert post.resolved() == []


# ---------------------------------------------------------------- 복원 Track 이 끝내 재관측되지 않음


def test_restored_track_never_reobserved_keeps_event_unverifiable(rig):
    """A 가 재시작 뒤 끝내 보이지 않는다(60 s). A 의 이벤트는 UNVERIFIABLE 로 남고 해소·AlertClear 가 없다. 복원 Track
    후보는 30 s 뒤 사라지고 연결 시간도 닫힌다."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 1000
    for k in range(0, 600, 5):
        post.tick(rig, start + k * DT, [])
    kept = post.tracker.get(previous.event_id)
    assert kept is not None and kept.verification is Verification.UNVERIFIABLE and kept.lifecycle is Lifecycle.UPDATED
    assert post.resolved() == [] and post.clears() == [] and post.raised() == []
    assert post.manager.restored_alive == ()
    assert post.linker.for_frame(post.frames[-1], post.manager.restore_status) is None


# ---------------------------------------------------------------- 연결 함수 단위


def test_linker_ignores_other_rules_and_zone_events(rig):
    """다른 규칙의 새 이벤트, 구역 대상 키는 잇지 않는다. LIVE 아닌·해소된 이벤트는 받지 않는다."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    post = restart(tracks, tracks_t, events)
    frame, _ = post.tick(rig, tracks_t + 1000, [("a2", 3.0, 3.0)])
    fn = post.linker.for_frame(frame, post.manager.restore_status)
    n = frame.tracks[0].world_id
    from simai_ai.risk import EventKey

    r1 = Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=(n,), zone_id="z-01", reasons=())
    r2 = Finding(rule_id="R2", rule_version=1, severity=Severity.HIGH, world_ids=(n,), zone_id="z-01", reasons=())
    assert fn(EventKey.of(r1), r1) == event_of(events, ids["a"]).event_id
    assert fn(EventKey.of(r2), r2) is None
    assert fn(EventKey("R1", (), "z-01"), r1) is None
    assert fn(EventKey("R1", ("w-post-9999",), None), r1) is None  # 이번 시각에 없는 사람
    from dataclasses import replace

    with pytest.raises(ValueError, match="LIVE"):
        RestoredEventLinker([replace(events[0], origin=Origin.REPLAY)])
    with pytest.raises(ValueError, match="해소"):
        RestoredEventLinker([replace(events[0], lifecycle=Lifecycle.RESOLVED)])


LINK_A = "01920000-0000-7000-8000-00000000000a"
LINK_C = "01920000-0000-7000-8000-00000000000c"


_SAMPLE_TRACK = json.loads((CONTRACTS / "examples" / "world_state.json").read_text(encoding="utf-8"))["payload"][
    "tracks"
][0]


class _Direct:
    """LifecycleTracker 만 직접 돌린다(기본 정책: 발생 지속 조건 3프레임 중 2)."""

    def __init__(self):
        self.clock = FixedClock(T)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id=GATEWAY, clock=self.clock)
        self.k = 0

    def step(self, subjects, related=None, observed=()):
        """observed: 이번 시각 OBSERVED·신원 확실로 보이는 World ID (없으면 관측 상실 안전 검사가 바로 발생시킨다)."""
        t = T + self.k * DT
        self.k += 1
        tracks = []
        for w in sorted(observed):
            doc = json.loads(json.dumps(_SAMPLE_TRACK))
            doc.update(world_id=w, t_last_valid=t, observation_state="OBSERVED", identity_confidence="HIGH")
            tracks.append(doc)
        world = WorldState.from_payload(
            {"t_latest_global": t, "calibration_version": "c", "scene_version": "s", "tracks": tracks}
        )
        findings = tuple(
            Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=(w,), zone_id="z", reasons=())
            for w in sorted(subjects)
        )
        self.clock.set(t + 10)
        ev = Evaluation(
            origin=Origin.LIVE,
            evaluated_at=t + 10,
            observation_time=t,
            calibration_version="c",
            scene_version="s",
            rules=(),
            zones=(),
            findings=findings,
        )
        return self.tracker.apply(ev, world, config_version="cfg", evidence_ref=MSG_ID, related=related)


def _summary(steps):
    raised = sorted(
        (t.state.world_ids, t.state.related_event_id)
        for s in steps
        for t in s.transitions
        if t.transition is Lifecycle.RAISED
    )
    alerts = sorted(type(sig).__name__ + ":" + ",".join(sig.world_ids) for s in steps for sig in s.local_signals)
    return raised, alerts


def test_lifecycle_related_hook_is_called_once_per_pending_key_and_only_for_raise():
    """연결 함수는 활성 이벤트가 없는 키에만, 지속 조건을 세는 동안 한 번 부른다. 발생한 뒤 갱신에는 쓰지 않는다."""
    d = _Direct()
    calls = []

    def related(key, finding):
        calls.append(key.world_ids)
        return LINK_A

    steps = [d.step({"w-x-0001"}, related) for _ in range(5)]
    (state,) = d.tracker.active()
    assert state.related_event_id == LINK_A and calls == [("w-x-0001",)]
    assert _summary(steps)[0] == [(("w-x-0001",), LINK_A)]


def test_related_hook_failure_in_same_tick_does_not_drop_other_events_or_alerts(caplog):
    """리뷰 M2: 같은 tick 에 세 키가 동시에 발생하고 그중 한 키에서 연결 함수가 예외를, 다른 키에서 잘못된 값을 낸다.
    판정·현장 경보는 모두 그대로 나오고(연결 없음으로 낮춤), 정상 키의 연결만 실린다. 로그를 남긴다. 연결 함수가 없을
    때와 전이·신호 구성이 같다."""

    def related(key, finding):
        if key.world_ids == ("w-b",):
            raise RuntimeError("hook boom")
        if key.world_ids == ("w-c",):
            return ""
        return LINK_A

    hooked, plain = _Direct(), _Direct()
    with caplog.at_level("WARNING", logger="simai_ai.risk.lifecycle"):
        got = [hooked.step({"w-a", "w-b", "w-c"}, related) for _ in range(3)]
    base = [plain.step({"w-a", "w-b", "w-c"}) for _ in range(3)]
    raised, alerts = _summary(got)
    assert raised == [(("w-a",), LINK_A), (("w-b",), None), (("w-c",), None)]
    assert alerts == ["LocalAlert:w-a", "LocalAlert:w-b", "LocalAlert:w-c"]
    assert [r[0] for r in raised] == [r[0] for r in _summary(base)[0]] and alerts == _summary(base)[1]
    assert [(e.world_ids, e.event_sequence, e.local_alerted) for e in hooked.tracker.active()] == [
        (e.world_ids, e.event_sequence, e.local_alerted) for e in plain.tracker.active()
    ]
    assert "hook boom" in caplog.text or "실패" in caplog.text
    assert "잘못된 값" in caplog.text


@pytest.mark.parametrize("bad", ["", 123, b"x"])
def test_related_hook_bad_values_become_no_link(bad):
    d = _Direct()
    steps = [d.step({"w-a"}, lambda key, finding, bad=bad: bad) for _ in range(3)]
    assert _summary(steps) == ([(("w-a",), None)], ["LocalAlert:w-a"])


def test_multiple_keys_raise_together_each_with_own_link():
    """여러 키가 같은 tick 에 발생하면 각자 처음 얻은 연결을 싣는다(섞이지 않음)."""
    d = _Direct()
    table = {("w-a",): LINK_A, ("w-c",): LINK_C}
    steps = [d.step({"w-a", "w-b", "w-c"}, lambda key, finding: table.get(key.world_ids)) for _ in range(3)]
    assert _summary(steps)[0] == [(("w-a",), LINK_A), (("w-b",), None), (("w-c",), LINK_C)]


def test_link_decided_while_pending_survives_window_close():
    """리뷰 M1(lifecycle 단위): 지속 조건 첫 프레임에서 얻은 연결은 다음 프레임에 함수가 None(연결 시간 끝)이어도
    발생할 때 싣는다."""
    d = _Direct()
    first = d.step({"w-a"}, lambda key, finding: LINK_A)
    assert first.transitions == () and dict(d.tracker.pending())
    second = d.step({"w-a"}, None)
    assert _summary([second])[0] == [(("w-a",), LINK_A)]


def test_link_hint_is_dropped_when_pending_ends_without_event():
    """순간 오검출로 대기가 이벤트 없이 끝나면 기억한 연결도 버린다. 연결 시간이 끝난 뒤 다시 생긴 위험에 옛 연결이
    새지 않는다."""
    d = _Direct()
    d.step({"w-a"}, lambda key, finding: LINK_A, observed={"w-a"})
    quiet = [d.step(set(), None, observed={"w-a"}) for _ in range(3)]  # 보이는데 위험이 없다: 대기만 끝난다
    assert dict(d.tracker.pending()) == {} and _summary(quiet)[0] == []
    steps = [d.step({"w-a"}, None, observed={"w-a"}) for _ in range(3)]
    assert _summary(steps)[0] == [(("w-a",), None)]


@pytest.mark.parametrize("first_risk_dt, linked", [(29_900, True), (30_000, True), (30_100, False)])
def test_persistence_crossing_link_window_keeps_link(rig, first_risk_dt, linked):
    """리뷰 M1 재현: 재시작 0.5 s 뒤부터 같은 새 ID 를 구역 밖(3.6)에서 계속 보다가 연결 시간 경계에서 구역 안(3.35)에
    들어온다. 첫 위험 프레임이 연결 시간 안(≤ 30 000 ms)이면 지속 조건을 채우는 다음 프레임이 창 밖이어도 연결이
    실린다. 첫 위험 프레임부터 창 밖이면 연결하지 않는다. 어느 경우든 이전 이벤트는 해소되지 않는다."""
    tracks, tracks_t, events, ids = before_restart(rig, [("a", 3.0, 3.0)])
    previous = event_of(events, ids["a"])
    post = restart(tracks, tracks_t, events)
    start = tracks_t + 500
    for dt in range(0, first_risk_dt, 100):
        post.tick(rig, start + dt, [("a2", 3.6, 3.0)])
    assert post.raised() == []
    for dt in (first_risk_dt, first_risk_dt + 100, first_risk_dt + 200):
        post.tick(rig, start + dt, [("a2", 3.35, 3.0)])
    (raised,) = post.raised()
    assert raised.state.related_event_id == (previous.event_id if linked else None)
    assert post.frames[-1].tracks[0].restored_candidates == (ids["a"],)
    assert post.resolved() == [] and post.tracker.get(previous.event_id) is not None


# ---------------------------------------------------------------- 실제 저널·체크포인트·recover


def test_end_to_end_recover_restore_and_link(tmp_path, sd, rig, schema_errors):
    """체크포인트(Track 포함) → recover → manager.restore → 재관측 → 새 이벤트에 related_event_id. 저널을 거쳐 EC2 로
    나가는 메시지도 계약에 맞는다."""
    clock = FixedClock(T)
    site = sd.Site(tmp_path, clock)
    pre = Tier0("pre", clock, tracker=site.tracker)
    # 재시작 전: 같은 장면을 저널이 있는 Tier 0 에서 다시 만든다
    for k in range(10):
        _, step = pre.tick(rig, T + k * DT, [("a", 3.0, 3.0)])
        site.journal.append_step(step)
    (before,) = site.tracker.active()
    site.checkpointer.update_inputs(
        config_version="cfg-1",
        last_capture_time=pre.manager.last_t,
        tracks_t=pre.manager.last_t,
        tracks=pre.manager.export_tracks(),
    )
    site.checkpoint()
    site.close()

    again = sd.Site(tmp_path, clock)
    result = again.result
    assert [e.event_id for e in result.restored] == [before.event_id]
    assert all(t.observation_state.value == "UNKNOWN" for t in result.tracks)
    post = Tier0("post", clock, tracker=again.tracker, linker=RestoredEventLinker(result.restored))
    post.manager.restore(result.tracks, result.tracks_t)
    start = result.tracks_t + 3000
    for k in range(10):
        _, step = post.tick(rig, start + k * DT, [("a2", 3.0, 3.0)])
        again.journal.append_step(step)
    (raised,) = post.raised()
    assert raised.state.related_event_id == before.event_id
    again.outbox.pump()
    sent = [m for m in again.sent if m["payload"]["transition"] == "RAISED"]
    assert [m["payload"].get("related_event_id") for m in sent] == [None, before.event_id]
    for m in again.sent:
        assert schema_errors("risk_event_transition", m) == [], m
    assert post.resolved() == [] and again.tracker.get(before.event_id) is not None
    again.close()
