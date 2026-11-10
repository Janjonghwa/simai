"""게시 완료 표식과 NPZ 세대를 묶는다. 합성 영상만 쓰며 원본을 남기지 않는다."""

import io
import json
import os
from pathlib import Path

import numpy as np
import pytest

from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter, publication_path
from simai_ai.providers.base import Frame


def frames(value=42):
    return {"cam1": Frame("cam1", 900, "FILE", {"stream_epoch": 3, "frame_sequence": 7},
                          image=np.full((3, 4, 3), value, np.uint8))}


def test_stamp_is_observed_after_data_replace_and_offer_time_is_preserved(tmp_path, monkeypatch):
    path = tmp_path / "input.npz"
    now = [1000]
    writer = LatestChannelWriter(path, clock_ms=lambda: now[0])
    reader = LatestChannelReader(path, require_publication=True)
    real_replace = os.replace
    checked = []
    def replace(src, dst):
        real_replace(src, dst)
        if dst == path:
            assert reader.latest() is None  # 새 NPZ의 완료 표식이 없거나 옛 세대면 채택하지 않는다.
            checked.append(True)
            now[0] += 90
    monkeypatch.setattr(os, "replace", replace)
    try:
        for _ in range(2):
            writer._write({"produced_at_ms": 1000}, frames())
            doc, images = reader.latest()
            assert doc["produced_at_ms"] == 1000 and doc["published_at_ms"] == now[0]
            assert doc["frames"]["cam1"]["capture_time_ms"] == 900
            assert doc["frames"]["cam1"]["frame_ref"] == {"stream_epoch": 3, "frame_sequence": 7}
            assert images["cam1"].tolist() == frames()["cam1"].image.tolist()
        with np.load(path, allow_pickle=False) as z:
            raw = json.loads(z["metadata"].tobytes())
            assert "published_at_ms" not in raw
            assert raw["publication_id"] == doc["publication_id"]
        assert len(checked) == 2
    finally:
        writer.close()


@pytest.mark.parametrize("corruption", ["missing", "generation", "format", "bool", "float", "negative", "list",
                                        "id_null", "id_empty", "id_bool"])
def test_uncommitted_or_invalid_generation_never_reads_images_or_runs_predicate(tmp_path, monkeypatch, corruption):
    path = tmp_path / "input.npz"
    writer = LatestChannelWriter(path, clock_ms=lambda: 1000)
    try:
        writer._write({"produced_at_ms": 900}, frames())
    finally:
        writer.close()
    marker = publication_path(path)
    doc = json.loads(marker.read_text())
    if corruption.startswith("id_"):
        with np.load(path, allow_pickle=False) as z:
            arrays = {k: z[k] for k in z.files}
        metadata = json.loads(arrays["metadata"].tobytes())
        metadata["publication_id"] = {"id_null": None, "id_empty": "", "id_bool": True}[corruption]
        arrays["metadata"] = np.frombuffer(json.dumps(metadata).encode(), dtype=np.uint8)
        np.savez(path, **arrays)
    elif corruption == "missing":
        marker.unlink()
    else:
        if corruption == "generation":
            doc["publication_id"] = "other"
        elif corruption == "format":
            doc["format"] = "other"
        elif corruption == "list":
            doc = []
        else:
            doc["published_at_ms"] = {"bool": True, "float": 1000.5, "negative": -1}[corruption]
        marker.write_text(json.dumps(doc))
    accessed = []
    getitem = np.lib.npyio.NpzFile.__getitem__
    def spy(z, key):
        accessed.append(key)
        return getitem(z, key)
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", spy)
    def predicate(_):
        pytest.fail("표식이 맞지 않는 snapshot을 predicate에 넘김")
    assert LatestChannelReader(path, require_publication=True).latest(predicate=predicate) is None
    assert accessed == ["metadata"]


@pytest.mark.parametrize("replace_during_predicate", [False, True])
def test_old_open_file_cannot_get_new_timestamp(tmp_path, monkeypatch, replace_during_predicate):
    path = tmp_path / "input.npz"
    now = [1000]
    writer = LatestChannelWriter(path, clock_ms=lambda: now[0])
    try:
        writer._write({"produced_at_ms": 900}, frames(1))
        old_bytes = path.read_bytes()
        real_open = Path.open
        def open_old(self, *args, **kw):
            if self == path and args == ("rb",):
                return io.BytesIO(old_bytes)  # Windows에서도 열린 옛 파일 핸들의 의미를 모사한다.
            return real_open(self, *args, **kw)
        def publish_new():
            now[0] = 1100
            writer._write({"produced_at_ms": 1050}, frames(2))
        if not replace_during_predicate:
            publish_new()  # 옛 NPZ + 새 표식이면 거절해야 한다.
        with monkeypatch.context() as patch:
            patch.setattr(Path, "open", open_old)
            def predicate(doc):
                assert doc["published_at_ms"] == 1000
                publish_new()  # 이미 확인한 옛 세대·옛 시각·옛 영상은 일관성을 유지한다.
                return True
            got = LatestChannelReader(path, require_publication=True).latest(predicate=predicate)
        if replace_during_predicate:
            doc, images = got
            assert doc["published_at_ms"] == 1000 and np.all(images["cam1"] == 1)
        else:
            assert got is None
        doc, images = LatestChannelReader(path, require_publication=True).latest()
        assert doc["published_at_ms"] == 1100 and np.all(images["cam1"] == 2)
    finally:
        writer.close()


def test_marker_failure_invalidates_new_npz_and_next_write_recovers(tmp_path, monkeypatch):
    path = tmp_path / "input.npz"
    writer = LatestChannelWriter(path, clock_ms=lambda: 1000)
    try:
        writer._write({}, frames(1))
        real_replace = os.replace
        with monkeypatch.context() as patch:
            def fail_marker(src, dst):
                if dst == publication_path(path):
                    raise OSError("표식 게시 실패")
                real_replace(src, dst)
            patch.setattr(os, "replace", fail_marker)
            with pytest.raises(OSError):
                writer._write({}, frames(2))
        assert LatestChannelReader(path, require_publication=True).latest() is None
        # 공용 reader의 아바타 기본 모드는 완료 표식 장애로 가용성이 줄지 않는다.
        assert np.all(LatestChannelReader(path).latest()[1]["cam1"] == 2)
        assert not list(tmp_path.glob("*.tmp"))
        writer._write({}, frames(3))
        assert np.all(LatestChannelReader(path, require_publication=True).latest()[1]["cam1"] == 3)
    finally:
        writer.close()


def test_same_npz_is_retried_when_delayed_marker_becomes_available(tmp_path):
    path = tmp_path / "input.npz"
    writer = LatestChannelWriter(path, clock_ms=lambda: 1000)
    try:
        writer._write({"produced_at_ms": 900}, frames())
    finally:
        writer.close()
    marker = publication_path(path)
    ready = marker.read_bytes()
    original = path.read_bytes()
    marker.unlink()
    reader = LatestChannelReader(path, require_publication=True)
    assert reader.latest() is None
    legacy_doc, _ = LatestChannelReader(path).latest()
    assert legacy_doc["produced_at_ms"] == 900 and "published_at_ms" not in legacy_doc
    marker.write_bytes(ready)
    assert reader.latest()[0]["published_at_ms"] == 1000
    assert path.read_bytes() == original


def test_legacy_npz_ignores_orphan_marker_in_strict_mode(tmp_path):
    path = tmp_path / "input.npz"
    doc = {"format": "simai.avatar-input@1", "produced_at_ms": 900, "frames": {}, "published_at_ms": 1000}
    np.savez(path, metadata=np.frombuffer(json.dumps(doc).encode(), dtype=np.uint8))
    publication_path(path).write_text("broken marker from an old producer")
    expected = {k: v for k, v in doc.items() if k != "published_at_ms"}
    assert LatestChannelReader(path, require_publication=True).latest() == (expected, {})
    assert LatestChannelReader(path).latest() == (expected, {})


def test_marker_write_delay_is_not_hidden_by_a_later_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "input.npz"
    now = [1000]
    writer = LatestChannelWriter(path, clock_ms=lambda: now[0])
    replace = os.replace
    def delayed(src, dst):
        if dst == publication_path(path):
            now[0] += 200  # 시각 관측 뒤 완료 표식 게시가 늦어지는 경우.
        replace(src, dst)
    monkeypatch.setattr(os, "replace", delayed)
    try:
        writer._write({"produced_at_ms": 500}, frames())
        doc, _ = LatestChannelReader(path, require_publication=True).latest()
        assert doc["published_at_ms"] == 1000 and now[0] - doc["published_at_ms"] == 200
    finally:
        writer.close()
