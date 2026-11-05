"""GLB 시각 보강 2단계(늘어난 면 제거·바닥·벽 평면 채우기, S15P31A101-511) 합성 시험. 작은 합성 방(6x6 m)으로 잰다."""
from __future__ import annotations

import zlib
from dataclasses import replace

import numpy as np
import pytest

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import glb_fill as gf

PHOTO = (90, 160, 70)
CFG = ge.EnhanceConfig(fill_vis_long_side=320, fill_wall_ransac_iter=300, check_tolerance_mean=1.0, check_tolerance_camera=1.0)


def grid(origin, u, v, nu, nv, cell, skip=None):
    """origin 에서 u·v 방향으로 nu x nv 칸. 법선 = u x v. skip(cu, cv) 이 True 인 칸은 만들지 않는다(구멍)."""
    origin, u, v = (np.asarray(x, float) for x in (origin, u, v))
    pts = [origin + u * (i * cell) + v * (j * cell) for j in range(nv + 1) for i in range(nu + 1)]
    idx = []
    for j in range(nv):
        for i in range(nu):
            if skip is not None and skip((i + 0.5) * cell, (j + 0.5) * cell):
                continue
            a, b, c, d = j * (nu + 1) + i, j * (nu + 1) + i + 1, (j + 1) * (nu + 1) + i + 1, (j + 1) * (nu + 1) + i
            idx += [[a, b, c], [a, c, d]]
    return np.asarray(pts), np.asarray(idx, np.int64)


def prim(name, V, F, rgb=PHOTO, kind="observed"):
    lin = ge.srgb_to_linear(np.asarray(rgb) / 255.0)
    return ge.Prim(name=name, kind=kind, pos=V, idx=F, factor=lin)


def room(floor_skip=None, wall_hole=True, partition=False, strip=True):
    """6x6 m 방: 바닥(구멍 1.4 m 사각 + 오른쪽 1 m 띠 없음) + 네 벽(x=0 벽에 문 크기 구멍)."""
    def fskip(x, y):
        hole = 2.0 < x < 3.4 and 2.0 < y < 3.4
        band = strip and x > 5.0
        return hole or band or (floor_skip(x, y) if floor_skip else False)

    ex, ey, ez = (1, 0, 0), (0, 1, 0), (0, 0, 1)
    parts = [("floor", *grid((0, 0, 0), ex, ey, 30, 30, 0.2, fskip))]
    parts.append(("wall_x0", *grid((0, 0, 0), ey, ez, 30, 13, 0.2, (lambda a, b: 2.0 < a < 3.0 and 0.8 < b < 1.6) if wall_hole else None)))
    parts.append(("wall_x6", *grid((6, 0, 0), ez, ey, 13, 30, 0.2)))
    parts.append(("wall_y0", *grid((0, 0, 0), ez, ex, 13, 30, 0.2)))
    parts.append(("wall_y6", *grid((0, 6, 0), ex, ez, 30, 13, 0.2)))
    if partition:  # 방 한가운데 3 m 길이 2 m 높이 칸막이(양쪽에 바닥이 있다 = 방 경계가 아니다)
        parts.append(("partition", *grid((1.5, 4.5, 0), ex, ez, 15, 10, 0.2)))
    return [prim(n, V, F) for n, V, F in parts]


def camera(name, eye, target, W=640, H=360, hfov=110.0, valid=None, color=PHOTO):
    eye, target = np.asarray(eye, float), np.asarray(target, float)
    f = target - eye
    f /= np.linalg.norm(f)
    r = np.cross(f, [0, 0, 1.0])
    r /= np.linalg.norm(r)
    R = np.stack([r, np.cross(f, r), f])
    fx = (W / 2) / np.tan(np.radians(hfov) / 2)
    K = np.array([[fx, 0, W / 2 - 0.5], [0, fx, H / 2 - 0.5], [0, 0, 1.0]])
    img = np.zeros((H, W, 3), np.uint8)
    img[:] = color
    return ge.CheckCamera(name, K, R, -R @ eye, img, valid)


def two_cameras():
    return [camera("cam_A", (1.0, 1.0, 2.4), (4.0, 4.0, 0.6)), camera("cam_B", (5.0, 1.0, 2.4), (2.0, 4.0, 0.6))]


def grazing_triangle(cam, dist=2.0, cos=0.1, size=0.4):
    """카메라 앞 dist 에서 시선과 |cos| = cos 로 비스듬한(사선) 삼각형. 화면에 몇십 화소는 차지한다."""
    c = -cam.R.T @ cam.t
    d = cam.R[2]
    n0 = cam.R[0]  # 시선에 수직
    n = n0 * np.sqrt(1 - cos**2) + d * cos
    q = c + dist * d
    e1 = cam.R[1] - (cam.R[1] @ n) * n
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    return ge.Prim("sliver", "observed", np.array([q + size * e1, q - size * e1, q + size * e2]), np.array([[0, 1, 2]]), factor=np.ones(3))


def model_of(prims):
    return ge.SceneModel(prims, {})


def cell_occupied(prims, name, x, y):
    p = next(p for p in prims if p.name == name)
    c = p.pos[p.idx].mean(1)
    return bool(((np.abs(c[:, 0] - x) < 0.1) & (np.abs(c[:, 1] - y) < 0.1)).any())


# ---------------------------------------------------------------------------


def test_stretched_removed_only_when_seen_grazing():
    prims = room(strip=False)
    cam = camera("cam_A", (3.0, 3.0, 2.4), (3.0, 6.0, 1.6))
    # 카메라 중심을 지나는 수직 평면 위 삼각형(사선, |cos| = 0) + 카메라 뒤쪽 면(안 보임)
    sliver = grazing_triangle(cam)
    behind = ge.Prim("behind", "observed", np.array([[3.0, 1.0, 5.0], [3.5, 1.0, 5.0], [3.0, 1.5, 5.0]]), np.array([[0, 1, 2]]), factor=np.ones(3))
    prims += [sliver, behind]
    mesh = gf.merge_mesh(prims)
    vis = [gf.render_visibility(mesh, cam, 320)]
    drop, rec = gf.stretched_faces(mesh, vis, CFG)
    names = [p.name for p in prims]
    removed_prims = {names[k] for k in np.unique(mesh.face_prim[drop])}
    assert removed_prims == {"sliver"}  # 정상 면·안 보인 면은 지우지 않는다
    assert rec["removed_faces"] == 1 and rec["removed_area_ratio"] < 0.01


def test_floor_fill_only_holes_and_to_room_boundary():
    prims = room()
    res = gf.run_fill(model_of(prims), two_cameras(), CFG)
    assert res.new_idx == {}  # 늘어난 면 없음 -> 기존 면 불변
    fl = next(f for f in res.fills if f.role == "floor")
    c = fl.prim.pos[fl.prim.idx].mean(1)
    assert np.allclose(fl.prim.pos[:, 2], 0.0)  # World Z=0
    assert res.report["floor_level"]["z_source"] == "WORLD_Z0"
    # 채움 면은 메시가 이미 있는 칸 위에 만들지 않는다
    assert not any(cell_occupied(prims, "floor", x, y) for x, y in c[:, :2][::7])
    area = len(fl.prim.idx) / 2 * 0.05 ** 2
    hole, band = 1.4 * 1.4, 1.0 * 6.0
    assert hole + band * 0.9 < area < (hole + band) * 1.1  # 구멍 + 오른쪽 띠(방 경계 x=6 까지), 그 밖은 없다
    assert c[:, 0].max() <= 6.06 and c[:, 1].min() >= -0.06 and c[:, 1].max() <= 6.06  # 벽 바깥으로 안 나간다
    assert (c[:, 0] > 5.0).any() and ((c[:, 0] > 2) & (c[:, 0] < 3.4) & (c[:, 1] > 2) & (c[:, 1] < 3.4)).any()


def test_walls_accepted_and_fake_partition_rejected():
    prims = room(partition=True)
    mesh = gf.merge_mesh(prims)
    keep = np.ones(len(mesh.F), bool)
    planes, rejected = gf.find_wall_planes(mesh, keep, 0.0, CFG)
    ds = sorted((round(p["n"][0], 1), round(p["n"][1], 1), round(p["d"], 1)) for p in planes)
    assert ds == [(-1.0, 0.0, -6.0), (0.0, -1.0, -6.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)]  # 네 벽, n 은 방 안쪽
    assert any(r["rejected"] == "NOT_ROOM_BOUNDARY" for r in rejected)  # 방 한가운데 칸막이는 가짜 벽(바닥이 양쪽에 있다)
    assert not any(abs(p["d"] - 4.5) < 0.2 for p in planes)


def test_no_plane_without_support_in_that_direction():
    prims = [p for p in room(strip=False) if p.name != "wall_y6"]  # y=6 벽 없음
    mesh = gf.merge_mesh(prims)
    planes, _ = gf.find_wall_planes(mesh, np.ones(len(mesh.F), bool), 0.0, CFG)
    assert len(planes) == 3 and not any(abs(p["n"][1] + 1) < 0.1 for p in planes)


def test_wall_fill_only_inside_plane_range_holes():
    prims = room(strip=False)
    res = gf.run_fill(model_of(prims), two_cameras(), CFG)
    walls = [f for f in res.fills if f.role.startswith("wall_")]
    x0 = next(f for f in walls if abs(f.stats["n"][0] - 1.0) < 0.01)
    c = x0.prim.pos[x0.prim.idx].mean(1)
    assert np.allclose(x0.prim.pos[:, 0], 0.0, atol=0.02)
    area = len(x0.prim.idx) / 2 * 0.05 ** 2
    assert 0.5 < area < 1.0 * 0.8 * 1.5  # 문 크기 구멍(1.0 x 0.8 m)만(칸 5 cm 팽창 여유)
    assert c[:, 1].min() > 1.8 and c[:, 1].max() < 3.2 and c[:, 2].min() > 0.6 and c[:, 2].max() < 1.8
    assert all(f.stats["fill_m2"] < 3.0 for f in walls)


def test_fill_source_ratios_and_colors():
    prims = room(strip=False)
    # cam_B 만 방 한쪽을 본다 -> 구멍 일부는 카메라 하나에만 보여 회색(consistent 규칙)
    cams = [camera("cam_A", (1.0, 1.0, 2.4), (4.0, 4.0, 0.6), hfov=70), camera("cam_B", (5.0, 5.0, 2.4), (3.0, 3.0, 0.0), hfov=40)]
    res = gf.run_fill(model_of(prims), cams, CFG)
    fl = next(f for f in res.fills if f.role == "floor")
    proj, gray = fl.source == gf.SRC_PROJECTED, (fl.source == gf.SRC_GRAY)
    assert set(np.unique(fl.source)) <= {0, 3}
    assert (np.abs(fl.image[proj].astype(int) - PHOTO).max() <= 1)  # 투영 텍셀 색 = 사진 색
    ratio = fl.stats["projected_ratio"] + fl.stats["gray_ratio"]
    assert abs(ratio - 1.0) < 1e-3
    ss = res.report["surface_source"]
    assert abs(sum(ss["fill_ratio"].values()) - 1.0) < 1e-3 and set(ss["values"]) == {"ESTIMATED_PLANE", "UNOBSERVED"}
    assert 0.0 < ss["fill_share_of_surface"] < 0.2
    # strict 는 메시가 깊이로 확인되는 시선만 → 구멍 한가운데(메시 없음)는 전부 회색
    strict = gf.run_fill(model_of(prims), cams, replace(CFG, fill_color_mode="strict"))
    sf = next(f for f in strict.fills if f.role == "floor")
    assert sf.stats["projected_ratio"] < fl.stats["projected_ratio"]
    assert gray.any() or fl.stats["gray_ratio"] == 0.0


def test_no_camera_or_disabled_skips_fill():
    prims = room()
    with pytest.raises(ge.EnhanceSkip) as e:
        gf.run_fill(model_of(prims), [], CFG)
    assert e.value.reason == "FILL_NO_CAMERAS"


# --------------------------------------------------------------------------- GLB 통합


def _glb(prims) -> bytes:
    blob = bytearray()
    views, accs, meshes, mats, nodes = [], [], [], [], []

    def add(data: bytes, target=None) -> int:
        while len(blob) % 4:
            blob.append(0)
        v = {"buffer": 0, "byteOffset": len(blob), "byteLength": len(data)}
        if target:
            v["target"] = target
        blob.extend(data)
        views.append(v)
        return len(views) - 1

    for p in prims:
        g = np.stack([p.pos[:, 0], p.pos[:, 2], -p.pos[:, 1]], 1).astype("<f4")  # World → glTF (x, z, −y)
        accs.append({"bufferView": add(g.tobytes(), 34962), "componentType": 5126, "count": len(g), "type": "VEC3",
                     "min": g.min(0).tolist(), "max": g.max(0).tolist()})
        pa = len(accs) - 1
        accs.append({"bufferView": add(p.idx.reshape(-1).astype("<u4").tobytes(), 34963), "componentType": 5125, "count": int(p.idx.size), "type": "SCALAR"})
        mats.append({"name": p.name, "pbrMetallicRoughness": {"baseColorFactor": [*p.factor.tolist(), 1.0]}})
        meshes.append({"name": p.name, "primitives": [{"attributes": {"POSITION": pa}, "indices": len(accs) - 1, "material": len(mats) - 1}]})
        nodes.append({"name": p.name, "mesh": len(meshes) - 1})
    doc = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": list(range(len(nodes)))}], "nodes": nodes, "meshes": meshes,
           "materials": mats, "bufferViews": views, "accessors": accs, "buffers": [{"byteLength": len(blob)}],
           "extras": {"simai": {"axis_convention": "GLTF_Y_UP"}}}
    return ge.serialize_glb(ge.GlbFile(doc, blob))


def _png_pixels(g, image_index) -> np.ndarray:
    im = g.doc["images"][image_index]
    v = g.doc["bufferViews"][im["bufferView"]]
    data = bytes(g.bin[v["byteOffset"]: v["byteOffset"] + v["byteLength"]])
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, w = 8, b"", 0
    h = 0
    ct = 0
    while pos < len(data):
        ln = int.from_bytes(data[pos: pos + 4], "big")
        tag = data[pos + 4: pos + 8]
        body = data[pos + 8: pos + 8 + ln]
        if tag == b"IHDR":
            w, h, ct = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big"), body[9]
        elif tag == b"IDAT":
            idat += body
        pos += 12 + ln
    c = 1 if ct == 0 else 3
    raw = np.frombuffer(zlib.decompress(idat), np.uint8).reshape(h, 1 + w * c)
    return raw[:, 1:].reshape(h, w, c).squeeze()


def test_enhance_glb_applies_fill_with_provenance_and_preserves_original():
    raw = _glb(room())
    res = ge.enhance_glb(raw, config=CFG, cameras=two_cameras())
    assert res.glb is not None and res.report["applied"] is True
    fi = res.report["fill"]
    assert fi["applied"] and fi["floor"]["applied"] and fi["surface_source"]["fill_ratio"]["ESTIMATED_PLANE"] is not None
    assert res.report["floor"]["reason"] == "REPLACED_BY_FILL"  # 옛 바닥 사각형 대신 구멍 채움
    assert res.report["triangles_delta"] == fi["added_triangles"] - fi["removed_faces"]
    g0, g1 = ge.parse_glb(raw), ge.parse_glb(res.glb)
    assert bytes(g1.bin)[: len(g0.bin)] == bytes(g0.bin)  # 원본 데이터는 그대로, 뒤에 덧붙이기만
    assert g1.doc["meshes"][: len(g0.doc["meshes"])] == g0.doc["meshes"]  # 지운 면이 없으면 원본 프리미티브 불변
    fills = [n for n in g1.doc["nodes"] if str(n.get("name", "")).startswith("estimated_") and "fill" in n["name"]]
    assert any(n["name"] == "estimated_floor_fill" for n in fills)
    for n in fills:
        sim = n["extras"]["simai"]
        assert sim["surface_source"] == "ESTIMATED_PLANE" and sim["safety_use"] is False
        src = _png_pixels(g1, sim["source_map_image"])
        assert set(np.unique(src)) <= {0, 3}
    assert g1.doc["extras"]["simai"]["enhanced"]["safety_use"] is False
    assert g1.doc["extras"]["simai"]["axis_convention"] == "GLTF_Y_UP"
    again = ge.enhance_glb(res.glb, config=CFG, cameras=two_cameras())
    assert again.glb is None and again.report["reason"] == "ALREADY_ENHANCED"


def test_stretched_removal_rewrites_only_indices():
    prims = room(strip=False)
    cams = [camera("cam_A", (3.0, 3.0, 2.4), (3.0, 6.0, 1.6))]  # 다른 카메라가 잘 보면 사선이 아니라 지우지 않는다(규칙: 모든 시선에서)
    prims.append(grazing_triangle(cams[0]))
    raw = _glb(prims)
    res = ge.enhance_glb(raw, config=CFG, cameras=cams)
    assert res.glb is not None and res.report["fill"]["stretched"]["removed_faces"] >= 1
    g0, g1 = ge.parse_glb(raw), ge.parse_glb(res.glb)
    sl = len(prims) - 1
    acc0 = g0.doc["accessors"][g0.doc["meshes"][sl]["primitives"][0]["indices"]]
    acc1 = g1.doc["accessors"][g1.doc["meshes"][sl]["primitives"][0]["indices"]]
    assert acc0["count"] == 3 and acc1["count"] == 0  # 사선 삼각형만 지워지고 색인만 새로 쓴다
    assert bytes(g1.bin)[: len(g0.bin)] == bytes(g0.bin)  # 원본 바이트 보존(새 색인은 뒤에)
    p0, p1 = g0.doc["meshes"][0]["primitives"][0], g1.doc["meshes"][0]["primitives"][0]
    assert p0["indices"] == p1["indices"]  # 안 지운 프리미티브는 그대로


def test_fill_failure_keeps_original(monkeypatch, tmp_path):
    raw = _glb(room())
    path = tmp_path / "scene.glb"
    path.write_bytes(raw)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(gf, "run_fill", boom)
    rep = ge.enhance_file(path, config=CFG, cameras=two_cameras())
    assert rep["applied"] is False and rep["reason"].startswith("ENHANCE_ERROR")
    assert path.read_bytes() == raw  # 실패 시 원본 GLB 그대로


def test_fill_disabled_or_no_cameras_keeps_legacy_behavior():
    raw = _glb(room())
    off = ge.enhance_glb(raw, config=replace(CFG, fill_enabled=False), cameras=two_cameras())
    assert off.report["fill"] == {"applied": False, "reason": "DISABLED"}
    nocam = ge.enhance_glb(raw, config=CFG)
    assert nocam.report["fill"] == {"applied": False, "reason": "NO_CAMERAS"}
    assert nocam.report["floor"]["applied"] is True  # 옛 바닥 사각형 경로는 그대로
    cfg = ge.EnhanceConfig.from_env(env={"SIMAI_GLB_FILL": "off"})
    assert cfg.fill_enabled is False
    assert ge.EnhanceConfig.from_env(env={}, options={"fill": False}).fill_enabled is False
    assert ge.EnhanceConfig.from_env(env={"SIMAI_FILL_COS_SLANT": "0.2"}).fill_cos_slant == 0.2


def test_sidecar_atlas_written_per_plane_with_inpaint_mask(tmp_path):
    import json

    path = tmp_path / "scene.glb"
    path.write_bytes(_glb(room()))
    rep = ge.enhance_file(path, config=CFG, cameras=two_cameras())
    assert rep["applied"] and rep["fill_atlas"]["dir"] == "scene_fill"
    d = tmp_path / "scene_fill"
    doc = json.loads((d / "fill_atlas.json").read_text(encoding="utf-8"))
    assert doc["safety_use"] is False and "floor" in {p["role"] for p in doc["planes"]} and any(p["role"].startswith("wall_") for p in doc["planes"])
    fl = next(p for p in doc["planes"] if p["role"] == "floor")
    assert fl["frame"]["u_axis"] == [1.0, 0.0, 0.0] and fl["frame"]["texel_m"] <= 0.0101 and max(fl["frame"]["size_px"]) <= 4096
    for p in doc["planes"]:
        assert all((d / f).is_file() for f in p["files"].values())
    # 인페인팅 마스크 = 채움 칸 안 + 사진 근거 없음(회색), 투영된 텍셀·칸 밖은 0
    res = ge.enhance_glb(_glb(room()), config=CFG, cameras=two_cameras())
    for f in res.fills:
        m = f.inpaint_mask > 0
        assert not m[f.source == gf.SRC_PROJECTED].any() and not m[~f.region].any()
        assert (m.sum() / max(f.region.sum(), 1)) == pytest.approx(f.stats["gray_ratio"], abs=1e-3)
        assert (f.image[m] == 128).all()  # 채울 곳은 회색 자리표시
