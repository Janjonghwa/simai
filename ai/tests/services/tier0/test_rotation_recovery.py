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

    res = rot.run_once()
    assert rot.worker_pid == pid
    assert res["status"] == "REQUIRES_SETUP"
    assert res["camera_id"] == target_cid
    assert service._pending_calibration is None
    assert service.rig.calibration_version == doc["calibration_version"]
    assert service.calibration.state == "DRIFTED"
    assert service.calibration_status["re_setup_required"] is True
    assert "현장 세팅 다시 하기" in service.calibration_status["re_setup_reason"]
    assert service.health.payload()["calibration_state"] == "DRIFTED"

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
