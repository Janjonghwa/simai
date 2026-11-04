"""VLM 위험구역 초안(`simai_ai.vlm.zone_draft`) 단위 시험 (S15P31A101-455).

- 합성 점군(높은 단상 1개 + 바닥 구멍 1개)에서 기하 후보 추출
- 평평한 바닥만 있는 점군에서는 빈 목록 반환(AGENTS 규칙 2 "모르면 비운다")
- GLB·PLY 바이트에서 점군 읽기
- 가짜 VLM JSON 응답 파싱과 VLM 실패·형식 오류 시 'AI 의견 없음' 폴백
"""

from __future__ import annotations

import json
import struct

import numpy as np
import pytest

from simai_ai.vlm.client import VlmResult, VlmTimeout, VlmUnavailable
from simai_ai.vlm.face_blur import Person, prepare_vlm_frames
from simai_ai.vlm.zone_draft import (
    NO_OPINION_TEXT,
    CameraPose,
    GeometryCandidate,
    ZoneGeometryConfig,
    assess_geometry_candidates,
    extract_geometry_candidates,
    load_points_from_bytes,
    parse_candidate_vlm_response,
    select_candidate_crops,
)


def _make_flat_floor(
    x_range: tuple[float, float] = (-2.0, 2.0),
    y_range: tuple[float, float] = (-2.0, 2.0),
    step: float = 0.08,
    hole_box: tuple[float, float, float, float] | None = None,
) -> np.ndarray:
    xs = np.arange(x_range[0], x_range[1] + 1e-6, step)
    ys = np.arange(y_range[0], y_range[1] + 1e-6, step)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(gx.size, dtype=np.float64)])
    if hole_box is not None:
        hx0, hy0, hx1, hy1 = hole_box
        in_hole = (pts[:, 0] >= hx0) & (pts[:, 0] <= hx1) & (pts[:, 1] >= hy0) & (pts[:, 1] <= hy1)
        pts = pts[~in_hole]
    return pts


def _make_elevated_platform(
    x_range: tuple[float, float] = (0.6, 1.8),
    y_range: tuple[float, float] = (0.4, 1.2),
    top_z: float = 0.75,
    step: float = 0.08,
) -> np.ndarray:
    xs = np.arange(x_range[0], x_range[1] + 1e-6, step)
    ys = np.arange(y_range[0], y_range[1] + 1e-6, step)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, top_z, dtype=np.float64)])


def test_extract_geometry_candidates_elevated_and_opening():
    """합성 점군(높은 단상 1개 + 바닥 구멍 1개)에서 ELEVATED_WORK_SURFACE 와 OPENING 후보를 모두 뽑는다."""
    floor = _make_flat_floor(hole_box=(-1.3, -1.0, -0.5, -0.3))
    platform = _make_elevated_platform(x_range=(0.6, 1.8), y_range=(0.4, 1.2), top_z=0.75)
    pts = np.vstack([floor, platform])

    candidates = extract_geometry_candidates(pts)
    kinds = {c.candidate_kind for c in candidates}
    assert "ELEVATED_WORK_SURFACE" in kinds
    assert "OPENING" in kinds

    elev = next(c for c in candidates if c.candidate_kind == "ELEVATED_WORK_SURFACE")
    assert elev.top_height_m == pytest.approx(0.75, abs=0.05)
    assert elev.obb is not None
    assert elev.obb.center[0] == pytest.approx(1.2, abs=0.1)
    assert elev.obb.center[1] == pytest.approx(0.8, abs=0.1)
    assert len(elev.polygon_xy) == 4

    opening = next(c for c in candidates if c.candidate_kind == "OPENING")
    assert opening.top_height_m is None
    assert opening.obb is None
    ox = [p[0] for p in opening.polygon_xy]
    oy = [p[1] for p in opening.polygon_xy]
    assert min(ox) == pytest.approx(-1.3, abs=0.2)
    assert max(ox) == pytest.approx(-0.5, abs=0.2)
    assert min(oy) == pytest.approx(-1.0, abs=0.2)
    assert max(oy) == pytest.approx(-0.3, abs=0.2)


def test_extract_geometry_candidates_depressed_opening_and_floor_edge():
    """함몰 점군(z <= -0.15m)에서 내부 개구부(OPENING)와 외곽 단부(FLOOR_EDGE)를 구분해 뽑는다."""
    floor = _make_flat_floor()
    # 내부 함몰 구멍 (-0.8 ~ -0.2, -0.8 ~ -0.2, z = -0.45)
    xs = np.arange(-0.8, -0.2, 0.08)
    ys = np.arange(-0.8, -0.2, 0.08)
    gx, gy = np.meshgrid(xs, ys)
    hole_pts = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, -0.45)])
    # 외곽 단부 낙차 (x = 2.05 ~ 2.25, y = -1.2 ~ 0.0, z = -0.60)
    ex = np.arange(2.05, 2.25, 0.08)
    ey = np.arange(-1.2, 0.0, 0.08)
    egx, egy = np.meshgrid(ex, ey)
    edge_pts = np.column_stack([egx.ravel(), egy.ravel(), np.full(egx.size, -0.60)])

    candidates = extract_geometry_candidates(np.vstack([floor, hole_pts, edge_pts]))
    kinds = {c.candidate_kind for c in candidates}
    assert "OPENING" in kinds
    assert "FLOOR_EDGE" in kinds


def test_flat_floor_or_empty_returns_empty_list():
    """평평한 바닥만 있거나 점이 없으면 후보를 지어내지 않고 빈 목록을 돌려준다(AGENTS 규칙 2)."""
    assert extract_geometry_candidates(np.zeros((0, 3))) == []
    assert extract_geometry_candidates(_make_flat_floor()) == []


def test_load_points_from_glb_and_ply():
    """미리보기 GLB 와 ASCII PLY 바이트에서 World XYZ 점군을 읽는다."""
    pts = np.array([[0.0, 0.0, 0.0], [1.0, 2.0, 0.75], [-0.5, 0.5, -0.4]], dtype=np.float32)
    bin_data = pts.tobytes()
    gltf = {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": len(bin_data)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": len(bin_data)}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "mode": 0}]}],
    }
    json_bytes = json.dumps(gltf).encode("utf-8")
    while len(json_bytes) % 4 != 0:
        json_bytes += b" "
    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_data)
    glb = (
        struct.pack("<4sII", b"glTF", 2, total_len)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
        + struct.pack("<II", len(bin_data), 0x004E4942)
        + bin_data
    )
    loaded_glb = load_points_from_bytes(glb, "s3://simai-bucket/preview.glb")
    np.testing.assert_allclose(loaded_glb, pts, atol=1e-5)

    ply = (
        b"ply\nformat ascii 1.0\nelement vertex 2\n"
        b"property float x\nproperty float y\nproperty float z\nend_header\n"
        b"0.5 1.0 0.75\n-0.5 -1.0 0.0\n"
    )
    loaded_ply = load_points_from_bytes(ply, "s3://simai-bucket/cloud.ply")
    np.testing.assert_allclose(loaded_ply, [[0.5, 1.0, 0.75], [-0.5, -1.0, 0.0]], atol=1e-5)


class _NoPeopleProvider:
    def people(self, image: np.ndarray) -> list[Person]:
        return []


class _FakeVlmClient:
    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.model = "nvidia/Cosmos-Reason2-2B"

    def complete(self, prompt, frames, *, system=None, response_format=None):
        self.calls.append({"prompt": prompt, "frames": tuple(frames), "system": system, "response_format": response_format})
        resp = self._responses.pop(0)
        if isinstance(resp, Exception):
            raise resp
        content = resp if isinstance(resp, str) else json.dumps(resp, ensure_ascii=False)
        return VlmResult(content=content, model=self.model, latency_ms=320, finish_reason="stop")


def _make_vlm_frame(cam_id: str = "cam1"):
    img = np.full((240, 320, 3), 180, dtype=np.uint8)
    gate = prepare_vlm_frames([(cam_id, img)], "BLURRED", _NoPeopleProvider())
    return gate.frames[0]


def test_select_candidate_crops_and_vlm_success():
    """기하 후보를 카메라에 투영해 얼굴 처리된 VlmFrame crop 을 만들고 Cosmos3-Nano JSON 응답을 파싱한다."""
    cand = GeometryCandidate(
        candidate_id="cand-elevated-01",
        candidate_kind="ELEVATED_WORK_SURFACE",
        polygon_xy=((0.5, 0.5), (1.5, 0.5), (1.5, 1.2), (0.5, 1.2)),
        z_min_m=0.0,
        z_max_m=2.5,
        top_height_m=0.75,
    )
    frame = _make_vlm_frame("cam1")
    # 위에서 아래(-Z 방향)로 내려다보는 카메라: R = diag(1, -1, -1), t = [0, 0, 3]
    pose = CameraPose(
        camera_id="cam1",
        K=np.array([[200.0, 0.0, 160.0], [0.0, 200.0, 120.0], [0.0, 0.0, 1.0]]),
        R=np.diag([1.0, -1.0, -1.0]),
        t=np.array([-1.0, 0.85, 3.0]),
        image_size=(320, 240),
    )
    crops, evidence = select_candidate_crops(cand, {"cam1": frame}, {"cam1": pose})
    assert evidence == ["cam1"]
    assert len(crops) == 1
    assert crops[0].face_policy == "BLURRED"
    assert crops[0].pixels.shape[0] <= 240 and crops[0].pixels.shape[1] <= 320

    vlm = _FakeVlmClient(
        [
            {
                "recommended_name": "중앙 고소 작업대 구역",
                "zone_kind": "WORK_SURFACE",
                "recommended_severity": "HIGH",
                "rationale": "바닥 대비 0.75m 높이의 작업대 상판이 관측되어 추락 주의 구역으로 제안합니다.",
                "confidence": 0.88,
            }
        ]
    )
    drafts, status, model_version = assess_geometry_candidates(
        [cand],
        vlm_client=vlm,
        frames_by_camera={"cam1": frame},
        cameras_by_id={"cam1": pose},
    )
    assert status == "SUCCEEDED"
    assert model_version == "nvidia/Cosmos-Reason2-2B"
    assert len(drafts) == 1
    assert drafts[0].recommended_name == "중앙 고소 작업대 구역"
    assert drafts[0].recommended_severity == "HIGH"
    assert drafts[0].vlm_status == "VLM"
    assert drafts[0].evidence_cameras == ("cam1",)


@pytest.mark.parametrize(
    "failure",
    [
        VlmUnavailable("vllm down"),
        VlmTimeout("timeout"),
        "not-a-json",
        {"recommended_name": "개구부", "zone_kind": "OPENING", "recommended_severity": "CAUTION", "rationale": "낮은 등급"},
    ],
)
def test_vlm_failure_falls_back_to_no_opinion_without_losing_geometry(failure):
    """VLM 비가용·시간 초과·비정상 JSON·허용되지 않는 등급(CAUTION) 시 기하 후보는 남기고 'AI 의견 없음'으로 폴백한다."""
    cand = GeometryCandidate(
        candidate_id="cand-opening-01",
        candidate_kind="OPENING",
        polygon_xy=((-1.0, -1.0), (-0.3, -1.0), (-0.3, -0.3), (-1.0, -0.3)),
        z_min_m=-0.5,
        z_max_m=0.2,
    )
    frame = _make_vlm_frame("cam1")
    vlm = _FakeVlmClient([failure])

    drafts, status, model_version = assess_geometry_candidates(
        [cand],
        vlm_client=vlm,
        frames_by_camera={"cam1": frame},
    )
    assert status == "NO_OPINION"
    assert model_version is None
    assert len(drafts) == 1
    assert drafts[0].candidate_id == "cand-opening-01"
    assert drafts[0].polygon_xy == cand.polygon_xy
    assert drafts[0].rationale == NO_OPINION_TEXT
    assert drafts[0].vlm_status == "NO_OPINION"
    assert drafts[0].recommended_severity == "CRITICAL"


def test_parse_candidate_vlm_response_rejects_bad_payload():
    cand = GeometryCandidate(
        candidate_id="cand-01",
        candidate_kind="OPENING",
        polygon_xy=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)),
        z_min_m=-0.5,
        z_max_m=0.2,
    )
    with pytest.raises(ValueError, match="recommended_severity"):
        parse_candidate_vlm_response(
            json.dumps(
                {
                    "recommended_name": "이름",
                    "zone_kind": "OPENING",
                    "recommended_severity": "INFO",
                    "rationale": "이유",
                    "confidence": 0.5,
                }
            ),
            cand,
        )
