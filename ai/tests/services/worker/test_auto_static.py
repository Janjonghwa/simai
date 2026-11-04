"""사람 지운 배경 묶음 static_reconstruct 자동 제출(S15P31A101-403, services.worker.auto_static).

조건 다섯 가지 각각에서 내지 않음, 모두 만족하면 한 번만 냄, 재시작 뒤 중복 없음, FAILED 묶음 재시도 없음, 업로드 실패 백오프,
업로드 중 LIVE 면 멈춤, 끄기 설정. 가짜 EC2 는 계약(static_reconstruct_submit)대로 같은 bundle_id 에 같은 작업을 돌려준다.
업로드(clean_bg_job, cv2)는 마지막 시험에서만 진짜를 쓴다(extras 없으면 건너뜀).
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from services.worker.auto_static import (
    AutoStaticSettings,
    AutoStaticSubmitter,
    LiveStarted,
    SubmitLedger,
    calibration_document_path,
)
from services.worker.client import Backoff
from services.worker.transport import STATIC_SUBMIT_PATH, Response, TransportError
from simai_ai.gpu.admission import Admission
from simai_ai.providers.contract import schema_errors

T0_MS = 1_762_050_000_000
CAL = "cal-403"


class Clock:
    def __init__(self) -> None:
        self.mono = 1000.0

    def ms(self) -> int:
        return T0_MS + int(self.mono * 1000)

    def advance(self, s: float) -> None:
        self.mono += s


class FakeGate:
    def __init__(self, live: bool = False) -> None:
        self.live = live
        self.fail = False

    def check(self) -> Admission:
        if self.fail:
            raise OSError("표시 파일 확인 실패")
        return Admission(self.live, not self.live, "시험")


class FakeEC2:
    """POST /api/worker/scene-jobs/static-reconstruct. 같은 bundle_id 는 처음 작업과 지금 상태를 돌려준다."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict[str, Any]] = {}
        self.posts: list[dict[str, Any]] = []
        self.reject: int | None = None
        self.down = False

    def post(self, path: str, body: dict[str, Any], timeout_s: float) -> Response:
        assert path == STATIC_SUBMIT_PATH
        assert not schema_errors(body, "static_reconstruct_submit"), schema_errors(body, "static_reconstruct_submit")
        self.posts.append(body)
        if self.down:
            raise TransportError("연결 끊김")
        if self.reject is not None:
            return Response(self.reject, {"code": "SCENE_CALIBRATION_INVALID"})
        bundle = body["payload"]["bundle_id"]
        created = bundle not in self.jobs
        if created:
            self.jobs[bundle] = {"job_id": f"job-{uuid.uuid4()}", "status": "QUEUED", "code": None}
        job = self.jobs[bundle]
        payload = {"bundle_id": bundle, "job_id": job["job_id"], "created": created, "job_status": job["status"]}
        if job["code"]:
            payload["job_error_code"] = job["code"]
        msg = {"schema_version": "1.0.0", "msg_type": "static_reconstruct_submit_result", "msg_id": str(uuid.uuid4()),
               "sent_at": T0_MS, "source_id": "ec2", "payload": payload}
        assert not schema_errors(msg, "static_reconstruct_submit_result")
        return Response(201 if created else 200, msg)

    def finish(self, bundle: str, status: str, code: str | None = None) -> None:
        self.jobs[bundle].update(status=status, code=code)


def new_bundle_id() -> str:
    return f"bundle-{uuid.uuid4().hex}"


def write_bundle(state: Path, bundle_id: str, *, version: str = CAL, complete: bool = True, mark: bool = True) -> Path:
    """-363 Tier 0 출력 모양(status.json 포인터 + <버전>/bundle-<hex>/COMPLETE)."""
    root = state / "clean-background"
    bundle_dir = root / version / bundle_id
    bundle_dir.mkdir(parents=True, exist_ok=True)
    if mark:
        (bundle_dir / "COMPLETE").write_text(json.dumps({"calibration_version": version}), encoding="utf-8")
    (root / "status.json").write_text(json.dumps({
        "calibration_version": version, "complete": complete, "bundle": str(bundle_dir) if complete else None,
    }), encoding="utf-8")
    return bundle_dir


def write_calibration(state: Path, *, active: str = CAL, doc_state: str = "CALIBRATED",
                      transitions: list[dict[str, Any]] | None = None) -> Path:
    """Tier 0 보정 버전 저장소 모양(calibrations/active.json + <sha256(버전)>.json)."""
    cal_dir = state / "calibrations"
    cal_dir.mkdir(parents=True, exist_ok=True)
    path = calibration_document_path(cal_dir, active)
    path.write_text(json.dumps({"calibration_version": active, "state": doc_state}), encoding="utf-8")
    (cal_dir / "active.json").write_text(json.dumps({"active": active, "versions": [active],
                                                      "transitions": transitions or []}), encoding="utf-8")
    return path


def job_input(camera_ids=("cam_A", "cam_B")) -> dict[str, Any]:
    return {
        "calibration_version": CAL,
        "calibration_ref": "s3://simai-site/static-inputs/b/calibration.json",
        "calibration_hash": "sha256:" + "a" * 64,
        "images": [{"camera_id": c, "uri": f"s3://simai-site/static-inputs/b/{c}.jpg", "kind": "CLEAN_BACKGROUND"} for c in camera_ids],
        "input_provenance": {"frames": {"source": "LIVE", "camera_ids": list(camera_ids)},
                             "calibration": {"source": "G1_AUTO", "calibration_version": CAL}, "is_upper_bound": False},
        "options": {"align_views": "off", "plane_cleanup": False},
    }


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        self.clock = Clock()
        self.gate = FakeGate()
        self.ec2 = FakeEC2()
        self.prepared: list[tuple[Path, Path]] = []
        self.prepare_error: Exception | None = None
        self.settings = AutoStaticSettings(state_dir=self.state, quiet_after_live_s=300.0, poll_s=30.0,
                                           backoff=Backoff(initial_s=60.0, factor=2.0, max_s=3600.0))

    def prepare(self, bundle_dir: Path, cal_path: Path, may_continue) -> dict[str, Any]:
        self.prepared.append((bundle_dir, cal_path))
        if self.prepare_error is not None:
            raise self.prepare_error
        assert may_continue()
        return job_input()

    def submitter(self) -> AutoStaticSubmitter:
        return AutoStaticSubmitter(self.settings, self.gate, self.ec2, "worker-01", prepare=self.prepare,
                                   clock_ms=self.clock.ms, monotonic=lambda: self.clock.mono)

    def ready(self) -> AutoStaticSubmitter:
        """조건을 모두 갖춘 상태에서 대기 시간까지 지난 제출기."""
        sub = self.submitter()
        self.clock.advance(self.settings.quiet_after_live_s)
        return sub

    def ledger(self) -> list[dict[str, Any]]:
        return SubmitLedger(self.settings.ledger_path).records()


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_settings_from_env_default_on_and_off(tmp_path: Path) -> None:
    s = AutoStaticSettings.from_env({"SIMAI_ROOT": str(tmp_path)})
    assert s.enabled and s.state_dir == tmp_path / "state" and s.quiet_after_live_s == 300.0
    assert s.clean_background_dir == tmp_path / "state" / "clean-background"
    off = AutoStaticSettings.from_env({"SIMAI_AUTO_STATIC_RECONSTRUCT": "0", "SIMAI_STATE_DIR": str(tmp_path / "x"),
                                       "SIMAI_AUTO_STATIC_QUIET_S": "60"})
    assert not off.enabled and off.state_dir == tmp_path / "x" and off.quiet_after_live_s == 60.0
    assert not s.exit_when_done and AutoStaticSettings.from_env({"SIMAI_AUTO_STATIC_EXIT_WHEN_DONE": "1"}).exit_when_done
    # lobby 처럼 표시 파일만 넘긴 기동: Tier 0 상태 폴더(표시 파일의 폴더)를 본다. SIMAI_STATE_DIR 가 있으면 그것이 먼저(-473)
    marker = tmp_path / "runs" / "lobby-live" / "state" / "tier0.live"
    assert AutoStaticSettings.from_env({"SIMAI_ROOT": str(tmp_path), "SIMAI_TIER0_MARKER": str(marker)}).state_dir == marker.parent
    assert AutoStaticSettings.from_env({"SIMAI_TIER0_MARKER": str(marker), "SIMAI_STATE_DIR": str(tmp_path / "x")}).state_dir == tmp_path / "x"
    for bad in ({"SIMAI_AUTO_STATIC_RECONSTRUCT": "yes"}, {"SIMAI_AUTO_STATIC_QUIET_S": "-1"},
                {"SIMAI_AUTO_STATIC_POLL_S": "abc"}):
        with pytest.raises(ValueError):
            AutoStaticSettings.from_env(bad)


def test_disabled_does_not_start_thread(tmp_path: Path, monkeypatch) -> None:
    import services.worker.__main__ as entry

    started = []
    monkeypatch.setattr(entry.threading.Thread, "start", lambda self: started.append(self.name))
    gpu = SimpleNamespace(marker=tmp_path / "tier0.live", policy=entry.WorkerGpuSettings.from_env({}).policy)
    off = AutoStaticSettings(state_dir=tmp_path, enabled=False)
    assert entry.start_auto_static(off, gpu, FakeEC2(), "worker-01", "https://ec2.test", "t", threading.Event()) is None
    on = AutoStaticSettings(state_dir=tmp_path)
    thread = entry.start_auto_static(on, gpu, FakeEC2(), "worker-01", "https://ec2.test", "t", threading.Event())
    assert thread is not None and started == ["auto-static-reconstruct"]


@pytest.mark.parametrize("case, reason", [
    ("no_status", "NO_BUNDLE"),
    ("incomplete", "INCOMPLETE"),
    ("no_mark", "NO_COMPLETE_MARK"),
    ("other_calibration", "NOT_ACTIVE_CALIBRATION"),
    ("no_active", "NO_ACTIVE_CALIBRATION"),
    ("drifted", "CALIBRATION_STATE"),
])
def test_condition1_bundle_and_active_calibration(env: Env, case: str, reason: str) -> None:
    bundle = new_bundle_id()
    if case != "no_status":
        write_bundle(env.state, bundle, complete=case != "incomplete", mark=case != "no_mark")
    if case == "other_calibration":
        write_calibration(env.state, active="cal-other")
    elif case == "drifted":
        # 문서는 CALIBRATED 지만 Tier 0 전환 이력의 마지막 상태가 DRIFTED
        write_calibration(env.state, transitions=[{"payload": {"calibration_version": CAL, "calibration_state": "DRIFTED"}}])
    elif case != "no_active":
        write_calibration(env.state)
    assert env.ready().tick() == reason
    assert env.ec2.posts == [] and env.prepared == [] and env.ledger() == []


def test_condition2_live_or_unknown_gate_does_nothing(env: Env) -> None:
    write_bundle(env.state, new_bundle_id())
    write_calibration(env.state)
    sub = env.ready()
    env.gate.live = True
    assert sub.tick() == "LIVE"
    env.gate.live, env.gate.fail = False, True  # 문을 못 보면 모름 → LIVE
    assert sub.tick() == "LIVE"
    assert env.ec2.posts == [] and env.prepared == []


def test_condition3_waits_quiet_time_after_live_and_after_start(env: Env) -> None:
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    sub = env.submitter()
    env.clock.advance(299)
    assert sub.tick() == "QUIET"  # 시작 시각을 마지막 LIVE 로 본다
    env.gate.live = True
    env.clock.advance(5)
    assert sub.tick() == "LIVE"
    env.gate.live = False
    env.clock.advance(299)
    assert sub.tick() == "QUIET"
    env.clock.advance(1)
    assert sub.tick() == "SUBMITTED"
    assert len(env.ec2.posts) == 1


def test_submits_once_then_records_finish_and_restart_does_not_resubmit(env: Env) -> None:
    bundle = new_bundle_id()
    bundle_dir = write_bundle(env.state, bundle)
    cal_path = write_calibration(env.state)
    sub = env.ready()
    assert sub.tick() == "SUBMITTED"
    assert env.prepared == [(bundle_dir, cal_path)]
    post = env.ec2.posts[0]
    assert post["source_id"] == "worker-01" and post["payload"]["bundle_id"] == bundle
    assert post["payload"]["input"]["calibration_version"] == CAL

    assert sub.tick() == "PENDING"  # 끝나지 않은 작업: 같은 요청으로 상태만 본다
    assert len(env.ec2.jobs) == 1
    env.ec2.finish(bundle, "SUCCEEDED")
    assert sub.tick() == "FINISHED"
    assert sub.tick() == "ALREADY_SUBMITTED"

    restarted = env.ready()  # 워커 재시작: 기록 파일로 같은 묶음을 다시 내지 않는다
    assert restarted.tick() == "ALREADY_SUBMITTED"
    assert len(env.ec2.jobs) == 1 and len(env.prepared) == 1
    events = [r["event"] for r in env.ledger()]
    assert events == ["SUBMITTED", "FINISHED"]
    finished = env.ledger()[-1]
    assert finished["job_status"] == "SUCCEEDED" and finished["job_id"] == env.ec2.jobs[bundle]["job_id"]


def test_condition5_pending_job_blocks_new_bundle_even_after_restart(env: Env) -> None:
    first = new_bundle_id()
    write_bundle(env.state, first)
    write_calibration(env.state)
    assert env.ready().tick() == "SUBMITTED"

    second = new_bundle_id()  # Tier 0 가 새 묶음을 공개했다
    write_bundle(env.state, second)
    sub = env.ready()
    assert sub.tick() == "PENDING"
    env.ec2.finish(first, "LEASED")
    assert sub.tick() == "PENDING"
    assert set(env.ec2.jobs) == {first}
    env.ec2.finish(first, "SUCCEEDED")
    assert sub.tick() == "FINISHED"
    assert sub.tick() == "SUBMITTED"  # 앞 작업이 끝난 뒤에야 새 묶음
    assert set(env.ec2.jobs) == {first, second}


def test_condition4_failed_job_is_recorded_and_not_retried(env: Env) -> None:
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    sub = env.ready()
    assert sub.tick() == "SUBMITTED"
    env.ec2.finish(bundle, "FAILED", "STATIC_BAKE_EMPTY")
    assert sub.tick() == "FINISHED"
    for _ in range(3):
        env.clock.advance(3600)
        assert sub.tick() == "ALREADY_SUBMITTED"
    assert len(env.prepared) == 1
    last = env.ledger()[-1]
    assert last["event"] == "FINISHED" and last["job_status"] == "FAILED" and last["job_error_code"] == "STATIC_BAKE_EMPTY"


def test_server_returns_existing_finished_job(env: Env) -> None:
    """기록을 쓰기 전에 꺼졌다가 다시 낸 경우: EC2 가 이미 끝난 같은 작업을 돌려주면 SUBMITTED·FINISHED 를 함께 남긴다."""
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    env.ec2.jobs[bundle] = {"job_id": "job-earlier", "status": "TIMED_OUT", "code": "TIMEOUT"}
    sub = env.ready()
    assert sub.tick() == "SUBMITTED"
    assert [r["event"] for r in env.ledger()] == ["SUBMITTED", "FINISHED"]
    assert env.ledger()[0]["job_id"] == "job-earlier" and env.ledger()[0]["created"] is False
    assert sub.tick() == "ALREADY_SUBMITTED"


@pytest.mark.parametrize("failure", ["prepare", "transport", "rejected"])
def test_upload_or_register_failure_backs_off_and_survives_restart(env: Env, failure: str) -> None:
    env.settings = replace(env.settings, quiet_after_live_s=10.0)  # 재시작 뒤 대기를 백오프보다 짧게
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    if failure == "prepare":
        env.prepare_error = RuntimeError("서명 요청 실패: HTTP 503")
    elif failure == "transport":
        env.ec2.down = True
    else:
        env.ec2.reject = 409
    sub = env.ready()
    assert sub.tick() == "SUBMIT_FAILED"
    env.clock.advance(30)
    assert sub.tick() == "BACKOFF"
    env.clock.advance(30)  # 첫 실패 뒤 60초
    assert sub.tick() == "SUBMIT_FAILED"
    restarted = env.ready()
    assert restarted.tick() == "BACKOFF"  # 재시작해도 기록으로 백오프를 잇는다(두 번째 실패 뒤 120초)
    env.clock.advance(109)
    assert restarted.tick() == "BACKOFF"
    env.prepare_error, env.ec2.down, env.ec2.reject = None, False, None
    env.clock.advance(1)
    assert restarted.tick() == "SUBMITTED"
    events = [r["event"] for r in env.ledger()]
    assert events == ["SUBMIT_FAILED", "SUBMIT_FAILED", "SUBMITTED"]
    assert len(env.ec2.jobs) == 1


def test_backoff_has_upper_bound(env: Env) -> None:
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    env.prepare_error = RuntimeError("계속 실패")
    sub = env.ready()
    for _ in range(10):
        assert sub.tick() == "SUBMIT_FAILED"
        env.clock.advance(3600)  # 상한 1시간이면 늘 다시 해 본다
    assert len(env.prepared) == 10


def test_live_during_upload_stops_without_counting_failure(env: Env) -> None:
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)

    def prepare(bundle_dir, cal_path, may_continue):
        env.gate.live = True
        if not may_continue():
            raise LiveStarted
        return job_input()

    env.prepare = prepare  # type: ignore[method-assign]
    sub = env.ready()
    assert sub.tick() == "LIVE"
    assert env.ec2.posts == [] and env.ledger() == []
    env.gate.live = False
    assert sub.tick() == "QUIET"  # LIVE 를 봤으니 다시 기다린다


def test_ledger_skips_broken_tail_line(env: Env) -> None:
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    assert env.ready().tick() == "SUBMITTED"
    with env.settings.ledger_path.open("a", encoding="utf-8") as f:
        f.write('{"event": "SUBMI')  # 쓰다가 꺼진 줄
    sub = env.ready()
    assert sub.tick() == "PENDING"
    assert len(env.ec2.jobs) == 1


def _run_until_stop(env: Env, sub: AutoStaticSubmitter, max_ticks: int = 20) -> tuple[threading.Event, int]:
    """run 루프를 실제 스레드 없이 돌린다: 주기 대기(stop.wait)마다 시계를 poll_s 만큼 민다."""
    stop = threading.Event()
    ticks = 0

    def wait(timeout=None):
        nonlocal ticks
        ticks += 1
        env.clock.advance(env.settings.poll_s)
        if ticks >= max_ticks:
            stop.set()
        return stop.is_set()

    stop.wait = wait  # type: ignore[method-assign]
    sub.run(stop)
    return stop, ticks


def test_exit_when_done_stops_after_own_job_finishes(env: Env) -> None:
    """after-live: 대기 → 제출 → 진행 중 → 끝나면 워커 프로세스를 끝낸다(stop). 실패·백오프·진행 중에는 끝내지 않는다."""
    env.settings = replace(env.settings, exit_when_done=True)
    bundle = new_bundle_id()
    write_bundle(env.state, bundle)
    write_calibration(env.state)
    sub = env.submitter()
    original = sub.tick
    seen: list[str] = []

    def tick() -> str:
        reason = original()
        seen.append(reason)
        if reason == "PENDING" and seen.count("PENDING") == 3:
            env.ec2.finish(bundle, "SUCCEEDED")
        return reason

    sub.tick = tick  # type: ignore[method-assign]
    stop, ticks = _run_until_stop(env, sub)
    assert stop.is_set() and ticks < 20
    assert seen[0] == "QUIET" and "SUBMITTED" in seen and seen[-1] == "FINISHED"
    assert [r["event"] for r in env.ledger()] == ["SUBMITTED", "FINISHED"]


def test_exit_when_done_with_nothing_to_submit_and_not_while_failing(env: Env) -> None:
    env.settings = replace(env.settings, exit_when_done=True)
    sub = env.ready()
    stop, ticks = _run_until_stop(env, sub)  # 낼 묶음이 없다(NO_BUNDLE)
    assert stop.is_set() and ticks == 0
    write_bundle(env.state, new_bundle_id())
    write_calibration(env.state)
    env.prepare_error = RuntimeError("계속 실패")
    stop, ticks = _run_until_stop(env, env.ready(), max_ticks=5)
    assert ticks == 5  # 실패·백오프 중에는 끝내지 않는다(운영자가 로그를 본다)
    env.gate.live = True
    stop, ticks = _run_until_stop(env, env.ready(), max_ticks=3)
    assert ticks == 3  # LIVE 면 끝내지 않는다


def test_calibration_path_matches_tier0_runtime_store(tmp_path: Path) -> None:
    pytest.importorskip("numpy")
    from simai_ai.calibration.runtime import RuntimeCalibrationStore

    fake = SimpleNamespace(root=tmp_path)
    assert calibration_document_path(tmp_path, "cal-x") == RuntimeCalibrationStore.path(fake, "cal-x")
    assert calibration_document_path(tmp_path, "cal-x").name == hashlib.sha256(b"cal-x").hexdigest() + ".json"


def test_clean_bg_uploader_uses_session_tool_with_worker_purpose(tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("cv2")
    from services.worker.auto_static import clean_bg_uploader
    from simai_ai.scene import clean_bg_job as cbj
    from simai_ai.scene.reconstruct import validate_input
    from tests.scene.test_clean_bg_job import (
        _make_calibration_doc,
        _write_clean_bg_bundle,
    )

    cal = _make_calibration_doc(calibration_version=CAL)
    cal_path = tmp_path / "cal.json"
    cal_path.write_text(json.dumps(cal), encoding="utf-8")
    _, bundle_dir, _ = _write_clean_bg_bundle(tmp_path / "clean-background", cal)
    calls: list[dict[str, Any]] = []

    def fake_upload(**kw):
        calls.append(kw)
        ext = ".json" if kw["content_type"] == "application/json" else ".jpg"
        return f"s3://simai-site/static-inputs/{kw['extra']['bundle_id']}/{kw['extra']['file_name']}{ext}"

    monkeypatch.setattr(cbj, "sign_and_upload_object", fake_upload)
    prepare = clean_bg_uploader("https://ec2.test", "worker-token")
    out = prepare(bundle_dir, cal_path, lambda: True)
    validate_input(out)
    assert out["calibration_hash"] == "sha256:" + hashlib.sha256(cal_path.read_bytes()).hexdigest()
    assert {c["purpose"] for c in calls} == {"STATIC_RECONSTRUCT_INPUT"} and len(calls) == 1 + len(cal["cameras"])
    assert calls[0]["content_type"] == "application/json" and calls[0]["token"] == "worker-token"
    # 키는 묶음·이름으로 정해진다(다시 내도 같은 키): 보정 문서는 calibration, 사진은 카메라 ID
    assert [c["extra"] for c in calls] == [{"bundle_id": bundle_dir.name, "file_name": n}
                                           for n in ["calibration", *(c["camera_id"] for c in cal["cameras"])]]
    assert [im["kind"] for im in out["images"]] == ["CLEAN_BACKGROUND"] * len(cal["cameras"])

    calls.clear()
    with pytest.raises(LiveStarted):
        prepare(bundle_dir, cal_path, lambda: False)
    assert calls == []  # LIVE 면 아무것도 올리지 않는다
