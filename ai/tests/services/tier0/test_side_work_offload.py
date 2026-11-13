"""곁가지 일 프로세스 분리(S15P31A101-662): 아바타 입력 공유 쓰기를 spawn 자식으로 뺀다(설정 offload_side_work, 기본 끔).

- 같은 문서·그림이면 분리 경로가 쓴 공유 채널 파일이 꺼짐 경로(LatestChannelWriter)와 같다(형식·메타·크롭 배열).
- 자식이 죽거나 멈추면 Tier 0(호출자)는 기다리지 않고, 자식을 다시 띄워 계속 쓴다. 상한을 넘으면 부모 안 쓰기로 넘어간다.
- 자식은 torch 를 불러오지 않는다(CUDA 문맥 없음). 칸 상한을 넘는 건은 버리고 센다.
- 녹화 재생(최대 속도, 결정론): 스위치를 끈 실행과 켠 실행(중간에 자식을 죽임)의 world_state·risk_event(I1 과 같은 봉투)가 같다.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE, AI_DIR / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_file_replay import (  # noqa: E402,F401 - scene 은 fixture
    CHANNEL_ENV,
    _config,
    _lines,
    scene,
)
from test_tier0_live_e2e import SimTruthDetector  # noqa: E402

from services.tier0.file_input import RISK_EVENT_FILE, WORLD_STATE_FILE  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from services.tier0.side_work import FALLBACK, OffloadedChannelWriter  # noqa: E402
from simai_ai.io.avatar_channel import (  # noqa: E402
    LatestChannelReader,
    LatestChannelWriter,
)
from simai_ai.providers.base import Frame  # noqa: E402


def _frames() -> dict[str, Frame]:
    rng = np.random.default_rng(3)
    return {cid: Frame(cid, 1000 + i, "FILE", {"stream_epoch": 1, "frame_sequence": 7 + i},
                       image=rng.integers(0, 255, (240, 320, 3), dtype=np.uint8))
            for i, cid in enumerate(("cam1", "cam2"))}


def _metadata(t: int = 1000) -> dict:
    tracks = [{"world_id": "w1", "per_camera": [
        {"camera_id": "cam1", "bbox": [40, 30, 90, 200], "keypoints_2d": [[60, 50, 0.9], [70, 150, 0.8]],
         "estimated_capture_time": 1000, "frame_ref": {"stream_epoch": 1, "frame_sequence": 7}},
        {"camera_id": "cam2", "bbox": [200, 20, 260, 180], "keypoints_2d": [],
         "estimated_capture_time": 1001, "frame_ref": {"stream_epoch": 1, "frame_sequence": 8}}]}]
    return {"world": {"t": t, "calibration_version": "calib-1", "tracks": tracks}, "calibration": {"cameras": []}}


def _wait(cond, timeout: float = 30.0) -> bool:
    return ts.wait_until(cond, timeout=timeout, step=0.05)


def _read(path: Path) -> tuple[dict, dict]:
    doc, arrays = LatestChannelReader(path).latest()
    doc = dict(doc)
    doc.pop("publication_id")
    return doc, {k: np.asarray(v).copy() for k, v in arrays.items()}


def test_offloaded_writer_publishes_the_same_file_as_in_process_writer(tmp_path) -> None:
    frames, clock = _frames(), (lambda: 123456)
    direct = LatestChannelWriter(tmp_path / "a" / "latest.npz", clock_ms=clock)
    off = OffloadedChannelWriter(tmp_path / "b" / "latest.npz", clock_ms=clock)
    try:
        assert direct.offer(_metadata(), frames)
        assert off.offer_deferred(_metadata, frames)
        assert _wait(lambda: direct.errors == 0 and (tmp_path / "a" / "latest.npz").exists() and off.written >= 1)
        a_doc, a_arrays = _read(tmp_path / "a" / "latest.npz")
        b_doc, b_arrays = _read(tmp_path / "b" / "latest.npz")
        assert a_doc == b_doc and a_doc["produced_at_ms"] == 123456 and a_doc["world"]["t"] == 1000
        assert set(a_arrays) == set(b_arrays) == {"cam1", "cam2"}
        for cid in a_arrays:
            np.testing.assert_array_equal(a_arrays[cid], b_arrays[cid])
        status = off.status()
        assert status["mode"] == "process" and status["alive"] and status["torch_in_child"] is False
        assert status["errors"] == 0 and status["restarts"] == 0
    finally:
        direct.close()
        off.close()
    assert off.status()["mode"] == "closed" and not off.status()["alive"]


def test_killed_child_is_restarted_and_offer_never_waits(tmp_path) -> None:
    off = OffloadedChannelWriter(tmp_path / "latest.npz", restart_interval_s=0.1)
    try:
        assert off.offer_deferred(_metadata, _frames())
        assert _wait(lambda: off.written >= 1)
        off._process.kill()  # 자식을 죽인다(시험만 내부에 손댄다)
        started = time.monotonic()
        for t in range(2000, 2100):
            off.offer_deferred(lambda t=t: _metadata(t), _frames())  # 호출자는 기다리지 않는다
        assert time.monotonic() - started < 0.5
        assert _wait(lambda: off.restarts == 1 and off.status()["alive"])
        assert _wait(lambda: off.offer_deferred(lambda: _metadata(5000), _frames()) and off.written >= 2
                     and _read(tmp_path / "latest.npz")[0]["world"]["t"] >= 2000)
        assert off.mode == "process" and "종료 코드" in (off.last_error or "")
    finally:
        off.close()


def test_stuck_child_is_restarted(tmp_path) -> None:
    off = OffloadedChannelWriter(tmp_path / "latest.npz", stuck_s=0.5, restart_interval_s=0.1)
    try:
        assert _wait(lambda: off.status()["alive"])
        off._slots[0].busy_seq, off._slots[0].busy_since = 10**9, time.monotonic()  # 돌아오지 않는 칸
        off._wake.set()
        assert _wait(lambda: off.restarts == 1)
        assert "멈춤" in off.last_error
    finally:
        off.close()


def test_repeated_crashes_fall_back_to_in_process_writer(tmp_path) -> None:
    off = OffloadedChannelWriter(tmp_path / "latest.npz", restart_interval_s=0.05, max_restarts=1)
    try:
        for _ in range(2):
            assert _wait(lambda: off._process is not None and off._process.is_alive())
            off._process.kill()
            off._wake.set()
            if off.restarts == 0:
                assert _wait(lambda: off.restarts == 1)
        assert _wait(lambda: off.mode == FALLBACK)
        assert off._process is None  # 작성자는 하나: 자식을 내린 뒤 넘어간다
        assert off.offer_deferred(lambda: _metadata(9000), _frames())
        assert _wait(lambda: (tmp_path / "latest.npz").exists() and _read(tmp_path / "latest.npz")[0]["world"]["t"] == 9000)
    finally:
        off.close()


def test_oversize_crops_are_dropped_and_counted(tmp_path) -> None:
    off = OffloadedChannelWriter(tmp_path / "latest.npz", slot_max_bytes=1024)
    try:
        assert off.offer_deferred(_metadata, _frames())
        assert _wait(lambda: off.oversize == 1)
        assert off.written == 0 and not (tmp_path / "latest.npz").exists()
    finally:
        off.close()


def test_switch_setting_defaults_off_and_env_overrides(tmp_path, scene) -> None:  # noqa: F811
    run, _, session = scene
    cfg = _config(tmp_path, run, session)
    assert load_settings(cfg, env={}).offload_side_work is False
    assert load_settings(cfg, env={"SIMAI_TIER0_OFFLOAD_SIDE_WORK": "1"}).offload_side_work is True
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc["offload_side_work"] = True
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    assert load_settings(cfg, env={}).offload_side_work is True
    assert load_settings(cfg, env={"SIMAI_TIER0_OFFLOAD_SIDE_WORK": "0"}).offload_side_work is False
    with pytest.raises(Tier0ConfigError):
        load_settings(cfg, env={"SIMAI_TIER0_OFFLOAD_SIDE_WORK": "yes"})


# --------------------------------------------------------------------------- 녹화 재생 같은 결과(스위치 끔 대 켬)


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


def _replay(tmp: Path, scene, *, offload: bool, kill_child: bool = False):  # noqa: F811
    run, frames, session = scene
    channel = tmp / "avatar" / "avatar-input.npz"
    env = {**CHANNEL_ENV, "SIMAI_AVATAR_SHARE": "1", "SIMAI_AVATAR_CHANNEL": str(channel),
           "SIMAI_TIER0_OFFLOAD_SIDE_WORK": "1" if offload else "0"}
    settings = load_settings(_config(tmp, run, session), env=env)
    detector = SimTruthDetector(frames, 0)
    service = Tier0Service(settings, detector_factory=lambda: detector, tracker_backend_factory=OneTrackBackend,
                           new_msg_id=_ids())
    killed = threading.Event()

    def killer() -> None:
        writer = service.perception.avatar_writer
        if _wait(lambda: writer.written >= 1, timeout=60) and writer._process is not None:
            writer._process.kill()
            killed.set()

    service.start()
    try:
        writer = service.perception.avatar_writer
        assert isinstance(writer, OffloadedChannelWriter) is offload
        if kill_child:
            threading.Thread(target=killer, daemon=True).start()
        assert service.finished.wait(120), service.status()["file_input"]
        if kill_child:
            assert killed.wait(60)
            assert _wait(lambda: writer.restarts >= 1 and writer.status()["alive"])
        status = service.status()["avatar_share"]
    finally:
        assert service.stop(timeout_s=30) is True
    worlds = [(m["msg_id"], m["sent_at"], m["payload"]) for m in _lines(tmp / "out" / WORLD_STATE_FILE)]
    return worlds, _events(tmp / "out" / RISK_EVENT_FILE), status, channel


def test_replay_outputs_are_identical_with_switch_off_and_on_even_when_child_dies(tmp_path, scene) -> None:  # noqa: F811
    off_worlds, off_events, off_status, off_channel = _replay(tmp_path / "off", scene, offload=False)
    on_worlds, on_events, on_status, on_channel = _replay(tmp_path / "on", scene, offload=True, kill_child=True)
    assert len(off_worlds) > 20 and len(off_events) >= 1
    assert on_worlds == off_worlds  # world_state 봉투(msg_id = 전이 evidence_ref 포함)가 한 줄도 다르지 않다
    assert on_events == off_events  # risk_event 전이가 같다
    # 곁가지 출력도 두 경로 모두 나왔다(같은 형식)
    assert off_status["offload"] is None and on_status["offload"]["restarts"] >= 1
    assert on_status["offload"]["torch_in_child"] is False
    for channel in (off_channel, on_channel):
        doc, _arrays = LatestChannelReader(channel).latest()
        assert doc["format"] == "simai.avatar-input@1" and doc["world"]["tracks"]
