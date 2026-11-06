"""12차 로비 장면(`current-scene-textured.glb`)에서 물체 후보 분리, 2D 마스크, 가짜 백엔드(`convex_hull`),
라바콘(`67.5 cm × 37 cm × 4.5 cm`)·안내판 정형 도형, 출처(`GENERATED=4`) 기록을 노트북에서 실행·검증하고
시각 확인 이미지 및 GPU Pod 일괄 실행용 12차 입력 번들을 저장하는 스크립트.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import cv2
import numpy as np

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import object_completion as oc
from simai_ai.scene import texture_provenance as tp

DEFAULT_GLB = Path("C:/Users/SSAFY/simai-local/grtmesh-poc/current-scene-textured.glb")
DEFAULT_CALIB = Path(
    "C:/Users/SSAFY/simai-local/session12/results/lobby-s12/state/config/bundles/"
    "01447ea4d264ca541a3ea250fa57f22c029447719e49b4cf16946a5c67f32650/calibration.json"
)
DEFAULT_PHOTO_DIR = Path(
    "C:/Users/SSAFY/simai-local/session12/results/cal-lobby-init-f0ab1c6b-jba2-2f6cc097/"
    "bundle-393d5fbdabad41749559af4526a0a2ce"
)
DEFAULT_OUT_DIR = Path("C:/Users/SSAFY/simai-local/object-completion")
DEFAULT_GPU_POC_DIR = Path("C:/Users/SSAFY/simai-local/gpu-poc-map")


def _strip_enhanced_nodes(raw: bytes) -> bytes:
    """이미 `-511` 보강(`estimated_*`)이 적용된 GLB에서 추정 평면 노드를 걷어내 순수 TSDF 원본 GLB를 복원한다."""
    import copy

    g = ge.parse_glb(raw)
    doc = copy.deepcopy(g.doc)
    est_nodes = {
        i
        for i, n in enumerate(doc.get("nodes", []))
        if str(n.get("name", "")).startswith("estimated_")
        or ((n.get("extras") or {}).get("simai") or {}).get("surface_source") == "ESTIMATED_PLANE"
    }
    for s in doc.get("scenes", []):
        if "nodes" in s:
            s["nodes"] = [n for n in s["nodes"] if n not in est_nodes]
    for n in doc.get("nodes", []):
        if "children" in n:
            n["children"] = [c for c in n["children"] if c not in est_nodes]
    simai = (doc.get("extras") or {}).get("simai")
    if isinstance(simai, dict):
        simai.pop("enhanced", None)
        simai.pop("object_completion", None)
    g.doc = doc
    return ge.serialize_glb(g)


PALETTE_BGR = [
    (36, 110, 255),   # 주황 (traffic_cone 등)
    (230, 160, 30),   # 하늘
    (80, 210, 80),    # 초록
    (210, 90, 210),   # 보라
    (50, 215, 235),   # 노랑
    (180, 110, 255),  # 분홍
    (255, 190, 90),   # 청록
    (90, 140, 230),   # 갈색/살구
    (130, 230, 150),  # 연두
    (240, 130, 140),  # 연보라
]


def _look_at_cam(
    cam_id: str,
    eye: tuple[float, float, float],
    target: tuple[float, float, float],
    w: int = 800,
    h: int = 600,
    fx: float = 650.0,
) -> ge.CheckCamera:
    eye_v = np.asarray(eye, dtype=np.float64)
    tgt_v = np.asarray(target, dtype=np.float64)
    forward = tgt_v - eye_v
    forward /= np.linalg.norm(forward)
    up_world = np.array([0.0, 0.0, 1.0], dtype=np.float64)
    if abs(float(forward @ up_world)) > 0.98:
        up_world = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    right = np.cross(forward, up_world)
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    R = np.vstack([right, down, forward])
    t = -R @ eye_v
    K = np.array([[fx, 0.0, w * 0.5], [0.0, fx, h * 0.5], [0.0, 0.0, 1.0]], dtype=np.float64)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    return ge.CheckCamera(cam_id, K, R, t, img)


def _render_glb_from_camera(raw_glb: bytes, cam: ge.CheckCamera, h: int, w: int) -> np.ndarray:
    """임의 GLB 바이트(추정 바닥/벽 및 완성 물체 노드 포함)를 지정 카메라 시점으로 렌더링한다 (RGB 반환)."""
    g = ge.parse_glb(raw_glb)
    doc = g.doc
    materials = doc.get("materials", [])
    textures = doc.get("textures", [])
    samplers = doc.get("samplers", [])
    scene_nodes = (
        doc["scenes"][doc.get("scene", 0)].get("nodes", [])
        if doc.get("scenes")
        else list(range(len(doc.get("nodes", []))))
    )

    prims: list[ge.Prim] = []
    stack = [(n, np.eye(4)) for n in scene_nodes]
    while stack:
        n, parent = stack.pop()
        node = doc["nodes"][n]
        M = parent @ ge._node_matrix(node)
        if "mesh" in node:
            mesh = doc["meshes"][node["mesh"]]
            for pi, p in enumerate(mesh["primitives"]):
                if p.get("mode", 4) != 4 or "POSITION" not in p["attributes"]:
                    continue
                attrs = p["attributes"]
                P = ge._accessor(g, attrs["POSITION"]).astype(np.float64)
                P = P @ M[:3, :3].T + M[:3, 3]
                idx = (
                    ge._accessor(g, p["indices"]).reshape(-1).astype(np.int64)
                    if "indices" in p
                    else np.arange(len(P), dtype=np.int64)
                )
                idx = idx[: len(idx) // 3 * 3].reshape(-1, 3)
                mat = materials[p["material"]] if "material" in p else {}
                pbr = mat.get("pbrMetallicRoughness", {})
                factor = ge.srgb_to_linear(np.asarray(pbr.get("baseColorFactor", [1, 1, 1, 1])[:3], np.float64))
                uv = None
                image = None
                wrap = (10497, 10497)
                tex_info = pbr.get("baseColorTexture")
                if tex_info is not None and "TEXCOORD_0" in attrs:
                    tex = textures[tex_info["index"]]
                    if tex.get("source") is not None:
                        image = int(tex["source"])
                        uv = ge._accessor(g, attrs["TEXCOORD_0"]).astype(np.float64)
                        if "sampler" in tex:
                            sm = samplers[tex["sampler"]]
                            wrap = (int(sm.get("wrapS", 10497)), int(sm.get("wrapT", 10497)))
                col = ge._accessor(g, attrs["COLOR_0"]).astype(np.float64)[:, :3] if "COLOR_0" in attrs else None
                prims.append(
                    ge.Prim(
                        name=str(mesh.get("name", "")),
                        kind="observed",
                        pos=ge._glb_to_world(P),
                        idx=idx,
                        uv=uv,
                        col=col,
                        factor=factor,
                        image=image,
                        wrap=wrap,
                        mesh_index=node["mesh"],
                        prim_index=pi,
                    )
                )
        for ch in node.get("children", []):
            stack.append((ch, M))

    model = ge.SceneModel(prims, {})
    rcfg = ge.EnhanceConfig(check_render_long_side=max(h, w), check_background_rgb=(28, 30, 34))
    rgb, _, _ = ge.render_view(g, model, prims, cam, rcfg)
    if rgb.shape[:2] != (h, w):
        rgb = cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA)
    return rgb


def _save_candidates_bev(
    merged: oc.MergedSceneMesh,
    candidates: list[oc.ObjectCandidate],
    summary: dict,
    out_path: Path,
) -> None:
    """물체 후보 분리 결과 조감도(BEV) + 측면 높이 분포 이미지를 저장한다."""
    H, W = 900, 1400
    canvas = np.full((H, W, 3), (24, 26, 30), dtype=np.uint8)

    # Left panel: BEV (XY: x in [-1, 10], y in [-1, 9])
    bev_w = 820
    x_min, x_max = -0.5, 9.8
    y_min, y_max = -0.5, 8.8

    def to_px(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        px = ((x - x_min) / (x_max - x_min) * (bev_w - 80) + 40).astype(np.int32)
        py = (H - 50 - (y - y_min) / (y_max - y_min) * (H - 110)).astype(np.int32)
        return np.clip(px, 0, bev_w - 1), np.clip(py, 0, H - 1)

    # Draw all floor/wall faces in muted gray
    fc = merged.centers
    px_all, py_all = to_px(fc[:, 0], fc[:, 1])
    canvas[py_all, px_all] = (70, 72, 78)

    # Draw each candidate
    for ci, cand in enumerate(candidates):
        col = PALETTE_BGR[ci % len(PALETTE_BGR)]
        c_fc = merged.centers[cand.face_indices]
        px, py = to_px(c_fc[:, 0], c_fc[:, 1])
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                canvas[np.clip(py + dy, 0, H - 1), np.clip(px + dx, 0, bev_w - 1)] = col

        bx0, by0 = to_px(np.array([cand.bbox_min_world[0]]), np.array([cand.bbox_max_world[1]]))
        bx1, by1 = to_px(np.array([cand.bbox_max_world[0]]), np.array([cand.bbox_min_world[1]]))
        cv2.rectangle(canvas, (int(bx0[0]) - 3, int(by0[0]) - 3), (int(bx1[0]) + 3, int(by1[0]) + 3), col, 2)
        label = f"{cand.candidate_id} ({cand.category})"
        cv2.putText(
            canvas,
            label,
            (int(bx0[0]), max(25, int(by0[0]) - 6)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.46,
            (255, 255, 255),
            1,
            cv2.LINE_AA,
        )

    cv2.putText(
        canvas,
        "Session 12 Lobby - Object Candidates BEV (World XY, Floor & Walls Removed)",
        (25, 32),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (240, 240, 240),
        2,
        cv2.LINE_AA,
    )

    # Right panel: table of candidates
    rx = bev_w + 20
    cv2.line(canvas, (bev_w, 20), (bev_w, H - 20), (80, 85, 95), 1)
    cv2.putText(
        canvas,
        f"Detected Candidates: {len(candidates)} (floor_z={summary['planes']['floor_z_m']}m, walls={summary['planes']['wall_planes']})",
        (rx, 40),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.50,
        (220, 230, 250),
        1,
        cv2.LINE_AA,
    )
    y_cursor = 78
    for ci, cand in enumerate(candidates):
        col = PALETTE_BGR[ci % len(PALETTE_BGR)]
        sz = cand.bbox_max_world - cand.bbox_min_world
        cv2.rectangle(canvas, (rx, y_cursor - 12), (rx + 16, y_cursor + 4), col, -1)
        info = (
            f"{cand.candidate_id:8s} | {cand.category:12s} | best={cand.best_camera_id or '-':5s} "
            f"| {sz[0]:.2f}x{sz[1]:.2f}x{sz[2]:.2f}m | F={len(cand.faces)}"
        )
        cv2.putText(
            canvas,
            info,
            (rx + 24, y_cursor),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (235, 235, 235),
            1,
            cv2.LINE_AA,
        )
        y_cursor += 34

    cv2.imwrite(str(out_path), canvas)


def _save_camera_masks_grid(
    cameras: list[ge.CheckCamera],
    candidates: list[oc.ObjectCandidate],
    out_path: Path,
) -> None:
    """4대 카메라 왜곡 보정 사진 위에 투영된 물체 후보 2D 마스크와 최적 시점 표시를 그리드로 저장한다."""
    panels: list[np.ndarray] = []
    tw, th = 640, 360
    for cam in cameras:
        vis_rgb = cv2.resize(cam.image, (tw, th), interpolation=cv2.INTER_AREA)
        vis_bgr = vis_rgb[:, :, ::-1].copy()
        overlay = vis_bgr.copy()

        for ci, cand in enumerate(candidates):
            m = cand.masks_2d.get(cam.camera_id)
            if m is None or not m.any():
                continue
            m_resized = cv2.resize(m.astype(np.uint8), (tw, th), interpolation=cv2.INTER_NEAREST) > 0
            col = PALETTE_BGR[ci % len(PALETTE_BGR)]
            overlay[m_resized] = col
            ys, xs = np.nonzero(m_resized)
            if len(ys):
                x0, y0, x1, y1 = int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())
                is_best = cand.best_camera_id == cam.camera_id
                thickness = 2 if is_best else 1
                cv2.rectangle(vis_bgr, (x0, y0), (x1, y1), col, thickness)
                tag = f"{cand.candidate_id}{'*BEST' if is_best else ''}"
                cv2.putText(
                    vis_bgr,
                    tag,
                    (x0, max(16, y0 - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.42,
                    (255, 255, 255) if is_best else col,
                    1,
                    cv2.LINE_AA,
                )

        blended = cv2.addWeighted(overlay, 0.42, vis_bgr, 0.58, 0.0)
        cv2.putText(
            blended,
            f"{cam.camera_id} (Undistorted + Projected 2D Masks, *BEST = Selected View)",
            (14, 26),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (255, 255, 255),
            2,
            cv2.LINE_AA,
        )
        panels.append(blended)

    top_row = np.hstack(panels[:2])
    bot_row = np.hstack(panels[2:4])
    grid = np.vstack([top_row, bot_row])
    cv2.imwrite(str(out_path), grid, [int(cv2.IMWRITE_JPEG_QUALITY), 93])


def _save_before_after_4cams(
    raw_before: bytes,
    raw_after: bytes,
    cameras: list[ge.CheckCamera],
    out_path: Path,
) -> None:
    """4대 보정 카메라 시점에서 완성 전/후 GLB 렌더링 비교 이미지를 저장한다."""
    rows: list[np.ndarray] = []
    rw, rh = 560, 315
    for cam in cameras:
        bef_rgb = _render_glb_from_camera(raw_before, cam, rh, rw)
        aft_rgb = _render_glb_from_camera(raw_after, cam, rh, rw)
        photo_rgb = cv2.resize(cam.image, (rw, rh), interpolation=cv2.INTER_AREA)

        bef_bgr = bef_rgb[:, :, ::-1].copy()
        aft_bgr = aft_rgb[:, :, ::-1].copy()
        pho_bgr = photo_rgb[:, :, ::-1].copy()

        cv2.putText(pho_bgr, f"{cam.camera_id} - Reference Photo", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
        cv2.putText(bef_bgr, f"{cam.camera_id} - BEFORE (Original GLB)", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (180, 220, 255), 2)
        cv2.putText(aft_bgr, f"{cam.camera_id} - AFTER (Objects Completed + Cone Template)", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (120, 255, 150), 2)
        rows.append(np.hstack([pho_bgr, bef_bgr, aft_bgr]))

    full = np.vstack(rows)
    cv2.imwrite(str(out_path), full, [int(cv2.IMWRITE_JPEG_QUALITY), 93])


def _save_zoom_and_provenance(
    raw_before: bytes,
    raw_after: bytes,
    res: oc.ObjectCompletionResult,
    out_zoom_path: Path,
    out_prov_path: Path,
) -> None:
    """라바콘(67.5cm x 37cm x 4.5cm) 및 주요 복원 물체 근접 비교(앞·뒤 미관측 시점)와 출처(`GENERATED=4`) 시각화 이미지를 저장한다."""
    # Find cone and a general completed object
    cone_node = next((n for n in res.completed_nodes if n.candidate.category == "traffic_cone"), None)
    gen_node = next((n for n in res.completed_nodes if n.candidate.category == "general"), None)
    sign_node = next((n for n in res.completed_nodes if n.candidate.category == "signboard"), None)

    close_cams: list[tuple[str, ge.CheckCamera]] = []
    if cone_node is not None:
        c_center = 0.5 * (cone_node.candidate.bbox_min_world + cone_node.candidate.bbox_max_world)
        cx, cy = float(c_center[0]), float(c_center[1])
        close_cams.append((
            "Traffic Cone (Front-Side View, 67.5x37x4.5cm)",
            _look_at_cam("cone_front", (cx - 0.85, cy + 0.55, 0.68), (cx, cy, 0.30), w=520, h=400, fx=520.0),
        ))
        close_cams.append((
            "Traffic Cone (Back/Unobserved View)",
            _look_at_cam("cone_back", (cx + 0.35, cy + 0.90, 0.68), (cx, cy, 0.30), w=520, h=400, fx=520.0),
        ))
    if gen_node is not None:
        g_center = 0.5 * (gen_node.candidate.bbox_min_world + gen_node.candidate.bbox_max_world)
        gx, gy = float(g_center[0]), float(g_center[1])
        close_cams.append((
            f"Completed Object ({gen_node.candidate.candidate_id}, Back-Side View)",
            _look_at_cam("gen_back", (gx + 1.25, gy + 1.15, 0.90), (gx, gy, 0.35), w=520, h=400, fx=480.0),
        ))
    if sign_node is not None:
        s_center = 0.5 * (sign_node.candidate.bbox_min_world + sign_node.candidate.bbox_max_world)
        sx, sy, sz = float(s_center[0]), float(s_center[1]), float(s_center[2])
        close_cams.append((
            f"Signboard Template ({sign_node.candidate.candidate_id})",
            _look_at_cam("sign_view", (sx - 1.15, sy + 0.65, sz + 0.15), (sx, sy, sz), w=520, h=400, fx=480.0),
        ))

    zoom_rows: list[np.ndarray] = []
    for title, zcam in close_cams:
        b_rgb = _render_glb_from_camera(raw_before, zcam, 400, 520)
        a_rgb = _render_glb_from_camera(raw_after, zcam, 400, 520)
        b_bgr = b_rgb[:, :, ::-1].copy()
        a_bgr = a_rgb[:, :, ::-1].copy()
        cv2.putText(b_bgr, f"BEFORE - {title}", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (180, 220, 255), 1, cv2.LINE_AA)
        cv2.putText(a_bgr, f"AFTER - {title}", (12, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (120, 255, 150), 1, cv2.LINE_AA)
        zoom_rows.append(np.hstack([b_bgr, a_bgr]))

    if zoom_rows:
        cv2.imwrite(str(out_zoom_path), np.vstack(zoom_rows))

    # Provenance visualization: render completed nodes colored by face_source (OBSERVED=2 green, GENERATED=4 orange/magenta)
    overview_cam = _look_at_cam("overview_oblique", (1.2, 7.6, 2.5), (5.2, 3.8, 0.4), w=960, h=640, fx=620.0)
    aft_overview = _render_glb_from_camera(raw_after, overview_cam, 640, 960)[:, :, ::-1].copy()
    prov_canvas = aft_overview.copy()

    for node in res.completed_nodes:
        V = node.output.vertices_world
        F = node.output.faces
        f_src = node.output.face_source
        tri_cam = V[F] @ overview_cam.R.T + overview_cam.t
        front = tri_cam[:, :, 2].min(axis=1) >= 0.1
        f_ids = np.nonzero(front)[0]
        if not len(f_ids):
            continue
        zc = tri_cam[f_ids, :, 2]
        u = overview_cam.K[0, 0] * tri_cam[f_ids, :, 0] / zc + overview_cam.K[0, 2] + 0.5
        v = overview_cam.K[1, 1] * tri_cam[f_ids, :, 1] / zc + overview_cam.K[1, 2] + 0.5
        tid, _, _ = ge.rasterize(np.stack([u, v], axis=2), zc, (640, 960), perspective=True)
        hit = tid >= 0
        if hit.any():
            src_hit = f_src[f_ids[tid[hit]]]
            obs_px = np.zeros_like(hit)
            gen_px = np.zeros_like(hit)
            obs_px[hit] = src_hit == tp.OBSERVED
            gen_px[hit] = src_hit == tp.GENERATED
            prov_canvas[obs_px] = (70, 215, 70)    # Green = OBSERVED (2)
            prov_canvas[gen_px] = (50, 95, 250)    # Orange-Red = GENERATED (4)

    cv2.putText(
        aft_overview,
        "Oblique Lobby Render - AFTER Object Completion",
        (16, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        prov_canvas,
        "Surface Provenance Map: OBSERVED(2)=Green, GENERATED(4)=Orange-Red",
        (16, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    cv2.imwrite(str(out_prov_path), np.hstack([aft_overview, prov_canvas]))


def _export_gpu_bundle(
    raw_glb: bytes,
    calib_path: Path,
    photo_dir: Path,
    cameras: list[ge.CheckCamera],
    candidates: list[oc.ObjectCandidate],
    out_dir: Path,
    gpu_poc_dir: Path = DEFAULT_GPU_POC_DIR,
) -> tuple[Path, Path]:
    """GPU Pod(H100)에서 Lead가 명령 한 줄로 3D-Fixer(`-515`)와 평면 인페인팅(`-514` FLUX.1-Fill-dev 8-bit)을
    연속 실행할 수 있도록 12차 입력 번들을 생성·압축한다.
    """
    import shutil

    baseline_glb = _strip_enhanced_nodes(raw_glb)
    bundle_dir = out_dir / "s12_3dfixer_bundle"
    if bundle_dir.exists():
        shutil.rmtree(bundle_dir)
    bundle_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "current-scene-textured.glb").write_bytes(raw_glb)
    (bundle_dir / "current-scene-baseline.glb").write_bytes(baseline_glb)
    (bundle_dir / "calibration.json").write_text(calib_path.read_text(encoding="utf-8"), encoding="utf-8")

    raw_img_dir = bundle_dir / "images_raw"
    raw_img_dir.mkdir(exist_ok=True)
    for cid in ("cam_A", "cam_B", "cam_C", "cam_D"):
        src_jpg = photo_dir / f"{cid}.jpg"
        if src_jpg.is_file():
            shutil.copy2(src_jpg, raw_img_dir / f"{cid}.jpg")

    images_dir = bundle_dir / "images_undistorted"
    images_dir.mkdir(exist_ok=True)
    cam_by_id = {c.camera_id: c for c in cameras}
    for cam in cameras:
        cv2.imwrite(str(images_dir / f"{cam.camera_id}.png"), cam.image[:, :, ::-1])

    cands_dir = bundle_dir / "candidates"
    cands_dir.mkdir(exist_ok=True)
    manifest: list[dict] = []
    for cand in candidates:
        if not cand.best_camera_id or cand.best_camera_id not in cam_by_id:
            continue
        cam = cam_by_id[cand.best_camera_id]
        mask = cand.masks_2d[cand.best_camera_id]
        cond = oc.ThreeDFixerBackend.prepare_conditioning_tensors(
            cam.image, mask, cam.K, cam.R, cam.t, crop_size=518
        )
        npz_path = cands_dir / f"{cand.candidate_id}.npz"
        np.savez_compressed(
            npz_path,
            instance_image_masked=cond["instance_image_masked"],
            scene_image_masked=cond["scene_image_masked"],
            extrinsics=cond["extrinsics"],
            intrinsics=cond["intrinsics"],
            points_world=cand.points_world.astype(np.float32),
            points_mask=cand.points_mask.astype(np.float32),
        )
        manifest.append(
            {
                "candidate_id": cand.candidate_id,
                "category": cand.category,
                "best_camera_id": cand.best_camera_id,
                "npz": f"candidates/{cand.candidate_id}.npz",
                "points_count": int(len(cand.points_world)),
                "faces_count": int(len(cand.faces)),
            }
        )

    (bundle_dir / "manifest.json").write_text(
        json.dumps({"session": "session12-lobby", "candidates": manifest}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tar_path = out_dir / "s12_3dfixer_bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(bundle_dir, arcname="s12_3dfixer_bundle")

    # 지시서 3) 노트북에서 미리 올려둘 통합 GPU 입력 묶음 (C:/Users/SSAFY/simai-local/gpu-poc-map/)
    gpu_poc_dir.mkdir(parents=True, exist_ok=True)
    poc_bundle_dir = gpu_poc_dir / "s12_gpu_poc_input"
    if poc_bundle_dir.exists():
        shutil.rmtree(poc_bundle_dir)
    shutil.copytree(bundle_dir, poc_bundle_dir)
    poc_tar_path = gpu_poc_dir / "s12_gpu_poc_input.tar.gz"
    with tarfile.open(poc_tar_path, "w:gz") as tf:
        tf.add(poc_bundle_dir, arcname="s12_gpu_poc_input")
    return tar_path, poc_tar_path


def main() -> int:
    out_dir = DEFAULT_OUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_glb = DEFAULT_GLB.read_bytes()
    calib = json.loads(DEFAULT_CALIB.read_text(encoding="utf-8"))
    photos = {cid: DEFAULT_PHOTO_DIR / f"{cid}.jpg" for cid in ("cam_A", "cam_B", "cam_C", "cam_D")}
    cameras = ge.cameras_from_raw(calib, photos)

    # 1) 본선 기본 백엔드("3d_fixer") 동작 검증: 노트북(6GB VRAM)에서는 3d_fixer가 일반 물체를 CompletionSkip 하고
    #    convex_hull로 대체하지 않은 채 원본 메시 조각을 보존(정형 도형인 라바콘만 교체)하는지 확인한다.
    default_cfg = oc.ObjectCompletionConfig()
    assert default_cfg.backend == "3d_fixer", f"Expected default backend '3d_fixer', got {default_cfg.backend}"
    res_default = oc.complete_glb_objects(raw_glb, config=default_cfg, cameras=cameras)
    assert res_default.report["candidate_summary"]["by_category"]["signboard"] == 0, (
        f"Signboard misclassification remained: {res_default.report['candidate_summary']}"
    )
    gen_objs_default = [o for o in res_default.report["objects"] if o["category"] == "general"]
    assert all(not o.get("accepted", False) and o.get("preserved_original_mesh") is True for o in gen_objs_default), (
        "Default 3d_fixer backend must preserve original mesh on skip, never substitute convex_hull"
    )
    (out_dir / "session12-default-3dfixer-skip-report.json").write_text(
        json.dumps(res_default.report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 2) 노트북 시각화 확인용 명시적 convex_hull 실행
    cfg = oc.ObjectCompletionConfig(
        backend="convex_hull",
        mask_backend="color_refined",
        texture_size=256,
    )
    res = oc.complete_glb_objects(raw_glb, config=cfg, cameras=cameras)
    assert res.glb is not None, f"Object completion was not applied: {res.report}"
    assert res.report["candidate_summary"]["by_category"]["signboard"] == 0, (
        f"Signboard misclassification remained: {res.report['candidate_summary']}"
    )

    out_glb_path = out_dir / "session12-objects-completed.glb"
    out_rep_path = out_dir / "session12-report.json"
    out_glb_path.write_bytes(res.glb)
    out_rep_path.write_text(json.dumps(res.report, ensure_ascii=False, indent=2), encoding="utf-8")

    g = ge.parse_glb(raw_glb)
    model = oc.load_scene_for_objects(g)
    merged = oc.build_merged_scene_mesh(g, model)

    _save_candidates_bev(
        merged,
        res.candidates,
        res.report["candidate_summary"],
        out_dir / "01_candidates_bev.png",
    )
    _save_camera_masks_grid(
        cameras,
        res.candidates,
        out_dir / "02_camera_masks_grid.jpg",
    )
    _save_before_after_4cams(
        raw_glb,
        res.glb,
        cameras,
        out_dir / "03_before_after_4cams.jpg",
    )
    _save_zoom_and_provenance(
        raw_glb,
        res.glb,
        res,
        out_dir / "04_cone_and_objects_zoom.png",
        out_dir / "05_surface_source_provenance.png",
    )
    tar_path, poc_tar_path = _export_gpu_bundle(
        raw_glb, DEFAULT_CALIB, DEFAULT_PHOTO_DIR, cameras, res.candidates, out_dir
    )

    print(
        json.dumps(
            {
                "default_backend": default_cfg.backend,
                "default_accepted_count": res_default.report["accepted_count"],
                "viz_backend": cfg.backend,
                "applied": res.report["applied"],
                "candidates_found": res.report["candidate_summary"]["candidates_found"],
                "by_category": res.report["candidate_summary"]["by_category"],
                "accepted_count": res.report["accepted_count"],
                "rejected_count": res.report["rejected_count"],
                "seconds": res.report["seconds"],
                "output_glb": str(out_glb_path),
                "gpu_bundle": str(tar_path),
                "gpu_poc_input_tar": str(poc_tar_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
