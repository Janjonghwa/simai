"""I5 `vlm_zone_draft` 처리기의 시연 개구부 스위치 시험 (S15P31A101-671).

스위치 기본(켬): U자 단상 점군 → 개구부 초안 1개(단상 개구부, CRITICAL), 개구부 확인 프롬프트·프롬프트 판. 끔: 이전 동작.
계약(입력·결과 스키마)은 바뀌지 않는다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import numpy as np
import pytest

from services.worker.handlers import JobContext
from services.worker.vlm_zone_draft import RESULT_SCHEMA, VlmZoneDraftHandler
from simai_ai.calibration.document import content_hash
from simai_ai.providers.contract import schema_errors
from simai_ai.vlm.client import VlmResult
from simai_ai.vlm.face_blur import Person
from simai_ai.vlm.zone_draft import (
    OPENING_PROMPT_VERSION,
    OPENING_RESPONSE_FORMAT,
    PLATFORM_OPENING_NAME,
    PROMPT_VERSION,
)

EXAMPLES = Path(__file__).resolve().parents[4] / "contracts" / "examples"
REQUEST = json.loads((EXAMPLES / "job_request.vlm_zone_draft.json").read_text(encoding="utf-8"))


def _grid(x0: float, x1: float, y0: float, y1: float, z: float, step: float = 0.05) -> np.ndarray:
    xs = np.arange(x0 + step / 2, x1, step)
    ys = np.arange(y0 + step / 2, y1, step)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, z)])


def _u_platform_cloud() -> bytes:
    """U자 단상(0.45 m, 방석 0.40 m) + 가운데 빈 자리(x 0.8~2.5, y 1.0~1.8) + 바닥."""
    floor = _grid(-2.0, 4.5, -2.0, 4.5, 0.0)
    under = (floor[:, 0] >= 0.0) & (floor[:, 0] <= 2.5) & (floor[:, 1] >= 0.0) & (floor[:, 1] <= 2.8)
    pts = np.vstack(
        [
            floor[~under],
            _grid(0.0, 2.5, 0.0, 1.0, 0.45),
            _grid(0.0, 2.5, 1.8, 2.8, 0.45),
            _grid(0.0, 0.8, 1.0, 1.8, 0.40),
        ]
    )
    return json.dumps({"points_xyz": pts.round(4).tolist()}).encode("utf-8")


CLOUD = _u_platform_cloud()
CAL_DOC = {
    "calibration_version": "cal-setup-12",
    "cameras": [
        {
            "camera_id": "cam_A",
            "intrinsics": {"fx": 200.0, "fy": 200.0, "cx": 160.0, "cy": 120.0},
            # 단상 위 3 m 에서 내려다본다
            "extrinsics": {"R": [[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]], "t": [-1.4, 1.4, 3.0]},
            "image_size": [320, 240],
        }
    ],
}


class _Store:
    def get(self, ctx, uri: str) -> bytes:
        if uri.endswith(".json") and "calibrations" in uri:
            return json.dumps(CAL_DOC).encode("utf-8")
        if uri.endswith((".ply", ".glb")):
            return CLOUD
        return b"frame:" + uri.encode("utf-8")

    def put(self, ctx, name: str, content: bytes, content_type: str) -> str:  # pragma: no cover
        raise AssertionError("vlm_zone_draft 는 객체를 올리지 않는다")


class _NoPeople:
    def people(self, image: np.ndarray) -> list[Person]:
        return []


def _decode(_data: bytes) -> np.ndarray:
    return np.full((240, 320, 3), 160, dtype=np.uint8)


class _FakeVlm:
    model = "nvidia/Cosmos-Reason2-2B"

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[dict] = []

    def complete(self, prompt, frames, *, system=None, response_format=None):
        self.calls.append({"prompt": prompt, "frames": tuple(frames), "system": system, "response_format": response_format})
        return VlmResult(content=json.dumps(self.answers.pop(0), ensure_ascii=False), model=self.model, latency_ms=300,
                         finish_reason="stop")


def _input() -> dict:
    inp = copy.deepcopy(REQUEST["payload"]["input"])
    inp["calibration_hash"] = content_hash(CAL_DOC)
    inp["frames"] = [f for f in inp["frames"] if f["camera_id"] == "cam_A"]
    return inp


def _ctx(inp) -> JobContext:
    return JobContext(job_id="job-671", lease_id="lease-671", job_type="vlm_zone_draft", attempt=1, input=inp)


def _handler(vlm, env) -> VlmZoneDraftHandler:
    return VlmZoneDraftHandler(_Store(), _NoPeople(), client_factory=(lambda: vlm) if vlm else None, decode=_decode,
                               monotonic=lambda: 10.0, env=env)


def test_default_switch_gives_one_platform_opening_without_vlm():
    result = _handler(None, {})(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["prompt_version"] == OPENING_PROMPT_VERSION
    assert result["status"] == "NO_OPINION"
    (draft,) = result["drafts"]
    assert draft["candidate_kind"] == "OPENING" and draft["zone_kind"] == "OPENING"
    assert draft["recommended_name"] == f"{PLATFORM_OPENING_NAME} 1"
    assert draft["recommended_severity"] == "CRITICAL" and draft["eval_type"] == "GROUND_FOOTPRINT"
    assert draft["candidate_id"].startswith("cand-platform-opening-")
    assert "단상 상판이" in draft["rationale"]


def test_default_switch_asks_opening_question_and_keeps_confirmed_opening():
    vlm = _FakeVlm({
        "is_marked_opening": True,
        "markings_seen": ["RAILING"],
        "recommended_name": "단상 개구부",
        "recommended_severity": "HIGH",
        "rationale": "줄무늬 난간이 단상 사이 빈 자리 위에 서 있다.",
        "confidence": 0.7,
    })
    result = _handler(vlm, {"SIMAI_ZONE_DRAFT_OPENING_ONLY": "1"})(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["status"] == "SUCCEEDED"
    (call,) = vlm.calls
    assert call["response_format"] == OPENING_RESPONSE_FORMAT
    (draft,) = result["drafts"]
    assert draft["recommended_name"] == "단상 개구부" and draft["recommended_severity"] == "CRITICAL"
    assert draft["vlm_status"] == "VLM" and draft["evidence_cameras"] == ["cam_A"]


def test_vlm_saying_not_an_opening_leaves_no_draft():
    vlm = _FakeVlm({
        "is_marked_opening": False,
        "markings_seen": [],
        "recommended_name": "없음",
        "recommended_severity": "HIGH",
        "rationale": "표시가 보이지 않는다.",
        "confidence": 0.6,
    })
    result = _handler(vlm, {})(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["drafts"] == [] and result["status"] == "SUCCEEDED"


@pytest.mark.parametrize("off", ["0", "false", "off"])
def test_switch_off_keeps_previous_prompt_and_platform_candidates(off):
    result = _handler(None, {"SIMAI_ZONE_DRAFT_OPENING_ONLY": off})(_ctx(_input()))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert result["prompt_version"] == PROMPT_VERSION
    kinds = [d["candidate_kind"] for d in result["drafts"]]
    assert "ELEVATED_WORK_SURFACE" in kinds
    assert not any(d["candidate_id"].startswith("cand-platform-opening-") for d in result["drafts"])


def test_given_geometry_candidates_are_filtered_and_deduped_when_switch_on():
    inp = _input()
    inp.pop("preview_scene_uri")
    inp.pop("pointcloud_uri")
    square = [[1.0, 1.0], [1.8, 1.0], [1.8, 1.6], [1.0, 1.6]]
    inp["geometry_candidates"] = [
        {"candidate_id": "e1", "candidate_kind": "ELEVATED_WORK_SURFACE", "polygon_xy": square, "z_min_m": 0.0,
         "z_max_m": 2.4, "top_height_m": 0.45},
        {"candidate_id": "o1", "candidate_kind": "OPENING", "polygon_xy": square, "z_min_m": -0.5, "z_max_m": 0.2},
        {"candidate_id": "o2", "candidate_kind": "OPENING", "polygon_xy": [[1.1, 1.05], [1.7, 1.05], [1.7, 1.55], [1.1, 1.55]],
         "z_min_m": -0.5, "z_max_m": 0.2},
    ]
    result = _handler(None, {"SIMAI_ZONE_DRAFT_MAX_OPENINGS": "3"})(_ctx(inp))
    assert schema_errors(result, RESULT_SCHEMA) == []
    assert [d["candidate_id"] for d in result["drafts"]] == ["o1"]
