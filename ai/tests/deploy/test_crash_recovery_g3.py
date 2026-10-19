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
    assert len(res["errors"]) == 4


def test_nothing_ran_is_error_not_pass(g3, tmp_path):
    code = g3.main(["--volume-dir", str(tmp_path), "--skip-service", "--skip-harness"])
    assert code == 2 and result(tmp_path)["verdict"] == "ERROR"
