"""Tier 0 LIVE 경로 노트북 E2E (S15P31A101-182): GPU 없이 실제 H.264 바이트 → I0 TLS → 디코딩(PyAV CPU) → 검출(대역) →
Local Tracker(대역 백엔드) → 카메라 간 연결 → World Track → 접지 → R1 판정까지 한 서비스(Tier0Service)로 돈다.

- 장면: SIM 교실(카메라 4대, 한 사람이 R1 구역으로 걸어 들어가 선다). SIM 2D keypoint 는 검출기 대역의 정답으로만 쓴다.
  영상은 프레임 번호를 화소로 새긴 H.264 이고(사람 이미지 없음, 커밋하지 않음), 검출기 대역은 디코딩한 그림에서 번호를
  읽어 그 번호의 SIM keypoint 를 낸다. 그림 번호와 헤더(촬영 시각·frame_sequence)가 다르면 결합 오류로 센다.
- ultralytics 가 없어서 검출기와 TrackTrack 백엔드는 대역을 주입한다(실제 엔진·추적기 경로는 Pod 확인, -183).
- 결과는 SIM 상한이다. 제품 성능 지표로 쓰지 않는다(AGENTS.md 규칙 7).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import live_video as lv  # noqa: E402
import tier0_site as ts  # noqa: E402
from test_live_input import OneTrackBackend  # noqa: E402
from test_i1_tls import make_cert  # noqa: E402

from services.tier0 import service as service_module  # noqa: E402
from services.tier0 import video_ingest as vi  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.service import Tier0Service, Tier0StartError  # noqa: E402
from services.tier0.settings import load_settings  # noqa: E402
from simai_ai.io import DecoderUnavailable, SimObservationFrameSource  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.risk import SystemClock  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections, PoseDetection  # noqa: E402

TOKEN = "laptop-device-token-182-e2e"
LAG_MS = 30  # 촬영 → 노트북 송신 지연 흉내


def _now() -> int:
    return time.time_ns() // 1_000_000


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("live")
    run = ts.run_sim(tmp, ts.scenario(8_000))
    src = SimObservationFrameSource(run)
    frames: dict[str, list] = {c: [] for c in src.camera_ids}
    for f in src.frames():
        frames[f.camera_id].append(f)
    videos = {c: lv.encode(list(range(len(fs))), gop=10) for c, fs in frames.items()}
    return run, frames, videos


class SimTruthDetector:
    """검출기 대역: 그림 번호 → 그 SIM 프레임의 2D keypoint. 결과 식별자·시각은 넣은 프레임 값 그대로(실제 검출기와 같다)."""

    def __init__(self, frames: dict[str, list], shift_ms: int) -> None:
        self.frames = frames
        self.shift = shift_ms
        self.calls = 0
        self.inferred = 0
        self.misbound = 0
        self.lock = threading.Lock()

    def detect(self, batch):
        out = []
        with self.lock:
            self.calls += 1
        for f in batch:
            sim = self.frames[f.camera_id][lv.read_index(f.image)]
            ok = (sim.frame_ref["frame_sequence"] == f.frame_ref["frame_sequence"]
                  and sim.estimated_capture_time + self.shift == f.estimated_capture_time)
            with self.lock:
                self.inferred += 1
                self.misbound += 0 if ok else 1
            dets = []
            for d in sim.detections or ():
                confs = [k[2] for k in d.keypoints_2d if k is not None and k[2] > 0]
                score = sum(confs) / len(confs) if confs else 0.0
                dets.append(PoseDetection(tuple(d.bbox), min(1.0, score), tuple(d.keypoints_2d)))
            out.append(CameraDetections(f.camera_id, f.frame_ref["stream_epoch"], f.frame_ref["frame_sequence"],
                                        f.estimated_capture_time, f.timestamp_source, f.timestamp_uncertainty_ms,
                                        tuple(dets)))
        return out


def _live_settings(tmp: Path, run: Path, cams, *, decoder: str = "cpu", video_env: bool = True,
                   control_port: int | None = None):
    tmp.mkdir(parents=True, exist_ok=True)
    cert, key = make_cert(tmp)  # 127.0.0.1 SAN 포함(노트북 제어 채널 시험 상대가 서버를 확인한다)
    port = ts.free_port()
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    ts.archive_bundle(tmp / "boot", calibration, "cfg-1", _now() - 1000)
    doc = {
        "gateway_id": ts.GATEWAY,
        "state_dir": str(tmp / "state"),
        "scene_version": "scene-sim",
        "calibration_file": str(run / "calibration.json"),
        "input": {"mode": "live", "camera_ids": list(cams), "decoder": decoder},
        "bootstrap": {"archive": str(tmp / "boot"), "config_version": "cfg-1"},
        "lifecycle": {"clear_hold_ms": 1500, "persistence": [2, 3], "resume_window_ms": 10000},
        "tick_ms": 100,
        "checkpoint_interval_s": 0.5,
    }
    cfg = tmp / "tier0-live.json"
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    env = {"SIMAI_TIER0_TLS_CERT_FILE": str(cert), "SIMAI_TIER0_TLS_KEY_FILE": str(key),
           "SIMAI_LAPTOP_TOKEN_SHA256": vi.token_sha256(TOKEN)}
    if control_port is not None:  # I0 제어 채널(시계 교환·하트비트). 노트북 시험 상대는 ts.LAPTOP_TOKEN 을 쓴다
        env |= {"SIMAI_TIER0_CONTROL_PORT": "70000", "RUNPOD_TCP_PORT_70000": str(control_port),
                "SIMAI_TIER0_CONTROL_LISTEN_HOST": "127.0.0.1",
                "SIMAI_LAPTOP_TOKEN_SHA256": f"{vi.token_sha256(TOKEN)},{vi.token_sha256(ts.LAPTOP_TOKEN)}"}
    if video_env:
        env["SIMAI_TIER0_VIDEO_PORTS"] = ",".join(f"{c}:{71001 + i}" for i, c in enumerate(cams))
        env.update({f"RUNPOD_TCP_PORT_{71001 + i}": str(port) for i in range(len(cams))})
        env["SIMAI_TIER0_VIDEO_LISTEN_HOST"] = "127.0.0.1"
    return load_settings(cfg, env=env), port


def _stream(port: int, cam: str, frames: list, video: lv.Encoded, shift: int, stop_after_ms: int | None,
            stop: threading.Event) -> None:
    sender = lv.VideoSender(port, cam, TOKEN)
    try:
        sender.codec(lv.codec_meta(cam, 1, video))
        start = frames[0].estimated_capture_time
        for f, (au, key) in zip(frames, video.frames, strict=True):
            if stop.is_set() or (stop_after_ms is not None and f.estimated_capture_time - start > stop_after_ms):
                return  # 이 카메라 연결이 끊긴다
            capture = f.estimated_capture_time + shift
            delay = (capture + LAG_MS - _now()) / 1000
            if delay > 0:
                time.sleep(delay)
            sender.frame(lv.frame_meta(cam, 1, f.frame_ref["frame_sequence"], capture, key), au)
    finally:
        sender.close()


def _video_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name.startswith("i0-video") and t.is_alive()]


def _run_live(tmp_path: Path, scene, *, with_control: bool):
    """LIVE 서비스를 띄우고 카메라 4대 H.264 를 실시간 속도로 보낸다. 마지막 카메라는 4.5 s 뒤 끊긴다.
    R1 이 생기고 끊긴 카메라가 NO_SIGNAL 이 된 시점의 (service, R1, health payload, detector) 를 넘기고, 끝나면 정리한다."""
    run, frames, videos = scene
    cams = tuple(frames)
    control_port = ts.free_port() if with_control else None
    settings, port = _live_settings(tmp_path, run, cams, control_port=control_port)
    start = min(fs[0].estimated_capture_time for fs in frames.values())
    anchor = _now() + 1500
    shift = anchor - start
    detector = SimTruthDetector(frames, shift)
    laptop = None
    if with_control:
        laptop = ts.Laptop(control_port, Path(settings.env["SIMAI_TIER0_TLS_CERT_FILE"])).start()
    service = Tier0Service(settings, detector_factory=lambda: detector, tracker_backend_factory=OneTrackBackend)
    service.start()  # LIVE 가 종료 코드 4 없이 기동한다
    stop = threading.Event()
    dropped = cams[-1]
    threads = [threading.Thread(target=_stream, args=(port, c, frames[c], videos[c], shift,
                                                      4500 if c == dropped else None, stop), daemon=True)
               for c in cams]
    try:
        assert "i0_video" in service.events and service.events.index("i0_video") < service.events.index("judge")
        for t in threads:
            t.start()
        assert ts.wait_until(lambda: any(s.rule_id == "R1" for s in service.tracker.active()), timeout=25),             service.status()["live_input"]
        assert detector.inferred > 0 and detector.misbound == 0  # 그림 ↔ 촬영 시각 결합 오류 0
        # 한 카메라 끊김: 그 카메라만 NO_SIGNAL, 나머지는 계속 관측·판정
        assert ts.wait_until(lambda: not service.live_input.video_connected(dropped), timeout=15)
        perceived = service.judge.stats.perceived
        assert ts.wait_until(lambda: service.judge.stats.perceived > perceived + 5, timeout=10)
        payload = service._live_health(service.health.payload(), "test")
        cams_health = {c["id"]: c for c in payload["cameras"]}
        assert cams_health[dropped]["status"] == "NO_SIGNAL"
        for c in cams:
            frames_c = cams_health[c]["frames"]
            assert frames_c["received"] > 0 and frames_c["gateway_to_runpod_loss"] == 0
            assert frames_c["skipped_by_policy"] > 0  # 25 fps 입력, 10 Hz 판정: latest-frame-wins 로 건너뜀
            assert service.clips.ring.camera(c).stats.accepted > 0  # 링 버퍼에도 압축 프레임
        assert [c for c in cams if cams_health[c]["status"] == "OK"] == [c for c in cams if c != dropped]
        message = Envelope(ts.GATEWAY, SystemClock()).make("health", payload)
        assert schema_errors(message, "health") == [], schema_errors(message, "health")
        status = service.status()["live_input"]
        assert status["detector"]["detector_errors"] == 0 and status["detector"]["mismatched"] == 0
        assert all(status["cameras"][c]["decode_failed"] == 0 for c in cams)
        yield service, payload
    finally:
        stop.set()
        assert service.stop(timeout_s=30) is True
        for t in threads:
            t.join(5)
        if laptop is not None:
            laptop.close()
    assert "i0_video" in service.events and not service.live_input.started
    assert _video_threads() == []  # 종료 때 영상 수신 스레드를 모두 기다렸다


def test_live_path_runs_from_h264_bytes_to_r1_judgment(tmp_path, scene) -> None:
    """노트북 제어 채널(시계 교환) 있음: 시계 차이를 알아 R1 이 확인(VERIFIED) 상태로 판정된다."""
    for service, payload in _run_live(tmp_path, scene, with_control=True):
        assert ts.wait_until(lambda: service.clock_sync.capture_clock(True) is not None, timeout=10)
        r1 = [s for s in service.tracker.active() if s.rule_id == "R1"][0]
        assert str(r1.origin) == "LIVE"
        assert ts.wait_until(lambda: str(service.tracker.get(r1.event_id).verification) == "VERIFIED", timeout=10), (
            service.tracker.get(r1.event_id), service.clock_sync.capture_clock(True), service.health.evaluate().report)


def test_live_without_control_channel_treats_clock_as_unknown_not_same(tmp_path, scene) -> None:
    """리뷰 F1: 제어 채널이 없는 LIVE 는 노트북 시계 차이를 모른다. SAME_CLOCK 으로 보지 않고 health UNSAFE_UNKNOWN,
    해소 확인 금지(R1 은 UNVERIFIABLE 로만 난다)."""
    from simai_ai.tracking.decision_time import ClockAwareVerification
    from simai_ai.tracking.system_health import HealthReason

    for service, payload in _run_live(tmp_path, scene, with_control=False):
        assert service.control is None and service.clock_sync is None
        snap = service.health.evaluate()
        assert snap.capture_clock is None and HealthReason.CLOCK_OFFSET_UNKNOWN in snap.report.reasons
        assert payload["system_health"] == "UNSAFE_UNKNOWN" and payload["laptop_clock_offset"] is None
        assert payload["pipeline_lag_ms"] is None
        assert isinstance(service.tracker._verification, ClockAwareVerification)
        r1 = [s for s in service.tracker.active() if s.rule_id == "R1"][0]
        time.sleep(0.5)
        assert str(service.tracker.get(r1.event_id).verification) == "UNVERIFIABLE"  # 확인·자동 해소 없음


def test_live_start_errors_have_clear_exit_codes(tmp_path, scene, monkeypatch) -> None:
    run, frames, _ = scene
    cams = tuple(frames)
    settings, _ = _live_settings(tmp_path / "a", run, cams, video_env=False)
    with pytest.raises(Tier0StartError) as e:
        Tier0Service(settings, detector_factory=lambda: None, tracker_backend_factory=OneTrackBackend).start()
    assert e.value.exit_code == 2 and "SIMAI_TIER0_VIDEO_PORTS" in str(e.value)

    settings, _ = _live_settings(tmp_path / "b", run, cams, decoder="nvdec")

    def no_nvdec(*_a, **_k):
        raise DecoderUnavailable("hwaccel 'cuda' 장치를 열지 못했다")

    monkeypatch.setattr(service_module, "NvdecDecoder", no_nvdec)
    with pytest.raises(Tier0StartError) as e:
        Tier0Service(settings, detector_factory=lambda: None, tracker_backend_factory=OneTrackBackend).start()
    assert e.value.exit_code == 4 and "nvdec" in str(e.value)  # CPU 로 몰래 떨어지지 않는다

    settings, _ = _live_settings(tmp_path / "c", run, cams)
    with pytest.raises(Tier0StartError) as e:
        Tier0Service(settings).start()  # 엔진·가중치 없음
    assert e.value.exit_code == 4 and "SIMAI_TIER0_ENGINE" in str(e.value) and "SIMAI_TIER0_WEIGHTS" in str(e.value)
