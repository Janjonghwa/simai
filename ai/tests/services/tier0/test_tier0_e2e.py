"""Tier 0 진입점 E2E (S15P31A101-171): SIM 입력 → R1 → 현장 경보(I0) → I1, 강제 종료 → 재시작 → 복구 → 재전송 → 스냅샷.

실제 프로세스(`python -m services.tier0 --config ...`)를 띄우고, 로컬 wss 서버(EC2 흉내, test_i1_tls.Ec2Server)와 노트북
시험 상대(tier0_site.Laptop, I0 제어 채널 wss)로 본다. GPU 를 쓰지 않는다(SIM 2D keypoint 가 검출기·Local Tracker 대역).
타이밍을 가정하지 않고 조건 대기(wait_until)로만 진행한다.

확인:
1. R1 발생 → 노트북 local_alert(RAISED) 와 EC2 risk_event_transition(RAISED, evidence_ref = 같은 tick world_state msg_id).
   world_state 에 config_version, health 는 재시작 하한이 풀린 뒤 active_site_alerts 에 R1. 받은 메시지는 모두 계약 통과.
2. 현장 확인(site_ack) → alarm_stop(같은 ack_id) → EC2 에 ACKNOWLEDGED.
3. EC2 단절 중에도 판정·현장 경보(health 목록)는 계속되고 system_health 는 DEGRADED(EC2_LINK_LOST)다(UNSAFE 가 아니다).
4. 강제 종료(kill) → 재시작: 새 연결에서 health → acked_offset 이후 재전송(resent, 오름차순, 같은 msg_id) → event_state_snapshot.
   복원된 R1 은 스냅샷·노트북 목록에 남고(확인 상태 유지) 자동 해소되지 않는다. 재시작 직후 health 는 UNSAFE_UNKNOWN.
5. 종료 신호 → 정상 종료(코드 0, 표시 파일 삭제). 두 프로세스가 EC2 로 보낸 전이와 저널이 빈칸·불일치 없이 같다.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
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
from test_i1_tls import Ec2Server, example, make_cert  # noqa: E402

from simai_ai.eventlog import EventJournal  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402


class DownableEc2(Ec2Server):
    down = False

    def _check(self, connection, request):
        if self.down:
            return connection.respond(503, "ec2 down\n")
        return super()._check(connection, request)


def _start(cfg: Path, env: dict[str, str], log_path: Path) -> subprocess.Popen:
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    log = open(log_path, "ab")  # noqa: SIM115 - 자식 프로세스 수명 동안 연다
    return subprocess.Popen(
        [sys.executable, "-m", "services.tier0", "--config", str(cfg)],
        cwd=AI_DIR, env={**os.environ, "PYTHONUTF8": "1", **env}, stdout=log, stderr=subprocess.STDOUT, **kwargs,
    )


def _graceful(proc: subprocess.Popen) -> int:
    proc.send_signal(signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGTERM)
    return proc.wait(30)


def _transitions(messages: list[dict]) -> list[dict]:
    return [m for m in messages if m["msg_type"] == "risk_event_transition"]


def _r1(payloads, transition=None):
    return [p for p in payloads if p["rule_id"] == "R1" and (transition is None or p["transition"] == transition)]


@pytest.fixture
def site(tmp_path):
    cert, key = make_cert(tmp_path)
    ec2 = DownableEc2(cert, key)
    run = ts.run_sim(tmp_path, ts.scenario(90_000))
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    now = time.time_ns() // 1_000_000
    ts.archive_bundle(tmp_path / "boot", calibration, "cfg-1", now - 1000)
    # 장면 시작을 프로세스가 뜬 뒤로 둔다(사람은 2.5 s 에 구역 안). 재시작한 프로세스도 같은 장면 시각을 이어 간다.
    cfg = ts.write_config(tmp_path / "tier0.json", run_dir=run, state_dir=tmp_path / "state", anchor_wall_ms=now + 4000,
                          archive=tmp_path / "boot", version="cfg-1")
    port = ts.free_port()
    env = ts.tier0_env(cert=cert, key=key, control_port=port, ec2_url=ec2.url)
    laptop = ts.Laptop(port, cert)
    procs: list[subprocess.Popen] = []
    yield {"ec2": ec2, "cfg": cfg, "env": env, "laptop": laptop, "procs": procs, "tmp": tmp_path, "cert": cert}
    laptop.close()
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
    ec2.stop()


def test_sim_r1_site_alert_i1_then_crash_restart_resync_and_journal_consistency(site) -> None:
    ec2, laptop, tmp = site["ec2"], site["laptop"], site["tmp"]
    log_path = tmp / "tier0.log"
    proc_a = _start(site["cfg"], site["env"], log_path)
    site["procs"].append(proc_a)
    laptop.start()

    # 1) R1 발생 → 현장 경보(I0)와 I1 전이
    def r1_raised() -> bool:
        return bool(_r1([m["payload"] for m in laptop.all_messages("local_alert")], None)) and bool(
            _r1([m["payload"] for m in _transitions(ec2.all_messages())], "RAISED"))

    assert ts.wait_until(r1_raised, timeout=40), log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
    [alert] = [m["payload"] for m in laptop.all_messages("local_alert") if m["payload"]["rule_id"] == "R1"][:1]
    assert alert["lifecycle"] == "RAISED" and alert["severity"] == "HIGH" and alert["source"] == "LIVE"
    raised = _r1([m["payload"] for m in _transitions(ec2.all_messages())], "RAISED")[0]
    assert raised["event_id"] == alert["event_id"] and raised["config_version"] == "cfg-1"
    # world_state 는 최신 칸이라 재전송 전이보다 늦게 도착할 수 있다(송신 우선순위). 조건으로 기다린다
    assert ts.wait_until(lambda: any(m["msg_type"] == "world_state" and m["payload"].get("config_version") == "cfg-1"
                                     for m in ec2.all_messages()), timeout=10)
    world_ids = {m["msg_id"]: m for m in ec2.all_messages() if m["msg_type"] == "world_state"}
    # 판정 근거: 같은 tick 의 world_state msg_id (보내졌으면 그 world_state 의 촬영 시각이 판정 관측 시각과 같다)
    if raised["evidence_ref"] in world_ids:
        assert world_ids[raised["evidence_ref"]]["payload"]["t_latest_global"] == raised["observation_time"]

    def released_with_r1() -> bool:
        return any(
            h["payload"]["system_health"] != "UNSAFE_UNKNOWN"
            and any(a["event_id"] == alert["event_id"] for a in h["payload"].get("active_site_alerts", []))
            for h in laptop.all_messages("health")
        )

    assert ts.wait_until(released_with_r1, timeout=15)

    # 2) 현장 확인 → alarm_stop → EC2 ACKNOWLEDGED
    ack_id = laptop.send_site_ack(alert["event_id"], alert["event_sequence"])
    assert ts.wait_until(lambda: any(m["payload"]["ack"]["ack_id"] == ack_id for m in laptop.all_messages("alarm_stop")))
    assert ts.wait_until(lambda: any(p.get("ack", {}).get("ack_id") == ack_id for p in
                                     (m["payload"] for m in _transitions(ec2.all_messages()))))
    acked = max(p["journal_offset"] for p in (m["payload"] for m in _transitions(ec2.messages(0))))
    ec2.send({**example("journal_ack"), "payload": {"gateway_id": ts.GATEWAY, "acked_offset": acked}})

    # 3) EC2 단절: 판정·현장 경보는 계속, system_health 는 DEGRADED(UNSAFE 아님)
    ec2.down = True
    ec2.drop(0)
    mark = len(laptop.messages(0, "health"))

    def offline_health_ok() -> bool:
        late = laptop.messages(0, "health")[mark:]
        return len(late) >= 3 and all(
            h["payload"]["system_health"] == "DEGRADED"
            and any(a["event_id"] == alert["event_id"] and a["acknowledged"] for a in h["payload"]["active_site_alerts"])
            for h in late[1:]
        )

    assert ts.wait_until(offline_health_ok, timeout=15)
    assert len(ec2.connections) == 1

    # 4) 강제 종료 → 재시작
    proc_a.kill()
    proc_a.wait(10)
    assert ts.wait_until(lambda: not laptop.connected.is_set(), timeout=10)
    laptop_conns = len(laptop.connections)
    ec2.down = False
    proc_b = _start(site["cfg"], site["env"], log_path)
    site["procs"].append(proc_b)
    assert ts.wait_until(lambda: len(ec2.connections) == 2, timeout=30), log_path.read_text("utf-8", "replace")[-3000:]
    assert ts.wait_until(lambda: any(m["msg_type"] == "event_state_snapshot" for m in ec2.messages(1)), timeout=20)
    assert ts.wait_until(lambda: len(laptop.connections) > laptop_conns, timeout=20)

    assert ts.wait_until(lambda: len(laptop.messages(laptop_conns, "health")) >= 1)
    first_health = laptop.messages(laptop_conns, "health")[0]["payload"]
    assert first_health["system_health"] == "UNSAFE_UNKNOWN"  # 재시작 직후(재관측·시계 교환 전)
    restored = {a["event_id"]: a for a in first_health["active_site_alerts"]}
    assert alert["event_id"] in restored and restored[alert["event_id"]]["acknowledged"] is True  # 복원 전에 빈 목록 금지

    second = ec2.messages(1)
    kinds = [m["msg_type"] for m in second]
    assert kinds[0] == "health"
    snap_at = kinds.index("event_state_snapshot")
    resent = [m for m in second[:snap_at] if m["msg_type"] == "risk_event_transition"]
    offsets = [m["payload"]["journal_offset"] for m in resent]
    assert offsets == list(range(acked + 1, acked + 1 + len(offsets))) and offsets
    snapshot = second[snap_at]["payload"]
    assert snapshot["journal_offset"] == offsets[-1]
    snap_events = {e["event_id"]: e for e in snapshot["active_events"]}
    assert alert["event_id"] in snap_events and snap_events[alert["event_id"]]["verification"] == "UNVERIFIABLE"
    assert snap_events[alert["event_id"]]["last_valid_observation_at"] is None  # 재시작 직후 근거 없음
    assert any(m["payload"].get("changes") and "RESTORED" in m["payload"]["changes"] for m in resent)

    assert ts.wait_until(lambda: any(h["payload"]["system_health"] != "UNSAFE_UNKNOWN"
                                     for h in laptop.messages(laptop_conns, "health")), timeout=20)
    time.sleep(0.5)

    # 5) 종료 신호 → 정상 종료
    assert _graceful(proc_b) == 0, log_path.read_text("utf-8", "replace")[-3000:]
    assert not (tmp / "state" / "tier0.live").exists()

    everything = ec2.all_messages() + laptop.all_messages()
    for m in everything:
        assert schema_errors(m, m["msg_type"]) == [], (m["msg_type"], schema_errors(m, m["msg_type"])[:3])
    sent: dict[int, dict] = {}
    for m in _transitions(ec2.all_messages()):
        p = m["payload"]
        prev = sent.setdefault(p["journal_offset"], m)
        assert prev["msg_id"] == m["msg_id"]  # 재전송은 같은 msg_id
    with EventJournal.open(tmp / "state" / "journal", clock=SystemClock(), readonly=True) as journal:
        entries = journal.read(0)
    assert sorted(sent) == list(range(len(sent)))  # 빈칸 없음
    for e in entries[: len(sent)]:
        m = sent[e.journal_offset]
        assert m["msg_id"] == e.msg_id
        assert m["payload"]["event_id"] == e.payload["event_id"]
        assert m["payload"]["event_sequence"] == e.payload["event_sequence"]
    assert len(entries) >= len(sent)
    # 확인 기록은 한 번만(재시작·재연결을 넘어도 ack_id 로 멱등)
    assert sum(1 for e in entries if e.meta.get("ack_id") == ack_id) == 1
