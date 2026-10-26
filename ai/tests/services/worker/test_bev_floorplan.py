"""bev_floorplan 처리기 (S15P31A101-239 완료 조건, 명세 4.1, 12.5).

객체 저장소와 영상 디코더는 가짜다. 보정은 계약 예시 calibration.g1_auto(CALIBRATED, cal-1)를 객체 저장소에 두고
calibration_ref·calibration_hash 로 받는다. 프레임은 회색 한 장씩(내용은 BEV 위치 정확도와 무관, 정확도는 tests/scene).
종단 시험은 실제 HTTP 가짜 EC2(conftest.run_main)로 대여 → 실행 → 결과 제출까지 돌리고, 가짜 EC2 가 모든 메시지를
계약(job_callback 2.2.0 의 bev_floorplan 결과 포함)으로 검증한다.
"""

from __future__ import annotations

import hashlib
import io
import json

import numpy as np
import pytest

import services.worker.__main__ as entry
from services.worker.bev_floorplan import BevFloorplanHandler
from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import load_example

CALIBRATION = json.dumps(load_example("calibration.g1_auto.json")).encode()
CAL_URI = "s3://simai-site/calibrations/cal-1.json"
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/bev_floorplan.schema.json#/$defs/result"}
T_CAPTURE = 1760342000000


def _npy(image: np.ndarray) -> bytes:
    buf = io.BytesIO()
    np.save(buf, image)
    return buf.getvalue()


def decode_npy(content: bytes) -> np.ndarray:
    try:
        return np.load(io.BytesIO(content))
    except Exception as e:  # noqa: BLE001
        raise ValueError(str(e)) from e


class FakeStore:
    """input 의 주소만 내주고, 올린 객체는 작업 전용 키에 둔다(EC2 가 키를 정한다, -248)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {CAL_URI: CALIBRATION}
        self.gets: list[str] = []
        self.puts: list[tuple[str, str]] = []
        self.get_failures = 0

    def get(self, ctx: JobContext, uri: str) -> bytes:
        assert ctx.lease_id, "대여에 묶여 서명받는다"
        self.gets.append(uri)
        if self.get_failures:
            self.get_failures -= 1
            raise ObjectStoreError("서명 API 일시 오류(가짜)")
        if uri not in self.objects:
            raise ObjectStoreError(f"없는 객체: {uri}", retryable=False)
        return self.objects[uri]

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        uri = f"s3://simai-site/jobs/{ctx.job_id}/{name}"
        assert uri not in self.objects, "덮어쓰지 않는다"
        self.objects[uri] = content
        self.puts.append((uri, content_type))
        return uri


@pytest.fixture
def store() -> FakeStore:
    s = FakeStore()
    for cam in ("cam1", "cam2", "cam3", "cam4"):
        s.objects[f"s3://simai-site/empty/{cam}-0001.npy"] = _npy(np.full((1080, 1920, 3), 128, dtype=np.uint8))
    return s


def bev_input(**over) -> dict:
    doc = {
        "calibration_version": "cal-1",
        "calibration_ref": CAL_URI,
        "calibration_hash": "sha256:" + hashlib.sha256(CALIBRATION).hexdigest(),
        "frame_source": "LIVE",
        "frames": [
            {"camera_id": cam, "frame_uri": f"s3://simai-site/empty/{cam}-0001.npy", "estimated_capture_time_ms": T_CAPTURE,
             "timestamp_source": "RECEIVE_ESTIMATED", "timestamp_uncertainty_ms": 25}
            for cam in ("cam1", "cam2", "cam3", "cam4")
        ],
        "scene_version": "scene-3",
    }
    doc.update(over)
    return doc


def handler_for(store: FakeStore) -> BevFloorplanHandler:
    return BevFloorplanHandler(store, clock_ms=lambda: 1760342460000, decode=decode_npy)


def ctx_for(inp: dict) -> JobContext:
    return JobContext("job-0001", "0192a6f0-7c1e-7b3a-9f2d-000000000311", "bev_floorplan", 1, inp)


def test_e2e_lease_run_upload_submit_and_result_passes_contract(ec2, run_main, store):
    job_id = ec2.add_job("bev_floorplan", bev_input(), priority="BACKGROUND", timeout_ms=60_000)

    assert run_main({"bev_floorplan": handler_for(store)}) == 0  # FakeEC2 가 job_callback(2.2.0 bev 결과 포함)을 계약으로 검증했다
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    r = job.result
    assert not schema_errors(r, RESULT_SCHEMA)
    png = store.objects[r["image_uri"]]
    assert png.startswith(b"\x89PNG") and r["image_sha256"] == "sha256:" + hashlib.sha256(png).hexdigest()
    assert [ct for _, ct in store.puts] == ["image/png", "application/json"]
    meta = json.loads(store.objects[r["metadata_uri"]])
    assert meta["calibration_version"] == r["calibration_version"] == "cal-1"
    assert r["scene_version"] is None  # 입력에 scene-3 이 있어도 정적 3D 층을 쓰지 않았다
    assert (r["purpose"], r["safety_use"]) == ("OVERVIEW_DISPLAY_ONLY", False)
    assert r["cameras_used"] == ["cam1", "cam2", "cam3", "cam4"] and r["coverage_ratio"] > 0
    assert r["input_provenance"] == {
        "frames": {"source": "LIVE", "camera_ids": ["cam1", "cam2", "cam3", "cam4"]},
        "calibration": {"source": "G1_AUTO", "calibration_version": "cal-1"},
        "is_upper_bound": False,
    }
    # 픽셀 ↔ 미터: 왼쪽 위 픽셀 중심 = origin + (m/2, −m/2)
    ras, m = r["raster"], r["raster"]["meters_per_pixel"]
    p2w = np.array(ras["pixel_to_world"])
    np.testing.assert_allclose(p2w @ [0, 0, 1], [ras["origin_xy"][0] + m / 2, ras["origin_xy"][1] - m / 2, 1])
    outline = r["room_outline"]
    assert outline["source"] == "inferred" and outline["basis"] == "CAMERA_FOOTPRINT" and len(outline["polygon_xy"]) >= 3
    lo, hi = ras["world_bounds"]["min_xy"], ras["world_bounds"]["max_xy"]
    assert all(lo[0] <= x <= hi[0] and lo[1] <= y <= hi[1] for x, y in outline["polygon_xy"])


def test_build_handlers_registers_bev_only_with_object_store(store):
    assert isinstance(entry.build_handlers(None, store)["bev_floorplan"], BevFloorplanHandler)
    # 서명 API 클라이언트(-248) 전에는 올릴 수 없는 작업을 대여하지 않는다
    assert "bev_floorplan" not in entry.build_handlers(None)


def test_calibration_hash_mismatch_fails_without_retry(store):
    inp = bev_input(calibration_hash="sha256:" + "0" * 64)
    with pytest.raises(JobFailed) as e:
        handler_for(store)(ctx_for(inp))
    assert (e.value.code, e.value.retryable) == ("CALIBRATION_HASH_MISMATCH", False)
    assert store.gets == [CAL_URI] and not store.puts  # 프레임을 받기 전에 멈춘다


@pytest.mark.parametrize("inp, why", [
    ({"calibration_version": "cal-1", "frame_uris": ["s3://simai-site/empty/cam1-0001.jpg"]}, "3.1.0 형식"),
    (bev_input(frames=[]), "프레임 없음"),
    (bev_input(frames=bev_input()["frames"][:1] * 2), "같은 카메라 두 장"),
])
def test_invalid_input_fails_before_any_download(store, inp, why):
    with pytest.raises(JobFailed) as e:
        handler_for(store)(ctx_for(inp))
    assert (e.value.code, e.value.retryable) == ("BEV_INPUT_INVALID", False), why
    assert not store.gets


def test_calibration_version_must_match_input(store):
    with pytest.raises(JobFailed) as e:
        handler_for(store)(ctx_for(bev_input(calibration_version="cal-7")))
    assert e.value.code == "CALIBRATION_INVALID"


def test_frame_size_mismatch_is_rejected(store):
    store.objects["s3://simai-site/empty/cam1-0001.npy"] = _npy(np.zeros((10, 10, 3), dtype=np.uint8))
    with pytest.raises(JobFailed) as e:
        handler_for(store)(ctx_for(bev_input()))
    assert (e.value.code, e.value.retryable) == ("BEV_INPUT_REJECTED", False)


def test_transient_store_error_is_retryable_and_ec2_releases_new_attempt(ec2, make_worker, store):
    store.get_failures = 1
    job_id = ec2.add_job("bev_floorplan", bev_input(), timeout_ms=60_000)
    worker = make_worker({"bev_floorplan": handler_for(store)})
    worker.run_once()
    assert ec2.jobs[job_id].error["code"] == "OBJECT_GET_FAILED" and ec2.jobs[job_id].state == "QUEUED"
    worker.run_once()
    job = ec2.jobs[job_id]
    assert (job.state, job.attempt) == ("SUCCEEDED", 2)


def test_cancelled_job_uploads_nothing(store):
    ctx = ctx_for(bev_input())
    ctx.cancelled.set()  # 러너가 LIVE 선점·대여 상실로 취소했다
    with pytest.raises(JobFailed) as e:
        handler_for(store)(ctx)
    assert e.value.code == "CANCELLED" and not store.puts
