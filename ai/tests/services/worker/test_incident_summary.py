"""분석 워커 `incident_summary` 처리기 시험 (명세 5.4 용도 2, 12.5, FR-88, S15P31A101-271, S15P31A101-387).

- 계약 예시 `job_request.incident_summary.json` 입력 → `job_callback` `incident_summary` 결과 스키마 통과.
- 클립에서 뽑은 프레임에 촬영 시각을 보간해 붙이고 이벤트 구간 필터와 장수 상한(`max_frames`)을 지킨다.
- 클립 전체를 BGR 배열로 디코딩해 쥐지 않고 `count_clip_frames` + `plan_clip_frames` 로 표본 인덱스를 먼저 고른 뒤
  선택된 프레임만 BGR 로 변환하며, 전체 디코딩(`sample_clip_frames`)과 완전히 같은 표본(표본 동등성)을 보장한다.
- 디코딩 도중 프레임마다 취소(`cancelled`)와 남은 시간 예산(`remaining_s`)을 확인해 즉시 중단한다.
- VLM 에 들어가는 모든 프레임은 `prepare_vlm_frames` 얼굴 흐림 관문을 통과한 `VlmFrame` 이고 안전모 영역은 보존된다.
- 근거 시각(`evidence_timestamps_ms`) 중 이벤트 구간 밖 시각은 버리고, 구간 안 시각이 하나도 없으면 재시도 후 `VLM_ANSWER_INVALID`.
- 결과에는 `severity`·`proposed_severity` 가 없으며 VLM 이 등급 필드를 내면 거부한다.
- 클립 다운로드 실패(`CLIP_FETCH_FAILED`), VLM 시간 초과(`VLM_TIMEOUT`), 얼굴 처리 실패(`NO_FACE_SAFE_FRAMES`), 작업 취소(`CANCELLED`),
  실제 H.264 Annex B 바이트 디코딩(`decode_clip_frames`), 진입점 `build_handlers` 등록을 확인한다.
"""

from __future__ import annotations

import copy
import io
import json
from typing import Any

import numpy as np
import pytest

import services.worker.__main__ as entry
from services.worker.handlers import JobContext, JobFailed
from services.worker.incident_summary import (
    INPUT_SCHEMA,
    RESULT_SCHEMA,
    ClipDecodeBudgetExceeded,
    ClipDecodeCancelled,
    IncidentSummaryHandler,
    count_clip_frames,
    decode_clip_frames,
)
from services.worker.objects import ObjectStoreError
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import load_example
from simai_ai.vlm.client import (
    VlmClient,
    VlmRejected,
    VlmResult,
    VlmSettings,
    VlmTimeout,
    VlmUnavailable,
)
from simai_ai.vlm.face_blur import Person, VlmFrame
from simai_ai.vlm.incident_summary import (
    PROMPT,
    RETRY_NOTE,
    assign_clip_timestamps,
    parse_answer,
    plan_clip_frames,
    resolve_event_window,
    sample_clip_frames,
    uniform_indices,
)

EXAMPLE_INPUT = load_example("job_request.incident_summary.json")["payload"]["input"]
CLIP_URI = EXAMPLE_INPUT["clips"][0]["clip_uri"]
W_START = EXAMPLE_INPUT["window_start_ms"]
W_END = EXAMPLE_INPUT["window_end_ms"]
FACE_KPS = (
    (160.0, 70.0, 0.95),
    (148.0, 60.0, 0.92),
    (172.0, 60.0, 0.92),
    (132.0, 66.0, 0.85),
    (188.0, 66.0, 0.85),
)


def _make_person_frame(seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(11 + seed)
    img = rng.integers(30, 230, size=(240, 320, 3), dtype=np.uint8)
    img[20:42, 120:200] = 245  # 안전모 영역(머리 윗부분)
    return img


def _person() -> Person:
    return Person(
        bbox=(100.0, 20.0, 220.0, 220.0),
        keypoints=FACE_KPS + (None,) * 12,
    )


class Provider:
    def __init__(self, error: Exception | None = None, *, fn=None) -> None:
        self.error = error
        self.fn = fn
        self.calls = 0

    def people(self, image: np.ndarray) -> list[Person]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.fn is not None:
            return self.fn(image)
        return [_person()]


def _pack_frames(frames: list[np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.save(buf, np.stack(frames, axis=0))
    return buf.getvalue()


def _unpack_frames(data: bytes) -> list[np.ndarray]:
    if data == b"corrupt":
        raise ValueError("손상된 클립 바이트")
    arr = np.load(io.BytesIO(data))
    return [arr[i] for i in range(len(arr))]


def _encode_h264_annexb(frames: list[np.ndarray]) -> bytes:
    av = pytest.importorskip("av")
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="h264") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width = frames[0].shape[1]
        stream.height = frames[0].shape[0]
        stream.pix_fmt = "yuv420p"
        stream.options = {"preset": "ultrafast", "tune": "zerolatency"}
        for bgr in frames:
            vf = av.VideoFrame.from_ndarray(bgr, format="bgr24")
            for packet in stream.encode(vf):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return buf.getvalue()


class FakeStore:
    def __init__(self, frames_per_clip: int = 5) -> None:
        frames = [_make_person_frame(i) for i in range(frames_per_clip)]
        self.objects: dict[str, bytes] = {CLIP_URI: _pack_frames(frames)}
        self.gets: list[str] = []
        self.error: ObjectStoreError | None = None

    def get(self, ctx: JobContext, uri: str) -> bytes:
        assert ctx.lease_id
        self.gets.append(uri)
        if self.error is not None:
            raise self.error
        if uri not in self.objects:
            raise ObjectStoreError(f"없는 클립 객체: {uri}", retryable=False)
        return self.objects[uri]

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        raise AssertionError("incident_summary 는 객체를 올리지 않는다")


class FakeVlm:
    def __init__(self, answers: list[Any] | None = None) -> None:
        self.answers = list(
            answers
            or [
                json.dumps(
                    {
                        "summary": "작업자가 위험 구역 z-01 안쪽으로 진입해 머문 뒤 이동했다.",
                        "evidence_timestamps_ms": [1760342390000, 1760342400000],
                    },
                    ensure_ascii=False,
                )
            ]
        )
        self.calls: list[dict[str, Any]] = []

    def complete(self, prompt: str, frames, *, system=None, response_format=None) -> VlmResult:
        frames = tuple(frames)
        assert all(isinstance(f, VlmFrame) for f in frames)
        self.calls.append(
            {
                "prompt": prompt,
                "frames": list(frames),
                "system": system,
                "response_format": response_format,
            }
        )
        nxt = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, VlmResult):
            return nxt
        return VlmResult(
            content=nxt,
            model="cosmos3-nano",
            latency_ms=850,
            input_tokens=320,
            output_tokens=48,
            finish_reason="stop",
        )


def _ctx(inp: dict[str, Any] | None = None) -> JobContext:
    return JobContext(
        "job-summary-1",
        "0192a6f0-7c1e-7b3a-9f2d-000000000312",
        "incident_summary",
        1,
        copy.deepcopy(EXAMPLE_INPUT if inp is None else inp),
    )


def _handler(
    store: FakeStore | None = None,
    vlm: FakeVlm | None = None,
    *,
    provider: Provider | None = None,
    monotonic=lambda: 1.0,
    decode_clip=_unpack_frames,
    count_clip=None,
    max_frames: int = 16,
) -> tuple[IncidentSummaryHandler, FakeStore, FakeVlm]:
    s = store or FakeStore()
    c = vlm or FakeVlm()
    p = Provider() if provider is None else provider
    return (
        IncidentSummaryHandler(
            s,
            p,
            client_factory=lambda: c,  # type: ignore[arg-type,return-value]
            monotonic=monotonic,
            decode_clip=decode_clip,
            count_clip=count_clip,
            max_frames=max_frames,
        ),
        s,
        c,
    )


# --- 종단 계약·정상 요약 ---


def test_contract_example_request_produces_contract_valid_result(ec2, run_main):
    h, store, vlm = _handler()
    job_id = ec2.add_job("incident_summary", copy.deepcopy(EXAMPLE_INPUT), priority="REPORT", timeout_ms=120_000)

    assert run_main({"incident_summary": h}) == 0
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    r = job.result
    assert r is not None
    assert not schema_errors(r, RESULT_SCHEMA)
    assert r["event_id"] == EXAMPLE_INPUT["event_id"]
    assert r["event_sequence"] == EXAMPLE_INPUT["event_sequence"]
    assert r["summary"] == "작업자가 위험 구역 z-01 안쪽으로 진입해 머문 뒤 이동했다."
    assert r["evidence_timestamps_ms"] == [1760342390000, 1760342400000]
    assert all(W_START <= ts <= W_END for ts in r["evidence_timestamps_ms"])
    assert (r["window_start_ms"], r["window_end_ms"]) == (W_START, W_END)
    assert r["frames_used"] == 5
    assert r["model_version"] == "cosmos3-nano"
    assert "severity" not in r and "proposed_severity" not in r
    assert store.gets == [CLIP_URI]
    assert len(vlm.calls) == 1


# --- 얼굴 흐림 게이트 ---


def test_frames_pass_through_face_blur_gate_and_helmet_stays():
    raw_frame = _make_person_frame(0)
    store = FakeStore(frames_per_clip=1)
    store.objects[CLIP_URI] = _pack_frames([raw_frame])
    h, _, vlm = _handler(store)

    h(_ctx())

    sent = vlm.calls[0]["frames"]
    assert len(sent) == 1 and all(isinstance(f, VlmFrame) for f in sent)
    blurred_img = sent[0].pixels
    assert not np.array_equal(blurred_img[55:85, 130:190], raw_frame[55:85, 130:190]), "얼굴 영역은 흐려져야 한다"
    assert np.array_equal(blurred_img[20:42, 120:200], raw_frame[20:42, 120:200]), "안전모 영역은 남아야 한다"


def test_excluded_face_policy_fills_face_region_with_solid_gray_and_keeps_helmet():
    raw_frame = _make_person_frame(0)
    store = FakeStore(frames_per_clip=1)
    store.objects[CLIP_URI] = _pack_frames([raw_frame])
    inp = copy.deepcopy(EXAMPLE_INPUT)
    inp["face_policy"] = "EXCLUDED"
    h, _, vlm = _handler(store)
    r = h(_ctx(inp))
    assert r["frames_used"] == 1
    sent = vlm.calls[0]["frames"]
    assert sent[0].face_policy == "EXCLUDED"
    assert np.all(sent[0].pixels[55:85, 145:175] == 128), "EXCLUDED 는 얼굴 영역을 단색(128)으로 칠한다"
    assert np.array_equal(sent[0].pixels[20:42, 120:200], raw_frame[20:42, 120:200]), "안전모 영역은 남아야 한다"


# --- 촬영 시각 보간·구간 필터·장수 상한 ---


def test_clip_frame_timestamps_and_max_frames_cap():
    store = FakeStore(frames_per_clip=20)
    h, _, vlm = _handler(store, max_frames=5)

    r = h(_ctx())
    assert r["frames_used"] == 5
    prompt = vlm.calls[0]["prompt"]
    for expected_ts in (1760342380000, 1760342390526, 1760342401053, 1760342409474, 1760342420000):
        assert f"촬영 시각 {expected_ts} ms" in prompt


def test_frames_outside_event_window_are_excluded_before_vlm():
    inp = copy.deepcopy(EXAMPLE_INPUT)
    inp["window_start_ms"] = 1760342390000
    inp["window_end_ms"] = 1760342410000
    # 5장: 380000, 390000, 400000, 410000, 420000 -> 구간 안은 390000, 400000, 410000 (3장)
    h, _, vlm = _handler(FakeStore(frames_per_clip=5))

    r = h(_ctx(inp))
    assert r["frames_used"] == 3
    prompt = vlm.calls[0]["prompt"]
    assert "1760342380000" not in prompt and "1760342420000" not in prompt
    assert "1760342390000" in prompt and "1760342400000" in prompt and "1760342410000" in prompt


# --- 근거 시각 이벤트 구간 검증 ---


def test_out_of_window_evidence_timestamps_are_dropped_when_in_window_remains():
    vlm = FakeVlm(
        [
            json.dumps(
                {
                    "summary": "작업자가 위험 구역에 진입했다.",
                    "evidence_timestamps_ms": [1760342300000, 1760342405000, 1760342395000, 1760342999000],
                },
                ensure_ascii=False,
            )
        ]
    )
    h, _, _ = _handler(vlm=vlm)
    r = h(_ctx())
    assert r["evidence_timestamps_ms"] == [1760342395000, 1760342405000]


def test_all_out_of_window_evidence_timestamps_retries_once_then_succeeds():
    vlm = FakeVlm(
        [
            json.dumps({"summary": "첫 시도 구간 밖", "evidence_timestamps_ms": [1760342000000]}, ensure_ascii=False),
            json.dumps({"summary": "두 번째 시도 정상", "evidence_timestamps_ms": [1760342400000]}, ensure_ascii=False),
        ]
    )
    h, _, _ = _handler(vlm=vlm)
    r = h(_ctx())
    assert r["summary"] == "두 번째 시도 정상"
    assert r["evidence_timestamps_ms"] == [1760342400000]
    assert len(vlm.calls) == 2
    assert vlm.calls[1]["prompt"].endswith(RETRY_NOTE)


def test_all_out_of_window_evidence_timestamps_twice_fails_non_retryable():
    out_of_range = json.dumps({"summary": "구간 밖 시각만", "evidence_timestamps_ms": [1760342000000]}, ensure_ascii=False)
    h, _, vlm = _handler(vlm=FakeVlm([out_of_range, out_of_range]))
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("VLM_ANSWER_INVALID", False)
    assert len(vlm.calls) == 2


# --- 등급·상향 의견 금지 ---


@pytest.mark.parametrize(
    "bad_payload",
    [
        {"summary": "요약", "evidence_timestamps_ms": [1760342400000], "proposed_severity": "CRITICAL"},
        {"summary": "요약", "evidence_timestamps_ms": [1760342400000], "severity": "HIGH"},
        {"summary": "", "evidence_timestamps_ms": [1760342400000]},
        {"summary": "요약", "evidence_timestamps_ms": []},
        {"summary": "요약", "evidence_timestamps_ms": ["1760342400000"]},
        {"summary": "요약", "evidence_timestamps_ms": [True]},
    ],
)
def test_vlm_output_with_severity_or_bad_fields_is_rejected(bad_payload):
    raw = json.dumps(bad_payload, ensure_ascii=False)
    h, _, vlm = _handler(vlm=FakeVlm([raw, raw]))
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("VLM_ANSWER_INVALID", False)
    assert len(vlm.calls) == 2


def test_truncated_length_response_is_rejected():
    truncated = VlmResult(
        content=json.dumps({"summary": "잘린 답", "evidence_timestamps_ms": [1760342400000]}, ensure_ascii=False),
        model="m",
        latency_ms=500,
        input_tokens=10,
        output_tokens=256,
        finish_reason="length",
    )
    h, _, _ = _handler(vlm=FakeVlm([truncated, truncated]))
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("VLM_ANSWER_INVALID", False)


# --- 클립 다운로드 실패·디코드 실패·VLM 시간 초과·취소 ---


@pytest.mark.parametrize("retryable", [True, False])
def test_clip_fetch_failure_propagates_retryable(retryable):
    store = FakeStore()
    store.error = ObjectStoreError("S3 클립 내려받기 실패", retryable=retryable)
    h, _, vlm = _handler(store)
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("CLIP_FETCH_FAILED", retryable)
    assert not vlm.calls


def test_corrupt_clip_raises_no_face_safe_frames_non_retryable():
    store = FakeStore()
    store.objects[CLIP_URI] = b"corrupt"
    h, _, vlm = _handler(store)
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("NO_FACE_SAFE_FRAMES", False)
    assert not vlm.calls


def test_detector_failure_raises_no_face_safe_frames_retryable():
    h, _, vlm = _handler(provider=Provider(error=RuntimeError("CUDA OOM")))
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == ("NO_FACE_SAFE_FRAMES", True)
    assert not vlm.calls


@pytest.mark.parametrize(
    ("err", "code", "retryable"),
    [
        (VlmTimeout("시간 초과"), "VLM_TIMEOUT", True),
        (VlmUnavailable("연결 실패"), "VLM_UNAVAILABLE", True),
        (VlmRejected("문맥 초과"), "VLM_REQUEST_REJECTED", False),
    ],
)
def test_vlm_client_errors_keep_code_and_retryable(err, code, retryable):
    h, _, _ = _handler(vlm=FakeVlm([err]))
    with pytest.raises(JobFailed) as exc:
        h(_ctx())
    assert (exc.value.code, exc.value.retryable) == (code, retryable)


def test_invalid_input_fails_before_clip_download():
    bad = copy.deepcopy(EXAMPLE_INPUT)
    bad["face_policy"] = "NONE"
    h, store, vlm = _handler()
    with pytest.raises(JobFailed) as exc:
        h(_ctx(bad))
    assert (exc.value.code, exc.value.retryable) == ("VLM_INPUT_INVALID", False)
    assert not store.gets and not vlm.calls


def test_inverted_window_fails_with_vlm_input_invalid():
    bad = copy.deepcopy(EXAMPLE_INPUT)
    bad["window_start_ms"] = 1760342420000
    bad["window_end_ms"] = 1760342380000
    h, store, _ = _handler()
    with pytest.raises(JobFailed) as exc:
        h(_ctx(bad))
    assert (exc.value.code, exc.value.retryable) == ("VLM_INPUT_INVALID", False)
    assert not store.gets


def test_cancelled_context_stops_before_vlm():
    ctx = _ctx()
    ctx.cancelled.set()
    h, store, vlm = _handler()
    with pytest.raises(JobFailed) as exc:
        h(ctx)
    assert exc.value.code == "CANCELLED"
    assert not store.gets and not vlm.calls


# --- 실제 PyAV H.264 Annex B 디코딩·선택적 BGR 보관·표본 동등성·취소/예산 전파 ---


def test_decode_clip_frames_decodes_real_h264_annexb_stream():
    pytest.importorskip("av")
    raw_frames = [_make_person_frame(i) for i in range(4)]
    h264_bytes = _encode_h264_annexb(raw_frames)
    assert count_clip_frames(h264_bytes) == 4
    decoded = decode_clip_frames(h264_bytes)
    assert len(decoded) == 4
    assert all(f.shape == (240, 320, 3) and f.dtype == np.uint8 for f in decoded)

    store = FakeStore()
    store.objects[CLIP_URI] = h264_bytes
    h, _, vlm = _handler(store, decode_clip=decode_clip_frames)
    r = h(_ctx())
    assert r["frames_used"] == 4
    assert len(vlm.calls[0]["frames"]) == 4


def test_selective_decode_only_converts_chosen_frames_and_matches_sample_clip_frames():
    """여러 클립에서 전체 BGR 배열을 만들지 않고 표본 인덱스만 변환해도 sample_clip_frames 와 결과가 완전히 같다."""
    pytest.importorskip("av")
    # cam1: 20장(일부 구간 밖 포함), cam2: 15장(전부 구간 안), cam3: 8장(전부 구간 밖)
    cam1_raw = [_make_person_frame(i) for i in range(20)]
    cam2_raw = [_make_person_frame(100 + i) for i in range(15)]
    cam3_raw = [_make_person_frame(200 + i) for i in range(8)]
    cam1_h264 = _encode_h264_annexb(cam1_raw)
    cam2_h264 = _encode_h264_annexb(cam2_raw)
    cam3_h264 = _encode_h264_annexb(cam3_raw)

    uri1 = "s3://simai-site/clips/cam1.h264"
    uri2 = "s3://simai-site/clips/cam2.h264"
    uri3 = "s3://simai-site/clips/cam3.h264"
    inp = copy.deepcopy(EXAMPLE_INPUT)
    inp["window_start_ms"] = 1760342390000
    inp["window_end_ms"] = 1760342415000
    base_clip = inp["clips"][0]
    inp["clips"] = [
        {
            **base_clip,
            "clip_id": "clip-cam1",
            "camera_id": "cam1",
            "object_key": "clips/cam1.h264",
            "clip_uri": uri1,
            "clip_start_capture_ms": 1760342380000,
            "clip_end_capture_ms": 1760342420000,
        },
        {
            **base_clip,
            "clip_id": "clip-cam2",
            "camera_id": "cam2",
            "object_key": "clips/cam2.h264",
            "clip_uri": uri2,
            "clip_start_capture_ms": 1760342392000,
            "clip_end_capture_ms": 1760342412000,
        },
        {
            **base_clip,
            "clip_id": "clip-cam3",
            "camera_id": "cam3",
            "object_key": "clips/cam3.h264",
            "clip_uri": uri3,
            "clip_start_capture_ms": 1760342350000,
            "clip_end_capture_ms": 1760342370000,
        },
    ]

    # 기준(전체 디코딩 후 sample_clip_frames)
    full_cam1 = decode_clip_frames(cam1_h264)
    full_cam2 = decode_clip_frames(cam2_h264)
    full_cam3 = decode_clip_frames(cam3_h264)
    expected = sample_clip_frames(
        [(inp["clips"][0], full_cam1), (inp["clips"][1], full_cam2), (inp["clips"][2], full_cam3)],
        window_start_ms=inp["window_start_ms"],
        window_end_ms=inp["window_end_ms"],
        max_frames=6,
    )
    assert len(expected) == 6

    store = FakeStore()
    store.objects = {uri1: cam1_h264, uri2: cam2_h264, uri3: cam3_h264}
    requested_indices: list[list[int]] = []

    def tracking_decode(data: bytes, *, frame_indices=None, cancelled=None, remaining_s=None):
        assert frame_indices is not None, "처리기 경로는 클립 전체 BGR 디코딩(frame_indices=None)을 호출하지 않아야 한다"
        idx_list = list(frame_indices)
        requested_indices.append(idx_list)
        return decode_clip_frames(
            data,
            frame_indices=idx_list,
            cancelled=cancelled,
            remaining_s=remaining_s,
        )

    seen_by_provider: list[np.ndarray] = []
    provider = Provider(fn=lambda img: seen_by_provider.append(img.copy()) or [_person()])
    h, _, vlm = _handler(
        store,
        provider=provider,
        decode_clip=tracking_decode,
        count_clip=count_clip_frames,
        max_frames=6,
    )
    r = h(_ctx(inp))
    assert r["frames_used"] == 6
    # 총 43장(20+15+8) 중 최종 표본 6장만 BGR 로 변환됐고, 구간 밖 cam3 은 2차 디코딩조차 하지 않았다
    assert sum(len(indices) for indices in requested_indices) == 6
    assert len(requested_indices) == 2

    sent = vlm.calls[0]["frames"]
    assert [f.source for f in sent] == [sf.source_id for sf in expected]
    for got_img, exp_sf in zip(seen_by_provider, expected, strict=True):
        assert np.array_equal(got_img, exp_sf.image), f"{exp_sf.source_id} 픽셀이 전체 디코딩 표본과 일치해야 한다"


def test_decode_clip_frames_checks_cancellation_and_budget_mid_stream():
    pytest.importorskip("av")
    h264_bytes = _encode_h264_annexb([_make_person_frame(i) for i in range(12)])

    # count_clip_frames 도중 취소
    checks = 0

    def cancel_after_three() -> bool:
        nonlocal checks
        checks += 1
        return checks > 5

    with pytest.raises(ClipDecodeCancelled):
        count_clip_frames(h264_bytes, cancelled=cancel_after_three)
    assert checks == 6

    # decode_clip_frames(선택 인덱스) 도중 시간 예산 소진
    rem_values = iter([1.0, 1.0, 1.0, 1.0, 0.0])
    with pytest.raises(ClipDecodeBudgetExceeded):
        decode_clip_frames(h264_bytes, frame_indices=[0, 5, 10], remaining_s=lambda: next(rem_values))


def test_handler_cancels_or_stops_on_budget_during_clip_decode_before_face_gate():
    pytest.importorskip("av")
    h264_bytes = _encode_h264_annexb([_make_person_frame(i) for i in range(10)])
    store = FakeStore()
    store.objects[CLIP_URI] = h264_bytes

    # 1) 디코딩 도중 ctx.cancelled 가 켜지면 즉시 CANCELLED 로 멈추고 얼굴 검출·VLM 을 부르지 않는다
    ctx_cancel = _ctx()
    decode_calls = 0

    def cancel_during_decode(data: bytes, *, frame_indices=None, cancelled=None, remaining_s=None):
        nonlocal decode_calls
        decode_calls += 1
        ctx_cancel.cancelled.set()
        return decode_clip_frames(data, frame_indices=frame_indices, cancelled=cancelled, remaining_s=remaining_s)

    provider_cancel = Provider()
    h_cancel, _, vlm_cancel = _handler(
        store,
        provider=provider_cancel,
        decode_clip=cancel_during_decode,
        count_clip=count_clip_frames,
    )
    with pytest.raises(JobFailed) as exc_cancel:
        h_cancel(ctx_cancel)
    assert exc_cancel.value.code == "CANCELLED"
    assert decode_calls == 1 and provider_cancel.calls == 0 and not vlm_cancel.calls

    # 2) 디코딩 도중 120초 예산이 소진되면 retryable NO_FACE_SAFE_FRAMES(budget)로 멈춘다
    now = [0.0]

    def expire_during_decode(data: bytes, *, frame_indices=None, cancelled=None, remaining_s=None):
        now[0] = 120.5
        return decode_clip_frames(data, frame_indices=frame_indices, cancelled=cancelled, remaining_s=remaining_s)

    provider_budget = Provider()
    h_budget, _, vlm_budget = _handler(
        store,
        provider=provider_budget,
        monotonic=lambda: now[0],
        decode_clip=expire_during_decode,
        count_clip=count_clip_frames,
    )
    with pytest.raises(JobFailed) as exc_budget:
        h_budget(_ctx())
    assert (exc_budget.value.code, exc_budget.value.retryable) == ("NO_FACE_SAFE_FRAMES", True)
    assert "budget" in exc_budget.value.message
    assert provider_budget.calls == 0 and not vlm_budget.calls


def test_handler_with_real_vlm_client_sends_blurred_jpeg_and_strict_schema():
    captured: dict[str, Any] = {}

    def fake_opener(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        resp = {
            "model": "cosmos3-nano",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "작업자가 위험 구역 안쪽을 지나갔다.",
                                "evidence_timestamps_ms": [1760342400000],
                            },
                            ensure_ascii=False,
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 200, "completion_tokens": 30},
        }

        class _R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(resp).encode("utf-8")

        return _R()

    client = VlmClient(VlmSettings(port=8000, model="cosmos3-nano"), opener=fake_opener)
    store = FakeStore(frames_per_clip=2)
    h = IncidentSummaryHandler(store, Provider(), client_factory=lambda: client, decode_clip=_unpack_frames)
    r = h(_ctx())
    assert not schema_errors(r, RESULT_SCHEMA)
    schema_props = captured["body"]["response_format"]["json_schema"]["schema"]["properties"]
    assert set(schema_props) == {"summary", "evidence_timestamps_ms"}
    user_content = captured["body"]["messages"][1]["content"]
    img_urls = [p["image_url"]["url"] for p in user_content if p["type"] == "image_url"]
    assert len(img_urls) == 2 and all(u.startswith("data:image/jpeg;base64,") for u in img_urls)


# --- 순수 함수 및 진입점 등록 ---


def test_pure_helpers_validation():
    assert assign_clip_timestamps(1000, 1000, 1) == [1000]
    assert assign_clip_timestamps(1000, 2000, 3) == [1000, 1500, 2000]
    assert uniform_indices(0, 5) == []
    assert uniform_indices(5, 1) == [2]
    with pytest.raises(ValueError):
        assign_clip_timestamps(2000, 1000, 2)
    with pytest.raises(ValueError):
        assign_clip_timestamps(1000, 2000, 0)
    with pytest.raises(ValueError):
        uniform_indices(3, 0)
    with pytest.raises(ValueError):
        plan_clip_frames([], window_start_ms=0, window_end_ms=10, max_frames=0)
    with pytest.raises(ValueError):
        sample_clip_frames([], window_start_ms=0, window_end_ms=10, max_frames=0)
    with pytest.raises(ValueError):
        decode_clip_frames(b"x", frame_indices=[-1])
    with pytest.raises(ValueError):
        resolve_event_window({"clips": []})
    with pytest.raises(ValueError):
        parse_answer("not-json", window_start_ms=0, window_end_ms=10)
    parts = PROMPT.build(EXAMPLE_INPUT)
    assert "이벤트 ID:" in parts.text and parts.response_format is not None
    assert not schema_errors(EXAMPLE_INPUT, INPUT_SCHEMA)


def test_build_handlers_registers_incident_summary_alongside_vlm_rationale(tmp_path):
    weights = tmp_path / "yolo26x-pose.pt"
    weights.write_bytes(b"pt")
    env = {"SIMAI_VLLM_MODEL": "cosmos3-nano", "SIMAI_TIER0_WEIGHTS": str(weights)}
    opened: list[str] = []
    h = entry.build_handlers(None, FakeStore(), env=env, pose=lambda w: opened.append(w) or Provider())
    assert "vlm_rationale" in h and "incident_summary" in h
    assert opened == [str(weights)], "pose 검출기는 한 번만 열어 두 처리기가 공유한다"
