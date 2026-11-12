"""관제 틀어짐 감시 오탐 줄이기(S15P31A101-646): 관절 재투영 DRIFTED 의 융합 제외 조건, 배경 재검증 복귀, 작은 회전 잡음,
세팅 카메라 확인 기준선 전달."""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import recording, site  # noqa: F401

from services.tier0 import service as service_module
from services.tier0.drift_watch import BackgroundDriftWatch
from simai_ai.calibration import rotation_recovery as rr
from simai_ai.calibration.camera_check import save_baseline
from simai_ai.calibration.monitor import (
    BASELINE_SOURCE_SETUP_CAMERA_CHECK,
    BaselineRecord,
    CameraMonitorReport,
    MonitorReport,
    MonitorStatus,
)


def _report(service, camera_id, *, status, verified, baseline, median):
    cams = []
    for cid in sorted(service.rig.cameras):
        if cid == camera_id:
            cams.append(CameraMonitorReport(cid, status, 30, median, median, baseline, verified, 8.0, 20_000,
                                            service.clock.now_ms()))
        else:
            cams.append(CameraMonitorReport(cid, MonitorStatus.MONITORING, 30, 1.0, 2.0, 1.0, verified, 4.0, 0,
                                            service.clock.now_ms()))
    return MonitorReport(service.clock.now_ms(), service.rig.calibration_version, tuple(cams), ())


def _first_camera(service):
    return sorted(service.rig.cameras)[0]


def test_learned_baseline_drift_keeps_camera_in_fusion_as_suspect(site):
    """즉석 학습 기준선(미확정)의 DRIFTED 는 융합에서 빼지 않고 의심으로만 둔다(14차 cam_A 오탐 → ID 갈라짐)."""
    service = site.service
    cid = _first_camera(service)
    service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=False, baseline=2.0,
                                        median=9.0), ())
    assert service.perception.excluded_cameras == ()
    assert cid in service.calibration_status["suspect_cameras"]
    assert "융합 유지" in service.calibration_status["suspect_reason"]


def test_verified_small_deviation_stays_but_large_deviation_excludes(site):
    service = site.service
    cid = _first_camera(service)
    # 검증 기준선 2 px, 창 6 px: DRIFTED 지만 크게 벗어나지 않음(제외 조건 max(2×2.5, 2+5) = 7 px)
    service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=True, baseline=2.0,
                                        median=6.0), ())
    assert service.perception.excluded_cameras == ()
    service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=True, baseline=2.0,
                                        median=9.0), ())
    assert service.perception.excluded_cameras == (cid,)
    assert cid in service._reprojection_excluded


def test_background_revalidation_returns_excluded_camera_and_limits_repeats(site):
    service = site.service
    cid = _first_camera(service)

    class _Rot:
        settings = type("S", (), {"config": rr.RotationConfig()})()
        document = {"calibration_version": service.rig.calibration_version}

    resets = []
    original_rot, original_watch = service.rotation_recovery, service.drift_watch
    service.rotation_recovery = _Rot()
    service.drift_watch = type("W", (), {"reset_camera": lambda self, v, c: resets.append((v, c)),
                                         "baselines": lambda self: {}})()
    stable = {"rotations": {cid: {"status": "ROTATION", "active_angle_rad": math.radians(0.1)}}}
    moved = {"rotations": {cid: {"status": "ROTATION", "active_angle_rad": math.radians(2.0)}}}

    for round_ in range(service_module.MAX_REVALIDATIONS):
        service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=True, baseline=2.0,
                                            median=9.0), ())
        assert service.perception.excluded_cameras == (cid,)
        service._revalidate_excluded(stable)
        service._revalidate_excluded(moved)  # 연속이 끊기면 처음부터 센다
        for _ in range(service_module.REVALIDATE_STABLE_CYCLES - 1):
            service._revalidate_excluded(stable)
        assert service.perception.excluded_cameras == (cid,)
        service._revalidate_excluded(stable)
        assert service.perception.excluded_cameras == ()
        assert resets[-1] == (service.rig.calibration_version, cid) and len(resets) == round_ + 1
    # 되돌림을 다 쓰면 다시 빼고 재세팅 안내로 둔다
    service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=True, baseline=2.0,
                                        median=9.0), ())
    for _ in range(service_module.REVALIDATE_STABLE_CYCLES + 2):
        service._revalidate_excluded(stable)
    assert service.perception.excluded_cameras == (cid,)
    assert any(item["camera_id"] == cid for item in service.re_setup_field())
    service.rotation_recovery, service.drift_watch = original_rot, original_watch


def test_background_source_still_excludes_immediately(site):
    """배경 회전 복구가 알린 DRIFTED(위치 이동·큰 회전)는 기준선과 상관없이 바로 뺀다(실제 이동 감지는 유지)."""
    service = site.service
    cid = _first_camera(service)
    service._apply_drift_report(_report(service, cid, status=MonitorStatus.DRIFTED, verified=False, baseline=None,
                                        median=4.0), (), source="BACKGROUND")
    assert service.perception.excluded_cameras == (cid,)
    assert cid not in service._reprojection_excluded


def _make_candidate(monkeypatch, angle_deg):
    doc = {"calibration_version": "cal-a", "cameras": [
        {"camera_id": "cam1", "K": [[1000, 0, 960], [0, 1000, 540], [0, 0, 1]], "R": np.eye(3).tolist(),
         "t": [0.0, 0.0, 0.0], "image_size": [1920, 1080], "distortion": {"model": "NONE", "coeffs": []}}]}
    from types import SimpleNamespace

    from scipy.spatial.transform import Rotation

    delta = Rotation.from_euler("y", angle_deg, degrees=True).as_matrix()
    monkeypatch.setattr(rr, "match_features", lambda a, b: (np.zeros((50, 2)), np.zeros((50, 2))))
    monkeypatch.setattr(rr, "fit_rotation", lambda *a, **k: {"status": "ROTATION", "delta_R": delta.tolist(),
                                                              "matches": 50})
    monkeypatch.setattr(rr, "evaluate_pairs", lambda *a, **k: {})
    feat = SimpleNamespace(size=(1920, 1080), points=np.zeros((0, 2)), mask=None)
    return rr.make_candidate(doc, {"cam1": (doc["cameras"][0], feat)}, {"cam1": feat}, {"cam1": 1},
                             at_ms=2, version="cal-rot", config=rr.RotationConfig())


def test_small_rotation_that_does_not_improve_pairs_is_no_evidence(monkeypatch):
    candidate, report = _make_candidate(monkeypatch, 1.0)
    assert candidate is None and report["status"] == "NO_EVIDENCE"
    assert "cam1" in report["no_evidence_cameras"]


def test_large_rotation_that_does_not_improve_pairs_still_requires_setup(monkeypatch):
    candidate, report = _make_candidate(monkeypatch, 3.0)
    assert candidate is None and report["status"] == "REQUIRES_SETUP"


def test_drift_watch_uses_setup_camera_check_baseline(tmp_path, site):
    service = site.service
    doc = service.calibration.current()
    version = doc["calibration_version"]
    values = {cid: 2.5 for cid in service.rig.cameras}
    save_baseline(tmp_path, BaselineRecord(version, values, tuple(sorted(values)),
                                           {c: BASELINE_SOURCE_SETUP_CAMERA_CHECK for c in values}))
    from simai_ai.calibration.camera_check import load_baseline

    watch = BackgroundDriftWatch(lambda r, c: None, baselines={},
                                 baseline_lookup=lambda v: load_baseline(tmp_path, v))
    record = watch._baseline_for(version)
    assert record is not None and set(record.verified_camera_ids) == set(values)
    assert watch._baseline_for("cal-unknown") is None
    # 이미 검증된 기준선이 저장돼 있으면 그것을 쓴다
    stored = BaselineRecord(version, {c: 1.0 for c in values}, tuple(sorted(values)))
    watch2 = BackgroundDriftWatch(lambda r, c: None, baselines={version: stored},
                                  baseline_lookup=lambda v: pytest.fail("lookup 불필요"))
    assert watch2._baseline_for(version) is stored
