"""I5 `vlm_zone_draft` 워커 처리기 시험 (S15P31A101-455)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

import services.worker.__main__ as entry
from services.worker.handlers import JobContext, JobFailed
from services.worker.vlm_zone_draft import RESULT_SCHEMA, VlmZoneDraftHandler
from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmResult, VlmUnavailable
from simai_ai.vlm.face_blur import Person, VlmFrame
from simai_ai.vlm.zone_draft import NO_OPINION_TEXT

EXAMPLES = Path(__file__).resolve().parents[4] / "contracts" / "examples"
REQUEST = json.loads((EXAMPLES / "job_request.vlm_zone_draft.json").read_text(encoding="utf-8"))
FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))


def _make_cloud_json() -> bytes:
    # 단상 1개(z=0.75) + 평평한 바닥(z=0.0)
    xs = np.arange(-1.5, 1.5, 0.08)
    ys = np.arange(-1.5, 1.5, 0.08)
    gx, gy = np.meshgrid(xs, ys)
    floor = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)])
    px = np.arange(0.4, 1.4, 0.08)
    py = np.arange(0.3, 1.1, 0.08)
    pgx, pgy = np.meshgrid(px, py)
    platform = np.column_stack([pgx.ravel(), pgy.ravel(), np.full(pgx.size, 0.75)])
    pts = np.vstack([floor, platform]).tolist()
    return json.dumps({"points_xyz": pts}).encode("utf-8")


CLOUD_BYTES = _make_cloud_json()
CAL_DOC = {
    "calibration_version": "cal-setup-12",
    "cameras": [
        {
            "camera_id": "cam1",
            "intrinsics": {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0},
            "extrinsics": {
                "R": [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]],
                "t": [-0.9, 0.7, 3.0],
            },
            "image_size": [320, 240],
        }
    ],
}
CAL_BYTES = json.dumps(CAL_DOC).encode("utf-8")
CAL_HASH = "sha256:" + hashlib.sha256(CAL_BYTES).hexdigest()


class _FakeStore:
    def get(self, ctx, uri: str) -> bytes:
        if uri.endswith(".json") and "calibrations" in uri:
            return CAL_BYTES
        if uri.endswith(".ply") or uri.endswith(".glb"):
            return CLOUD_BYTES
        return b"frame:" + uri.encode("utf-8")

    def put(self, ctx, name: str, content: bytes, content_type: str) -> str:  # pragma: no cover
        raise AssertionError("vlm_zone_draft 는 객체를 업로드하지 않는다")


class _FakeProvider:
    def people(self, image: np.ndarray) -> list[Person]:
        return [Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)]


def _decode(_data: bytes) -> np.ndarray:
    return np.full((240, 320, 3), 200, dtype=np.uint8)


class _FakeVlm:
    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []
        self.model = "nvidia/Cosmos-Reason2-2B"

    def complete(self, prompt, frames, *, system=None, response_format=None):
        frames = tuple(frames)
        assert all(isinstance(f, VlmFrame) for f in frames)
        self.calls.append({"prompt": prompt, "frames": frames, "system": system, "response_format": response_format})
        ans = self.answers.pop(0)
        if isinstance(ans, Exception):
            raise ans
        content = ans if isinstance(ans, str) else json.dumps(ans, ensure_ascii=False)
        return VlmResult(content=content, model=self.model, latency_ms=410, finish_reason="stop")


def _input(**overrides):
    inp = copy.deepcopy(REQUEST["payload"]["input"])
    inp["calibration_hash"] = CAL_HASH
    inp.update(overrides)
    return inp


def _ctx(inp) -> JobContext:
    return JobContext(job_id="job-455", lease_id="lease-455", job_type="vlm_zone_draft", attempt=1, input=inp)


def test_handler_produces_contract_valid_drafts_with_vlm():
    vlm = _FakeVlm(
        {
            "recommended_name": "중앙 고소 작업대 구역",
            "zone_kind": "WORK_SURFACE",
            "recommended_severity": "HIGH",
            "rationale": "바닥 대비 0.75m 높이의 상판이 관측되어 작업대 추락 주의 구역으로 제안합니다.",
            "confidence": 0.87,
        }
    )
    handler = VlmZoneDraftHandler(
        _FakeStore(),
        _FakeProvider(),
        client_factory=lambda: vlm,
        decode=_decode,
        monotonic=lambda: 10.0,
    )
    result = handler(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["safety_use"] is False
    assert result["status"] == "SUCCEEDED"
    assert result["model_version"] == "nvidia/Cosmos-Reason2-2B"
    assert len(result["drafts"]) >= 1
    first = result["drafts"][0]
    assert first["candidate_kind"] == "ELEVATED_WORK_SURFACE"
    assert first["recommended_name"] == "중앙 고소 작업대 구역"
    assert first["recommended_severity"] == "HIGH"
    assert first["vlm_status"] == "VLM"
    assert len(vlm.calls) == 1


def test_handler_falls_back_to_no_opinion_when_vlm_unavailable_or_missing():
    vlm = _FakeVlm(VlmUnavailable("vllm down"))
    handler = VlmZoneDraftHandler(
        _FakeStore(),
        _FakeProvider(),
        client_factory=lambda: vlm,
        decode=_decode,
        monotonic=lambda: 10.0,
    )
    result = handler(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["safety_use"] is False
    assert result["status"] == "NO_OPINION"
    assert result["model_version"] is None
    assert len(result["drafts"]) >= 1
    assert result["drafts"][0]["rationale"] == NO_OPINION_TEXT
    assert result["drafts"][0]["vlm_status"] == "NO_OPINION"


def test_handler_rejects_person_name_or_observed_face_policy():
    handler = VlmZoneDraftHandler(_FakeStore(), _FakeProvider(), client_factory=None)
    bad = _input(person_name="홍길동")
    with pytest.raises(JobFailed) as exc_info:
        handler(_ctx(bad))
    assert exc_info.value.code == "VLM_ZONE_DRAFT_INPUT_INVALID"


def test_build_handlers_registers_vlm_zone_draft(tmp_path):
    handlers_without_vlm = entry.build_handlers(None, _FakeStore(), env={}, pose=lambda p: _FakeProvider())
    assert isinstance(handlers_without_vlm["vlm_zone_draft"], VlmZoneDraftHandler)

    weights = tmp_path / "yolo26x-pose.pt"
    weights.write_bytes(b"w")
    handlers_with_vlm = entry.build_handlers(
        None,
        _FakeStore(),
        env={"SIMAI_TIER0_WEIGHTS": str(weights)},
        pose=lambda p: _FakeProvider(),
    )
    assert isinstance(handlers_with_vlm["vlm_zone_draft"], VlmZoneDraftHandler)
