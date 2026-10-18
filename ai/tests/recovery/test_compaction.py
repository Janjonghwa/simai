"""저널 정리 경계 (명세 3.2 저널 계약: min(acked_offset, last_applied_journal_offset) 이전만 정리, 부록 D-G3-13).

세그먼트를 기록마다 하나로 두고(segment_max_bytes=1) 보관 기간을 0 으로 해 정리 경계를 offset 단위로 정확히 본다.
"""

import pytest

from simai_ai.eventlog import JournalConfig
from simai_ai.recovery import RecoverySource

SMALL = JournalConfig(segment_max_bytes=1, retention_ms=0)


def _ack(site, sd, offset):
    site.outbox.pump()
    res = site.outbox.on_journal_ack({"gateway_id": sd.GATEWAY, "acked_offset": offset})
    assert res.accepted


def _busy(site, sd, n=3):
    for i in range(n):
        site.raise_event(f"w-{site.journal.next_offset}-{i}")


@pytest.mark.parametrize("acked_first", [True, False], ids=["acked<last_applied", "acked>last_applied"])
def test_compaction_never_passes_min_of_acked_and_last_applied(tmp_path, clock, sd, acked_first):
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    _busy(site, sd)
    gen1 = site.checkpoint()
    assert site.journal.first_offset == 0  # acked_offset 을 모르면 정리하지 않는다
    _busy(site, sd)
    if acked_first:
        acked = 1  # acked < 남은 세대의 last_applied
        _ack(site, sd, acked)
        gen2 = site.checkpoint()
    else:
        gen2 = site.checkpoint()
        _busy(site, sd)
        acked = site.journal.last_offset  # acked > 남은 세대의 last_applied
        _ack(site, sd, acked)
        site.checkpoint()  # gen3: 남은 세대 gen2, gen3 → floor = gen2
    floor = site.store.compaction_floor()
    expected_floor = gen1.last_applied_journal_offset if acked_first else gen2.last_applied_journal_offset
    assert floor == expected_floor
    cutoff = min(acked, floor)
    # cutoff 이하만 지웠다: 남은 첫 offset 이 cutoff+1 (기록마다 세그먼트 하나, 활성 세그먼트는 남김)
    assert site.journal.first_offset == cutoff + 1
    assert site.journal.read(cutoff + 1)[0].journal_offset == cutoff + 1
    site.close()


def test_restart_after_compaction_matches_journal_and_does_not_resurrect_resolved(tmp_path, clock, sd):
    """D-G3-13: ACK 후 정리 직후 종료해도 해소된 이벤트가 활성으로 되살아나지 않고, 정리 전 발생한 활성 이벤트는 남는다."""
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    old = site.raise_event("w-old")  # 정리될 구간에서 발생, 계속 활성
    gone = site.raise_event("w-gone")
    site.hold([sd.finding("w-old"), sd.finding("w-gone")], 2)
    site.resolve("w-gone", others=["w-old"])  # 정리될 구간에서 해소
    site.checkpoint()
    site.hold([sd.finding("w-old")], 2)
    truth = sd.journal_truth(tmp_path, clock)
    _ack(site, sd, site.journal.last_offset)
    site.checkpoint()
    site.checkpoint()
    assert site.journal.first_offset > 0
    site.close()

    again = sd.Site(tmp_path, clock, journal_config=SMALL)
    assert again.result.source is RecoverySource.CHECKPOINT
    sd.assert_matches_journal(again.result, truth)
    ids = {s.event_id for s in again.result.restored}
    assert old.event_id in ids and gone.event_id not in ids
    again.close()


def test_previous_generation_still_replayable_after_compaction(tmp_path, clock, sd):
    """반례: 정리 뒤 최신 체크포인트가 깨져도 직전 세대로 물러설 수 있어야 한다(정리 하한 = 남은 세대의 최솟값)."""
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    site.raise_event("w-1")
    site.checkpoint()
    _busy(site, sd)
    gen_prev = site.checkpoint()
    _busy(site, sd)
    _ack(site, sd, site.journal.last_offset)
    gen_last = site.checkpoint()
    assert site.journal.first_offset == gen_prev.last_applied_journal_offset + 1  # 직전 세대 offset 까지만 정리
    truth = sd.journal_truth(tmp_path, clock)  # 남은 구간 정답 + 아래 비교는 활성 집합 전체
    expected = {s.event_id for s in site.result.projection.active()}
    site.close()
    path = tmp_path / "checkpoints" / f"checkpoint-{gen_last.checkpoint_seq:020d}.json"
    path.write_bytes(b"{broken")
    again = sd.Site(tmp_path, clock, journal_config=SMALL)
    assert again.result.checkpoint == gen_prev
    assert {s.event_id for s in again.result.restored} == expected
    assert set(truth) <= expected
    again.close()


def test_no_compaction_while_any_generation_has_no_offset(tmp_path, clock, sd):
    """저널이 비었을 때 쓴 세대(last_applied None)가 남아 있으면 정리 하한을 모른다 → 정리하지 않는다."""
    site = sd.Site(tmp_path, clock, journal_config=SMALL)
    site.checkpoint()  # 빈 저널
    _busy(site, sd)
    _ack(site, sd, site.journal.last_offset)
    site.checkpoint()
    assert site.store.compaction_floor() is None and site.journal.first_offset == 0
    site.close()
