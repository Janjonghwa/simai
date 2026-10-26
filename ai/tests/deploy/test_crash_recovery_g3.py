"""-92 강제 종료 복구 실측 스크립트(deploy/scripts/crash_recovery_g3.py) 시험 (S15P31A101-183). 로컬 디렉터리 드라이런.

- 줄인 실행(체크포인트 기록 중 1회, seed 1): 실제 Tier 0 프로세스 kill -9 ·재시작과 -51 결정적 지점이 모두 돌고 G3 0건 PASS.
- 음성 대조: 저널 정답에 없는 이벤트를 끼워 넣으면(복구가 저널과 어긋난 것과 같다) D-G3-11 위반으로 FAIL.
- 모르는 상태를 통과로 보지 않음: 자식 프로세스가 지점에 닿기 전에 죽으면 위반이 아니라 판정 불가(ERROR),
  아무 시나리오도 돌지 않으면 ERROR.
"""

from __future__ import annotations

import json
from pathlib import Path


def result(volume: Path) -> dict:
    [path] = list(volume.glob("*/result.json"))
    return json.loads(path.read_text(encoding="utf-8"))


def test_reduced_run_passes_with_zero_g3_violations(g3, tmp_path):
    code = g3.main(["--volume-dir", str(tmp_path), "--checkpoint-rounds", "1", "--seeds", "1", "--duration-ms", "120000"])
    res = result(tmp_path)
    assert code == 0, (res["errors"], [s.get("violations") for s in res["scenarios"]])
    assert res["verdict"] == "PASS" and all(v["count"] == 0 for v in res["g3"].values())
    names = [s["name"] for s in res["scenarios"]]
    assert names[:4] == ["service_after_transition", "service_after_ack", "service_during_checkpoint_1", "service_final"]
    assert "harness_during_checkpoint" in names and "harness_after_compact" in names
    ack = next(s for s in res["scenarios"] if s["name"] == "service_after_ack")
    assert ack["first_health"] == "UNSAFE_UNKNOWN" and ack["truth_active"]
    # 시험용 TLS 개인 키는 볼륨 경로에 남지 않는다
    assert not [p for p in tmp_path.rglob("*") if p.suffix in (".key", ".pem")]


def test_negative_control_phantom_event_in_truth_is_reported_as_d_g3_11(g3, tmp_path, monkeypatch):
    import site_driver  # crash_recovery_g3 가 sys.path 에 tests/recovery 를 넣었다

    real = site_driver.journal_truth

    def phantom(root, clock):
        truth = real(root, clock)
        if truth:
            some = next(iter(truth.values()))
            truth["00000000-0000-7000-8000-000000000000"] = some  # 저널에 없는 활성 이벤트
        return truth

    monkeypatch.setattr(site_driver, "journal_truth", phantom)
    code = g3.main(["--volume-dir", str(tmp_path), "--checkpoint-rounds", "1", "--seeds", "1", "--duration-ms", "120000"])
    res = result(tmp_path)
    assert code == 1 and res["verdict"] == "FAIL"
    assert res["g3"]["D-G3-11"]["count"] > 0
    bad = {s["name"] for s in res["scenarios"] if s.get("violations")}
    assert "service_after_transition" in bad and "harness_after_transition" in bad


def test_child_that_dies_before_the_kill_point_is_error_not_violation(g3, tmp_path, monkeypatch):
    g3.harness_scenarios([])  # tests/recovery 모듈을 불러 둔다
    import test_crash_recovery

    broken = tmp_path / "broken_child.py"
    broken.write_text("raise SystemExit('준비 실패 흉내')\n", encoding="utf-8")
    monkeypatch.setattr(test_crash_recovery, "CHILD", broken)
    code = g3.main(["--volume-dir", str(tmp_path / "vol"), "--skip-service", "--seeds"])
    res = result(tmp_path / "vol")
    assert code == 2 and res["verdict"] == "ERROR"
    assert all(v["count"] == 0 for v in res["g3"].values())  # 위반으로 세지 않는다
    assert len(res["errors"]) == 5  # 결정적 지점 5개(-113 운영자 상향 직후 포함), seed 없음


def test_nothing_ran_is_error_not_pass(g3, tmp_path):
    code = g3.main(["--volume-dir", str(tmp_path), "--skip-service", "--skip-harness"])
    assert code == 2 and result(tmp_path)["verdict"] == "ERROR"


# ---------------------------------------------------------------- D-G3-09 판정 (S15P31A101-198)
# -192 리뷰 중 부하에서 난 D-G3-09 1건의 원인: 시동 첫 판정에 프레임이 아직 없는 카메라는 정상으로 세지 않아(모르면 저하) R7 이
# 잠깐 나고, 그 R7 이 해소 유지 시간을 채우기 전에 첫 kill 이 와 복원됐다가 다음 프로세스에서 감시 확보 근거로 해소됐다(정상 동작,
# S15P31A101-174). 하네스가 모든 복원 이벤트의 해소를 위반으로 셌다. 아래는 부하로 늦어진 종료 시점을 전이 열로 흉내 낸다.

HOLD = 1500


def tr(seq, transition, verification, obs, *, eid="e1", rule="R7", world_ids=(), changes=()):
    return {"event_id": eid, "event_sequence": seq, "transition": transition, "verification": verification,
            "observation_time": obs, "rule_id": rule, "world_ids": list(world_ids), "changes": list(changes)}


def r7_restored_then(*after):
    """R7 이 시동 때 나고(1) 해소 전에 kill → 복원(2, UNVERIFIABLE) 된 뒤 after 가 이어진다."""
    return [tr(1, "RAISED", "VERIFIED", 1_000), tr(2, "UPDATED", "UNVERIFIABLE", 1_000, changes=["RESTORED"]), *after]


def judge(g3, transitions, restored):
    return g3.restored_resolution_violations(transitions, restored, lambda rule: HOLD)


def test_restored_zone_event_resolved_after_evidence_held_is_not_a_violation(g3):
    seq = r7_restored_then(tr(3, "UPDATED", "VERIFIED", 5_000), tr(4, "RESOLVED", "VERIFIED", 5_000 + HOLD))
    bad, ok = judge(g3, seq + seq, {"e1": 2})  # 재전송 중복은 한 번으로 센다
    assert bad == [] and ok == ["e1"]


def test_restored_zone_event_resolved_before_hold_or_without_evidence_is_a_violation(g3):
    """반례: 유지 시간 1 ms 모자람 / 복원 뒤 VERIFIED 없이 해소(복원 전 VERIFIED 로 해소) / 중간 UNVERIFIABLE 로 끊긴 근거."""
    short = r7_restored_then(tr(3, "UPDATED", "VERIFIED", 5_000), tr(4, "RESOLVED", "VERIFIED", 5_000 + HOLD - 1))
    none = r7_restored_then(tr(3, "RESOLVED", "VERIFIED", 9_000))
    broken = r7_restored_then(tr(3, "UPDATED", "VERIFIED", 5_000), tr(4, "UPDATED", "UNVERIFIABLE", 5_800),
                              tr(5, "UPDATED", "VERIFIED", 5_900), tr(6, "RESOLVED", "VERIFIED", 5_000 + HOLD + 100))
    for seq in (short, none, broken):
        bad, ok = judge(g3, seq, {"e1": 2})
        assert len(bad) == 1 and "근거 유지 없이" in bad[0] and ok == [], seq


def test_missing_transition_before_zone_resolution_is_a_violation_not_a_pass(g3):
    """반례(모르면 통과 아님): 해소 전 전이 하나(재시작 복원 UNVERIFIABLE)가 EC2 에 없으면 근거 구간을 확인할 수 없다."""
    seq = [t for t in r7_restored_then(tr(3, "UPDATED", "UNVERIFIABLE", 2_000, changes=["RESTORED"]),
                                       tr(4, "UPDATED", "VERIFIED", 5_000), tr(5, "RESOLVED", "VERIFIED", 9_000))
           if t["event_sequence"] != 3]
    bad, _ = judge(g3, seq, {"e1": 2})
    assert len(bad) == 1 and "빠져" in bad[0]


def test_restored_person_event_resolution_is_still_a_violation(g3):
    """음성 대조(기존 판정 유지): 사람 대상 복원 이벤트는 근거·유지 시간과 상관없이 복원 뒤 해소되면 위반이다."""
    seq = [tr(1, "RAISED", "VERIFIED", 1_000, rule="R1", world_ids=["w-a-0001"]),
           tr(2, "UPDATED", "UNVERIFIABLE", 1_000, rule="R1", world_ids=["w-a-0001"], changes=["RESTORED"]),
           tr(3, "UPDATED", "VERIFIED", 5_000, rule="R1", world_ids=["w-a-0001"]),
           tr(4, "RESOLVED", "VERIFIED", 9_000, rule="R1", world_ids=["w-a-0001"])]
    bad, ok = judge(g3, seq, {"e1": 2})
    assert len(bad) == 1 and "사람이 그대로 있는데" in bad[0] and ok == []


def test_resolution_before_resume_and_restore_is_not_counted(g3):
    """반례(순서): 해소(3) → 재개(4) → 복원(5) 된 사람 대상 이벤트의 예전 해소는 복원 이벤트의 해소가 아니다. 복원 뒤 다시
    해소되면 위반이다."""
    w = {"rule": "R1", "world_ids": ["w-a-0001"]}
    seq = [tr(1, "RAISED", "VERIFIED", 1_000, **w), tr(2, "UPDATED", "VERIFIED", 1_100, **w),
           tr(3, "RESOLVED", "VERIFIED", 2_600, **w), tr(4, "UPDATED", "VERIFIED", 3_000, changes=["RESUMED"], **w),
           tr(5, "UPDATED", "UNVERIFIABLE", 3_000, changes=["RESTORED"], **w)]
    assert judge(g3, seq, {"e1": 5}) == ([], [])
    bad, _ = judge(g3, seq + [tr(6, "RESOLVED", "VERIFIED", 9_000, **w)], {"e1": 5})
    assert len(bad) == 1


def test_late_cameras_at_start_restore_r7_and_the_run_still_passes(g3, tmp_path, monkeypatch):
    """지연 주입(실제 Tier 0 프로세스): cam2~4 의 첫 1.2 s 프레임을 지워 시동 때 R7(정상 카메라 1대)이 반드시 나고 해소 전에 첫
    kill 로 복원되게 한다(-192 리뷰에서 부하로 우연히 난 순서). 복원 R7 이 근거로 해소돼도 D-G3-09 위반이 아니고 PASS 다."""
    import tier0_site

    real = tier0_site.run_sim

    def late_cameras(workdir, doc=None, seed=1):
        out = real(workdir, doc, seed)
        for cam in ("cam2", "cam3", "cam4"):
            path = out / "observations" / f"{cam}.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            start = json.loads(lines[0])["estimated_capture_time_ms"]
            keep = [line for line in lines if json.loads(line)["estimated_capture_time_ms"] >= start + 1200]
            path.write_text("\n".join(keep) + "\n", encoding="utf-8")
        return out

    monkeypatch.setattr(tier0_site, "run_sim", late_cameras)
    code = g3.main(["--volume-dir", str(tmp_path), "--checkpoint-rounds", "1", "--duration-ms", "120000", "--skip-harness"])
    res = result(tmp_path)
    assert code == 0, (res["errors"], [s.get("violations") for s in res["scenarios"]])
    final = next(s for s in res["scenarios"] if s["name"] == "service_final")
    assert final["restored_zone_events"], final  # 지연 주입이 실제로 복원 R7 을 만들었다
    resolved = final["restored_zone_resolved_with_evidence"]
    assert resolved and set(resolved) <= set(final["restored_zone_events"]), final  # 복원 R7 이 실제로 근거로 해소됐다(권장 2)


def r7_resolved_resumed_restored_again(*after):
    """복원(2) → 근거 유지 뒤 정상 해소(4) → 재개(5) → 다시 kill·복원(6, 새 복원 구간) 뒤 after."""
    return [*r7_restored_then(tr(3, "UPDATED", "VERIFIED", 5_000), tr(4, "RESOLVED", "VERIFIED", 5_000 + HOLD)),
            tr(5, "UPDATED", "VERIFIED", 6_600, changes=["RESUMED"]),
            tr(6, "UPDATED", "UNVERIFIABLE", 6_600, changes=["RESTORED"]), *after]


def test_every_restored_segment_is_judged_not_only_the_first_resolution(g3):
    """반례(리뷰 필수 1): 첫 복원 구간의 정상 해소가 재개 → 재복원 뒤 구간의 근거 없는 해소·유지 부족·중간 UNVERIFIABLE·전이 누락을
    가리지 않는다. restored 는 처음 복원 sequence(2)만 안다."""
    no_evidence = r7_resolved_resumed_restored_again(tr(7, "RESOLVED", "VERIFIED", 9_000))
    short = r7_resolved_resumed_restored_again(tr(7, "UPDATED", "VERIFIED", 8_000),
                                               tr(8, "RESOLVED", "VERIFIED", 8_000 + HOLD - 1))
    broken = r7_resolved_resumed_restored_again(tr(7, "UPDATED", "VERIFIED", 8_000), tr(8, "UPDATED", "UNVERIFIABLE", 8_500),
                                                tr(9, "UPDATED", "VERIFIED", 8_600), tr(10, "RESOLVED", "VERIFIED", 8_000 + HOLD))
    for seq in (no_evidence, short, broken):
        bad, ok = judge(g3, seq, {"e1": 2})
        assert len(bad) == 1 and "근거 유지 없이" in bad[0], seq
        assert ok == ["e1"]  # 첫 구간의 정상 해소는 그대로 정상
    gap = [t for t in r7_resolved_resumed_restored_again(tr(7, "UPDATED", "VERIFIED", 8_000),
                                                         tr(8, "RESOLVED", "VERIFIED", 9_900))
           if t["event_sequence"] != 6]  # 재복원 전이가 EC2 에 없다: 구간을 알 수 없다
    bad, _ = judge(g3, gap, {"e1": 2})
    assert len(bad) == 1 and "빠져" in bad[0]


def test_resolution_after_resume_without_a_new_restore_is_not_a_restored_resolution(g3):
    """음성 대조: 정상 해소 → 재개 → (재시작 없이) 해소는 복원 이벤트의 해소가 아니다. 재복원 뒤 근거를 갖춘 해소는 정상이다."""
    resumed_only = [*r7_restored_then(tr(3, "UPDATED", "VERIFIED", 5_000), tr(4, "RESOLVED", "VERIFIED", 5_000 + HOLD)),
                    tr(5, "UPDATED", "VERIFIED", 6_600, changes=["RESUMED"]), tr(6, "RESOLVED", "VERIFIED", 6_700)]
    assert judge(g3, resumed_only, {"e1": 2}) == ([], ["e1"])
    good = r7_resolved_resumed_restored_again(tr(7, "UPDATED", "VERIFIED", 8_000), tr(8, "RESOLVED", "VERIFIED", 8_000 + HOLD))
    assert judge(g3, good, {"e1": 2}) == ([], ["e1"])
    person = [{**t, "rule_id": "R1", "world_ids": ["w-a-0001"]} for t in r7_resolved_resumed_restored_again(
        tr(7, "UPDATED", "VERIFIED", 8_000), tr(8, "RESOLVED", "VERIFIED", 9_900))]
    bad, _ = judge(g3, person, {"e1": 2})
    assert len(bad) == 2 and all("사람이 그대로 있는데" in b for b in bad)  # 두 복원 구간의 해소가 모두 위반


# ---------------------------------------------------------------- 운영자 종료 (S15P31A101-200)
CLOSE = {"kind": "OPERATOR_CLOSED", "close_id": "7a2b3c4d-5e6f-4a1b-9c2d-3e4f5a6b7c8d", "channel": "CONTROL_ROOM",
         "actor_role": "ADMIN", "actor_id": "user-0003", "requested_at": 9_000, "reason": "카메라 철거"}


def closed(seq, obs, *, close=CLOSE, **kw):
    """EC2 로 나간 운영자 종료 전이(I1 risk_event_transition 2.0.0 resolution)."""
    return tr(seq, "RESOLVED", "UNVERIFIABLE", obs, changes=["OPERATOR_CLOSED"], **kw) | {"resolution": close}


def test_operator_close_of_restored_zone_event_is_an_evidenced_resolution(g3):
    """관리자가 누가·왜를 남기고 닫은 복원 R7 은 관측 VERIFIED 유지가 없어도 근거 있는 해소다."""
    bad, ok = judge(g3, r7_restored_then(tr(3, "UPDATED", "UNVERIFIABLE", 5_000), closed(4, 9_000)), {"e1": 2})
    assert bad == [] and ok == ["e1"]


def test_operator_close_without_who_or_why_or_on_a_person_is_a_violation(g3):
    """반례: 종료 기록의 사유·계정·close_id 가 비었거나 없으면, 또는 사람 대상이면 위반이다."""
    for close in ({**CLOSE, "reason": ""}, {k: v for k, v in CLOSE.items() if k != "actor_id"}, {**CLOSE, "close_id": None},
                  {**CLOSE, "kind": "OBSERVED"}, "closed"):
        bad, ok = judge(g3, r7_restored_then(closed(3, 9_000, close=close)), {"e1": 2})
        assert len(bad) == 1 and "누가·왜" in bad[0] and ok == [], close
    w = {"rule": "R1", "world_ids": ["w-a-0001"]}
    person = [tr(1, "RAISED", "VERIFIED", 1_000, **w), tr(2, "UPDATED", "UNVERIFIABLE", 1_000, changes=["RESTORED"], **w),
              closed(3, 9_000, **w)]
    bad, _ = judge(g3, person, {"e1": 2})
    assert len(bad) == 1 and "사람 대상인데 운영자 종료" in bad[0]


def test_negative_control_unmarked_resolution_without_evidence_is_still_a_violation(g3):
    """음성 대조: lifecycle 변경 사유(changes)에 OPERATOR_CLOSED 가 있어도 계약 resolution 이 없는 해소는 관측 해소라 근거 유지
    없이는 위반이다(저널 내부 close 만 실은 것도 같다)."""
    seq = r7_restored_then(tr(3, "RESOLVED", "VERIFIED", 9_000, changes=["OPERATOR_CLOSED"]) | {"close": CLOSE})
    bad, ok = judge(g3, seq, {"e1": 2})
    assert len(bad) == 1 and "근거 유지 없이" in bad[0] and ok == []
