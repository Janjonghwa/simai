"""World ID 분할(ID 교정) 시험 (명세 5.2 ID 교정 기록, 15.1 G3, 부록 D-G3-10, S15P31A101-201).

합성 카메라·골격·클러스터 도구는 test_world_track.py 의 것을 그대로 쓴다. 모든 입력은 손으로 만든 SIM 이다.

교차 장면: A 가 (2.0, 3.0) 에서 +x 로 1 m/s 걷는다(k=0..9, 네 카메라 로컬 트랙 a). k=10..14 에 B 가 A 와 겹쳐 지나가
Cross-view 가 한 클러스터로 묶는다(cam1·cam2 는 A 의 박스 a, cam3·cam4 는 B 의 박스 b 를 따라간다). k=15 부터 B 가
(3.5, 3.0) 에서 +y 로 갈라져 나간다(네 카메라 로컬 트랙 b). cam3·cam4 에서 W 의 로컬 트랙 a·b 가 같은 시각의 두 검출로
보이므로 W 는 두 사람을 묶고 있었다.
"""

import importlib.util
from pathlib import Path

import pytest

from simai_ai.geometry.triangulation import triangulate_skeleton
from simai_ai.risk import (
    AlertClear,
    Change,
    Evaluation,
    Finding,
    FixedClock,
    IdentitySplit,
    Lifecycle,
    LifecycleTracker,
    Origin,
    Severity,
    WorldState,
)
from simai_ai.tracking.cross_view import ViewCluster
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    IdentityFlag,
    IdentityRevision,
    RevisionKind,
    RevisionReason,
    TrackCheckpoint,
    WorldIdIssuer,
    WorldTrackConfig,
    WorldTrackManager,
)

_spec = importlib.util.spec_from_file_location("_wt_helpers", Path(__file__).with_name("test_world_track.py"))
wt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wt)

T, DT = wt.T, wt.DT
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
UNCERTAIN = IdentityConfidence.IDENTITY_UNCERTAIN
A_IDS = {c: f"{c}-a" for c in wt.ALL}
B_IDS = {c: f"{c}-b" for c in wt.ALL}
MERGED = {"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-b", "cam4": "cam4-b"}
EVIDENCE = (("cam3", "cam3-b"), ("cam4", "cam4-b"))


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


def _manager(session="test"):
    return WorldTrackManager(WorldIdIssuer(session), WorldTrackConfig())


def _mixed(rig, name, t, x, y, ids, uncertain=False):
    views = tuple(wt._view(rig, cam, lid, t, x, y) for cam, lid in sorted(ids.items()))
    skeleton = triangulate_skeleton([v.view for v in views], rig) if len(views) >= 2 else None
    reasons = (wt.UncertainReason.AMBIGUOUS_ASSIGNMENT,) if uncertain else ()
    return ViewCluster(name, views, skeleton, reasons)


def _tick(m, rig, k, people):
    """people: (이름, x, y, 로컬 트랙 ID, uncertain) 목록. 사람마다 클러스터 하나."""
    t = T + k * DT
    clusters = [_mixed(rig, name, t, x, y, ids, *rest) for name, x, y, ids, *rest in people]
    return m.update(wt._frame(rig, t, clusters), rig)


def ax(k):
    """A 의 x (1 m/s 로 +x)."""
    return 2.0 + 0.1 * k


def _merged_walk(m, rig, merged=MERGED):
    """k=0..14: A 혼자 걷다가 B 와 겹쳐 한 클러스터로 묶인다. A 의 World ID W 를 돌려준다."""
    frames = [_tick(m, rig, k, [("A", ax(k), 3.0, A_IDS)]) for k in range(10)]
    frames += [_tick(m, rig, k, [("AB", ax(k), 3.0, merged)]) for k in range(10, 15)]
    (w,) = (tr.world_id for tr in frames[-1].tracks)
    assert m.issuer.issued == (w,)
    return w


def _apart(k, b_xy, b_ids=B_IDS, a_ids=A_IDS, b_uncertain=False):
    return [("A", ax(k), 3.0, a_ids), ("B", *b_xy, b_ids, b_uncertain)]


def b_leaves(k):
    """B 는 (3.5, 3.0) 에서 +y 로 1.5 m/s 갈라져 나간다(k=15: 0.15 m, k=16: 0.32 m, k=17: 0.49 m 떨어짐)."""
    return 3.5, min(3.0 + 0.15 * (k - 14), 4.5)


def _split_scene(m, rig, stop=40):
    w = _merged_walk(m, rig)
    frames = [_tick(m, rig, k, _apart(k, b_leaves(k))) for k in range(15, stop)]
    (n,) = (tr.world_id for tr in frames[0].tracks if tr.world_id != w)
    return w, n, frames


def _splits(m):
    return [r for r in m.revisions if r.kind is RevisionKind.SPLIT]


# ---------------------------------------------------------------- 분할


def test_crossing_merged_then_separated_is_split_with_revision(rig, contract_errors):
    """교차 장면: 갈라진 첫 시각에 새 ID N 이 바로 생기고(사람을 빠뜨리지 않음) 분할 후보가 된다. 두 골반이 0.35 m 넘게
    떨어진 근거 시각이 3번 쌓인 k=19 에 분할한다. 원래 ID W 는 예측 위치에 가까운 A 에 남고, 두 ID 의 identity_revision 이
    함께 1 이 되며 별칭은 바뀌지 않는다."""
    m = _manager()
    w, n, frames = _split_scene(m, rig)
    by_k = {(f.t - T) // DT: f for f in frames}
    assert all(by_k[k].revisions == () for k in range(15, 19))  # k=15·16 은 0.35 m 안, k=17·18 은 근거 1·2번째
    (rev,) = by_k[19].revisions
    assert rev == IdentityRevision(
        t=T + 19 * DT,
        kind=RevisionKind.SPLIT,
        world_id=n,
        previous_world_id=w,
        identity_revision=1,
        reason=str(RevisionReason.SPLIT_OBSERVED),
        evidence=EVIDENCE,
        since=T + 15 * DT,
    )
    assert m.revisions == (rev,) and m.issuer.issued == (w, n)
    assert rev.to_split() == IdentitySplit(world_id=w, new_world_id=n, identity_revision=1)
    assert rev.to_payload()["since"] == T + 15 * DT and rev.to_payload()["kind"] == "SPLIT"
    with pytest.raises(ValueError):
        rev.to_correction()  # 병합 형식으로 넘기지 않는다
    last = frames[-1]
    assert last.track(w).position_xy[0] == pytest.approx(ax(39), abs=0.05)  # W 는 A 를 따라간다
    assert last.track(n).position_xy == pytest.approx(b_leaves(39), abs=0.05)
    assert (last.track(w).identity_revision, last.track(n).identity_revision) == (1, 1)
    assert last.track(w).aliases == () and last.track(n).aliases == ()
    assert frozenset((w, n)) in m._distinct  # 같은 카메라의 두 검출: 다시 합치지 않는다
    # 후보인 동안(k=15~)과 분할 뒤 일관성 확인 동안 둘 다 IDENTITY_UNCERTAIN(IDENTITY_SPLIT)
    for k in range(15, 23):
        for wid in (w, n):
            assert by_k[k].track(wid).identity_confidence is UNCERTAIN, (k, wid)
            assert IdentityFlag.IDENTITY_SPLIT in by_k[k].track(wid).identity_flags, (k, wid)
    assert IdentityFlag.IDENTITY_SPLIT not in last.track(w).identity_flags  # 확인 3시각 + 유지 1 s 뒤 풀린다
    assert last.track(w).identity_confidence is not UNCERTAIN
    for f in frames:
        msg = world_state_message(world_state_payload(f, f.t, "scene-1"), msg_id=MSG_ID, sent_at=f.t)
        assert contract_errors("world_state", msg) == []
        revisions = {doc["world_id"]: doc["identity_revision"] for doc in msg["payload"]["tracks"]}
        assert revisions == {w: int(f.t >= rev.t), n: int(f.t >= rev.t)}
        assert all("aliases" not in doc for doc in msg["payload"]["tracks"])
        # world_state 2.1.0 split_from: 분할을 확정한 시각부터 새 ID 에만 원래 ID (후보인 동안·원래 ID 에는 없음)
        split_from = {doc["world_id"]: doc.get("split_from") for doc in msg["payload"]["tracks"]}
        assert split_from == {w: None, n: w if f.t >= rev.t else None}
        assert msg["schema_version"] == "2.1.0"


@pytest.mark.parametrize("d, split", [(0.34, False), (0.36, True)])
def test_split_distance_boundary(rig, d, split):
    """반례(경계): 갈라진 두 사람이 0.35 m(duplicate_radius_m) 안에 붙어 다니면 같은 사람일 수 있어 분할하지 않고 둘 다
    불확실로 둔다. 넘으면 근거 3시각 뒤 분할한다."""
    m = _manager()
    w = _merged_walk(m, rig)
    frames = [_tick(m, rig, k, _apart(k, (ax(k), 3.0 + d))) for k in range(15, 40)]
    assert (len(_splits(m)) == 1) is split
    if split:
        assert [len(f.revisions) for f in frames[:4]] == [0, 0, 1, 0]
    else:
        n = next(tr.world_id for tr in frames[0].tracks if tr.world_id != w)
        assert all(f.track(wid).identity_confidence is UNCERTAIN for f in frames for wid in (w, n))
        assert all(f.track(w).identity_revision == 0 for f in frames)


def test_single_camera_split_is_not_split(rig):
    """반례: W 의 로컬 트랙이 한 카메라(cam4)에서만 두 검출로 갈라지면 로컬 ID 전환과 구별할 수 없어 분할하지 않는다.
    B 는 지금처럼 새 ID 이고 분할 표시도 없다."""
    m = _manager()
    w = _merged_walk(m, rig, merged={"cam1": "cam1-a", "cam2": "cam2-a", "cam3": "cam3-a", "cam4": "cam4-b"})
    frames = [_tick(m, rig, k, _apart(k, b_leaves(k))) for k in range(15, 40)]
    assert m.revisions == () and len(frames[-1].tracks) == 2
    assert all(IdentityFlag.IDENTITY_SPLIT not in tr.identity_flags for f in frames for tr in f.tracks)
    assert frames[-1].track(w).identity_revision == 0


def test_uncertain_cross_view_cluster_is_not_a_split_candidate(rig):
    """반례: 갈라진 쪽 클러스터가 Cross-view IDENTITY_UNCERTAIN 이면 근거가 아니다(연결 자체가 불확실)."""
    m = _manager()
    _merged_walk(m, rig)
    _tick(m, rig, 15, _apart(15, (3.5, 3.6), b_uncertain=True))
    for k in range(16, 40):
        _tick(m, rig, k, _apart(k, (3.5, 3.6)))
    assert m.revisions == ()


def test_short_separation_then_rejoin_is_not_split(rig):
    """반례: 떨어진 근거가 2시각(k=17·18)뿐이고 다시 한 클러스터로 묶이면 분할하지 않는다. 다시 묶인 동안 N 의 로컬 트랙이
    W 의 클러스터에 2대 이상 섞여도 W·N 은 같은 카메라의 두 검출로 보인 병합 금지 쌍이라 병합하지도 않는다."""
    m = _manager()
    w = _merged_walk(m, rig)
    first = [_tick(m, rig, k, _apart(k, b_leaves(k))) for k in range(15, 19)]
    n = next(tr.world_id for tr in first[0].tracks if tr.world_id != w)
    rejoined = [_tick(m, rig, k, [("AB", ax(k), 3.0, MERGED)]) for k in range(19, 70)]
    assert m.revisions == () and frozenset((w, n)) in m._distinct
    assert all(f.track(w) is not None for f in rejoined)
    assert rejoined[-1].track(w).identity_revision == 0 and m._splits == {}


@pytest.mark.parametrize("hidden_ticks, split", [(10, True), (25, False)])
def test_occlusion_during_candidate(rig, hidden_ticks, split):
    """반례(가림): 근거 2시각 뒤 B 가 모든 카메라에서 가려지면 그동안은 세지도 버리지도 않는다. 1 s 가림 뒤 같은 로컬 트랙으로
    돌아오면 세 번째 근거로 분할하고, 근거가 key_ttl_ms(2 s) 넘게 끊기면(2.5 s 가림) 후보를 버려 분할하지 않는다."""
    m = _manager()
    w = _merged_walk(m, rig)
    for k in range(15, 19):
        _tick(m, rig, k, _apart(k, b_leaves(k)))
    for k in range(19, 19 + hidden_ticks):
        _tick(m, rig, k, [("A", ax(k), 3.0, A_IDS)])
    back = 19 + hidden_ticks
    frames = [_tick(m, rig, k, _apart(k, (3.5, 3.6))) for k in range(back, back + 10)]
    assert (len(_splits(m)) == 1) is split
    if split:
        assert len(frames[0].revisions) == 1 and frames[0].revisions[0].previous_world_id == w
    else:
        assert frames[-1].track(w).identity_revision == 0


def _side_offset(k):
    """B 가 A 옆으로 갈라졌다가(0.6 m) 다시 붙고(k=28..34 한 클러스터) 다시 갈라지는 y 간격. None 이면 한 클러스터."""
    if k <= 24:
        return min(0.15 * (k - 14), 0.6)
    if k <= 27:
        return 0.6 - 0.15 * (k - 24)
    if k <= 34:
        return None
    return min(0.15 * (k - 34), 0.6)


def test_split_does_not_oscillate_with_merge(rig):
    """반례(진동): 분할 뒤 두 사람이 다시 붙어 한 클러스터로 묶이는 동안 N 의 로컬 트랙이 W 의 클러스터에 2대 이상 섞여도
    병합하지 않고(병합 금지 쌍), 다시 갈라지면 N 이 같은 ID 로 이어진다(두 번째 분할·새 ID 없음). 교정 기록은 분할 한 건이다."""
    m = _manager()
    w = _merged_walk(m, rig)
    for k in range(15, 60):
        d = _side_offset(k)
        f = _tick(m, rig, k, [("AB", ax(k), 3.0, MERGED)] if d is None else _apart(k, (ax(k), 3.0 + d)))
    assert [r.kind for r in m.revisions] == [RevisionKind.SPLIT] and m.revisions[0].t == T + 19 * DT
    n = m.revisions[0].world_id
    assert m.issuer.issued == (w, n) and {tr.world_id for tr in f.tracks} == {w, n}
    assert (f.track(w).identity_revision, f.track(n).identity_revision) == (1, 1)
    assert f.track(n).position_xy[1] == pytest.approx(3.6, abs=0.05)


def test_merged_then_split_keeps_aliases_and_raises_revision(rig):
    """병합으로 생긴 ID 가 분할되면 identity_revision 은 둘 중 큰 값 + 1 이고 별칭은 빠지지 않는다(덮어쓰지 않음)."""
    m = _manager()
    w = _merged_walk(m, rig)
    tr = m._tracks[w]
    tr.identity_revision, tr.aliases = 1, ["w-test-0099"]  # 앞선 병합의 결과를 흉내 낸다
    for k in range(15, 25):
        f = _tick(m, rig, k, _apart(k, b_leaves(k)))
    (rev,) = _splits(m)
    assert rev.identity_revision == 2
    assert f.track(w).aliases == ("w-test-0099",) and f.track(rev.world_id).aliases == ()
    assert f.track(rev.world_id).identity_revision == 2


def test_restart_after_split_keeps_revision_in_checkpoint_and_no_pending_split(rig):
    """재시작 복원: 체크포인트에는 분할 뒤 identity_revision 이 남고(별칭 그대로), 분할 후보는 남기지 않는다. 재시작 뒤에는
    로컬 트래커가 새로 시작하므로 교정 근거가 없어 분할·병합을 다시 내지 않고, 재관측된 두 사람은 새 ID 로 복원 Track
    후보만 단다."""
    m = _manager()
    w, n, frames = _split_scene(m, rig, stop=30)
    tracks = m.export_tracks()
    restored = tuple(TrackCheckpoint.from_record(tc.to_record()) for tc in tracks)
    assert {tc.world_id: tc.identity_revision for tc in restored} == {w: 1, n: 1}
    assert all(tc.aliases == () for tc in restored)

    m2 = _manager(session="next")
    m2.restore(restored, frames[-1].t)
    after = [
        _tick(m2, rig, k, [("A", ax(k), 3.0, {c: f"{c}-ra" for c in wt.ALL}),
                           ("B", *b_leaves(k), {c: f"{c}-rb" for c in wt.ALL})])
        for k in range(32, 60)
    ]  # fmt: skip
    assert m2.revisions == () and len(m2.issuer.issued) == 2
    assert all(tr.identity_revision == 0 for tr in after[-1].tracks)
    assert {r for tr in after[0].tracks for r in tr.restored_candidates} <= {w, n}
    assert all(tr.split_from is None for f in after for tr in f.tracks)  # 이전 세션의 분할 출처는 잇지 않는다
    assert all("split_from" not in tc.to_record() for tc in tracks)  # 체크포인트 형식은 그대로


def test_split_from_survives_occlusion_and_is_validated_by_sender(rig, contract_errors):
    """split_from 은 N 이 가려졌다 같은 ID 로 돌아와도 같은 값이고, 송신 측은 자기 ID·별칭인 split_from 을 내지 않는다
    (스키마로는 두 값의 같음을 막을 수 없다)."""
    from dataclasses import replace

    m = _manager()
    w, n, _ = _split_scene(m, rig, stop=25)
    for k in range(25, 35):  # B 만 1 s 가려진다
        _tick(m, rig, k, [("A", ax(k), 3.0, A_IDS)])
    f = _tick(m, rig, 35, _apart(35, b_leaves(35)))
    assert f.track(n).updated and f.track(n).split_from == w and f.track(w).split_from is None
    msg = world_state_message(world_state_payload(f, f.t, "scene-1"), msg_id=MSG_ID, sent_at=f.t)
    assert contract_errors("world_state", msg) == []
    for bad in (n, ""):
        broken = replace(f, tracks=tuple(replace(tr, split_from=bad) if tr.world_id == n else tr for tr in f.tracks))
        with pytest.raises(ValueError, match="split_from"):
            world_state_payload(broken, f.t, "scene-1")
    aliased = replace(f, tracks=tuple(replace(tr, split_from="w-x", aliases=("w-x",)) if tr.world_id == n else tr
                                      for tr in f.tracks))  # fmt: skip
    with pytest.raises(ValueError, match="split_from"):
        world_state_payload(aliased, f.t, "scene-1")


# ---------------------------------------------------------------- G3: 분할 후 진행 중 위험 자동 해소 0건


class _RiskLoop:
    """World Track → world_state → Risk 생명주기. inside(track) 인 Track 에 R1 HIGH 판정을 낸다. 교정은 판정 전에 넘긴다.

    split=False 는 음성 대조다(분할을 Risk 에 넘기지 않던 이전 동작).
    """

    def __init__(self, inside, split=True):
        self.clock = FixedClock(T)
        self.tracker = LifecycleTracker(origin=Origin.LIVE, gateway_id="tier0-01", clock=self.clock)
        self.inside = inside
        self.split = split
        self.transitions = []
        self.signals = []

    def feed(self, frame):
        for rev in frame.revisions:
            if rev.kind is RevisionKind.MERGE:
                self.transitions += self.tracker.correct_identity(rev.to_correction()).transitions
            elif self.split:
                self.transitions += self.tracker.split_identity(rev.to_split()).transitions
        world = WorldState.from_payload(world_state_payload(frame, frame.t, "scene-1"))
        findings = [
            Finding(rule_id="R1", rule_version=1, severity=Severity.HIGH, world_ids=(tr.world_id,), zone_id="z-01",
                    reasons=("xy_inside_polygon",))
            for tr in frame.tracks
            if tr.updated and self.inside(tr)
        ]  # fmt: skip
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
        step = self.tracker.apply(evaluation, world, config_version="cfg-1")
        self.transitions += step.transitions
        self.signals += step.local_signals

    def resolved(self, event_id):
        return [t for t in self.transitions if t.transition is Lifecycle.RESOLVED and t.event_id == event_id]


def b_stays_then_leaves(k):
    """B 는 갈라져 나간 뒤 구역(x < 3.55) 안 (3.5, 3.6) 에 머물다가 k=70 부터 +x 로 1 m/s 걸어 구역 밖으로 나간다."""
    return (3.5, b_leaves(k)[1]) if k < 18 else (min(3.5 + 0.1 * max(k - 70, 0), 4.5), 3.6)


def _risk_scene(rig, split=True):
    m = _manager()
    loop = _RiskLoop(inside=lambda tr: tr.position_xy[0] < 3.55, split=split)
    for k in range(10):
        loop.feed(_tick(m, rig, k, [("A", ax(k), 3.0, A_IDS)]))
    for k in range(10, 15):
        loop.feed(_tick(m, rig, k, [("AB", ax(k), 3.0, MERGED)]))
    (w_event,) = loop.tracker.active()
    by_k = {}
    for k in range(15, 140):  # A 는 k=16 부터 구역 밖, B 는 k=70 까지 구역 안
        by_k[k] = _tick(m, rig, k, _apart(k, b_stays_then_leaves(k)))
        loop.feed(by_k[k])
    return m, loop, w_event, by_k


def test_g3_split_keeps_pre_split_event_while_split_partner_is_in_danger(rig):
    """D-G3-10(분할): 합쳐진 W 에 걸린 R1 HIGH 가 진행 중일 때 W 가 둘로 갈라져 W(A)는 구역 밖, N(B)은 구역 안에 남는다.
    분할 전 이벤트가 둘 중 누구의 것인지 모르므로 W 에 그대로 두고(원본 World ID 보존, 키 그대로), W 만 구역 밖에서
    확실히 보여도 해소하지 않는다. N 의 같은 위험은 N 의 새 이벤트로 난다. B 도 나간 뒤에는 둘 다 해소된다(무기한 남지
    않는다)."""
    m, loop, w_event, by_k = _risk_scene(rig)
    (rev,) = m.revisions
    w, n = rev.previous_world_id, rev.world_id
    assert w_event.world_ids == (w,)
    assert by_k[60].track(w).identity_confidence is not UNCERTAIN  # W 의 분할 표시가 풀린 뒤에도
    resolved = loop.resolved(w_event.event_id)
    assert resolved and min(t.state.observation_time for t in resolved) > T + 70 * DT  # B 가 나간 뒤에야 해소
    revised = [t for t in loop.transitions if t.event_id == w_event.event_id and Change.IDENTITY_REVISED in t.changes]
    assert [t.state.observation_time for t in revised] == [rev.t]  # 분할은 IDENTITY_REVISED 로 표시된다
    assert revised[0].state.world_ids == revised[0].state.origin_world_ids == (w,)
    n_events = [t for t in loop.transitions if t.transition is Lifecycle.RAISED and t.state.world_ids == (n,)]
    assert len(n_events) == 1  # 갈라져 나간 쪽의 같은 위험은 새 이벤트
    assert not [t for t in loop.transitions if t.transition is Lifecycle.RAISED and t.state.world_ids == (w,)][1:]


def test_negative_control_split_not_passed_to_risk_resolves_wrong_event(rig):
    """음성 대조: 분할을 Risk 에 넘기지 않으면(이전 동작) B 가 아직 구역 안인데 W(A)의 관측만으로 분할 전 이벤트가 해소된다.
    위 시험이 분할 상대 규칙을 실제로 잡는다."""
    _, loop, w_event, _ = _risk_scene(rig, split=False)
    resolved = loop.resolved(w_event.event_id)
    assert resolved and resolved[0].state.observation_time < T + 70 * DT
    assert any(isinstance(s, AlertClear) and s.event_id == w_event.event_id for s in loop.signals)


def test_g3_chained_split_partner_keeps_pre_split_event(rig):
    """리뷰 R1 회귀: W→N 뒤 N 이 다시 N→M 으로 갈라지면 M 도 W 의 분할 전 이벤트의 분할 상대다(계보 전체). W·N 이 구역
    밖에서 안전해도 M 이 구역 안에 남아 있는 동안 해소하지 않고, M 도 나간 뒤에야 해소한다."""
    m = _manager()
    loop = _RiskLoop(inside=lambda tr: tr.position_xy[0] < 3.55)
    for k in range(15):
        loop.feed(_tick(m, rig, k, [("AB", ax(k), 3.0, A_IDS if k < 10 else MERGED)]))
    (old,) = loop.tracker.active()
    for k in range(15, 40):
        loop.feed(_tick(m, rig, k, _apart(k, b_stays_then_leaves(k))))
    c_ids = {c: f"{c}-c" for c in wt.ALL}
    mixed = {**B_IDS, "cam3": "cam3-c", "cam4": "cam4-c"}  # B·C 가 겹쳐 한 클러스터
    for k in range(40, 45):
        loop.feed(_tick(m, rig, k, [("A", ax(k), 3.0, A_IDS), ("BC", 3.5, 3.6, mixed)]))

    def c_xy(k):  # C 는 구역 안에서 +y 로 갈라졌다가 k=120 부터 +x 로 걸어 나간다
        return (min(3.5 + 0.1 * max(k - 119, 0), 4.5), min(3.6 + 0.15 * (k - 44), 4.5))

    for k in range(45, 200):
        loop.feed(_tick(m, rig, k, [("A", min(ax(k), 6.5), 3.0, A_IDS), ("B", min(3.5 + 0.1 * (k - 44), 4.5), 3.6, B_IDS),
                                    ("C", *c_xy(k), c_ids)]))  # fmt: skip
    w, n = m.revisions[0].previous_world_id, m.revisions[0].world_id
    assert [(r.previous_world_id, (r.t - T) // DT) for r in m.revisions] == [(w, 19), (n, 48)]
    resolved = loop.resolved(old.event_id)
    assert resolved and min(t.state.observation_time for t in resolved) > T + 120 * DT  # M 이 나간 뒤에야


def test_split_candidate_expires_across_input_stall(rig):
    """리뷰 R2 회귀: 전체 입력이 멈춰 update 가 불리지 않다가(2.6 s) 돌아오면, 근거가 key_ttl_ms(2 s) 넘게 끊긴 분할 후보를
    이번 시각의 근거로 되살리지 않고 버린다(분할하지 않는다)."""
    m = _manager()
    _merged_walk(m, rig)
    for k in range(15, 36):  # 0.2 m 붙어 다녀 근거를 세지 못한다
        _tick(m, rig, k, [("A", 3.5, 3.0, A_IDS), ("B", 3.5, 3.2, B_IDS)])
    for k in (36, 37):
        _tick(m, rig, k, [("A", 3.5, 3.0, A_IDS), ("B", 3.5, 3.4, B_IDS)])
    assert [sp.count for sp in m._splits.values()] == [2]
    frames = [_tick(m, rig, k, [("A", 3.5, 3.0, A_IDS), ("B", 3.5, 3.4, B_IDS)]) for k in (63, 64, 65, 66)]
    assert m._splits == {} and all(f.revisions == () for f in frames) and m.revisions == ()
