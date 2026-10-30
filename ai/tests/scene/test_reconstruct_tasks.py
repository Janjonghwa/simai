"""실제 CPU 입력 준비·World 정렬·정점색 폴백을 작은 합성 자료로 확인한다."""
import json
import threading
import sys
import types

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from simai_ai.scene import reconstruct_tasks as tasks
from simai_ai.scene.reconstruct import ReconstructionError, read_json, write_json
from simai_ai.scene.reconstruct_process import ProcessStages


def synthetic(root, *, frame=False):
    K = [[80., 0, 32], [0, 80., 24], [0, 0, 1]]
    (root / "images").mkdir()
    cams, images, views = [], [], []
    for i, cid in enumerate(("cam_A", "cam_B")):
        image = np.full((48, 64, 3), [30, 90, 150], np.uint8)
        ok, data = cv2.imencode(".jpg", image)
        assert ok
        (root / "images" / f"{cid}.image").write_bytes(data.tobytes())
        cams.append({"camera_id": cid, "K": K, "R": np.eye(3).tolist(), "t": [-float(i), 0., 0.],
                     "image_size": [64, 48], "distortion": {"model": "NONE", "coeffs": []}})
        images.append({"camera_id": cid, "kind": "FRAME" if frame else "CLEAN_BACKGROUND"})
        T = np.eye(4)
        T[0, 3] = i * .5
        views.append({"name": cid, "cam2world": T.tolist(), "model_input_hw": [48, 64], "intrinsics_model_res": K})
    write_json(root / "calibration.json", {"cameras": cams})
    write_json(root / "job.json", {"input": {"images": images, "input_provenance": {"frames": {"source": "SIM"}, "is_upper_bound": True}}})
    (root / "mapanything").mkdir()
    write_json(root / "mapanything" / "cameras.json", {"convention": "cam2world 4x4, OpenCV", "views": views})
    return views


def test_clean_prepare_skips_segmenter_and_stool_check_keeps_calibration(tmp_path, monkeypatch):
    synthetic(tmp_path)
    monkeypatch.delenv("SIMAI_SEG_WEIGHTS", raising=False)
    tasks.prepare(tmp_path)
    p = read_json(tmp_path / "prepared" / "prepared.json")
    assert p["segmenter"] is None and all(v["mask"] is None for v in p["views"])
    before = (tmp_path / "calibration.json").read_bytes()
    tasks.align_to_calibration(tmp_path)
    tasks.measure(tmp_path)
    result = read_json(tmp_path / "scale_check.json")
    assert result["scale"] is None and result["sample_count"] == 0
    assert not result["calibration_changed"] and before == (tmp_path / "calibration.json").read_bytes()


def test_frame_without_seg_fails(tmp_path, monkeypatch):
    synthetic(tmp_path, frame=True)
    monkeypatch.delenv("SIMAI_SEG_WEIGHTS", raising=False)
    with pytest.raises(ReconstructionError) as err:
        tasks.prepare(tmp_path)
    assert err.value.code == "STATIC_SEGMENTER_MISSING"


def test_world_alignment_and_missing_texrecon(tmp_path, monkeypatch):
    synthetic(tmp_path)
    M = tasks.align_to_calibration(tmp_path)
    np.testing.assert_allclose(M[:3, :3], 2 * np.eye(3))
    monkeypatch.setattr(tasks.shutil, "which", lambda name: None)
    tasks.bake(tmp_path)
    assert read_json(tmp_path / "bake.json") == {"texture": None, "fallback_reason": "TEXRECON_NOT_FOUND"}
    c = read_json(tmp_path / "mapanything" / "cameras.json")
    c["views"][1]["cam2world"][1][3] = 5.
    write_json(tmp_path / "mapanything" / "cameras.json", c)
    with pytest.raises(ReconstructionError, match="일치 조건"):
        tasks.align_to_calibration(tmp_path)


def test_real_vertex_mesh_fallback(tmp_path):
    trimesh = pytest.importorskip("trimesh")
    views = synthetic(tmp_path)
    tasks.prepare(tmp_path)
    tasks.align_to_calibration(tmp_path)
    raw = {}
    for v in views:
        cid = v["name"]
        raw.update({f"{cid}__depth_z": np.full((48, 64), 2., np.float32), f"{cid}__conf": np.ones((48, 64)),
            f"{cid}__mask": np.ones((48, 64), bool), f"{cid}__intrinsics": np.array(v["intrinsics_model_res"]),
            f"{cid}__camera_pose": np.array(v["cam2world"])})
    np.savez(tmp_path / "mapanything" / "raw.npz", **raw)
    write_json(tmp_path / "bake.json", {"texture": None, "fallback_reason": "TEXRECON_NOT_FOUND"})
    tasks.fallback(tmp_path)
    result = read_json(tmp_path / "bake.json")
    assert result["texture"] is None and result["assets"][0]["path"] == "scene.glb"
    scene = trimesh.load(tmp_path / "scene.glb", force="scene")
    assert sum(len(g.faces) for g in scene.geometry.values()) > 0
    # 모델 z=2 → World z=4 → glTF y=4 (변환을 두 번 하지 않음).
    for mesh in scene.geometry.values():
        np.testing.assert_allclose(mesh.vertices[:, 1], 4.)


def test_process_stage_failure_code(tmp_path):
    synthetic(tmp_path, frame=True)
    # GPU 초기화 전에 실패를 확인하는 CPU 단계. bake 는 잘못된 카메라 행렬을 기각한다.
    c = read_json(tmp_path / "mapanything" / "cameras.json")
    c["views"][0]["cam2world"][0][0] = 2
    write_json(tmp_path / "mapanything" / "cameras.json", c)
    with pytest.raises(ReconstructionError) as err:
        ProcessStages().run("bake", tmp_path, timeout_s=15, cancelled=threading.Event())
    assert err.value.code == "STATIC_WORLD_ALIGNMENT_FAILED"


def test_bake_uses_shared_library_and_exports_provenance(tmp_path, monkeypatch):
    from simai_ai.scene import texture_mesh
    synthetic(tmp_path)
    job = read_json(tmp_path / "job.json")
    job.update(run_id="fake-run")
    job["input"]["options"] = {"plane_cleanup": True, "align_views": "sim3"}
    write_json(tmp_path / "job.json", job)
    monkeypatch.setitem(sys.modules, "open3d", types.ModuleType("open3d"))
    monkeypatch.setattr(tasks.shutil, "which", lambda name: "fake-texrecon")
    seen = []
    def fake_main(args):
        seen.extend(args)
        out = tmp_path / "textured"
        out.mkdir()
        (out / "scene.glb").write_bytes(b"fake-glb")
        (out / "surface_source_0000.png").write_bytes(b"fake-lossless-map")
        write_json(out / "metrics.json", {"bakes": {"area": {"glb": "scene.glb", "ratios_area": {
            "OBSERVED": .6, "INTERPOLATED": .1, "UNOBSERVED": .2, "ESTIMATED_PLANE": .1}}}})
        return 0
    monkeypatch.setattr(texture_mesh, "main", fake_main)
    tasks.bake(tmp_path)
    rec = read_json(tmp_path / "bake.json")
    assert "--plane-cleanup" in seen and seen[seen.index("--align-views") + 1] == "sim3"
    assert rec["assets"][1]["ratios"]["ESTIMATED_PLANE"] == .1
    assert rec["observed_surface_ratio"] == .6 and "PLANE_CLEANUP" in rec["postprocess"]


def test_process_timeout_and_cancel_are_bounded(tmp_path):
    stages = ProcessStages()
    with pytest.raises(ReconstructionError) as err:
        stages.run("measure", tmp_path, timeout_s=.001, cancelled=threading.Event())
    assert err.value.code == "STATIC_MEASURE_TIMEOUT"
    event = threading.Event()
    event.set()
    with pytest.raises(ReconstructionError) as err:
        stages.run("measure", tmp_path, timeout_s=10, cancelled=event)
    assert err.value.code == "CANCELLED"
