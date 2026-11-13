"""Tier 0 자원 격리(S15P31A101-662): cv2 스레드 제한, 검출 고우선순위 CUDA 스트림(선택). CPU 고정은 tests/deploy 에서 본다.

노트북·CI 에는 cv2·torch·ultralytics 가 없어 가짜 모듈로 부르는 순서만 본다(실제 GPU 효과는 GPU 세션 대조, 설계 문서 5절).
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import tier0_site as ts  # noqa: E402

from services.tier0.settings import Tier0ConfigError, load_settings  # noqa: E402
from services.tier0.yolo_detector import UltralyticsPoseDetector  # noqa: E402
from simai_ai.gpu.cv_threads import limit_cv2_threads  # noqa: E402
from simai_ai.providers.base import Frame  # noqa: E402


def test_cv2_threads_default_one_keep_and_bad_value(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(setNumThreads=calls.append))
    assert limit_cv2_threads({}) == 1 and calls == [1]
    assert limit_cv2_threads({"SIMAI_CV2_THREADS": "keep"}) is None and calls == [1]  # 되돌리기: cv2 기본값 그대로
    assert limit_cv2_threads({"SIMAI_CV2_THREADS": "2"}) == 2 and calls == [1, 2]
    with pytest.raises(ValueError):
        limit_cv2_threads({"SIMAI_CV2_THREADS": "many"})
    monkeypatch.setitem(sys.modules, "cv2", None)  # cv2 없음(노트북·CI): 건너뛴다
    assert limit_cv2_threads({}) is None


def _fake_stack(monkeypatch, *, cuda: bool = True) -> dict:
    state: dict = {"in_stream": False, "predict_in_stream": [], "priorities": []}

    class Stream:
        def __init__(self, priority: int = 0) -> None:
            state["priorities"].append(priority)

        @staticmethod
        def priority_range() -> tuple[int, int]:
            return (0, -5)

    @contextlib.contextmanager
    def stream(_s):
        state["in_stream"] = True
        try:
            yield
        finally:
            state["in_stream"] = False

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: cuda, Stream=Stream, stream=stream)
    monkeypatch.setitem(sys.modules, "torch", torch)

    class YOLO:
        def __init__(self, path, task):
            self.path = path

        def predict(self, images, **kwargs):
            state["predict_in_stream"].append(state["in_stream"])
            return [types.SimpleNamespace(boxes=None, keypoints=None) for _ in images]

    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = YOLO
    utils = types.ModuleType("ultralytics.utils")
    utils.LOGGER = types.SimpleNamespace(filters=[], addFilter=lambda f: None)
    monkeypatch.setitem(sys.modules, "ultralytics", ultralytics)
    monkeypatch.setitem(sys.modules, "ultralytics.utils", utils)
    return state


def _frame() -> Frame:
    return Frame("cam1", 1000, "CAMERA_STREAM", {"stream_epoch": 1, "frame_sequence": 2}, image=object())


def test_high_priority_stream_wraps_detection_when_enabled(monkeypatch) -> None:
    state = _fake_stack(monkeypatch)
    detector = UltralyticsPoseDetector("x.engine", high_priority_stream=True)
    for _ in range(2):
        out = detector.detect([_frame()])
        assert out[0].camera_id == "cam1" and out[0].detections == ()
    assert state["predict_in_stream"] == [True, True]
    assert state["priorities"] == [-5] and detector.stream_priority == -5  # 스트림은 한 번만 만든다


def test_default_and_no_cuda_use_the_current_stream(monkeypatch) -> None:
    state = _fake_stack(monkeypatch)
    UltralyticsPoseDetector("x.engine").detect([_frame()])  # 기본 normal
    assert state["predict_in_stream"] == [False] and state["priorities"] == []
    state = _fake_stack(monkeypatch, cuda=False)
    UltralyticsPoseDetector("x.engine", high_priority_stream=True).detect([_frame()])
    assert state["predict_in_stream"] == [False] and state["priorities"] == []


def test_cuda_stream_priority_setting(tmp_path) -> None:
    (tmp_path / "sim").mkdir()
    run = ts.run_sim(tmp_path / "sim", ts.scenario(2_000))
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    ts.archive_bundle(tmp_path / "boot", calibration, "cfg-1", time.time_ns() // 1_000_000 - 1000)
    cfg = ts.write_config(tmp_path / "tier0.json", run_dir=run, state_dir=tmp_path / "state",
                          anchor_wall_ms=time.time_ns() // 1_000_000, archive=tmp_path / "boot", version="cfg-1")
    assert load_settings(cfg, env={}).cuda_stream_priority == "normal"
    assert load_settings(cfg, env={"SIMAI_TIER0_CUDA_STREAM_PRIORITY": "HIGH"}).cuda_stream_priority == "high"
    with pytest.raises(Tier0ConfigError):
        load_settings(cfg, env={"SIMAI_TIER0_CUDA_STREAM_PRIORITY": "urgent"})


def test_i1_health_and_calibration_messages_do_not_block_on_held_judge_lock_while_reporting_true_stall(
    tmp_path, monkeypatch
) -> None:
    """-672: 정밀 맵 굽기 등 CPU 경합으로 판정 스레드가 judge_lock 을 길게 쥐고 있어도 I1 health·보정 이력 조회는
    블로킹되지 않고 즉시 나가며, 실제로 판정 진행(t_latest_global)이 멈추면 UNSAFE_UNKNOWN 을 숨기지 않고 보고한다."""
    import threading
    from services.tier0.service import Tier0Service
    from simai_ai.providers.contract import schema_errors
    from simai_ai.tracking.system_health import HealthReason

    (tmp_path / "sim").mkdir()
    run = ts.run_sim(tmp_path / "sim", ts.scenario(10_000))
    calibration = json.loads((run / "calibration.json").read_text(encoding="utf-8"))
    now_ms = [1_700_000_000_000]
    mono_ns = [1_000_000_000_000]
    ts.archive_bundle(tmp_path / "boot", calibration, "cfg-1", now_ms[0] - 1000)
    cfg = ts.write_config(
        tmp_path / "tier0.json",
        run_dir=run,
        state_dir=tmp_path / "state",
        anchor_wall_ms=now_ms[0],
        archive=tmp_path / "boot",
        version="cfg-1",
    )
    settings = load_settings(cfg, env={})
    service = Tier0Service(settings, mono_ns=lambda: mono_ns[0], wall_ms=lambda: now_ms[0])
    monkeypatch.setattr(service, "_spawn", lambda name, target: None)
    monkeypatch.setattr(service, "_ec2_ok", lambda: True)
    monkeypatch.setattr(service, "_gateway_ok", lambda: True)
    service.start()
    try:
        service.link = types.SimpleNamespace(
            client=types.SimpleNamespace(connected=True, timing=types.SimpleNamespace(summary=lambda: None)),
            publisher=types.SimpleNamespace(
                envelope=types.SimpleNamespace(
                    make=lambda msg_type, payload, msg_id=None: {
                        "msg_type": msg_type,
                        "payload": payload,
                        "msg_id": msg_id,
                    }
                ),
                world_state_drops=lambda: None,
            ),
        )

        # 시계 동기화가 확보된 상태로 두고 초기 프레임 처리로 복구 게이트 해제 및 정상 진행 상태 확보
        from simai_ai.tracking.time_model import ClockOffset

        service.health._capture_clock = lambda gw_ok: ClockOffset(0, 1.0) if gw_ok else None
        service.calibration.ensure_scene_snapshot(
            effective_at=now_ms[0], msg_id="00000000-0000-4000-8000-000000000671"
        )
        for _ in range(3):
            now_ms[0] += 100
            mono_ns[0] += 100_000_000
            service.judge.tick()
            service.health.evaluate()
        initial_snap = service.health.evaluate()
        assert HealthReason.LAG_UNSAFE not in initial_snap.report.reasons
        assert HealthReason.RECOVERING not in initial_snap.report.reasons
        assert str(initial_snap.report.system_health) in ("NOMINAL", "DEGRADED")

        # 초기 보정 스냅샷 이력을 먼저 비운 뒤, 이후 반복 호출이 lock 없이 빈 배열을 돌려주는지 확인
        initial_cal_msgs = service._calibration_messages()
        assert len(initial_cal_msgs) == 1

        locked = threading.Event()
        release = threading.Event()

        def hold_judge_lock() -> None:
            with service.judge_lock:
                locked.set()
                release.wait(10.0)

        holder = threading.Thread(target=hold_judge_lock, daemon=True)
        holder.start()
        assert locked.wait(2.0)
        try:
            t0 = time.perf_counter()
            payload = service.health.payload()
            site_fields = service._site_fields()
            re_setup = service.re_setup_field()
            cal_msgs = service._calibration_messages()
            elapsed_s = time.perf_counter() - t0

            # judge_lock 이 다른 스레드에 잡혀 있어도 50ms 안에 즉시 반환되어야 한다
            assert elapsed_s < 0.05, f"health/calibration 경로가 judge_lock 에 블로킹됨: {elapsed_s:.3f}s"
            assert payload["system_health"] in ("NOMINAL", "DEGRADED")
            assert "active_site_alerts" in site_fields and "active_site_alerts_as_of" in site_fields
            assert re_setup == []
            assert cal_msgs == []
            envelope_msg = {
                "schema_version": "2.3.0",
                "msg_type": "health",
                "msg_id": "00000000-0000-4000-8000-000000000672",
                "source_id": settings.gateway_id,
                "sent_at": now_ms[0],
                "payload": {**payload, **site_fields},
            }
            assert schema_errors(envelope_msg, "health") == []

            # 그러나 실제로 판정 루프 진행(t_latest_global)이 lag_unsafe_ms 를 넘겨 멈추면 진짜 정체를 숨기지 않고 UNSAFE_UNKNOWN 으로 낸다
            now_ms[0] += 3_000
            mono_ns[0] += 3_000_000_000
            stalled_snap = service.health.evaluate()
            stalled_payload = service.health.payload()
            assert HealthReason.LAG_UNSAFE in stalled_snap.report.reasons
            assert stalled_payload["system_health"] == "UNSAFE_UNKNOWN"
        finally:
            release.set()
            holder.join(2.0)
    finally:
        service.link = None
        service.stop(timeout_s=5.0)

