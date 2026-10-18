"""재시작 복구 (명세 3.2 재시작 복구·복구 순서, 부록 D-G3-09·11·13).

복구 결과는 늘 '저널 전체를 payload 로 접은 정답'과 비교한다(site_driver.journal_truth, 복구 코드와 독립). 같은
프로세스 안 시험은 저널을 닫고 다시 연다. 저널은 기록마다 fsync 하므로 닫기는 내구성에 아무것도 더하지 않는다
(프로세스 강제 종료는 test_crash_recovery.py).
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from simai_ai.eventlog import TRANSITION_KIND, EventJournal, JournalConfig
from simai_ai.eventlog.record import JournalEntry
from simai_ai.recovery import (
    CheckpointStore,
    EventProjection,
    RecoveryError,
    RecoveryIncomplete,
    RecoverySource,
    ReplayError,
)
from simai_ai.recovery.checkpoint import FORMAT_VERSION, checkpoint_name
from simai_ai.risk import Severity
from simai_ai.risk.alerting import AckActor
from simai_ai.risk.lifecycle import Change, EventState, Lifecycle, Verification
from simai_ai.risk.site_alerts import build_active_site_alerts

SMALL = JournalConfig(segment_max_bytes=1, retention_ms=0)  # 기록마다 세그먼트 하나(정리 경계를 정확히 보려고)


def _cp_path(root: Path, seq: int) -> Path:
    return root / "checkpoints" / checkpoint_name(seq)


def _rewrite(path: Path, mutate) -> None:
    """체크포인트 body 를 바꾸고 sha256 을 다시 맞춘다(해시는 맞지만 내용이 틀린 경우를 만든다)."""
    import hashlib

    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    body = json.dumps(doc["body"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    doc["sha256"] = hashlib.sha256(body).hexdigest()
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# --- 기본 복구 순서 -----------------------------------------------------------------------


def test_first_start_is_empty(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock)
    assert site.result.source is RecoverySource.EMPTY and site.result.restored == ()
    assert site.result.restore_step.transitions == () and site.journal.next_offset == 0
    site.close()


def test_checkpoint_then_replay_matches_journal(tmp_path, clock, sd):
    """체크포인트 복원 → 그 offset 이후 저널 재적용. 체크포인트 뒤의 확인·상향·해소·새 발생이 모두 반영된다."""
    site = sd.Site(tmp_path, clock)
    a = site.raise_event("w-1", Severity.HIGH)
    site.raise_event("w-2", Severity.CAUTION)
    cp = site.checkpoint()
    assert {e.event_id for e in cp.events} == {a.event_id, site.tracker.active()[1].event_id}
    site.ack(a.event_id)
    site.raise_event("w-3", Severity.CRITICAL)
    site.hold([sd.finding("w-1"), sd.finding("w-2", Severity.HIGH), sd.finding("w-3", Severity.CRITICAL, 0.9)], 12)
    site.resolve("w-1", others=["w-2", "w-3"])
    truth = sd.journal_truth(tmp_path, clock)
    assert len(truth) == 2
    site.close()

    clock.advance(5000)
    again = sd.Site(tmp_path, clock)
    r = again.result
    assert r.source is RecoverySource.CHECKPOINT and r.checkpoint == cp and r.rejected == ()
    assert r.replay_from == cp.last_applied_journal_offset + 1
    assert r.config_version == "cfg-12" and r.last_capture_time == cp.last_capture_time
    sd.assert_matches_journal(r, truth)
    assert a.event_id not in {s.event_id for s in r.restored}  # 체크포인트에 RAISED, 저널에 RESOLVED → RESOLVED 가 최종
    again.close()


def test_restored_events_keep_site_alarm_and_acks_and_never_auto_resolve(tmp_path, clock, sd):
    """D-G3-09: 자동 해소 0건, 현장 경보를 끄지 않음, 새 이벤트 중복 없음. 확인 기록·경보 단계는 이어진다."""
    site = sd.Site(tmp_path, clock)
    a = site.raise_event("w-1", Severity.HIGH)
    site.ack(a.event_id, AckActor.SITE_BUTTON, "SITE_BUTTON")
    site.raise_event("w-2", Severity.CAUTION)
    site.hold([sd.finding("w-1"), sd.finding("w-2", Severity.CRITICAL)])  # 상향: 현장 경보 단계가 새로 시작
    site.checkpoint()
    before = build_active_site_alerts(site.tracker.active())
    assert [x["acknowledged"] for x in before] == [True, False]
    site.close()

    clock.advance(3000)
    again = sd.Site(tmp_path, clock)
    after = build_active_site_alerts(again.tracker.active())
    # 복원 전에 보낸 목록과 같은 경보(같은 확인 상태)가 복원 직후 목록에 있다: 현장 소리·표시를 끄지 않는다
    assert [(x["event_id"], x["acknowledged"], x["severity"]) for x in after] == [
        (x["event_id"], x["acknowledged"], x["severity"]) for x in before
    ]
    assert [x["event_sequence"] for x in after] == [x["event_sequence"] + 1 for x in before]
    assert again.result.restore_step.local_signals == ()
    for t in again.result.restore_step.transitions:
        assert Change.RESTORED in t.changes and t.state.verification is Verification.UNVERIFIABLE
    ids = {s.event_id for s in again.tracker.active()}
    # 재관측 전(사람 없음) 10초, 그 뒤 새 World ID 만 보이는 10초: 해소도 새 이벤트도 없다
    for present in (set(), {"w-new-1"}):
        for _ in range(100):
            step = again.step([], present=present)
            assert all(t.transition is not Lifecycle.RESOLVED for t in step.transitions)
            assert all(t.transition is not Lifecycle.RAISED for t in step.transitions)
    assert {s.event_id for s in again.tracker.active()} == ids
    # 복원 전이는 저널에 남고 outbox 가 막히지 않고 보낸다(판정이 아닌 전이)
    assert again.outbox.pump() > 0 and again.outbox.blocked is None
    assert {m["payload"]["event_id"] for m in again.sent if "RESTORED" in m["payload"]["changes"]} == ids
    again.close()


def test_resumed_event_after_checkpoint_is_restored(tmp_path, clock, sd):
    """체크포인트 뒤 재개(같은 event_id 로 RESOLVED 뒤 UPDATED)도 저널대로 활성으로 되살린다."""
    site = sd.Site(tmp_path, clock, policy=sd.RESUME_POLICY)
    a = site.raise_event("w-1")
    site.ack(a.event_id)
    site.resolve("w-1")
    site.checkpoint()
    resumed = site.raise_event("w-1")
    assert resumed.event_id == a.event_id and resumed.resume_count == 1 and not resumed.acknowledged
    truth = sd.journal_truth(tmp_path, clock)
    site.close()
    again = sd.Site(tmp_path, clock, policy=sd.RESUME_POLICY)
    sd.assert_matches_journal(again.result, truth)
    (s,) = again.result.restored
    assert s.event_id == a.event_id and s.resume_count == 1 and s.acks == ()
    again.close()


def test_double_restart_continues_sequence(tmp_path, clock, sd):
    """복원 전이도 저널에 남으므로 두 번째 재시작은 그 상태에서 이어 sequence 를 올린다."""
    site = sd.Site(tmp_path, clock)
    a = site.raise_event("w-1")
    site.close()
    first = sd.Site(tmp_path, clock)
    assert first.result.source is RecoverySource.JOURNAL
    first.close()
    truth = sd.journal_truth(tmp_path, clock)
    second = sd.Site(tmp_path, clock)
    sd.assert_matches_journal(second.result, truth)
    (s,) = second.result.restored
    assert s.event_id == a.event_id and s.event_sequence == 3
    second.close()


# --- 체크포인트 손상·버전 불일치 → 직전 세대 / 저널 전체 -----------------------------------------


def _two_generations(tmp_path, clock, sd, **site_kwargs):
    site = sd.Site(tmp_path, clock, **site_kwargs)
    a = site.raise_event("w-1")
    cp1 = site.checkpoint()
    site.ack(a.event_id)
    site.raise_event("w-2", Severity.CRITICAL)
    cp2 = site.checkpoint()
    site.resolve("w-1", others=["w-2"])
    site.raise_event("w-3")
    truth = sd.journal_truth(tmp_path, clock)
    site.close()
    return cp1, cp2, truth


def _truncate(p: Path) -> None:
    p.write_bytes(p.read_bytes()[: len(p.read_bytes()) // 2])


def _bitflip(p: Path) -> None:
    data = bytearray(p.read_bytes())
    i = data.index(b'"severity"') + 12
    data[i] ^= 0x01
    p.write_bytes(bytes(data))


CORRUPTIONS = {
    "truncated": (_truncate, "JSON"),
    "bitflip": (_bitflip, "sha256"),
    "garbage": (lambda p: p.write_bytes(b"\x00\xff garbage"), "JSON"),
    "version": (lambda p: _rewrite(p, lambda d: d.update(format_version=FORMAT_VERSION + 1)), "형식 버전"),
    "other_gateway": (lambda p: _rewrite(p, lambda d: d["body"].update(gateway_id="tier0-99")), "다른 Tier 0"),
    # 해시는 맞지만 파생 필드가 상태와 어긋난 내용(확인 기록 없는데 acknowledged=true)
    "derived_mismatch": (
        lambda p: _rewrite(p, lambda d: d["body"]["events"][0].update(acks=[])),
        "파생 필드",
    ),
    "seq_mismatch": (lambda p: _rewrite(p, lambda d: d["body"].update(checkpoint_seq=99)), "세대"),
}


@pytest.mark.parametrize("kind", sorted(CORRUPTIONS))
def test_corrupt_latest_checkpoint_falls_back_to_previous(tmp_path, clock, sd, kind):
    cp1, cp2, truth = _two_generations(tmp_path, clock, sd)
    corrupt, reason = CORRUPTIONS[kind]
    corrupt(_cp_path(tmp_path, cp2.checkpoint_seq))
    again = sd.Site(tmp_path, clock)
    r = again.result
    assert r.source is RecoverySource.CHECKPOINT and r.checkpoint == cp1
    assert [x.checkpoint_seq for x in r.rejected] == [cp2.checkpoint_seq] and reason in r.rejected[0].reason
    sd.assert_matches_journal(r, truth)
    again.close()


def test_partial_write_leaves_only_tmp_and_previous_generation_is_used(tmp_path, clock, sd):
    """체크포인트 쓰는 도중 종료(임시 파일만 남음): 임시 파일은 버리고 직전 세대 + 저널로 복구한다."""
    cp1, cp2, truth = _two_generations(tmp_path, clock, sd)
    path = _cp_path(tmp_path, cp2.checkpoint_seq)
    path.rename(path.with_name(path.name + ".tmp"))
    again = sd.Site(tmp_path, clock)
    assert again.result.checkpoint == cp1 and again.result.rejected == ()
    assert not list((tmp_path / "checkpoints").glob("*.tmp"))
    sd.assert_matches_journal(again.result, truth)
    # 세대 번호는 되돌아가지 않는다(남은 이름 기준으로 이어 감)
    assert again.checkpoint().checkpoint_seq == cp1.checkpoint_seq + 1
    again.close()


def test_all_checkpoints_corrupt_replays_whole_journal(tmp_path, clock, sd):
    cp1, cp2, truth = _two_generations(tmp_path, clock, sd)
    for cp in (cp1, cp2):
        _truncate(_cp_path(tmp_path, cp.checkpoint_seq))
    again = sd.Site(tmp_path, clock)
    assert again.result.source is RecoverySource.JOURNAL and again.result.replay_from == 0
    assert len(again.result.rejected) == 2
    sd.assert_matches_journal(again.result, truth)
    again.close()


def test_no_usable_checkpoint_and_compacted_journal_refuses_to_guess(tmp_path, clock, sd):
    """반례: 체크포인트가 모두 깨졌고 저널 앞부분이 정리됐다. 활성 이벤트를 다 알 수 없으므로 '없음'으로 시작하지 않는다."""
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    site.raise_event("w-1")  # 이 이벤트의 기록은 정리로 사라진다. 체크포인트에만 남는다
    site.checkpoint()
    site.raise_event("w-2")
    site.outbox.pump()
    site.outbox.on_journal_ack({"gateway_id": sd.GATEWAY, "acked_offset": site.journal.last_offset})
    site.checkpoint()
    site.checkpoint()
    assert site.journal.first_offset > 0
    names = sorted((tmp_path / "checkpoints").glob("checkpoint-*.json"))
    site.close()
    for p in names:
        _truncate(p)
    with pytest.raises(RecoveryIncomplete):
        sd.Site(tmp_path, clock, journal_config=SMALL)


# --- 체크포인트가 저널보다 앞섬·다른 저널 ------------------------------------------------------


def test_checkpoint_ahead_of_journal_stops(tmp_path, clock, sd):
    """반례: 저널이 fsync 된 기록을 잃었다(체크포인트가 저널보다 앞섬). 물러서서 활성 이벤트를 끄지 않고 멈춘다."""
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    site.raise_event("w-1")
    site.raise_event("w-2")
    site.checkpoint()
    segments = sorted((tmp_path / "journal").glob("*.journal"))
    site.close()
    segments[-1].unlink()  # 마지막 기록(세그먼트) 유실
    with pytest.raises(RecoveryError, match="앞선다"):
        sd.Site(tmp_path, clock, journal_config=SMALL)


def test_checkpoint_from_another_journal_stops_even_if_offsets_fit(tmp_path, clock, sd):
    """반례: 저널이 다른 것으로 바뀌어 offset 은 맞는다(더 길다). msg_id 로 알아보고 멈춘다."""
    a = tmp_path / "a"
    site = sd.Site(a, clock)
    site.raise_event("w-1")
    site.checkpoint()
    site.close()
    b = tmp_path / "b"
    other = sd.Site(b, clock, seed=7)
    for i in range(4):
        other.raise_event(f"w-x{i}")
    other.close()
    for p in (b / "checkpoints").glob("*"):
        p.unlink()
    for p in (a / "checkpoints").glob("*.json"):
        (b / "checkpoints" / p.name).write_bytes(p.read_bytes())
    with pytest.raises(RecoveryError, match="다른 저널"):
        sd.Site(b, clock)


def test_checkpoint_behind_journal_replays_the_rest(tmp_path, clock, sd):
    """체크포인트가 저널보다 뒤처진 것(마지막 체크포인트 뒤 전이)은 정상: 나머지를 적용한다."""
    site = sd.Site(tmp_path, clock)
    site.raise_event("w-1")
    cp = site.checkpoint()
    for i in range(5):
        site.raise_event(f"w-{i + 2}")
    truth = sd.journal_truth(tmp_path, clock)
    behind = site.journal.next_offset - (cp.last_applied_journal_offset + 1)
    assert behind >= 5
    site.close()
    again = sd.Site(tmp_path, clock)
    assert again.result.checkpoint == cp and again.result.stats.applied == behind
    sd.assert_matches_journal(again.result, truth)
    again.close()


# --- 저널 기록을 믿을 수 없을 때 ------------------------------------------------------------


def test_transition_without_state_is_not_skipped(tmp_path, clock, sd):
    """반례: meta.state 없는 전이(51 이전 형식·손상)를 건너뛰면 활성 이벤트를 잃는다. 지어내지 않고 멈춘다."""
    site = sd.Site(tmp_path, clock)
    a = site.raise_event("w-1")
    payload = site.journal.entry(0).payload_dict()
    site.close()
    with EventJournal.open(tmp_path / "journal", clock=clock) as j:
        j.append(TRANSITION_KIND, payload | {"event_id": a.event_id[:-1] + "f"})
    with pytest.raises(RecoveryError, match="meta.state"):
        sd.Site(tmp_path, clock)


def test_state_not_matching_payload_is_rejected(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock)
    site.raise_event("w-1")
    entry = site.journal.entry(0)
    site.close()
    with EventJournal.open(tmp_path / "journal", clock=clock) as j:
        bad = entry.payload_dict() | {"event_sequence": 2, "transition": "UPDATED"}
        j.append(TRANSITION_KIND, bad, {"state": dict(entry.meta["state"])})
    with pytest.raises(RecoveryError, match="다르다"):
        sd.Site(tmp_path, clock)


# --- 멱등: 이벤트별 sequence --------------------------------------------------------------------


def _entries(site, states_and_kinds):
    """(EventTransition) 들을 offset 0 부터 이어 붙인 가짜 저널 기록(meta.state 포함)."""
    out = []
    for offset, t in enumerate(states_and_kinds):
        out.append(
            JournalEntry(
                journal_offset=offset,
                kind=TRANSITION_KIND,
                msg_id=f"m-{offset}",
                recorded_at=0,
                payload=t.to_payload(),
                meta={"state": t.state.to_record()},
            )
        )
    return out


def test_late_lower_sequence_does_not_resurrect_resolved_event(tmp_path, clock, sd):
    """반례(순서 뒤섞인 기록): RESOLVED 뒤에 낮은 sequence 기록이 와도 해소된 이벤트를 되살리지 않는다."""
    site = sd.Site(tmp_path, clock)
    raised = site.step([sd.finding("w-1")]).transitions[0]
    ack = site.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "a", clock.now_ms(), channel="CONTROL_ROOM")
    resolved = None
    for _ in range(10):
        step = site.step([])
        if step.transitions and step.transitions[0].transition is Lifecycle.RESOLVED:
            resolved = step.transitions[0]
            break
    assert resolved is not None
    p = EventProjection(sd.GATEWAY)
    for e in _entries(site, [raised, resolved, ack.transitions[0]]):
        p.apply(e)
    assert p.active() == () and p.stats.duplicates == 1
    site.close()


def test_stale_lower_sequence_does_not_regress_state(tmp_path, clock, sd):
    """같은 이벤트가 체크포인트(또는 앞 기록)와 재적용 기록에 둘 다 있으면 한 번만 적용하고 낮은 sequence 로 되돌리지 않는다."""
    site = sd.Site(tmp_path, clock)
    raised = site.step([sd.finding("w-1")]).transitions[0]
    ack = site.tracker.acknowledge(raised.event_id, AckActor.ADMIN, "a", clock.now_ms(), channel="CONTROL_ROOM")
    p = EventProjection(sd.GATEWAY)
    for e in _entries(site, [raised, ack.transitions[0], raised]):
        p.apply(e)
    (s,) = p.active()
    assert s.event_sequence == 2 and s.acknowledged and p.stats.duplicates == 1
    # 체크포인트가 이미 반영한 기록을 다시 적용해도 같다
    entries = _entries(site, [raised, ack.transitions[0]])
    q = EventProjection(sd.GATEWAY, events=[ack.transitions[0].state])
    for e in entries:
        q.apply(e)
    assert q.active() == (ack.transitions[0].state,) and q.stats.duplicates == 2
    site.close()


def test_projection_rejects_non_contiguous_offsets(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock)
    raised = site.step([sd.finding("w-1")]).transitions[0]
    (e,) = _entries(site, [raised])
    p = EventProjection(sd.GATEWAY, last_applied_journal_offset=5)
    with pytest.raises(ReplayError, match="이어지지"):
        p.apply(e)
    other = replace(e, meta={"state": replace(raised.state, gateway_id="tier0-99").to_record()})
    with pytest.raises(ReplayError, match="다른 Tier 0"):
        EventProjection(sd.GATEWAY).apply(other)
    site.close()


def test_checkpoint_store_requires_load_and_rejects_resolved_events(tmp_path, clock, sd):
    site = sd.Site(tmp_path, clock)
    a = site.raise_event("w-1")
    site.close()
    store = CheckpointStore(tmp_path / "cp2", gateway_id=sd.GATEWAY)
    kwargs = {
        "written_at": 1,
        "last_applied_journal_offset": None,
        "last_applied_msg_id": None,
        "config_version": None,
        "last_capture_time": None,
    }
    with pytest.raises(Exception, match="load"):
        store.save(events=[a], **kwargs)
    store.load()
    with pytest.raises(Exception, match="해소"):
        store.save(events=[replace(a, lifecycle=Lifecycle.RESOLVED)], **kwargs)
    with pytest.raises(ValueError):
        CheckpointStore(tmp_path / "cp3", gateway_id=sd.GATEWAY, keep=1)
    assert isinstance(EventState.from_record(a.to_record()), EventState)


def test_new_generation_replaces_corrupt_file_not_the_good_previous_one(tmp_path, clock, sd):
    """반례: 깨진 최신 파일을 남기고 온전한 직전 세대를 지우면, 새 세대가 다시 깨질 때 물러설 곳이 없다."""
    cp1, cp2, _ = _two_generations(tmp_path, clock, sd)
    _truncate(_cp_path(tmp_path, cp2.checkpoint_seq))
    again = sd.Site(tmp_path, clock)
    cp3 = again.checkpoint()
    names = sorted(p.name for p in (tmp_path / "checkpoints").iterdir())
    assert names == [checkpoint_name(cp1.checkpoint_seq), checkpoint_name(cp3.checkpoint_seq)]
    assert again.store.compaction_floor() == cp1.last_applied_journal_offset
    again.close()
