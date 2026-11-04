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
from test_clean_background import make_feed  # noqa: E402
from test_tier0_service import _settings  # noqa: E402
from services.tier0.clean_background import BackgroundCleanBackground  # noqa: E402
from services.tier0.i1_client import Envelope  # noqa: E402
from services.tier0.rotation_recovery import BackgroundRotationRecovery  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
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
    assert candidate_doc is not None
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
    monkeypatch.setattr(BackgroundCleanBackground, "run", lambda self: None)
    monkeypatch.setattr(BackgroundRotationRecovery, "run", lambda self: None)


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
    res0 = rot.run_once()
    assert res0["status"] == "STABLE"

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
    assert res["status"] == "REQUIRES_SETUP"
    assert res["camera_id"] == target_cid
    assert service._pending_calibration is None
    assert service.rig.calibration_version == doc["calibration_version"]
    assert service.calibration.state == "DRIFTED"
    assert service.calibration_status["re_setup_required"] is True
    assert "현장 세팅 다시 하기" in service.calibration_status["re_setup_reason"]
    assert service.health.payload()["calibration_state"] == "DRIFTED"

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
        fake_cpu = [0.0]

        def step_cpu() -> float:
            val = fake_cpu[0]
            fake_cpu[0] += 0.006  # 2번째 호출에서 10ms 예산 초과
            return val

        rot._cpu_clock_s = step_cpu
        ref_images = {cam["camera_id"]: _render_plane_view(cam) for cam in doc["cameras"]}
        t_ms = _feed_clean_background(cb, ref_images, start_ms=service.clock.now_ms(), count=3)
        site.elapsed[0] = t_ms - 1_760_342_400_000
        deferred = rot.run_once()
        assert deferred["status"] == "BUDGET_DEFERRED"
        assert rot.budget_deferrals == 1

        # 2) 회전 복구 스레드가 무거운 계산 중이어도 판정 tick은 절대 기다리지 않는다
        rot._cpu_clock_s = lambda: 0.0

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
