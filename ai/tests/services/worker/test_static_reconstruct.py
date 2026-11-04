"""합성 입력(SIM 상한)·가짜 GPU/굽기로 I5 복원 흐름과 실패를 검증한다."""
import copy
import json
import threading
from pathlib import Path

import pytest

from services.worker.__main__ import build_handlers
from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from services.worker.runner import Outcome
from services.worker.static_reconstruct import StaticReconstructHandler
from simai_ai.providers.contract import schema_errors
from simai_ai.providers.testing import load_example
from simai_ai.scene.reconstruct import ReconstructionError, sha256, write_json


class Store:
    def __init__(self):
        self.cal = load_example("calibration.json")
        second = copy.deepcopy(self.cal["cameras"][0])
        second["camera_id"] = "camera-second"
        second["t"][0] += 1
        self.cal["cameras"].append(second)
        self.raw = json.dumps(self.cal).encode()
        self.gets, self.puts = [], {}
        self.fail = False

    def get(self, ctx, uri):
        self.gets.append(uri)
        if self.fail:
            raise ObjectStoreError("일시 전송 실패")
        return self.raw if uri == "s3://site/cal.json" else b"synthetic-image"

    def put(self, ctx, name, raw, content_type):
        self.puts[name] = raw
        return "s3://site/output/" + name


def input_for(store):
    ids = [c["camera_id"] for c in store.cal["cameras"]][:2]
    return {"calibration_ref": "s3://site/cal.json", "calibration_version": store.cal["calibration_version"],
        "calibration_hash": sha256(store.raw),
        "input_provenance": {"frames": {"source": "SIM", "camera_ids": ids}, "is_upper_bound": True,
            "calibration": {"source": store.cal["provider_source"], "calibration_version": store.cal["calibration_version"]}},
        "images": [{"camera_id": cid, "uri": f"s3://site/{cid}.jpg", "kind": "CLEAN_BACKGROUND"} for cid in ids]}


class FakeStages:
    def __init__(self, *, fallback=False, fail=None):
        self.calls = []
        self.progress = []
        self.fallback, self.fail = fallback, fail

    def run(self, stage, root, *, timeout_s, cancelled):
        assert timeout_s > 0
        self.calls.append(stage)
        if stage == self.fail:
            raise ReconstructionError(f"STATIC_{stage.upper()}_TOOL_FAILED", "가짜 도구 실패")
        if stage == "mapanything":
            (root / "mapanything").mkdir()
            write_json(root / "mapanything" / "metrics.json", {"result": "SUCCESS", "model_version": "fake-mapanything"})
        if stage == "preview":
            (root / "preview_scene.glb").write_bytes(b"synthetic-preview-glb")
            write_json(
                root / "preview.json",
                {
                    "scene_tier": "PREVIEW",
                    "preview_scene": True,
                    "texture": None,
                    "fallback_reason": "PREVIEW_FAST_PATH",
                    "assets": [
                        {
                            "path": "preview_scene.glb",
                            "kind": "GLTF_VISUAL",
                            "content_type": "model/gltf-binary",
                            "triangle_count": 1200,
                        }
                    ],
                    "postprocess": ["OUTLIER_REMOVAL", "WORLD_ALIGNMENT"],
                    "observed_surface_ratio": 1.0,
                    "metrics": {"vertex_count": 650, "triangle_count": 1200, "build_s": 0.05},
                },
            )
        if stage == "bake" and self.fallback:
            write_json(root / "bake.json", {"texture": None, "fallback_reason": "TEXRECON_NOT_FOUND"})
        if (stage == "bake" and not self.fallback) or stage == "fallback":
            (root / "scene.glb").write_bytes(b"synthetic-glb")
            assets = [{"path": "scene.glb", "kind": "GLTF_VISUAL", "content_type": "model/gltf-binary"}]
            if not self.fallback:
                (root / "surface.png").write_bytes(b"synthetic-png")
                assets.append({"path": "surface.png", "kind": "SURFACE_SOURCE_MAP", "content_type": "image/png"})
            write_json(root / "bake.json", {"texture": None if self.fallback else "texrecon",
                "fallback_reason": "TEXRECON_NOT_FOUND" if self.fallback else None, "assets": assets,
                "postprocess": ["WORLD_ALIGNMENT"], "observed_surface_ratio": .7})
        if stage == "measure":
            write_json(root / "scale_check.json", {"scale": None, "reason": "INSUFFICIENT_SAMPLES", "calibration_changed": False})


def handler(store, stages, tmp_path):
    return StaticReconstructHandler(store, clock_ms=lambda: 1_800_000_000_000, stages=stages, workspace=tmp_path)


def context(store):
    return JobContext("job-364", "lease-1", "static_reconstruct", 1, input_for(store))


@pytest.mark.parametrize("fallback", [False, True])
def test_order_progress_and_schema_callback(ec2, make_worker, tmp_path, fallback):
    store, stages = Store(), FakeStages(fallback=fallback)
    h = handler(store, stages, tmp_path)
    fractions = []

    def observed(ctx):
        original = ctx.report_progress
        def report(value):
            fractions.append(value)
            original(value)
        ctx.report_progress = report
        return h(ctx)

    job_id = ec2.add_job("static_reconstruct", input_for(store), timeout_ms=60000)
    assert make_worker({"static_reconstruct": observed}).run_once() is Outcome.ACCEPTED
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    assert schema_errors(job.result, "scene_output") == []
    assert stages.calls == ["prepare", "mapanything", "bake", *(["fallback"] if fallback else []), "measure"]
    assert fractions == sorted(fractions) and fractions[0] == 0 and fractions[-1] == 1
    assert job.result["state"] == "CANDIDATE" and job.result["geometry_role"] == "VISUAL"
    metrics = json.loads(store.puts["metrics.json"])
    assert schema_errors(json.loads(store.puts["run_record.json"]), "run_record") == []
    assert metrics["texture"] == (None if fallback else "texrecon")
    assert metrics["scale_check"]["scale"] is None
    assert store.raw == json.dumps(store.cal).encode()  # 보정 불변
    assert list(tmp_path.iterdir()) == []  # 임시 이미지 정리


@pytest.mark.parametrize("stage", ["prepare", "mapanything", "bake", "measure"])
def test_external_failure_stops_and_uploads_nothing(tmp_path, stage):
    store, stages = Store(), FakeStages(fail=stage)
    with pytest.raises(JobFailed) as err:
        handler(store, stages, tmp_path)(context(store))
    assert err.value.code == f"STATIC_{stage.upper()}_TOOL_FAILED"
    assert stages.calls[-1] == stage and not store.puts


@pytest.mark.parametrize("change,code", [
    (lambda i: i.update(images=[]), "STATIC_INPUT_INVALID"),
    (lambda i: i["images"].__setitem__(1, copy.deepcopy(i["images"][0])), "STATIC_INPUT_INVALID"),
    (lambda i: i.update(calibration_hash="sha256:" + "0" * 64), "CALIBRATION_HASH_MISMATCH"),
    (lambda i: i.update(calibration_version="other"), "CALIBRATION_INVALID"),
    (lambda i: i["input_provenance"].update(is_upper_bound=False), "STATIC_INPUT_INVALID"),
])
def test_reject_bad_inputs(tmp_path, change, code):
    store, stages = Store(), FakeStages()
    ctx = context(store)
    change(ctx.input)
    with pytest.raises(JobFailed) as err:
        handler(store, stages, tmp_path)(ctx)
    assert err.value.code == code
    assert not stages.calls and not store.puts


def test_cancellation_and_retryable_object_failure(tmp_path):
    store, stages = Store(), FakeStages()
    ctx = context(store)
    ctx.cancelled.set()
    with pytest.raises(JobFailed, match="CANCELLED"):
        handler(store, stages, tmp_path)(ctx)
    assert not store.gets
    store.fail = True
    with pytest.raises(JobFailed) as err:
        handler(store, stages, tmp_path)(context(store))
    assert err.value.code == "STATIC_OBJECT_FAILED" and err.value.retryable


def test_registration_requires_store():
    assert "static_reconstruct" not in build_handlers(None, env={})
    assert isinstance(build_handlers(None, Store(), env={})["static_reconstruct"], StaticReconstructHandler)


def test_missing_output_is_failure(tmp_path):
    store = Store()
    class Empty(FakeStages):
        def run(self, stage, root, **kwargs):
            super().run(stage, root, **kwargs)
            if stage == "mapanything":
                write_json(root / "mapanything" / "metrics.json", {"result": "ERROR"})
    with pytest.raises(JobFailed, match="STATIC_MAPANYTHING_EMPTY"):
        handler(store, Empty(), tmp_path)(context(store))
    assert not store.puts


def test_stage_deadline_is_enforced_with_injected_clock(tmp_path):
    store = Store()
    clock = [0.]
    class Slow(FakeStages):
        def run(self, stage, root, **kwargs):
            super().run(stage, root, **kwargs)
            clock[0] += 181
    h = handler(store, Slow(), tmp_path)
    h.pipeline.monotonic = lambda: clock[0]
    with pytest.raises(JobFailed, match="STATIC_PREPARE_TIMEOUT"):
        h(context(store))
    assert not store.puts


def test_failed_tool_log_survives_temporary_directory_cleanup(tmp_path):
    store = Store()
    class LoggedFailure(FakeStages):
        def run(self, stage, root, **kwargs):
            (root / "prepare.log").write_text("synthetic tool diagnostic", encoding="utf-8")
            raise ReconstructionError("STATIC_PREPARE_TOOL_FAILED", "실패")
    with pytest.raises(JobFailed) as err:
        handler(store, LoggedFailure(), tmp_path)(context(store))
    assert err.value.code == "STATIC_PREPARE_TOOL_FAILED"
    assert "s3://site/output/failed-prepare.log" in err.value.message
    assert store.puts["failed-prepare.log"] == b"synthetic tool diagnostic"
    assert store.puts["failed-prepare.log"] == b"synthetic tool diagnostic"
    assert not list(tmp_path.iterdir())


def test_upload_skips_empty_logs_and_includes_filename_on_empty_asset_or_auxiliary(tmp_path):
    store = Store()

    class MixedLogsStages(FakeStages):
        def run(self, stage, root, **kwargs):
            super().run(stage, root, **kwargs)
            if stage == "prepare":
                (root / "prepare.log").write_bytes(b"")
            elif stage == "bake":
                (root / "bake.log").write_bytes(b"texrecon completed\n")

    result = handler(store, MixedLogsStages(), tmp_path)(context(store))
    assert "prepare.log" not in store.puts
    assert "prepare.log=" not in result["quality"]["notes"]
    assert store.puts["bake.log"] == b"texrecon completed\n"
    assert "bake.log=s3://site/output/bake.log" in result["quality"]["notes"]

    class EmptyAssetStages(FakeStages):
        def run(self, stage, root, **kwargs):
            super().run(stage, root, **kwargs)
            if stage == "bake":
                (root / "scene.glb").write_bytes(b"")

    with pytest.raises(JobFailed) as asset_err:
        handler(Store(), EmptyAssetStages(), tmp_path)(context(store))
    assert asset_err.value.code == "STATIC_OUTPUT_EMPTY"
    assert "scene.glb" in asset_err.value.message

    class EmptyAuxStages(FakeStages):
        def run(self, stage, root, **kwargs):
            super().run(stage, root, **kwargs)
            if stage == "bake":
                (root / "observed_camera_0000.png").write_bytes(b"")
                rec = json.loads((root / "bake.json").read_text(encoding="utf-8"))
                rec["auxiliary"] = ["observed_camera_0000.png"]
                write_json(root / "bake.json", rec)

    with pytest.raises(JobFailed) as aux_err:
        handler(Store(), EmptyAuxStages(), tmp_path)(context(store))
    assert aux_err.value.code == "STATIC_OUTPUT_EMPTY"
    assert "observed_camera_0000.png" in aux_err.value.message


def test_preview_only_mode_emits_preview_candidate_without_bake(ec2, make_worker, tmp_path):
    """-451: mode='preview' 요청은 texrecon/TSDF 굽기를 건너뛰고 preview_scene CANDIDATE 를 바로 낸다."""
    store, stages = Store(), FakeStages()
    h = handler(store, stages, tmp_path)
    inp = input_for(store)
    inp["options"] = {"mode": "preview", "preview_stride": 2}
    job_id = ec2.add_job("static_reconstruct", inp, timeout_ms=60000)
    assert make_worker({"static_reconstruct": h}).run_once() is Outcome.ACCEPTED
    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED", job.error
    assert schema_errors(job.result, "scene_output") == []
    assert stages.calls == ["prepare", "mapanything", "preview", "measure"]
    assert job.result["scene_version"].startswith("scene-preview-")
    assert job.result["state"] == "CANDIDATE"
    assert job.result["geometry_role"] == "VISUAL"
    assert job.result["method"]["reconstructor_detail"] == "preview_scene"
    assert job.result["assets"][0]["triangle_count"] == 1200
    assert "scene_tier=PREVIEW; preview_scene=true; texture=null: PREVIEW_FAST_PATH" in job.result["quality"]["notes"]


def test_two_stage_mode_emits_preview_candidate_then_full_scene_for_seamless_swap(tmp_path):
    """-451: 2단계 모드에서 preview 후보가 먼저 콜백·저장소로 나가고, 이어 구운 고화질 후보가 같은 보정 버전으로 등록된다."""
    store, stages = Store(), FakeStages()
    preview_events = []

    def on_preview(preview_doc):
        # preview 콜백 시점에는 아직 bake 가 실행되기 전이어야 한다
        assert "bake" not in stages.calls
        assert schema_errors(preview_doc, "scene_output") == []
        preview_events.append(preview_doc)

    h = StaticReconstructHandler(
        store,
        clock_ms=lambda: 1_800_000_000_000,
        stages=stages,
        workspace=tmp_path,
        on_preview=on_preview,
    )
    ctx = context(store)
    ctx.input["options"] = {"mode": "two_stage", "target_faces": 150_000, "num_threads": 0}
    full_result = h(ctx)

    assert len(preview_events) == 1
    prev = preview_events[0]
    assert prev["scene_version"] == "scene-preview-job-364-lease-1"
    assert prev["state"] == "CANDIDATE" and prev["geometry_role"] == "VISUAL"
    assert prev["calibration_version"] == full_result["calibration_version"]
    assert "scene_tier=PREVIEW; preview_scene=true" in prev["quality"]["notes"]

    assert stages.calls == ["prepare", "mapanything", "preview", "bake", "measure"]
    assert schema_errors(full_result, "scene_output") == []
    assert full_result["scene_version"] == "scene-job-364-lease-1"
    assert full_result["state"] == "CANDIDATE"
    assert "scene_tier=FULL; replaces_preview_scene=scene-preview-job-364-lease-1" in full_result["quality"]["notes"]

    metrics = json.loads(store.puts["metrics.json"])
    assert metrics["preview_scene"]["scene_version"] == "scene-preview-job-364-lease-1"
    assert "preview_scene_output.json" in store.puts


def test_live_admission_gate_still_blocks_static_reconstruct(tmp_path):
    """-451: LIVE 관제 중에는 Tier 0 간섭 방지를 위해 static_reconstruct 가 계속 차단된다."""
    from simai_ai.gpu.admission import DEFAULT_LIVE_BLOCKED, AdmissionGate, AdmissionPolicy

    assert "static_reconstruct" in DEFAULT_LIVE_BLOCKED
    marker = tmp_path / "tier0.live"
    live_gate = AdmissionGate(marker, AdmissionPolicy(allow_without_marker=False), wall_s=lambda: 100.0, monotonic=lambda: 100.0)
    decision_live = live_gate.check()
    assert decision_live.live and decision_live.blocks("static_reconstruct")
    assert "static_reconstruct" not in decision_live.allowed_types(["static_reconstruct", "vlm_explain"])

    setup_gate = AdmissionGate(marker, AdmissionPolicy(allow_without_marker=True), wall_s=lambda: 100.0, monotonic=lambda: 100.0)
    decision_setup = setup_gate.check()
    assert not decision_setup.live and decision_setup.admit and not decision_setup.blocks("static_reconstruct")
    assert "static_reconstruct" in decision_setup.allowed_types(["static_reconstruct", "vlm_explain"])
