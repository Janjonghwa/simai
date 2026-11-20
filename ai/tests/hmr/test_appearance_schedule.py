"""외형 표본 주기와 작업 신호의 안전한 폴백."""

import json
from types import SimpleNamespace

import pytest

from simai_ai.hmr.work_state import read_work_state


def test_appearance_work_state(tmp_path):
    marker = tmp_path / "tier0.live"
    assert read_work_state(marker, 100, 15) is None
    marker.write_text(json.dumps({"started": True, "work_in_progress": False}), encoding="utf-8")
    now = marker.stat().st_mtime
    assert read_work_state(marker, now, 15) is False
    assert read_work_state(marker, now + 16, 15) is None
    for value in (True, None, 0, "false"):
        marker.write_text(json.dumps({"started": True, "work_in_progress": value}), encoding="utf-8")
        assert read_work_state(marker, marker.stat().st_mtime, 15) is (True if value is True else None)
    marker.write_text("{", encoding="utf-8")
    assert read_work_state(marker, marker.stat().st_mtime, 15) is None
    marker.write_text('{"started": false, "work_in_progress": false}', encoding="utf-8")
    assert read_work_state(marker, marker.stat().st_mtime, 15) is None


def test_appearance_intervals_and_saturation():
    pytest.importorskip("numpy")
    from simai_ai.hmr.appearance import AppearanceAccumulator

    acc = AppearanceAccumulator.__new__(AppearanceAccumulator)
    acc.config = SimpleNamespace(sample_interval_ms=2000)
    acc.last_sample, acc.work_in_progress, acc.low_gain_samples = 1000, None, 0
    assert not acc.sample_due(2999) and acc.sample_due(3000)
    acc.set_work_in_progress(False)
    assert not acc.sample_due(1499) and acc.sample_due(1500)
    acc.set_work_in_progress(True)
    for _ in range(5):
        acc.record_sample_gain(.004)
    assert not acc.sample_due(6999) and acc.sample_due(7000)
    acc.record_sample_gain(.005)
    assert acc.sample_due(3000)
    for _ in range(5):
        acc.record_sample_gain(0)
    acc.set_work_in_progress(None)
    assert acc.low_gain_samples == 0 and acc.sample_due(3000)
    acc.schedule_revision = 1
    acc.low_gain_samples = 5
    acc.set_schedule_revision(2)
    assert acc.low_gain_samples == 0
