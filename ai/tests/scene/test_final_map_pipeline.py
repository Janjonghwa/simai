"""S15P31A101-539: 최종 맵 파이프라인(pose_depth_fit 게이트 1~4, mesh_clean 4단계, hole_fill_core, reconstruct_tasks 통합) 단위 시험."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

from simai_ai.providers.contract import schema_errors
from simai_ai.scene import hole_fill_core as hfc
from simai_ai.scene import mesh_clean as mclean
from simai_ai.scene import pose_depth_fit as pdf
from simai_ai.scene import reconstruct_tasks as tasks
from simai_ai.scene.reconstruct import read_json, sha256, write_json


def _make_four_cam_synthetic_workspace(root: Path, *, perturb_calib_z_m: float = 0.0) -> dict:
    """4대 카메라가 바닥(Z=0)과 중앙 테이블 상판(Z=0.72m)을 함께 보는 합성 작업 폴더를 만든다."""
    (root / "images").mkdir(parents=True, exist_ok=True)
    (root / "prepared").mkdir(parents=True, exist_ok=True)
    (root / "mapanything").mkdir(parents=True, exist_ok=True)
    (root / "depth").mkdir(parents=True, exist_ok=True)

    W, H = 96, 72
    K = np.array([[60.0, 0.0, 48.0], [0.0, 60.0, 36.0], [0.0, 0.0, 1.0]], dtype=np.float64)

    # 4 corner cameras at height Z=2.4m looking inward toward room center (4.0, 4.0, 0.4)
    cam_specs = [
        ("cam_A", np.array([1.0, 1.0, 2.4]), np.array([4.0, 4.0, 0.3])),
        ("cam_B", np.array([7.0, 1.0, 2.4]), np.array([4.0, 4.0, 0.3])),
        ("cam_C", np.array([7.0, 7.0, 2.4]), np.array([4.0, 4.0, 0.3])),
        ("cam_D", np.array([1.0, 7.0, 2.4]), np.array([4.0, 4.0, 0.3])),
    ]

    # Create a textured synthetic floor pattern so SIFT features match across views
    rng = np.random.default_rng(42)
    tex_grid = rng.integers(40, 220, size=(256, 256, 3), dtype=np.uint8)
    tex_grid = cv2.GaussianBlur(tex_grid, (5, 5), 1.0)

    cal_cameras = []
    prep_views = []
    ma_views = []
    depth_views = []
    npz_data: dict[str, np.ndarray] = {}

    for idx, (cid, pos, target) in enumerate(cam_specs):
        fwd = target - pos
        fwd = fwd / np.linalg.norm(fwd)
        up_world = np.array([0.0, 0.0, 1.0])
        right = np.cross(fwd, up_world)
        right = right / np.linalg.norm(right)
        down = np.cross(fwd, right)
        R_c2w = np.column_stack([right, down, fwd])
        c2w_true = np.eye(4, dtype=np.float64)
        c2w_true[:3, :3] = R_c2w
        c2w_true[:3, 3] = pos

        # Render synthetic depth & RGB from true pose:
        # Floor at Z=0, plus a central table [3.0..5.0, 3.0..5.0] at Z=0.72m
        uu, vv = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
        xn = (uu - K[0, 2]) / K[0, 0]
        yn = (vv - K[1, 2]) / K[1, 1]
        dirs_c = np.stack([xn, yn, np.ones_like(xn)], axis=-1)
        dirs_w = dirs_c @ R_c2w.T

        ray_wz = np.minimum(dirs_w[:, :, 2], -1e-3)
        z_floor = (0.0 - pos[2]) / ray_wz
        pts_floor = pos[None, None, :] + dirs_w * z_floor[:, :, None]

        z_table = (0.72 - pos[2]) / ray_wz
        pts_table = pos[None, None, :] + dirs_w * z_table[:, :, None]
        on_table = (
            (pts_table[:, :, 0] >= 3.0)
            & (pts_table[:, :, 0] <= 5.0)
            & (pts_table[:, :, 1] >= 3.0)
            & (pts_table[:, :, 1] <= 5.0)
            & (z_table > 0.5)
        )
        depth_z = np.where(on_table, z_table, z_floor).astype(np.float32)
        pts_surf = np.where(on_table[:, :, None], pts_table, pts_floor)

        # Sample RGB from world XY texture so multi-view SIFT features correspond to exact 3D points
        tx = np.clip(((pts_surf[:, :, 0] / 8.0) * 255.0).astype(np.int32), 0, 255)
        ty = np.clip(((pts_surf[:, :, 1] / 8.0) * 255.0).astype(np.int32), 0, 255)
        img_bgr = tex_grid[ty, tx]

        img_path = root / "prepared" / f"{cid}.jpg"
        cv2.imwrite(str(img_path), img_bgr)
        (root / "images" / f"{cid}.image").write_bytes(img_path.read_bytes())

        # Calibration pose: optionally perturb cam_A/cam_B pitch/roll around X/Y axis so tabletop Z spreads > 6 cm
        c2w_cal = c2w_true.copy()
        if perturb_calib_z_m != 0.0 and idx in (0, 1):
            sign = 1.0 if idx == 0 else -1.0
            ang = sign * np.deg2rad(3.5)
            # Pitch rotation around World X-axis tilts horizontal surfaces in Z
            R_pert = np.array(
                [[1.0, 0.0, 0.0], [0.0, np.cos(ang), -np.sin(ang)], [0.0, np.sin(ang), np.cos(ang)]],
                dtype=np.float64,
            )
            c2w_cal[:3, :3] = R_pert @ c2w_cal[:3, :3]
            c2w_cal[2, 3] += sign * perturb_calib_z_m

        R_w2c_cal = c2w_cal[:3, :3].T
        t_w2c_cal = -R_w2c_cal @ c2w_cal[:3, 3]

        cal_cameras.append(
            {
                "camera_id": cid,
                "image_size": [W, H],
                "K": K.tolist(),
                "R": R_w2c_cal.tolist(),
                "t": t_w2c_cal.tolist(),
                "distortion": {"model": "NONE", "coeffs": []},
            }
        )
        prep_views.append(
            {
                "name": cid,
                "camera_id": cid,
                "image": f"{cid}.jpg",
                "mask": None,
                "image_size": [W, H],
                "K_undistorted": K.tolist(),
            }
        )
        ma_views.append(
            {
                "name": cid,
                "cam2world": c2w_true.tolist(),
                "intrinsics_model_res": K.tolist(),
                "model_input_hw": [H, W],
            }
        )
        depth_views.append(
            {
                "name": cid,
                "cam2world": c2w_true.tolist(),
                "intrinsics_model_res": K.tolist(),
                "model_input_hw": [H, W],
            }
        )
        # Provide unscaled raw depth (scale factor 0.5) to test M4 scale+offset+tilt recovery
        npz_data[f"{cid}__depth_z"] = (depth_z / 2.0).astype(np.float32)
        npz_data[f"{cid}__mask"] = np.ones((H, W), dtype=bool)
        npz_data[f"{cid}__conf"] = np.ones((H, W), dtype=np.float32)
        npz_data[f"{cid}__intrinsics"] = K.astype(np.float32)
        npz_data[f"{cid}__camera_pose"] = c2w_true.astype(np.float32)

    ma_raw = {k: (depth_z.astype(np.float32) if k.endswith("__depth_z") else v) for k, v in npz_data.items()}
    # Store per-camera true metric depth in mapanything/raw.npz and unscaled depth in depth/depth.npz
    for cid, _, _ in cam_specs:
        ma_raw[f"{cid}__depth_z"] = (npz_data[f"{cid}__depth_z"] * 2.0).astype(np.float32)

    from simai_ai.scene import run_hybrid_depth as hd

    write_json(root / "calibration.json", {"calibration_version": "cal-test-539", "cameras": cal_cameras})
    write_json(
        root / "prepared" / "prepared.json",
        {
            "format": "simai.mapanything_inputs",
            "format_version": "0.1.0",
            "views": prep_views,
            "input_provenance": {"frames": {"source": "SIM", "camera_ids": [c[0] for c in cam_specs]}, "is_upper_bound": True},
        },
    )
    write_json(root / "mapanything" / "cameras.json", {"convention": "cam2world 4x4, OpenCV", "views": ma_views})
    np.savez_compressed(root / "mapanything" / "raw.npz", **ma_raw)
    cams_sha = sha256((root / "mapanything" / "cameras.json").read_bytes())
    write_json(
        root / "world_alignment.json",
        {
            "format": "simai.world_alignment",
            "status": "OK",
            "cameras_json_sha256": cams_sha,
            "mapanything_to_world": np.eye(4).tolist(),
            "scale": 1.0,
            "max_center_residual_m": 0.0,
        },
    )
    prep_loaded = hd.load_prepared(root / "prepared")
    depth_doc_views = []
    for cid, _, _ in cam_specs:
        fname = f"{cid}.depth.npz"
        np.savez_compressed(
            root / "depth" / fname,
            depth=npz_data[f"{cid}__depth_z"],
            valid=npz_data[f"{cid}__mask"],
        )
        depth_doc_views.append(
            {
                "name": cid,
                "file": fname,
                "image_hw": [H, W],
                "K": K.tolist(),
                "native_hw": [H, W],
                "valid_ratio": 1.0,
            }
        )
    write_json(
        root / "depth" / "depth.json",
        {
            "format": "simai.hybrid_depth",
            "format_version": "0.1.0",
            "result": "SUCCESS",
            "adapter": {"adapter": "worldmirror2", "process_res": W, "pose_conditioned": True},
            "ma_run": {"dir": "mapanything", "cameras_json_sha256": cams_sha},
            "prepared_sha256": prep_loaded["_sha256"],
            "views": depth_doc_views,
        },
    )
    write_json(
        root / "job.json",
        {
            "run_id": "run-test-539",
            "input": {
                "images": [{"camera_id": c[0], "kind": "CLEAN_BACKGROUND"} for c in cam_specs],
                "input_provenance": {
                    "frames": {"source": "SIM", "camera_ids": [c[0] for c in cam_specs]},
                    "is_upper_bound": True,
                },
                "options": {"final_map_pipeline": True},
            },
        },
    )
    return prep_loaded


def test_pose_depth_fit_method2_passes_when_consistent_and_fails_on_tabletop_spread(tmp_path):
    """방식 2 게이트 1~4: 정합된 자세에서는 PASSED, 상판 높이 차(>3cm)가 있으면 FALLBACK 으로 전환됨을 검증한다."""
    # Case 1: Consistent calibration.json poses -> Method 2 PASSED
    root_ok = tmp_path / "ok"
    prep_ok = _make_four_cam_synthetic_workspace(root_ok, perturb_calib_z_m=0.0)
    cal_bytes_before = (root_ok / "calibration.json").read_bytes()

    res_ok = pdf.fit_and_evaluate_poses_and_depths(
        ma_run=root_ok / "mapanything",
        prepared=prep_ok,
        depth_dir=root_ok / "depth",
        world_alignment_path=root_ok / "world_alignment.json",
        calibration_path=root_ok / "calibration.json",
        mode="method2",
    )
    assert res_ok.status == "PASSED"
    assert res_ok.reconstruction_path == "METHOD2_CALIB_WM2"
    assert res_ok.pose_depth_fit_summary["passed"] is True
    assert res_ok.pose_depth_fit_summary["max_camera_abs_dz_median_m"] <= pdf.GATE2_MAX_ABS_DZ_M
    assert res_ok.pose_depth_fit_summary["coobserved_surface_3d_median_m"] <= pdf.GATE4_MAX_COOBS_DIFF_M
    assert set(res_ok.map_cameras.keys()) == {"cam_A", "cam_B", "cam_C", "cam_D"}
    assert all(v["pose_source"] == "CALIBRATION" for v in res_ok.map_cameras.values())
    assert (root_ok / "calibration.json").read_bytes() == cal_bytes_before

    # Case 2: Perturbed calibration.json pose (+9 cm on cam_A, -9 cm on cam_B -> ~9-18 cm tabletop spread)
    root_bad = tmp_path / "bad"
    prep_bad = _make_four_cam_synthetic_workspace(root_bad, perturb_calib_z_m=0.09)
    res_bad = pdf.fit_and_evaluate_poses_and_depths(
        ma_run=root_bad / "mapanything",
        prepared=prep_bad,
        depth_dir=root_bad / "depth",
        world_alignment_path=root_bad / "world_alignment.json",
        calibration_path=root_bad / "calibration.json",
        mode="method2",
    )
    assert res_bad.status == "FALLBACK"
    assert res_bad.pose_depth_fit_summary["passed"] is False
    assert res_bad.pose_depth_fit_summary["failure_reason"] is not None
    assert "GATE4" in res_bad.pose_depth_fit_summary["failure_reason"] or "GATE2" in res_bad.pose_depth_fit_summary["failure_reason"]

    # Fallback 1 on the same workspace recovers < 3 cm surface agreement
    res_fb1 = pdf.fit_and_evaluate_poses_and_depths(
        ma_run=root_bad / "mapanything",
        prepared=prep_bad,
        depth_dir=root_bad / "depth",
        world_alignment_path=root_bad / "world_alignment.json",
        calibration_path=root_bad / "calibration.json",
        mode="fallback1",
    )
    assert res_fb1.reconstruction_path == "FALLBACK1_MA_ALIGNED_WM2"
    assert res_fb1.pose_depth_fit_summary["coobserved_surface_3d_median_m"] <= pdf.GATE4_MAX_COOBS_DIFF_M
    assert all(v["pose_source"] == "MA_ALIGNED_SIM3" for v in res_fb1.map_cameras.values())


def test_mesh_clean_four_stages_removes_artifacts_and_keeps_valid_surface():
    """-528 mesh_clean 4단계 필터(긴 간선, 방/천장 경계, 스침각, 소형 부유 조각) 동작을 검증한다."""
    # Build a dense valid floor grid (20x20 cells -> 800 triangles, edge ~0.08m, area > 1.0 m^2)
    nx, ny = 21, 21
    xs, ys = np.meshgrid(np.linspace(1.0, 2.4, nx), np.linspace(1.0, 2.4, ny))
    V_floor = np.column_stack([xs.ravel(), ys.ravel(), np.zeros(nx * ny)])
    tris = []
    for j in range(ny - 1):
        for i in range(nx - 1):
            v00 = j * nx + i
            v10 = v00 + 1
            v11 = v00 + nx + 1
            v01 = v00 + nx
            tris.append([v00, v10, v11])
            tris.append([v00, v11, v01])
    F_floor = np.array(tris, dtype=np.int64)

    # Add artifact triangles:
    # 1) Long edge (> 0.18m)
    base_idx = len(V_floor)
    V_extra = np.array(
        [
            [1.0, 1.0, 0.1],
            [1.5, 1.0, 0.1],
            [1.0, 1.5, 0.1],  # long edge = 0.5m
            [1.2, 1.2, 3.1],
            [1.25, 1.2, 3.1],
            [1.2, 1.25, 3.1],  # ceiling > 2.65m
            [3.0, 3.0, 0.5],
            [3.04, 3.0, 0.5],
            [3.0, 3.04, 0.5],  # tiny isolated floating triangle (<200 faces)
        ],
        dtype=np.float64,
    )
    F_extra = np.array(
        [
            [base_idx, base_idx + 1, base_idx + 2],
            [base_idx + 3, base_idx + 4, base_idx + 5],
            [base_idx + 6, base_idx + 7, base_idx + 8],
        ],
        dtype=np.int64,
    )
    V_all = np.vstack([V_floor, V_extra]).astype(np.float32)
    F_all = np.vstack([F_floor, F_extra]).astype(np.int32)
    parsed = {
        "V_world": V_all,
        "F": F_all,
        "face_is_observed": np.ones(len(F_all), dtype=bool),
    }
    res = mclean.compute_cleanup_stage_masks(parsed, [], min_cluster_faces=200)
    masks = res["masks"]
    assert int(masks["a_long_edge"].sum()) < len(F_all)
    assert int(masks["b_room_ceiling_bounds"].sum()) < int(masks["a_long_edge"].sum())
    assert int(masks["d_small_components"].sum()) == len(F_floor)
    assert int(masks["d_small_components"][: len(F_floor)].sum()) == len(F_floor)
    assert not bool(masks["d_small_components"][-1])


def test_hole_fill_core_see_through_and_outlier_suppression():
    """hole_fill_core 의 SEE_THROUGH=5 보존 및 Oklab 외톨이 감쇠 동작을 검증한다."""
    H, W = 32, 32
    obs = np.full((H, W, 3), 180, dtype=np.uint8)
    filled = np.full((H, W, 3), 120, dtype=np.uint8)
    inpaint_mask = np.zeros((H, W), dtype=np.uint8)
    inpaint_mask[8:24, 8:24] = 255
    st_mask = np.zeros((H, W), dtype=bool)
    st_mask[24:30, 24:30] = True

    blended, src_map = hfc.feather_observed_filled_boundary(
        obs, filled, inpaint_mask, feather_m=0.02, texel_m=0.005, see_through_mask=st_mask
    )
    assert blended.shape == (H, W, 3)
    assert np.all(src_map[st_mask] == hfc.SRC_SEE_THROUGH)
    summary = hfc.compute_surface_source_summary(src_map, "hybrid_tone_lama", 0.005)
    assert summary["area_m2"]["see_through"] > 0.0

    # Test Oklab outlier removal across 3 cameras (2 agree on gray, 1 has dark shadow outlier)
    cand_mask = np.ones((3, 10), dtype=bool)
    cand_srgb = np.zeros((3, 10, 3), dtype=np.float64)
    cand_srgb[0] = [0.65, 0.65, 0.65]
    cand_srgb[1] = [0.64, 0.66, 0.65]
    cand_srgb[2] = [0.10, 0.10, 0.10]  # dark outlier
    cand_w = np.ones((3, 10), dtype=np.float64)
    nb_srgb = np.full((10, 3), 0.65, dtype=np.float64)
    out_srgb, has_obs, st = hfc.select_camera_colors_with_outlier_removal(
        cand_mask, cand_srgb, cand_w, nb_srgb, is_floor=True, sharp_exponent=6.0
    )
    assert np.all(has_obs)
    assert st["outliers_suppressed"] == 10
    assert np.all(out_srgb[:, 0] > 0.58)


def _build_synthetic_textured_tsdf_glb() -> bytes:
    """바닥(Z=0, 1..7m)과 중앙 테이블 상판(Z=0.72m, 3..5m) 격자로 구성된 합성 텍스처 GLB 바이트를 생성한다."""
    import struct
    from simai_ai.scene.final_map_bake import encode_png_any

    # Build a 20x20 grid (800 faces) on floor [1.0..7.0, 1.0..7.0, Z=0] + 10x10 grid (200 faces) on table [3.2..4.8, 3.2..4.8, Z=0.72]
    verts_w = []
    uvs = []
    faces = []

    def _add_patch(x0, x1, y0, y1, z_val, nx, ny):
        base = len(verts_w)
        xs, ys = np.meshgrid(np.linspace(x0, x1, nx), np.linspace(y0, y1, ny))
        us, vs = np.meshgrid(np.linspace(0.05, 0.95, nx), np.linspace(0.05, 0.95, ny))
        for x, y, u, v in zip(xs.ravel(), ys.ravel(), us.ravel(), vs.ravel()):
            verts_w.append([x, y, z_val])
            uvs.append([u, v])
        for j in range(ny - 1):
            for i in range(nx - 1):
                v00 = base + j * nx + i
                v10 = v00 + 1
                v11 = v00 + nx + 1
                v01 = v00 + nx
                faces.append([v00, v10, v11])
                faces.append([v00, v11, v01])

    _add_patch(2.5, 4.5, 2.5, 4.5, 0.0, 21, 21)
    _add_patch(3.5, 4.5, 3.5, 4.5, 0.72, 16, 16)

    V_w = np.asarray(verts_w, dtype=np.float32)
    # Convert World (x, y, z) -> glTF Y-up (x, z, -y)
    V_glb = np.column_stack([V_w[:, 0], V_w[:, 2], -V_w[:, 1]]).astype(np.float32)
    UV = np.asarray(uvs, dtype=np.float32)
    F = np.asarray(faces, dtype=np.uint32)

    tex_rgb = np.full((32, 32, 3), 175, dtype=np.uint8)
    png_bytes = encode_png_any(tex_rgb)

    pos_b = V_glb.tobytes()
    uv_b = UV.tobytes()
    idx_b = F.tobytes()

    parts = [pos_b, uv_b, idx_b, png_bytes]
    views = []
    offset = 0
    bin_chunks = bytearray()
    for p in parts:
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(p)})
        bin_chunks.extend(p)
        offset += len(p)
        while offset % 4:
            bin_chunks.append(0)
            offset += 1

    gltf_doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "images": [{"bufferView": 3, "mimeType": "image/png"}],
        "textures": [{"source": 0}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {"POSITION": 0, "TEXCOORD_0": 1},
                        "indices": 2,
                        "material": 0,
                    }
                ]
            }
        ],
        "buffers": [{"byteLength": len(bin_chunks)}],
        "bufferViews": views,
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,
                "count": len(V_glb),
                "type": "VEC3",
                "min": V_glb.min(axis=0).tolist(),
                "max": V_glb.max(axis=0).tolist(),
            },
            {"bufferView": 1, "componentType": 5126, "count": len(UV), "type": "VEC2"},
            {"bufferView": 2, "componentType": 5125, "count": int(F.size), "type": "SCALAR"},
        ],
        "extras": {"simai": {"surface_source": {"kept_mesh_faces": int(len(F))}}},
    }
    json_bytes = json.dumps(gltf_doc, separators=(",", ":")).encode("utf-8")
    while len(json_bytes) % 4:
        json_bytes += b" "
    total_len = 12 + 8 + len(json_bytes) + 8 + len(bin_chunks)
    return (
        struct.pack("<4sII", b"glTF", 2, total_len)
        + struct.pack("<I4s", len(json_bytes), b"JSON")
        + json_bytes
        + struct.pack("<I4s", len(bin_chunks), b"BIN\x00")
        + bytes(bin_chunks)
    )


def test_reconstruct_tasks_final_map_pipeline_and_measure_single_pose(tmp_path, monkeypatch):
    """reconstruct_tasks.bake 가 합성 깊이 -> depth_fitted -> texture_mesh 인자/해시 검증 -> mesh_clean -> 실제 final_map_bake -> scene_final_wm2.glb 까지 끝까지 돌고 단일 자세와 scene_output 2.3.0 스키마를 준수함을 검증한다."""
    import sys
    import types
    from simai_ai.scene import final_map_bake as fmb
    from simai_ai.scene import texture_mesh as tm

    _make_four_cam_synthetic_workspace(tmp_path, perturb_calib_z_m=0.09)
    prep_sha = read_json(tmp_path / "depth" / "depth.json")["prepared_sha256"]
    write_json(
        tmp_path / "mapanything" / "metrics.json",
        {"result": "SUCCESS", "prepared_sha256": prep_sha, "model_version": "wm2-test"},
    )

    monkeypatch.setenv("SIMAI_TEXRECON", "fake-texrecon")
    monkeypatch.setenv("SIMAI_LAMA_WEIGHTS", str(tmp_path / "no_weights.pt"))
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/texrecon")
    monkeypatch.setitem(sys.modules, "open3d", types.ModuleType("open3d"))

    # Use coarse texel resolution in unit test so real final_map_bake finishes in < 0.5s on CPU
    monkeypatch.setattr(fmb, "TEXEL_M", 0.25)
    monkeypatch.setattr(fmb, "WALL_TEXEL_R3_M", 0.25)
    monkeypatch.setattr(fmb, "GRID_CELL_M", 0.50)

    raw_tsdf_glb = _build_synthetic_textured_tsdf_glb()
    tm_calls: list[list[str]] = []

    def fake_tm_main(argv: list[str]) -> int:
        from simai_ai.scene import run_hybrid_depth as hd

        tm_calls.append(list(argv))
        # Parse real CLI args (catches --voxel-m vs --voxel regressions) and check metrics.json match!
        parsed = tm.build_parser().parse_args(argv)
        prep_doc = hd.load_prepared(parsed.prepared)
        hd.check_run_matches_prepared(parsed.ma_run, prep_doc)
        _, cams_sha = hd.read_cameras(parsed.ma_run)
        if parsed.world_alignment is not None:
            hd.load_world_alignment(parsed.world_alignment, cams_sha)
        parsed.out.mkdir(parents=True, exist_ok=True)
        (parsed.out / "textured_area.glb").write_bytes(raw_tsdf_glb)
        write_json(
            parsed.out / "metrics.json",
            {
                "bakes": {
                    "area": {
                        "glb": "textured_area.glb",
                        "glb_triangles": 1000,
                        "ratios_area": {"OBSERVED": 0.85, "ESTIMATED_PLANE": 0.15},
                    }
                },
                "geometry": {"faces": 1000, "decimated": True},
            },
        )
        return 0

    monkeypatch.setattr(tm, "main", fake_tm_main)
    cal_before = (tmp_path / "calibration.json").read_bytes()

    tasks.bake(tmp_path)
    assert len(tm_calls) == 1
    assert "--voxel" in tm_calls[0]
    assert "--voxel-m" not in tm_calls[0]
    assert (tmp_path / "depth_fitted" / "metrics.json").is_file()
    assert (tmp_path / "textured" / "input_cleaned.glb").is_file()
    assert (tmp_path / "textured" / "scene_final_wm2.glb").is_file()

    bake_doc = read_json(tmp_path / "bake.json")
    assert bake_doc["reconstructor"] == "WORLDMIRROR2_HYBRID"
    assert bake_doc["reconstruction_path"] == "FALLBACK1_MA_ALIGNED_WM2"
    assert bake_doc["fallback_reason"] is None
    assert bake_doc["pose_depth_fit"]["passed"] is False
    assert set(bake_doc["map_cameras"].keys()) == {"cam_A", "cam_B", "cam_C", "cam_D"}
    assert all(v["pose_source"] == "MA_ALIGNED_SIM3" for v in bake_doc["map_cameras"].values())
    assert "MESH_CLEAN_V2" in bake_doc["postprocess"]
    assert "FURNITURE_PLANES" in bake_doc["postprocess"]

    # Measure stage uses map_cameras from bake.json and keeps calibration.json immutable
    tasks.measure(tmp_path)
    sc_doc = read_json(tmp_path / "scale_check.json")
    assert sc_doc["calibration_changed"] is False
    assert (tmp_path / "calibration.json").read_bytes() == cal_before

    # Verify StaticReconstructor produces schema-valid scene_output 2.3.0
    import copy
    import threading
    from simai_ai.providers.testing import load_example
    from simai_ai.scene.reconstruct import StaticReconstructor

    cal_full = copy.deepcopy(load_example("calibration.json"))
    base_cam = copy.deepcopy(cal_full["cameras"][0])
    cal_full["cameras"] = []
    for cid in ("cam_A", "cam_B", "cam_C", "cam_D"):
        c_copy = copy.deepcopy(base_cam)
        c_copy["camera_id"] = cid
        cal_full["cameras"].append(c_copy)
    cal_bytes = json.dumps(cal_full).encode("utf-8")
    inp_doc = {
        "calibration_version": cal_full["calibration_version"],
        "calibration_ref": "s3://simai/cal.json",
        "calibration_hash": sha256(cal_bytes),
        "input_provenance": {
            "frames": {"source": "SIM", "camera_ids": ["cam_A", "cam_B", "cam_C", "cam_D"]},
            "calibration": {"source": cal_full["provider_source"], "calibration_version": cal_full["calibration_version"]},
            "is_upper_bound": True,
        },
        "images": [
            {"camera_id": cid, "uri": f"s3://simai/{cid}.jpg", "kind": "CLEAN_BACKGROUND"}
            for cid in ("cam_A", "cam_B", "cam_C", "cam_D")
        ],
        "options": {"final_map_pipeline": True},
    }

    class NoopStages:
        def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
            pass

    sr = StaticReconstructor(NoopStages(), clock_ms=lambda: 1_800_000_000_000)
    for p in (tmp_path / "images").glob("*"):
        p.unlink()
    (tmp_path / "images").rmdir()

    scene_out = sr.run(
        inp_doc,
        tmp_path,
        run_id="run-test-539",
        cancelled=threading.Event(),
        progress=lambda _: None,
        get=lambda uri: cal_bytes if uri.endswith("cal.json") else b"fake-img",
        put=lambda name, raw, ct: f"s3://simai/out/{name}",
    )
    assert scene_out["schema_version"] == "2.3.0"
    assert scene_out["method"]["reconstructor"] == "WORLDMIRROR2_HYBRID"
    assert scene_out["method"]["reconstruction_path"] == "FALLBACK1_MA_ALIGNED_WM2"
    assert scene_out["method"]["fallback_reason"] is None
    assert scene_out["method"]["pose_depth_fit"]["passed"] is False
    assert set(scene_out["method"]["map_cameras"].keys()) == {"cam_A", "cam_B", "cam_C", "cam_D"}
    assert schema_errors(scene_out, "scene_output") == []


def test_final_map_pipeline_method2_and_fallback2_records_reason(tmp_path, monkeypatch, caplog):
    """방식 2 게이트 통과 시 METHOD2_CALIB_WM2 선택, 새 파이프라인 실패 시 조용히 숨기지 않고 FALLBACK2_LEGACY_TEXRECON 과 실패 원인을 로그·bake.json·scene_output 에 남김을 검증한다."""
    import copy
    import logging
    import sys
    import threading
    import types
    from simai_ai.providers.testing import load_example
    from simai_ai.scene import final_map_bake as fmb
    from simai_ai.scene import texture_mesh as tm
    from simai_ai.scene.reconstruct import StaticReconstructor

    # 1) Method 2 gate passed when reconstruction_path="auto" and calibration poses are consistent
    m2_root = tmp_path / "m2"
    _make_four_cam_synthetic_workspace(m2_root, perturb_calib_z_m=0.0)
    job_m2 = read_json(m2_root / "job.json")
    job_m2["input"]["options"]["reconstruction_path"] = "auto"
    write_json(m2_root / "job.json", job_m2)
    prep_sha = read_json(m2_root / "depth" / "depth.json")["prepared_sha256"]
    write_json(
        m2_root / "mapanything" / "metrics.json",
        {"result": "SUCCESS", "prepared_sha256": prep_sha, "model_version": "wm2-test"},
    )
    raw_tsdf_glb = _build_synthetic_textured_tsdf_glb()
    (m2_root / "textured").mkdir(parents=True, exist_ok=True)
    (m2_root / "textured" / "input_cleaned.glb").write_bytes(raw_tsdf_glb)
    monkeypatch.setenv("SIMAI_LAMA_WEIGHTS", str(tmp_path / "no_weights.pt"))
    monkeypatch.setattr(fmb, "TEXEL_M", 0.25)
    monkeypatch.setattr(fmb, "WALL_TEXEL_R3_M", 0.25)
    monkeypatch.setattr(fmb, "GRID_CELL_M", 0.50)

    tasks.bake(m2_root)
    bake_m2 = read_json(m2_root / "bake.json")
    assert bake_m2["reconstruction_path"] == "METHOD2_CALIB_WM2"
    assert bake_m2["uses_known_pose"] is True
    assert bake_m2["fallback_reason"] is None

    # 2) Fallback 2 when final_map_pipeline fails (e.g. texture_mesh fails on fitted depth, then legacy texrecon succeeds)
    fb2_root = tmp_path / "fb2"
    _make_four_cam_synthetic_workspace(fb2_root, perturb_calib_z_m=0.0)
    prep_sha_fb2 = read_json(fb2_root / "depth" / "depth.json")["prepared_sha256"]
    write_json(
        fb2_root / "mapanything" / "metrics.json",
        {"result": "SUCCESS", "prepared_sha256": prep_sha_fb2, "model_version": "ma-test"},
    )
    monkeypatch.setenv("SIMAI_TEXRECON", "fake-texrecon")
    monkeypatch.setattr("shutil.which", lambda x: "/usr/bin/texrecon")
    monkeypatch.setitem(sys.modules, "open3d", types.ModuleType("open3d"))

    call_idx = {"n": 0}

    def fake_tm_fail_then_succeed(argv: list[str]) -> int:
        from simai_ai.scene import run_hybrid_depth as hd

        call_idx["n"] += 1
        parsed = tm.build_parser().parse_args(argv)
        prep_doc = hd.load_prepared(parsed.prepared)
        hd.check_run_matches_prepared(parsed.ma_run, prep_doc)
        if call_idx["n"] == 1:
            # Simulate failure in new pipeline's TSDF/texrecon step
            return 2
        # Legacy Fallback 2 call on root / "mapanything"
        parsed.out.mkdir(parents=True, exist_ok=True)
        (parsed.out / "scene.glb").write_bytes(raw_tsdf_glb)
        src_png = np.full((8, 8), 1, dtype=np.uint8)
        cv2.imwrite(str(parsed.out / "surface_source_area.png"), src_png)
        write_json(
            parsed.out / "metrics.json",
            {
                "bakes": {
                    "area": {
                        "glb": "scene.glb",
                        "glb_triangles": 1000,
                        "ratios_area": {"OBSERVED": 0.9, "UNOBSERVED": 0.1},
                    }
                },
                "geometry": {"faces": 1000, "decimated": True},
            },
        )
        return 0

    monkeypatch.setattr(tm, "main", fake_tm_fail_then_succeed)
    monkeypatch.setattr(tasks, "enhance_visual_glb", lambda root, glb, opts: {"applied": False})

    with caplog.at_level(logging.WARNING):
        tasks.bake(fb2_root)

    assert any("FALLBACK2_LEGACY_TEXRECON" in r.message for r in caplog.records)
    bake_fb2 = read_json(fb2_root / "bake.json")
    assert bake_fb2["texture"] == "texrecon"
    assert bake_fb2["reconstruction_path"] == "FALLBACK2_LEGACY_TEXRECON"
    assert bake_fb2["fallback_reason"] == "FINAL_MAP_TEXTURE_MESH_FAILED:exit_2"

    tasks.measure(fb2_root)
    cal_full = copy.deepcopy(load_example("calibration.json"))
    base_cam = copy.deepcopy(cal_full["cameras"][0])
    cal_full["cameras"] = []
    for cid in ("cam_A", "cam_B", "cam_C", "cam_D"):
        c_copy = copy.deepcopy(base_cam)
        c_copy["camera_id"] = cid
        cal_full["cameras"].append(c_copy)
    cal_bytes = json.dumps(cal_full).encode("utf-8")
    inp_doc = {
        "calibration_version": cal_full["calibration_version"],
        "calibration_ref": "s3://simai/cal.json",
        "calibration_hash": sha256(cal_bytes),
        "input_provenance": {
            "frames": {"source": "SIM", "camera_ids": ["cam_A", "cam_B", "cam_C", "cam_D"]},
            "calibration": {"source": cal_full["provider_source"], "calibration_version": cal_full["calibration_version"]},
            "is_upper_bound": True,
        },
        "images": [
            {"camera_id": cid, "uri": f"s3://simai/{cid}.jpg", "kind": "CLEAN_BACKGROUND"}
            for cid in ("cam_A", "cam_B", "cam_C", "cam_D")
        ],
        "options": {"final_map_pipeline": True},
    }

    ran_stages: list[str] = []

    class RecordingNoopStages:
        def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
            ran_stages.append(stage)

    for p in (fb2_root / "images").glob("*"):
        p.unlink()
    (fb2_root / "images").rmdir()

    sr = StaticReconstructor(RecordingNoopStages(), clock_ms=lambda: 1_800_000_000_000)
    scene_fb2 = sr.run(
        inp_doc,
        fb2_root,
        run_id="run-test-539",
        cancelled=threading.Event(),
        progress=lambda _: None,
        get=lambda uri: cal_bytes if uri.endswith("cal.json") else b"fake-img",
        put=lambda name, raw, ct: f"s3://simai/out/{name}",
    )
    assert "fallback" not in ran_stages
    assert scene_fb2["schema_version"] == "2.3.0"
    assert scene_fb2["method"]["reconstruction_path"] == "FALLBACK2_LEGACY_TEXRECON"
    assert scene_fb2["method"]["fallback_reason"] == "FINAL_MAP_TEXTURE_MESH_FAILED:exit_2"
    assert "fallback_reason=FINAL_MAP_TEXTURE_MESH_FAILED:exit_2" in scene_fb2["quality"]["notes"]
    assert schema_errors(scene_fb2, "scene_output") == []


def test_run_predict_with_calibration_pose_conditioning(tmp_path):
    """S15P31A101-539: run_hybrid_depth.run_predict 에 calibration 경로를 전달하면 calibration.json 자세를 조건으로 쓰고 pose_source=CALIBRATION 을 기록한다."""
    from simai_ai.scene import run_hybrid_depth as hd

    prep = _make_four_cam_synthetic_workspace(tmp_path, perturb_calib_z_m=0.05)
    # Create raw.npz in mapanything so FakeMapAnythingAdapter can run
    raw_npz = {}
    for v in prep["views"]:
        cid = v["name"]
        w, h = v["image_size"]
        d = np.load(tmp_path / "depth" / f"{cid}.depth.npz")["depth"]
        raw_npz[f"{cid}__depth_z"] = d
        raw_npz[f"{cid}__mask"] = np.isfinite(d) & (d > 0)
        raw_npz[f"{cid}__conf"] = np.ones((h, w), dtype=np.float32)
        raw_npz[f"{cid}__intrinsics"] = np.asarray(v["K_undistorted"], dtype=np.float32)
    np.savez_compressed(tmp_path / "mapanything" / "raw.npz", **raw_npz)

    views_ma = hd.predict_views(tmp_path / "mapanything", prep)
    views_cal = hd.predict_views(tmp_path / "mapanything", prep, calibration=tmp_path / "calibration.json")
    # Perturbed cam_A has +0.05 m Z difference in calibration.json vs mapanything/cameras.json
    assert not np.allclose(views_ma[0].cam2world[:3, 3], views_cal[0].cam2world[:3, 3])

    out_cal = tmp_path / "depth_calib"
    doc = hd.run_predict(
        hd.FakeMapAnythingAdapter(tmp_path / "mapanything"),
        tmp_path / "mapanything",
        prep,
        out_cal,
        calibration=tmp_path / "calibration.json",
    )
    assert doc["pose_source"] == "CALIBRATION"
    assert doc["calibration_json_sha256"].startswith("sha256:")


def test_mvs_io_image_ext_and_pod_queue_script_contracts(tmp_path):
    """Pod 세션 실패 지점 회귀 방지: mvs_io 의 images/*.image 로드, pod_final_map_539.sh 하드코딩 제거, DA3 기본 비활성화, pod_decide.sh --bundle/--out-dir 인자 전달을 검증한다."""
    import shutil
    import subprocess
    import sys

    ai_root = Path(__file__).resolve().parents[2]
    if str(ai_root) not in sys.path:
        sys.path.insert(0, str(ai_root))
    from experiments.geometry_bench import mvs_io as mio

    scene_dir = tmp_path / "scene_in"
    _make_four_cam_synthetic_workspace(scene_dir, perturb_calib_z_m=0.09)

    # 1) Step 3 failure regression: only images/{cid}.image exists in images/ (no .png/.jpg in root or images/)
    cams = mio.load_and_undistort_cameras(scene_dir / "calibration.json", scene_dir / "images")
    assert set(cams.keys()) == {"cam_A", "cam_B", "cam_C", "cam_D"}
    cams_from_root = mio.load_and_undistort_cameras(scene_dir / "calibration.json", scene_dir)
    assert set(cams_from_root.keys()) == {"cam_A", "cam_B", "cam_C", "cam_D"}
    cal_payload = mio.resolve_calibration_payload(scene_dir)
    assert cal_payload["calibration_version"] == "cal-test-539"

    # 2) Static inspection of pod_final_map_539.sh & pod_depth_adapters.sh
    repo_root = Path(__file__).resolve().parents[3]
    queue_sh = repo_root / "deploy" / "scripts" / "pod_final_map_539.sh"
    queue_text = queue_sh.read_text(encoding="utf-8")
    assert "C:/Users/" not in queue_text
    assert "/workspace/out/ma_single" not in queue_text
    assert 'ENABLE_DA3="${SIMAI_ENABLE_DA3:-0}"' in queue_text
    assert '--bundle "$DECIDE_BUNDLE_PY" --out-dir "$STEP4_DIR/results"' in queue_text
    assert 'bash "$DECIDE_SCRIPT" all' not in queue_text

    adapters_sh = repo_root / "ai" / "experiments" / "static_scene" / "pod_depth_adapters.sh"
    adapters_text = adapters_sh.read_text(encoding="utf-8")
    assert "setup_da3()" in adapters_text

    # 3) Run pod_final_map_539.sh --dry-run on the synthetic single --scene-input folder
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash_bin = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if bash_bin:
        out_queue = tmp_path / "queue_out"
        res = subprocess.run(
            [
                bash_bin,
                str(queue_sh),
                "--dry-run",
                "--python",
                sys.executable,
                "--scene-input",
                str(scene_dir),
                "--out",
                str(out_queue),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert res.returncode == 0, f"pod_final_map_539.sh failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        assert (out_queue / "step1_method2_calib_pose" / "gate_report.json").is_file()
        assert (out_queue / "step2_bake_comparison" / "bake.json").is_file()
        assert (out_queue / "step2_bake_comparison" / "scene_final_wm2.glb").is_file()
        da3_doc = read_json(out_queue / "step3_da3_2cm" / "da3_2cm_summary.json")
        assert da3_doc["enable_da3"] is False
        assert da3_doc["status"] == "SKIPPED_DEFAULT_OFF"
        assert da3_doc["mvs_io_cameras_verified"] == ["cam_A", "cam_B", "cam_C", "cam_D"]
        dec_doc = read_json(out_queue / "step4_decide_538" / "decide_queue_status.json")
        assert "--bundle" in dec_doc["cli_invocation"]
        assert "--out-dir" in dec_doc["cli_invocation"]
        assert (out_queue / "final_map_539_pod_bundle.tar.gz").is_file()

