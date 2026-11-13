"""site_setup 보조 촬영(S15P31A101-605): 굽기 전 보조 촬영 단계 연결·대체, SUPPLEMENT_REBAKE 진입점.

단계(supplement·bake 등)는 가짜로 바꾸고 작업 폴더 파일만 본다. 보조 촬영 알고리즘 자체는 tests/scene/test_supplement_capture.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from services.worker.auto_static import AutoStaticSettings
from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from services.worker.site_setup import (
    BakedScene,
    BakeReclaimed,
    CleanBackgroundBundle,
    ObjectRef,
    RebakeOutcome,
    SiteSetupHandler,
)
from services.worker.site_setup_stages import ProductionSiteSetupStages
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import load_example
from simai_ai.scene import supplement_capture as sc
from simai_ai.scene.reconstruct import ReconstructionError

SUMMARY_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/supplement_summary"}
RESULT_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/result"}
GLB = {"kind": "GLTF_VISUAL", "path": "scene.glb", "content_type": "model/gltf-binary"}


class _Store:
    def __init__(self, objects: dict[str, bytes] | None = None, fail: set[str] | None = None) -> None:
        self.objects, self.fail = objects or {}, fail or set()
        self.gets: list[str] = []
        self.puts: list[tuple[str, bytes, str]] = []

    def get(self, ctx: JobContext, uri: str) -> bytes:
        self.gets.append(uri)
        if uri in self.fail:
            raise ObjectStoreError("x", retryable=True)
        return self.objects[uri]

    def put(self, ctx: JobContext, name: str, content: bytes, content_type: str) -> str:
        self.puts.append((name, content, content_type))
        return f"s3://bucket/{name}"


class _Stages:
    """단계 이름 → 콜백(root). 실패는 ReconstructionError."""

    def __init__(self, actions: dict[str, Callable[[Path], None]] | None = None) -> None:
        self.actions, self.ran = actions or {}, []

    def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
        self.ran.append(stage)
        action = self.actions.get(stage)
        if action is not None:
            action(root)


def _write(root: Path, name: str, doc: Any) -> None:
    (root / name).write_text(json.dumps(doc), encoding="utf-8")


def _bake_ok(root: Path) -> None:
    (root / "scene.glb").write_bytes(b"glb-" + (b"sup" if (root / "prepared_base").exists() else b"base"))
    _write(root, "bake.json", {"assets": [GLB], "map_cameras": {"cam_A": {}}})


def _bake_fail_with_supplement(root: Path) -> None:
    if (root / "prepared_base").exists():
        raise ReconstructionError("STATIC_BAKE_TOOL_FAILED", "보조 view 굽기 실패")
    _bake_ok(root)


def _supplement_stage(status: str, reason: str | None = None) -> Callable[[Path], None]:
    """가짜 supplement 단계: 요약을 쓰고, APPLIED 면 run_supplement 처럼 작업 폴더를 병합본으로 바꾼다."""

    def run(root: Path) -> None:
        req = json.loads((root / "supplement" / "request.json").read_text(encoding="utf-8"))
        media = [sc.MediaInput(m["media_id"], m["kind"], None, status=m.get("status"),
                               captured_at_ms=m.get("captured_at_ms")) for m in req["media"]]
        used = [m.media_id for m in media if m.status is None] if status == "APPLIED" else []
        doc = sc.summary(status=status, reason=reason, media=media, frames_selected=3, frames_registered=3 if used else 0,
                         used_media=used, registered_by_media={mid: 3 for mid in used})
        if status == "APPLIED":
            for name in ("prepared", "mapanything"):
                (root / f"{name}_new").mkdir()
            sc.swap_in(root, root / "prepared_new", root / "mapanything_new", {"format": "simai.world_alignment"})
        (root / "supplement" / "result.json").write_text(json.dumps({"summary": doc}), encoding="utf-8")

    return run


def _input(**extra: Any) -> dict[str, Any]:
    inp = copy.deepcopy(load_example("job_request.site_setup_supplement.json")["payload"]["input"])
    inp.update(extra)
    return inp


def _objects() -> dict[str, bytes]:
    return {m["uri"]: b"media-" + m["media_id"].encode() for m in _input()["supplement_media"]}


def _fix_sha(inp: dict[str, Any], objects: dict[str, bytes]) -> dict[str, Any]:
    for m in inp["supplement_media"]:
        if "sha256" in m:
            m["sha256"] = hashlib.sha256(objects[m["uri"]]).hexdigest()
    return inp


def _stages_for(tmp_path: Path, inp: dict[str, Any], store: _Store, stages: _Stages,
                remaining: float | None = None) -> ProductionSiteSetupStages:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    for name in ("prepared", "mapanything"):
        (root / name).mkdir(exist_ok=True)
    ctx = JobContext("job-1", "lease-1", "site_setup", 1, inp)
    if remaining is not None:
        ctx.remaining_s = lambda: remaining  # type: ignore[method-assign]
    return ProductionSiteSetupStages(ctx, store, AutoStaticSettings(state_dir=tmp_path / "state"), stages, root,
                                     clock_ms=lambda: 1, calibration_loader=lambda _s: None)


def _bake(s: ProductionSiteSetupStages) -> BakedScene:
    return s.bake(setup_session_id="s", site_id="site", calibration_doc={}, backgrounds=CleanBackgroundBundle("b", {}),
                  preview_scene_version="scene-preview-x")


# --------------------------------------------------------------------------- FULL_SETUP + supplement_media
def test_bake_without_supplement_media_is_unchanged(tmp_path: Path) -> None:
    stages = _Stages({"bake": _bake_ok})
    inp = load_example("job_request.site_setup.json")["payload"]["input"]
    baked = _bake(_stages_for(tmp_path, inp, _Store(), stages))
    assert stages.ran == ["bake"] and baked.supplement is None


def test_bake_applies_supplement_downloads_media_and_reports_summary(tmp_path: Path) -> None:
    objects = _objects()
    store = _Store(objects)
    stages = _Stages({"supplement": _supplement_stage("APPLIED"), "bake": _bake_ok})
    s = _stages_for(tmp_path, _fix_sha(_input(), objects), store, stages)
    baked = _bake(s)
    assert stages.ran == ["supplement", "bake"]
    assert sorted(store.gets) == sorted(objects)  # 입력에 있는 객체 주소만 받는다
    req = json.loads((s.root / "supplement" / "request.json").read_text(encoding="utf-8"))
    assert [m["path"] for m in req["media"]] == ["media/sup-01.video", "media/sup-02.image", "media/sup-03.image"]
    assert req["media"][0]["device"] == {"make": "samsung", "model": "SM-S921N"} and req["media"][1]["source"] == "FILE_UPLOAD"
    assert baked.supplement["status"] == "APPLIED" and schema_errors(dict(baked.supplement), SUMMARY_SCHEMA) == []
    assert store.puts[-1][1] == b"glb-sup"
    bake = json.loads((s.root / "bake.json").read_text(encoding="utf-8"))
    assert bake["supplement_capture"]["summary"]["status"] == "APPLIED"


def test_bake_failure_with_supplement_rebakes_without_it(tmp_path: Path) -> None:
    objects = _objects()
    store = _Store(objects)
    stages = _Stages({"supplement": _supplement_stage("APPLIED"), "bake": _bake_fail_with_supplement})
    s = _stages_for(tmp_path, _fix_sha(_input(), objects), store, stages)
    baked = _bake(s)
    assert stages.ran == ["supplement", "bake", "bake"]
    assert baked.supplement["status"] == "FALLBACK" and baked.supplement["reason"] == sc.R_BAKE_FAILED
    assert all(m["status"] != "USED" for m in baked.supplement["media"])
    # 자리 잡았던 파일은 미등록이 아니라 '등록됐지만 쓰지 않음'(1.13.0, -652)
    assert {m["status"] for m in baked.supplement["media"] if m["frames_registered"]} == {sc.M_REGISTERED_NOT_USED}
    assert schema_errors(dict(baked.supplement), SUMMARY_SCHEMA) == []
    assert store.puts[-1][1] == b"glb-base" and not (s.root / "prepared_base").exists()


def test_download_failure_and_bad_hash_mark_media_and_stage_still_runs(tmp_path: Path) -> None:
    objects = _objects()
    inp = _fix_sha(_input(), objects)
    inp["supplement_media"][0]["sha256"] = "0" * 64  # 받은 바이트와 다름
    store = _Store(objects, fail={inp["supplement_media"][1]["uri"]})
    stages = _Stages({"supplement": _supplement_stage("APPLIED"), "bake": _bake_ok})
    s = _stages_for(tmp_path, inp, store, stages)
    baked = _bake(s)
    states = {m["media_id"]: m["status"] for m in baked.supplement["media"]}
    assert states == {"sup-01": "DOWNLOAD_FAILED", "sup-02": "DOWNLOAD_FAILED", "sup-03": "USED"}


@pytest.mark.parametrize(("env", "remaining", "reason"), [
    ({"SIMAI_RECONSTRUCTION_PATH": "method2"}, None, sc.R_PATH_UNSUPPORTED),
    ({}, 300.0, sc.R_TIME_BUDGET),
])
def test_supplement_skipped_when_path_unsupported_or_no_time(tmp_path: Path, monkeypatch, env, remaining, reason) -> None:
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    stages = _Stages({"bake": _bake_ok})
    baked = _bake(_stages_for(tmp_path, _input(), _Store(_objects()), stages, remaining))
    assert stages.ran == ["bake"]
    assert baked.supplement["status"] == "SKIPPED" and baked.supplement["reason"] == reason
    assert schema_errors(dict(baked.supplement), SUMMARY_SCHEMA) == []


def test_supplement_stage_failure_falls_back_and_bakes_baseline(tmp_path: Path) -> None:
    def boom(root: Path) -> None:
        raise ReconstructionError("STATIC_SUPPLEMENT_TOOL_FAILED", "x")

    objects = _objects()
    stages = _Stages({"supplement": boom, "bake": _bake_ok})
    baked = _bake(_stages_for(tmp_path, _fix_sha(_input(), objects), _Store(objects), stages))
    assert baked.supplement["status"] == "FALLBACK" and baked.supplement["reason"] == sc.R_RECONSTRUCTION_FAILED
    assert stages.ran == ["supplement", "bake"]


def test_supplement_stage_cancel_is_not_swallowed(tmp_path: Path) -> None:
    def cancel(root: Path) -> None:
        raise ReconstructionError("CANCELLED", "x")

    objects = _objects()
    stages = _Stages({"supplement": cancel, "bake": _bake_ok})
    with pytest.raises(JobFailed) as exc:
        _bake(_stages_for(tmp_path, _fix_sha(_input(), objects), _Store(objects), stages))
    assert exc.value.code == "CANCELLED"


# --------------------------------------------------------------------------- SUPPLEMENT_REBAKE
def _cal_doc() -> dict[str, Any]:
    cams = []
    for i, cid in enumerate(("cam_A", "cam_B", "cam_C", "cam_D")):
        C = np.array([float(i), 0.0, 1.52])
        R = np.diag([1.0, -1.0, -1.0])
        cams.append({"camera_id": cid, "K": [[100, 0, 80], [0, 100, 45], [0, 0, 1]], "R": R.tolist(),
                     "t": (-R @ C).tolist(), "image_size": [160, 90], "distortion": {"model": "NONE", "coeffs": []}})
    return {"calibration_version": "cal-setup-12", "provider_source": "G1_AUTO", "cameras": cams}


def _rebake_input() -> tuple[dict[str, Any], dict[str, bytes]]:
    inp = copy.deepcopy(load_example("job_request.site_setup_supplement_rebake.json")["payload"]["input"])
    objects = _objects()
    cal_raw = json.dumps(_cal_doc()).encode()
    base = inp["rebake_base"]
    objects[base["calibration_document"]["uri"]] = cal_raw
    base["calibration_document"]["sha256"] = hashlib.sha256(cal_raw).hexdigest()
    for im in base["bake_images"]["images"]:
        raw = b"jpeg-" + im["camera_id"].encode()
        objects[im["uri"]] = raw
        im["sha256"] = hashlib.sha256(raw).hexdigest()
    return _fix_sha(inp, objects), objects


def _rebake(s: ProductionSiteSetupStages, seen: list | None = None) -> RebakeOutcome:
    inp = s.ctx.input
    return s.rebake(setup_session_id=inp["setup_session_id"], site_id=inp["site_id"],
                    camera_ids=inp["calibration_target"]["camera_ids"], camera_heights_m=inp["camera_heights_m"],
                    height_tolerance_cm=inp["height_tolerance_cm"], rebake_base=inp["rebake_base"],
                    on_height_checks=(seen.append if seen is not None else lambda _h: None))


def test_rebake_applied_builds_root_from_base_and_uploads_new_scene(tmp_path: Path) -> None:
    inp, objects = _rebake_input()
    store = _Store(objects)
    stages = _Stages({"supplement": _supplement_stage("APPLIED"), "bake": _bake_ok})
    s = _stages_for(tmp_path, inp, store, stages)
    seen: list = []
    out = _rebake(s, seen)
    assert stages.ran == ["prepare", "mapanything", "supplement", "bake"]  # 배경·보정·미리보기 단계 없음
    assert (s.root / "images" / "cam_C.image").read_bytes() == b"jpeg-cam_C"
    assert json.loads((s.root / "calibration.json").read_text(encoding="utf-8"))["calibration_version"] == "cal-setup-12"
    job = json.loads((s.root / "job.json").read_text(encoding="utf-8"))
    assert [im["camera_id"] for im in job["input"]["images"]] == ["cam_A", "cam_B", "cam_C", "cam_D"]
    assert out.scene_version == "scene-job-1-lease-1" and out.scene_asset is not None
    assert seen and seen[0]["cam_A"]["restored_height_m"] == pytest.approx(1.52)
    assert out.height_checks["cam_A"]["within_tolerance"] is True


def test_rebake_fallback_keeps_base_scene_without_baking(tmp_path: Path) -> None:
    inp, objects = _rebake_input()
    store = _Store(objects)
    stages = _Stages({"supplement": _supplement_stage("FALLBACK", sc.R_TOO_FEW_REGISTERED), "bake": _bake_ok})
    out = _rebake(_stages_for(tmp_path, inp, store, stages))
    assert stages.ran == ["prepare", "mapanything", "supplement"]
    assert out.scene_version == "scene-setup-12" and out.scene_asset is None and store.puts == []
    assert out.supplement["reason"] == sc.R_TOO_FEW_REGISTERED


def test_rebake_bake_failure_reports_fallback_and_keeps_base(tmp_path: Path) -> None:
    inp, objects = _rebake_input()
    stages = _Stages({"supplement": _supplement_stage("APPLIED"), "bake": _bake_fail_with_supplement})
    out = _rebake(_stages_for(tmp_path, inp, _Store(objects), stages))
    assert out.scene_version == "scene-setup-12" and out.scene_asset is None
    assert out.supplement["status"] == "FALLBACK" and out.supplement["reason"] == sc.R_BAKE_FAILED


def test_rebake_rejects_base_object_with_wrong_hash(tmp_path: Path) -> None:
    inp, objects = _rebake_input()
    inp["rebake_base"]["bake_images"]["images"][0]["sha256"] = "f" * 64
    with pytest.raises(JobFailed) as exc:
        _rebake(_stages_for(tmp_path, inp, _Store(objects), _Stages()))
    assert exc.value.code == "INVALID_INPUT"


class _WorkStartsDuring(_Stages):
    """실제 ProcessStages 처럼 멈춤 신호를 본다. `at` 단계가 도는 사이 하트비트가 작업 시작(live_work)을 받는다."""

    def __init__(self, ctx_box: list[JobContext], at: str, actions: dict[str, Callable[[Path], None]]) -> None:
        super().__init__(actions)
        self._box, self._at = ctx_box, at
        self.signals: dict[str, Any] = {}

    def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
        self.ran.append(stage)
        self.signals[stage] = cancelled
        if stage == self._at:
            self._box[0].live_work.set()
        if cancelled.is_set():
            raise ReconstructionError("CANCELLED", "작업 시작으로 자식 종료")
        action = self.actions.get(stage)
        if action is not None:
            action(root)


@pytest.mark.parametrize(("at", "status"), [("prepare", "SKIPPED"), ("mapanything", "SKIPPED"),
                                            ("supplement", "FALLBACK"), ("bake", "FALLBACK")])
def test_rebake_reclaimed_on_work_start_keeps_base_scene(tmp_path: Path, at: str, status: str) -> None:
    """다시 굽기는 모든 GPU 단계에서 작업 시작(LIVE)으로 멈추고, 실패가 아니라 원래 맵 유지로 끝난다(-621, 명세 3.2 5항)."""
    inp, objects = _rebake_input()
    store = _Store(objects)
    box: list[JobContext] = []
    stages = _WorkStartsDuring(box, at, {"supplement": _supplement_stage("APPLIED"), "bake": _bake_ok})
    s = _stages_for(tmp_path, inp, store, stages)
    box.append(s.ctx)
    out = _rebake(s)
    order = ["prepare", "mapanything", "supplement", "bake"]
    assert stages.ran == order[: order.index(at) + 1]  # 멈춘 뒤 다음 단계는 돌지 않는다
    assert out.scene_version == "scene-setup-12" and out.scene_asset is None and store.puts == []
    assert out.supplement["status"] == status and out.supplement["reason"] == sc.R_LIVE_WORK_STARTED
    assert all(m["status"] != "USED" for m in out.supplement.get("media", []))
    assert schema_errors(dict(out.supplement), SUMMARY_SCHEMA) == []
    # 처리기가 내는 결과도 계약(기준 scene_version, artifacts.scene 없음)을 지킨다
    result = _handler(rebaker=lambda **kw: out)(JobContext("job-1", "lease-1", "site_setup", 1, inp))
    assert schema_errors(dict(result), RESULT_SCHEMA) == []
    assert result["status"] == "COMPLETED" and "artifacts" not in result


def test_rebake_cancel_wins_over_work_start(tmp_path: Path) -> None:
    """대여 취소가 함께 켜져 있으면 회수가 아니라 CANCELLED 실패다(재대여 대상)."""
    inp, objects = _rebake_input()
    s = _stages_for(tmp_path, inp, _Store(objects), _Stages())
    s.ctx.live_work.set()
    s.ctx.cancelled.set()
    stages = _WorkStartsDuring([s.ctx], "prepare", {})
    s.stages = stages
    with pytest.raises(JobFailed) as exc:
        _rebake(s)
    assert exc.value.code == "CANCELLED"


def test_full_setup_supplement_stage_stops_on_work_start(tmp_path: Path) -> None:
    """일반 세팅 정식 굽기 안의 보조 촬영 단계도 작업 시작으로 멈춘다. 처리기가 정식 Scene 없음으로 바꾼다(-597·-621)."""
    objects = _objects()
    box: list[JobContext] = []
    stages = _WorkStartsDuring(box, "supplement", {"supplement": _supplement_stage("APPLIED"), "bake": _bake_ok})
    s = _stages_for(tmp_path, _fix_sha(_input(), objects), _Store(objects), stages)
    box.append(s.ctx)
    with pytest.raises(BakeReclaimed):
        _bake(s)
    assert stages.ran == ["supplement"]


def test_rebake_only_widens_reclaim_for_rebake_mode(tmp_path: Path) -> None:
    """prepare·mapanything 은 다시 굽기에서만 작업 시작 신호를 본다. 일반 세팅(READY_FOR_ZONES 전)은 그대로 취소만 본다."""
    inp, objects = _rebake_input()
    s = _stages_for(tmp_path, inp, _Store(objects), _Stages())
    stages = _WorkStartsDuring([s.ctx], "none", {})
    s.stages = stages
    s._run_stage("prepare")
    assert stages.signals["prepare"] is s.ctx.cancelled
    s._rebaking = True
    s._run_stage("mapanything")
    assert stages.signals["mapanything"] is not s.ctx.cancelled


# --------------------------------------------------------------------------- 처리기(계약 모양)
def _handler(**kw: Any) -> SiteSetupHandler:
    def never(**_k: Any) -> Any:
        raise AssertionError("다시 굽기는 배경·초기 복원을 하지 않는다")

    return SiteSetupHandler(background_collector=never, world_initializer=never, **kw)


def test_handler_rebake_reports_progress_and_contract_valid_results(tmp_path: Path) -> None:
    inp, _ = _rebake_input()
    summary = sc.summary(status="APPLIED", reason=None, media=[sc.MediaInput("sup-01", "VIDEO", None)],
                         frames_selected=3, frames_registered=3, used_media=["sup-01"])
    checks = {c: {"input_height_m": 1.5, "restored_height_m": 1.52, "difference_cm": 2.0, "within_tolerance": True}
              for c in ("cam_A", "cam_B", "cam_C", "cam_D")}

    def rebaker(**kw: Any) -> RebakeOutcome:
        kw["on_height_checks"](checks)
        return RebakeOutcome("scene-new", summary, checks, ObjectRef("s3://b/scene.glb", "c" * 64, 10))

    snaps: list[dict] = []
    ctx = JobContext("job-1", "lease-1", "site_setup", 1, inp, _on_site_setup=snaps.append)
    result = _handler(rebaker=rebaker)(ctx)
    assert schema_errors(dict(result), RESULT_SCHEMA) == []
    assert result["mode"] == "SUPPLEMENT_REBAKE" and result["calibration_version"] == "cal-setup-12"
    assert result["preview_scene_version"] == "scene-setup-12-preview" and result["artifacts"] == {
        "scene": {"uri": "s3://b/scene.glb", "sha256": "c" * 64, "size_bytes": 10}}
    assert [s["status"] for s in snaps] == ["BAKING_TEXTURE"]

    fb = sc.summary(status="FALLBACK", reason=sc.R_ALIGNMENT_WORSE, media=[sc.MediaInput("sup-01", "VIDEO", None)])
    result = _handler(rebaker=lambda **kw: RebakeOutcome("scene-setup-12", fb, checks))(
        JobContext("job-1", "lease-1", "site_setup", 1, inp))
    assert schema_errors(dict(result), RESULT_SCHEMA) == []
    assert result["scene_version"] == "scene-setup-12" and "artifacts" not in result


def test_handler_without_rebaker_rejects_rebake_mode() -> None:
    inp, _ = _rebake_input()
    with pytest.raises(JobFailed) as exc:
        _handler()(JobContext("job-1", "lease-1", "site_setup", 1, inp))
    assert exc.value.code == "INVALID_INPUT"
