"""프로세스 분리 곁가지(S15P31A101-738): 이벤트 프레임·클립·배경 누적을 검출 프로세스로 옮겨도 단일 경로와 같은 결과를 낸다.

(a) file 재생 결정론: 같은 녹화를 최대 속도로 단일 경로와 분리 경로에 넣으면 판정 출력(world_state·risk_event)이 같고, 이벤트 프레임
    (event_frame_ready 의 카메라·촬영 시각·JPEG 해시)·배경 누적(표본 수·회전 복구 입력 표본)도 같다. 분리 경로의 event_frame_ready 는
    판정 프로세스의 I1 송신 자리로 들어온다(msg_id·순서는 판정 쪽).
    장면의 R1 은 HIGH·REPLAY 라 실제로는 근거 사진 대상이 아니다. 시험은 대상 고르기 함수(wants_frames)만 바꿔 같은 경로를 태운다.
(b) 클립: 판정 쪽이 전이에서 뽑아 보낸 값(RemoteClips)으로 검출 쪽 클립 서비스가 단일 경로와 같은 대상·자르기를 한다.
(c) 자식 재시작: 상태 명령(배경 시작·회전 표본 크기)을 새 세대에 다시 보내고, 끝난 세대의 응답 대기·경계 큐를 비운다.
결과는 SIM 상한이다(제품 성능 아님, AGENTS 규칙 7).
"""

from __future__ import annotations

import functools
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("cv2")
pytest.importorskip("av")
np = pytest.importorskip("numpy")

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE, AI_DIR / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import split_fakes  # noqa: E402
from clip_stream import T0, Stream, transition  # noqa: E402
from split_fakes import scripted_spec  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_file_replay import CHANNEL_ENV, _config, _lines, scene  # noqa: E402,F401 - scene 은 fixture
from test_tier0_live_e2e import SimTruthDetector  # noqa: E402

from services.tier0 import event_frames as event_frames_module  # noqa: E402
from services.tier0.clips.ring_buffer import PacketRingBuffer  # noqa: E402
from services.tier0.clips.service import EventClipService  # noqa: E402
from services.tier0.clips.trigger import clip_trigger_for  # noqa: E402
from services.tier0.detect_process import DetectorProcess, RemoteLiveInput, SplitDetectorFeed  # noqa: E402
from services.tier0.event_frames import EventFrameIo  # noqa: E402
from services.tier0.file_input import RISK_EVENT_FILE, WORLD_STATE_FILE  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from services.tier0.split_side_work import ChildSideWork, RemoteClips, SideWorkSpec  # noqa: E402
from simai_ai.risk import FixedClock, Severity  # noqa: E402


def _wait(cond, timeout: float = 30.0, every: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(every)
    return bool(cond())


def _ids():
    n = iter(range(10**9))
    return lambda: f"00000000-0000-4000-8000-{next(n):012d}"


# --------------------------------------------------------------------------- (a) file 재생 결정론


def _side_config(tmp: Path, scene) -> Path:  # noqa: F811
    run, _, session = scene
    cfg = _config(tmp, run, session)
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    # 8초 장면에서 배경 표본·회전 복구 입력이 생기게 간격·하한을 줄인다. 최대 속도 재생은 1~2초에 끝나 배경 작업이 못 따라가
    # 버리는 표본(시각 의존)이 생기므로 대기열을 넉넉히 두고 같은 프로세스 안 누적(offload 끔)으로 잰다(분리 경로는 검출 프로세스 안)
    doc["clean_background"] = {"sample_interval_ms": 1000, "min_samples_per_camera": 3, "max_samples_per_camera": 12,
                              "setup_min_samples_per_camera": 3,
                              "offload": False, "queue_depth": 256, "max_pending_bytes": 512 * 1024 * 1024}
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    return cfg


def _replay(tmp: Path, scene, monkeypatch, *, split: bool):  # noqa: F811
    _, frames, _ = scene
    sent: list[tuple[str, dict]] = []
    io = EventFrameIo("https://ec2.invalid", "t" * 32, signer_factory=split_fakes.FrameSigner,
                      transport_factory=split_fakes.NullTransport)
    monkeypatch.setattr(Tier0Service, "_event_frame_io", lambda self: io)
    monkeypatch.setattr(Tier0Service, "_event_frame_send", lambda self: lambda kind, payload: sent.append((kind, payload)))
    # 장면의 R1(HIGH·REPLAY) 최초 발생을 근거 사진 대상으로 본다(고르기 함수만 바꿈: 판정 쪽에서 고르므로 두 경로 같다)
    monkeypatch.setattr(event_frames_module, "wants_frames",
                        lambda t: t.state.rule_id == "R1" and t.transition.name == "RAISED")
    env = {**CHANNEL_ENV, "SIMAI_TIER0_PROCESS_SPLIT": "1" if split else "0"}
    settings = load_settings(_side_config(tmp, scene), env=env)
    service = Tier0Service(settings, detector_factory=functools.partial(SimTruthDetector, frames, 0),
                           tracker_backend_factory=OneTrackBackend, new_msg_id=_ids())
    service.start()
    try:
        assert service.finished.wait(120), service.status()["file_input"]
        raised = [m for m in _lines(tmp / "out" / RISK_EVENT_FILE)
                  if m["payload"]["rule_id"] == "R1" and m["payload"]["lifecycle"] == "RAISED"]
        assert raised
        assert _wait(lambda: len(sent) >= len(raised), timeout=30), (len(sent), len(raised))
        background = service.clean_background
        assert background is not None and service.rotation_recovery is not None

        def rotation():
            views = background.rotation_snapshot()
            return None if views is None else {c: v["signature"] for c, v in views.items()}

        # 배경 작업 스레드가 마지막 표본까지 넣을 때까지(회전 입력 서명이 1초 동안 그대로)
        last = {"sig": None, "at": time.monotonic()}

        def settled() -> bool:
            sig = rotation()
            if sig != last["sig"]:
                last.update(sig=sig, at=time.monotonic())
                return False
            return sig is not None and time.monotonic() - last["at"] >= 1.0

        assert _wait(settled, timeout=60, every=0.2), last
        views = background.rotation_snapshot()
        status = service.status()
    finally:
        assert service.stop(timeout_s=30) is True
    bg_status = json.loads((service.settings.state_dir / "clean-background" / "status.json").read_text(encoding="utf-8"))
    worlds = [(m["msg_id"], m["payload"]) for m in _lines(tmp / "out" / WORLD_STATE_FILE)]
    return worlds, sent, views, bg_status, status


def _normalized_frames(sent):
    """event_id(UUIDv7 무작위)만 순서 번호로 바꾼다. 로컬 사본 경로·객체 주소에 든 event_id 도 같이 바꾼다."""
    order: dict[str, int] = {}
    out = []
    for kind, payload in sent:
        eid = payload["event_id"]
        n = str(order.setdefault(eid, len(order)))
        text = json.dumps(payload, sort_keys=True).replace(eid, f"E{n}")
        out.append((kind, json.loads(text)))
    return out


def test_split_side_work_matches_single_process_on_file_replay(tmp_path, scene, monkeypatch) -> None:  # noqa: F811
    one = _replay(tmp_path / "single", scene, monkeypatch, split=False)
    two = _replay(tmp_path / "split", scene, monkeypatch, split=True)
    one_worlds, one_sent, one_views, one_bg, one_status = one
    two_worlds, two_sent, two_views, two_bg, two_status = two

    assert two_worlds == one_worlds  # 판정 출력은 그대로
    # 이벤트 프레임: 같은 카메라·촬영 시각·JPEG(해시·크기)·로컬 사본 경로
    assert _normalized_frames(two_sent) == _normalized_frames(one_sent)
    frames = [f for _, p in one_sent for f in p["frames"]]
    assert frames and all("local_copy" in f for f in frames)
    assert two_status["process_split"]["side_work"]["event_frames"]["relayed"] == len(two_sent)

    # 배경 누적: 같은 카메라별 표본 수·생성(분리 경로는 검출 프로세스가 같은 상태 파일을 쓴다), 버린 표본 없음
    def counts(doc):
        return {c: (v["sample_count"], v.get("exported")) for c, v in doc["cameras"].items()}

    assert one_bg["queue_dropped"] == 0 and two_bg["queue_dropped"] == 0
    assert counts(two_bg) == counts(one_bg) and any(n for n, _ in counts(one_bg).values())
    # 회전 복구 입력(판정 쪽): 분리 경로는 검출 프로세스가 쓴 파일을 읽는다. 같은 서명·같은 축소 표본
    assert one_views is not None and two_views is not None and set(one_views) == set(two_views)
    for cid, view in one_views.items():
        other = two_views[cid]
        assert tuple(other["signature"]) == tuple(view["signature"]) and other["sample_times"] == view["sample_times"]
        for (img_a, mask_a), (img_b, mask_b) in zip(view["samples"], other["samples"], strict=True):
            assert np.array_equal(img_a, img_b) and np.array_equal(mask_a, mask_b)
    assert two_status["process_split"]["disabled"] == []


# --------------------------------------------------------------------------- (b) 클립


class _InProcess:
    """DetectorProcess 대역: 곁가지 명령을 같은 프로세스의 ChildSideWork 에 바로 넘긴다(명령 변환만 본다)."""

    def __init__(self, side: ChildSideWork, clock) -> None:
        self.side, self.clock = side, clock

    def send_side(self, cmd, *, required=False) -> bool:
        self.side.handle(self.clock.now_ms(), *cmd)
        return True

    def snapshot(self):
        return self.side.status()

    def on_side(self, kind, handler) -> None:
        self.side_handlers = {**getattr(self, "side_handlers", {}), kind: handler}


def test_remote_clips_cut_the_same_clips_as_single_process() -> None:
    cams = ("cam_A", "cam_B")
    clock = FixedClock(T0)
    single = EventClipService(PacketRingBuffer(cams), clock)
    side = ChildSideWork(SideWorkSpec(clips=True), cams, clock=clock, put=lambda _m: True, put_blocking=lambda _m: None)
    remote = RemoteClips(_InProcess(side, clock))
    stream = Stream(cams)
    for m in stream.start():
        single.receive(m)
        side.clips.receive(m)  # 분리 경로: 검출 프로세스의 영상 수신이 바로 넣는다
    second = "0192a0a0-0000-7000-8000-000000000002"
    for second_i in range(50):
        for f in stream.advance(1_000):
            single.receive(f)
            side.clips.receive(f)
        clock.set(stream.t_ms)
        if second_i == 25:
            ts = [transition(observation_time=stream.t_ms), transition(observation_time=stream.t_ms),  # 같은 열쇠 중복
                  transition(severity=Severity.CAUTION, observation_time=stream.t_ms),  # 대상 아님
                  transition(severity=Severity.CRITICAL, observation_time=stream.t_ms, event_id=second)]
            assert remote.on_transitions(ts) == [t for t in map(clip_trigger_for, ts) if t is not None]
            single.on_transitions(ts)
        single.poll()
        remote.poll()
    child = vars(side.clips.stats)
    assert child == vars(single.stats) | {"ignored_transitions": 0}  # 대상 아닌 전이는 판정 쪽에서 걸러 센다
    assert single.stats.cut == 4 and single.stats.triggers == 2 and single.stats.duplicate_triggers == 1
    assert remote.stats.cut == single.stats.cut and remote.pending == single.pending == 0
    assert remote.stats.parent_ignored_transitions == single.stats.ignored_transitions == 1


# --------------------------------------------------------------------------- (c) 자식 재시작


def test_restart_replays_state_commands_and_clears_waiters(tmp_path) -> None:
    from dataclasses import replace

    from services.tier0.settings import CleanBackgroundSettings
    from services.tier0.split_side_work import RemoteCleanBackground

    spec = replace(scripted_spec(), side_work=SideWorkSpec(clean_background=CleanBackgroundSettings(offload=False),
                                                           state_dir=str(tmp_path), clips=True))
    process = DetectorProcess(spec, now_ms=lambda: time.time_ns() // 1_000_000, mono_s=time.monotonic,
                              backoff_s=0.1, backoff_max_s=0.2)
    process.start()
    feed = SplitDetectorFeed(process, connected=process.connected, source="LIVE", async_mode=True)
    live = RemoteLiveInput(process)
    try:
        document = {"calibration_version": "cal-1",
                    "cameras": [{"camera_id": c, "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "t": [0, 0, 0]}
                                for c in spec.camera_ids]}
        background = RemoteCleanBackground(process, spec.side_work.clean_background, tmp_path, document,
                                           {"source": "LIVE"})
        background.configure_rotation_samples(64)
        live.start()
        assert _wait(lambda: "bg_rotation_signatures" in process.snapshot(), timeout=30)
        assert background.rotation_snapshot() is None  # 표본이 모자라면 모름(None), 응답은 온다
        assert background.dump_failures == 0
        status_file = tmp_path / "clean-background" / "status.json"
        assert _wait(status_file.is_file, timeout=30)

        process._gen.process.kill()
        assert _wait(lambda: process.status()["generation"] == 1 and process.healthy(), timeout=60)
        # 새 세대에도 배경 누적이 다시 시작됐다(상태 명령 재전송): 상태가 다시 실리고 회전 입력 요청에 답한다
        assert _wait(lambda: "bg_rotation_signatures" in process.snapshot(), timeout=30)
        status_file.unlink()
        assert _wait(status_file.is_file, timeout=30)
        assert background.rotation_snapshot() is None and background.dump_failures == 0
        assert process._waiting == set() and process._replies == {}  # 끝난 세대의 응답 대기가 남지 않는다
        feed.poll(0)
        assert all(not d.local_track_id.endswith("-g0") for o in feed.poll(0) for d in o.detections)
    finally:
        process.stop()
    assert not process.status()["alive"]


def test_side_request_without_child_returns_none_quickly() -> None:
    spec = scripted_spec()
    process = DetectorProcess(spec, now_ms=lambda: 0, mono_s=time.monotonic)  # 띄우지 않는다
    started = time.perf_counter()
    assert process.side_request(("bg_dump_rotation", "x", ()), timeout_s=5) is None
    assert process.send_side(("frames", "e", 0, 0, ("cam_A",))) is False
    assert time.perf_counter() - started < 0.5
    process.stop()


def test_remote_event_frames_choose_requests_on_judge_side_and_relay_ready_to_i1() -> None:
    from services.tier0.i1_client import QueueFull
    from services.tier0.split_side_work import RemoteEventFrames

    sent_cmds, handlers = [], {}
    process = SimpleNamespace(send_side=lambda cmd, required=False: sent_cmds.append(cmd) or True,
                              on_side=lambda kind, fn: handlers.__setitem__(kind, fn), snapshot=lambda: {})
    calls = {"n": 0}
    relayed = []

    def send(kind, payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise QueueFull(kind)  # 신뢰 큐가 차면 같은 내용으로 기다렸다 다시 넣는다
        relayed.append((kind, payload))

    frames = RemoteEventFrames(process, ("cam_A", "cam_B"), send_event=send)
    frames.start()
    try:
        t = transition(observation_time=T0 + 4000, severity=Severity.CRITICAL)
        track = SimpleNamespace(world_id=t.state.world_ids[0], cameras=("cam_B",))
        frames.on_transitions([t, t], [track])  # 같은 event_id 는 한 번
        assert sent_cmds == [("frames", t.state.event_id, T0 + 4000, t.occurred_at, ("cam_B", "cam_A"))]
        handlers["event_frame_ready"]({"event_id": t.state.event_id, "frames": [{"camera_id": "cam_B"}]})
        assert _wait(lambda: relayed, timeout=5)
        assert relayed[0][0] == "event_frame_ready" and calls["n"] == 2
    finally:
        assert frames.stop(2.0)
