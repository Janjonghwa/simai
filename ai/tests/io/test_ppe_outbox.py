"""PPE → Tier 0 outbox(simai_ai.io.ppe_outbox, S15P31A101-507·-565): 순서·상한·오래된 파일·깨진 파일·source_id 맞춤,
그리고 Tier 0 I1Publisher.add_source 로 I1Client 텔레메트리 대기열까지 가는지."""

import json
import os
from pathlib import Path

import pytest

from simai_ai.io.ppe_outbox import PpeOutboxReader, PpeOutboxWriter, ppe_outbox_path

AI_DIR = Path(__file__).resolve().parents[2]  # services 패키지는 설치하지 않는다


def message(seq, *, epoch=1_760_342_000_000, source="tier0-01", msg_type="ppe_state"):
    return {"schema_version": "1.0.0", "msg_type": msg_type, "msg_id": f"00000000-0000-4000-8000-{seq:012d}",
            "sent_at": 1_760_342_400_000 + seq, "source_id": source,
            "payload": {"stream_epoch": epoch, "state_sequence": seq, "people": []}}


def test_path_prefers_env_then_shm_then_state(tmp_path):
    assert ppe_outbox_path({"SIMAI_PPE_OUTBOX": str(tmp_path / "x")}) == tmp_path / "x"
    assert ppe_outbox_path({}, shm_dir=tmp_path) == tmp_path / "simai-ppe-outbox"
    assert ppe_outbox_path({}, root=tmp_path, shm_dir=tmp_path / "missing") == tmp_path / "state" / "ppe-outbox"


def test_reader_returns_messages_in_send_order_and_removes_them(tmp_path):
    writer, reader = PpeOutboxWriter(tmp_path), PpeOutboxReader(tmp_path, source_id="tier0-01")
    for seq in (2, 0, 1):
        assert writer.put(message(seq))
    writer.put(message(0, epoch=1_760_342_999_000))  # 다시 시작한 PPE(더 큰 epoch)는 뒤에 온다
    got = reader.take()
    assert [(m["payload"]["stream_epoch"], m["payload"]["state_sequence"]) for m in got] == [
        (1_760_342_000_000, 0), (1_760_342_000_000, 1), (1_760_342_000_000, 2), (1_760_342_999_000, 0)]
    assert reader.take() == []
    assert not list(tmp_path.iterdir())


def test_reader_takes_at_most_max_batch_per_call(tmp_path):
    writer, reader = PpeOutboxWriter(tmp_path), PpeOutboxReader(tmp_path, source_id="tier0-01", max_batch=2)
    for seq in range(5):
        writer.put(message(seq))
    assert [m["payload"]["state_sequence"] for m in reader.take()] == [0, 1]
    assert [m["payload"]["state_sequence"] for m in reader.take()] == [2, 3]


def test_writer_drops_oldest_beyond_max_pending(tmp_path):
    writer = PpeOutboxWriter(tmp_path, max_pending=3)
    for seq in range(5):
        writer.put(message(seq))
    assert writer.dropped == 2
    got = PpeOutboxReader(tmp_path, source_id="tier0-01").take()
    assert [m["payload"]["state_sequence"] for m in got] == [2, 3, 4]


def test_writer_counts_unwritable_message_without_raising(tmp_path):
    writer = PpeOutboxWriter(tmp_path)
    assert writer.put({"payload": {}}) is False
    assert writer.put({**message(0), "x": float("nan")}) is False
    assert writer.errors == 2 and not list(tmp_path.iterdir())


def test_reader_drops_stale_broken_and_foreign_files(tmp_path):
    writer = PpeOutboxWriter(tmp_path)
    writer.put(message(0))
    old = tmp_path / "1760342000000-000000000000.json"
    os.utime(old, (old.stat().st_mtime - 60, old.stat().st_mtime - 60))
    (tmp_path / "1760342000000-000000000001.json").write_text("{broken", encoding="utf-8")
    writer.put(message(2, msg_type="world_state"))
    (tmp_path / ".1760342000000-000000000003.json.1.tmp").write_text("{}", encoding="utf-8")  # 쓰는 중인 임시 파일
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    reader = PpeOutboxReader(tmp_path, source_id="tier0-01")
    assert reader.take() == []
    assert reader.stats() == {"taken": 0, "stale": 1, "invalid": 2, "errors": 0, "relabeled": 0}
    assert sorted(p.name for p in tmp_path.iterdir()) == [".1760342000000-000000000003.json.1.tmp", "notes.json"]


def test_reader_relabels_source_id_to_its_own_connection(tmp_path):
    PpeOutboxWriter(tmp_path).put(message(0, source="tier0-01"))
    reader = PpeOutboxReader(tmp_path, source_id="tier0-lobby")
    [got] = reader.take()
    assert got["source_id"] == "tier0-lobby" and reader.relabeled == 1


def test_missing_folder_is_empty(tmp_path):
    assert PpeOutboxReader(tmp_path / "none", source_id="tier0-01").take() == []


class _Clock:
    def now_ms(self):
        return 1_760_342_400_000


class _Connector:
    def connect(self):
        raise ConnectionError("offline")


def test_tier0_publisher_relays_outbox_messages_unchanged(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(AI_DIR))
    from services.tier0.i1_client import Envelope, I1Client
    from services.tier0.i1_publisher import I1Publisher

    client = I1Client(_Connector(), on_message=lambda _raw: None, telemetry_limit=3)
    publisher = I1Publisher(client, Envelope("tier0-01", _Clock()), health_payload=lambda: None)
    publisher.add_source(PpeOutboxReader(tmp_path, source_id=publisher.source_id).take)
    PpeOutboxWriter(tmp_path).put(message(7))
    publisher.tick()
    assert client.queue_sizes() == {"reliable": 0, "telemetry": 1}
    assert [m for _, m in client._telemetry] == [message(7)]
    assert json.loads(json.dumps(client._telemetry[0][1])) == message(7)
    assert not any(Path(tmp_path).iterdir())
