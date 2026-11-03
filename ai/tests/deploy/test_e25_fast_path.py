"""e25 판정 도구 (deploy/scripts/e25_fast_path.py, S15P31A101-25): 로그 파싱, 근거 없는 이벤트를 분모에 넣는 p95, 위반 판정."""

import csv
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "deploy" / "scripts"))

import e25_fast_path as e25  # noqa: E402

COLUMNS = ("event_id rule_id severity tier0_severity job_id job_status attempt error_code rule_severity proposed_severity clause_count "
           "event_to_rationale_ms within_deadline latency_ms frames frames_local frames_after_observation clip_keys tracks jobs").split()


def row(i=0, ms="3000", **changes):
    base = dict.fromkeys(COLUMNS, "")
    base |= {"event_id": f"e{i}", "rule_id": "R1", "severity": "CRITICAL", "tier0_severity": "CRITICAL", "job_id": f"j{i}",
             "job_status": "SUCCEEDED", "attempt": "1", "rule_severity": "CRITICAL", "clause_count": "0", "event_to_rationale_ms": ms,
             "within_deadline": "t", "latency_ms": "1500", "frames": "4", "frames_local": "4", "frames_after_observation": "0",
             "clip_keys": "0", "tracks": "1", "jobs": "1"}
    return base | changes


def unanswered(i, status="TIMED_OUT"):
    return row(i, ms="", job_status=status, rule_severity="", latency_ms="", within_deadline="", clause_count="")


def test_pass_needs_enough_samples():
    assert e25.judge([row(i) for i in range(30)], {}, "relay")["verdict"] == "PASS"
    short = e25.judge([row(i) for i in range(29)], {}, "relay")
    assert short["verdict"] == "INSUFFICIENT" and short["p95_all_events_ms"] == 3000


def test_events_without_rationale_count_against_p95():
    """성공 건만 보면 3 s 지만 근거 없는 이벤트가 5% 를 넘으면 FAIL 이다."""
    rows = [row(i) for i in range(27)] + [unanswered(27), unanswered(28), row(29, ms="", job_id="", job_status="", jobs="0", frames="", tracks="")]
    result = e25.judge(rows, {}, "relay")
    assert result["verdict"] == "FAIL" and result["p95_all_events_unbounded"] and result["p95_all_events_ms"] is None
    assert result["p95_succeeded_ms"] == 3000
    assert result["job_status"] == {"SUCCEEDED": 27, "TIMED_OUT": 2, "NO_JOB": 1} and result["events_without_job"] == 1
    # 표본 30 에서 1건(3.3%)은 p95 에 들지 않는다
    assert e25.judge([row(i) for i in range(29)] + [unanswered(29)], {}, "relay")["verdict"] == "PASS"


def test_late_success_fails_p95():
    assert e25.judge([row(i, ms="7100", within_deadline="f") for i in range(30)], {}, "relay")["verdict"] == "FAIL"


@pytest.mark.parametrize(("changes", "name"), [
    ({"severity": "HIGH", "tier0_severity": "HIGH"}, "downgrades"),
    ({"severity": "HIGH"}, "direct_severity_changes"),
    ({"proposed_severity": "CRITICAL"}, "proposed_severity_not_null"),
    ({"clause_count": "1"}, "clause_ids_not_empty"),
    ({"frames_after_observation": "1"}, "frames_after_observation"),
    ({"clip_keys": "1"}, "clip_input_keys"),
    ({"jobs": "2"}, "events_with_multiple_jobs"),
])
def test_any_violation_fails_even_with_few_samples(changes, name):
    result = e25.judge([row(0, **changes)], {}, "relay")
    assert result["verdict"] == "FAIL" and result["violations"][name] == 1


def test_clock_suspect_and_empty_tracks_are_reported_not_judged():
    result = e25.judge([row(i, ms="1000", tracks="0") for i in range(30)], {}, "live")
    assert result["verdict"] == "PASS" and result["clock_suspect"] == 30 and result["jobs_with_empty_tracks"] == 30


def test_parse_logs_matches_product_log_lines():
    tier0 = ("INFO event_frames_ready event_id=a frames=4 local=4 event_to_ready_ms=312\n"
             "WARNING event_frames_failed event_id=b reason=no_frames\n"
             "WARNING event_frames_upload_failed event_id=a camera_id=cam_A reason=OSError\n")
    worker = ("INFO 이벤트 a: 근거 완료 frames=4 gate_ms=210 vlm_ms=1900 total_ms=2110\n"
              "WARNING 이벤트 사진 로컬 사본을 쓰지 않는다(reason=sha256), 객체 저장소에서 받는다\n"
              "WARNING Tier 0 우선 접수 문: 받지 않음 (지연)\n")
    parsed = e25.parse_logs(tier0, worker)
    assert parsed["ready"]["a"]["event_to_ready_ms"] == 312 and parsed["done"]["a"]["total_ms"] == 2110
    assert parsed["frame_failed"] == [{"event_id": "b", "reason": "no_frames"}]
    assert parsed["counts"] == {"local_rejected": 1, "late_frames": 0, "face_dropped": 0, "gate_closed": 1, "upload_failed": 1}


def test_watch_refuses_without_live_and_reads_only_new_log(tmp_path, monkeypatch):
    logs, marker, out = tmp_path / "logs", tmp_path / "tier0.live", tmp_path / "out"
    logs.mkdir()
    (logs / "tier0.log").write_text("event_frames_ready event_id=old frames=4 local=4 event_to_ready_ms=1\n", encoding="utf-8")
    (logs / "worker.log").write_text("", encoding="utf-8")
    argv = ["watch", "--out", str(out), "--logs", str(logs), "--marker", str(marker), "--min-events", "1", "--max-s", "5",
            "--poll-s", "0", "--settle-s", "0"]
    assert e25.main(argv) == e25.EXIT_USAGE and not out.exists()  # 표시 파일 없음
    marker.write_text("{}", encoding="utf-8")
    os.utime(marker, (time.time() - 120, time.time() - 120))
    assert e25.main(argv) == e25.EXIT_USAGE  # 노후

    marker.touch()
    real_sleep = time.sleep

    def append_then_sleep(seconds):
        with open(logs / "tier0.log", "a", encoding="utf-8") as f:
            f.write("event_frames_ready event_id=new frames=4 local=4 event_to_ready_ms=2\n")
        real_sleep(0)

    monkeypatch.setattr(e25.time, "sleep", append_then_sleep)
    assert e25.main(argv) == e25.EXIT_PASS
    summary = json.loads((out / "pod_summary.json").read_text(encoding="utf-8"))
    assert list(summary["ready"]) == ["new"] and summary["since_ms"] <= summary["until_ms"]


def test_report_reads_csv_and_returns_verdict_code(tmp_path, capsys):
    summary, db, out = tmp_path / "s.json", tmp_path / "d.csv", tmp_path / "r.json"
    summary.write_text(json.dumps({"since_ms": 1, "until_ms": 2, "ready": {"a": {"event_to_ready_ms": 300}}, "done": {}, "counts": {}}), encoding="utf-8")
    with open(db, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, COLUMNS)
        writer.writeheader()
        writer.writerows(row(i) for i in range(30))
    assert e25.main(["report", "--summary", str(summary), "--db-csv", str(db), "--input", "relay", "--out", str(out)]) == e25.EXIT_PASS
    assert json.loads(out.read_text(encoding="utf-8"))["segments_p95_ms"]["event_to_ready_ms"] == 300
    assert e25.main(["sql", "--summary", str(summary)]) == 0
    assert "to_timestamp(1 / 1000.0)" in capsys.readouterr().out
