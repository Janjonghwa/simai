#!/usr/bin/env python3
"""Network Volume 강제 종료 복구 실측 (S15P31A101-92, GPU 1차 세션 큐 e 항목). 부록 D-G3-09·11·13·14, 15.1 G3.

두 부분으로 나눠 같은 볼륨 경로에서 돈다. GPU 를 쓰지 않는다(SIM 2D keypoint).

1. 서비스(S15P31A101-171 실제 Tier 0 프로세스 `python -m services.tier0`, SIM 입력, 로컬 wss EC2 흉내·노트북 시험 상대):
   - 전이 직후: 노트북이 R1 local_alert(RAISED)를 받자마자 kill -9 (저널 fsync 전일 수도 있다)
   - ACK 직후: 현장 확인(site_ack) → alarm_stop 을 받자마자 kill -9
   - 체크포인트 기록 중: 체크포인트 주기를 50 ms 로 두고 checkpoints/*.tmp 가 보이면 kill -9 (여러 번)
   매번 다시 띄우고, 종료 시점 저널을 payload 로만 접은 정답(site_driver.journal_truth)과
   (a) 같은 상태 디렉터리 사본에서 recover 한 결과(-51 assert_matches_journal), (b) 다시 띄운 서비스가 저널에 쓴 RESTORED 전이와
   event_state_snapshot, (c) 재시작 첫 health(UNSAFE_UNKNOWN)를 비교한다. EC2 로 나간 전이가 저널에 없는지(D-G3-18),
   저널 전이가 결국 EC2 에 전달됐는지(D-G3-14), 복원 이벤트가 사람이 그대로 있는데 해소됐는지(D-G3-09, 구역 대상은 감시 확보
   근거가 해소 유지 시간 동안 이어진 뒤의 해소만 정상: restored_resolution_violations)도 센다.
2. 결정적 지점(S15P31A101-51 tests/recovery 의 강제 종료 시험을 볼륨 경로로 그대로 부른다): 전이 직후, 체크포인트 임시 파일
   fsync 뒤·rename 전, ACK→정리(compact) 직후(D-G3-13), 해소 뒤 과거 RAISED 재기록, 바쁜 임의 시점(seed).

    python deploy/scripts/crash_recovery_g3.py --volume-dir /workspace/simai/runs/crash92      # Pod (Network Volume)
    python deploy/scripts/crash_recovery_g3.py --volume-dir C:/tmp/crash92                    # 노트북 드라이런(로컬 디렉터리)

ai/ 의 Python 환경(시험 의존성 pytest·cryptography·websockets 포함)으로 돌린다. 결과: <volume-dir>/<UTC 시각>/result.json,
$SIMAI_RUN_DIR 가 있으면 그 아래 crash92.json·metrics.json(스칼라). 시험용 TLS 인증서·키는 볼륨이 아니라 임시 디렉터리에 만들고 지운다.
종료 코드: 0 G3 강제 종료 항목 모두 0건(PASS), 1 위반 있음(FAIL), 2 시나리오가 돌지 못함(판정 불가).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AI = REPO / "ai"
for p in (AI / "tests" / "recovery", AI / "tests" / "services" / "tier0", AI):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
os.environ.setdefault("PYTHONUTF8", "1")

ITEMS = ("D-G3-09", "D-G3-11", "D-G3-13", "D-G3-14", "D-G3-18")
ITEM_TEXT = {
    "D-G3-09": "재시작: 활성 이벤트 자동 해소, 재관측 전 System Health 가 UNSAFE_UNKNOWN 이 아님",
    "D-G3-11": "체크포인트 기록 중·전이 직후 강제 종료 시 복구 상태가 저널과 어긋남",
    "D-G3-13": "ACK 후 outbox 정리 직후 강제 종료 시 해소 이벤트가 되살아남",
    "D-G3-14": "저널 기록 직후·전송 직전 강제 종료 시 전이가 EC2 에 전달되지 않음",
    "D-G3-18": "EC2 로 나간 전이가 저널(fsync)에 없음",
}


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]}Z] {msg}", flush=True)


class Tally:
    """G3 항목별 위반 수와 시나리오 기록. 시나리오가 예외로 끝나면 errors 에 넣는다(위반 0건으로 세지 않는다)."""

    def __init__(self) -> None:
        self.counts = {k: 0 for k in ITEMS}
        self.scenarios: list[dict] = []
        self.errors: list[dict] = []

    def violate(self, item: str, scenario: dict, why: str) -> None:
        self.counts[item] += 1
        scenario.setdefault("violations", []).append({"item": item, "why": why})
        log(f"  위반 {item}: {why}")


# ---------------------------------------------------------------- 1. 서비스 강제 종료


def _journal_entries(state: Path):
    from simai_ai.eventlog import EventJournal
    from simai_ai.risk import SystemClock

    jdir = state / "journal"
    if not jdir.is_dir():
        return []
    with EventJournal.open(jdir, clock=SystemClock(), readonly=True) as j:
        first = j.first_offset
        return list(j.iter_from(first if first is not None else j.next_offset))


def restored_resolution_violations(transitions, restored: dict[str, int], hold_ms) -> tuple[list[str], list[str]]:
    """D-G3-09 '재시작: 활성 이벤트 자동 해소'. (위반 문장 목록, 근거를 갖춰 해소된 구역 대상 복원 이벤트 ID 목록)을 돌려준다.

    transitions: EC2 로 나간 전이 payload(재전송 중복 포함), restored: event_id → 처음 복원된 RESTORED 전이의 sequence,
    hold_ms: 규칙 ID → 해소 유지 시간(Tier 0 설정 그대로). 처음 복원 뒤 전이를 sequence 순서로 따라가며 복원 구간(RESTORED 전이부터
    다음 해소까지)마다 그 구간의 해소를 본다. 복원 구간이 해소로 끝나고 재개(같은 event_id)된 뒤의 해소는 복원 이벤트의 해소가 아니고,
    다시 RESTORED 가 오면 새 복원 구간이다. 처음 복원 전에 해소됐다가 재개돼 복원된 이벤트의 예전 해소도 세지 않는다.
    - 사람 대상: 재시작 뒤 그 사람은 새 World ID 로만 보여 이전 대상의 재관측으로 해소를 확인할 수 없다(recovery.relink).
      복원 구간의 해소는 위반이다.
    - 구역 대상(R7 형태): 복원 보호(S15P31A101-174)대로 판정의 감시 확보 근거가 있을 때만 VERIFIED 가 되고, 그 VERIFIED 가
      해소 유지 시간 동안 이어져야 해소된다(명세 6.4 유효 관측으로 해소 확인). 그 구간의 전이로 보이지 않으면(VERIFIED 없음·중간에
      UNVERIFIABLE·유지 시간 미달) 위반이다. 부하로 재시작 직후 판정이 늦어지면 시동 때 잠깐 난 R7(아직 프레임이 없는 카메라는
      정상으로 세지 않음)이 해소 전에 종료·복원되고 다음 프로세스에서 근거로 해소된다(S15P31A101-198).
    - 운영자 종료(I1 risk_event_transition 2.0.0 resolution, S15P31A101-200): 관리자가 누가·왜(resolution 의 kind OPERATOR_CLOSED·
      close_id·actor_id·reason)를 남기고 닫은 구역 대상 해소는 근거 있는 해소로 본다(zone_ok). 그 값이 비었거나 사람 대상이면
      위반이다. 관측 VERIFIED 유지는 보지 않는다. resolution 이 없는 해소는 관측 해소로 본다(lifecycle 의 changes 는 보지 않는다).
    - 처음 복원과 어떤 해소 사이의 sequence 가 빠지면 구간을 알 수 없어 위반으로 센다(모르면 통과 아님).
    """
    by_event: dict[str, dict[int, dict]] = {}
    for p in transitions:
        by_event.setdefault(p["event_id"], {})[p["event_sequence"]] = p
    bad: list[str] = []
    zone_ok: list[str] = []
    for eid, first in sorted(restored.items()):
        seqs = by_event.get(eid, {})
        last_resolved = max((s for s, p in seqs.items() if s > first and p["transition"] == "RESOLVED"), default=None)
        if last_resolved is None:
            continue
        missing = [s for s in range(first, last_resolved) if s not in seqs]
        if missing:
            bad.append(f"복원 이벤트 {eid} 의 해소 전 전이(sequence {missing[:5]})가 빠져 복원 구간·근거를 확인할 수 없음")
            continue
        protected = False  # 지금 복원 구간 안인가
        since = None  # 복원 구간 안에서 마지막으로 이어진 VERIFIED 의 시작 관측 시각
        for s in range(first, last_resolved + 1):
            p = seqs[s]
            rule = p["rule_id"]
            if "RESTORED" in (p.get("changes") or []):
                protected, since = True, None
                continue
            if p["transition"] == "RESOLVED" and p.get("resolution") is not None:
                res = p["resolution"] if isinstance(p["resolution"], dict) else {}
                if p["world_ids"]:
                    bad.append(f"복원 이벤트 {eid}({rule}) 가 사람 대상인데 운영자 종료됨 (sequence {s})")
                elif res.get("kind") != "OPERATOR_CLOSED" or not all(
                        isinstance(res.get(k), str) and res[k] for k in ("close_id", "actor_id", "reason")):
                    bad.append(f"구역 대상 복원 이벤트 {eid}({rule}) 의 운영자 종료에 누가·왜(close_id·actor_id·reason)가 없음 "
                               f"(sequence {s})")
                elif protected and eid not in zone_ok:
                    zone_ok.append(eid)
                protected, since = False, None
                continue
            if p["transition"] == "RESOLVED":
                if protected and p["world_ids"]:
                    bad.append(f"복원 이벤트 {eid}({rule}) 가 사람이 그대로 있는데 해소됨 (sequence {s})")
                elif protected:
                    held = None if since is None else p["observation_time"] - since
                    if held is None or held < hold_ms(rule):
                        bad.append(f"구역 대상 복원 이벤트 {eid}({rule}) 가 감시 확보 근거 유지 없이 해소됨 "
                                   f"(sequence {s}, VERIFIED 유지 {held} ms, 기준 {hold_ms(rule)} ms)")
                    elif eid not in zone_ok:
                        zone_ok.append(eid)
                protected, since = False, None  # 해소로 복원 구간이 끝난다(재개 뒤 해소는 복원 이벤트의 해소가 아님)
                continue
            if p["verification"] != "VERIFIED":
                since = None
            elif since is None:
                since = p["observation_time"]
    return bad, zone_ok


class ServiceRun:
    """실제 Tier 0 프로세스를 같은 상태 디렉터리로 kill -9 · 재시작하며 비교한다."""

    def __init__(self, base: Path, secrets: Path, tally: Tally, *, duration_ms: int, checkpoint_rounds: int,
                 timeout_s: float) -> None:
        import tier0_site as ts
        from test_i1_tls import make_cert
        from test_tier0_e2e import DownableEc2

        self.ts = ts
        self.base = base
        self.tally = tally
        self.timeout_s = timeout_s
        self.checkpoint_rounds = checkpoint_rounds
        base.mkdir(parents=True, exist_ok=True)
        cert, key = make_cert(secrets)  # 개인 키는 볼륨이 아니라 임시 디렉터리(결과 묶음에 섞이지 않게)
        self.ec2 = DownableEc2(cert, key)
        run = ts.run_sim(base, ts.scenario(duration_ms))
        calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
        now = now_ms()
        ts.archive_bundle(base / "boot", calibration, "cfg-1", now - 1000)
        self.anchor = now + 4000
        self.state = base / "state"
        kw = dict(run_dir=run, state_dir=self.state, anchor_wall_ms=self.anchor, archive=base / "boot", version="cfg-1")
        self.cfg = ts.write_config(base / "tier0.json", **kw)
        self.cfg_fast = ts.write_config(base / "tier0-checkpoint50ms.json", **kw)
        doc = json.loads(self.cfg_fast.read_text(encoding="utf-8"))
        doc["checkpoint_interval_s"] = 0.05
        self.cfg_fast.write_text(json.dumps(doc), encoding="utf-8")
        port = ts.free_port()
        self.env = ts.tier0_env(cert=cert, key=key, control_port=port, ec2_url=self.ec2.url)
        self.laptop = ts.Laptop(port, cert)
        self.log_path = base / "tier0.log"
        self.procs: list = []
        self.lock_retries = 0
        self.tmp_caught = 0
        self.restored_events: dict[str, int] = {}  # 한 번이라도 복원된 이벤트 → 처음 복원된 RESTORED 전이의 sequence

    # -- 프로세스

    def start(self, cfg: Path):
        """띄운다. 강제 종료 직후 저널 작성자 잠금(flock)이 늦게 풀리면 바로 죽으므로 잠시 다시 띄운다."""
        from test_tier0_e2e import _start

        deadline = time.monotonic() + 60
        while True:
            mark = self.log_path.stat().st_size if self.log_path.exists() else 0
            proc = _start(cfg, self.env, self.log_path)
            self.procs.append(proc)
            try:
                proc.wait(3)
            except Exception:  # noqa: BLE001 - 3 초 넘게 살아 있으면 기동한 것으로 본다
                return proc
            tail = self.log_path.read_bytes()[mark:].decode("utf-8", "replace")
            if "WriterLockError" in tail and time.monotonic() < deadline:
                self.lock_retries += 1
                log(f"  저널 작성자 잠금이 아직 풀리지 않음 → 다시 띄움 ({self.lock_retries})")
                time.sleep(0.5)
                continue
            raise RuntimeError(f"Tier 0 가 바로 끝남 (코드 {proc.returncode}): {tail[-1500:]}")

    def kill9(self, proc) -> None:
        proc.kill()  # POSIX SIGKILL, Windows TerminateProcess. 정리 코드는 돌지 않는다
        proc.wait(30)

    def wait(self, cond, what: str, timeout: float | None = None) -> None:
        if not self.ts.wait_until(cond, timeout=timeout or self.timeout_s, step=0.01):
            tail = self.log_path.read_text("utf-8", "replace")[-2000:] if self.log_path.exists() else ""
            raise TimeoutError(f"{what} 를 기다리다 시간 초과\n{tail}")

    # -- 메시지

    def transitions(self, conn_from: int = 0) -> list[dict]:
        ec2 = self.ec2
        return [m for i in range(conn_from, len(ec2.connections)) for m in ec2.messages(i)
                if m["msg_type"] == "risk_event_transition"]

    # -- 시나리오

    def run(self) -> None:
        laptop = self.laptop
        proc = self.start(self.cfg)
        laptop.start()

        # 1) 전이 직후
        sc = self._scenario("service_after_transition", "D-G3-11", "R1 local_alert(RAISED) 수신 직후 kill -9")
        self.wait(lambda: any(m["payload"]["rule_id"] == "R1" and m["payload"]["lifecycle"] == "RAISED"
                              for m in laptop.all_messages("local_alert")), "R1 local_alert")
        self.kill9(proc)
        proc = self._restart_and_compare(sc, self.cfg)

        # 2) ACK 직후
        sc = self._scenario("service_after_ack", "D-G3-11", "site_ack → alarm_stop 수신 직후 kill -9")
        conn = len(laptop.connections) - 1

        def unacked() -> list[dict]:
            hs = laptop.messages(conn, "health")
            return [a for a in (hs[-1]["payload"].get("active_site_alerts") or [])
                    if not a["acknowledged"]] if hs else []

        self.wait(lambda: bool(unacked()), "확인 안 된 현장 경보")
        target = unacked()[0]
        ack_id = laptop.send_site_ack(target["event_id"], target["event_sequence"])
        sc["ack"] = {"event_id": target["event_id"], "event_sequence": target["event_sequence"]}
        self.wait(lambda: any(m["payload"]["ack"]["ack_id"] == ack_id for m in laptop.all_messages("alarm_stop")),
                  "alarm_stop")
        self.kill9(proc)
        entries = _journal_entries(self.state)
        sc["ack_in_journal"] = any(e.meta.get("ack_id") == ack_id for e in entries)
        proc = self._restart_and_compare(sc, self.cfg_fast)

        # 3) 체크포인트 기록 중 (50 ms 주기)
        for i in range(self.checkpoint_rounds):
            sc = self._scenario(f"service_during_checkpoint_{i + 1}", "D-G3-11",
                                "checkpoints/*.tmp 가 보이면 kill -9 (50 ms 주기)")
            cps = self.state / "checkpoints"
            seen = False
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if any(cps.glob("*.tmp")):
                    seen = True
                    break
                time.sleep(0.0005)
            self.kill9(proc)
            left = sorted(p.name for p in cps.glob("*.tmp"))
            sc["tmp_seen_before_kill"] = seen
            sc["tmp_left_after_kill"] = left  # 남아 있으면 기록 도중(rename 전)에 죽은 것이 확실하다
            if left:
                self.tmp_caught += 1
            last = i == self.checkpoint_rounds - 1
            proc = self._restart_and_compare(sc, self.cfg if last else self.cfg_fast)

        # 4) 마지막: 사람이 그대로 서 있는 동안 복원 이벤트가 해소되지 않는지 보고 정상 종료
        time.sleep(3)
        from test_tier0_e2e import _graceful

        code = _graceful(proc)
        final = self._scenario("service_final", "D-G3-09", "사람이 그대로 서 있는 동안 복원 이벤트가 해소되지 않았는지, 정상 종료")
        final["graceful_exit"] = code
        from services.tier0.settings import load_settings

        policy = load_settings(self.cfg, self.env).lifecycle
        payloads = [m["payload"] for m in self.transitions()]
        bad, zone_ok = restored_resolution_violations(payloads, self.restored_events, policy.hold_ms)
        for why in bad:
            self.tally.violate("D-G3-09", final, why)
        final["restored_events"] = len(self.restored_events)
        final["restored_zone_events"] = sorted(
            {p["event_id"] for p in payloads if p["event_id"] in self.restored_events and not p["world_ids"]})
        final["restored_zone_resolved_with_evidence"] = zone_ok

    def _scenario(self, name: str, item: str, what: str) -> dict:
        sc = {"name": name, "part": "service", "item": item, "what": what, "started_at": now_ms()}
        self.tally.scenarios.append(sc)
        log(f"▶ {name}: {what}")
        return sc

    def _restart_and_compare(self, sc: dict, cfg: Path):
        """kill 뒤: 정답(저널) → 사본 recover 비교 → EC2 로 나간 것 대조 → 재시작 → RESTORED·스냅샷·health 비교."""
        import site_driver as sd

        from simai_ai.risk import FixedClock

        tally, ec2, laptop = self.tally, self.ec2, self.laptop
        entries = _journal_entries(self.state)
        end = entries[-1].journal_offset if entries else -1
        by_offset = {e.journal_offset: e for e in entries}
        truth = sd.journal_truth(self.state, FixedClock(now_ms()))
        sc |= {"journal_end_offset": end, "truth_active": {k: v["payload"]["event_sequence"] for k, v in truth.items()}}

        # (a) 같은 상태의 사본에서 -51 recover → 저널 정답과 같은가 (원본은 건드리지 않는다)
        copy = self.base / f"verify-{sc['name']}"
        # 잠금 파일은 뺀다: 강제 종료 직후 운영체제가 잠금을 늦게 풀면(Windows) 읽을 수 없고, 사본은 자기 잠금을 새로 만든다
        shutil.copytree(self.state, copy, ignore=shutil.ignore_patterns("*.lock"))
        site = None
        try:
            site = sd.Site(copy, FixedClock(now_ms() + 3_600_000))
            sd.assert_matches_journal(site.result, truth)
            sc["offline_recover"] = {"source": str(site.result.source), "restored": len(site.result.restored)}
        except AssertionError as e:
            tally.violate("D-G3-11", sc, f"사본 recover 가 저널과 다름: {e}")
        finally:
            if site is not None:
                site.close()

        # EC2 로 나간 전이는 모두 저널(fsync)에 있어야 한다
        for m in self.transitions():
            p = m["payload"]
            e = by_offset.get(p["journal_offset"])
            if e is None or e.msg_id != m["msg_id"]:
                tally.violate("D-G3-18", sc, f"EC2 로 나간 전이 offset {p['journal_offset']} 가 저널에 없거나 msg_id 다름")

        # (b)(c) 재시작
        conns, laptop_conns = len(ec2.connections), len(laptop.connections)
        proc = self.start(cfg)
        self.wait(lambda: len(ec2.connections) > conns and any(
            m["msg_type"] == "event_state_snapshot" for m in ec2.messages(conns)), "재시작 뒤 event_state_snapshot")
        self.wait(lambda: len(laptop.connections) > laptop_conns and laptop.messages(laptop_conns, "health"),
                  "재시작 뒤 노트북 health")
        first = laptop.messages(laptop_conns, "health")[0]["payload"]
        sc["first_health"] = first["system_health"]
        if first["system_health"] != "UNSAFE_UNKNOWN":
            tally.violate("D-G3-09", sc, f"재시작 첫 health 가 {first['system_health']} (UNSAFE_UNKNOWN 이어야)")
        listed = {a["event_id"] for a in first.get("active_site_alerts") or []}
        # 현장 경보 대상(LIVE CRITICAL·HIGH, 명세 7.3)은 재시작 첫 health 목록에도 있어야 한다(복원 전 빈 목록 금지, 3.10).
        # R5 CAUTION 같은 대상 밖 이벤트는 목록에 없는 것이 맞다
        site_targets = {k for k, v in truth.items() if v["payload"]["severity"] in ("CRITICAL", "HIGH")}
        if not site_targets <= listed:
            tally.violate("D-G3-09", sc, f"재시작 첫 health 목록에 복원 현장 경보 빠짐: {sorted(site_targets - listed)}")

        new = [m for m in ec2.messages(conns)]
        snap_at = next(i for i, m in enumerate(new) if m["msg_type"] == "event_state_snapshot")
        restored = {m["payload"]["event_id"]: m["payload"]["event_sequence"] for m in new[:snap_at]
                    if m["msg_type"] == "risk_event_transition" and m["payload"]["journal_offset"] > end
                    and "RESTORED" in (m["payload"].get("changes") or [])}
        sc["service_restored"] = restored
        want = {k: v["payload"]["event_sequence"] + 1 for k, v in truth.items()}
        if restored != want:
            tally.violate("D-G3-11", sc, f"재시작 서비스 RESTORED {restored} ≠ 저널 정답 {want}")
        active = {e["event_id"] for e in new[snap_at]["payload"]["active_events"]}
        if not set(truth) <= active:
            tally.violate("D-G3-11", sc, f"재시작 스냅샷에 복원 이벤트 빠짐: {sorted(set(truth) - active)}")
        for eid, seq in restored.items():  # 처음 복원된 sequence 를 둔다(그 뒤의 해소를 모두 본다)
            self.restored_events.setdefault(eid, seq)

        # 저널 기록 직후 죽어 EC2 에 못 간 전이도 재시작 뒤 재전송으로 전달돼야 한다(종료 시점 저널 끝까지)
        sent = {m["payload"]["journal_offset"]: m["msg_id"] for m in self.transitions()}
        first_offset = entries[0].journal_offset if entries else 0  # 정리(compact)된 앞부분은 이미 EC2 가 확인했다
        missing = [o for o in range(first_offset, end + 1) if sent.get(o) != by_offset[o].msg_id]
        if missing:
            tally.violate("D-G3-14", sc, f"저널 offset {missing[:10]} 가 재시작 뒤에도 EC2 에 전달되지 않음")
        sc["finished_at"] = now_ms()
        log(f"  정답 활성 {len(truth)}개, 재시작 복원 {len(restored)}개, 첫 health {first['system_health']}")
        return proc

    def close(self) -> None:
        self.laptop.close()
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait(10)
        self.ec2.stop()


# ---------------------------------------------------------------- 2. 결정적 지점 (-51 시험 재사용)


# tests/recovery/test_crash_recovery.Child.wait_for 가 자식이 지점에 닿지 못했을 때 내는 문장(복구 비교 전 실패)
CHILD_SETUP_FAILURES = ("자식이 먼저 끝났다", "자식이 기대한 줄을 내지 않았다")


def harness_scenarios(seeds: list[int]) -> list[tuple[str, str, str, object]]:
    """(이름, G3 항목, 설명, 호출) — tests/recovery/test_crash_recovery.py 의 시험 함수를 볼륨 경로로 부른다."""
    import site_driver as sd
    import test_crash_recovery as t

    from simai_ai.risk import FixedClock

    def clock():
        return FixedClock(sd.T0 + 3_600_000)

    out = [
        ("harness_after_transition", "D-G3-11", "전이(fsync) 직후 SIGKILL",
         lambda root: t.test_kill_right_after_transition(root, sd, clock())),
        ("harness_during_checkpoint", "D-G3-11", "체크포인트 임시 파일 fsync 뒤·rename 전 SIGKILL",
         lambda root: t.test_kill_during_checkpoint_write(root, sd, clock())),
        ("harness_after_compact", "D-G3-13", "EC2 ACK → 저널 정리 직후 SIGKILL",
         lambda root: t.test_kill_right_after_compaction(root, sd, clock())),
        ("harness_stale_after_checkpoint", "D-G3-11", "해소 → 체크포인트 → 과거 RAISED 재기록 → SIGKILL",
         lambda root: t.test_kill_after_stale_transition_past_checkpoint_does_not_resurrect(root, sd, clock())),
    ]
    for seed in seeds:
        out.append((f"harness_busy_seed{seed}", "D-G3-11", f"판정·확인·해소·10ms 체크포인트 중 임의 시점 SIGKILL (seed {seed})",
                    lambda root, s=seed: t.test_kill_at_random_point_while_busy(root, sd, clock(), s)))
    return out


def run_harness(base: Path, tally: Tally, seeds: list[int]) -> None:
    for name, item, what, call in harness_scenarios(seeds):
        sc = {"name": name, "part": "harness", "item": item, "what": what, "started_at": now_ms()}
        tally.scenarios.append(sc)
        log(f"▶ {name}: {what}")
        root = base / name
        root.mkdir(parents=True)
        try:
            call(root)
            sc["result"] = "ok"
        except AssertionError as e:
            if str(e).startswith(CHILD_SETUP_FAILURES):  # 자식이 지점에 닿기 전에 죽음 = 시나리오가 돌지 못함
                sc["result"] = "error"
                tally.errors.append({"scenario": name, "error": f"자식 프로세스 준비 실패: {str(e)[:500]}"})
                log(f"  오류(판정 불가): {str(e)[:200]}")
            else:
                sc["result"] = "mismatch"
                tally.violate(item, sc, f"{type(e).__name__}: {str(e)[:500]}")
        except Exception as e:  # noqa: BLE001 - 시나리오가 돌지 못함(판정 불가)
            sc["result"] = "error"
            tally.errors.append({"scenario": name, "error": f"{type(e).__name__}: {e}"})
            log(f"  오류: {type(e).__name__}: {e}")
        sc["finished_at"] = now_ms()


# ---------------------------------------------------------------- 실행


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # pytest 캡처 스트림에는 없다
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Network Volume 강제 종료 복구 실측 (S15P31A101-92)")
    ap.add_argument("--volume-dir", type=Path, required=True,
                    help="시험 상태를 둘 디렉터리. Pod 는 Network Volume 아래(예: /workspace/simai/runs/crash92), 노트북은 로컬")
    ap.add_argument("--checkpoint-rounds", type=int, default=3, help="체크포인트 기록 중 kill 반복 수 (기본 3)")
    ap.add_argument("--seeds", type=int, nargs="*", default=[1, 2, 3, 4], help="바쁜 임의 시점 kill seed (기본 1 2 3 4)")
    ap.add_argument("--duration-ms", type=int, default=240_000, help="SIM 장면 길이 (기본 240 s)")
    ap.add_argument("--timeout-s", type=float, default=60.0, help="조건 대기 하나의 최대 시간 (기본 60 s)")
    ap.add_argument("--skip-service", action="store_true", help="서비스 강제 종료를 건너뛴다")
    ap.add_argument("--skip-harness", action="store_true", help="결정적 지점(-51 시험)을 건너뛴다")
    args = ap.parse_args(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = args.volume_dir / stamp
    try:
        base.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        print(f"crash_recovery_g3: {base} 를 만들지 못함: {e}", file=sys.stderr)
        return 2
    log(f"상태 디렉터리 {base}")
    if os.name == "nt" and len(str(base.resolve())) > 110:
        log("경고: Windows 경로 길이 한도(260자)에 걸릴 수 있다(구성 묶음 보관 파일 이름이 길다). 짧은 --volume-dir 를 쓴다")
    tally = Tally()
    started = now_ms()
    secrets = Path(tempfile.mkdtemp(prefix="simai-crash92-tls-"))
    try:
        if not args.skip_service:
            svc = None
            try:
                svc = ServiceRun(base / "svc", secrets, tally, duration_ms=args.duration_ms,
                                 checkpoint_rounds=args.checkpoint_rounds, timeout_s=args.timeout_s)
                svc.run()
            except Exception as e:  # noqa: BLE001 - 서비스 부분이 돌지 못함. 결정적 지점은 이어서 돈다
                tally.errors.append({"scenario": "service", "error": f"{type(e).__name__}: {e}",
                                     "trace": traceback.format_exc()[-3000:]})
                log(f"서비스 시나리오 오류: {type(e).__name__}: {e}")
            finally:
                if svc is not None:
                    svc.close()
                    service_stats = {"lock_retries": svc.lock_retries, "checkpoint_tmp_caught": svc.tmp_caught}
                else:
                    service_stats = {}
        else:
            service_stats = {}
        if not args.skip_harness:
            run_harness(base / "harness", tally, args.seeds)
    finally:
        shutil.rmtree(secrets, ignore_errors=True)

    ran = [s for s in tally.scenarios if s.get("result") != "error"]
    violations = sum(tally.counts.values())
    verdict = "FAIL" if violations else ("ERROR" if tally.errors or not ran else "PASS")
    result = {
        "check": "crash_recovery_g3", "jira": "S15P31A101-92", "volume_dir": str(base),
        "started_at": started, "finished_at": now_ms(), "verdict": verdict,
        "g3": {k: {"text": ITEM_TEXT[k], "count": v, "zero": v == 0} for k, v in tally.counts.items()},
        "service": service_stats, "scenarios": tally.scenarios, "errors": tally.errors,
        "notes": "판정 불가(ERROR)는 위반 0건으로 보지 않는다. 체크포인트 기록 중 종료는 checkpoint_tmp_caught(종료 뒤 *.tmp 가 "
                 "남은 횟수)가 0 이면 서비스 경로에서는 확인하지 못한 것이고 결정적 지점 harness_during_checkpoint 로 본다.",
    }
    text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    (base / "result.json").write_text(text, encoding="utf-8")
    run_dir = os.environ.get("SIMAI_RUN_DIR")
    if run_dir:
        try:
            Path(run_dir, "crash92.json").write_text(text, encoding="utf-8")
            metrics = {"verdict": verdict, "scenarios": len(tally.scenarios), "errors": len(tally.errors),
                       **{k.replace("-", "_").lower(): v for k, v in tally.counts.items()}, **service_stats}
            Path(run_dir, "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"crash_recovery_g3: 결과를 {run_dir} 에 쓰지 못함: {e}", file=sys.stderr)
    log("G3 강제 종료 항목: " + ", ".join(f"{k} {v}건" for k, v in tally.counts.items()))
    log(f"판정 {verdict} (시나리오 {len(tally.scenarios)}개, 오류 {len(tally.errors)}개) → {base / 'result.json'}")
    return {"PASS": 0, "FAIL": 1}.get(verdict, 2)


if __name__ == "__main__":
    sys.exit(main())
