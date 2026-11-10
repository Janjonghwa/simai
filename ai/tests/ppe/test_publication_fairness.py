"""게시 후 150ms·카메라 순환 기회·촬영 시각 6칸 상태를 합성 시계로 확인한다."""

from collections import Counter
import json

import numpy as np
import pytest

from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter
from simai_ai.providers.base import Frame

from .test_service_loop import make_loop, shared

CAMERAS = ("cam1", "cam2", "cam3", "cam4")
START = 1_760_342_400_000


def run_budgeted(*, rotating=True, frames=16, cost_ms=160):
    loop, source, gate, detector, out, clock = make_loop()
    seen = []
    infer = detector.infer
    def slow_infer(image):
        seen.append((loop._last_camera, clock.t))
        result = infer(image)
        clock.t += cost_ms
        return result
    detector.infer = slow_infer
    if not rotating:
        loop._camera_order = lambda images: sorted(images)  # 수정 전 고정 순서 비교군.
    for i in range(frames):
        clock.t = START + i * 500 + 30
        source.value = shared(clock.t - 30, dict.fromkeys(CAMERAS, i + 1), cams=CAMERAS,
                              produced=clock.t - 400)
        source.value[0]["published_at_ms"] = clock.t
        loop.step()
    return loop, source, gate, detector, out, clock, seen


def test_single_inference_budget_is_shared_and_still_builds_six_bin_evidence():
    loop, _, _, _, out, _, seen = run_budgeted()
    fixed = run_budgeted(rotating=False)
    assert Counter(c for c, _ in seen) == dict.fromkeys(CAMERAS, 4)
    assert Counter(c for c, _ in fixed[-1]) == {"cam1": 16}
    assert [c for c, _ in seen] == list(CAMERAS) * 4
    assert loop.counts["inferred"] == fixed[0].counts["inferred"] == 16
    for camera in CAMERAS:
        times = [t for c, t in seen if c == camera]
        assert all(b - a >= 500 for a, b in zip(times, times[1:]))
    last = out.messages[-1]["payload"]["people"][0]
    assert last["helmet"]["state"] == "WORN" and last["vest"]["state"] == "NOT_WORN"
    assert loop.judge.latest_capture_time_ms == START + 15 * 500
    assert not loop.judge.dropped


def test_two_inferences_per_budget_keep_opportunities_balanced():
    loop, _, _, _, _, _, seen = run_budgeted(cost_ms=90)
    assert [c for c, _ in seen[:8]] == list(CAMERAS) * 2
    assert Counter(c for c, _ in seen) == dict.fromkeys(CAMERAS, 8)
    assert loop.counts["inferred"] == 32


def test_camera_removal_insertion_and_unprocessed_snapshot_do_not_reset_cursor():
    loop, source, gate, _, _, clock, _ = run_budgeted(frames=1)
    assert loop._last_camera == "cam1"
    assert loop._camera_order({c: None for c in ("cam0", "cam2", "cam4")}) == ["cam2", "cam4", "cam0"]
    loop._last_camera = "cam3"
    assert loop._camera_order({c: None for c in ("cam1", "cam2")}) == ["cam1", "cam2"]
    before = loop._last_camera
    clock.t += 1000  # 같은 입력의 이미 본/만료 카메라는 처리하지 않는다.
    loop.step()
    assert loop._last_camera == before
    gate.admission.admit = False
    source.value = shared(clock.t, dict.fromkeys(CAMERAS, 2), cams=CAMERAS, produced=clock.t)
    loop.step()
    assert loop._last_camera == before


@pytest.mark.parametrize("elapsed,expected", [(150, 1), (151, 0)])
def test_publication_deadline_is_rechecked_after_gate_cost(elapsed, expected):
    loop, source, gate, detector, _, clock = make_loop()
    clock.t = 1000
    source.value = shared(900, {"cam1": 1}, produced=500)
    source.value[0]["published_at_ms"] = 1000
    calls = []
    check = gate.check
    def slow_gate():
        calls.append(True)
        if len(calls) == 2:
            clock.t = 1000 + elapsed
        return check()
    gate.check = slow_gate
    loop.step()
    assert len(detector.calls) == expected
    assert bool(loop._last_frame) is bool(expected)


def test_new_publication_does_not_bypass_gate_or_legacy_wait_limit():
    loop, source, gate, detector, _, clock = make_loop()
    clock.t = 1000
    source.value = shared(900, {"cam1": 1}, produced=500)
    loop.step()  # legacy는 150ms 초과인 produced 시각을 계속 쓴다.
    assert not detector.calls
    source.value[0]["published_at_ms"] = clock.t
    gate.admission.admit = False
    loop.step()
    assert not detector.calls
    gate.admission.admit = True
    loop.step()
    assert len(detector.calls) == 1


@pytest.mark.parametrize("stamp", [None, True, "1000", 1000.5, -1, 1001])
def test_bad_or_future_publication_never_consumes_camera(stamp):
    loop, source, _, detector, _, clock = make_loop()
    clock.t = 1000
    source.value = shared(900, {"cam1": 1}, produced=990)
    source.value[0]["published_at_ms"] = stamp
    loop.step()
    assert not detector.calls and not loop._last_frame and loop._last_camera is None
    assert loop.counts["publication_invalid"] == 1


def test_cursor_moves_after_detector_exception_but_not_after_rate_rejection():
    loop, source, _, detector, _, clock = make_loop()
    clock.t = 1030
    source.value = shared(1000, dict.fromkeys(CAMERAS, 1), cams=CAMERAS, produced=1030)
    infer = detector.infer
    def fail(_):
        raise RuntimeError("가짜 검출 실패")
    detector.infer = fail
    with pytest.raises(RuntimeError):
        loop.step()
    assert loop._last_camera == "cam1" and set(loop._last_frame) == {"cam1"}
    # 다음 입력도 모두 처리율 제한이면 순서를 소비하지 않는다.
    detector.infer = infer
    loop._last_infer_at.update(dict.fromkeys(CAMERAS, 1030))
    source.value = shared(1000, dict.fromkeys(CAMERAS, 2), cams=CAMERAS, produced=1030)
    loop.step()
    assert loop._last_camera == "cam1"
    seen = []
    def slow(image):
        seen.append(loop._last_camera)
        result = infer(image)
        clock.t += 160
        return result
    detector.infer = slow
    clock.t = 1530
    source.value = shared(1500, dict.fromkeys(CAMERAS, 3), cams=CAMERAS, produced=1530)
    loop.step()
    assert seen == ["cam2"] and loop._last_camera == "cam2"


def test_real_slow_writer_uses_publication_not_offer_time(tmp_path, monkeypatch):
    loop, _, _, detector, _, clock = make_loop()
    clock.t = 1000
    path = tmp_path / "input.npz"
    writer = LatestChannelWriter(path, clock_ms=clock)
    doc, images = shared(900, {"cam1": 1}, produced=1000)
    metadata = {k: v for k, v in doc.items() if k not in ("format", "frames")}
    frame = Frame("cam1", 900, "FILE", doc["frames"]["cam1"]["frame_ref"], image=images["cam1"])
    real_savez = np.savez
    def slow_write(*args, **kw):
        clock.t += 400
        real_savez(*args, **kw)
    monkeypatch.setattr(np, "savez", slow_write)
    try:
        writer._write(metadata, {"cam1": frame})
        loop.source = LatestChannelReader(path, require_publication=True)
        returned, _ = loop.source.latest()
        assert returned["produced_at_ms"] == 1000 and returned["published_at_ms"] == 1400
        loop.step()
        assert len(detector.calls) == 1  # 쓰기 400ms가 새 대기 예산을 미리 소비하지 않는다.
        assert loop._last_frame["cam1"] == (3, 1)
        assert loop.judge.latest_capture_time_ms == 900  # 촬영 근거는 새 시각으로 바꾸지 않는다.
        clock.t = 1551
        # 같은 파일에서 아직 보지 않은 카메라처럼 만들어도 오래된 표식은 재채택하지 않는다.
        loop._last_frame.clear()
        loop._last_infer_at.clear()
        loop._last_bin.clear()
        loop.step()
        assert len(detector.calls) == 1 and loop.counts["metadata_wait_exceeded"] == 1
    finally:
        writer.close()


@pytest.mark.parametrize("check_at", ["metadata", "image"])
@pytest.mark.parametrize("age,expected", [(150, 1), (151, 0)])
def test_real_reader_publication_deadline_before_and_after_images(tmp_path, monkeypatch, check_at, age, expected):
    loop, _, _, detector, _, clock = make_loop()
    clock.t = 1000
    path = tmp_path / "input.npz"
    writer = LatestChannelWriter(path, clock_ms=clock)
    doc, images = shared(900, {"cam1": 1}, produced=500)
    frame = Frame("cam1", 900, "FILE", doc["frames"]["cam1"]["frame_ref"], image=images["cam1"])
    try:
        writer._write({"world": doc["world"], "produced_at_ms": 500}, {"cam1": frame})
    finally:
        writer.close()
    accessed = []
    getitem = np.lib.npyio.NpzFile.__getitem__
    def read(z, key):
        accessed.append(key)
        value = getitem(z, key)
        if check_at == "image" and key.startswith("image"):
            clock.t = 1000 + age
        return value
    monkeypatch.setattr(np.lib.npyio.NpzFile, "__getitem__", read)
    clock.t = 1000 + age if check_at == "metadata" else 1000
    loop.source = LatestChannelReader(path, require_publication=True)
    loop.step()
    assert len(detector.calls) == expected
    assert bool(loop._last_frame) is bool(expected)
    if check_at == "metadata" and not expected:
        assert accessed == ["metadata"]
        assert loop.counts["metadata_wait_exceeded"] == 1
    elif not expected:
        assert accessed == ["metadata", "image_0"]
        assert loop.counts["wait_exceeded"] == 1


def test_clock_reversal_after_gate_rejects_publication_without_consuming_frame():
    loop, source, gate, detector, _, clock = make_loop()
    clock.t = 1000
    source.value = shared(900, {"cam1": 1}, produced=500)
    source.value[0]["published_at_ms"] = 1000
    check = gate.check
    calls = []
    def reverse():
        calls.append(True)
        if len(calls) == 2:
            clock.t = 999
        return check()
    gate.check = reverse
    loop.step()
    assert not detector.calls and not loop._last_frame
    assert loop.counts["publication_invalid"] == 1


def test_unavailable_publication_still_expires_previous_evidence(tmp_path):
    loop, _, _, _, out, clock, _ = run_budgeted()
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "WORN"
    path = tmp_path / "unready.npz"
    doc = {"format": "simai.avatar-input@1", "publication_id": "not_committed", "frames": {}}
    np.savez(path, metadata=np.frombuffer(json.dumps(doc).encode(), dtype=np.uint8))
    loop.source = LatestChannelReader(path, require_publication=True)
    clock.t += 11_000
    loop.step()
    assert loop.counts["no_input"] == 1
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "UNKNOWN"


def test_legacy_embedded_publication_time_cannot_refresh_old_input(tmp_path):
    loop, _, _, detector, _, clock = make_loop()
    clock.t = 1000
    doc, images = shared(900, {"cam1": 1}, produced=0)
    doc["published_at_ms"] = 1000  # 구 writer에 이 metadata를 넣어도 원래 offer 기준을 우회하면 안 된다.
    path = tmp_path / "legacy.npz"
    np.savez(path, metadata=np.frombuffer(json.dumps(doc).encode(), dtype=np.uint8),
             **{doc["frames"][c]["array"]: image for c, image in images.items()})
    loop.source = LatestChannelReader(path, require_publication=True)
    loop.step()
    assert not detector.calls and not loop._last_frame and loop._last_camera is None
    assert loop.counts["metadata_wait_exceeded"] == 1
