"""`site_setup` 운영 단계 어댑터 시험 (S15P31A101-465). 실제 MapAnything·GPU 없이 가짜 단계 도구와 가짜 객체 저장소를 쓴다."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

import pytest

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
    handler = ProductionSiteSetupHandler(_Store(), _settings(tmp_path), _Stages(), clock_ms=lambda: 1, workspace=work)
    with pytest.raises(JobFailed) as exc:
        handler(_ctx())
    assert exc.value.code == "BACKGROUND_INCOMPLETE" and exc.value.retryable is True
    assert list(work.iterdir()) == []


def test_load_active_calibration_reads_tier0_store_and_ignores_unreadable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert load_active_calibration(settings) is None
    cal_dir = settings.calibrations_dir
    cal_dir.mkdir(parents=True)
    (cal_dir / "active.json").write_text(json.dumps({"active": VERSION}), encoding="utf-8")
    assert load_active_calibration(settings) is None
    calibration_document_path(cal_dir, VERSION).write_text(json.dumps({"calibration_version": VERSION}), encoding="utf-8")
    assert load_active_calibration(settings) == {"calibration_version": VERSION}
