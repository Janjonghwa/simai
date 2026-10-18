"""재시작 경로의 이전 이벤트 연결 E2E (S15P31A101-178): 실제 프로세스 kill → 재시작 → 같은 위험 재발생 → related_event_id.

test_tier0_e2e(-171)와 같은 방식이다. 실제 프로세스(`python -m services.tier0 --config ...`), 로컬 wss EC2 흉내(Ec2Server),
노트북 시험 상대(tier0_site.Laptop). GPU 를 쓰지 않는다(SIM 2D keypoint). 시각은 조건 대기(wait_until)로만 진행한다.

1. 한 사람이 R1 구역에 서 있다. kill → 재시작하면 새 World ID 의 같은 R1 이 새 이벤트로 발생하고 related_event_id 가 이전
   이벤트다(계약 통과). 이전 이벤트는 해소되지 않고(UNVERIFIABLE 로 목록에 남음) 그 World ID 로 남는다. 재시작한 프로세스의
   world_state 에는 복원 Track(이전 세션 World ID)이 나가지 않는다(복원 위치가 OBSERVED 로 새지 않는다).
   이어서 한 번 더 kill → 재시작(재시작 두 번 연속): 새 R1 은 바로 앞 세션의 이벤트에 이어지고, 앞의 두 이벤트 모두 남는다.
2. 다른 사람이 같은 자리: A 가 구역에 서 있을 때 kill, 내려가 있는 동안 A 는 나가고 B 가 같은 자리에 선다. 재시작 직후 EC2 가
   끊겨 있어도 판정·현장 경보는 나가고, B 의 새 R1 이 생겨도 A 의 이전 경보는 해소되지 않는다. EC2 가 돌아오면 B 의 RAISED 가
   related_event_id 와 함께 재전송된다(연결은 정보일 뿐 같은 사람이라는 뜻이 아니다).
"""

from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_i1_tls import make_cert  # noqa: E402
from test_tier0_e2e import DownableEc2, _graceful, _start, _transitions  # noqa: E402

from simai_ai.providers.contract import schema_errors  # noqa: E402

ANCHOR_DELAY_MS = 4000


def _two_people(duration_ms: int = 90_000) -> dict:
    """A 는 2.5 s 에 구역에 서고 12 s 에 나가 15 s 에 구석에 선다. B 는 구석에 있다가 16 s 에 걸어 19 s 에 같은 자리에 선다."""
    doc = copy.deepcopy(ts.scenario(duration_ms))
    doc["name"] = "tier0-restore-other-person"
    doc["people"] = [
        {"id": "a", "height_m": 1.72, "waypoints": [{"t_ms": 0, "xy": [1.5, 2.0]}, {"t_ms": 2500, "xy": [4.6, 3.8]},
                                                    {"t_ms": 12_000, "xy": [4.6, 3.8]}, {"t_ms": 15_000, "xy": [1.2, 6.3]}]},
        {"id": "b", "height_m": 1.65, "waypoints": [{"t_ms": 0, "xy": [7.8, 6.4]}, {"t_ms": 16_000, "xy": [7.8, 6.4]},
                                                    {"t_ms": 19_000, "xy": [4.6, 3.8]}]},
    ]
    return doc


def _make_site(tmp_path: Path, doc: dict) -> dict:
    cert, key = make_cert(tmp_path)
    ec2 = DownableEc2(cert, key)
    run = ts.run_sim(tmp_path, doc)
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    now = time.time_ns() // 1_000_000
    ts.archive_bundle(tmp_path / "boot", calibration, "cfg-1", now - 1000)
    anchor = now + ANCHOR_DELAY_MS
    cfg = ts.write_config(tmp_path / "tier0.json", run_dir=run, state_dir=tmp_path / "state", anchor_wall_ms=anchor,
                          archive=tmp_path / "boot", version="cfg-1")
    port = ts.free_port()
    env = ts.tier0_env(cert=cert, key=key, control_port=port, ec2_url=ec2.url)
    return {"ec2": ec2, "cfg": cfg, "env": env, "laptop": ts.Laptop(port, cert), "procs": [], "tmp": tmp_path,
            "anchor": anchor, "log": tmp_path / "tier0.log"}


def _close(site: dict) -> None:
    site["laptop"].close()
    for proc in site["procs"]:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    site["ec2"].stop()


@pytest.fixture
def walker(tmp_path):
    site = _make_site(tmp_path, ts.scenario(90_000))
    yield site
    _close(site)


@pytest.fixture
def other_person(tmp_path):
    site = _make_site(tmp_path, _two_people())
    yield site
    _close(site)


def _log_tail(site: dict) -> str:
    return site["log"].read_text("utf-8", "replace")[-4000:]


def _scene_ms(site: dict) -> int:
    return time.time_ns() // 1_000_000 - site["anchor"]


def _r1_raised(messages: list[dict]) -> list[dict]:
    return [m["payload"] for m in _transitions(messages)
            if m["payload"]["rule_id"] == "R1" and m["payload"]["transition"] == "RAISED"]


def _resolved_ids(messages: list[dict]) -> set[str]:
    return {m["payload"]["event_id"] for m in _transitions(messages) if m["payload"]["transition"] == "RESOLVED"}


def _local_r1(laptop) -> list[dict]:
    return [m["payload"] for m in laptop.all_messages("local_alert") if m["payload"]["rule_id"] == "R1"]


def _checkpoint_track_ids(site: dict) -> set[str]:
    """지금 디스크에 있는 체크포인트들의 Track World ID(내부 형식을 그대로 읽는다)."""
    ids: set[str] = set()
    for path in (site["tmp"] / "state" / "checkpoints").glob("checkpoint-*.json"):
        try:
            body = json.loads(path.read_text(encoding="utf-8"))["body"]
        except (OSError, ValueError, KeyError):
            continue  # 쓰는 중
        ids |= {t["world_id"] for t in body["tracks"]}
    return ids


def _marker_restore(site: dict) -> dict | None:
    try:
        doc = json.loads((site["tmp"] / "state" / "tier0.live").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return (doc.get("status") or {}).get("restore_link")


def _world_state_ids(messages: list[dict]) -> set[str]:
    return {t["world_id"] for m in messages if m["msg_type"] == "world_state" for t in m["payload"]["tracks"]}


def _world_states_since(ec2, conn_from: int) -> int:
    return sum(1 for i in range(conn_from, len(ec2.connections)) for m in ec2.messages(i) if m["msg_type"] == "world_state")


def _health_alert_ids(health: dict) -> set[str]:
    return {a["event_id"] for a in health["payload"].get("active_site_alerts", [])}


def _later_healths(laptop, conn_index: int, n: int) -> list[dict]:
    """재시작한 프로세스와의 노트북 연결(conn_index)이 생길 때까지 기다리고, 그 연결에서 지금부터 health n 개를 모은다."""
    assert ts.wait_until(lambda: len(laptop.connections) > conn_index, timeout=30)
    mark = len(laptop.messages(conn_index, "health"))
    assert ts.wait_until(lambda: len(laptop.messages(conn_index, "health")) >= mark + n, timeout=15)
    return laptop.messages(conn_index, "health")[mark:]


def _kill(site: dict, proc) -> None:
    proc.kill()
    proc.wait(10)
    assert ts.wait_until(lambda: not site["laptop"].connected.is_set(), timeout=10)


def _contracts_ok(messages: list[dict]) -> None:
    for m in messages:
        errors = schema_errors(m, m["msg_type"])
        assert errors == [], (m["msg_type"], errors[:3])


def _raise_and_checkpoint(site: dict, proc, known: set[str], conn_from: int = 0) -> dict:
    """새 R1 RAISED(known 밖)가 EC2 에 오고, 그 사람의 Track 이 체크포인트에 실릴 때까지 기다린다."""
    ec2 = site["ec2"]

    def fresh() -> list[dict]:
        msgs = [m for i in range(conn_from, len(ec2.connections)) for m in ec2.messages(i)]
        return [p for p in _r1_raised(msgs) if p["event_id"] not in known]

    assert ts.wait_until(lambda: bool(fresh()), timeout=40), _log_tail(site)
    raised = fresh()[0]
    assert ts.wait_until(lambda: set(raised["world_ids"]) <= _checkpoint_track_ids(site), timeout=10), _log_tail(site)
    return raised


def test_restart_relinks_same_risk_keeps_previous_alert_and_restored_tracks_stay_out_of_world_state(walker) -> None:
    site, ec2, laptop = walker, walker["ec2"], walker["laptop"]
    proc_a = _start(site["cfg"], site["env"], site["log"])
    site["procs"].append(proc_a)
    laptop.start()
    first = _raise_and_checkpoint(site, proc_a, set())
    assert first.get("related_event_id") is None
    old_ids = _checkpoint_track_ids(site)
    _kill(site, proc_a)

    # 1) 재시작: 같은 사람·같은 위험 → 새 이벤트 + related_event_id
    conns = len(ec2.connections)
    laptop_conns = len(laptop.connections)
    proc_b = _start(site["cfg"], site["env"], site["log"])
    site["procs"].append(proc_b)
    assert ts.wait_until(lambda: len(ec2.connections) > conns, timeout=30), _log_tail(site)
    second = _raise_and_checkpoint(site, proc_b, {first["event_id"]}, conn_from=conns)
    assert second["related_event_id"] == first["event_id"], second
    assert not set(second["world_ids"]) & old_ids
    # 표시 파일은 5 s 마다 쓴다
    assert ts.wait_until(lambda: (_marker_restore(site) or {}).get("state") == "ACCEPTED", timeout=12)
    assert set(first["world_ids"]) <= set(_marker_restore(site)["restored"])
    # 이전 경보는 해소되지 않고 노트북 목록에 남는다(새 이벤트가 생긴 뒤에도)
    for h in _later_healths(laptop, laptop_conns, 3):
        assert {first["event_id"], second["event_id"]} <= _health_alert_ids(h)
    assert ts.wait_until(lambda: _world_states_since(ec2, conns) >= 5, timeout=10)
    session_b = [m for i in range(conns, len(ec2.connections)) for m in ec2.messages(i)]
    assert first["event_id"] not in _resolved_ids(session_b)
    seen = _world_state_ids(session_b)
    assert seen and not seen & old_ids  # 복원 Track 은 world_state 에 나가지 않는다

    # 2) 재시작 두 번 연속: 바로 앞 세션 이벤트에 잇고, 앞의 두 이벤트 모두 남는다
    old_ids_b = _checkpoint_track_ids(site) - old_ids
    _kill(site, proc_b)
    conns_c = len(ec2.connections)
    laptop_conns_c = len(laptop.connections)
    proc_c = _start(site["cfg"], site["env"], site["log"])
    site["procs"].append(proc_c)
    assert ts.wait_until(lambda: len(ec2.connections) > conns_c, timeout=30), _log_tail(site)
    third = _raise_and_checkpoint(site, proc_c, {first["event_id"], second["event_id"]}, conn_from=conns_c)
    assert third["related_event_id"] == second["event_id"], third
    for h in _later_healths(laptop, laptop_conns_c, 3):
        assert {first["event_id"], second["event_id"], third["event_id"]} <= _health_alert_ids(h)
    assert ts.wait_until(lambda: _world_states_since(ec2, conns_c) >= 5, timeout=10)
    session_c = [m for i in range(conns_c, len(ec2.connections)) for m in ec2.messages(i)]
    assert not _resolved_ids(session_c) & {first["event_id"], second["event_id"]}
    seen_c = _world_state_ids(session_c)
    assert seen_c and not seen_c & (old_ids | old_ids_b)

    assert _graceful(proc_c) == 0, _log_tail(site)
    _contracts_ok(ec2.all_messages() + laptop.all_messages())


def test_other_person_at_same_spot_after_restart_with_ec2_down_does_not_resolve_previous_alert(other_person) -> None:
    site, ec2, laptop = other_person, other_person["ec2"], other_person["laptop"]
    proc_a = _start(site["cfg"], site["env"], site["log"])
    site["procs"].append(proc_a)
    laptop.start()
    alert_a = _raise_and_checkpoint(site, proc_a, set())
    assert _scene_ms(site) < 11_000, "A 가 구역을 떠나기 전에 내려야 한다(시험 기계가 너무 느리다)"
    old_ids = _checkpoint_track_ids(site)
    _kill(site, proc_a)

    # 내려가 있는 동안 A 는 나가고 B 가 같은 자리에 선다. 재시작 직후 EC2 는 끊겨 있다
    ec2.down = True
    assert ts.wait_until(lambda: _scene_ms(site) >= 19_500, timeout=30)
    conns = len(ec2.connections)
    laptop_conns = len(laptop.connections)
    proc_b = _start(site["cfg"], site["env"], site["log"])
    site["procs"].append(proc_b)
    assert ts.wait_until(lambda: len(laptop.connections) > laptop_conns, timeout=30), _log_tail(site)

    def b_alert() -> list[str]:
        # 재시작 직후 노트북이 다시 붙기 전에 발생하면 local_alert 는 놓친다. 현장 경보 목록(health)이 맞춘다(-161)
        found = [p["event_id"] for p in _local_r1(laptop) if p["lifecycle"] == "RAISED"]
        found += [a["event_id"] for h in laptop.messages(laptop_conns, "health")
                  for a in h["payload"].get("active_site_alerts", []) if a["rule_id"] == "R1"]
        return [e for e in dict.fromkeys(found) if e != alert_a["event_id"]]

    assert ts.wait_until(lambda: bool(b_alert()), timeout=30), _log_tail(site)  # EC2 없이도 현장 경보는 나간다
    [event_b] = b_alert()
    assert len(ec2.connections) == conns  # 아직 EC2 에 붙지 못했다
    # B 가 같은 자리에서 관측돼도 A 의 이전 경보는 해소되지 않는다(해소 대기 1.5 s 를 넉넉히 넘겨 본다)
    for h in _later_healths(laptop, laptop_conns, 4):
        assert {alert_a["event_id"], event_b} <= _health_alert_ids(h)
    assert not any(p["event_id"] == alert_a["event_id"] and p["lifecycle"] == "RESOLVED" for p in _local_r1(laptop))
    assert ts.wait_until(lambda: (_marker_restore(site) or {}).get("state") == "ACCEPTED", timeout=12)

    # EC2 가 돌아오면 B 의 RAISED 가 related_event_id 와 함께 (재)전송된다. A 의 해소는 없다
    ec2.down = False
    assert ts.wait_until(lambda: any(p["event_id"] == event_b for p in _r1_raised(ec2.all_messages())),
                         timeout=40), _log_tail(site)
    [raised_b] = [p for p in _r1_raised(ec2.all_messages()) if p["event_id"] == event_b][:1]
    assert raised_b["related_event_id"] == alert_a["event_id"]  # 정보(같은 사람이라는 뜻이 아니다)
    assert not set(raised_b["world_ids"]) & old_ids
    assert ts.wait_until(lambda: any(m["msg_type"] == "event_state_snapshot" for m in ec2.messages(-1)), timeout=20)
    snapshot = [m for m in ec2.messages(-1) if m["msg_type"] == "event_state_snapshot"][-1]["payload"]
    snap = {e["event_id"]: e for e in snapshot["active_events"]}
    assert alert_a["event_id"] in snap and snap[alert_a["event_id"]]["verification"] == "UNVERIFIABLE"
    assert alert_a["event_id"] not in _resolved_ids(ec2.all_messages())
    def session_b() -> list[dict]:
        return [m for i in range(conns, len(ec2.connections)) for m in ec2.messages(i)]

    # world_state 는 최신 칸이라 재전송·스냅샷 뒤에 온다
    assert ts.wait_until(lambda: len([m for m in session_b() if m["msg_type"] == "world_state"]) >= 5, timeout=10)
    seen = _world_state_ids(session_b())
    assert seen and not seen & old_ids

    assert _graceful(proc_b) == 0, _log_tail(site)
    _contracts_ok(ec2.all_messages() + laptop.all_messages())
