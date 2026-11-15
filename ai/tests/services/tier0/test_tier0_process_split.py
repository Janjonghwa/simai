"""검출·판정 프로세스 분리(S15P31A101-728, 설정 process_split 기본 끔).

(a) 같은 결과: 같은 녹화를 최대 속도로 단일 경로와 분리 경로에 넣으면 world_state 봉투(클러스터·관절·World ID·msg_id)와 risk_event
    전이가 한 줄도 다르지 않다. 아바타 입력 공유도 분리 경로에서 검출 프로세스가 같은 형식으로 쓴다.
(b) 장애: file 재생 중 검출 프로세스가 죽으면 재생 실패(종료 코드 4)로 끝난다. LIVE 는 죽으면 바로 카메라를 연결 아님으로 보고
    (관측을 지어내지 않음) 새 세대를 띄우며 새 세대의 로컬 ID 는 옛 ID 와 겹치지 않는다. 너무 자주 죽으면 fatal → 종료 코드 4.
    판정 프로세스가 죽으면 검출 프로세스도 끝난다.
(c) 경계 큐: 카메라별 깊이만큼 앞뒤 관측을 남기고(최신 1건만 남기지 않음) 넘치면 가장 오래된 것을 버리며, 중복·역순은 버린다.
    하트비트가 끊기면 검출 처리 시간 창에 모름을 넣는다(아바타 관문이 멈춘다).
"""

from __future__ import annotations

import functools
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE, AI_DIR / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from split_fakes import OneTrackBackend as ScriptedTrackBackend, scripted_spec  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_file_replay import (  # noqa: E402,F401 - scene 은 fixture
    CHANNEL_ENV,
    _config,
    _lines,
    scene,
)
from test_tier0_live_e2e import SimTruthDetector  # noqa: E402

from services.tier0.detect_process import (  # noqa: E402
    DetectorProcess,
    DetectorProcessSpec,
    RemoteLiveInput,
    RemoteProcessingWindow,
    SplitDetectorFeed,
    detect_cpu_set,
)
from services.tier0.file_input import RISK_EVENT_FILE, WORLD_STATE_FILE  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.io.avatar_channel import LatestChannelReader  # noqa: E402
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection  # noqa: E402


def _wait(cond, timeout: float = 30.0, every: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(every)
    return bool(cond())


# --------------------------------------------------------------------------- 설정


def test_switch_defaults_off_and_env_overrides(tmp_path, scene) -> None:  # noqa: F811
    run, _, session = scene
    cfg = _config(tmp_path, run, session)
    s = load_settings(cfg, env={})
    assert s.process_split is False and s.detect_cpus == "none"
    assert load_settings(cfg, env={"SIMAI_TIER0_PROCESS_SPLIT": "1"}).process_split is True
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc.update(process_split=True, detect_cpus="auto")
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    s = load_settings(cfg, env={})
    assert s.process_split is True and s.detect_cpus == "auto"
    assert load_settings(cfg, env={"SIMAI_TIER0_PROCESS_SPLIT": "0"}).process_split is False
    assert load_settings(cfg, env={"SIMAI_TIER0_DETECT_CPUS": "0-3,6"}).detect_cpus == "0-3,6"
    with pytest.raises(Tier0ConfigError):
        load_settings(cfg, env={"SIMAI_TIER0_PROCESS_SPLIT": "yes"})
    with pytest.raises(Tier0ConfigError):
        load_settings(cfg, env={"SIMAI_TIER0_DETECT_CPUS": "half"})


def test_detect_cpu_set() -> None:
    assert detect_cpu_set("none", [0, 1, 2, 3]) is None
    assert detect_cpu_set("auto", [4, 5, 6, 7, 8]) == {4, 5}
    assert detect_cpu_set("auto", [3]) == {3}
    assert detect_cpu_set("0-1,3", [0, 1, 2, 3]) == {0, 1, 3}
    with pytest.raises(ValueError):
        detect_cpu_set("0-9", [0, 1, 2, 3])


# --------------------------------------------------------------------------- (a) 같은 결과


def _ids():
    n = iter(range(10**9))
    return lambda: f"00000000-0000-4000-8000-{next(n):012d}"


def _events(path: Path):
    """event_id(UUIDv7 무작위 비트)만 처음 나온 순서 번호로 바꾼다(test_max_speed_replay_is_deterministic 과 같다)."""
    order: dict[str, int] = {}
    out = []
    for m in _lines(path):
        payload = dict(m["payload"])
        payload["event_id"] = order.setdefault(payload["event_id"], len(order))
        out.append((m["msg_id"], m["sent_at"], payload))
    return out


def _service(tmp: Path, scene, *, split: bool, speed="max", avatar: bool = True):  # noqa: F811
    run, frames, session = scene
    channel = tmp / "avatar" / "avatar-input.npz"
    env = {**CHANNEL_ENV, "SIMAI_TIER0_PROCESS_SPLIT": "1" if split else "0"}
    if avatar:
        env.update(SIMAI_AVATAR_SHARE="1", SIMAI_AVATAR_CHANNEL=str(channel))
    settings = load_settings(_config(tmp, run, session, speed=speed), env=env)
    # 분리 경로의 검출기는 검출 프로세스가 만든다: 팩토리는 pickle 되는 이름이어야 한다
    service = Tier0Service(settings, detector_factory=functools.partial(SimTruthDetector, frames, 0),
                           tracker_backend_factory=OneTrackBackend, new_msg_id=_ids())
    return service, channel


def _replay(tmp: Path, scene, *, split: bool):  # noqa: F811
    service, channel = _service(tmp, scene, split=split)
    service.start()
    try:
        assert service.finished.wait(120), service.status()["file_input"]
        status = service.status()
    finally:
        assert service.stop(timeout_s=30) is True
    worlds = [(m["msg_id"], m["sent_at"], m["payload"]) for m in _lines(tmp / "out" / WORLD_STATE_FILE)]
    return worlds, _events(tmp / "out" / RISK_EVENT_FILE), status, channel


def test_replay_outputs_identical_single_vs_split(tmp_path, scene) -> None:  # noqa: F811
    one_worlds, one_events, one_status, one_channel = _replay(tmp_path / "single", scene, split=False)
    two_worlds, two_events, two_status, two_channel = _replay(tmp_path / "split", scene, split=True)
    assert len(one_worlds) > 20 and len(one_events) >= 1
    assert any(w[2]["tracks"] for w in one_worlds)
    assert two_worlds == one_worlds  # 클러스터·관절·World ID·msg_id(= 전이 evidence_ref)까지 같다
    assert two_events == one_events  # 이벤트 전이가 같다
    split_status = two_status["process_split"]
    assert one_status["process_split"] is None
    assert split_status["pid"] is not None and split_status["generation"] == 0 and split_status["restarts"] == 0
    assert split_status["out_of_order"] == 0 and split_status["dropped_observations"] == 0
    assert two_status["file_input"]["failed"] is None
    # 아바타 입력 공유: 분리 경로에서는 검출 프로세스가 같은 형식으로 쓴다
    assert two_status["avatar_share"]["offload"]["mode"] == "detect_process"
    for channel in (one_channel, two_channel):
        doc, _arrays = LatestChannelReader(channel).latest()
        assert doc["format"] == "simai.avatar-input@1" and doc["world"]["tracks"]


# --------------------------------------------------------------------------- (b) 장애


def test_file_replay_fails_when_detect_process_dies(tmp_path, scene) -> None:  # noqa: F811
    service, _ = _service(tmp_path, scene, split=True, speed=1.0, avatar=False)
    service.start()
    try:
        process = service.detect_process
        assert _wait(lambda: (process.status()["steps"] or 0) >= 5, timeout=30)
        process._gen.process.kill()
        assert service.finished.wait(30)
        failed = service.file_input.failed
        assert failed is not None and "검출 프로세스" in failed  # 진입점은 종료 코드 4
        assert process.status()["restarts"] == 0  # 재생은 다시 띄우지 않는다
    finally:
        service.stop(timeout_s=30)


def _live(max_restarts: int = 3):
    process = DetectorProcess(scripted_spec(), now_ms=lambda: time.time_ns() // 1_000_000, mono_s=time.monotonic,
                              max_restarts=max_restarts, backoff_s=0.1, backoff_max_s=0.2)
    process.start()
    feed = SplitDetectorFeed(process, connected=process.connected, source="LIVE", async_mode=True)
    live = RemoteLiveInput(process)
    live.start()
    return process, feed, live


def _collect(feed, until, timeout: float = 30.0) -> list[Observation2DFrame]:
    got: list[Observation2DFrame] = []

    def step() -> bool:
        got.extend(feed.poll(0))
        return until(got)

    assert _wait(step, timeout=timeout), f"관측 {len(got)}개"
    return got


def test_live_child_death_marks_cameras_unknown_and_restarts_with_new_local_ids() -> None:
    process, feed, live = _live()
    try:
        first = _collect(feed, lambda got: len(got) >= 6)
        assert _wait(lambda: process.connected("cam_A") and process.connected("cam_B"))
        assert {d.local_track_id for o in first for d in o.detections} == {"cam_A-t1", "cam_B-t1"}
        health = live.merge_health({"cameras": [{"id": "cam_A", "status": "OK"}]}, "i1")
        assert health["cameras"][0]["frames"]["received"] > 0

        process._gen.process.kill()
        # 죽은 순간부터 연결 아님(모르면 비움). 관측을 지어내지 않는다
        assert _wait(lambda: not process.connected("cam_A"), timeout=5)
        feed.poll(0)
        assert _wait(lambda: process.status()["generation"] == 1 and process.connected("cam_A"), timeout=60)
        last = {o.camera_id: o.estimated_capture_time_ms for o in first}
        after = _collect(feed, lambda got: any(d.local_track_id.endswith("-g1") for o in got for d in o.detections))
        new_ids = {d.local_track_id for o in after for d in o.detections if d.local_track_id.endswith("-g1")}
        assert new_ids <= {"cam_A-t1-g1", "cam_B-t1-g1"} and new_ids
        for o in after:  # 경계 큐는 카메라별 촬영 시각이 늘기만 한다
            assert o.estimated_capture_time_ms > last.get(o.camera_id, -1)
            last[o.camera_id] = o.estimated_capture_time_ms
        status = process.status()
        assert status["restarts"] == 1 and status["deaths"] == 1 and status["fatal"] is None
    finally:
        process.stop()


def test_live_child_dying_too_often_is_fatal() -> None:
    process, feed, _live_input = _live(max_restarts=1)
    try:
        _collect(feed, lambda got: len(got) >= 2)
        process._gen.process.kill()
        assert _wait(lambda: process.status()["generation"] == 1 and process.connected("cam_A"), timeout=60)
        process._gen.process.kill()
        assert _wait(lambda: process.fatal_reason is not None, timeout=30)
        assert not process.connected("cam_A")
    finally:
        process.stop()


def test_service_turns_fatal_detect_process_into_exit_code_4(tmp_path, scene) -> None:  # noqa: F811
    service, _ = _service(tmp_path, scene, split=True, avatar=False)  # 시작하지 않는다
    service.detect_process = SimpleNamespace(fatal_reason="60초 안에 4번")
    service._check_detect_process()
    assert service.fatal.is_set() and service.fatal_exit_code == 4


def test_detect_process_exits_when_judge_process_dies() -> None:
    psutil = pytest.importorskip("psutil")
    parent = subprocess.Popen([sys.executable, str(HERE / "split_orphan_parent.py")], stdout=subprocess.PIPE, text=True)
    try:
        line = parent.stdout.readline()
        child_pid = int(line.strip())
        assert psutil.pid_exists(child_pid)
        parent.kill()
        parent.wait(10)
        assert _wait(lambda: not psutil.pid_exists(child_pid) or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE,
                     timeout=15, every=0.1)
    finally:
        if parent.poll() is None:
            parent.kill()


# --------------------------------------------------------------------------- (c) 경계 큐·지연


def _obs(cam: str, t: int, seq: int) -> Observation2DFrame:
    det = PoseDetection((0.0, 0.0, 10.0, 10.0), 0.9, tuple((1.0, 1.0, 0.9) for _ in range(17)))
    return Observation2DFrame(cam, 0, seq, t, "CAMERA_STREAM", 5.0, (TrackedDetection(f"{cam}-t1", det),))


def test_boundary_queue_keeps_depth_drops_oldest_and_rejects_duplicates() -> None:
    spec = DetectorProcessSpec(mode="live", camera_ids=("cam_A", "cam_B"), detect_queue_depth=8)
    process = DetectorProcess(spec, now_ms=lambda: 0, mono_s=time.monotonic)  # 띄우지 않고 경계 큐만 쓴다
    process._accept([_obs("cam_A", 1000 + 40 * i, i) for i in range(12)])
    process._accept([_obs("cam_B", 1010, 1), _obs("cam_B", 1010, 1), _obs("cam_B", 990, 0), _obs("cam_C", 1, 1)])
    process._accept([_obs("cam_A", 1000, 0)])  # 이미 넘긴 시각(재시작한 검출 프로세스가 다시 낸 경우 등)
    out = process.take()
    a = [o for o in out if o.camera_id == "cam_A"]
    # 최신 1건이 아니라 깊이(8)만큼 앞뒤 관측을 남긴다(보간용). 넘친 4건은 가장 오래된 것부터 버렸다
    assert [o.frame_sequence for o in a] == list(range(4, 12))
    assert [o.frame_sequence for o in out if o.camera_id == "cam_B"] == [1]
    assert [o.estimated_capture_time_ms for o in out] == sorted(o.estimated_capture_time_ms for o in out)
    st = process.status()
    assert st["dropped_observations"] == 4 and st["out_of_order"] == 3 and st["unknown_camera"] == 1
    assert process.take() == []
    process.stop()


def test_processing_window_counts_unknown_when_detect_process_is_unhealthy() -> None:
    window = RemoteProcessingWindow(150)
    window.process = SimpleNamespace(spec=SimpleNamespace(mode="live"), healthy=lambda: True, in_progress_ms=lambda: None)
    window.add_samples([10.0, 20.0, None])
    took = window.take()
    assert took["samples"] == 3 and took["unknown"] == 1
    window.process = SimpleNamespace(spec=SimpleNamespace(mode="live"), healthy=lambda: False, in_progress_ms=lambda: None)
    took = window.take()
    assert took["unknown"] >= 1  # 하트비트 끊김·종료: 모름 → 아바타 관문이 멈춘다
    window.process = SimpleNamespace(spec=SimpleNamespace(mode="live"), healthy=lambda: True, in_progress_ms=lambda: 900.0)
    took = window.take()
    assert took["lag_ms_max"] >= 900.0  # 진행 중인 긴 검출을 정상으로 보지 않는다


def test_avatar_requests_are_dropped_not_blocking_when_child_is_absent() -> None:
    from services.tier0.detect_process import RemoteAvatarWriter

    spec = DetectorProcessSpec(mode="live", camera_ids=("cam_A",))
    process = DetectorProcess(spec, now_ms=lambda: 0, mono_s=time.monotonic)
    writer = RemoteAvatarWriter(process, wall_ms=lambda: 0)
    frame = SimpleNamespace(tracks=[SimpleNamespace(local_keys=(("cam_A", "cam_A-t1"),), avatar_update_hold=False)])
    started = time.perf_counter()
    for _ in range(100):
        assert writer.offer_tick(frame, {"tracks": [{}]}, 1000, SimpleNamespace(calibration_version="c1"), {}) is False
    assert time.perf_counter() - started < 0.5
    assert writer.dropped == 100 and writer.status()["send_dropped"] == 100
    process.stop()


def test_scripted_backend_is_picklable_by_name() -> None:
    import pickle

    assert pickle.loads(pickle.dumps(scripted_spec())).tracker_backend_factory is ScriptedTrackBackend
    assert threading.current_thread() is threading.main_thread()
