"""S15P31A101-454: 합성 회전·이동 영상과 Tier 0 안전장치(2회 연속 확인·교체 후 감시·비차단 틱) 검증."""

from __future__ import annotations

import copy
import json
import math
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import candidate, recording, site  # noqa: E402, F401
from test_tier0_service import _settings  # noqa: E402
from services.tier0.clean_background import BackgroundCleanBackground  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.rotation_recovery import BackgroundRotationRecovery  # noqa: E402
from services.tier0.settings import (  # noqa: E402
    CleanBackgroundSettings,
    RotationRecoverySettings,
    Tier0ConfigError,
    load_settings,
)
from simai_ai.calibration.document import content_hash  # noqa: E402
from simai_ai.calibration.rotation_recovery import (  # noqa: E402
    RotationConfig,
    angle,
    extract_features,
    fixed_center_camera,
    make_candidate,
    validate_rotation_candidate,
)
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.tracking.local_tracker import CameraDetections  # noqa: E402


@pytest.mark.parametrize("model,coeffs", [
    ("NONE", []), ("OPENCV", [0.1, 0, 0, 0]),
    ("OPENCV_PINHOLE", [0.1, 0, 0, 0]), ("OPENCV_FISHEYE", [0.1, 0, 0, 0]),
])
def test_extract_features_contract_distortion(model, coeffs):
    rng = np.random.default_rng(494)
    image = rng.integers(0, 256, (256, 256), dtype=np.uint8)
    camera = {"image_size": [256, 256], "K": [[200, 0, 128], [0, 200, 128], [0, 0, 1]],
              "distortion": {"model": model, "coeffs": coeffs}}
    result = extract_features(image, np.full_like(image, 255), camera, RotationConfig())
    assert len(result.points) > 0
    raw = extract_features(image, np.full_like(image, 255),
                           {**camera, "distortion": {"model": "NONE", "coeffs": []}}, RotationConfig())
    if model != "NONE":
        points = raw.points[:, None]
        k = np.asarray(camera["K"], dtype=float)
        d = np.asarray(coeffs, dtype=float)
        expected = (cv2.fisheye.undistortPoints(points, k, d, P=k) if model == "OPENCV_FISHEYE"
                    else cv2.undistortPoints(points, k, d, P=k))[:, 0]
        np.testing.assert_allclose(result.points, expected)


def test_scaled_recent_background_respects_occlusion_and_variance():
    from types import SimpleNamespace
    from simai_ai.calibration.world_frame.clean_background import _Sample

    frames = []
    for i in range(4):
        image = np.full((128, 128), 80, np.uint8)
        mask = np.ones_like(image, dtype=bool)
        if i < 2:
            image[20:40, 20:40] = 250
            mask[20:40, 20:40] = False
        if i < 3:
            mask[90:110, 90:110] = False
        image[60:80, 60:80] = 0 if i < 2 else 200
        frames.append(_Sample(frame_bgr=image, reveal_mask=mask, capture_time_ms=i, person_cover_ratio=0, byte_size=0))
    worker = BackgroundCleanBackground.__new__(BackgroundCleanBackground)
    worker._rotation_side = 64
    worker._rotation_lock = threading.Lock()
    worker._rotation_samples = {}
    worker.accumulator = SimpleNamespace(_samples={"a": frames}, config=SimpleNamespace(rows_per_chunk=64))
    worker.settings = SimpleNamespace(min_samples_per_camera=4)
    small = worker._small_samples("a", frames)
    from simai_ai.calibration.rotation_worker import synthesize_samples
    bg, valid = synthesize_samples([(s[1], s[2]) for s in small])
    end = frames[-1].capture_time_ms
    assert end == 3 and bg.shape == (64, 64)
    assert bg[15, 15] == 80 and valid[15, 15] == 255
    assert bg[35, 35] == 0 and valid[35, 35] == 0
    assert bg[50, 50] == 0 and valid[50, 50] == 0
    assert worker._small_samples("a", frames)[0] is small[0]


def test_reference_cache_changes_with_version_and_camera(monkeypatch, tmp_path):
    from simai_ai.calibration.rotation_worker import RotationComputation

    cam = {"camera_id": "a", "image_size": [2, 2], "K": [1]}
    req = {"document": {"calibration_version": "v1", "cameras": [cam]},
           "references": {"a": {"key": ("v1", 0), "camera": cam, "image": "image", "mask": "mask"}},
           "recent": {"a": {"signature": (0, 0, 3), "size": (2, 2),
                              "samples": [(np.zeros((2, 2), np.uint8), np.ones((2, 2), bool))] * 3}},
           "recent_ms": {"a": 3}, "now_ms": 3, "version": "out", "config": RotationConfig(), "budget_ms": 500}
    computation = RotationComputation()
    calls = []
    monkeypatch.setattr(cv2, "imread", lambda *_: np.zeros((2, 2), dtype=np.uint8))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.extract_features",
                        lambda *_, **kw: calls.append(1) or object())
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.reference_features",
                        lambda *_, **kw: (calls.append(1) or object(), RotationConfig()))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.make_candidate", lambda *_, **kw: (None, {"status": "STABLE"}))
    assert computation.compute(req)["status"] == "COMPLETED"
    computation.compute(req)
    assert len(calls) == 2  # 기준과 최근 각 한 번
    req["references"]["a"]["key"] = ("v2", 0)
    computation.compute(req)
    assert len(calls) == 3
    req["references"]["a"]["key"] = ("v2", 1)
    computation.compute(req)
    assert len(calls) == 4
    req["recent"]["a"]["signature"] = (0, 1, 4)
    computation.compute(req)
    assert len(calls) == 5
    cam["K"] = [2]
    computation.compute(req)
    assert len(calls) == 6

    # 추출 직후 예산을 넘겨도 같은 서명은 다음 주기에 완료한 특징을 재사용한다.
    computation.recent.clear()
    fake_cpu = [0.]
    computation.clock = lambda: fake_cpu[0]
    def costly_extract(*args, **kwargs):
        calls.append(1)
        fake_cpu[0] += .6
        return object()
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.extract_features", costly_extract)
    assert computation.compute(req)["status"] == "BUDGET_DEFERRED"
    assert len(calls) == 7
    assert computation.compute(req)["status"] == "COMPLETED"
    assert len(calls) == 7


@pytest.mark.parametrize("coarse_count,expected", [(120, 320), (0, 960)])
def test_reference_uses_coarse_features_only_when_sufficient(monkeypatch, coarse_count, expected):
    from types import SimpleNamespace
    from simai_ai.calibration.rotation_worker import reference_features
    calls = []
    def extract(image, mask, camera, config, **kwargs):
        calls.append(config.max_image_side)
        return SimpleNamespace(points=np.zeros((coarse_count if len(calls) == 1 else 120, 2)))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.extract_features", extract)
    image = np.zeros((540, 960, 3), np.uint8)
    mask = np.full((540, 960), 255, np.uint8)
    _, config = reference_features(image, mask, {"image_size": [960, 540]}, RotationConfig())
    assert config.max_image_side == expected
    assert calls == ([320] if expected == 320 else [320, 960])
    with pytest.raises(ValueError, match="STREAM_CHANGED"):
        reference_features(image, mask, {"image_size": [1920, 1080]}, RotationConfig())


def test_synthesis_rejects_chromatic_changes_and_masked_outliers():
    from simai_ai.calibration.rotation_worker import synthesize_samples
    a = np.full((16, 16, 3), (255, 0, 0), np.uint8)
    b = np.full_like(a, (0, 49, 0))  # 거의 같은 회색조, 크게 다른 색
    mask = np.ones((16, 16), bool)
    _, valid = synthesize_samples([(a, mask), (a, mask), (b, mask), (b, mask)])
    assert not valid.any()
    partial = mask.copy()
    partial[:, :8] = False
    bg, valid = synthesize_samples([(a, mask), (a, mask), (b, partial), (b, partial)])
    assert (valid[:, :8] == 255).all() and not valid[:, 8:].any()
    assert (bg[:, :8] == cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)[:, :8]).all()


def _euler_matrix(rx_deg: float = 0.0, ry_deg: float = 0.0, rz_deg: float = 0.0) -> np.ndarray:
    rx, ry, rz = map(math.radians, (rx_deg, ry_deg, rz_deg))
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    Rx = np.array([[1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx]])
    Ry = np.array([[cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy]])
    Rz = np.array([[cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0]])
    return Rz @ Ry @ Rx


def _build_3d_landmarks() -> tuple[np.ndarray, list[np.ndarray]]:
    """4대 카메라 공통 시야([1.6..7.4] x [1.3..6.2] x [0.1..1.9]m) 안의 3D 랜드마크와 고유 SIFT 패치."""
    rng = np.random.default_rng(20260411)
    xs = np.linspace(1.6, 7.4, 6)
    ys = np.linspace(1.3, 6.2, 5)
    zs = np.linspace(0.1, 1.9, 4)
    grid = np.array([[x, y, z] for x in xs for y in ys for z in zs], dtype=float)
    jitter = rng.uniform(-0.12, 0.12, size=grid.shape)
    pts3d = grid + jitter

    rad = 22
    yy, xx = np.mgrid[-rad : rad + 1, -rad : rad + 1].astype(np.float32)
    rr = np.hypot(xx, yy)
    tt = np.arctan2(yy, xx)
    peak = np.exp(-(rr**2) / (2.0 * 1.9**2))
    ring_env = np.exp(-((rr - 8.5) ** 2) / (2.0 * 3.0**2))

    patches: list[np.ndarray] = []
    for _ in range(len(pts3d)):
        mod = np.zeros_like(tt)
        for k in range(1, 6):
            amp = float(rng.uniform(0.2, 1.0))
            phase = float(rng.uniform(0.0, 2.0 * math.pi))
            mod += amp * np.cos(k * tt + phase)
        mod = mod / (np.max(np.abs(mod)) + 1e-6)
        sign = 1.0 if rng.random() > 0.5 else -1.0
        val = np.clip(128.0 + sign * 95.0 * peak + 38.0 * ring_env * mod, 5.0, 250.0).astype(np.float32)
        patches.append(np.stack([val, val, val], axis=-1))
    return pts3d, patches


PTS3D, PATCHES = _build_3d_landmarks()
RAD = 22
_YY, _XX = np.mgrid[-RAD : RAD + 1, -RAD : RAD + 1].astype(np.float32)
ALPHA = np.clip((RAD - np.hypot(_XX, _YY)) / 5.0, 0.0, 1.0)[..., None]


def _render_plane_view(camera: dict) -> np.ndarray:
    """카메라 투영 행렬로 3D 랜드마크를 서브픽셀 정밀도로 투영한 기준 배경 영상을 생성한다."""
    w, h = camera["image_size"]
    K = np.asarray(camera["K"], dtype=float)
    R = np.asarray(camera["R"], dtype=float)
    t = np.asarray(camera["t"], dtype=float)
    img = np.full((h, w, 3), 128, dtype=np.uint8)
    Xc = (R @ PTS3D.T).T + t
    uv = (K @ Xc.T).T
    u = uv[:, 0] / uv[:, 2]
    v = uv[:, 1] / uv[:, 2]
    for idx in range(len(PTS3D)):
        if Xc[idx, 2] <= 0.5:
            continue
        ui, vi = int(round(u[idx])), int(round(v[idx]))
        if not (RAD + 2 <= ui < w - RAD - 2 and RAD + 2 <= vi < h - RAD - 2):
            continue
        du, dv = float(u[idx] - ui), float(v[idx] - vi)
        M = np.float32([[1.0, 0.0, du], [0.0, 1.0, dv]])
        shifted = cv2.warpAffine(
            PATCHES[idx],
            M,
            (2 * RAD + 1, 2 * RAD + 1),
            flags=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        roi = img[vi - RAD : vi + RAD + 1, ui - RAD : ui + RAD + 1].astype(np.float32)
        img[vi - RAD : vi + RAD + 1, ui - RAD : ui + RAD + 1] = (
            roi * (1.0 - ALPHA) + shifted * ALPHA
        ).astype(np.uint8)
    return img


def _warp_pure_rotation(image: np.ndarray, camera: dict, delta_R: np.ndarray) -> np.ndarray:
    """기준 배경 영상에 같은 카메라 순수 회전 호모그래피 H = K * delta_R * K^-1 를 적용한다."""
    w, h = camera["image_size"]
    K = np.asarray(camera["K"], dtype=float)
    H = K @ np.asarray(delta_R, dtype=float) @ np.linalg.inv(K)
    return cv2.warpPerspective(image, H, (w, h), flags=cv2.INTER_LINEAR, borderValue=(128, 128, 128))


def _render_parallax_translation(camera: dict, shift_world: np.ndarray) -> np.ndarray:
    """카메라 중심 이동(shift_world)으로 다중 깊이 3D 랜드마크에 실제 시차(parallax)가 발생한 영상을 생성한다."""
    moved = copy.deepcopy(camera)
    R = np.asarray(camera["R"], dtype=float)
    C = -R.T @ np.asarray(camera["t"], dtype=float) + np.asarray(shift_world, dtype=float)
    moved["t"] = (-R @ C).tolist()
    return _render_plane_view(moved)


def _feed_clean_background(
    worker: BackgroundCleanBackground,
    images: dict[str, np.ndarray],
    *,
    start_ms: int,
    count: int = 3,
    step_ms: int = 100,
) -> int:
    t = start_ms
    for _ in range(count):
        t += step_ms
        for cid, img in images.items():
            frame = Frame(cid, t, "FILE", {"stream_epoch": 1, "frame_sequence": t}, image=img.copy())
            det = CameraDetections(cid, 1, t, t, "FILE", None, ())
            assert worker.offer(frame, det)
            assert worker.process_one()
    return t


@pytest.mark.parametrize(
    "angles_deg",
    [
        (2.5, -1.5, 1.0),   # 약 3.1° 소각 회전
        (-4.0, 5.5, -3.0),  # 약 7.4° 삼각대 헤드 처짐+회전
    ],
)
def test_synthetic_homography_rotation_recovery_accuracy(site, angles_deg):
    doc = copy.deepcopy(site.service.calibration.current())
    doc["provider_source"] = "G1_AUTO"
    doc["state"] = "CALIBRATED"
    doc["content_hash"] = content_hash(doc)
    config = RotationConfig()

    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    references = {}
    for cam in doc["cameras"]:
        cid = cam["camera_id"]
        mask = np.full((cam["image_size"][1], cam["image_size"][0]), 255, dtype=np.uint8)
        references[cid] = (cam, extract_features(ref_images[cid], mask, cam, config))

    target_cid = "cam3"
    delta_true = _euler_matrix(*angles_deg)
    true_angle_deg = math.degrees(angle(delta_true))
    assert 0.5 < true_angle_deg < 15.0

    recent_features = {}
    recent_ms = {}
    for cam in doc["cameras"]:
        cid = cam["camera_id"]
        mask = np.full((cam["image_size"][1], cam["image_size"][0]), 255, dtype=np.uint8)
        img = _warp_pure_rotation(ref_images[cid], cam, delta_true) if cid == target_cid else ref_images[cid]
        recent_features[cid] = extract_features(img, mask, cam, config)
        recent_ms[cid] = 10_000

    candidate_doc, report = make_candidate(
        doc, references, recent_features, recent_ms, at_ms=10_000, version="cal-rot-test", config=config
    )
    assert candidate_doc is not None, report
    assert report["status"] == "PROPOSED"
    assert report["changed_camera_ids"] == [target_cid]
    fit = report["rotations"][target_cid]
    assert fit["status"] == "ROTATION"
    assert fit["singular_ratio"] >= config.min_singular_ratio
    assert max(fit["residual_p95_px"], fit["rotation_residual_p95_px"]) <= config.max_residual_px

    # 복구된 자세와 실제 회전 행렬 사이의 각도 오차 < 0.15° 검증 및 카메라 중심 C 보존 검증
    old_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    new_cam = next(c for c in candidate_doc["cameras"] if c["camera_id"] == target_cid)
    expected_cam = fixed_center_camera(old_cam, delta_true)
    err_deg = math.degrees(angle(np.asarray(new_cam["R"]) @ np.asarray(expected_cam["R"]).T))
    assert err_deg < 0.15

    center = lambda c: -np.asarray(c["R"]).T @ np.asarray(c["t"])
    assert np.allclose(center(new_cam), center(old_cam), atol=1e-6, rtol=0)

    # 인접 카메라 쌍 재투영 오차가 유의미하게 감소했는지 확인
    neighbors = [v for v in report["pairs"].values() if target_cid in v["camera_ids"]]
    assert neighbors
    for pair_metric in neighbors:
        assert pair_metric["after"]["median_px"] < pair_metric["before"]["median_px"] * 0.5
        assert pair_metric["after"]["p95_px"] <= config.max_pair_p95_px

    report["consecutive_confirmation"] = {"count": 2, "required": 2, "validation_end_ms": 10_000}
    validation = validate_rotation_candidate(candidate_doc, report, doc)
    assert validation == {"accepted": True, "method": "BACKGROUND_FIXED_CENTER_ROTATION", "changed_camera_ids": [target_cid]}


@pytest.fixture(autouse=True)
def _no_background_daemon_loops(monkeypatch):
    workers = []
    original = BackgroundRotationRecovery.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        workers.append(self)
    monkeypatch.setattr(BackgroundRotationRecovery, "__init__", initialize)
    monkeypatch.setattr(BackgroundCleanBackground, "run", lambda self: None)
    monkeypatch.setattr(BackgroundRotationRecovery, "run", lambda self: None)
    yield
    for worker in workers:
        worker.close()


def _setup_service_with_rotation_worker(site):
    service = site.service
    init_doc, init_report = candidate(service, version="cal-field-init")
    init_doc[" cameras" if False else "cameras"] = copy.deepcopy(service.calibration.current()["cameras"])
    init_doc["provider_source"] = "G1_AUTO"
    init_doc["content_hash"] = content_hash(init_doc)
    init_report["calibration"]["content_hash"] = init_doc["content_hash"]
    assert service.propose_calibration(init_doc, init_report)
    site.tick()
    service._swap_probation = None
    doc = copy.deepcopy(service.calibration.current())

    cb_settings = replace(
        CleanBackgroundSettings(),
        sample_interval_ms=100,
        min_samples_per_camera=3,
        max_samples_per_camera=4,
    )
    prov = {"frames": {"source": "FILE", "dataset_id": "synthetic-rot"}, "is_upper_bound": True}
    cb = BackgroundCleanBackground(doc, prov, settings=cb_settings, state_dir=site.settings.state_dir, stop=service._stop)
    rot_settings = RotationRecoverySettings(enabled=True, safety_enabled=True, interval_ms=1_000, cpu_budget_ms=5_000)
    rot = BackgroundRotationRecovery(
        doc,
        prov,
        cb,
        settings=rot_settings,
        now_ms=service.clock.now_ms,
        stop=service._stop,
        propose=service._propose_rotation_calibration,
        on_drift_report=service._apply_drift_report,
        on_requires_setup=service._record_requires_setup,
        in_probation=lambda: service._swap_probation is not None,
        reference_document=lambda: service.calibration.safety_reference()[0],
    )
    service.clean_background = cb
    service.rotation_recovery = rot
    return service, doc, cb, rot


@pytest.mark.parametrize("failure_mode", ["translation_parallax", "large_rotation"])
def test_translation_and_large_rotation_require_setup_without_auto_overwrite(site, failure_mode):
    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000

    # 기준 배경(STABLE) 확인
    parent_threads = cv2.getNumThreads()
    res0 = rot.run_once()
    assert res0["status"] == "STABLE"
    import os
    assert rot.worker_pid != os.getpid()
    assert rot.status()["cpu_scope"] == "ROTATION_WORKER_PROCESS"
    assert cv2.getNumThreads() == parent_threads
    pid = rot.worker_pid

    target_cid = "cam3"
    target_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    disturbed = dict(ref_images)
    if failure_mode == "translation_parallax":
        disturbed[target_cid] = _render_parallax_translation(target_cam, np.array([0.45, -0.35, 0.25]))
    else:
        disturbed[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, _euler_matrix(12.0, -13.0, 5.0))

    t_ms = _feed_clean_background(cb, disturbed, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000

    # 1차 감지: 연속 확인(2회) 전에는 SUSPECT(CONFIRMING)로 두고 아직 DRIFTED/re_setup을 확정하지 않는다
    res1 = rot.run_once()
    assert rot.worker_pid == pid
    assert res1["status"] == "REQUIRES_SETUP"
    assert res1["camera_id"] == target_cid
    assert rot.last_status == "CONFIRMING"
    assert res1["consecutive_confirmation"]["count"] == 1
    assert service.calibration.state != "DRIFTED"
    assert service.re_setup_field() == []
    assert target_cid not in service.perception.excluded_cameras

    # 새 표본 도착 뒤 2차 감지: 2회 연속 확인 완료 -> DRIFTED 확정, health.re_setup 등재, 3D 융합 제외
    t_ms = _feed_clean_background(cb, disturbed, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    res = rot.run_once()
    assert rot.worker_pid == pid
    assert res["status"] == "REQUIRES_SETUP"
    assert res["camera_id"] == target_cid
    assert rot.last_status == "REQUIRES_SETUP"
    assert res["consecutive_confirmation"]["count"] == 2
    assert service._pending_calibration is None
    assert service.rig.calibration_version == doc["calibration_version"]
    assert service.calibration.state == "DRIFTED"
    assert service.calibration_status["re_setup_required"] is True
    assert "현장 세팅 다시 하기" in service.calibration_status["re_setup_reason"]
    assert service.health.payload()["calibration_state"] == "DRIFTED"
    assert target_cid in service.perception.excluded_cameras

    # 보고 필드(health.re_setup, 계약 2.2.0): 틀어진 카메라만, 사유·감지 시각과 함께 싣는다
    health = service.health.payload()
    [item] = health["re_setup"]
    assert item["camera_id"] == target_cid and item["reason"] == res["reason"]
    assert 0 < item["detected_at"] <= service.clock.now_ms()
    assert schema_errors(Envelope(service.settings.gateway_id, service.clock).make("health", health), "health") == []

    # I1 calibration_update 전환 기록 검증
    last_transition = service.calibration.transitions[-1]["payload"]
    assert last_transition["calibration_state"] == "DRIFTED"
    assert last_transition["reason"] == "DRIFT_DETECTED"
    envelope = Envelope(service.settings.gateway_id, service.clock)
    assert schema_errors(envelope.make("calibration_update", last_transition), "calibration_update") == []

    # 틀어진 카메라의 최근 배경 누적 창만 reset 되고 다른 카메라는 유지된다
    cb.process_one()
    assert cb.accumulator.camera_status(target_cid).sample_count == 0
    for cid in ref_images:
        if cid != target_cid:
            assert cb.accumulator.camera_status(cid).sample_count >= 3


def test_safety_gates_confirmation_swap_reset_and_probation_rollback(site):
    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    target_cid = "cam3"
    target_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    delta_first = _euler_matrix(2.5, -1.5, 1.0)
    rotated_images = dict(ref_images)
    rotated_images[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, delta_first)

    # 1차 회전 관측 -> CONFIRMING (count=1, 아직 교체하지 않음)
    t_ms = _feed_clean_background(cb, rotated_images, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r1 = rot.run_once()
    assert r1["status"] == "PROPOSED"
    assert rot.last_status == "CONFIRMING"
    assert rot.confirmation.count == 1
    assert service._pending_calibration is None

    # 도중에 카메라가 추가로 흔들려 각도 차가 confirmation_tolerance_rad(0.5°)를 넘으면 확인 카운트가 1로 리셋됨
    delta_settled = _euler_matrix(-4.0, 5.5, -3.0)
    settled_images = dict(ref_images)
    settled_images[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, delta_settled)
    t_ms = _feed_clean_background(cb, settled_images, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rot.run_once()
    assert rot.last_status == "CONFIRMING"
    assert rot.confirmation.count == 1
    assert service._pending_calibration is None

    # 같은 자세의 새 표본 도착 -> 2회 연속 확인 완료 -> 후보 채택 및 회전 카메라 최근 창만 reset
    ref_bytes_before = (cb._reference[target_cid][0] / f"{target_cid}.jpg").read_bytes()
    t_ms = _feed_clean_background(cb, settled_images, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r2 = rot.run_once()
    assert rot.last_status == "PROPOSED"
    assert rot.confirmation.count == 2
    assert service._pending_calibration is not None
    cb.process_one()
    assert cb.accumulator.camera_status(target_cid).sample_count == 0
    assert all(cb.accumulator.camera_status(cid).sample_count >= 3 for cid in ref_images if cid != target_cid)
    assert (cb._reference[target_cid][0] / f"{target_cid}.jpg").read_bytes() == ref_bytes_before

    # 다음 판정 tick 경계에서 원자적으로 새 calibration_version 활성화 및 SwapProbation(60초) 시작
    site.tick()
    swapped_version = service.rig.calibration_version
    assert swapped_version == r2["calibration"]["version"]
    assert service._swap_probation is not None
    assert service.calibration_status["probation"] == "교체 뒤 감시 중"
    assert service.calibration.safety_reference()[1] == (target_cid,)

    # 교체 직후 60초 감시 기간 안에 배경이 다시 틀어져(예: 원위치 복귀/추가 처짐) 10초 이상 지속되면 자동 롤백
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rot.run_once()
    assert rot.last_status == "PROBATION_BAD"
    assert service._swap_probation is not None  # 아직 10초 지속 전

    t_ms = _feed_clean_background(cb, ref_images, start_ms=t_ms + 10_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rot.run_once()
    assert rot.last_status == "PROBATION_BAD"
    assert service.calibration_status["probation"] == "악화 지속: 되돌림 대기"
    assert service._swap_probation is None

    # 다음 tick에 롤백 버전이 활성화되고 기존 자세가 복원되며 자동 복구는 보류된다
    site.tick()
    restored = service.calibration.current()
    assert restored["calibration_version"].startswith("cal-rollback-")
    assert restored["cameras"] == doc["cameras"]
    assert "되돌림 뒤" in rot.blocked_reason


def test_judgment_tick_never_blocked_and_cpu_budget_defers_cleanly(site, monkeypatch):
    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    rot.settings = replace(rot.settings, cpu_budget_ms=10)
    entered, release = threading.Event(), threading.Event()
    worker: threading.Thread | None = None
    try:
        # 1) CPU 예산 초과 시 예외로 죽지 않고 BUDGET_DEFERRED로 다음 주기에 이월하는지 검증
        from simai_ai.calibration.rotation_worker import RotationComputation
        fake_cpu = [0.0]
        def step_cpu():
            fake_cpu[0] += .006
            return fake_cpu[0]
        computation = RotationComputation(cpu_clock=step_cpu)
        monkeypatch.setattr(rot, "_compute", computation.compute)
        ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
        t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
        site.elapsed[0] = t_ms - 1_760_342_400_000
        deferred = rot.run_once()
        assert deferred["status"] == "BUDGET_DEFERRED"
        assert rot.budget_deferrals == 1
        assert set(rot.status()["last_cpu_ms"]) == {
            "reference_read", "reference_extract", "recent_prepare", "recent_extract",
            "matching", "estimation", "total",
        }

        # 2) 회전 복구 스레드가 무거운 계산 중이어도 판정 tick은 절대 기다리지 않는다

        def slow_load_references(*args, **kwargs):
            entered.set()
            assert release.wait(5)
            return None

        monkeypatch.setattr(rot, "_load_references", slow_load_references)
        worker = threading.Thread(target=rot.run_once)
        worker.start()
        assert entered.wait(2)
        started = time.perf_counter()
        for _ in range(12):
            site.tick()
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5
    finally:
        release.set()
        if worker is not None:
            worker.join(3)


@pytest.mark.parametrize(
    "bad_cfg",
    [
        {"enabled": "yes"},
        {"interval_ms": 0},
        {"cpu_budget_ms": -5},
        {"min_angle_rad": 0.5, "max_angle_rad": 0.1},
        {"min_singular_ratio": 1.2},
        {"unknown_threshold": 1.0},
    ],
)
def test_rotation_recovery_settings_validation(tmp_path, recording, bad_cfg):
    _settings(tmp_path, recording, anchor_ms=1000)
    path = tmp_path / "tier0.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["rotation_recovery"] = bad_cfg
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Tier0ConfigError):
        load_settings(path, env={})

    doc["rotation_recovery"] = {
        "enabled": True,
        "interval_ms": 10_000,
        "cpu_budget_ms": 300,
        "min_angle_rad": math.radians(0.8),
        "max_residual_px": 2.5,
        "min_singular_ratio": 0.985,
    }
    path.write_text(json.dumps(doc), encoding="utf-8")
    loaded = load_settings(path, env={})
    assert loaded.rotation_recovery.interval_ms == 10_000
    assert loaded.rotation_recovery.cpu_budget_ms == 300
    assert math.isclose(loaded.rotation_recovery.config.min_angle_rad, math.radians(0.8))
    assert math.isclose(loaded.rotation_recovery.config.max_residual_px, 2.5)
    assert math.isclose(loaded.rotation_recovery.config.min_singular_ratio, 0.985)


def test_re_setup_field_keeps_first_detection_and_clears_when_new_calibration_activates(site):
    """health.re_setup(-480): 같은 카메라가 다시 감지돼도 처음 감지 시각을 지키고 사유만 갱신한다.
    새 보정이 활성화되면 비워 '해소'를 알린다(빈 배열). 판정(system_health 의 DEGRADED 하한 등)은 이 필드와 무관하다."""
    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    assert service.re_setup_field() == [] and service.health.payload()["re_setup"] == []

    service._record_requires_setup("cam3", "매칭 부족")
    [first] = service.re_setup_field()
    site.elapsed[0] += 1_000
    service._record_requires_setup("cam3", "시차 잔차 초과")
    service._record_requires_setup("cam1", "매칭 부족")
    items = service.re_setup_field()
    assert [i["camera_id"] for i in items] == ["cam1", "cam3"]  # camera_id 오름차순
    cam3 = next(i for i in items if i["camera_id"] == "cam3")
    assert cam3["detected_at"] == first["detected_at"] and cam3["reason"] == "시차 잔차 초과"
    assert next(i for i in items if i["camera_id"] == "cam1")["detected_at"] > first["detected_at"]

    new_doc, new_report = candidate(service, version="cal-after-resetup")
    assert service.propose_calibration(new_doc, new_report)
    site.tick()
    assert service.rig.calibration_version == "cal-after-resetup"
    assert service.re_setup_field() == [] and service.health.payload()["re_setup"] == []
    assert service.calibration_status["re_setup_required"] is False
    assert service.perception.excluded_cameras == ()


def _synthetic_person_observation_frames(rig, t_ms: int, *, rotated_cameras: dict[str, np.ndarray] | None = None):
    """4대 카메라 시야 안의 합성 사람 1명(17관절)을 투영해 Observation2DFrame 목록을 만든다."""
    from simai_ai.geometry import distortion
    from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection

    points = np.array([4.0, 3.0, 0.0]) + np.column_stack(
        [np.zeros(17), np.linspace(-0.15, 0.15, 17), np.linspace(0.1, 1.7, 17)]
    )
    out = []
    for cid, camera in rig.cameras.items():
        xc = camera.to_camera(points)
        if rotated_cameras and cid in rotated_cameras:
            xc = xc @ rotated_cameras[cid].T
        in_front = xc[:, 2] > 0.2
        if not np.all(in_front):
            # 카메라가 크게 돌아가 사람이 시야 밖으로 벗어난 경우 검출 없음
            out.append(
                Observation2DFrame(
                    camera_id=cid,
                    stream_epoch=1,
                    frame_sequence=t_ms,
                    estimated_capture_time_ms=t_ms,
                    timestamp_source="SIM",
                    timestamp_uncertainty_ms=0.0,
                    detections=(),
                )
            )
            continue
        xy = distortion.distort(xc[:, :2] / xc[:, 2:3], camera.distortion_model, camera.distortion_coeffs)
        uv = (np.column_stack([xy, np.ones(17)]) @ camera.K.T)[:, :2]
        w, h = camera.image_size
        if not np.all((uv[:, 0] >= 0) & (uv[:, 0] < w) & (uv[:, 1] >= 0) & (uv[:, 1] < h)):
            out.append(
                Observation2DFrame(
                    camera_id=cid,
                    stream_epoch=1,
                    frame_sequence=t_ms,
                    estimated_capture_time_ms=t_ms,
                    timestamp_source="SIM",
                    timestamp_uncertainty_ms=0.0,
                    detections=(),
                )
            )
            continue
        kps = tuple((float(u), float(v), 0.95) for u, v in uv)
        bbox = (
            float(np.min(uv[:, 0]) - 10),
            float(np.min(uv[:, 1]) - 10),
            float(np.max(uv[:, 0]) + 10),
            float(np.max(uv[:, 1]) + 10),
        )
        det = TrackedDetection("local-1", PoseDetection(bbox, 0.95, kps))
        out.append(
            Observation2DFrame(
                camera_id=cid,
                stream_epoch=1,
                frame_sequence=t_ms,
                estimated_capture_time_ms=t_ms,
                timestamp_source="SIM",
                timestamp_uncertainty_ms=0.0,
                detections=(det,),
            )
        )
    return out


def test_reproduce_severe_drift_empty_and_with_person_and_confirm_background_detection(site):
    """지시서 1·2: 카메라 1대가 30° 회전하거나 완전히 다른 장면을 비출 때(빈 방 / 사람 있음):
    1) 사람 관절 기반 ReprojectionMonitor(drift_watch)는 30° 회전 시 CrossViewAssociator 게이트에서 탈락해
       3+ 카메라 클러스터 표본이 0개(INSUFFICIENT_DATA)라 감지하지 못함을 재현한다.
    2) 사람 없는 배경 전용 비교(BackgroundRotationRecovery)는 30° 회전과 완전히 다른 장면 모두
       2회 연속 확인 뒤 DRIFTED 및 health.re_setup 으로 확정하고 3D 융합에서 제외한다."""
    from services.tier0.drift_watch import person_observations
    from simai_ai.calibration.monitor import MonitorConfig, MonitorStatus, ReprojectionMonitor
    from simai_ai.tracking.cross_view import CrossViewAssociator, LocalTrackletBuffer

    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    target_cid = "cam3"
    target_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    rot_30 = _euler_matrix(18.0, -24.0, 8.0)  # 약 30.2° 큰 회전
    assert math.degrees(angle(rot_30)) > 28.0

    # --- 1) 기존 사람 기반 감시(ReprojectionMonitor + CrossViewAssociator)의 한계 재현 ---
    monitor = ReprojectionMonitor(
        doc,
        MonitorConfig(window_ms=5_000, min_window_samples=3, baseline_min_samples=3, baseline_min_span_ms=500, sustain_ms=1_000),
    )
    buf = LocalTrackletBuffer()
    assoc_engine = CrossViewAssociator(service.rig)
    # 정상 상태에서 baseline 확립 (t=1000..3000)
    for step_t in (1_000, 1_500, 2_000, 2_500):
        for obs in _synthetic_person_observation_frames(service.rig, step_t):
            buf.add(obs)
        views, _ = buf.views_at(step_t)
        af = assoc_engine.associate(step_t, views)
        rep = monitor.observe(step_t, [p.views for p in person_observations(af)])
    assert all(c.baseline_median_px is not None for c in rep.cameras)

    # (a) 빈 방에서 cam3 가 30° 회전 (t=4000..7000): 사람 관측이 없으므로 NO_EVIDENCE
    rep_empty = monitor.observe(7_000, ())
    cam3_empty = next(c for c in rep_empty.cameras if c.camera_id == target_cid)
    assert cam3_empty.status is MonitorStatus.NO_EVIDENCE

    # (b) 사람이 있어도 cam3 가 30° 회전하면 CrossViewAssociator 게이트에서 cam3 가 빠져 3대 클러스터(cam1,cam2,cam4)만 형성됨
    for step_t in (14_000, 14_500, 15_000, 15_500, 16_000):
        # 시야 안에 남도록 작은 픽셀 이동이지만 에피폴라 게이트(30px)를 넘는 15° 회전 투영도 함께 확인
        for obs in _synthetic_person_observation_frames(service.rig, step_t, rotated_cameras={target_cid: _euler_matrix(0.0, 12.0, 0.0)}):
            buf.add(obs)
        views, _ = buf.views_at(step_t)
        af = assoc_engine.associate(step_t, views)
        for cluster in af.clusters:
            if len(cluster.views) >= 3:
                assert target_cid not in cluster.camera_ids
        rep_person = monitor.observe(step_t, [p.views for p in person_observations(af)])
    cam3_person = next(c for c in rep_person.cameras if c.camera_id == target_cid)
    assert cam3_person.status is MonitorStatus.NO_EVIDENCE
    assert cam3_person.window_samples == 0

    # --- 2) 배경 전용 비교: 30° 회전 및 완전히 다른 장면(바닥/벽 추락) 감지 ---
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    # Case A: 30° 회전 (특징점 일부가 겹쳐도 허용각 15° 초과 또는 매칭 붕괴로 REQUIRES_SETUP)
    rotated_30_images = dict(ref_images)
    rotated_30_images[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, rot_30)
    t_ms = _feed_clean_background(cb, rotated_30_images, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_30_1 = rot.run_once()
    assert r_30_1["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"
    assert service.calibration.state != "DRIFTED"

    # Case B: 완전히 다른 장면(쓰러져 바닥/벽만 비추는 무특징·이질 장면)으로 전환 -> 2회차 연속 확인으로 DRIFTED 확정
    fallen_scene = np.full_like(ref_images[target_cid], 42)
    cv2.line(fallen_scene, (0, 100), (fallen_scene.shape[1], 120), (60, 60, 60), 3)
    completely_different = dict(ref_images)
    completely_different[target_cid] = fallen_scene
    t_ms = _feed_clean_background(cb, completely_different, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_fall_2 = rot.run_once()
    assert r_fall_2["status"] == "REQUIRES_SETUP" and rot.last_status == "REQUIRES_SETUP"
    assert r_fall_2["rotations"][target_cid]["matches"] < rot.settings.config.min_matches
    assert service.calibration.state == "DRIFTED"
    assert target_cid in service.perception.excluded_cameras
    assert [item["camera_id"] for item in service.re_setup_field()] == [target_cid]


def test_multi_camera_fall_exclusion_and_under_two_cameras_null_position(site):
    """지시서 2·3·4(-540 및 -564):
    - 같은 판정 주기에 2대(cam2, cam3)가 동시에 실패하면 카메라 고장이 아니라 기준·환경 문제로 보고
      SUSPECT(health 경고)로만 남기며 융합에서 제외하지 않는다.
    - 시간차를 두고 1대씩(cam2 -> cam3) 쓰러지면 각 카메라는 DRIFTED·health.re_setup·융합 제외된다.
    - 남은 2대(cam1, cam4)만으로도 사람 3D World 위치·관절이 정상 삼각측량된다(드리프트 카메라 관측은 섞이지 않음).
    - DRIFTED 카메라가 이후 정상 배경/관측을 보내도 재세팅 전에는 절대 자동 복귀하지 않는다.
    - 추가로 cam1 까지 쓰러져 제외 후 남는 카메라가 2대 미만이 될 상황이면 cam1 은 융합에서 제외하지 않고
      health 및 calibration_status 에 감시 저하 사유를 남긴다.
    - 실제 관측 카메라가 1대(< 2대)뿐이면 3D 위치·관절을 지어내지 않고 null 처리한다.
    - 새 보정(재세팅)이 활성화되면 그제서야 제외가 풀리고 4대 융합이 복구된다."""
    from simai_ai.contact import SurfaceSet

    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    # 0) cam2, cam3 가 같은 판정 주기에 동시에 실패하면 SUSPECT 로만 기록되고 융합에서 제외되지 않는다(-564)
    cam3_cfg = next(c for c in doc["cameras"] if c["camera_id"] == "cam3")
    two_fallen = dict(ref_images)
    two_fallen["cam2"] = np.full_like(ref_images["cam2"], 30)
    two_fallen["cam3"] = _warp_pure_rotation(ref_images["cam3"], cam3_cfg, _euler_matrix(20.0, -22.0, 6.0))

    t_ms = _feed_clean_background(cb, two_fallen, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_sim = rot.run_once()
    assert r_sim["status"] == "SUSPECT"
    assert set(r_sim["suspect_cameras"]) == {"cam2", "cam3"}
    assert rot.last_status == "SUSPECT"
    assert service.perception.excluded_cameras == ()
    assert service.re_setup_field() == []

    # 1) 짧은 창 이후 1대씩 순차적으로 쓰러진 경우: 먼저 cam2 단독 추락 -> 2회 확인 뒤 DRIFTED 및 제외
    t_ms = _feed_clean_background(cb, ref_images, start_ms=t_ms + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    cam2_only = dict(ref_images)
    cam2_only["cam2"] = np.full_like(ref_images["cam2"], 30)
    t_ms = _feed_clean_background(cb, cam2_only, start_ms=t_ms + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r1 = rot.run_once()
    assert r1["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"
    assert service.perception.excluded_cameras == ()

    t_ms = _feed_clean_background(cb, cam2_only, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r2 = rot.run_once()
    assert r2["status"] == "REQUIRES_SETUP" and rot.last_status == "REQUIRES_SETUP"
    assert service.perception.excluded_cameras == ("cam2",)

    # 이어서 짧은 창(2*interval_ms=2000ms) 경과 뒤 cam3 단독 큰 회전 -> 2회 확인 뒤 DRIFTED 및 제외 (남은 활성 카메라 2대)
    t_ms = _feed_clean_background(cb, two_fallen, start_ms=t_ms + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r2_b1 = rot.run_once()
    assert r2_b1["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"

    t_ms = _feed_clean_background(cb, two_fallen, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r2_b2 = rot.run_once()
    assert r2_b2["status"] == "REQUIRES_SETUP" and rot.last_status == "REQUIRES_SETUP"
    assert [item["camera_id"] for item in service.re_setup_field()] == ["cam2", "cam3"]
    assert service.perception.excluded_cameras == ("cam2", "cam3")

    # R7 구역 감시 입력에서도 cam2, cam3 가 fusion_excluded=True 로 표시되는지 확인
    mon_state = service.judge._monitoring(service.judge.active_config)
    excluded_in_mon = {c.camera_id for c in mon_state.cameras if c.fusion_excluded}
    assert {"cam2", "cam3"} <= excluded_in_mon

    # 2) 남은 2대(cam1, cam4)와 틀어진 2대(cam2, cam3)가 모두 사람 관측을 보내도
    #    cam2, cam3 는 융합에서 배제되고 cam1, cam4 2대만으로 정확한 World 3D 트랙이 생성·갱신된다.
    base_t = max(service.perception.manager.last_t or 0, t_ms) + 1_000
    bad_rot = {"cam2": _euler_matrix(15.0, -10.0, 5.0), "cam3": _euler_matrix(-12.0, 14.0, -6.0)}
    for step_idx in range(1, 5):
        obs_t = base_t + step_idx * 100
        site.elapsed[0] = obs_t - 1_760_342_400_000
        service.perception.ingest(_synthetic_person_observation_frames(service.rig, obs_t, rotated_cameras=bad_rot))
        perceived = service.perception.step(obs_t, SurfaceSet(()))
    assert perceived is not None
    assert len(perceived.frame.tracks) == 1
    track = perceived.frame.tracks[0]
    assert track.cameras == ("cam1", "cam4")
    assert track.pelvis_xyz is not None
    assert np.linalg.norm(np.asarray(track.pelvis_xyz[:2]) - np.array([4.0, 3.0])) < 0.15

    # 3) cam2, cam3 가 다시 정상 배경을 보내도 재세팅 전에는 절대 자동 복구·복귀되지 않으며,
    #    그 상태에서 추가로 cam1 까지 쓰러지면 제외 후 남는 카메라가 1대(< 2대)가 되므로
    #    cam1 은 융합에서 제외하지 않고(excluded_cameras == ("cam2", "cam3")) health 에 감시 저하 사유를 남긴다(-564).
    cam1_fallen = dict(ref_images)
    cam1_fallen["cam1"] = np.full_like(ref_images["cam1"], 15)
    t_ms = _feed_clean_background(cb, cam1_fallen, start_ms=obs_t + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r3 = rot.run_once()
    assert r3["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"
    assert r3["camera_id"] == "cam1"

    t_ms = _feed_clean_background(cb, cam1_fallen, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r4 = rot.run_once()
    assert r4["status"] == "REQUIRES_SETUP" and rot.last_status == "REQUIRES_SETUP"
    assert [item["camera_id"] for item in service.re_setup_field()] == ["cam1", "cam2", "cam3"]
    assert service.perception.excluded_cameras == ("cam2", "cam3")
    assert "최소 카메라 2대" in (service.calibration_status.get("monitoring_degraded_reason") or "")
    cam1_re_setup = next(item for item in service.health.payload()["re_setup"] if item["camera_id"] == "cam1")
    assert "감시 저하" in cam1_re_setup["reason"]

    # 4) 실제 사람 관측이 1대(cam4, < 2대)에서만 들어오면 3D 위치·관절을 절대 지어내지 않고 null(UNKNOWN) 처리한다.
    #    extrapolation 창(500ms) 이후 기존 트랙의 pelvis_xyz 는 None 이 되고 관절도 모두 None 이다.
    single_cam_t = t_ms + 1_200
    site.elapsed[0] = single_cam_t - 1_760_342_400_000
    only_cam4_frames = [
        f
        for f in _synthetic_person_observation_frames(
            service.rig,
            single_cam_t,
            rotated_cameras={"cam1": _euler_matrix(85.0, 0.0, 0.0)},
        )
    ]
    service.perception.ingest(only_cam4_frames)
    perceived_single = service.perception.step(single_cam_t, SurfaceSet(()))
    assert perceived_single is not None
    for snap in perceived_single.frame.tracks:
        assert snap.skeleton is None
    for ws_track in perceived_single.payload["tracks"]:
        assert ws_track["pelvis_xyz"] is None
        assert ws_track.get("joints_3d") is None

    # 5) 재세팅(새 calibration_version 활성화)을 거치면 제외 목록과 re_setup 이 초기화되어 4대 융합이 복구된다.
    new_doc, new_report = candidate(service, version="cal-resetup-recovered")
    new_doc["cameras"] = copy.deepcopy(doc["cameras"])
    new_doc["provider_source"] = "G1_AUTO"
    new_doc["content_hash"] = content_hash(new_doc)
    new_report["calibration"]["content_hash"] = new_doc["content_hash"]
    assert service.propose_calibration(new_doc, new_report)
    site.tick()
    assert service.perception.excluded_cameras == ()
    assert service.re_setup_field() == []
    assert service.calibration_status.get("monitoring_degraded_reason") is None


def test_false_positive_prevention_occlusion_lighting_and_small_shake(site):
    """지시서 5: 오탐 방지 시험:
    1) 사람이 화면 일부(25% 가림 및 70% 과반 가림)를 가리는 경우 DRIFTED 가 되지 않는다(STABLE 또는 NO_EVIDENCE).
    2) 조명 밝기가 변하는 경우(창 전체 조도 감소/증가 및 창 내부 급격한 밝기 단차) DRIFTED 가 되지 않는다.
    3) 작은 흔들림(0.25° 미세 진동 -> STABLE, 3.1° 회전 -> PROPOSED, 1회성 일시 불량 -> 2회차 복귀 시 STABLE)은
       DRIFTED 로 빠지지 않는다."""
    from simai_ai.tracking.local_tracker import PoseDetection

    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    target_cid = "cam3"
    w, h = next(c["image_size"] for c in doc["cameras"] if c["camera_id"] == target_cid)

    # 1-A) 사람이 화면 일부를 가린 채 서 있는 경우 (검출 박스 마스킹 + 마스킹 안 된 국소 가림 동시 포함)
    for _ in range(4):
        t_ms += 100
        for cid, img in ref_images.items():
            frame_img = img.copy()
            dets = ()
            if cid == target_cid:
                # YOLO 검출로 마스킹되는 사람 영역
                bx1, by1, bx2, by2 = int(w * 0.28), int(h * 0.30), int(w * 0.45), int(h * 0.58)
                frame_img[by1:by2, bx1:bx2] = (35, 180, 210)
                dets = (PoseDetection((bx1, by1, bx2, by2), 0.92, (None,) * 17),)
                # 검출 박스에 잡히지 않은 작은 가방/물체 국소 가림 (70x70px)
                frame_img[int(h * 0.72) : int(h * 0.80), int(w * 0.65) : int(w * 0.72)] = 240
            frame = Frame(cid, t_ms, "FILE", {"stream_epoch": 1, "frame_sequence": t_ms}, image=frame_img)
            det = CameraDetections(cid, 1, t_ms, t_ms, "FILE", None, dets)
            assert cb.offer(frame, det)
            assert cb.process_one()
    site.elapsed[0] = t_ms - 1_760_342_400_000
    res_occ_partial = rot.run_once()
    assert res_occ_partial["status"] == "STABLE", res_occ_partial
    assert service.calibration.state != "DRIFTED"
    assert service.re_setup_field() == []

    # 1-B) 사람이 카메라 바로 앞(화면의 75%)을 가려 유효 배경이 부족한 경우 -> REQUIRES_SETUP 이 아니라 NO_EVIDENCE
    for _ in range(4):
        t_ms += 100
        for cid, img in ref_images.items():
            frame_img = img.copy()
            dets = ()
            if cid == target_cid:
                bx1, by1, bx2, by2 = int(w * 0.05), int(h * 0.05), int(w * 0.90), int(h * 0.90)
                frame_img[by1:by2, bx1:bx2] = 200
                dets = (PoseDetection((bx1, by1, bx2, by2), 0.95, (None,) * 17),)
            frame = Frame(cid, t_ms, "FILE", {"stream_epoch": 1, "frame_sequence": t_ms}, image=frame_img)
            det = CameraDetections(cid, 1, t_ms, t_ms, "FILE", None, dets)
            assert cb.offer(frame, det)
            assert cb.process_one()
    site.elapsed[0] = t_ms - 1_760_342_400_000
    res_occ_heavy = rot.run_once()
    assert res_occ_heavy["status"] == "NO_EVIDENCE", res_occ_heavy
    assert rot.last_status == "NO_EVIDENCE"
    assert service.calibration.state != "DRIFTED"
    assert service.re_setup_field() == []

    # 2) 조명 밝기 변화 (창 내부에서 조명이 갑자기 어두워지거나 밝아져 프레임 간 밝기 단차 +38/-35 가 발생한 경우)
    for step_i in range(4):
        t_ms += 100
        for cid, img in ref_images.items():
            frame_img = img.copy()
            if cid == target_cid:
                shift = -35.0 if step_i < 2 else 25.0  # 창 내 밝기 60단위 점프 (기존 분산 게이트는 variance=900>225 로 전부 탈락했음)
                frame_img = np.clip(frame_img.astype(np.float32) + shift, 0, 255).astype(np.uint8)
            frame = Frame(cid, t_ms, "FILE", {"stream_epoch": 1, "frame_sequence": t_ms}, image=frame_img)
            det = CameraDetections(cid, 1, t_ms, t_ms, "FILE", None, ())
            assert cb.offer(frame, det)
            assert cb.process_one()
    site.elapsed[0] = t_ms - 1_760_342_400_000
    res_light = rot.run_once()
    assert res_light["status"] == "STABLE", res_light
    assert service.calibration.state != "DRIFTED"
    assert service.re_setup_field() == []

    # 3-A) 1회성 일시 불량(예: 순간 블러/가림) 뒤 다음 주기에 정상 배경 복귀 시 연속 확인에서 기각되어 DRIFTED 가 되지 않음
    transient_bad = dict(ref_images)
    transient_bad[target_cid] = np.full_like(ref_images[target_cid], 20)
    t_ms = _feed_clean_background(cb, transient_bad, start_ms=t_ms, count=4)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_glitch = rot.run_once()
    assert r_glitch["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"
    assert service.calibration.state != "DRIFTED"

    # 다음 주기에 정상 배경으로 복귀 -> STABLE 로 리셋
    t_ms = _feed_clean_background(cb, ref_images, start_ms=t_ms, count=4)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_recover = rot.run_once()
    assert r_recover["status"] == "STABLE" and rot.last_status == "STABLE"
    assert rot.confirmation.drift_counts == {}
    assert service.calibration.state != "DRIFTED"

    # 3-B) 작은 흔들림(0.2° 미세 진동 -> STABLE, 3.1° 회전 -> 자동 복구 후보 CONFIRMING/PROPOSED)은 DRIFTED 가 되지 않음
    target_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    micro_shake = dict(ref_images)
    micro_shake[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, _euler_matrix(0.15, -0.1, 0.05))
    t_ms = _feed_clean_background(cb, micro_shake, start_ms=t_ms, count=4)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"

    small_rot = dict(ref_images)
    small_rot[target_cid] = _warp_pure_rotation(ref_images[target_cid], target_cam, _euler_matrix(2.5, -1.5, 1.0))
    t_ms = _feed_clean_background(cb, small_rot, start_ms=t_ms, count=4)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    r_small = rot.run_once()
    assert r_small["status"] == "PROPOSED" and rot.last_status == "CONFIRMING"
    assert service.calibration.state != "DRIFTED"
    assert service.re_setup_field() == []


def test_drift_crowd_suspect_and_min_two_cameras_protection(site):
    """S15P31A101-564 시험 (a)·(b)·(c)·(d):
    (a) 동시 다수 실패(같은 판정 주기 및 짧은 창 내 2대 이상 실패) -> SUSPECT(health 경고)로만 남기고 융합 제외 없음
    (b) 가림 많은 배경(사람 가림 비율·미노출 비율·정지 랜드마크 위 국소 혼잡 잔차) -> 판정 보류(NO_EVIDENCE), DRIFTED 승격 안 함
    (c) 1대 큰 회전(실제 쓰러짐) -> DRIFTED + 융합 제외 + 재세팅 배너 유지
    (d) 제외 후 남는 카메라가 2대 미만이면 제외하지 않고 health 에 감시 저하 사유 기록"""
    from simai_ai.calibration.rotation_recovery import fit_rotation
    from simai_ai.tracking.local_tracker import PoseDetection

    service, doc, cb, rot = _setup_service_with_rotation_worker(site)
    service.health._gateway_ok = lambda: True
    service.health._ec2_connected = lambda: True
    service.health._gate.apply = lambda report, **kw: report
    service.health._calibration_state = lambda: (
        "DRIFTED" if service.calibration.state == "DRIFTED" else "CALIBRATED"
    )
    ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
    t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    service.perception.ingest(_synthetic_person_observation_frames(service.rig, t_ms))
    service.watchdog.observe(t_ms, service._mono_ns())
    assert rot.run_once()["status"] == "STABLE"
    assert service.health.payload()["system_health"] == "NOMINAL"

    # --- (a) 동시 다수(3대: cam1, cam2, cam3) 실패 및 짧은 창 연속 실패 -> SUSPECT·제외 없음 ---
    three_failed = dict(ref_images)
    for cid in ("cam1", "cam2", "cam3"):
        three_failed[cid] = np.full_like(ref_images[cid], 25)
    t_ms = _feed_clean_background(cb, three_failed, start_ms=t_ms, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    ra1 = rot.run_once()
    assert ra1["status"] == "SUSPECT"
    assert set(ra1["suspect_cameras"]) == {"cam1", "cam2", "cam3"}
    assert rot.last_status == "SUSPECT"
    assert service.calibration.state != "DRIFTED"
    assert service.perception.excluded_cameras == ()
    assert service.re_setup_field() == []

    # 2회차 연속으로 3대가 또 실패해도 DRIFTED 승격이나 융합 제외 없이 SUSPECT 만 유지
    t_ms = _feed_clean_background(cb, three_failed, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    service.perception.ingest(_synthetic_person_observation_frames(service.rig, t_ms))
    service.watchdog.observe(t_ms, service._mono_ns())
    ra2 = rot.run_once()
    assert ra2["status"] == "SUSPECT"
    assert rot.last_status == "SUSPECT"
    assert service.calibration.state != "DRIFTED"
    assert service.perception.excluded_cameras == ()
    assert service.re_setup_field() == []
    assert service.calibration_status["suspect_cameras"] == ["cam1", "cam2", "cam3"]
    assert service.health.payload()["system_health"] == "DEGRADED"

    # 정상 복귀 후 상태 초기화 확인
    t_ms = _feed_clean_background(cb, ref_images, start_ms=t_ms + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    assert rot.run_once()["status"] == "STABLE"
    assert service.calibration_status.get("suspect_cameras") is None

    # --- (b) 13차 로비 혼잡 재현: 사람 가림 비율 높고 미검출 그림자/군중 노이즈가 섞인 배경 -> NO_EVIDENCE 보류 ---
    target_cid = "cam2"
    target_cam = next(c for c in doc["cameras"] if c["camera_id"] == target_cid)
    w, h = target_cam["image_size"]
    for step_idx in range(4):
        t_ms += 100
        for cid, img in ref_images.items():
            frame_img = img.copy()
            dets = ()
            if cid == target_cid:
                # 로비 하단·중단에 다수 일반인 분포(mean_person_cover_ratio >= 0.15)
                b1 = (int(w * 0.10), int(h * 0.45), int(w * 0.35), int(h * 0.85))
                b2 = (int(w * 0.45), int(h * 0.40), int(w * 0.72), int(h * 0.85))
                dets = (
                    PoseDetection(b1, 0.91, (None,) * 17),
                    PoseDetection(b2, 0.89, (None,) * 17),
                )
                # 마스크 경계 밖의 미검출 인파/그림자 노이즈로 일부 정적 패치 오염 (상단 배경은 그대로 유지)
                rng = np.random.default_rng(1300 + step_idx)
                frame_img[int(h * 0.55) : int(h * 0.95), int(w * 0.72) : int(w * 0.96)] = rng.integers(
                    10, 240, size=(int(h * 0.95) - int(h * 0.55), int(w * 0.96) - int(w * 0.72), 3), dtype=np.uint8
                )
            frame = Frame(cid, t_ms, "FILE", {"stream_epoch": 1, "frame_sequence": t_ms}, image=frame_img)
            det = CameraDetections(cid, 1, t_ms, t_ms, "FILE", None, dets)
            assert cb.offer(frame, det)
            assert cb.process_one()
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rb = rot.run_once()
    assert rb["status"] in ("STABLE", "NO_EVIDENCE"), rb
    assert service.calibration.state != "DRIFTED"
    assert service.perception.excluded_cameras == ()

    # fit_rotation 단위 검증: valid_ratio >= 0.5 이어도 person_cover_ratio >= 0.10 또는 정지 랜드마크 유지 시 NO_EVIDENCE
    cfg = RotationConfig()
    rng = np.random.default_rng(564)
    p_pts = rng.uniform([50, 50], [900, 500], size=(60, 2))
    q_pts = p_pts.copy()
    # 절반(30개)은 카메라가 안 움직여 제자리(오차 < 1px), 나머지 절반(30개)은 혼잡 오매칭으로 크게 튐 -> inlier_ratio < 0.6 이지만 정지 앵커 존재
    q_pts[30:] += rng.uniform(25.0, 80.0, size=(30, 2))
    fit_crowd = fit_rotation(
        p_pts,
        q_pts,
        target_cam["K"],
        (w, h),
        cfg,
        valid_ratio=0.75,
        person_cover_ratio=0.18,
        never_revealed_ratio=0.04,
    )
    assert fit_crowd["status"] == "NO_EVIDENCE"
    fit_stationary_anchor = fit_rotation(
        p_pts,
        q_pts,
        target_cam["K"],
        (w, h),
        cfg,
        valid_ratio=0.85,
        person_cover_ratio=0.02,
        never_revealed_ratio=0.0,
    )
    assert fit_stationary_anchor["status"] == "NO_EVIDENCE"

    # --- (c) 1대 큰 회전(실제 쓰러짐, 30° 회전) -> DRIFTED + 융합 제외 + 재세팅 배너 유지 ---
    rot_30 = _euler_matrix(18.0, -24.0, 8.0)
    one_rotated = dict(ref_images)
    one_rotated["cam3"] = _warp_pure_rotation(ref_images["cam3"], next(c for c in doc["cameras"] if c["camera_id"] == "cam3"), rot_30)
    t_ms = _feed_clean_background(cb, one_rotated, start_ms=t_ms + 3_000, count=3)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rc1 = rot.run_once()
    assert rc1["status"] == "REQUIRES_SETUP" and rot.last_status == "CONFIRMING"
    t_ms = _feed_clean_background(cb, one_rotated, start_ms=t_ms, count=1)
    site.elapsed[0] = t_ms - 1_760_342_400_000
    rc2 = rot.run_once()
    assert rc2["status"] == "REQUIRES_SETUP" and rot.last_status == "REQUIRES_SETUP"
    assert service.calibration.state == "DRIFTED"
    assert service.perception.excluded_cameras == ("cam3",)
    assert [item["camera_id"] for item in service.re_setup_field()] == ["cam3"]

    # --- (d) 2대 미만 보호: 4대 중 2대(cam2, cam3)만 제외 허용되고, 3번째(cam1)·4번째(cam4) 제외 요청은 거부되며 감시 저하 사유 기록 ---
    service.perception.ingest(_synthetic_person_observation_frames(service.rig, t_ms))
    service.watchdog.observe(t_ms, service._mono_ns())
    protected = service.perception.exclude_cameras(("cam1", "cam2", "cam3", "cam4"))
    assert service.perception.excluded_cameras == ("cam1", "cam3")
    assert set(protected) == {"cam2", "cam4"}
    service._record_requires_setup("cam2", "위치 이동/시차 의심")
    assert "cam2" not in service.perception.excluded_cameras
    assert len(service.rig.cameras) - len(service.perception.excluded_cameras) == 2
    assert "감시 저하" in (service.calibration_status.get("monitoring_degraded_reason") or "")
    health_payload = service.health.payload()
    assert health_payload["system_health"] == "DEGRADED"
    cam2_banner = next(i for i in health_payload["re_setup"] if i["camera_id"] == "cam2")
    assert "최소 카메라 2대 유지로 융합 제외 보류·감시 저하" in cam2_banner["reason"]

