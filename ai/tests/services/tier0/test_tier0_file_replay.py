"""Tier 0 파일 입력(녹화 재생, S15P31A101-215) 노트북 E2E: 합성 녹화 세션(gateway/recorder 형식, 실제 H.264 바이트) →
I0 재생 → 디코딩(PyAV CPU) → 검출(대역) → Local Tracker(대역) → World Track → R1 판정 → world_state·risk_event JSONL.

- 장면·영상·검출기 대역은 LIVE E2E(test_tier0_live_e2e)와 같다: SIM 교실에서 한 사람이 R1 구역(HIGH)으로 걸어 들어가 선다.
  영상은 프레임 번호를 화소로 새긴 H.264 이고(사람 이미지 없음, 시험 임시 디렉터리에만 씀), 검출기 대역은 그림 번호의 SIM
  keypoint 를 낸다. SIM 상한 결과다(제품 성능 아님, AGENTS 규칙 7).
- 판정 출처는 REPLAY 다. 현장 경보·I1 로 아무것도 나가지 않는지, LIVE 저장 위치와 섞이지 않는지, 보정·녹화가 틀리면 시작하지
  않는지 본다. 음성 대조: 출처만 LIVE 로 되돌리면 같은 경로에서 local_alert 가 나간다(시험이 새는 것을 잡는다).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE, AI_DIR / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_video as lv  # noqa: E402
import tier0_site as ts  # noqa: E402
from recording_session import Segment, write_session  # noqa: E402
from recording_session import header as frame_header  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_live_e2e import SimTruthDetector  # noqa: E402

from services.tier0 import service as service_module  # noqa: E402
from services.tier0.file_input import RISK_EVENT_FILE, WORLD_STATE_FILE  # noqa: E402
from services.tier0.judge import JudgmentLoop  # noqa: E402
from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.io import SimObservationFrameSource  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import Origin  # noqa: E402

SESSION = "rec-test-file-replay"
SCENE_START_MS = ts.scenario()["start_time_ms"]  # SIM 장면 시작 = 녹화 첫 촬영 시각
RECEIVE_LAG_MS = 30


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    """SIM 장면 → 카메라 4대 합성 녹화 세션(촬영 시각 = SIM 시각, 수신 = +30 ms). 녹화는 읽기만 하므로 모듈에서 하나를 쓴다."""
    tmp = tmp_path_factory.mktemp("replay")
    run = ts.run_sim(tmp, ts.scenario(8_000))
    src = SimObservationFrameSource(run)
    frames: dict[str, list] = {c: [] for c in src.camera_ids}
    for f in src.frames():
        frames[f.camera_id].append(f)
    cameras = {}
    for cam, fs in frames.items():
        video = lv.encode(list(range(len(fs))), gop=10)
        seqs = [f.frame_ref["frame_sequence"] for f in fs]
        assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))  # 녹화 형식: frame_sequence 가 이어진다
        segment = Segment(0, lv.codec_meta(cam, 0, video))
        for f, (au, key) in zip(fs, video.frames, strict=True):
            t = f.estimated_capture_time
            segment.frames.append((frame_header(cam, 0, f.frame_ref["frame_sequence"], t, key,
                                                received_ms=t + RECEIVE_LAG_MS), au))
        cameras[cam] = [segment]
    session = write_session(tmp, SESSION, cameras)
    return run, frames, session


def _config(tmp: Path, run: Path, session: Path, *, speed="max", jsonl=True, calibration: Path | None = None,
            camera_ids=None, tick_ms: int = 100) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    calibration_doc = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    ts.archive_bundle(tmp / "boot", calibration_doc, "cfg-1", SCENE_START_MS - 1000)  # 녹화 시각보다 먼저 낸 묶음
    doc = {
        "gateway_id": ts.GATEWAY,
        "state_dir": str(tmp / "state"),
        "scene_version": "scene-sim",
        "calibration_file": str(calibration or run / "calibration.json"),
        "input": {"mode": "file", "session_dir": str(session), "decoder": "cpu", "speed": speed},
        "bootstrap": {"archive": str(tmp / "boot"), "config_version": "cfg-1"},
        "lifecycle": {"clear_hold_ms": 1500, "persistence": [2, 3], "resume_window_ms": 10000},
        "tick_ms": tick_ms,
        "checkpoint_interval_s": 0.5,
    }
    if jsonl:
        doc["input"]["jsonl_dir"] = str(tmp / "out")
    if camera_ids is not None:
        doc["input"]["camera_ids"] = camera_ids
    cfg = tmp / "tier0-file.json"
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    return cfg


# 실제 알림 채널 환경 변수가 있어도 file 입력은 열지 않는다(반례: 이 값들로 LIVE·SIM 이면 I1 접속·제어 채널을 연다)
CHANNEL_ENV = {"SIMAI_EC2_I1_URL": "wss://ec2.invalid/ws/tier0", "SIMAI_TIER0_TOKEN": "t" * 32,
               "SIMAI_TIER0_CONTROL_PORT": "70000", "RUNPOD_TCP_PORT_70000": "1"}


class SignalRecorder:
    """판정 루프의 현장 경보 송신 자리(send_i0)를 가로챈다: 출처와 상관없이 LIVE 경로 코드가 무엇을 보내려 했는지 센다."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, kind, payload) -> bool:
        self.sent.append((kind, dict(payload)))
        return True


def _run(tmp: Path, scene, monkeypatch, *, speed="max", origin: Origin | None = None, new_msg_id=None, tick_ms=100):
    run, frames, session = scene
    settings = load_settings(_config(tmp, run, session, speed=speed, tick_ms=tick_ms), env=CHANNEL_ENV)
    recorder = SignalRecorder()

    class Spy(JudgmentLoop):
        def __init__(self, **kw):
            kw["send_i0"] = recorder  # 제어 채널이 있었다면 연결됐을 자리
            super().__init__(**kw)

    monkeypatch.setattr(service_module, "JudgmentLoop", Spy)
    if origin is not None:
        monkeypatch.setitem(service_module.INPUT_ORIGINS, "file", origin)
    detector = SimTruthDetector(frames, 0)
    kwargs = {} if new_msg_id is None else {"new_msg_id": new_msg_id}
    service = Tier0Service(settings, detector_factory=lambda: detector, tracker_backend_factory=OneTrackBackend, **kwargs)
    service.start()
    try:
        assert service.finished.wait(90), service.status()["file_input"]
    finally:
        assert service.stop(timeout_s=30) is True
    return service, settings, recorder, detector


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_replay_writes_contract_jsonl_and_sends_no_alerts(tmp_path, scene, monkeypatch) -> None:
    service, settings, recorder, detector = _run(tmp_path, scene, monkeypatch)
    # 실제 알림 채널: 설정이 I1·제어 채널을 만들지 않았고, LIVE 경로의 송신 자리에도 아무것도 가지 않았다
    assert settings.i1 is None and settings.control is None and service.link is None and service.control is None
    assert recorder.sent == [] and service.judge.stats.local_alerts == 0
    assert str(service.tracker.origin) == "REPLAY"
    # 검출 경로가 녹화 프레임을 그림 ↔ 헤더 결합 오류 없이 다 지나갔다
    assert detector.inferred > 0 and detector.misbound == 0
    file_status = service.status()["file_input"]
    assert file_status["finished"] and file_status["errors"] == 0 and file_status["failed"] is None
    assert all(c["decode_failed"] == 0 for c in file_status["cameras"].values())

    out = tmp_path / "out"
    worlds = _lines(out / WORLD_STATE_FILE)
    assert len(worlds) == service.judge.stats.perceived == service.jsonl.world_state_lines > 20
    for message in worlds:
        assert schema_errors(message, "world_state") == []
    # 모든 줄이 입력 출처(FILE, 녹화 dataset_id)를 싣는다: 파일을 읽는 쪽(-187)이 LIVE 결과와 섞지 않게
    for message in worlds:
        assert message["payload"]["input_provenance"]["frames"] == {
            "source": "FILE", "dataset_id": f"recording/{SESSION}", "camera_ids": list(scene[1])}
    t_latest = [m["payload"]["t_latest_global"] for m in worlds]
    sent = [m["sent_at"] for m in worlds]
    assert t_latest == sorted(t_latest) and sent == sorted(sent)  # 녹화 시각 기준, 거꾸로 가지 않는다
    start = json.loads((scene[2] / "manifest.json").read_text(encoding="utf-8"))["started_at_ms"]
    assert start <= t_latest[0] and t_latest[-1] <= start + 8_000 + 1_000  # 재생 시계 = 녹화 시각(실제 시각 아님)
    assert all(m["payload"]["config_version"] == "cfg-1" for m in worlds)

    events = _lines(out / RISK_EVENT_FILE)
    assert len(events) == service.jsonl.risk_event_lines == service.judge.stats.transitions
    for message in events:
        assert schema_errors(message, "risk_event") == []
        assert message["payload"]["source"] == "REPLAY" and "delivery" not in message["payload"]
    raised = [m for m in events if m["payload"]["rule_id"] == "R1" and m["payload"]["lifecycle"] == "RAISED"]
    assert raised and raised[0]["payload"]["severity"] == "HIGH"  # LIVE 였다면 현장 경보 대상 등급
    # 판정 근거 world_state 가 같은 파일에 있다(evidence_ref 는 그 tick 의 world_state msg_id)
    assert raised[0]["sent_at"] >= worlds[0]["sent_at"]

    # LIVE 저장 위치와 섞이지 않는다: 상태는 state/replay/<실행>/ 이고 LIVE 의 state/journal 은 없다. REPLAY 는 저널에 넣지 않는다
    live_state = tmp_path / "state"
    assert settings.state_dir.parent == live_state / "replay" and settings.state_dir.name.startswith("run-")
    assert not (live_state / "journal").exists() and not (live_state / "tier0.live").exists()
    assert service.journal.next_offset == 0 and service.judge.stats.journal_errors == 0


def test_negative_control_live_origin_on_the_same_path_does_send_local_alert(tmp_path, scene, monkeypatch) -> None:
    """음성 대조: 출처만 LIVE 로 되돌리면 같은 녹화·같은 경로에서 local_alert 가 송신 자리로 간다.
    그래서 위 시험의 '0건'은 시험이 새는 것을 못 보는 탓이 아니다."""
    service, _settings, recorder, _ = _run(tmp_path, scene, monkeypatch, origin=Origin.LIVE)
    assert [k for k, _ in recorder.sent].count("local_alert") >= 1
    assert all(p["source"] == "LIVE" for k, p in recorder.sent if k == "local_alert")
    assert service.journal.next_offset > 0  # LIVE 였다면 저널에도 들어간다(재생 상태 디렉터리 안)


def test_max_speed_replay_is_deterministic(tmp_path, scene, monkeypatch) -> None:
    """명세 21.3: 시계를 주입식으로 고정해 결과가 매번 같다(최대 속도는 tick 마다 tick_ms 씩만 나아간다)."""
    def ids():
        n = iter(range(10**9))
        return lambda: f"00000000-0000-4000-8000-{next(n):012d}"

    def events(path):
        """event_id(UUIDv7, 무작위 비트)만 처음 나온 순서 번호로 바꾼다. 나머지(msg_id·시각·전이·수치)는 그대로 비교한다."""
        order: dict[str, int] = {}
        out = []
        for m in _lines(path):
            payload = dict(m["payload"])
            payload["event_id"] = order.setdefault(payload["event_id"], len(order))
            out.append((m["msg_id"], m["sent_at"], payload))
        return out

    outputs, event_outputs = [], []
    for name in ("a", "b"):
        _run(tmp_path / name, scene, monkeypatch, new_msg_id=ids())
        outputs.append([(m["msg_id"], m["sent_at"], m["payload"]) for m in _lines(tmp_path / name / "out" / WORLD_STATE_FILE)])
        event_outputs.append(events(tmp_path / name / "out" / RISK_EVENT_FILE))
    assert outputs[0] == outputs[1] and len(outputs[0]) > 20
    assert event_outputs[0] == event_outputs[1] and len(event_outputs[0]) >= 1


def test_realtime_speed_replay_runs_on_the_recording_clock(tmp_path, scene, monkeypatch) -> None:
    """배속 재생(speed 2 = 실제 시간의 2배): 녹화 시각으로 진행하고 끝나면 finished. 판정 tick 은 실제 시간 tick_ms 마다라
    녹화 시각으로는 tick_ms x speed 마다 표본을 뜬다(여기서는 50 ms x 2 = 100 ms, 최대 속도와 같은 간격)."""
    service, *_ = _run(tmp_path, scene, monkeypatch, speed=2, tick_ms=50)
    worlds = _lines(tmp_path / "out" / WORLD_STATE_FILE)
    assert len(worlds) > 20 and all(schema_errors(m, "world_state") == [] for m in worlds)
    events = _lines(tmp_path / "out" / RISK_EVENT_FILE)
    assert any(m["payload"]["rule_id"] == "R1" and m["payload"]["source"] == "REPLAY" for m in events)


# --------------------------------------------------------------------------- 시작 거절(지어내지 않는다)


def _start(settings):
    return Tier0Service(settings, detector_factory=lambda: SimTruthDetector({}, 0),
                        tracker_backend_factory=OneTrackBackend).start()


def test_file_mode_needs_a_calibration_file_path(tmp_path, scene) -> None:
    run, _, session = scene
    cfg = _config(tmp_path, run, session)
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    del doc["calibration_file"]
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Tier0ConfigError, match="file 입력은 보정 문서 경로가 필요하다"):
        load_settings(cfg, env={})


@pytest.mark.parametrize("case", ["missing", "no_extrinsics", "camera_not_calibrated"])
def test_replay_refuses_to_start_without_usable_calibration(tmp_path, scene, case) -> None:
    """보정 문서가 없거나 외부 파라미터가 없거나 녹화 카메라가 보정에 없으면 시작하지 않는다(종료 코드 2, 지어내지 않음)."""
    run, _, session = scene
    calibration = tmp_path / "calibration.json"
    doc = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    if case == "no_extrinsics":
        del doc["cameras"][0]["R"]
        del doc["cameras"][0]["t"]
    elif case == "camera_not_calibrated":
        doc["cameras"] = doc["cameras"][1:]
    if case != "missing":
        calibration.write_text(json.dumps(doc), encoding="utf-8")
    settings = load_settings(_config(tmp_path, run, session, calibration=calibration), env={})
    with pytest.raises(Tier0StartError) as e:
        _start(settings)
    assert e.value.exit_code == 2
    assert ("없는 녹화 카메라" if case == "camera_not_calibrated" else "보정 문서를 읽지 못했다") in str(e.value)
    assert not settings.live_marker.exists() and not (tmp_path / "out" / WORLD_STATE_FILE).exists()


def test_replay_refuses_damaged_manifest_and_missing_camera(tmp_path, scene) -> None:
    run, _, session = scene
    settings = load_settings(_config(tmp_path / "a", run, session, camera_ids=["cam_X"]), env={})
    with pytest.raises(Tier0StartError) as e:
        _start(settings)
    assert e.value.exit_code == 4 and "manifest 에 없는 카메라" in str(e.value)

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{", encoding="utf-8")
    settings = load_settings(_config(tmp_path / "b", run, broken), env={})
    with pytest.raises(Tier0StartError) as e:
        _start(settings)
    assert e.value.exit_code == 4 and "녹화를 열지 못했다" in str(e.value)


def test_replay_does_not_overwrite_existing_jsonl(tmp_path, scene) -> None:
    run, _, session = scene
    settings = load_settings(_config(tmp_path, run, session), env={})
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / WORLD_STATE_FILE).write_text("earlier\n", encoding="utf-8")
    with pytest.raises(Tier0StartError) as e:
        _start(settings)
    assert e.value.exit_code == 2 and "FileExistsError" in str(e.value)
    assert (tmp_path / "out" / WORLD_STATE_FILE).read_text(encoding="utf-8") == "earlier\n"


# --------------------------------------------------------------------------- 재생 입력 단위(가짜 디코더)


def test_file_input_reconnect_marks_camera_lost_and_drains_only_after_flush(tmp_path) -> None:
    """반례(순서·경계): 세그먼트 사이(재접속)에는 그 카메라를 연결로 보지 않고, 마지막 세그먼트 끝은 끊김으로 바꾸지 않으며,
    녹화를 다 넣었어도 대기열에 프레임이 남아 있으면 끝났다고 하지 않는다."""
    from recording_session import codec_doc

    from services.tier0.file_input import FileInput, ReplayClock
    from simai_ai.io import FakeDecoder, FreshnessConfig, I0Ingest, RecordingReplay
    from simai_ai.io.decoder import fake_payload

    t0 = 1792552246000

    def seg(epoch, start, n):
        return Segment(epoch, codec_doc("cam_A", epoch), [
            (frame_header("cam_A", epoch, i, start + 40 * i, i == 0, received_ms=start + 40 * i),
             fake_payload("cam_A", epoch, i, start + 40 * i)) for i in range(n)])

    session = write_session(tmp_path, "rec-test-reconnect", {"cam_A": [seg(0, t0, 3), seg(1, t0 + 1000, 2)]})
    replay = RecordingReplay(session)
    clock = ReplayClock(replay.start_ms, None)
    ingest = I0Ingest(replay.camera_ids, lambda _c: FakeDecoder(), clock_ms(clock),
                      FreshnessConfig(queue_depth=1, max_queue_wait_ms=10_000, reorder_window_ms=0))
    fi = FileInput(replay, ingest, clock)
    assert not fi.connected("cam_A")  # 코덱 정보 전
    fi.pump()  # t0: 코덱 + 첫 키프레임
    assert fi.connected("cam_A") and not fi.drained()
    clock.advance_ms(100)
    fi.pump()  # 첫 세그먼트 끝(t0+80) → 연결 끝
    assert not fi.connected("cam_A")
    clock.advance_ms(900)
    fi.pump()  # t0+1000: 둘째 세그먼트 코덱 + 키프레임
    assert fi.connected("cam_A")
    clock.advance_ms(100)
    fi.pump()  # 녹화 끝: 디코더 비움. 마지막 세그먼트 끝은 끊김이 아니다
    assert replay.finished and fi.closed and fi.connected("cam_A")
    assert not fi.drained()  # 대기열에 마지막 프레임이 남아 있다
    tickets = ingest.next_batch()
    assert [t.frame.frame_ref for t in tickets] == [{"stream_epoch": 1, "frame_sequence": 1}]
    assert fi.drained() and fi.errors == 0 and fi.failed is None


def clock_ms(replay_clock):
    class _Clock:
        def now_ms(self) -> int:
            return replay_clock.wall_ms()

    return _Clock()


def test_each_replay_run_gets_its_own_state_dir(tmp_path, scene) -> None:
    """반례(재시작): 같은 설정으로 다시 재생해도 이전 재생의 저널·체크포인트(Track)를 되살리지 않는다(실행마다 새 디렉터리)."""
    run, _, session = scene
    cfg = _config(tmp_path, run, session)
    a, b = load_settings(cfg, env={}), load_settings(cfg, env={})
    assert a.state_dir != b.state_dir and a.state_dir.parent == b.state_dir.parent == tmp_path / "state" / "replay"
    assert a.journal_dir.parent == a.state_dir and a.live_marker.parent == a.state_dir


def test_replay_clock_modes() -> None:
    from services.tier0.file_input import ReplayClock

    fake = iter([0, 0, 500_000_000, 500_000_000])
    clock = ReplayClock(1_000, 2.0, monotonic_ns=lambda: next(fake))
    assert clock.wall_ms() == 1_000 and clock.wall_ms() == 1_000 + 1_000  # 실제 0.5 s × 2배
    with pytest.raises(RuntimeError):
        clock.advance_ms(1)
    fast = ReplayClock(5_000, None)
    assert fast.max_speed and fast.wall_ms() == 5_000
    fast.advance_ms(100)
    assert fast.wall_ms() == 5_100 and fast.mono_ns() == 100_000_000
    with pytest.raises(ValueError):
        ReplayClock(0, 0)


# --------------------------------------------------------------------------- 재생 실패는 정상 완료가 아니다 (리뷰 F1·F2)


def _cli(tmp: Path, scene, monkeypatch, *, max_seconds: float = 60) -> tuple[int, float]:
    """진입점(main)을 시험 검출기·추적기 대역으로 돌린다. --run-seconds 는 안전장치다: 재생 실패가 finished 로 이어지지 않으면
    이 시간까지 기다린 뒤 0 으로 끝나므로 시험이 멈추지 않고 실패한다."""
    import time

    from services.tier0.__main__ import main

    run, frames, session = scene
    cfg = _config(tmp, run, session)
    original = service_module.Tier0Service

    class WithStandIns(original):  # type: ignore[misc, valid-type]
        def __init__(self, settings, **kw):
            super().__init__(settings, detector_factory=lambda: SimTruthDetector(frames, 0),
                             tracker_backend_factory=OneTrackBackend, **kw)

    monkeypatch.setattr(service_module, "Tier0Service", WithStandIns)
    started = time.monotonic()
    code = main(["--config", str(cfg), "--run-seconds", str(max_seconds), "--log-level", "WARNING"])
    return code, time.monotonic() - started


def test_decoder_flush_failure_ends_the_replay_with_exit_code_4(tmp_path, scene, monkeypatch) -> None:
    """F1: 녹화 끝에 디코더 flush 가 DecodeError 를 내면 재생 실패로 남기고 진입점은 제한 시간 안에 종료 코드 4 다
    (판정 스레드가 예외로 죽어 진입점이 무기한 기다리지 않는다)."""
    from simai_ai.io import DecodeError, PyAvDecoder

    class FlushFails(PyAvDecoder):
        def flush(self):
            raise DecodeError("injected flush failure")

    monkeypatch.setattr(service_module, "PyAvDecoder", FlushFails)
    code, elapsed = _cli(tmp_path, scene, monkeypatch)
    assert code == 4 and elapsed < 45


class _FailingFile:
    """n 번째 write 부터 OSError(디스크 가득 참)를 내는 파일 대역. 그 전 줄은 실제 파일에 쓴다."""

    def __init__(self, f, fail_at: int) -> None:
        self._f, self._fail_at, self.writes = f, fail_at, 0

    def write(self, text):
        self.writes += 1
        if self.writes >= self._fail_at:
            raise OSError(28, "injected disk full")
        return self._f.write(text)

    def flush(self):
        self._f.flush()

    def close(self):
        self._f.close()


@pytest.mark.parametrize("target, fail_at", [("risk_event", 2), ("world_state", 10)])
def test_jsonl_write_failure_is_a_failed_replay_with_exit_code_5(tmp_path, scene, monkeypatch, target, fail_at) -> None:
    """F2: 결과 기록이 실패하면(디스크·권한) 위험 이벤트를 잃은 채 정상 완료로 보고하지 않는다. 종료 코드 5, 이미 쓴 줄은
    남고, 실패 지점(파일·줄 번호)이 replay_failed.json 에 남는다."""
    from services.tier0 import file_input
    from services.tier0.file_input import FAILURE_FILE

    original_init = file_input.JsonlOutput.__init__

    def init(self, *a, **kw):
        original_init(self, *a, **kw)
        if target == "risk_event":
            self._events = _FailingFile(self._events, fail_at)
        else:
            self._world = _FailingFile(self._world, fail_at)

    monkeypatch.setattr(file_input.JsonlOutput, "__init__", init)
    code, elapsed = _cli(tmp_path, scene, monkeypatch)
    assert code == 5 and elapsed < 45
    out = tmp_path / "out"
    failure = json.loads((out / FAILURE_FILE).read_text(encoding="utf-8"))
    name = RISK_EVENT_FILE if target == "risk_event" else WORLD_STATE_FILE
    assert failure["file"] == name and failure["line"] == fail_at and "injected disk full" in failure["error"]
    kept = _lines(out / name)
    assert len(kept) == fail_at - 1  # 실패 전 줄은 그대로, 실패 뒤로는 쓰지 않음
    for message in kept:
        assert schema_errors(message, target) == []
