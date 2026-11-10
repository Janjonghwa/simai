"""채널 진단의 유계성·오류 계측·writer 호출자의 비대기 성질."""

from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import threading

import numpy as np
import pytest

from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter
from simai_ai.io.channel_timing import ChannelTiming


def rows(timing):
    return {(r["stage"], r["camera"]): r for r in timing.summary()["series"]}


def test_recent_percentiles_and_keys_are_bounded_even_with_external_camera_ids():
    timing = ChannelTiming(capacity=4, max_series=2)
    for ms in range(100):
        timing.record("infer", ms, camera="cam1")
    for i in range(1000):
        timing.record("infer", camera=f"cam{i + 2}")
    timing.record("infer", camera="x" * 129)
    summary = timing.summary()
    assert len(summary["series"]) == 2 and summary["overflow"] == 1000
    row = rows(timing)["infer", "cam1"]
    assert row["count"] == 100 and row["window_samples"] == 4
    assert row["ms"] == {"mean": 97.5, "max": 99, "p50": 97, "p95": 99, "p99": 99}


def test_concurrent_recording_and_failed_stage_use_injected_clock():
    now = [1.0]
    timing = ChannelTiming(clock=lambda: now[0])
    with pytest.raises(OSError), timing.measure("write"):
        now[0] += .025
        raise OSError("disk full")
    assert rows(timing)["write", None]["ms"]["p95"] == 25
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: timing.record("count"), range(1000)))
    assert rows(timing)["count", None]["count"] == 1000


def test_writer_offer_does_not_take_diagnostic_lock_or_move_produced_time(tmp_path):
    timing = ChannelTiming()
    writer = LatestChannelWriter(tmp_path / "shared.npz", period_s=.001, clock_ms=lambda: 123, timing=timing)
    entered, release = threading.Event(), threading.Event()
    write = writer._write

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        write(*args)

    writer._write = blocked
    try:
        assert writer.offer({}, {}) and entered.wait(1)
        # writer가 계측 락을 잡아도 Tier 0 offer는 기다리지 않는다.
        with timing._lock:
            assert writer.offer({}, {})
            assert writer._pending[0]["produced_at_ms"] == 123
        release.set()
    finally:
        release.set()
        writer._thread.join(.1)
        writer.close()
    doc, _ = LatestChannelReader(writer.path).latest()
    assert doc["produced_at_ms"] == 123
    stages = {r["stage"] for r in timing.summary()["series"]}
    assert {"write_prepare", "write_npz", "write_replace", "write_total",
            "offer_to_write_start", "offer_to_write_complete"} <= stages
    assert "produced_at_ms" not in json.dumps(timing.summary())


def test_reader_uses_same_open_snapshot_if_path_is_replaced_in_predicate(tmp_path, monkeypatch):
    path, new = tmp_path / "shared.npz", tmp_path / "new.npz"
    def snapshot(target, value):
        doc = {"format": "simai.avatar-input@1", "frames": {"c": {"array": "image"}}, "value": value}
        np.savez(target, metadata=np.frombuffer(json.dumps(doc).encode(), dtype=np.uint8), image=np.array([value]))
    snapshot(path, 1)
    snapshot(new, 2)
    original = path.read_bytes()
    path_open = Path.open
    # Windows의 열린 파일 삭제 제한과 무관하게, 이미 열린 이전 파일 핸들을 모사한다.
    def open_snapshot(self, *args, **kw):
        if self == path and args == ("rb",):
            return io.BytesIO(original)
        return path_open(self, *args, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_snapshot)
        def replace(doc):
            os.replace(new, path)
            return True
        doc, images = LatestChannelReader(path).latest(predicate=replace)
    assert doc["value"] == 1 and images["c"].tolist() == [1]
    doc, images = LatestChannelReader(path).latest()
    assert doc["value"] == 2 and images["c"].tolist() == [2]


def test_reader_records_failure_and_metadata_only_without_image_stage(tmp_path):
    reader = LatestChannelReader(tmp_path / "missing.npz")
    assert reader.latest() is None
    assert rows(reader.timing)["read_error", None]["count"] == 1
    writer = LatestChannelWriter(reader.path)
    try:
        writer._write({}, {})
        assert reader.latest(predicate=lambda _: False)[1] == {}
    finally:
        writer.close()
    stats = rows(reader.timing)
    assert stats["images_skipped", None]["count"] == 1
    assert ("read_images", None) not in stats


def test_writer_distinguishes_npz_write_and_replace_cost(tmp_path, monkeypatch):
    now = [0.0]
    timing = ChannelTiming(clock=lambda: now[0])
    writer = LatestChannelWriter(tmp_path / "shared.npz", timing=timing)
    savez, replace = np.savez, os.replace
    def slow_write(*args, **kw):
        savez(*args, **kw)
        now[0] += .013
    def slow_replace(*args):
        replace(*args)
        now[0] += .004
    monkeypatch.setattr(np, "savez", slow_write)
    monkeypatch.setattr(os, "replace", slow_replace)
    try:
        writer._write({"produced_at_ms": 123}, {})
    finally:
        writer.close()
    assert rows(timing)["write_npz", None]["ms"]["p95"] == 13
    assert rows(timing)["write_replace", None]["ms"]["p95"] == 4
    assert LatestChannelReader(writer.path).latest()[0]["produced_at_ms"] == 123
