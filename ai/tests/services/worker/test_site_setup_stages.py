"""`site_setup` 운영 단계 어댑터 시험 (S15P31A101-465). 실제 MapAnything·GPU 없이 가짜 단계 도구와 가짜 객체 저장소를 쓴다."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from services.worker.auto_static import AutoStaticSettings, calibration_document_path
from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from services.worker.site_setup import CleanBackgroundBundle, ObjectRef
from services.worker.site_setup_stages import (
    ProductionSiteSetupHandler,
    ProductionSiteSetupStages,
    load_active_calibration,
)
from simai_ai.providers.testing import load_example
from simai_ai.scene.reconstruct import ReconstructionError

VERSION = "cal-lobby-1"
BUNDLE = "bundle-0123456789abcdef0123456789abcdef"
CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")


class _Store:
    def __init__(self, fail: ObjectStoreError | None = None) -> None:
        self.puts: list[tuple[str, bytes, str]] = []
        self.fail = fail

    def get(self, ctx: JobContext, uri: str) -> bytes:  # pragma: no cover - 시험에서 쓰지 않는다
        raise AssertionError("get 은 쓰지 않는다")

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        if self.fail:
            raise self.fail
        self.puts.append((name, content, content_type))
        return f"s3://bucket/{name}"


class _Stages:
    """단계 이름 → 작업 폴더에 쓸 파일. 실패는 ReconstructionError 로 던진다."""

    def __init__(self, files: dict[str, dict[str, bytes]] | None = None, errors: dict[str, ReconstructionError] | None = None) -> None:
        self.files, self.errors, self.ran = files or {}, errors or {}, []

    def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
        self.ran.append(stage)
        if stage in self.errors:
            raise self.errors[stage]
        for name, raw in self.files.get(stage, {}).items():
            (root / name).write_bytes(raw)


def _ctx() -> JobContext:
    return JobContext("job-1", "lease-1", "site_setup", 1, load_example("job_request.site_setup.json")["payload"]["input"])


def _settings(tmp_path: Path) -> AutoStaticSettings:
    return AutoStaticSettings(state_dir=tmp_path / "state")


def _bundle(settings: AutoStaticSettings, *, ready: dict[str, bool] | None = None, version: str = VERSION) -> Path:
    root = settings.clean_background_dir
    path = root / version / BUNDLE
    path.mkdir(parents=True)
    cameras = {}
    for cid in CAMS:
        (path / f"{cid}.png").write_bytes(b"png")
        cameras[cid] = {"camera_id": cid, "ready": (ready or {}).get(cid, True), "image": f"{cid}.png"}
    (path / "COMPLETE").write_text("{}", encoding="utf-8")
    (path / "clean_backgrounds.json").write_text(json.dumps({"cameras": cameras}), encoding="utf-8")
    (root / "status.json").write_text(json.dumps({"complete": True, "bundle": str(path), "calibration_version": version}), encoding="utf-8")
    return path


def _stages(tmp_path: Path, stages: _Stages, store: _Store | None = None, *, prior: dict[str, Any] | None = None) -> ProductionSiteSetupStages:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return ProductionSiteSetupStages(
        _ctx(), store or _Store(), _settings(tmp_path), stages, root, clock_ms=lambda: 1,
        calibration_loader=lambda _s: prior,
    )


def test_collect_without_bundle_reports_zero_so_handler_can_retry(tmp_path: Path) -> None:
    got = _stages(tmp_path, _Stages(), prior={"calibration_version": VERSION}).collect(
        setup_session_id="s", site_id="site", camera_ids=CAMS)
    assert got.collection_pct_by_cam == dict.fromkeys(CAMS, 0.0)


def test_collect_reads_complete_bundle_and_per_camera_ready(tmp_path: Path) -> None:
    path = _bundle(_settings(tmp_path), ready={"cam_C": False})
    got = _stages(tmp_path, _Stages(), prior={"calibration_version": VERSION}).collect(
        setup_session_id="s", site_id="site", camera_ids=CAMS)
    assert got.bundle_id == BUNDLE
    assert got.collection_pct_by_cam == {"cam_A": 100.0, "cam_B": 100.0, "cam_C": 0.0, "cam_D": 100.0}
    assert got.frames_by_cam["cam_A"] == [path / "cam_A.png"]


def test_collect_rejects_bundle_of_another_calibration_version(tmp_path: Path) -> None:
    _bundle(_settings(tmp_path))
    got = _stages(tmp_path, _Stages(), prior={"calibration_version": "cal-other"}).collect(
        setup_session_id="s", site_id="site", camera_ids=CAMS)
    assert set(got.collection_pct_by_cam.values()) == {0.0}


def test_initialize_without_active_calibration_fails_without_inventing_intrinsics(tmp_path: Path) -> None:
    s = _stages(tmp_path, _Stages(), prior=None)
    bg = s.collect(setup_session_id="s", site_id="site", camera_ids=CAMS)
    with pytest.raises(JobFailed) as exc:
        s.initialize(setup_session_id="s", site_id="site", camera_ids=CAMS, camera_heights_m=dict.fromkeys(CAMS, 1.5),
                     height_tolerance_cm=5.0, backgrounds=bg, created_at_ms=1)
    assert exc.value.code == "INTRINSICS_UNAVAILABLE" and exc.value.retryable is False


def test_publish_keeps_prior_document_and_uploads_new_version(tmp_path: Path) -> None:
    store = _Store()
    s = _stages(tmp_path, _Stages(), store)
    (s.root / "calibration.json").write_text('{"calibration_version": "cal-old"}', encoding="utf-8")
    published = s.publish({"calibration_version": "cal-new", "cameras": []})
    assert published.calibration_version == "cal-new"
    raw = store.puts[0][1]
    assert published.document == ObjectRef("s3://bucket/calibration-cal-new.json", hashlib.sha256(raw).hexdigest(), len(raw))
    written = json.loads((s.root / "calibration.json").read_text(encoding="utf-8"))
    assert written["calibration_version"] == "cal-new"
    # 세팅 출처(Tier 0 채택 조건). 활성 보정을 읽기 전이라 previous 는 지어내지 않는다
    assert written["setup_session_id"] == _ctx().input["setup_session_id"] and "previous_calibration_version" not in written
    assert json.loads((s.root / "calibration.prior.json").read_text(encoding="utf-8"))["calibration_version"] == "cal-old"
    s._prior = {"calibration_version": "cal-old"}
    assert s.publish({"calibration_version": "cal-new2", "cameras": []}).calibration_version == "cal-new2"
    assert json.loads(store.puts[-1][1])["previous_calibration_version"] == "cal-old"
    assert [p[0] for p in store.puts] == ["calibration-cal-new.json", "calibration-cal-new2.json"]


def test_publish_maps_object_store_failure_to_retryable_job_failure(tmp_path: Path) -> None:
    s = _stages(tmp_path, _Stages(), _Store(ObjectStoreError("5xx", retryable=True)))
    with pytest.raises(JobFailed) as exc:
        s.publish({"calibration_version": "cal-new"})
    assert exc.value.code == "SETUP_OBJECT_FAILED" and exc.value.retryable is True


def _preview_files() -> dict[str, bytes]:
    return {
        "preview_scene.glb": b"glb",
        "preview.json": json.dumps({"assets": [{"kind": "GLTF_VISUAL", "path": "preview_scene.glb", "content_type": "model/gltf-binary"}]}).encode(),
    }


def test_preview_runs_stage_uploads_glb_and_names_scene_by_run(tmp_path: Path) -> None:
    store = _Store()
    s = _stages(tmp_path, _Stages({"preview": _preview_files()}), store)
    got = s.preview(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert got.preview_scene_version == "scene-preview-job-1-lease-1"
    assert got.scene_asset == ObjectRef("s3://bucket/preview_scene.glb", hashlib.sha256(b"glb").hexdigest(), 3)
    assert store.puts == [("preview_scene.glb", b"glb", "model/gltf-binary")]


def test_bake_falls_back_when_texrecon_missing(tmp_path: Path) -> None:
    store = _Store()
    asset = {"kind": "GLTF_VISUAL", "path": "scene.glb", "content_type": "model/gltf-binary"}
    stages = _Stages({
        "bake": {"bake.json": json.dumps({"fallback_reason": "TEXRECON_NOT_FOUND", "texture": None}).encode()},
        "fallback": {"scene.glb": b"glb", "bake.json": json.dumps({"assets": [asset]}).encode()},
    })
    s = _stages(tmp_path, stages, store)
    baked = s.bake(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle(BUNDLE, {}),
                   preview_scene_version="scene-preview-x")
    assert baked.scene_version == "scene-job-1-lease-1" and stages.ran == ["bake", "fallback"]
    assert baked.scene_asset is not None and baked.scene_asset.uri == "s3://bucket/scene.glb"
    assert store.puts == [("scene.glb", b"glb", "model/gltf-binary")]


@pytest.mark.parametrize(("code", "retryable"), [("CANCELLED", True), ("STATIC_PREVIEW_TIMEOUT", True),
                                                   ("STATIC_PREVIEW_TOOL_FAILED", True), ("STATIC_MAPANYTHING_EMPTY", False)])
def test_stage_errors_map_to_contract_failures(tmp_path: Path, code: str, retryable: bool) -> None:
    s = _stages(tmp_path, _Stages(errors={"preview": ReconstructionError(code, "x")}))
    with pytest.raises(JobFailed) as exc:
        s.preview(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert exc.value.code == code and exc.value.retryable is retryable


def test_stage_output_must_stay_inside_work_folder(tmp_path: Path) -> None:
    doc = {"assets": [{"kind": "GLTF_VISUAL", "path": "../escape.glb", "content_type": "model/gltf-binary"}]}
    s = _stages(tmp_path, _Stages({"preview": {"preview.json": json.dumps(doc).encode()}}))
    with pytest.raises(JobFailed) as exc:
        s.preview(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert exc.value.code == "STATIC_OUTPUT_EMPTY"


def test_handler_without_bundle_fails_retryably_and_cleans_work_folder(tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    handler = ProductionSiteSetupHandler(_Store(), _settings(tmp_path), _Stages(), clock_ms=lambda: 1, workspace=work,
                                         background_wait_s=0)
    with pytest.raises(JobFailed) as exc:
        handler(_ctx())
    assert exc.value.code == "BACKGROUND_INCOMPLETE" and exc.value.retryable is True
    assert list(work.iterdir()) == []


def test_production_handler_connects_sift(tmp_path: Path, monkeypatch) -> None:
    from services.worker import site_setup_stages
    from simai_ai.calibration.extrinsics.feature_matching import match_sift

    seen = {}

    class Handler:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def __call__(self, ctx):
            return {}

    monkeypatch.setattr(site_setup_stages, "SiteSetupHandler", Handler)
    handler = ProductionSiteSetupHandler(_Store(), _settings(tmp_path), _Stages(), workspace=tmp_path, clock_ms=lambda: 1)
    assert handler(_ctx()) == {}
    assert seen["feature_matcher"] is match_sift


def test_load_active_calibration_reads_tier0_store_and_ignores_unreadable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert load_active_calibration(settings) is None
    cal_dir = settings.calibrations_dir
    cal_dir.mkdir(parents=True)
    (cal_dir / "active.json").write_text(json.dumps({"active": VERSION}), encoding="utf-8")
    assert load_active_calibration(settings) is None
    calibration_document_path(cal_dir, VERSION).write_text(json.dumps({"calibration_version": VERSION}), encoding="utf-8")
    assert load_active_calibration(settings) == {"calibration_version": VERSION}


# ---------------------------------------------------------------------------- S15P31A101-473
def _status(settings: AutoStaticSettings, cameras: dict[str, dict[str, Any]], *, version: str = VERSION) -> None:
    root = settings.clean_background_dir
    root.mkdir(parents=True, exist_ok=True)
    (root / "status.json").write_text(json.dumps({"calibration_version": version, "cameras": cameras, "complete": False,
                                                  "bundle": None}), encoding="utf-8")


def test_collect_reports_partial_collection_from_tier0_status(tmp_path: Path) -> None:
    """묶음 완성 전에는 Tier 0 status.json 의 표본 수로 수집률(최대 99)을 알린다. 다른 보정의 수집은 0."""
    settings = _settings(tmp_path)
    _status(settings, {"cam_A": {"sample_count": 5}, "cam_B": {"sample_count": 40, "ready": True},
                       "cam_C": {"sample_count": 19}, "cam_D": {"sample_count": 0, "exported": True}})
    s = _stages(tmp_path, _Stages(), prior={"calibration_version": VERSION})
    got = s.collect(setup_session_id="s", site_id="site", camera_ids=CAMS)
    assert got.collection_pct_by_cam == {"cam_A": 25.0, "cam_B": 99.0, "cam_C": 95.0, "cam_D": 99.0}
    assert got.missing_cameras(CAMS) == list(CAMS)
    other = _stages(tmp_path, _Stages(), prior={"calibration_version": "cal-other"})
    assert set(other.collect(setup_session_id="s", site_id="site", camera_ids=CAMS).collection_pct_by_cam.values()) == {0.0}


def test_collect_sets_observed_until_from_bundle_time_ranges(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = _bundle(settings)
    meta = json.loads((path / "clean_backgrounds.json").read_text(encoding="utf-8"))
    for i, cid in enumerate(CAMS):
        meta["cameras"][cid]["time_range_ms"] = [1_000, 2_000 + i]
    (path / "clean_backgrounds.json").write_text(json.dumps(meta), encoding="utf-8")
    got = _stages(tmp_path, _Stages(), prior={"calibration_version": VERSION}).collect(
        setup_session_id="s", site_id="site", camera_ids=CAMS)
    assert got.observed_until_ms == 2_003 and got.missing_cameras(CAMS) == []


def test_production_handler_waits_for_bundle_with_heartbeats(tmp_path: Path) -> None:
    """운영 처리기: 묶음이 없으면 설정한 시간까지 다시 보며 COLLECTING_BACKGROUND 수집률을 보낸 뒤 재시도 가능하게 끝난다."""
    settings = _settings(tmp_path)
    cams = _ctx().input["calibration_target"]["camera_ids"]
    _status(settings, {cid: {"sample_count": 10} for cid in cams})
    now = {"t": 0.0}

    def sleep(event: Any, seconds: float) -> None:
        now["t"] += seconds

    handler = ProductionSiteSetupHandler(_Store(), settings, _Stages(), clock_ms=lambda: 1, workspace=tmp_path,
                                         background_wait_s=6.0, background_poll_s=2.0, monotonic=lambda: now["t"], sleep=sleep)
    ctx = _ctx()
    with pytest.raises(JobFailed) as exc:
        handler(ctx)
    assert exc.value.code == "BACKGROUND_INCOMPLETE" and exc.value.retryable is True and now["t"] == 6.0
    assert ctx.site_setup_history[0]["background_collection_pct"] == dict.fromkeys(cams, 50.0)


def test_background_wait_settings_from_env() -> None:
    from services.worker.site_setup_stages import background_wait_from_env

    assert background_wait_from_env({}) == (240.0, 2.0)
    assert background_wait_from_env({"SIMAI_SETUP_BACKGROUND_WAIT_S": "0", "SIMAI_SETUP_BACKGROUND_POLL_S": "0.5"}) == (0.0, 0.5)
    for bad in ({"SIMAI_SETUP_BACKGROUND_WAIT_S": "-1"}, {"SIMAI_SETUP_BACKGROUND_POLL_S": "0"}, {"SIMAI_SETUP_BACKGROUND_WAIT_S": "x"}):
        with pytest.raises(ValueError):
            background_wait_from_env(bad)


def test_stage_failure_keeps_log_tail_in_message_and_copies_logs(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """작업 폴더는 끝나면 지워지므로 실패 단계 로그 끝을 메시지·워커 로그에 남기고 상태 폴더에 복사한다."""
    s = _stages(tmp_path, _Stages(errors={"preview": ReconstructionError("STATIC_PREVIEW_TOOL_FAILED", "종료 코드 1")}))
    (s.root / "preview.log").write_text("\n".join(f"line {i}" for i in range(50)) + "\nCUDA out of memory\n", encoding="utf-8")
    with caplog.at_level("WARNING", logger="services.worker"), pytest.raises(JobFailed) as exc:
        s.preview(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert exc.value.code == "STATIC_PREVIEW_TOOL_FAILED" and exc.value.message.startswith("종료 코드 1 | preview.log 끝:")
    assert exc.value.message.endswith("line 48 / line 49 / CUDA out of memory") and len(exc.value.message) < 500
    assert "line 31" in caplog.text and "line 30" not in caplog.text  # 워커 로그는 끝 20줄
    kept = s.keep_failure_logs()
    assert kept == _settings(tmp_path).state_dir / "worker" / "site-setup-failed" / "job-1-lease-1"
    assert (kept / "preview.log").read_text(encoding="utf-8").endswith("CUDA out of memory\n")


def test_production_handler_keeps_failed_stage_logs(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    class _LoggingStages(_Stages):
        def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
            (root / f"{stage}.log").write_text("boom\n", encoding="utf-8")
            raise ReconstructionError("STATIC_PREPARE_TOOL_FAILED", "종료 코드 2")

    handler = ProductionSiteSetupHandler(_Store(), settings, _LoggingStages(), clock_ms=lambda: 1, workspace=work,
                                         background_wait_s=0)
    s = ProductionSiteSetupStages(_ctx(), _Store(), settings, _LoggingStages(), work, clock_ms=lambda: 1)
    with pytest.raises(JobFailed):
        s._run_stage("prepare")
    assert s.keep_failure_logs() is not None
    with pytest.raises(JobFailed) as exc:
        handler(_ctx())  # 묶음이 없어 단계 전에 끝난다: 남길 로그가 없으면 아무것도 만들지 않는다
    assert exc.value.code == "BACKGROUND_INCOMPLETE"
    assert [p.name for p in (settings.state_dir / "worker" / "site-setup-failed").iterdir()] == ["job-1-lease-1"]


# ---------------------------------------------------------------------------- S15P31A101-479
def _png(width: int, height: int, seed: int = 0) -> bytes:
    image = np.random.default_rng(seed).integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    ok, raw = cv2.imencode(".png", image)
    assert ok
    return bytes(raw.tobytes())


def _frames_bundle(tmp_path: Path, sizes: dict[str, tuple[int, int]]) -> CleanBackgroundBundle:
    frames = {}
    for idx, (cid, (w, h)) in enumerate(sizes.items()):
        path = tmp_path / f"{cid}.png"
        path.write_bytes(_png(w, h, idx))
        frames[cid] = [path]
    return CleanBackgroundBundle(BUNDLE, dict.fromkeys(sizes, 100.0), frames)


def test_publish_frames_uploads_one_small_jpeg_per_camera_with_sha(tmp_path: Path) -> None:
    store = _Store()
    s = _stages(tmp_path, _Stages(), store)
    bundle = _frames_bundle(tmp_path, {"cam_A": (2304, 1296), "cam_B": (640, 360)})
    got = s.publish_frames(setup_session_id="s", camera_ids=("cam_A", "cam_B"), backgrounds=bundle)
    assert [g.camera_id for g in got] == ["cam_A", "cam_B"] and {g.face_policy for g in got} == {"BLURRED"}
    assert [(n, ct) for n, _, ct in store.puts] == [("frame-cam_A.jpg", "image/jpeg"), ("frame-cam_B.jpg", "image/jpeg")]
    for ref, (_, raw, _) in zip(got, store.puts, strict=True):
        assert ref.ref == ObjectRef(f"s3://bucket/frame-{ref.camera_id}.jpg", hashlib.sha256(raw).hexdigest(), len(raw))
        assert raw[:3] == b"\xff\xd8\xff"
    sizes = [cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR).shape[:2] for _, raw, _ in store.puts]
    assert sizes == [(720, 1280), (360, 640)]  # 긴 변 1280 이하로만 줄이고 작은 것은 그대로
    # 같은 배경이면 같은 JPEG(재시도해도 같은 sha256)
    again = _Store()
    _stages(tmp_path, _Stages(), again).publish_frames(setup_session_id="s", camera_ids=("cam_A", "cam_B"), backgrounds=bundle)
    assert [r[1] for r in again.puts] == [r[1] for r in store.puts]


def test_publish_frames_skips_cameras_without_usable_background(tmp_path: Path) -> None:
    store = _Store()
    s = _stages(tmp_path, _Stages(), store)
    bundle = _frames_bundle(tmp_path, {"cam_A": (320, 240)})
    (tmp_path / "bad.png").write_bytes(b"not an image")
    bundle = CleanBackgroundBundle(BUNDLE, dict.fromkeys(("cam_A", "cam_B", "cam_C"), 100.0),
                                   {**bundle.frames_by_cam, "cam_B": [tmp_path / "bad.png"]})
    got = s.publish_frames(setup_session_id="s", camera_ids=("cam_A", "cam_B", "cam_C"), backgrounds=bundle)
    assert [g.camera_id for g in got] == ["cam_A"] and len(store.puts) == 1  # 못 만든 프레임은 지어내지 않고 건너뛴다


def test_publish_frames_maps_object_store_failure_to_job_failure(tmp_path: Path) -> None:
    s = _stages(tmp_path, _Stages(), _Store(ObjectStoreError("5xx", retryable=True)))
    with pytest.raises(JobFailed) as exc:
        s.publish_frames(setup_session_id="s", camera_ids=("cam_A",), backgrounds=_frames_bundle(tmp_path, {"cam_A": (64, 64)}))
    assert exc.value.code == "SETUP_OBJECT_FAILED" and exc.value.retryable is True


def _rig_doc(version: str, yaw: float = 0.0, shift: tuple[float, float] = (0.0, 0.0)) -> dict[str, Any]:
    """4대 코너 리그 보정 문서. yaw·shift 는 같은 리그를 다른 World 로 표현한 값(X_ref = Rz·X + T)."""
    c, s = np.cos(yaw), np.sin(yaw)
    Rz, T = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]), np.array([shift[0], shift[1], 0.0])
    cams = []
    for cid, center in zip(CAMS, ((7.5, 7.3, 2.44), (7.4, 0.7, 2.44), (0.0, 0.6, 2.48), (0.0, 7.3, 2.48))):
        z = np.array([3.7, 4.0, 0.8]) - np.asarray(center)
        z /= np.linalg.norm(z)
        x = np.cross(z, [0.0, 0.0, 1.0])
        x /= np.linalg.norm(x)
        R = np.stack([x, np.cross(z, x), z]) @ Rz
        C = Rz.T @ (np.asarray(center) - T)
        cams.append({"camera_id": cid, "R": R.tolist(), "t": (-R @ C).tolist()})
    return {"calibration_version": version, "state": "PROVISIONAL", "provider_source": "G1_AUTO", "cameras": cams}


def test_anchor_world_moves_new_calibration_into_prior_world_and_publish_marks_contract(tmp_path: Path) -> None:
    """활성 보정(이전 채택 World)이 있으면 같은 리그의 새 보정을 그 World 로 옮기고, 등록 문서는 계약 2.5.0 이다(-584)."""
    store = _Store()
    s = _stages(tmp_path, _Stages(), store)
    s._prior = _rig_doc("cal-old")
    new = _rig_doc("cal-new", yaw=0.6, shift=(1.5, -2.0))
    doc = s.anchor_world(new)
    assert doc["world_anchor"]["accepted"] is True and doc["world_anchor"]["reference_calibration_version"] == "cal-old"
    for a, b in zip(doc["cameras"], s._prior["cameras"]):
        assert np.allclose(a["R"], b["R"], atol=1e-9) and np.allclose(a["t"], b["t"], atol=1e-9)
    s.publish(doc)
    sent = json.loads(store.puts[-1][1])
    assert sent["schema_version"] == "2.5.0" and sent["previous_calibration_version"] == "cal-old"
    # 이전 보정이 없으면(첫 세팅) 기록 없이 그대로
    s._prior = None
    assert s.anchor_world(new) == new


def test_world_anchor_off_switch_keeps_new_world(tmp_path: Path) -> None:
    """SIMAI_SETUP_WORLD_ANCHOR=off 면 변환하지 않고 DISABLED 로 기록한다. 기본은 auto, 그 밖의 값은 설정 오류."""
    from services.worker.site_setup_stages import world_anchor_enabled_from_env

    assert world_anchor_enabled_from_env({}) is True and world_anchor_enabled_from_env({"SIMAI_SETUP_WORLD_ANCHOR": "auto"}) is True
    assert world_anchor_enabled_from_env({"SIMAI_SETUP_WORLD_ANCHOR": " OFF "}) is False
    with pytest.raises(ValueError):
        world_anchor_enabled_from_env({"SIMAI_SETUP_WORLD_ANCHOR": "on"})
    root = tmp_path / "root"
    root.mkdir()
    s = ProductionSiteSetupStages(_ctx(), _Store(), _settings(tmp_path), _Stages(), root, clock_ms=lambda: 1,
                                  calibration_loader=lambda _s: None, world_anchor_enabled=False)
    s._prior = _rig_doc("cal-old")
    new = _rig_doc("cal-new", yaw=0.6, shift=(1.5, -2.0))
    doc = s.anchor_world(new)
    assert doc["world_anchor"]["accepted"] is False and doc["world_anchor"]["reason"] == "DISABLED"
    assert [c["R"] for c in doc["cameras"]] == [c["R"] for c in new["cameras"]]


# ---------------------------------------------------------------------------- S15P31A101-593
def _jpeg(w: int, h: int, seed: int) -> bytes:
    image = np.random.default_rng(seed).integers(0, 255, size=(h, w, 3), dtype=np.uint8)
    ok, raw = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    assert ok
    return bytes(raw.tobytes())


def test_publish_bake_images_uploads_the_exact_work_folder_file_per_camera(tmp_path: Path) -> None:
    """미리보기·굽기가 읽는 images/<카메라>.image 를 그대로 올린다(JPEG 이면 바이트 그대로 = 같은 sha256). 없는 카메라는 건너뛴다."""
    store = _Store()
    s = _stages(tmp_path, _Stages(), store)
    (s.root / "images").mkdir()
    jpeg = _jpeg(320, 180, 1)
    (s.root / "images" / "cam_A.image").write_bytes(jpeg)
    (s.root / "images" / "cam_B.image").write_bytes(_png(320, 180, 2))  # JPEG 가 아니면 같은 해상도 JPEG 로
    (s.root / "images" / "cam_D.image").write_bytes(b"not an image")
    got = s.publish_bake_images(setup_session_id="s", camera_ids=("cam_A", "cam_B", "cam_C", "cam_D"),
                                backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert got is not None and got.bundle_id == BUNDLE
    assert [i.camera_id for i in got.images] == ["cam_A", "cam_B"] and {i.face_policy for i in got.images} == {"OBSERVED"}
    assert [(n, ct) for n, _, ct in store.puts] == [("bake-cam_A.jpg", "image/jpeg"), ("bake-cam_B.jpg", "image/jpeg")]
    assert store.puts[0][1] == jpeg
    for ref, (_, raw, _) in zip(got.images, store.puts, strict=True):
        assert ref.ref == ObjectRef(f"s3://bucket/bake-{ref.camera_id}.jpg", hashlib.sha256(raw).hexdigest(), len(raw))
        assert raw[:3] == b"\xff\xd8\xff"
    assert cv2.imdecode(np.frombuffer(store.puts[1][1], np.uint8), cv2.IMREAD_COLOR).shape[:2] == (180, 320)  # 해상도 불변


def test_publish_bake_images_without_files_returns_none_and_maps_store_failure(tmp_path: Path) -> None:
    s = _stages(tmp_path, _Stages(), _Store(ObjectStoreError("5xx", retryable=True)))
    assert s.publish_bake_images(setup_session_id="s", camera_ids=CAMS, backgrounds=CleanBackgroundBundle(BUNDLE, {})) is None
    (s.root / "images").mkdir()
    (s.root / "images" / "cam_A.image").write_bytes(_jpeg(64, 64, 3))
    with pytest.raises(JobFailed) as exc:
        s.publish_bake_images(setup_session_id="s", camera_ids=("cam_A",), backgrounds=CleanBackgroundBundle(BUNDLE, {}))
    assert exc.value.code == "SETUP_OBJECT_FAILED" and exc.value.retryable is True
