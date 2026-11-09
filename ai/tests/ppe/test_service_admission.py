"""PPE-03: 한 묶음에서도 새 검출마다 Tier 0 접수 신호를 확인한다(GPU 없이 실행)."""

import json
import os
from types import SimpleNamespace

import pytest

from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy, LoadWindow, marker_section

from .test_service_loop import make_loop, run, shared

CAMERAS = ("cam1", "cam2", "cam3", "cam4")
START = 1_760_342_400_000


@pytest.mark.parametrize("completed", [1, 3])
@pytest.mark.parametrize("live,code", [
    (True, "over_budget"), (True, "lag_unknown"), (True, "marker_absent"), (False, "not_live"),
])
def test_admission_change_stops_remaining_cameras_without_consuming_their_frames(completed, live, code):
    loop, source, gate, detector, out, clock = make_loop()
    source.value = shared(START, dict.fromkeys(CAMERAS, 1), cams=CAMERAS)
    clock.t = START + 30
    infer = detector.infer

    def close_after_inference(image):
        candidates = infer(image)
        if len(detector.calls) == completed:
            # NOT_LIVE는 AdmissionGate에서 admit=True여도 PPE는 실행하면 안 된다.
            gate.admission = SimpleNamespace(live=live, admit=not live, code=code)
        return candidates

    detector.infer = close_after_inference
    loop.step()
    assert len(detector.calls) == completed
    assert bool(out.messages) is live
    for records in (loop._last_frame, loop._last_bin, loop._last_infer_at):
        assert set(records) == set(CAMERAS[:completed])
    assert loop.counts["gate_" + code if live else "not_live"] == 1

    # 같은 입력을 다시 읽는다. 이미 추론한 카메라는 500ms 제한, 나머지는 즉시 처리 가능해야 한다.
    gate.admission = SimpleNamespace(live=True, admit=True, code="OK")
    clock.t += 50
    loop.step()
    assert len(detector.calls) == len(CAMERAS)
    assert set(loop._last_frame) == set(CAMERAS)


@pytest.mark.parametrize("live", [True, False])
def test_admission_is_rechecked_after_observations_before_first_inference(monkeypatch, live):
    from services.ppe.loop import frame_observations

    loop, source, gate, detector, out, clock = make_loop()
    source.value = shared(START, dict.fromkeys(CAMERAS, 1), cams=CAMERAS)
    clock.t = START + 30

    def close_after_observations(*args):
        observations = frame_observations(*args)
        gate.admission = SimpleNamespace(live=live, admit=not live, code="over_budget" if live else "not_live")
        return observations

    monkeypatch.setattr("services.ppe.loop.frame_observations", close_after_observations)
    loop.step()
    assert detector.calls == []
    assert bool(out.messages) is live
    assert not loop._last_frame and not loop._last_bin and not loop._last_infer_at


def test_tier0_stopping_during_last_inference_suppresses_publication():
    loop, source, gate, detector, out, clock = make_loop()
    source.value = shared(START, {"cam1": 1})
    clock.t = START + 30
    infer = detector.infer

    def stop_tier0(image):
        candidates = infer(image)
        gate.admission = SimpleNamespace(live=False, admit=True, code="not_live")
        return candidates

    detector.infer = stop_tier0
    loop.step()
    assert len(detector.calls) == 1
    assert not out.messages and loop.state_sequence == 0
    assert loop.counts["not_live"] == 1


@pytest.mark.parametrize("gate_delay_ms,expected_inferences", [(140, 1), (141, 0)])
def test_frame_wait_limit_includes_the_final_admission_check(gate_delay_ms, expected_inferences):
    loop, source, gate, detector, _out, clock = make_loop()
    source.value = shared(START, {"cam1": 1})  # produced=START+20
    clock.t = START + 30
    checks = []

    def slow_check():
        checks.append(clock.t)
        if len(checks) == 2:  # step 시작 검사 뒤, 검출 직전 재확인에 시간이 걸린다.
            clock.t += gate_delay_ms
        return gate.admission

    gate.check = slow_check
    loop.step()
    assert len(detector.calls) == expected_inferences  # 150ms까지 허용, 151ms는 버림
    assert loop.counts["wait_exceeded"] == 1 - expected_inferences
    assert bool(loop._last_frame) is bool(expected_inferences)
    assert bool(loop._last_bin) is bool(expected_inferences)
    assert bool(loop._last_infer_at) is bool(expected_inferences)


def test_mid_batch_load_block_keeps_reporting_and_evidence_expires():
    loop, source, gate, detector, out, clock = make_loop()
    run(loop, source, clock, START, 20, cams=CAMERAS)
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "WORN"
    completed = len(detector.calls)
    infer = detector.infer

    def block_tier0(image):
        candidates = infer(image)
        gate.admission = SimpleNamespace(live=True, admit=False, code="over_budget")
        return candidates

    detector.infer = block_tier0
    before = len(out.messages)
    run(loop, source, clock, START + 5000, 60, cams=CAMERAS)
    assert len(detector.calls) == completed + 1
    assert len(out.messages) > before
    assert out.messages[-1]["payload"]["people"][0]["helmet"]["state"] == "UNKNOWN"


@pytest.mark.parametrize("failure", ["over_budget", "lag_unknown", "marker_absent", "marker_unreadable", "marker_stale"])
def test_real_admission_marker_blocks_between_cameras_and_requires_ten_stable_seconds(tmp_path, failure):
    loop, source, _gate, detector, out, clock = make_loop()
    marker = tmp_path / "tier0.live"
    gate = AdmissionGate(marker, AdmissionPolicy(), wall_s=lambda: clock.t / 1000,
                         monotonic=lambda: (clock.t - START) / 1000)
    loop.gate = gate

    def write_marker(*, lag=120, unknown=False):
        load = LoadWindow()
        load.add(None if unknown else lag)
        doc = {"started": True, "gpu_priority": marker_section(
            phase="running", reserve={"state": "OK"}, load=load.take(), written_at_ms=clock.t, pid=1)}
        marker.write_text(json.dumps(doc), encoding="utf-8")
        os.utime(marker, (clock.t / 1000, clock.t / 1000))

    def tick(elapsed):
        clock.t = START + elapsed
        source.value = shared(clock.t - 30, dict.fromkeys(CAMERAS, elapsed + 1), cams=CAMERAS)

    tick(0)
    write_marker()
    loop.step()  # 정상 표본이어도 10초 안정 대기
    assert not detector.calls
    infer = detector.infer

    def fail_after_first(image):
        candidates = infer(image)
        if len(detector.calls) == 1:
            if failure == "marker_absent":
                marker.unlink()
            elif failure == "marker_unreadable":
                marker.write_text("{broken", encoding="utf-8")
                os.utime(marker, (clock.t / 1000, clock.t / 1000))
            elif failure == "marker_stale":
                os.utime(marker, (clock.t / 1000 - 16, clock.t / 1000 - 16))
            else:
                write_marker(lag=601 if failure == "over_budget" else 120, unknown=failure == "lag_unknown")
        return candidates

    detector.infer = fail_after_first
    tick(10_000)
    write_marker()
    loop.step()
    assert len(detector.calls) == 1
    assert loop.counts["gate_" + failure] == 1
    assert out.messages  # LIVE 부하 차단은 상태 보고를 막지 않는다.

    for elapsed in (10_500, 20_499):
        tick(elapsed)
        write_marker()
        loop.step()
        assert len(detector.calls) == 1  # 회복 첫 정상 검사로부터 10초 미만
    tick(20_500)
    write_marker()
    loop.step()
    assert len(detector.calls) == 5  # 정확히 10초 정상 뒤 새 묶음 4대 모두 처리
    assert loop.counts["gate_stabilizing"] == 3  # 기동 1회 + 회복 중 2회
