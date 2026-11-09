from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest

from experiments.geometry_bench.metrics import (
    compute_edge_sharpness_for_view,
    parse_glb_primitives_world,
)
from experiments.geometry_bench.mvs_io import (
    UndistortedCamera,
    _write_rgb_image,
    adapt_mvsi_wide_baseline_neighbors,
    aggregate_multiframe_depths,
    build_visible_point_cloud_from_depths,
    export_colmap_text_workspace,
    from_glb_points,
    parse_openmvs_obj_and_export_glb,
    quaternion_wxyz_to_rotmat,
    read_openmvs_mvs_summary,
    rotmat_to_quaternion_wxyz,
    to_glb_points,
    write_openmvs_mvs_file,
)


def _make_dummy_cameras(tmp_path: Path) -> dict[str, UndistortedCamera]:
    cams: dict[str, UndistortedCamera] = {}
    K = np.array([[500.0, 0.0, 32.0], [0.0, 500.0, 24.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    for idx, cid in enumerate(["cam_A", "cam_B"]):
        arr = np.full((48, 64, 3), 120 + idx * 30, dtype=np.uint8)
        R = np.eye(3, dtype=np.float64)
        t = np.array([-idx * 0.25, 0.0, 0.0], dtype=np.float64)
        c2w = np.eye(4, dtype=np.float64)
        c2w[:3, :3] = R.T
        c2w[:3, 3] = -R.T @ t
        cams[cid] = UndistortedCamera(
            camera_id=cid,
            width=64,
            height=48,
            K=K.copy(),
            R_w2c=R,
            t_w2c=t,
            cam2world=c2w,
            image_rgb=arr,
            valid_mask=np.ones((48, 64), dtype=bool),
        )
    return cams


def test_coordinate_and_quaternion_roundtrip() -> None:
    pts_world = np.array(
        [[1.25, -2.5, 0.0], [0.0, 3.1, 1.8], [-0.4, -0.9, 2.2]],
        dtype=np.float64,
    )
    pts_glb = to_glb_points(pts_world)
    # GLTF_Y_UP: (x, z, -y)
    np.testing.assert_allclose(pts_glb[:, 0], pts_world[:, 0], atol=1e-6)
    np.testing.assert_allclose(pts_glb[:, 1], pts_world[:, 2], atol=1e-6)
    np.testing.assert_allclose(pts_glb[:, 2], -pts_world[:, 1], atol=1e-6)
    pts_back = from_glb_points(pts_glb)
    np.testing.assert_allclose(pts_back, pts_world, atol=1e-6)

    angles = [0.2, -0.5, 1.1]
    cx, cy, cz = np.cos(angles)
    sx, sy, sz = np.sin(angles)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    R = Rz @ Ry @ Rx
    q = rotmat_to_quaternion_wxyz(R)
    R_back = quaternion_wxyz_to_rotmat(q)
    np.testing.assert_allclose(R_back, R, atol=1e-6)


def test_aggregate_multiframe_depths_preserves_unobserved_and_suppresses_outliers() -> None:
    base = np.full((16, 16), 3.0, dtype=np.float32)
    base[0:4, 0:4] = np.nan  # unobserved region must remain masked False (0.0), never treated as valid

    f1 = base.copy()
    f2 = base.copy() * 1.15  # scale drift on frame 2
    f3 = base.copy()
    f3[8:10, 8:10] = 9.5  # transient foreground outlier in 1 frame

    med_depths, masks, stats = aggregate_multiframe_depths(
        {"cam_A": [f1, f2, f3]},
        align_scale_to_first=True,
        min_valid_frames=1,
    )
    med = med_depths["cam_A"]
    mask = masks["cam_A"]
    assert med.shape == (16, 16)
    # Unobserved region is masked False (AGENTS.md Rule 2 & 7)
    assert not bool(mask[0:4, 0:4].any())
    assert bool(mask[5:, 5:].all())
    # Observed region recovers 3.0m despite frame 2 scale drift and frame 3 outlier
    np.testing.assert_allclose(med[5:, 5:], 3.0, atol=1e-4)
    assert stats["cam_A"]["num_frames"] == 3


def test_build_visible_point_cloud_and_openmvs_binary_roundtrip(tmp_path: Path) -> None:
    cams = _make_dummy_cameras(tmp_path)
    dA = np.full((48, 64), 2.5, dtype=np.float32)
    dB = np.full((48, 64), 2.5, dtype=np.float32)
    pts_xyz, pts_rgb, point_views, pt_stats = build_visible_point_cloud_from_depths(
        cams,
        {"cam_A": dA, "cam_B": dB},
        stride=8,
        covis_rel_tol=0.10,
    )
    assert len(pts_xyz) > 10
    assert pt_stats["multi_view_points"] > 0

    colmap_info = export_colmap_text_workspace(
        tmp_path / "colmap",
        cams,
        points_xyz=pts_xyz[:25],
        points_rgb=pts_rgb[:25],
        point_views=point_views[:25],
    )
    assert colmap_info["cameras_txt"].is_file()
    assert colmap_info["images_txt"].is_file()
    assert colmap_info["points3d_txt"].is_file()
    # Ensure Unix LF line endings (no CRLF) for Linux OpenMVS compatibility
    raw_bytes = colmap_info["images_txt"].read_bytes()
    assert b"\r\n" not in raw_bytes

    mvs_path = tmp_path / "scene.mvs"
    write_openmvs_mvs_file(
        mvs_path,
        cams,
        points_xyz=pts_xyz[:25],
        points_rgb=pts_rgb[:25],
        point_views=point_views[:25],
    )
    summary = read_openmvs_mvs_summary(mvs_path)
    assert summary["num_platforms"] == 2
    assert summary["num_images"] == 2
    assert summary["num_vertices"] == 25
    assert summary["bytes_consumed"] == summary["file_size"]
    # Verify OpenMVS K_norm normalizes cx and cy by max(width, height) = 64.0
    k_norm = summary["platforms"][0]["cameras"][0]["K_norm"]
    np.testing.assert_allclose(k_norm[0, 2], 32.0 / 64.0, atol=1e-6)
    np.testing.assert_allclose(k_norm[1, 2], 24.0 / 64.0, atol=1e-6)


def test_prepare_reconstruct_job_root_and_check_mode(tmp_path: Path) -> None:
    from experiments.geometry_bench.run_bench import (
        prepare_reconstruct_job_root,
        run_check_mode,
    )

    calib_doc = {
        "calibration_version": "cal-test-v1",
        "cameras": [
            {
                "camera_id": "cam_A",
                "intrinsics": {
                    "width": 64,
                    "height": 48,
                    "K": [[50.0, 0.0, 32.0], [0.0, 50.0, 24.0], [0.0, 0.0, 1.0]],
                    "distortion": [0.0, 0.0, 0.0, 0.0, 0.0],
                },
                "extrinsics": {
                    "R_world_to_cam": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]],
                    "t_world_to_cam": [0.0, 0.0, 0.0],
                },
            }
        ],
    }
    calib_p = tmp_path / "calib.json"
    calib_p.write_text(json.dumps(calib_doc), encoding="utf-8")
    imgs_dir = tmp_path / "imgs"
    imgs_dir.mkdir()
    _write_rgb_image(imgs_dir / "cam_A.png", np.full((48, 64, 3), 128, dtype=np.uint8))

    root_out = tmp_path / "ma_root"
    setup_res = prepare_reconstruct_job_root(
        root_out,
        calib_p,
        imgs_dir,
        run_id="unit_test_ma",
        calibration_version="cal-test-v1",
        run_prepare_stage=False,
    )
    assert setup_res["cameras"] == ["cam_A"]
    assert (root_out / "job.json").is_file()
    assert (root_out / "calibration.json").is_file()
    assert (root_out / "images" / "cam_A.image").is_file()

    # Run check mode (symbol & CLI verification)
    check_res = run_check_mode(
        calib_path=tmp_path / "nonexistent_calib.json",
        bundle_dir=tmp_path / "nonexistent_bundle",
        out_dir=tmp_path / "check_out",
    )
    assert check_res["status"] == "OK"
    assert "simai_ai.scene.reconstruct_tasks" in check_res["python_symbols"]
    assert "simai_ai.scene.texture_mesh" in check_res["python_symbols"]
    assert "simai_ai.scene.run_hybrid_depth" in check_res["python_symbols"]


def test_obj_to_glb_and_parse_primitives_roundtrip(tmp_path: Path) -> None:
    tex_img = np.full((16, 16, 3), [180, 90, 40], dtype=np.uint8)
    _write_rgb_image(tmp_path / "tex0.png", tex_img)
    (tmp_path / "test.mtl").write_text(
        "newmtl material_0\nKa 1 1 1\nKd 1 1 1\nmap_Kd tex0.png\n",
        encoding="utf-8",
    )
    obj_text = "\n".join(
        [
            "mtllib test.mtl",
            "v 0.0 0.0 0.0",
            "v 1.0 0.0 0.0",
            "v 0.0 2.0 0.5",
            "vt 0.1 0.1",
            "vt 0.9 0.1",
            "vt 0.1 0.9",
            "usemtl material_0",
            "f 1/1/0 2/2/0 3/3/0",
            "",
        ]
    )
    obj_path = tmp_path / "test.obj"
    obj_path.write_text(obj_text, encoding="utf-8")

    V_w = np.array([[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 2.0, 0.5]], dtype=np.float32)
    F_w = np.array([[0, 1, 2]], dtype=np.int32)
    glb_path = tmp_path / "out.glb"
    stats = parse_openmvs_obj_and_export_glb(obj_path, V_w, F_w, glb_path)
    assert stats["total_faces"] == 1
    assert stats["observed_faces"] == 1
    assert glb_path.is_file()

    parsed = parse_glb_primitives_world(glb_path)
    assert parsed["mesh_faces"] == 1
    assert parsed["observed_faces"] == 1
    np.testing.assert_allclose(parsed["V_world"], V_w, atol=1e-5)


def test_compute_edge_sharpness_for_view() -> None:
    photo = np.zeros((64, 64, 3), dtype=np.uint8)
    photo[:, 32:, :] = 220
    render = photo.copy()
    mask = np.ones((64, 64), dtype=bool)
    depth = np.full((64, 64), 2.0, dtype=np.float32)
    depth[:, 32:] = 3.5

    res = compute_edge_sharpness_for_view(photo, render, depth, mask)
    assert res["grad_cos_sim"] is not None and res["grad_cos_sim"] > 0.95
    assert res["edge_f1_px2"] is not None and res["edge_f1_px2"] > 0.95
    assert res["edge_chamfer_px"] is not None and res["edge_chamfer_px"] < 0.5
    assert res["depth_edge_photo_dist_px"] is not None and res["depth_edge_photo_dist_px"] < 1.5


def test_adapt_mvsi_wide_baseline_neighbors(tmp_path: Path) -> None:
    payload = bytearray(128)
    struct.pack_into("<IIfff", payload, 32, 1, 50000, 1.15, 1.10, 0.35)
    hdr = b"MVS\x00" + struct.pack("<IIQ", 1, 2, len(payload))
    mvsi_in = tmp_path / "recon.mvs"
    mvsi_out = tmp_path / "recon_wide.mvs"
    mvsi_in.write_bytes(hdr + zlib.compress(bytes(payload)))

    res = adapt_mvsi_wide_baseline_neighbors(mvsi_in, mvsi_out, n_cams=4)
    assert res["updated_neighbor_entries"] == 1
    out_raw = mvsi_out.read_bytes()
    out_decomp = zlib.decompress(out_raw[20:])
    id_v, pts_v, scl, ang, scr = struct.unpack_from("<IIfff", out_decomp, 32)
    assert id_v == 1 and pts_v == 50000
    assert abs(ang - 0.70) < 1e-5
    assert abs(scr - 0.35) < 1e-5


def test_run_record_conforms_to_contract_schema() -> None:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    repo_root = Path(__file__).resolve().parents[4]
    contracts_dir = repo_root / "contracts"
    registry = Registry()
    for p in sorted(contracts_dir.rglob("*.schema.json")):
        s = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(s["$id"], Resource.from_contents(s))

    schema_path = contracts_dir / "experiments" / "run_record.schema.json"
    record_path = repo_root / "ai" / "experiments" / "geometry_bench" / "run_record.json"
    assert record_path.is_file()
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, registry=registry)
    errors = list(validator.iter_errors(record))
    assert not errors, [e.message for e in errors]


def test_mapanything_mask_conditions_and_selection_rule(tmp_path: Path) -> None:
    from experiments.geometry_bench.hole_analysis import (
        MASK_CONDITIONS,
        _build_pinhole_rays_world,
        _resize_mask_nearest,
        build_depth_views_for_mask_condition,
        select_best_mask_condition,
    )

    ma_run = tmp_path / "mapanything"
    ma_run.mkdir(parents=True)

    h, w = 20, 30
    depth = np.full((h, w), 3.0, dtype=np.float32)
    conf = np.linspace(1.0, 10.0, h * w, dtype=np.float32).reshape(h, w)
    # Default edge mask cuts out the last 4 columns
    mask_def = np.ones((h, w), dtype=bool)
    mask_def[:, -4:] = False
    K = np.array([[25.0, 0.0, 15.0], [0.0, 25.0, 10.0], [0.0, 0.0, 1.0]], dtype=np.float32)
    c2w_A = np.eye(4, dtype=np.float32)
    c2w_B = np.eye(4, dtype=np.float32)
    c2w_B[0, 3] = 3.0  # 3.0 m baseline so _check_units passes

    np.savez_compressed(
        ma_run / "raw.npz",
        names=np.array(["cam_A", "cam_B"]),
        cam_A__depth_z=depth,
        cam_A__conf=conf,
        cam_A__mask=mask_def,
        cam_A__intrinsics=K,
        cam_A__camera_pose=c2w_A,
        cam_B__depth_z=depth,
        cam_B__conf=conf,
        cam_B__mask=mask_def,
        cam_B__intrinsics=K,
        cam_B__camera_pose=c2w_B,
    )
    cams_map = {
        "cam_A": {"camera_id": "cam_A", "width": w, "height": h, "K": K.tolist(), "cam2world": c2w_A.tolist()},
        "cam_B": {"camera_id": "cam_B", "width": w, "height": h, "K": K.tolist(), "cam2world": c2w_B.tolist()},
    }
    cams_doc = {
        "convention": "cam2world_opencv",
        "views": [
            {"name": "cam_A", "width": w, "height": h, "K": K.tolist(), "cam2world": c2w_A.tolist()},
            {"name": "cam_B", "width": w, "height": h, "K": K.tolist(), "cam2world": c2w_B.tolist()},
        ],
    }
    (ma_run / "cameras.json").write_text(json.dumps(cams_doc), encoding="utf-8")

    spec_by_id = {s.condition_id: s for s in MASK_CONDITIONS}
    _, _, st_A = build_depth_views_for_mask_condition(
        ma_run, {}, cams_map, spec_by_id["A_baseline"]
    )
    _, _, st_B = build_depth_views_for_mask_condition(
        ma_run, {}, cams_map, spec_by_id["B_no_edge_mask"]
    )
    _, _, st_C = build_depth_views_for_mask_condition(
        ma_run, {}, cams_map, spec_by_id["C_conf_relaxed_p2"]
    )
    _, _, st_D = build_depth_views_for_mask_condition(
        ma_run, {}, cams_map, spec_by_id["D_both_off"]
    )

    assert st_B["valid_pixel_ratio_mean"] > st_A["valid_pixel_ratio_mean"]
    assert st_C["valid_pixel_ratio_mean"] > st_A["valid_pixel_ratio_mean"]
    assert st_D["valid_pixel_ratio_mean"] > st_B["valid_pixel_ratio_mean"]
    assert st_D["valid_pixel_ratio_mean"] > st_C["valid_pixel_ratio_mean"]

    # Verify selection rule rejects conditions that spawn excessive floating clusters or low photo support
    mock_results = {
        "A_baseline": {
            "priority_metrics": {
                "1_same_cam_black_hole_ratio_mean": 0.120,
                "2_new_faces_photo_supported_ratio": 1.0,
                "3_floating_cluster_area_m2": 0.20,
                "4_cross_view_reproj_median_px": 3.5,
            }
        },
        "B_no_edge_mask": {
            "priority_metrics": {
                "1_same_cam_black_hole_ratio_mean": 0.085,
                "2_new_faces_photo_supported_ratio": 0.30,  # Rejected (< 0.50)
                "3_floating_cluster_area_m2": 1.40,         # Also exceeds +0.75 m^2
                "4_cross_view_reproj_median_px": 4.2,
            }
        },
        "C_conf_relaxed_p2": {
            "priority_metrics": {
                "1_same_cam_black_hole_ratio_mean": 0.092,
                "2_new_faces_photo_supported_ratio": 0.88,
                "3_floating_cluster_area_m2": 0.32,
                "4_cross_view_reproj_median_px": 3.8,
            }
        },
        "D_both_off": {
            "priority_metrics": {
                "1_same_cam_black_hole_ratio_mean": 0.070,
                "2_new_faces_photo_supported_ratio": 0.75,
                "3_floating_cluster_area_m2": 1.50,  # Rejected (+1.30 m^2 > 0.75 m^2)
                "4_cross_view_reproj_median_px": 5.5,
            }
        },
    }
    sel = select_best_mask_condition(mock_results)
    assert sel["selected_condition_id"] == "C_conf_relaxed_p2"
    assert "B_no_edge_mask" in sel["rejected_conditions"]
    assert "D_both_off" in sel["rejected_conditions"]

    rays = _build_pinhole_rays_world(K, np.eye(3), np.zeros(3), w, h)
    assert rays.shape == (h, w, 6)
    resized = _resize_mask_nearest(mask_def, 60, 40)
    assert resized.shape == (40, 60)


def test_v2_cleanup_stage_masks_and_glb_filter_in_place(tmp_path: Path) -> None:
    from experiments.geometry_bench.mvs_io import _encode_rgb_png_bytes
    from experiments.geometry_bench.v2_clean import (
        compute_v2_cleanup_stage_masks,
        filter_glb_triangles_in_place,
    )

    tex_img = np.full((8, 8, 3), 200, dtype=np.uint8)
    (tmp_path / "material_0_map_Kd.png").write_bytes(_encode_rgb_png_bytes(tex_img))
    mtl_text = "newmtl material_0\nmap_Kd material_0_map_Kd.png\n"
    (tmp_path / "test.mtl").write_text(mtl_text, encoding="utf-8")

    # 3 triangles in World coordinates:
    # Face 0: valid small floor triangle at Z=0.0 (edge <= 0.05m)
    # Face 1: stretched bridge triangle with edge 0.50m (> 0.15m -> removed at stage a)
    # Face 2: high ceiling triangle at Z=2.90m (> 2.65m -> removed at stage b)
    V_w = np.array(
        [
            [2.00, 2.00, 0.00],
            [2.04, 2.00, 0.00],
            [2.00, 2.04, 0.00],
            [2.00, 2.00, 0.50],
            [2.50, 2.00, 0.50],
            [2.00, 2.50, 0.50],
            [2.00, 2.00, 2.90],
            [2.04, 2.00, 2.90],
            [2.00, 2.04, 2.90],
        ],
        dtype=np.float32,
    )
    F_w = np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8]], dtype=np.int32)

    obj_lines = ["mtllib test.mtl"]
    for v in V_w:
        obj_lines.append(f"v {v[0]} {v[1]} {v[2]}")
    obj_lines.extend(["vt 0.1 0.1", "vt 0.9 0.1", "vt 0.1 0.9", "usemtl material_0"])
    obj_lines.extend(
        [
            "f 1/1/0 2/2/0 3/3/0",
            "f 4/1/0 5/2/0 6/3/0",
            "f 7/1/0 8/2/0 9/3/0",
        ]
    )
    obj_path = tmp_path / "test.obj"
    obj_path.write_text("\n".join(obj_lines) + "\n", encoding="utf-8")

    in_glb = tmp_path / "in.glb"
    parse_openmvs_obj_and_export_glb(obj_path, V_w, F_w, in_glb)
    parsed = parse_glb_primitives_world(in_glb)
    assert parsed["mesh_faces"] == 3

    # Camera at (2.0, 2.0, 2.4) looking straight down (-Z in World)
    R_w2c = np.array([[1.0, 0.0, 0.0], [0.0, -1.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float64)
    C_w = np.array([2.0, 2.0, 2.4], dtype=np.float64)
    t_w2c = -R_w2c @ C_w
    c2w = np.eye(4, dtype=np.float64)
    c2w[:3, :3] = R_w2c.T
    c2w[:3, 3] = C_w
    cam = UndistortedCamera(
        camera_id="cam_A",
        width=100,
        height=100,
        K=np.array([[80.0, 0.0, 50.0], [0.0, 80.0, 50.0], [0.0, 0.0, 1.0]], dtype=np.float64),
        R_w2c=R_w2c,
        t_w2c=t_w2c,
        cam2world=c2w,
        image_rgb=np.zeros((100, 100, 3), dtype=np.uint8),
        valid_mask=np.ones((100, 100), dtype=bool),
    )

    res = compute_v2_cleanup_stage_masks(parsed, {"cam_A": cam}, min_cluster_faces=1)
    masks = res["masks"]
    assert list(masks["a_long_edge"]) == [True, False, True]
    assert list(masks["b_room_ceiling_bounds"]) == [True, False, False]
    assert list(masks["c_grazing_all_cameras"]) == [True, False, False]
    assert list(masks["d_small_components"]) == [True, False, False]

    out_glb = tmp_path / "out_clean.glb"
    f_info = filter_glb_triangles_in_place(in_glb, out_glb, masks["d_small_components"])
    assert f_info["input_faces"] == 3
    assert f_info["kept_faces"] == 1
    assert f_info["removed_faces"] == 2
    parsed_clean = parse_glb_primitives_world(out_glb)
    assert parsed_clean["mesh_faces"] == 1
    np.testing.assert_allclose(parsed_clean["V_world"], V_w[:3], atol=1e-5)


def test_native_binary_lookup_skips_unreadable_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    """일반 사용자(Jenkins)에게 /root/... 후보는 PermissionError 다. 없는 것으로 보고 다음 후보로 간다."""
    from experiments.geometry_bench import mvs_io
    from experiments.geometry_bench.run_bench import find_native_texrecon_binary

    def _deny(self: Path) -> bool:
        if str(self).replace("\\", "/").startswith("/root/"):
            raise PermissionError(13, "Permission denied", str(self))
        return False

    monkeypatch.setattr(Path, "is_file", _deny)
    monkeypatch.delenv("SIMAI_OPENMVS_BIN_DIR", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.delenv("SIMAI_TEXRECON", raising=False)
    monkeypatch.setattr(mvs_io.shutil, "which", lambda _name: None)
    monkeypatch.setattr("experiments.geometry_bench.run_bench.shutil.which", lambda _name: None)
    assert mvs_io.find_native_openmvs_binary("TextureMesh") is None
    assert find_native_texrecon_binary() is None
