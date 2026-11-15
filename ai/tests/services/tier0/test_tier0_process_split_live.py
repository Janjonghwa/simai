"""검출·판정 프로세스 분리(S15P31A101-728)의 LIVE 경로 노트북 E2E: 실제 H.264 → I0 TLS(검출 프로세스가 연 영상 포트) → 디코딩 →
검출(대역) → Local Tracker → 관측 경계 큐 → 판정 프로세스의 카메라 간 연결·World Track·R1 까지. test_tier0_live_e2e 와 같은 장면이다.

- 한 카메라가 끊기면 그 카메라만 NO_SIGNAL(검출 프로세스 보고값), health 는 계약을 지킨다.
- 기동 실패는 단일 경로와 같은 종료 코드다(영상 포트 설정 없음 2, 검출기 없음 4).
- 결과는 SIM 상한이다. 제품 성능 지표로 쓰지 않는다(AGENTS.md 규칙 7).
"""

from __future__ import annotations

import dataclasses
import functools
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_tier0_live_e2e import (  # noqa: E402,F401 - scene 은 fixture
    SimTruthDetector,
    _live_settings,
    _now,
    _stream,
    _video_threads,
    scene,
)

from services.tier0.detect_process import SplitDetectorFeed  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402


def _split(settings):
    return dataclasses.replace(settings, process_split=True)


def test_split_live_path_runs_from_h264_bytes_to_r1_judgment(tmp_path, scene) -> None:  # noqa: F811
    run, frames, videos = scene
    cams = tuple(frames)
    settings, port = _live_settings(tmp_path, run, cams)
    start = min(fs[0].estimated_capture_time for fs in frames.values())
    shift = _now() + 1500 - start
    service = Tier0Service(_split(settings), detector_factory=functools.partial(SimTruthDetector, frames, shift),
                           tracker_backend_factory=OneTrackBackend)
    service.start()
    stop = threading.Event()
    dropped = cams[-1]
    threads = [threading.Thread(target=_stream, args=(port, c, frames[c], videos[c], shift,
                                                      4500 if c == dropped else None, stop), daemon=True)
               for c in cams]
    try:
        assert isinstance(service.perception.feed, SplitDetectorFeed)
        assert "i0_video" in service.events and service.events.index("i0_video") < service.events.index("judge")
        assert service.detect_process.status()["pid"] is not None
        for t in threads:
            t.start()
        assert ts.wait_until(lambda: any(s.rule_id == "R1" for s in service.tracker.active()), timeout=30), \
            service.status()["live_input"]
        assert _video_threads() == []  # 영상 수신 스레드는 판정 프로세스에 없다(검출 프로세스가 연다)
        assert ts.wait_until(lambda: not service.perception.feed.camera_connected(dropped), timeout=15)
        perceived = service.judge.stats.perceived
        assert ts.wait_until(lambda: service.judge.stats.perceived > perceived + 5, timeout=10)
        payload = service._live_health(service.health.payload(), "test")
        cams_health = {c["id"]: c for c in payload["cameras"]}
        assert cams_health[dropped]["status"] == "NO_SIGNAL"
        assert [c for c in cams if cams_health[c]["status"] == "OK"] == [c for c in cams if c != dropped]
        for c in cams:
            assert cams_health[c]["frames"]["received"] > 0 and cams_health[c]["frames"]["gateway_to_runpod_loss"] == 0
        message = Envelope(ts.GATEWAY, SystemClock()).make("health", payload)
        assert schema_errors(message, "health") == [], schema_errors(message, "health")
        status = service.status()
        assert status["live_input"]["detector"]["detector_errors"] == 0
        assert status["live_input"]["detector"]["mismatched"] == 0
        split = status["process_split"]
        assert split["healthy"] and split["restarts"] == 0 and split["out_of_order"] == 0 and split["ipc_dropped"] == 0
        assert split["disabled"] == []  # -738·-744: 클립·배경 누적·이벤트 프레임·Re-ID 는 검출 프로세스가 한다
        # 클립(-738): R1(HIGH, LIVE) 발생 전이가 클립 대상으로 검출 프로세스에 가고, 링 버퍼는 그쪽 영상 수신이 채운다
        assert ts.wait_until(lambda: getattr(service.clips.stats, "triggers", 0) >= 1, timeout=10), service.clips.stats
        assert service.clips.stats.cmd_dropped == 0
        marker_detect = service._gpu_section("running")["processing"]
        assert marker_detect["detect_required"] is True and marker_detect["detect"]["samples"] > 0
    finally:
        stop.set()
        assert service.stop(timeout_s=30) is True
        for t in threads:
            t.join(5)
    assert not service.detect_process.status()["alive"]


def test_split_live_start_errors_keep_exit_codes(tmp_path, scene) -> None:  # noqa: F811
    run, frames, _ = scene
    cams = tuple(frames)
    settings, _ = _live_settings(tmp_path / "a", run, cams, video_env=False)
    with pytest.raises(Tier0StartError) as e:
        Tier0Service(_split(settings), detector_factory=functools.partial(SimTruthDetector, frames, 0),
                     tracker_backend_factory=OneTrackBackend).start()
    assert e.value.exit_code == 2 and "SIMAI_TIER0_VIDEO_PORTS" in str(e.value)

    settings, _ = _live_settings(tmp_path / "c", run, cams)
    with pytest.raises(Tier0StartError) as e:
        Tier0Service(_split(settings)).start()  # 엔진·가중치 없음
    assert e.value.exit_code == 4 and "SIMAI_TIER0_ENGINE" in str(e.value)
