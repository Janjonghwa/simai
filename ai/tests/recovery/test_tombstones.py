"""해소 기록(tombstone)과 형식 (리뷰 M1·M2: 체크포인트 경계를 넘은 과거 전이, 이전 형식 체크포인트·저널).

M1 재현: 해소 → 체크포인트 → 같은 이벤트의 과거 전이가 저널에 다시 기록됨 → 재시작. 계속 돌던 투영은 활성 0건인데
재시작하면 되살아나던 결함이다. 재시작 결과는 같은 저널을 처음부터 끝까지 적용한 결과와 같아야 한다.
"""

import hashlib
import json
from pathlib import Path

import pytest

from simai_ai.eventlog import TRANSITION_KIND, EventJournal
from simai_ai.recovery import LegacyJournalError, RecoverySource
from simai_ai.recovery.checkpoint import checkpoint_name


def _cp(root: Path, seq: int) -> Path:
    return root / "checkpoints" / checkpoint_name(seq)


def _raise_and_resolve(site, sd, world_id):
    raised = site.step([sd.finding(world_id)]).transitions[0]
    site.resolve(world_id)
    return raised


def _stale_append(site, transitions):
    """해소된 이벤트의 과거 전이를 공개 API 로 다시 기록한다(중복 기록·버그 흉내)."""
    for t in transitions:
        site.journal.append_transition(t)
    site.result.projection.catch_up(site.journal)


def test_resolved_before_checkpoint_is_not_resurrected_by_later_stale_transition(tmp_path, clock, sd):
    """리뷰 M1(같은 프로세스): 계속 돈 투영과 재시작 결과가 같다(활성 0건)."""
    site = sd.Site(tmp_path, clock)
    raised = _raise_and_resolve(site, sd, "w-1")
    cp = site.checkpoint()
    assert cp.events == () and [t.event_id for t in cp.tombstones] == [raised.event_id]
    _stale_append(site, [raised])
    assert site.result.projection.active() == ()
    site.close()
    again = sd.Site(tmp_path, clock)
    assert again.result.source is RecoverySource.CHECKPOINT
    assert again.result.restored == () and again.result.stats.duplicates == 1
    assert again.result.restore_step.transitions == ()
    again.close()


def test_many_tombstones_evicted_ids_are_still_rejected_by_watermark(tmp_path, clock, sd):
    """반례(tombstone 이 많을 때): 상한(여기서는 3)을 넘어 밀려난 이벤트의 과거 전이도 되살리지 않는다.
    밀려난 이벤트는 판정 시각 watermark 로, 남은 것은 sequence 로 거른다. 그 뒤의 새 발생은 그대로 받는다."""
    site = sd.Site(tmp_path, clock, max_tombstones=3)
    raised = [_raise_and_resolve(site, sd, f"w-{i}") for i in range(6)]
    cp = site.checkpoint()
    assert [t.event_id for t in cp.tombstones] == [r.event_id for r in raised[-3:]]
    assert cp.resolved_watermark is not None
    assert cp.resolved_watermark >= raised[2].state.evaluated_at
    _stale_append(site, [raised[0], raised[5]])  # 밀려난 것 하나, 남은 것 하나
    assert site.result.projection.active() == ()
    fresh = site.raise_event("w-new")  # 정상 새 발생
    site.close()
    again = sd.Site(tmp_path, clock, max_tombstones=3)
    r = again.result
    assert [s.event_id for s in r.restored] == [fresh.event_id]
    assert (r.stats.stale, r.stats.duplicates) == (1, 1)
    again.close()


def test_stale_entries_rejected_during_full_journal_replay_with_eviction(tmp_path, clock, sd):
    """체크포인트 없이 저널 처음부터 적용하는 중에 밀려나도 같은 결과다(계속 돈 투영 = 재시작)."""
    site = sd.Site(tmp_path, clock, max_tombstones=2)
    raised = [_raise_and_resolve(site, sd, f"w-{i}") for i in range(5)]
    _stale_append(site, [raised[0], raised[1]])
    assert site.result.projection.active() == ()
    site.close()
    again = sd.Site(tmp_path, clock, max_tombstones=2)
    assert again.result.source is RecoverySource.JOURNAL and again.result.restored == ()
    again.close()


@pytest.mark.parametrize("resolved_between", [False, True], ids=["resolved_before_gen1", "resolved_between_gens"])
def test_tombstone_after_falling_back_to_previous_generation(tmp_path, clock, sd, resolved_between):
    """반례(세대 물러섬 뒤): 최신 세대가 깨져 직전 세대로 물러서도 해소된 이벤트의 과거 전이를 되살리지 않는다.
    직전 세대에 tombstone 이 있거나(해소가 그 전), 재적용 구간에서 RESOLVED 를 다시 배운다(해소가 두 세대 사이)."""
    site = sd.Site(tmp_path, clock)
    other = site.raise_event("w-keep")
    if resolved_between:
        site.checkpoint()
        raised = site.step([sd.finding("w-1"), sd.finding("w-keep")]).transitions
        raised = next(t for t in raised if "w-1" in t.state.world_ids)
        site.resolve("w-1", others=["w-keep"])
    else:
        raised = site.step([sd.finding("w-1"), sd.finding("w-keep")]).transitions
        raised = next(t for t in raised if "w-1" in t.state.world_ids)
        site.resolve("w-1", others=["w-keep"])
        site.checkpoint()
    site.hold([sd.finding("w-keep")])
    gen2 = site.checkpoint()
    _stale_append(site, [raised])
    site.close()
    _cp(tmp_path, gen2.checkpoint_seq).write_bytes(b"{broken")
    again = sd.Site(tmp_path, clock)
    r = again.result
    assert r.checkpoint is not None and r.checkpoint.checkpoint_seq == gen2.checkpoint_seq - 1
    assert [s.event_id for s in r.restored] == [other.event_id]
    again.close()


def _as_v1(path: Path) -> None:
    """체크포인트 파일을 형식 1(해소 기록 없음)로 바꾼다. 해시는 맞춘다."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["format_version"] = 1
    for key in ("resolved", "resolved_watermark"):
        del doc["body"][key]
    body = json.dumps(doc["body"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    doc["sha256"] = hashlib.sha256(body).hexdigest()
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


def test_old_format_checkpoint_is_not_used_and_journal_still_knows_resolution(tmp_path, clock, sd):
    """반례(옛 형식 체크포인트): 형식 1 은 해소 기록이 없어 그대로 쓰면 M1 이 다시 생긴다. 읽지 않고 저널 전체로 물러서며,
    저널 전체 재적용은 해소를 다시 배워 과거 전이를 되살리지 않는다."""
    site = sd.Site(tmp_path, clock)
    raised = _raise_and_resolve(site, sd, "w-1")
    keep = site.raise_event("w-keep")
    cp = site.checkpoint()
    _stale_append(site, [raised])
    site.close()
    _as_v1(_cp(tmp_path, cp.checkpoint_seq))
    again = sd.Site(tmp_path, clock)
    r = again.result
    assert r.source is RecoverySource.JOURNAL
    assert len(r.rejected) == 1 and "이전 형식" in r.rejected[0].reason
    assert [s.event_id for s in r.restored] == [keep.event_id]
    again.close()


def test_legacy_journal_without_state_refuses_with_procedure(tmp_path, clock, sd):
    """리뷰 M2(Lead 결정: bridge 없음): meta.state 없는 이전 형식 저널은 원인과 조치를 담은 오류로 시작을 거부한다.
    체크포인트가 있어도 재적용 전에 첫 기록으로 미리 알아본다."""
    helper = sd.Site(tmp_path / "helper", clock)
    step = helper.step([sd.finding("w-dev")], journal=False)
    helper.close()
    root = tmp_path / "site"
    with EventJournal.open(root / "journal", clock=clock) as j:
        for t in step.transitions:  # 이전 코드처럼 payload 만 기록(meta.state 없음)
            j.append(TRANSITION_KIND, t.to_payload(), {"evidence_ref": sd.WORLD_STATE_MSG_ID})
    with pytest.raises(LegacyJournalError) as err:
        sd.Site(root, clock)
    message = str(err.value)
    assert "meta.state 없는 이전 형식 저널" in message and "보관 위치로 옮기고 새로 시작" in message
    assert "tier0-recovery.md" in message
    assert not (root / "checkpoints").exists()  # 체크포인트를 읽기 전에(재적용 전에) 미리 알아봤다
