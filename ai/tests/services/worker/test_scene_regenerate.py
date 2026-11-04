import json
from pathlib import Path
from types import SimpleNamespace
import uuid

import numpy as np
import pytest

from services.worker.handlers import JobContext, JobFailed
from services.worker.scene_regenerate import SceneRegenerateHandler
from simai_ai.providers.contract import schema_errors
from simai_ai.scene.reconstruct import sha256


def test_regenerate_worker_success_and_failures(tmp_path):
    bundle = tmp_path / ('bundle-' + 'a' * 32)
    bundle.mkdir()
    (bundle / 'COMPLETE').write_text('{}')
    calls = []
    submitter = SimpleNamespace(settings=SimpleNamespace(state_dir=tmp_path),
        _candidate=lambda: SimpleNamespace(bundle_dir=bundle, calibration_version='cal'),
        _active_calibration=lambda: ('cal', tmp_path / 'cal.json', 'CALIBRATED'),
        prepare=lambda b, c, check: {'ok': check()},
        _post=lambda payload: calls.append(payload) or {'job_id': 'job-1'})
    ctx = JobContext('job', 'lease', 'scene_regenerate', 1, {'request_id': 12, 'requested_at_ms': 1})
    marker = tmp_path / 'tier0.live'
    handler = SceneRegenerateHandler(submitter, marker, timeout_s=.02)
    assert handler(ctx)['static_reconstruct_job_id'] == 'job-1'
    assert calls[0]['request_id'] == 12
    submitter._candidate = lambda: 'NO_BUNDLE'
    with pytest.raises(JobFailed, match='TIER0_NOT_RUNNING'):
        handler(ctx)
    marker.touch()
    with pytest.raises(JobFailed, match='TIMEOUT'):
        handler(ctx)
    control = tmp_path / 'clean_background'
    (control / 'regenerate.response.json').write_text(json.dumps({'request_id': 12, 'status': 'NOT_READY'}))
    with pytest.raises(JobFailed, match='BACKGROUND_NOT_READY'):
        handler(ctx)
    (control / 'regenerate.response.json').write_text(json.dumps({'request_id': 12, 'status': 'READY', 'bundle_path': str(bundle), 'calibration_version': 'cal'}))
    submitter.prepare = lambda *args: (_ for _ in ()).throw(OSError())
    with pytest.raises(JobFailed, match='UPLOAD_FAILED'):
        handler(ctx)


def test_regenerate_worker_refines_bundle_and_reports_quality(tmp_path: Path) -> None:
    """S15P31A101-461: 워커 SceneRegenerateHandler 가 이전 배경 채움·비움 품질 비율을 계산해 제출 및 콜백 결과에 남긴다."""
    cv2 = pytest.importorskip("cv2")

    version_dir = tmp_path / "clean_background" / "cal-1"
    prev_dir = version_dir / ("camera-" + "0" * 32)
    bundle = version_dir / ("bundle-" + "b" * 32)
    prev_dir.mkdir(parents=True)
    bundle.mkdir(parents=True)

    h, w = 24, 32
    # cam_1 은 이전 REFERENCE 배경이 있고, cam_2 는 이전 배경이 없다고 가정한다
    prev_img = np.full((h, w, 3), 120, dtype=np.uint8)
    ok, prev_buf = cv2.imencode(".png", prev_img)
    assert ok
    (prev_dir / "cam_1.png").write_bytes(prev_buf.tobytes())
    prev_meta = {
        "format": "simai.clean_backgrounds",
        "calibration_version": "cal-1",
        "camera_generations": {"cam_1": 1},
        "cameras": {
            "cam_1": {
                "camera_id": "cam_1",
                "ready": True,
                "calibration_version": "cal-1",
                "image": "cam_1.png",
                "image_sha256": sha256(prev_buf.tobytes()),
                "image_size": [w, h],
                "background_source": "REFERENCE",
            }
        },
    }
    (prev_dir / "clean_backgrounds.json").write_text(json.dumps(prev_meta), encoding="utf-8")

    # 현재 묶음: cam_1 과 cam_2 모두 [6:14, 8:20] (8x12 = 96 / 768 = 0.125) 영역에 사람이 앉아 있어 valid_mask == 0
    recent_img = np.full((h, w, 3), 200, dtype=np.uint8)
    recent_img[6:14, 8:20] = 0
    mask = np.full((h, w), 255, dtype=np.uint8)
    mask[6:14, 8:20] = 0
    _, img_buf = cv2.imencode(".png", recent_img)
    _, msk_buf = cv2.imencode(".png", mask)

    cameras_doc = {}
    for cid in ("cam_1", "cam_2"):
        (bundle / f"{cid}.png").write_bytes(img_buf.tobytes())
        (bundle / f"{cid}.valid.png").write_bytes(msk_buf.tobytes())
        cameras_doc[cid] = {
            "camera_id": cid,
            "ready": True,
            "calibration_version": "cal-1",
            "image": f"{cid}.png",
            "valid_mask": f"{cid}.valid.png",
            "image_sha256": sha256(img_buf.tobytes()),
            "image_size": [w, h],
            "background_source": "RECENT",
        }

    bundle_meta = {
        "format": "simai.clean_backgrounds",
        "calibration_version": "cal-1",
        "ready": True,
        "complete": True,
        "camera_generations": {"cam_1": 1, "cam_2": 1},
        "input_provenance": {"frames": {"source": "LIVE"}},
        "cameras": cameras_doc,
    }
    (bundle / "clean_backgrounds.json").write_text(json.dumps(bundle_meta), encoding="utf-8")
    (bundle / "COMPLETE").write_text(
        json.dumps({"calibration_version": "cal-1", "metadata": "clean_backgrounds.json", "camera_ids": ["cam_1", "cam_2"]}),
        encoding="utf-8",
    )

    calls: list[dict] = []
    submitter = SimpleNamespace(
        settings=SimpleNamespace(state_dir=tmp_path),
        _candidate=lambda: SimpleNamespace(bundle_dir=bundle, calibration_version="cal-1"),
        _active_calibration=lambda: ("cal-1", tmp_path / "cal.json", "CALIBRATED"),
        prepare=lambda b, c, check: {"ok": check()},
        _post=lambda payload: calls.append(payload) or {"job_id": "job-regen-461"},
    )
    ctx = JobContext(
        "job-20261020-0001",
        "0192a6f0-7c1e-7b3a-9f2d-000000000311",
        "scene_regenerate",
        1,
        {"request_id": 7, "requested_at_ms": 1_760_000_000_000},
    )
    handler = SceneRegenerateHandler(submitter, tmp_path / "tier0.live", blend_radius_px=3)
    res = handler(ctx)

    assert res["bundle_id"] == bundle.name
    assert res["static_reconstruct_job_id"] == "job-regen-461"
    assert "quality" in res
    q = res["quality"]
    # cam_1 은 이전 배경으로 채워져 previous_fill_ratio=0.125, unrevealed_ratio=0.0
    # cam_2 는 이전 배경이 없어 previous_fill_ratio=0.0, unrevealed_ratio=0.125
    assert q["cameras"]["cam_1"] == {"previous_fill_ratio": 0.125, "unrevealed_ratio": 0.0}
    assert q["cameras"]["cam_2"] == {"previous_fill_ratio": 0.0, "unrevealed_ratio": 0.125}
    assert q["previous_fill_ratio"] == pytest.approx(0.0625)
    assert q["unrevealed_ratio"] == pytest.approx(0.0625)
    assert calls[0]["input"]["regenerate_quality"] == q

    # job_callback 계약 검증 통과 확인
    callback_doc = {
        "schema_version": "2.5.0",
        "msg_type": "job_callback",
        "msg_id": str(uuid.uuid4()),
        "sent_at": 1_760_342_460_100,
        "source_id": "worker-01",
        "payload": {
            "job_id": "job-20261020-0001",
            "lease_id": "0192a6f0-7c1e-7b3a-9f2d-000000000311",
            "job_type": "scene_regenerate",
            "status": "SUCCEEDED",
            "started_at": 1_760_342_400_500,
            "finished_at": 1_760_342_460_050,
            "result": dict(res),
        },
    }
    assert schema_errors(callback_doc, "job_callback") == []
