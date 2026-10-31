"""vlm_rationale 처리기 (S15P31A101-25, S15P31A101-273): 가짜 저장소·pose·VLM 으로 계약 예시 입력 → 계약에 맞는 결과, 상향만, VlmFrame 만."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import numpy as np
import pytest

import services.worker.__main__ as entry
from services.worker.handlers import JobContext, JobFailed
from services.worker.vlm_rationale import VlmRationaleHandler
from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmClient, VlmResult, VlmSettings, VlmTimeout, VlmUnavailable
from simai_ai.vlm.face_blur import Person, VlmFrame
from simai_ai.vlm.rationale import (
    PROMPT,
    PROPOSAL_DISCARD_REASON,
    RESPONSE_FORMAT,
    SEVERITIES,
    SEVERITY_RANK,
    build_prompt,
    check_proposal,
    parse_answer,
    upward_only,
)

EXAMPLES = Path(__file__).resolve().parents[4] / "contracts" / "examples"
REQUEST = json.loads((EXAMPLES / "job_request.vlm_rationale.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((EXAMPLES / "job_callback.vlm_rationale.json").read_text(encoding="utf-8"))["payload"]["result"]
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/vlm_rationale.schema.json#/$defs/result"}
FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))


def _input(**changes):
    inp = copy.deepcopy(REQUEST["payload"]["input"])
    inp.update(changes)
    return inp


def _ctx(inp) -> JobContext:
    return JobContext(job_id="j", lease_id="l", job_type="vlm_rationale", attempt=1, input=inp)


class Store:
    def get(self, ctx, uri):
        return b"synthetic:" + uri.encode()

    def put(self, ctx, name, content, content_type):  # pragma: no cover
        raise AssertionError("처리한 이미지는 올리지 않는다")


class Provider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def people(self, image):
        if self.error:
            raise self.error
        return [Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)]


def _decode(data: bytes) -> np.ndarray:
    return np.full((320, 240, 3), 200, np.uint8)


class FakeVlm:
    """VlmClient.complete 꼴. 받은 이미지·질문을 남기고 answers 를 차례로 돌려준다(예외면 던진다)."""

    def __init__(self, *answers, latency_ms: int = 800) -> None:
        self.answers, self.latency_ms, self.calls = list(answers), latency_ms, []

    def complete(self, prompt, frames, *, system=None, response_format=None):
        frames = tuple(frames)
        assert all(isinstance(f, VlmFrame) for f in frames)
        self.calls.append({"prompt": prompt, "frames": frames, "system": system, "response_format": response_format})
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        finish = "stop"
        if isinstance(a, tuple):
            a, finish = a
        content = a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
        return VlmResult(content=content, model="cosmos3-nano", latency_ms=self.latency_ms, finish_reason=finish)


def _answer(proposed="CRITICAL", text="작업자가 단상 가장자리 쪽으로 몸을 기울이고 있고 난간이 보이지 않는다."):
    return {"rationale": text, "proposed_severity": proposed}


def _run(inp, vlm, provider=None, clock=None):
    handler = VlmRationaleHandler(Store(), provider or Provider(), client_factory=lambda: vlm, decode=_decode,
                                  monotonic=clock or (lambda: 0.0))
    return handler(_ctx(inp))


def test_contract_example_gives_contract_result():
    vlm = FakeVlm(_answer())
    result = _run(_input(), vlm)
    assert schema_errors(result, RESULT_SCHEMA) == []
    keys = ("event_id", "event_sequence", "severity", "rationale", "proposed_severity")
    assert {k: result[k] for k in keys} == {k: EXPECTED[k] for k in keys}
    assert result["clause_ids"] == [] and result["model_version"] == "cosmos3-nano"
    call = vlm.calls[0]
    assert call["response_format"] == RESPONSE_FORMAT and "등급을 내리거나" in call["system"]
    # 촬영 시각 순: 직전 프레임(-3.1초) → 이벤트 시점 프레임
    assert [f.source for f in call["frames"]] == ["s3://simai-events/0192a6f0/cam1-10249-blur.jpg",
                                                  "s3://simai-events/0192a6f0/cam1-10324-blur.jpg"]
    assert "-3.1초" in call["prompt"] and "+0.0초" in call["prompt"] and "R2(개구부·가장자리 근접)" in call["prompt"]
    assert "edge_distance_m=1.4" in call["prompt"] and "머리 높이 1.68 m" in call["prompt"]


def test_frames_reaching_vlm_are_face_processed():
    vlm = FakeVlm(_answer())
    _run(_input(), vlm)
    for f in vlm.calls[0]["frames"]:
        assert f.face_policy == "BLURRED" and f.record.regions == 1
        assert not f.pixels.flags.writeable


@pytest.mark.parametrize(
    "severity,proposed",
    [
        ("INFO", "CAUTION"),
        ("INFO", "HIGH"),
        ("INFO", "CRITICAL"),
        ("CAUTION", "HIGH"),
        ("CAUTION", "CRITICAL"),
        ("HIGH", "CRITICAL"),
    ],
)
def test_strictly_upward_opinion_is_kept_without_discard_log(severity, proposed, caplog):
    with caplog.at_level(logging.WARNING, logger="services.worker"):
        result = _run(_input(severity=severity), FakeVlm(_answer(proposed)))
    assert result["proposed_severity"] == proposed and result["severity"] == severity
    assert PROPOSAL_DISCARD_REASON not in caplog.text


@pytest.mark.parametrize("proposed", ["HIGH", "CAUTION", "INFO"])
def test_same_or_lower_opinion_becomes_null_and_logs_reason_code(proposed, caplog):
    inp = _input(severity="HIGH")
    with caplog.at_level(logging.WARNING, logger="services.worker"):
        result = _run(inp, FakeVlm(_answer(proposed)))
    assert result["proposed_severity"] is None and result["severity"] == "HIGH"
    assert f"reason={PROPOSAL_DISCARD_REASON}" in caplog.text
    assert inp["event_id"] in caplog.text and f"proposed_severity={proposed}" in caplog.text


@pytest.mark.parametrize("proposed", ["CRITICAL", "HIGH", "CAUTION", "INFO", None])
def test_critical_event_never_gets_opinion(proposed, caplog):
    with caplog.at_level(logging.WARNING, logger="services.worker"):
        result = _run(_input(severity="CRITICAL"), FakeVlm(_answer(proposed)))
    assert result["proposed_severity"] is None and result["severity"] == "CRITICAL"
    if proposed is None:
        assert PROPOSAL_DISCARD_REASON not in caplog.text
    else:
        assert f"reason={PROPOSAL_DISCARD_REASON}" in caplog.text


def test_frames_after_observation_time_are_dropped():
    inp = _input()
    later = dict(inp["frames"][0], frame_uri="s3://simai-events/0192a6f0/cam1-10400-blur.jpg",
                 estimated_capture_time_ms=inp["observation_time"] + 2000)
    inp["frames"].append(later)
    vlm = FakeVlm(_answer())
    _run(inp, vlm)
    assert later["frame_uri"] not in [f.source for f in vlm.calls[0]["frames"]]
    assert len(vlm.calls[0]["frames"]) == 2


def test_only_later_frames_is_input_invalid():
    inp = _input()
    for f in inp["frames"]:
        f["estimated_capture_time_ms"] = inp["observation_time"] + 1
    with pytest.raises(JobFailed) as e:
        _run(inp, FakeVlm())
    assert e.value.code == "VLM_INPUT_INVALID" and not e.value.retryable


def test_no_face_safe_frames_fails_without_calling_vlm():
    vlm = FakeVlm()
    with pytest.raises(JobFailed) as e:
        _run(_input(), vlm, provider=Provider(RuntimeError("CUDA OOM")))
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and vlm.calls == []


def test_bad_json_is_asked_again_once_then_fails():
    vlm = FakeVlm("난간이 없다", "{\"rationale\": \"\"}")
    with pytest.raises(JobFailed) as e:
        _run(_input(), vlm)
    assert e.value.code == "VLM_ANSWER_INVALID" and not e.value.retryable
    assert len(vlm.calls) == 2 and "형식에 맞지 않았다" in vlm.calls[1]["prompt"]


def test_bad_json_then_good_answer_succeeds():
    result = _run(_input(severity="HIGH"), FakeVlm("not json", _answer()))
    assert result["proposed_severity"] == "CRITICAL"


def test_truncated_answer_is_not_used():
    vlm = FakeVlm((_answer(), "length"), _answer("HIGH"))
    result = _run(_input(), vlm)
    assert len(vlm.calls) == 2 and result["proposed_severity"] is None


def test_no_second_ask_without_time_left():
    times = iter([0.0, 4.5])  # 시작, 첫 응답 뒤
    vlm = FakeVlm("not json", _answer(), latency_ms=1500)
    with pytest.raises(JobFailed) as e:
        _run(_input(), vlm, clock=lambda: next(times))
    assert e.value.code == "VLM_ANSWER_INVALID" and len(vlm.calls) == 1


@pytest.mark.parametrize("error", [VlmTimeout("느림"), VlmUnavailable("연결 실패")])
def test_vlm_errors_pass_through_retryable(error):
    with pytest.raises(JobFailed) as e:
        _run(_input(), FakeVlm(error))
    assert e.value.code == error.code and e.value.retryable


def test_invalid_input_is_not_retryable():
    inp = _input()
    inp["frames"][0]["face_policy"] = "OBSERVED"
    with pytest.raises(JobFailed) as e:
        _run(inp, FakeVlm())
    assert e.value.code == "VLM_INPUT_INVALID" and not e.value.retryable


def test_cancelled_before_vlm_call():
    ctx = _ctx(_input())
    ctx.cancelled.set()
    vlm = FakeVlm(_answer())
    handler = VlmRationaleHandler(Store(), Provider(), client_factory=lambda: vlm, decode=_decode)
    with pytest.raises(JobFailed) as e:
        handler(ctx)
    assert e.value.code == "CANCELLED" and vlm.calls == []


@pytest.mark.parametrize("severity", ["CRITICAL", "HIGH", "CAUTION", "INFO"])
@pytest.mark.parametrize("proposed", ["CRITICAL", "HIGH", "CAUTION", "INFO", None])
def test_no_path_to_downgrade_or_resolve(severity, proposed):
    """invalid 예시(vlm_downgrade·vlm_resolve)처럼 되는 결과가 없다: 어떤 의견이든 결과는 계약을 지키고 등급은 그대로."""
    result = _run(_input(severity=severity), FakeVlm(_answer(proposed)))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["severity"] == severity and set(result) <= set(EXPECTED) | {"clause_ids"}


def test_severity_never_changes_across_all_combinations():
    """D-G7-01: 모든 입력 등급 × VLM 의견 조합에서 결과 severity 가 입력과 달라진 경우 0건."""
    severity_changed = 0
    for severity in SEVERITIES:
        for proposed in (*SEVERITIES, None):
            result = _run(_input(severity=severity), FakeVlm(_answer(proposed)))
            if result["severity"] != severity:
                severity_changed += 1
            if proposed is not None and SEVERITY_RANK[proposed] > SEVERITY_RANK[severity]:
                assert result["proposed_severity"] == proposed
            else:
                assert result["proposed_severity"] is None
    assert severity_changed == 0


def test_real_client_rejects_non_vlmframe_images():
    """실제 클라이언트로 보내는 요청 본문에도 VlmFrame 으로 만든 data URL 이외의 이미지가 없다."""
    sent = []

    def opener(request, timeout):
        sent.append(json.loads(request.data))

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"model": "cosmos3-nano",
                                   "choices": [{"message": {"content": json.dumps(_answer())}}]}).encode()
        return R()

    client = VlmClient(VlmSettings(), opener=opener, encode=lambda px: ("image/jpeg", b"jpg"))
    result = _run(_input(severity="HIGH"), client)
    images = [p for p in sent[0]["messages"][-1]["content"] if p["type"] == "image_url"]
    assert len(images) == 2 and all(p["image_url"]["url"].startswith("data:image/jpeg;base64,") for p in images)
    assert sent[0]["response_format"]["type"] == "json_schema" and result["proposed_severity"] == "CRITICAL"


# --- 순수 모듈 ---


def test_parse_answer_rejects_extra_keys_and_bad_severity():
    with pytest.raises(ValueError):
        parse_answer(json.dumps({**_answer(), "clause_ids": ["산업안전보건기준에 관한 규칙 제43조"]}))
    with pytest.raises(ValueError):
        parse_answer(json.dumps(_answer("RESOLVED")))
    assert parse_answer(json.dumps(_answer(None))).proposed_severity is None


def test_upward_only():
    assert upward_only("CAUTION", "HIGH") == "HIGH"
    assert upward_only("HIGH", "HIGH") is None and upward_only("HIGH", "CAUTION") is None
    assert upward_only("CRITICAL", None) is None
    assert check_proposal("CAUTION", "HIGH") == ("HIGH", None)
    assert check_proposal("HIGH", "HIGH") == (None, PROPOSAL_DISCARD_REASON)
    assert check_proposal("HIGH", "CAUTION") == (None, PROPOSAL_DISCARD_REASON)
    assert check_proposal("CRITICAL", "CRITICAL") == (None, PROPOSAL_DISCARD_REASON)
    assert check_proposal("CRITICAL", None) == (None, None)


def test_build_prompt_without_zone_or_tracks():
    inp = _input(zone_id=None, metrics={}, structured_state={})
    text = build_prompt(inp, [{"camera_id": "cam2", "estimated_capture_time_ms": inp["observation_time"] - 4000}])
    assert "구역: 없음" in text and "1. 카메라 cam2, 이벤트 시점 기준 -4.0초" in text


def test_prompt_object_matches_g7_tool_and_handler():
    """G7 도구는 frames 없이 build 를 부른다. 프레임 줄만 빠지고 나머지는 처리기 질문과 같다."""
    from experiments.g7_eval.g7_answers import load_prompt

    assert load_prompt("simai_ai.vlm.rationale:PROMPT") is PROMPT and PROMPT.placeholder is False
    inp = _input(confidence_reasons=["OCCLUDED"])
    without = PROMPT.build({k: v for k, v in inp.items() if k != "frames"})
    with_frames = PROMPT.build(inp)
    assert "프레임" not in without.text and "신뢰도 사유: OCCLUDED" in without.text
    assert with_frames.text.startswith(without.text.rsplit("\n", 1)[0])
    assert without.response_format == RESPONSE_FORMAT and without.system == with_frames.system


# --- 등록 ---


def test_registered_only_with_store_vlm_settings_and_weights(tmp_path):
    weights = tmp_path / "yolo26x-pose.pt"
    weights.write_bytes(b"w")
    env = {"SIMAI_TIER0_WEIGHTS": str(weights)}
    assert isinstance(entry.build_handlers(None, Store(), env=env, pose=lambda p: Provider())["vlm_rationale"],
                      VlmRationaleHandler)
    assert "vlm_rationale" not in entry.build_handlers(None, None, env=env, pose=lambda p: Provider())
    assert "vlm_rationale" not in entry.build_handlers(None, Store(), env={}, pose=lambda p: Provider())
    bad = {**env, "SIMAI_VLM_TIMEOUT_S": "zero"}
    assert "vlm_rationale" not in entry.build_handlers(None, Store(), env=bad, pose=lambda p: Provider())

    def broken(path):
        raise RuntimeError("ultralytics 가 없다")

    handlers = entry.build_handlers(None, Store(), env=env, pose=broken)
    assert "vlm_rationale" not in handlers and "tbm_report" in handlers


def test_four_frames_parallel_download_propagates_remaining_budget_to_vlm():
    """9차 세션 재현: 프레임 4장 병렬 다운로드(1.8초) + 얼굴 처리(0.4초) 뒤 남은 2.8초 예산으로 VLM 을 호출한다."""
    import threading

    now = [0.0]
    barrier = threading.Barrier(4, timeout=2.0)
    lock = threading.Lock()
    finished_fetch = 0

    class ParallelStore:
        def get(self, ctx, uri):
            nonlocal finished_fetch
            assert ctx.remaining_s() == pytest.approx(5.0)
            barrier.wait()
            with lock:
                finished_fetch += 1
                if finished_fetch == 4:
                    now[0] += 1.8  # 4장 병렬 다운로드 총 1.8초(순차였으면 7.2초로 시간 초과)
            return b"synthetic:" + uri.encode()

        def put(self, ctx, name, content, content_type):  # pragma: no cover
            raise AssertionError

    class TimedProvider:
        def people(self, image):
            now[0] += 0.1  # 프레임당 0.1초 × 4장 = 0.4초
            return [Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)]

    seen_timeouts: list[float] = []

    def opener(request, timeout):
        seen_timeouts.append(timeout)
        now[0] += 1.2

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(
                    {"model": "cosmos3-nano", "choices": [{"message": {"content": json.dumps(_answer())}}]}
                ).encode()

        return R()

    inp = _input(severity="HIGH")
    base_ts = inp["observation_time"]
    template = inp["frames"][0]
    inp["frames"] = [
        {
            **template,
            "camera_id": f"cam{(i % 2) + 1}",
            "frame_uri": f"s3://simai-events/0192a6f0/frame-{i}.jpg",
            "estimated_capture_time_ms": base_ts - (3 - i) * 1000,
            "face_policy": "BLURRED",
        }
        for i in range(4)
    ]
    client = VlmClient(VlmSettings(timeout_s=30.0), opener=opener, encode=lambda px: ("image/jpeg", b"jpg"))
    handler = VlmRationaleHandler(
        ParallelStore(),
        TimedProvider(),
        client_factory=lambda: client,
        decode=_decode,
        monotonic=lambda: now[0],
    )
    result = handler(_ctx(inp))
    assert seen_timeouts == [pytest.approx(2.8)]
    assert result["proposed_severity"] == "CRITICAL"
    assert result["latency_ms"] == 3400


def test_budget_exhausted_during_gate_skips_vlm_call():
    now = [0.0]

    class SlowProvider:
        def people(self, image):
            now[0] = 5.0  # 첫 프레임 검출 직후 5초 예산 소진
            return [Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)]

    vlm = FakeVlm(_answer())
    handler = VlmRationaleHandler(
        Store(),
        SlowProvider(),
        client_factory=lambda: vlm,
        decode=_decode,
        monotonic=lambda: now[0],
    )
    with pytest.raises(JobFailed) as e:
        handler(_ctx(_input()))
    assert e.value.code == "VLM_TIMEOUT" and e.value.retryable
    assert vlm.calls == []
