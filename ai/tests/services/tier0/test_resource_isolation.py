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
