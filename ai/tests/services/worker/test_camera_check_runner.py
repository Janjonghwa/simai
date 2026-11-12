"""세팅 「카메라 확인」 워커 실행기와 Tier 0 입력 내보내기의 파일 주고받기 시험(S15P31A101-646)."""

from __future__ import annotations

import itertools
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "calibration"))

import test_camera_check as cc

from services.tier0.camera_check_tap import (
    REQUEST_FILE,
    CameraCheckTap,
    session_views_path,
)
from services.worker.camera_check import CameraCheckRunner
from simai_ai.calibration.camera_check import load_baseline, views_to_json

SESSION = "0192a6f0-7c1e-7b3a-9f2d-000000000646"


def _runner(tmp_path: Path) -> CameraCheckRunner:
    return CameraCheckRunner(tmp_path, clock_ms=lambda: cc.T0, config=cc.CFG)


def test_runner_reads_tier0_views_passes_and_writes_baseline(tmp_path):
    truth = cc._doc()
    runner = _runner(tmp_path)
    first = runner.start(SESSION, truth, expires_at_ms=cc.T0 + 600_000)
    assert first["status"] == "WAITING"
    request = json.loads((tmp_path / REQUEST_FILE).read_text(encoding="utf-8"))
    assert request["setup_session_id"] == SESSION and request["calibration_version"] == "cal-check-1"

    path = session_views_path(tmp_path, SESSION)
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = cc._walk(truth)
    half = len(frames) // 4  # 6초: 아직 판정 전
    with path.open("w", encoding="utf-8") as fh:
        for t, views in frames[:half]:
            fh.write(json.dumps(views_to_json(t, views)) + "\n")
        fh.write('{"t": 1, "views": [')  # 아직 쓰는 중인 마지막 줄은 다음 poll 에 읽는다
    assert runner.poll()["status"] == "MEASURING"
    with path.open("a", encoding="utf-8") as fh:
        fh.write('x]}\n')  # 위 줄을 마저 쓴다(깨진 줄이라 형식 오류 하나로 센다)
        for t, views in frames[half:]:
            fh.write(json.dumps(views_to_json(t, views)) + "\n")
    summary = runner.poll()
    assert summary["status"] == "PASSED"
    assert runner.bad_lines == 1
    assert not (tmp_path / REQUEST_FILE).exists()  # 끝나면 Tier 0 가 쓰기를 멈추게 요청을 지운다
    record = load_baseline(tmp_path, "cal-check-1")
    assert record is not None and set(record.verified_camera_ids) == set(cc.CORNERS)
    assert runner.poll() == summary  # 끝난 뒤에는 같은 판정


def test_runner_incomplete_when_no_views(tmp_path):
    runner = _runner(tmp_path)
    runner.start(SESSION, cc._doc(), expires_at_ms=cc.T0 + 1)
    summary = runner.incomplete()
    assert summary["status"] == "INCOMPLETE"
    assert not (tmp_path / REQUEST_FILE).exists()
    assert load_baseline(tmp_path, "cal-check-1") is None


def test_runner_start_clears_old_views_of_same_session(tmp_path):
    path = session_views_path(tmp_path, SESSION)
    path.parent.mkdir(parents=True)
    path.write_text("old\n", encoding="utf-8")
    _runner(tmp_path).start(SESSION, cc._doc(), expires_at_ms=cc.T0 + 1)
    assert not path.exists()


def test_tap_writes_only_while_request_exists_and_throttles(tmp_path):
    now = [cc.T0]
    tap = CameraCheckTap(tmp_path, lambda: now[0], max_rate_hz=5.0, check_interval_ms=1)
    stop = threading.Event()
    thread = threading.Thread(target=tap.run, args=(stop,), daemon=True)
    thread.start()
    truth = cc._doc()
    frames = cc._walk(truth, seconds=2, hz=10)
    tap.offer(*frames[0])  # 요청 없음: 쓰지 않는다
    (tmp_path / REQUEST_FILE).write_text(json.dumps({"setup_session_id": SESSION, "expires_at_ms": cc.T0 + 10_000}),
                                         encoding="utf-8")
    for t, views in frames[1:]:
        now[0] += 2
        tap.offer(t, views)
    now[0] = cc.T0 + 20_000  # 만료 뒤에는 쓰지 않는다
    tap.offer(cc.T0 + 30_000, frames[-1][1])
    deadline = threading.Event()
    for _ in range(50):
        if tap.written >= 9:
            break
        deadline.wait(0.05)
    stop.set()
    thread.join(timeout=2)
    lines = session_views_path(tmp_path, SESSION).read_text(encoding="utf-8").splitlines()
    ts = [json.loads(line)["t"] for line in lines]
    assert len(ts) == tap.written and 9 <= len(ts) <= 10  # 10 Hz 입력을 5 Hz 로 줄인다
    assert all(b - a >= 200 for a, b in itertools.pairwise(ts))
    assert max(ts) < cc.T0 + 30_000


def test_tap_ignores_bad_request(tmp_path):
    tap = CameraCheckTap(tmp_path, lambda: cc.T0, check_interval_ms=1)
    (tmp_path / REQUEST_FILE).write_text(json.dumps({"setup_session_id": "../../etc"}), encoding="utf-8")
    t, views = cc._walk(cc._doc(), seconds=1)[0]
    tap.offer(t, views)
    assert tap.status()["session"] is None


def test_offline_evaluation_of_recorded_views(tmp_path):
    from services.worker.camera_check import main

    truth = cc._doc()
    views = tmp_path / "views.jsonl"
    views.write_text("".join(json.dumps(views_to_json(t, v)) + "\n" for t, v in cc._walk(truth, seconds=40)),
                     encoding="utf-8")
    calibration = tmp_path / "cal.json"
    calibration.write_text(json.dumps(truth), encoding="utf-8")
    # 운영 기준(30초 이상, 쌍 근거 30)으로 40초 걷기는 통과한다
    assert main(["--views", str(views), "--calibration", str(calibration)]) == 0
    rotated = tmp_path / "rot.json"
    rotated.write_text(json.dumps(cc._doc(rotate={"cam2": 2.0})), encoding="utf-8")
    assert main(["--views", str(views), "--calibration", str(rotated)]) == 1
