"""실제 CPU 입력 준비·World 정렬·정점색 폴백을 작은 합성 자료로 확인한다."""
import json
import subprocess
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


@pytest.mark.parametrize("stage", ["prepare", "mapanything", "preview", "bake", "fallback", "measure"])
def test_subprocess_stage_entrypoints_do_not_fail_with_import_error(tmp_path, stage):
    stage_dir = tmp_path / stage
    stage_dir.mkdir()
    env = ProcessStages().env
    proc = subprocess.run(
        [sys.executable, "-m", "simai_ai.scene.reconstruct_tasks", stage, str(stage_dir)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 1
    assert "ImportError" not in proc.stderr
    err = read_json(stage_dir / f"{stage}.error.json")
    assert err["message"] != "ImportError"
    assert err["code"] != "STATIC_GPU_BUDGET_FAILED"
    if stage == "mapanything":
        assert err["code"] == "STATIC_MODEL_MISSING"


def test_process_stage_prepare_runs_end_to_end_in_subprocess(tmp_path):
    synthetic(tmp_path)
    ProcessStages().run("prepare", tmp_path, timeout_s=30, cancelled=threading.Event())
    prepared = read_json(tmp_path / "prepared" / "prepared.json")
    assert len(prepared["views"]) == 2


def _parse_glb(raw: bytes):
    import struct

    magic, ver, total = struct.unpack_from("<4sII", raw, 0)
    assert (magic, ver, total) == (b"glTF", 2, len(raw))
    jlen, jtype = struct.unpack_from("<I4s", raw, 12)
    assert jtype == b"JSON"
    doc = json.loads(raw[20 : 20 + jlen].decode("utf-8"))
    blen, btype = struct.unpack_from("<I4s", raw, 20 + jlen)
    assert btype == b"BIN\x00"
    bin_buf = raw[28 + jlen : 28 + jlen + blen]
    return doc, bin_buf


def test_preview_gltf_from_synthetic_pointcloud_and_depth_views(tmp_path):
    """-451: 작은 합성 점구름과 깊이 시점에서 빠른 미리보기 glTF(GLTF_Y_UP, 정점색, PREVIEW 표시)를 생성한다."""
    views = synthetic(tmp_path)
    tasks.prepare(tmp_path)
    tasks.align_to_calibration(tmp_path)

    # 1) 합성 점구름(모델 좌표 z=2.0 -> World z=4.0 -> glTF Y=+4.0)
    gx, gy = np.meshgrid(np.linspace(0.0, 0.2, 4), np.linspace(0.0, 0.2, 4))
    pts = np.column_stack([gx.ravel(), gy.ravel(), np.full(gx.size, 2.0)])
    cols = np.full((len(pts), 3), [40, 120, 200], dtype=np.uint8)
    np.savez(tmp_path / "mapanything" / "pointcloud.npz", points_xyz=pts, colors_rgb=cols)

    tasks.preview(tmp_path)
    prev = read_json(tmp_path / "preview.json")
    assert prev["scene_tier"] == "PREVIEW" and prev["preview_scene"] is True
    assert prev["fallback_reason"] == "PREVIEW_FAST_PATH"
    assert prev["assets"][0]["path"] == "preview_scene.glb"
    assert prev["assets"][0]["triangle_count"] > 0

    doc, bin_buf = _parse_glb((tmp_path / "preview_scene.glb").read_bytes())
    simai = doc["extras"]["simai"]
    assert simai["scene_tier"] == "PREVIEW" and simai["preview_scene"] is True
    assert simai["geometry_role"] == "VISUAL" and simai["safety_use"] is False
    assert simai["axis_convention"] == "GLTF_Y_UP"
    assert simai["vertex_count"] == len(pts)
    assert simai["triangle_count"] == prev["assets"][0]["triangle_count"]

    pos_view = doc["bufferViews"][0]
    verts_glb = np.frombuffer(
        bin_buf,
        dtype="<f4",
        count=len(pts) * 3,
        offset=pos_view["byteOffset"],
    ).reshape(-1, 3)
    # 모델 z=2 -> World z=4 (scale=2) -> glTF Y=4 (단일 변환 검증)
    np.testing.assert_allclose(verts_glb[:, 1], 4.0, atol=1e-5)

    # 2) MapAnything 깊이 시점(raw.npz) 입력에서도 preview_stride 로 빠르게 격자 메시 glTF 생성
    (tmp_path / "mapanything" / "pointcloud.npz").unlink()
    job = read_json(tmp_path / "job.json")
    job["input"]["options"] = {"preview_stride": 2}
    write_json(tmp_path / "job.json", job)
    raw = {}
    for v in views:
        cid = v["name"]
        raw.update(
            {
                f"{cid}__depth_z": np.full((48, 64), 2.0, np.float32),
                f"{cid}__conf": np.ones((48, 64)),
                f"{cid}__mask": np.ones((48, 64), bool),
                f"{cid}__intrinsics": np.array(v["intrinsics_model_res"]),
                f"{cid}__camera_pose": np.array(v["cam2world"]),
            }
        )
    np.savez(tmp_path / "mapanything" / "raw.npz", **raw)
    tasks.preview(tmp_path)
    prev2 = read_json(tmp_path / "preview.json")
    assert prev2["metrics"]["preview_stride"] == 2
    assert prev2["assets"][0]["triangle_count"] > 0
    assert prev2["metrics"]["build_s"] >= 0.0


def test_fuse_mesh_decimation_face_count_and_stage_timings(monkeypatch):
    """-451: TSDF 메시가 target_faces 보다 크면 decimation 단계로 면 수를 줄이고 단계별 시간과 스레드 수를 기록한다."""
    import os
    from simai_ai.scene import texture_mesh as tm

    gx, gy = np.meshgrid(np.linspace(0.0, 2.0, 50), np.linspace(0.0, 2.0, 50))
    V_full = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(gx.size, dtype=float)])
    F_list = []
    for r in range(49):
        for c in range(49):
            i0 = r * 50 + c
            F_list.append([i0, i0 + 1, i0 + 50])
            F_list.append([i0 + 1, i0 + 51, i0 + 50])
    F_full = np.asarray(F_list, dtype=np.int64)
    assert len(F_full) == 4802

    class FakeMesh:
        def __init__(self, verts, tris):
            self.vertices = np.asarray(verts, float)
            self.triangles = np.asarray(tris, np.int64)

        def remove_degenerate_triangles(self):
            return self

        def remove_duplicated_triangles(self):
            return self

        def remove_duplicated_vertices(self):
            return self

        def remove_non_manifold_edges(self):
            return self

        def remove_unreferenced_vertices(self):
            return self

        def cluster_connected_triangles(self):
            n = len(self.triangles)
            return np.zeros(n, dtype=np.int32), np.array([n], dtype=np.int64), np.array([1.0])

        def remove_triangles_by_mask(self, mask):
            self.triangles = self.triangles[~np.asarray(mask, dtype=bool)]

        def simplify_quadric_decimation(self, target_number_of_triangles):
            return FakeMesh(self.vertices, self.triangles[: int(target_number_of_triangles)])

    class FakeVolume:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def integrate(self, rgbd, intr, extr):
            pass

        def extract_triangle_mesh(self):
            return FakeMesh(V_full, F_full)

    fake_o3d = types.ModuleType("open3d")
    fake_o3d.__version__ = "0.19.0"
    fake_o3d.pipelines = types.SimpleNamespace(
        integration=types.SimpleNamespace(
            ScalableTSDFVolume=FakeVolume,
            TSDFVolumeColorType=types.SimpleNamespace(NoColor=0),
        )
    )
    fake_o3d.geometry = types.SimpleNamespace(
        Image=lambda arr: arr,
        RGBDImage=types.SimpleNamespace(create_from_color_and_depth=lambda *a, **k: (a, k)),
    )
    fake_o3d.camera = types.SimpleNamespace(PinholeCameraIntrinsic=lambda *a: a)
    monkeypatch.setitem(sys.modules, "open3d", fake_o3d)

    K = np.array([[80.0, 0.0, 16.0], [0.0, 80.0, 12.0], [0.0, 0.0, 1.0]])
    dview = tm.DepthView(
        name="cam_A",
        depth=np.full((24, 32), 3.0, dtype=np.float32),
        valid=np.ones((24, 32), dtype=bool),
        K=K,
        cam2world=np.eye(4),
    )
    cfg = tm.Config(target_faces=1500, num_threads=6, min_cluster_faces=10)
    V_out, F_out, rec = tm.fuse_mesh([dview], cfg)

    assert tm.DEFAULT_BAKE_TARGET_FACES == 150_000
    assert rec["tsdf_faces"] == 4802
    assert rec["faces_before_decimation"] == 4802
    assert rec["target_faces"] == 1500
    assert rec["decimated"] is True
    assert rec["faces"] == 1500 and len(F_out) == 1500 and len(V_out) == len(V_full)
    assert rec["num_threads"] == 6 and rec["requested_threads"] == 6
    assert os.environ.get("OMP_NUM_THREADS") == "6"
    for key in ("conflict_s", "tsdf_s", "clean_s", "decimate_s"):
        assert key in rec and isinstance(rec[key], float) and rec[key] >= 0.0


def test_bake_passes_threads_and_target_faces_to_texture_mesh(tmp_path, monkeypatch):
    """-451: bake 단계가 스레드 수(0=전체 코어 또는 지정값)와 target_faces(기본 15만)를 전달하고 단계별 시간을 남긴다."""
    from simai_ai.scene import texture_mesh as tm

    synthetic(tmp_path)
    job = read_json(tmp_path / "job.json")
    job.update(run_id="fast-bake-run")
    job["input"]["options"] = {"num_threads": 8, "target_faces": 150_000}
    write_json(tmp_path / "job.json", job)

    monkeypatch.setitem(sys.modules, "open3d", types.ModuleType("open3d"))
    monkeypatch.setattr(tasks.shutil, "which", lambda name: "fake-texrecon")
    seen = []

    def fake_main(args):
        seen.extend(args)
        out = tmp_path / "textured"
        out.mkdir(exist_ok=True)
        (out / "scene.glb").write_bytes(b"fake-glb")
        (out / "surface_source_0000.png").write_bytes(b"fake-lossless-map")
        write_json(
            out / "metrics.json",
            {
                "geometry": {
                    "tsdf_faces": 398_000,
                    "faces_before_decimation": 395_000,
                    "target_faces": 150_000,
                    "decimated": True,
                    "faces": 149_800,
                    "vertices": 76_000,
                },
                "threading": {"requested_num_threads": 8, "effective_num_threads": 8},
                "times": {
                    "load_depth_s": 0.4,
                    "tsdf_s": 4.2,
                    "decimate_s": 1.1,
                    "geometry_s": 6.5,
                    "bake_area_s": 38.0,
                    "total_s": 45.2,
                },
                "bakes": {
                    "area": {
                        "glb": "scene.glb",
                        "glb_triangles": 149_800,
                        "ratios_area": {
                            "OBSERVED": 0.75,
                            "INTERPOLATED": 0.05,
                            "UNOBSERVED": 0.2,
                            "ESTIMATED_PLANE": 0.0,
                        },
                    }
                },
            },
        )
        return 0

    monkeypatch.setattr(tm, "main", fake_main)
    tasks.bake(tmp_path)
    assert seen[seen.index("--num-threads") + 1] == "8"
    assert seen[seen.index("--target-faces") + 1] == "150000"

    rec = read_json(tmp_path / "bake.json")
    assert "DECIMATION" in rec["postprocess"]
    assert rec["assets"][0]["triangle_count"] == 149_800
    assert rec["target_faces"] == 150_000
    assert rec["num_threads"] == 8
    assert rec["stage_times"]["tsdf_s"] == 4.2
    assert rec["stage_times"]["decimate_s"] == 1.1

    # num_threads=0 기본값이면 모든 CPU 코어 수(>=1)로 풀린다
    tr = tm.Texrecon("fake-texrecon", tmp_path, num_threads=0)
    assert tr.requested_threads == 0 and tr.num_threads >= 1


def test_regenerate_quality_fields_maps_camera_key_to_scene_quality_name():
    from simai_ai.scene.reconstruct import regenerate_quality_fields

    out = regenerate_quality_fields({"regenerate_quality": {
        "previous_fill_ratio": 0.1, "unrevealed_ratio": 0.0,
        "cameras": {"cam1": {"previous_fill_ratio": 0.2, "unrevealed_ratio": 0.0}}}})
    assert out == {"previous_fill_ratio": 0.1, "unrevealed_ratio": 0.0,
                   "camera_background_quality": {"cam1": {"previous_fill_ratio": 0.2, "unrevealed_ratio": 0.0}}}
    assert regenerate_quality_fields({}) == {}


def test_alignment_failure_reports_camera_residuals_and_scale(tmp_path, caplog):
    synthetic(tmp_path)
    cameras = read_json(tmp_path / "mapanything" / "cameras.json")
    cameras["views"][1]["cam2world"][1][3] = 5.0
    write_json(tmp_path / "mapanything" / "cameras.json", cameras)
    with pytest.raises(ReconstructionError) as err:
        tasks.align_to_calibration(tmp_path)
    message = str(err.value)
    assert all(part in message for part in ("scale=", "cam_A: center=", "cam_B: center=", "rotation="))
    assert message in caplog.text


def test_texrecon_timing_records_stages_and_total():
    from simai_ai.scene.texture_mesh import record_texrecon_times
    times = {}
    seconds = {"label": 1.2345, "texture": 2.3456}
    record_texrecon_times(times, "area", seconds)
    assert times["texrecon_area_s"] == 3.58
    assert times["texrecon_area_label_s"] == seconds["label"]
    assert times["texrecon_area_texture_s"] == seconds["texture"]
