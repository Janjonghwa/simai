"""GLB 시각 보강(바닥 평면·미관측 색 채우기, S15P31A101-481) 합성 시험."""
from __future__ import annotations

import cv2
import numpy as np

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import reconstruct_tasks as tasks


def _png(rgb: tuple[int, int, int]) -> bytes:
    img = np.zeros((8, 8, 3), np.uint8)
    img[:] = rgb[::-1]
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def _glb(floor_rgb=(120, 100, 80)) -> bytes:
    """World 4x4 m 바닥(가운데 1x1 m 구멍) + 미관측 벽(x=0). glTF 변환 (x, z, -y)."""
    n = 41
    xs = np.linspace(0, 4, n)
    pts, idx = [], []
    vid = -np.ones((n, n), int)
    for i, x in enumerate(xs):
        for j, y in enumerate(xs):
            vid[i, j] = len(pts)
            pts.append((x, y, 0.02))
    for i in range(n - 1):
        for j in range(n - 1):
            cx, cy = (xs[i] + xs[i + 1]) / 2, (xs[j] + xs[j + 1]) / 2
            if 1.5 < cx < 2.5 and 1.5 < cy < 2.5:
                continue
            a, b, c, d = vid[i, j], vid[i + 1, j], vid[i + 1, j + 1], vid[i, j + 1]
            idx += [a, b, c, a, c, d]
    floor_w = np.asarray(pts, np.float32)
    floor_g = np.stack([floor_w[:, 0], floor_w[:, 2], -floor_w[:, 1]], 1).astype("<f4")
    floor_uv = np.full((len(floor_w), 2), 0.5, "<f4")
    floor_idx = np.asarray(idx, "<u4")
    wall_w = np.array([[0, 0, 0.1], [0, 4, 0.1], [0, 4, 1.0], [0, 0, 1.0]], np.float32)
    wall_g = np.stack([wall_w[:, 0], wall_w[:, 2], -wall_w[:, 1]], 1).astype("<f4")
    wall_idx = np.array([0, 1, 2, 0, 2, 3], "<u4")
    png = _png(floor_rgb)

    blob = bytearray()
    views: list[dict] = []
    accs: list[dict] = []

    def add(data: bytes, target=None) -> int:
        while len(blob) % 4:
            blob.append(0)
        v = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            v["target"] = target
        blob.extend(data)
        views.append(v)
        return len(views) - 1

    def acc(view, comp, count, typ, **kw) -> int:
        accs.append({"bufferView": view, "componentType": comp, "count": count, "type": typ, **kw})
        return len(accs) - 1

    f_pos = acc(add(floor_g.tobytes(), 34962), 5126, len(floor_g), "VEC3", min=floor_g.min(0).tolist(), max=floor_g.max(0).tolist())
    f_uv = acc(add(floor_uv.tobytes(), 34962), 5126, len(floor_uv), "VEC2")
    f_idx = acc(add(floor_idx.tobytes(), 34963), 5125, len(floor_idx), "SCALAR")
    w_pos = acc(add(wall_g.tobytes(), 34962), 5126, 4, "VEC3", min=wall_g.min(0).tolist(), max=wall_g.max(0).tolist())
    w_idx = acc(add(wall_idx.tobytes(), 34963), 5125, 6, "SCALAR")
    img_view = add(png)
    doc = {
        "asset": {"version": "2.0"},
        "scene": 0, "scenes": [{"nodes": [0, 1]}],
        "nodes": [{"name": "textured_0000", "mesh": 0}, {"name": "unobserved_neutral", "mesh": 1}],
        "meshes": [
            {"name": "textured_0000", "primitives": [{"attributes": {"POSITION": f_pos, "TEXCOORD_0": f_uv}, "indices": f_idx, "material": 0}]},
            {"name": "unobserved_neutral", "primitives": [{"attributes": {"POSITION": w_pos}, "indices": w_idx, "material": 1}]},
        ],
        "materials": [
            {"name": "textured_0000", "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}},
            {"name": "unobserved_neutral", "pbrMetallicRoughness": {"baseColorFactor": [0.5, 0.5, 0.5, 1]}},
        ],
        "textures": [{"source": 0}],
        "images": [{"bufferView": img_view, "mimeType": "image/png"}],
        "bufferViews": views, "accessors": accs, "buffers": [{"byteLength": len(blob)}],
        "extras": {"simai": {"axis_convention": "GLTF_Y_UP"}},
    }
    return ge.serialize_glb(ge.GlbFile(doc, blob))


def _doc_bin(raw: bytes):
    g = ge.parse_glb(raw)
    return g.doc, bytes(g.bin)


def _camera(photo_rgb) -> ge.CheckCamera:
    """바닥 가운데 위 5 m 에서 아래를 보는 카메라."""
    R = np.array([[1, 0, 0], [0, -1, 0], [0, 0, -1]], float)
    center = np.array([2.0, 2.0, 5.0])
    t = -R @ center
    K = np.array([[200.0, 0, 160], [0, 200.0, 120], [0, 0, 1]])
    img = np.zeros((240, 320, 3), np.uint8)
    img[:] = photo_rgb
    return ge.CheckCamera("cam_T", K, R, t, img)


def test_floor_and_recolor_applied_original_preserved():
    raw = _glb()
    res = ge.enhance_glb(raw)
    assert res.glb is not None and res.report["applied"] is True
    assert res.report["check"]["mode"] == "MESH_METRICS_ONLY" and res.report["check"]["reason"] == "NO_CHECK_INPUT"
    fl, rc = res.report["floor"], res.report["recolor"]
    assert fl["empty_ratio_after"] < fl["empty_ratio_before"] and fl["surface_source"] == "ESTIMATED_PLANE"
    assert fl["z_m"] < 0.02 and fl["color_srgb8"] == [120, 100, 80]
    assert rc["unobserved_gray_area_m2_after"] < rc["unobserved_area_m2_before"] * 0.1
    doc0, bin0 = _doc_bin(raw)
    doc1, bin1 = _doc_bin(res.glb)
    assert bin1[: len(bin0)] == bin0  # 관측 데이터·텍스처 바이트는 그대로, 뒤에 덧붙이기만
    assert doc1["meshes"][0] == doc0["meshes"][0] and doc1["images"] == doc0["images"]
    wall = doc1["meshes"][1]["primitives"][0]
    assert "COLOR_0" in wall["attributes"]
    assert doc1["materials"][wall["material"]]["pbrMetallicRoughness"]["baseColorFactor"][:3] == [1.0, 1.0, 1.0]
    enh = doc1["extras"]["simai"]["enhanced"]
    assert enh["applied"] and enh["safety_use"] is False and enh["estimated_floor_plane"]["surface_source"] == "ESTIMATED_PLANE"
    assert doc1["extras"]["simai"]["axis_convention"] == "GLTF_Y_UP"
    assert any(n.get("name") == "estimated_floor_plane" for n in doc1["nodes"])
    again = ge.enhance_glb(res.glb)
    assert again.glb is None and again.report["reason"] == "ALREADY_ENHANCED"


def test_check_accepts_when_photo_matches_enhanced_floor():
    res = ge.enhance_glb(_glb(), cameras=[_camera((120, 100, 80))])
    chk = res.report["check"]
    assert chk["mode"] == "RENDER_MAE" and chk["accepted"] is True and res.glb is not None
    assert chk["mae_mean_after"] <= chk["mae_mean_before"]


def test_check_regression_keeps_original():
    res = ge.enhance_glb(_glb(), cameras=[_camera((0, 0, 0))])
    assert res.glb is None and res.report["applied"] is False and res.report["reason"] == "CHECK_REGRESSION"
    assert res.report["check"]["mae_mean_after"] > res.report["check"]["mae_mean_before"]


def test_disabled_and_skip_reasons():
    assert ge.enhance_glb(_glb(), config=ge.EnhanceConfig(enabled=False)).report["reason"] == "DISABLED"
    res = ge.enhance_glb(b"not-a-glb-at-all-not-a-glb-at-all")
    assert res.glb is None and res.report["reason"] == "NOT_GLB"
    assert ge.EnhanceConfig.from_env({"SIMAI_GLB_ENHANCE": "0"}).enabled is False
    assert ge.EnhanceConfig.from_env({}, {"enhance": False}).enabled is False


def test_stage_helper_in_place_and_failures_keep_original(tmp_path):
    glb = tmp_path / "scene.glb"
    glb.write_bytes(_glb())
    rep = tasks._enhance_summary(tasks.enhance_visual_glb(tmp_path, glb, {}))
    assert rep["applied"] and rep["safety_use"] is False and rep["check_mode"] == "MESH_METRICS_ONLY"
    assert rep["floor_filled_area_m2"] > 0 and rep["neutral_recolored_area_m2"] > 0
    assert "enhanced" in ge.parse_glb(glb.read_bytes()).doc["extras"]["simai"]
    off = tmp_path / "off.glb"
    off.write_bytes(_glb())
    before = off.read_bytes()
    assert tasks.enhance_visual_glb(tmp_path, off, {"enhance": False})["reason"] == "DISABLED"
    assert off.read_bytes() == before
    junk = tmp_path / "junk.glb"
    junk.write_bytes(b"fake-glb")
    assert tasks.enhance_visual_glb(tmp_path, junk, {})["applied"] is False and junk.read_bytes() == b"fake-glb"
