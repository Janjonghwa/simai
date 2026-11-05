"""장면 GLB 3D 물체 복원(S15P31A101-515) 단위 시험."""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import object_completion as oc
from simai_ai.scene import texture_provenance as tp


def _box_open_back(
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    z0: float,
    z1: float,
    grid: int = 5,
) -> tuple[np.ndarray, np.ndarray]:
    """윗면 + 앞/좌/우 옆면만 관측되고 뒷면(y=y1)과 바닥면이 빈 부분 관측 상자 메시를 만든다."""
    verts: list[np.ndarray] = []
    faces: list[np.ndarray] = []

    def add_patch(p00, p10, p11, p01, n: int = grid) -> None:
        base = len(verts)
        us = np.linspace(0.0, 1.0, n)
        vs = np.linspace(0.0, 1.0, n)
        for u in us:
            for v in vs:
                p = (
                    (1 - u) * (1 - v) * p00
                    + u * (1 - v) * p10
                    + u * v * p11
                    + (1 - u) * v * p01
                )
                verts.append(p)
        for i in range(n - 1):
            for j in range(n - 1):
                a = base + i * n + j
                b = base + (i + 1) * n + j
                c = base + (i + 1) * n + (j + 1)
                d = base + i * n + (j + 1)
                faces.append(np.array([[a, b, c], [a, c, d]], dtype=np.int64))

    add_patch(
        np.array([x0, y0, z1]),
        np.array([x1, y0, z1]),
        np.array([x1, y1, z1]),
        np.array([x0, y1, z1]),
    )
    add_patch(
        np.array([x0, y0, z0]),
        np.array([x1, y0, z0]),
        np.array([x1, y0, z1]),
        np.array([x0, y0, z1]),
    )
    add_patch(
        np.array([x0, y1, z0]),
        np.array([x0, y0, z0]),
        np.array([x0, y0, z1]),
        np.array([x0, y1, z1]),
    )
    add_patch(
        np.array([x1, y0, z0]),
        np.array([x1, y1, z0]),
        np.array([x1, y1, z1]),
        np.array([x1, y0, z1]),
    )
    return np.asarray(verts, dtype=np.float64), np.vstack(faces)


def _partial_cone(
    cx: float,
    cy: float,
    height: float = 0.58,
    r_bot: float = 0.14,
    r_top: float = 0.025,
) -> tuple[np.ndarray, np.ndarray]:
    """전면 절반만 관측된 라바콘 부분 메시를 만든다."""
    verts: list[list[float]] = []
    faces: list[list[int]] = []
    n_ang = 12
    n_z = 8
    angles = np.linspace(-0.85 * np.pi, 0.15 * np.pi, n_ang)
    zs = np.linspace(0.03, height, n_z)
    for z in zs:
        t = (z - 0.03) / max(height - 0.03, 1e-6)
        r = r_bot * (1.0 - t) + r_top * t
        for a in angles:
            verts.append([cx + r * np.cos(a), cy + r * np.sin(a), float(z)])
    for iz in range(n_z - 1):
        for ia in range(n_ang - 1):
            a = iz * n_ang + ia
            b = a + 1
            c = (iz + 1) * n_ang + (ia + 1)
            d = (iz + 1) * n_ang + ia
            faces.append([a, b, c])
            faces.append([a, c, d])
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def _partial_signboard(
    cx: float,
    cy: float,
    width: float = 0.48,
    z0: float = 0.38,
    z1: float = 1.05,
) -> tuple[np.ndarray, np.ndarray]:
    """얇은 평판형 안내판 부분 메시를 만든다 (`z_min >= 0.30` 조건 충족)."""
    verts: list[list[float]] = []
    faces: list[list[int]] = []
    nx, nz = 7, 9
    xs = np.linspace(cx - width * 0.5, cx + width * 0.5, nx)
    zs = np.linspace(z0, z1, nz)
    for x in xs:
        for z in zs:
            verts.append([float(x), float(cy + 0.008 * np.sin(x * 5.0)), float(z)])
    for i in range(nx - 1):
        for j in range(nz - 1):
            a = i * nz + j
            b = (i + 1) * nz + j
            c = (i + 1) * nz + (j + 1)
            d = i * nz + (j + 1)
            faces.append([a, b, c])
            faces.append([a, c, d])
    return np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64)


def _synthetic_scene_glb() -> bytes:
    """바닥(4x4 m) + 수직 벽(x=0) + 일반 상자 의자 + 주황 라바콘 + 평판 안내판으로 이루어진 합성 GLB."""
    # 1. Floor (0..4, 0..4, z=0.0)
    nf = 25
    xs = np.linspace(0.0, 4.0, nf)
    f_pts: list[tuple[float, float, float]] = []
    f_idx: list[int] = []
    for i in range(nf):
        for j in range(nf):
            f_pts.append((float(xs[i]), float(xs[j]), 0.0))
    for i in range(nf - 1):
        for j in range(nf - 1):
            a = i * nf + j
            b = (i + 1) * nf + j
            c = (i + 1) * nf + (j + 1)
            d = i * nf + (j + 1)
            f_idx += [a, b, c, a, c, d]
    V_floor = np.asarray(f_pts, dtype=np.float64)
    F_floor = np.asarray(f_idx, dtype=np.int64).reshape(-1, 3)

    # 2. Wall at x=0.0, y=0..4, z=0..2.2 (법선이 +X를 향하도록 정점 순서 지정)
    nw = 15
    ys = np.linspace(0.0, 4.0, nw)
    zs = np.linspace(0.0, 2.2, nw)
    w_pts: list[tuple[float, float, float]] = []
    w_idx: list[int] = []
    for i in range(nw):
        for j in range(nw):
            w_pts.append((0.0, float(ys[i]), float(zs[j])))
    for i in range(nw - 1):
        for j in range(nw - 1):
            a = i * nw + j
            b = (i + 1) * nw + j
            c = (i + 1) * nw + (j + 1)
            d = i * nw + (j + 1)
            w_idx += [a, b, c, a, c, d]
    V_wall = np.asarray(w_pts, dtype=np.float64)
    F_wall = np.asarray(w_idx, dtype=np.int64).reshape(-1, 3)

    # 3. Objects
    V_box, F_box = _box_open_back(1.3, 1.7, 1.3, 1.7, 0.03, 0.45)
    V_cone, F_cone = _partial_cone(2.8, 1.5, height=0.58)
    V_sign, F_sign = _partial_signboard(1.6, 3.1, width=0.48, z0=0.38, z1=1.05)

    parts = [
        (V_floor, F_floor, (130, 125, 115)),
        (V_wall, F_wall, (180, 180, 175)),
        (V_box, F_box, (45, 95, 195)),
        (V_cone, F_cone, (238, 82, 32)),
        (V_sign, F_sign, (215, 210, 195)),
    ]

    blob = bytearray()
    views: list[dict] = []
    accs: list[dict] = []

    def add_view(data: bytes, target: int | None = None) -> int:
        while len(blob) % 4:
            blob.append(0)
        v = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target is not None:
            v["target"] = target
        blob.extend(data)
        views.append(v)
        return len(views) - 1

    def add_acc(view: int, comp: int, count: int, typ: str, **kw) -> int:
        accs.append(
            {"bufferView": view, "componentType": comp, "count": count, "type": typ, **kw}
        )
        return len(accs) - 1

    V_all_list: list[np.ndarray] = []
    F_all_list: list[np.ndarray] = []
    UV_all_list: list[np.ndarray] = []
    atlas = np.zeros((64, 64 * len(parts), 3), dtype=np.uint8)
    v_offset = 0
    for pi, (Vp, Fp, rgb) in enumerate(parts):
        atlas[:, pi * 64 : (pi + 1) * 64] = rgb[::-1]
        V_all_list.append(Vp)
        F_all_list.append(Fp + v_offset)
        u_mid = (pi + 0.5) / len(parts)
        uv = np.column_stack(
            [
                np.full(len(Vp), u_mid, dtype=np.float32),
                np.full(len(Vp), 0.5, dtype=np.float32),
            ]
        )
        UV_all_list.append(uv)
        v_offset += len(Vp)

    V_all = np.vstack(V_all_list)
    F_all = np.vstack(F_all_list).astype("<u4")
    UV_all = np.vstack(UV_all_list).astype("<f4")
    V_glb = np.stack([V_all[:, 0], V_all[:, 2], -V_all[:, 1]], axis=1).astype("<f4")

    ok, png_buf = cv2.imencode(".png", atlas)
    assert ok

    pos_acc = add_acc(
        add_view(V_glb.tobytes(), 34962),
        5126,
        len(V_glb),
        "VEC3",
        min=V_glb.min(axis=0).tolist(),
        max=V_glb.max(axis=0).tolist(),
    )
    uv_acc = add_acc(add_view(UV_all.tobytes(), 34962), 5126, len(UV_all), "VEC2")
    idx_acc = add_acc(add_view(F_all.reshape(-1).tobytes(), 34963), 5125, int(F_all.size), "SCALAR")
    img_view = add_view(png_buf.tobytes())

    doc = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"name": "textured_0000", "mesh": 0}],
        "meshes": [
            {
                "name": "textured_0000",
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_acc, "TEXCOORD_0": uv_acc},
                        "indices": idx_acc,
                        "material": 0,
                    }
                ],
            }
        ],
        "materials": [
            {
                "name": "textured_0000",
                "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}},
            }
        ],
        "textures": [{"source": 0}],
        "images": [{"bufferView": img_view, "mimeType": "image/png"}],
        "bufferViews": views,
        "accessors": accs,
        "buffers": [{"byteLength": len(blob)}],
        "extras": {"simai": {"axis_convention": "GLTF_Y_UP"}},
    }
    return ge.serialize_glb(ge.GlbFile(doc, blob))


def _look_at_camera(
    cam_id: str,
    eye: tuple[float, float, float],
    target: tuple[float, float, float],
    w: int = 320,
    h: int = 240,
) -> ge.CheckCamera:
    """World `eye`에서 `target`을 바라보는 OpenCV 카메라를 만든다."""
    eye_v = np.asarray(eye, dtype=np.float64)
    tgt_v = np.asarray(target, dtype=np.float64)
    forward = tgt_v - eye_v
    forward /= np.linalg.norm(forward)
    up_world = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    right = np.cross(forward, up_world)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    R = np.vstack([right, down, forward])
    t = -R @ eye_v
    K = np.array([[220.0, 0.0, w * 0.5], [0.0, 220.0, h * 0.5], [0.0, 0.0, 1.0]], dtype=np.float64)
    img = np.full((h, w, 3), (130, 125, 115), dtype=np.uint8)
    return ge.CheckCamera(cam_id, K, R, t, img)


def _four_cameras_for_scene(raw_glb: bytes) -> list[ge.CheckCamera]:
    """4대 카메라를 배치하고 장면 물체를 투영해 실제 사진 색을 합성한다."""
    cams = [
        _look_at_camera("cam_A", (2.0, -0.4, 1.7), (2.0, 2.0, 0.35)),
        _look_at_camera("cam_B", (4.3, 2.0, 1.7), (2.0, 2.0, 0.35)),
        _look_at_camera("cam_C", (2.0, 4.4, 1.7), (2.0, 2.0, 0.35)),
        _look_at_camera("cam_D", (0.4, 0.4, 1.8), (2.2, 2.2, 0.35)),
    ]
    g = ge.parse_glb(raw_glb)
    model = oc.load_scene_for_objects(g)
    merged = oc.build_merged_scene_mesh(g, model)
    for cam in cams:
        h, w = cam.image.shape[:2]
        tri_cam = merged.V[merged.F] @ cam.R.T + cam.t
        front = tri_cam[:, :, 2].min(axis=1) >= 0.1
        front_ids = np.nonzero(front)[0]
        if not len(front_ids):
            continue
        zc = tri_cam[front_ids, :, 2]
        u = cam.K[0, 0] * tri_cam[front_ids, :, 0] / zc + cam.K[0, 2] + 0.5
        v = cam.K[1, 1] * tri_cam[front_ids, :, 1] / zc + cam.K[1, 2] + 0.5
        tid, _, _ = ge.rasterize(np.stack([u, v], axis=2), zc, (h, w), perspective=True)
        valid = tid >= 0
        if valid.any():
            fids = front_ids[tid[valid]]
            cam.image[valid] = np.clip(np.rint(merged.face_rgb_srgb[fids]), 0, 255).astype(np.uint8)
    return cams


def test_candidate_separation_and_camera_masks():
    """1. 바닥·벽 평면 제거 후 물체 후보(일반 물체·라바콘·안내판) 분리 및 4카메라 2D 마스크/최적 카메라 선택 시험."""
    raw = _synthetic_scene_glb()
    cams = _four_cameras_for_scene(raw)
    g = ge.parse_glb(raw)
    model = oc.load_scene_for_objects(g)
    merged = oc.build_merged_scene_mesh(g, model)
    cfg = oc.ObjectCompletionConfig(mask_backend="color_refined")

    candidates, summary = oc.extract_object_candidates(merged, cams, cfg)
    assert abs(summary["planes"]["floor_z_m"] - 0.0) < 0.02
    assert summary["planes"]["wall_planes"] >= 1
    assert summary["candidates_found"] == 3

    categories = {c.category for c in candidates}
    assert categories == {"general", "traffic_cone", "signboard"}

    for cand in candidates:
        assert cand.best_camera_id in {"cam_A", "cam_B", "cam_C", "cam_D"}
        assert len(cand.masks_2d) >= 1
        best_mask = cand.masks_2d[cand.best_camera_id]
        assert best_mask.dtype == bool
        assert best_mask.shape == (240, 320)
        assert cand.camera_views[cand.best_camera_id]["visible_pixels"] > 20


def test_icp_alignment_and_floor_contact():
    """2. 복원된 메시의 관측 점군 ICP 정렬 및 바닥(Z=0) 접지 보정 시험."""
    V_obs, F_obs = _box_open_back(1.3, 1.7, 1.3, 1.7, 0.02, 0.44)
    # Completed mesh deliberately shifted by (+0.04, -0.03, +0.05) and floating above Z=0
    V_shifted = V_obs + np.array([0.04, -0.03, 0.05], dtype=np.float64)
    comp = oc.CompletionOutput(
        vertices_world=V_shifted,
        faces=F_obs.copy(),
        face_source=np.full(len(F_obs), tp.GENERATED, dtype=np.uint8),
        uv=np.zeros((len(V_shifted), 2), dtype=np.float64),
        texture_rgb=np.full((32, 32, 3), 128, dtype=np.uint8),
        texel_source_map=np.full((32, 32), tp.GENERATED, dtype=np.uint8),
        backend_name="test_backend",
    )

    cfg = oc.ObjectCompletionConfig()
    aligned, align_info = oc.align_completed_mesh(comp, V_obs, floor_z_m=0.0, cfg=cfg, snap_to_floor=True)

    # Base must snap to Z=0.0
    assert abs(float(aligned.vertices_world[:, 2].min()) - 0.0) < 1e-6
    assert align_info["floor_contact_error_m"] == 0.0
    # XY center must align closely to observed candidate center
    xy_err = np.linalg.norm(
        aligned.vertices_world[:, :2].mean(axis=0) - V_obs[:, :2].mean(axis=0)
    )
    assert xy_err < 0.015


def test_validation_failure_keeps_original_mesh():
    """3. 검증 수치(IoU·관측 점 거리·삼각형 수 등) 기준 미달일 때 원본 메시를 그대로 유지하는지 시험."""
    raw = _synthetic_scene_glb()
    cams = _four_cameras_for_scene(raw)

    class BadDisplacedBackend:
        name = "bad_displaced"

        def complete(self, inp: oc.CompletionInput) -> oc.CompletionOutput:
            V_bad = inp.observed_vertices_world * 0.1 + np.array([3.5, 3.5, 1.2])
            F_bad = inp.observed_faces.copy()
            return oc.CompletionOutput(
                vertices_world=V_bad,
                faces=F_bad,
                face_source=np.full(len(F_bad), tp.GENERATED, dtype=np.uint8),
                uv=np.zeros((len(V_bad), 2), dtype=np.float64),
                texture_rgb=np.zeros((16, 16, 3), dtype=np.uint8),
                texel_source_map=np.full((16, 16), tp.GENERATED, dtype=np.uint8),
                backend_name=self.name,
            )

    # Set strict thresholds so templates & general objects all fail validation
    strict_th = oc.ValidationThresholds(
        min_reprojection_iou=0.999,
        max_obs_to_mesh_median_m=0.0001,
        max_faces_per_object=5,
    )
    cfg = oc.ObjectCompletionConfig(thresholds=strict_th)
    res = oc.complete_glb_objects(raw, cameras=cams, config=cfg, backend=BadDisplacedBackend())
    assert res.glb is None
    assert res.report["applied"] is False
    assert res.report["reason"] == "ALL_CANDIDATES_REJECTED"
    assert res.report["accepted_count"] == 0
    assert res.report["rejected_count"] == 3
    for obj_rep in res.report["objects"]:
        assert obj_rep["validation"]["accepted"] is False
        assert len(obj_rep["validation"]["rejection_reasons"]) >= 1


def test_provenance_surface_source_and_glb_merge():
    """4. 복원 물체 GLB 병합(개별 노드 삽입 + 원본 조각 제거) 및 면·텍셀별 출처(`OBSERVED=2`, `GENERATED=4`) 기록 시험."""
    assert tp.GENERATED == 4
    assert tp.ALL_SURFACE_VALUES["GENERATED"] == 4
    assert oc.SURFACE_VALUE_TABLE["4"] == "GENERATED"

    raw = _synthetic_scene_glb()
    cams = _four_cameras_for_scene(raw)
    cfg = oc.ObjectCompletionConfig(
        backend="convex_hull",
        mask_backend="projection",
        thresholds=oc.ValidationThresholds(min_reprojection_iou=0.25),
    )
    res = oc.complete_glb_objects(raw, cameras=cams, config=cfg)
    assert res.glb is not None
    assert res.report["applied"] is True
    assert res.report["accepted_count"] == 3

    g_out = ge.parse_glb(res.glb)
    doc_out = g_out.doc
    simai_ext = doc_out["extras"]["simai"]
    assert simai_ext["axis_convention"] == "GLTF_Y_UP"
    assert simai_ext["surface_source"]["values"]["GENERATED"] == 4
    obj_ext = simai_ext["object_completion"]
    assert obj_ext["applied"] is True
    assert obj_ext["safety_use"] is False
    assert obj_ext["surface_values"]["4"] == "GENERATED"
    assert obj_ext["surface_values"]["2"] == "OBSERVED"

    comp_nodes = [
        n
        for n in doc_out["nodes"]
        if str(n.get("name", "")).startswith(("completed_", "template_"))
    ]
    assert len(comp_nodes) == res.report["accepted_count"]
    for node in comp_nodes:
        ns = node["extras"]["simai"]
        assert ns["safety_use"] is False
        assert ns["surface_source"] in {"GENERATED", "OBSERVED"}
        assert ns["source_map_image"] is not None
        ratios = ns["face_source_ratios_area"]
        assert abs(sum(ratios.values()) - 1.0) < 1e-4
        assert ratios.get("GENERATED", 0.0) > 0.0

        # Decode provenance source_map_image PNG and confirm texel values are in {UNOBSERVED(0), OBSERVED(2), ESTIMATED_PLANE(3), GENERATED(4)}
        src_img_idx = int(ns["source_map_image"])
        bv = doc_out["bufferViews"][doc_out["images"][src_img_idx]["bufferView"]]
        png_raw = bytes(g_out.bin[bv["byteOffset"] : bv["byteOffset"] + bv["byteLength"]])
        src_map = cv2.imdecode(np.frombuffer(png_raw, np.uint8), cv2.IMREAD_UNCHANGED)
        assert src_map is not None
        uniq_vals = set(np.unique(src_map).tolist())
        assert tp.GENERATED in uniq_vals
        assert uniq_vals <= {tp.UNOBSERVED, tp.OBSERVED, tp.ESTIMATED_PLANE, tp.GENERATED}


def test_disable_switch_and_skip_reasons(tmp_path):
    """5. `SIMAI_GLB_OBJECTS=off` 비활성화 스위치 및 중복 실행 건너뛰기 시험."""
    raw = _synthetic_scene_glb()
    for val in ("0", "false", "off", "no", "disabled"):
        cfg_env = oc.ObjectCompletionConfig.from_env({"SIMAI_GLB_OBJECTS": val})
        assert cfg_env.enabled is False
        res = oc.complete_glb_objects(raw, config=cfg_env)
        assert res.glb is None and res.report["reason"] == "DISABLED"

    cfg_opt = oc.ObjectCompletionConfig.from_env({}, {"objects": False})
    assert cfg_opt.enabled is False
    assert oc.complete_glb_objects(raw, config=cfg_opt).report["reason"] == "DISABLED"

    # Invalid GLB bytes
    bad = oc.complete_glb_objects(b"not-a-valid-glb")
    assert bad.glb is None and bad.report["reason"] == "NOT_GLB"

    # Second run on already-completed GLB skips with ALREADY_COMPLETED
    cams = _four_cameras_for_scene(raw)
    cfg = oc.ObjectCompletionConfig(thresholds=oc.ValidationThresholds(min_reprojection_iou=0.25))
    first = oc.complete_glb_objects(raw, cameras=cams, config=cfg)
    assert first.glb is not None
    second = oc.complete_glb_objects(first.glb, cameras=cams, config=cfg)
    assert second.glb is None and second.report["reason"] == "ALREADY_COMPLETED"

    # File helper test
    p = tmp_path / "scene.glb"
    p.write_bytes(raw)
    rep_file = oc.complete_glb_file(p, config=cfg, cameras=cams)
    assert rep_file["applied"] is True


def test_traffic_cone_and_signboard_template_dimensions():
    """6. 라바콘 정형 도형 치수(높이 67.5 cm, 밑면 37 cm 정사각형, 윗지름 4.5 cm) 및 안내판/3D-Fixer 입력 텐서 시험."""
    V_cone, F_cone, UV_cone = oc.build_traffic_cone_template()
    spans = V_cone.max(axis=0) - V_cone.min(axis=0)
    assert abs(float(V_cone[:, 2].min()) - 0.0) < 1e-6
    assert abs(float(V_cone[:, 2].max()) - 0.675) < 1e-6
    assert abs(float(spans[0]) - 0.37) < 1e-6
    assert abs(float(spans[1]) - 0.37) < 1e-6
    assert abs(float(spans[2]) - 0.675) < 1e-6

    # Top ring vertices at z == 0.675 must have diameter 0.045 m (radius 0.0225 m)
    top_mask = np.abs(V_cone[:, 2] - 0.675) < 1e-6
    top_pts = V_cone[top_mask]
    top_radii = np.hypot(top_pts[:, 0], top_pts[:, 1])
    top_outer = top_radii[top_radii > 1e-6]
    assert len(top_outer) > 0
    assert np.allclose(top_outer * 2.0, 0.045, atol=1e-6)
    assert UV_cone.shape == (len(V_cone), 2)
    assert F_cone.shape[1] == 3

    # Test complete_traffic_cone end-to-end on partial cone observation
    V_obs, F_obs = _partial_cone(2.5, 1.8, height=0.58)
    cam = _look_at_camera("cam_A", (2.5, 0.2, 1.2), (2.5, 1.8, 0.3))
    mask = np.zeros((240, 320), dtype=bool)
    mask[60:180, 120:200] = True
    inp = oc.CompletionInput(
        candidate_id="cone_00",
        category="traffic_cone",
        best_camera=cam,
        mask_2d=mask,
        all_cameras=[cam],
        all_masks_2d={"cam_A": mask},
        observed_points_world=V_obs,
        points_mask=np.ones(len(V_obs), dtype=np.float32),
        observed_vertices_world=V_obs,
        observed_faces=F_obs,
        observed_face_mask=np.ones(len(F_obs), dtype=bool),
        observed_vertex_colors_srgb=np.full((len(V_obs), 3), (240, 80, 30), dtype=np.uint8),
        floor_z_m=0.0,
    )
    cfg = oc.ObjectCompletionConfig()
    cone_out = oc.complete_traffic_cone(inp, cfg)
    cone_spans = cone_out.vertices_world.max(axis=0) - cone_out.vertices_world.min(axis=0)
    assert abs(float(cone_spans[2]) - 0.675) < 1e-5
    assert (cone_out.face_source == tp.GENERATED).any()
    assert cone_out.meta["height_m"] == 0.675
    assert cone_out.meta["base_side_m"] == 0.37
    assert cone_out.meta["top_diameter_m"] == 0.045

    # Test ThreeDFixerBackend conditioning tensor preparation & CPU/VRAM guard
    cond = oc.ThreeDFixerBackend.prepare_conditioning_tensors(
        cam.image, mask, cam.K, cam.R, cam.t, crop_size=518
    )
    assert cond["extrinsics"].shape == (4, 4)
    assert cond["intrinsics"].shape == (3, 3)
    assert 0.0 < cond["intrinsics"][0, 0] < 5.0
    assert 0.0 < cond["intrinsics"][0, 2] <= 1.0
    assert 0.0 < cond["intrinsics"][1, 2] <= 1.0
    assert cond["instance_image_masked"].shape == (4, 518, 518)
    assert cond["scene_image_masked"].shape == (4, 518, 518)

    fixer = oc.ThreeDFixerBackend(cfg)
    with pytest.raises((oc.CompletionSkip, ImportError)):
        fixer.complete(inp)
