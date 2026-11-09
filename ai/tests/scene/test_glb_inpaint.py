"""평면 구멍 인페인팅(S15P31A101-514, glb_inpaint.py) 단위·통합 시험."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import glb_fill as gf
from simai_ai.scene import glb_inpaint as gi
from simai_ai.scene.texture_provenance import ESTIMATED_PLANE, GENERATED, UNOBSERVED

from .test_glb_fill import CFG, PHOTO, _glb, _png_pixels, camera, room


def _partial_cameras():
    """구멍 일부는 한 카메라에만 보여 회색(inpaint_mask=255)으로 남고 일부는 두 카메라에 보여 투영(ESTIMATED_PLANE=3)되는 시점."""
    return [
        camera("cam_A", (1.0, 1.0, 2.4), (4.0, 4.0, 0.6), hfov=70),
        camera("cam_B", (5.0, 5.0, 2.4), (3.0, 3.0, 0.0), hfov=40),
    ]


def _prepare_fill_only(tmp_path: Path) -> tuple[Path, Path, ge.EnhanceResult]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = _glb(room(strip=False))
    res = ge.enhance_glb(raw, config=CFG, cameras=_partial_cameras())
    assert res.glb is not None and res.fills
    glb_path = tmp_path / "scene.glb"
    sidecar_dir = tmp_path / "scene_fill"
    glb_path.write_bytes(res.glb)
    gf.write_sidecar(res.fills, sidecar_dir)
    return glb_path, sidecar_dir, res


def test_observed_texels_byte_invariant_and_only_mask_becomes_generated(tmp_path: Path):
    pytest.importorskip("cv2")
    raw_orig = _glb(room(strip=False))
    glb_path, sidecar_dir, res = _prepare_fill_only(tmp_path)

    before_albedo = {}
    before_mask = {}
    before_source = {}
    for f in res.fills:
        before_albedo[f.role] = gi.decode_png((sidecar_dir / f"{f.role}_albedo.png").read_bytes()).copy()
        before_mask[f.role] = gi.decode_png((sidecar_dir / f"{f.role}_inpaint_mask.png").read_bytes()).copy()
        before_source[f.role] = gi.decode_png((sidecar_dir / f"{f.role}_surface_source.png").read_bytes()).copy()
    assert any((m > 0).any() for m in before_mask.values())

    # 가짜 백엔드: 마스크 밖(mask==0)까지 일부러 다른 색([210, 45, 180])으로 덮어써도
    # inpaint_plane 이 mask==0 바이트 불변을 강제하는지 검증한다.
    fill_color = np.array([210, 45, 180], dtype=np.uint8)

    def fake_backend(albedo: np.ndarray, mask: np.ndarray, surface_source: np.ndarray | None = None, *, role: str = "floor", config: gi.InpaintConfig | None = None) -> np.ndarray:
        out = np.empty_like(albedo)
        out[:] = fill_color
        return out

    rep = gi.inpaint_sidecar_to_glb(
        glb_path,
        sidecar_dir,
        config=gi.InpaintConfig(mode="classic"),
        classic_fn=fake_backend,
        fill_report=res.report["fill"],
    )
    assert rep["applied"] is True and rep["backend_used"] == "classic"

    g_orig = ge.parse_glb(raw_orig)
    g_after = ge.parse_glb(glb_path.read_bytes())
    # 원본 GLB 바이너리 접두사 보존
    assert bytes(g_after.bin)[: len(g_orig.bin)] == bytes(g_orig.bin)

    atlas = json.loads((sidecar_dir / "fill_atlas.json").read_text(encoding="utf-8"))
    assert atlas["source_values"] == {
        "ESTIMATED_PLANE": ESTIMATED_PLANE,
        "GENERATED": GENERATED,
        "UNOBSERVED": UNOBSERVED,
    }

    for p in atlas["planes"]:
        role = p["role"]
        alb_before = before_albedo[role]
        msk = before_mask[role] > 0
        src_before = before_source[role]

        alb_after = gi.decode_png((sidecar_dir / f"{role}_inpainted.png").read_bytes())
        src_after = gi.decode_png((sidecar_dir / f"{role}_surface_source.png").read_bytes())

        # 1) 관측·마스크 밖(mask == 0) 텍셀은 바이트 단위로 완전히 동일해야 한다
        assert alb_after[~msk].tobytes() == alb_before[~msk].tobytes()
        assert src_after[~msk].tobytes() == src_before[~msk].tobytes()

        # 2) 마스크 안(mask == 255)만 새 색으로 바뀌고 출처가 GENERATED(4)로 기록된다
        if msk.any():
            assert (alb_after[msk] == fill_color).all()
            assert (src_after[msk] == GENERATED).all()
            assert p["texel_ratios"]["GENERATED"] > 0.0
            assert p["texel_ratios"]["UNOBSERVED"] == 0.0
        assert sum(p["texel_ratios"].values()) == pytest.approx(1.0, abs=1e-3)

        # 3) GLB 내부 텍스처·출처 지도·노드 메타데이터도 사이드카와 일치해야 한다
        node = next(n for n in g_after.doc["nodes"] if n.get("name") == f"estimated_{role}_fill")
        sim = node["extras"]["simai"]
        assert sim["safety_use"] is False
        assert sim["source_values"]["GENERATED"] == GENERATED
        mat = g_after.doc["materials"][g_after.doc["meshes"][node["mesh"]]["primitives"][0]["material"]]
        tex_idx = mat["pbrMetallicRoughness"]["baseColorTexture"]["index"]
        alb_img_idx = g_after.doc["textures"][tex_idx]["source"]
        glb_alb = _png_pixels(g_after, alb_img_idx)
        glb_src = _png_pixels(g_after, sim["source_map_image"])
        assert np.array_equal(glb_alb, alb_after)
        assert np.array_equal(glb_src, src_after)
        assert set(np.unique(glb_src)) <= {UNOBSERVED, ESTIMATED_PLANE, GENERATED}

    ss = res.report["fill"]["surface_source"]
    assert ss["values"]["GENERATED"] == GENERATED
    assert ss["fill_ratio"]["GENERATED"] > 0.0
    assert ss["fill_ratio"]["UNOBSERVED"] == 0.0
    assert sum(ss["fill_ratio"].values()) == pytest.approx(1.0, abs=1e-3)


def test_fallback_chain_diffusion_to_classic_to_gray(tmp_path: Path):
    pytest.importorskip("cv2")
    # 1) diffusion 실패 -> classic 폴백
    glb1, side1, res1 = _prepare_fill_only(tmp_path / "case1")

    def fail_diffusion(*a, **k):
        raise RuntimeError("diffusers not installed")

    def ok_classic(albedo: np.ndarray, mask: np.ndarray, surface_source: np.ndarray | None = None, *, role: str = "floor", config: gi.InpaintConfig | None = None) -> np.ndarray:
        out = albedo.copy()
        out[mask > 0] = [100, 150, 200]
        return out

    rep1 = gi.inpaint_sidecar_to_glb(
        glb1,
        side1,
        config=gi.InpaintConfig(mode="diffusion"),
        diffusion_fn=fail_diffusion,
        classic_fn=ok_classic,
        fill_report=res1.report["fill"],
    )
    assert rep1["applied"] is True
    assert rep1["backend_used"] == "classic"
    assert any(p.get("fallback_from") == "diffusion" for p in rep1["planes"].values() if p["masked_texels"] > 0)

    # 2) diffusion 실패 + classic 실패 -> 중립 회색(128, UNOBSERVED=0) 유지 폴백
    glb2, side2, res2 = _prepare_fill_only(tmp_path / "case2")
    before_bytes = glb2.read_bytes()

    def fail_classic(*a, **k):
        raise RuntimeError("classic failed")

    rep2 = gi.inpaint_sidecar_to_glb(
        glb2,
        side2,
        config=gi.InpaintConfig(mode="diffusion"),
        diffusion_fn=fail_diffusion,
        classic_fn=fail_classic,
        fill_report=res2.report["fill"],
    )
    assert rep2["applied"] is False
    assert rep2["backend_used"] == "gray_fallback"
    assert any(p.get("fallback_from") == "diffusion->classic" for p in rep2["planes"].values() if p["masked_texels"] > 0)
    assert glb2.read_bytes() == before_bytes  # 폴백 시 GLB 회색 원본 그대로 유지
    for f in res2.fills:
        msk = gi.decode_png((side2 / f"{f.role}_inpaint_mask.png").read_bytes()) > 0
        alb = gi.decode_png((side2 / f"{f.role}_inpainted.png").read_bytes())
        src = gi.decode_png((side2 / f"{f.role}_surface_source.png").read_bytes())
        if msk.any():
            assert (alb[msk] == 128).all()
            assert (src[msk] == UNOBSERVED).all()


def test_inpaint_disabled_keeps_gray_and_provenance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pytest.importorskip("cv2")
    path = tmp_path / "scene.glb"
    path.write_bytes(_glb(room(strip=False)))
    monkeypatch.setenv("SIMAI_GLB_INPAINT", "off")
    rep = ge.enhance_file(path, config=CFG, cameras=_partial_cameras())
    assert rep["applied"] is True
    assert rep["inpaint"]["applied"] is False and rep["inpaint"]["reason"] == "DISABLED"

    g = ge.parse_glb(path.read_bytes())
    fills = [n for n in g.doc["nodes"] if str(n.get("name", "")).startswith("estimated_") and "fill" in n["name"]]
    for n in fills:
        src = _png_pixels(g, n["extras"]["simai"]["source_map_image"])
        assert set(np.unique(src)) <= {UNOBSERVED, ESTIMATED_PLANE}


def test_large_plane_tiling_seam_continuity():
    # 4096 px 상한 타일 분할 점검: (y0, y1, x0, x1) 형식
    boxes_4k = gi.tile_boxes(4096, 4096, 512, 64)
    assert len(boxes_4k) == 81
    assert boxes_4k[0] == (0, 512, 0, 512) and boxes_4k[-1] == (3584, 4096, 3584, 4096)

    # 1200 x 900 평면에서 타일 경계를 가로지르는 큰 마스크가 매끄럽게 블렌딩되는지 확인
    H, W = 900, 1200
    albedo = np.full((H, W, 3), PHOTO, dtype=np.uint8)
    mask = np.zeros((H, W), dtype=np.uint8)
    mask[100:800, 150:1050] = 255  # 여러 512x512 타일 경계를 가로지름
    albedo[mask > 0] = 128

    calls: list[tuple[int, int]] = []

    def tile_offset_backend(tile_alb: np.ndarray, tile_m: np.ndarray, tile_idx: int) -> np.ndarray:
        calls.append(tile_alb.shape[:2])
        # 타일마다 미세한 편차(최대 16)가 있어도 겹침 코사인 블렌딩으로 경계 단차가 완화되는지 확인
        _, xx = np.mgrid[: tile_alb.shape[0], : tile_alb.shape[1]]
        out = tile_alb.copy()
        val = np.clip(140 + (tile_idx % 3) * 8 + (xx / max(tile_alb.shape[1], 1)) * 10, 0, 255).astype(np.uint8)
        out[:, :, 0] = val
        out[:, :, 1] = val
        out[:, :, 2] = val
        return out

    blended, used_tiles = gi.inpaint_tiled(albedo, mask, tile_offset_backend, tile_size=512, overlap=128)
    assert used_tiles >= 6 and len(calls) >= 6
    # 관측 영역(mask == 0)은 바이트 불변
    assert np.array_equal(blended[mask == 0], albedo[mask == 0])
    # 타일 경계 내부에서 인접 화소 간 급격한 단차(jump)가 없어야 한다
    roi = blended[200:700, 200:1000, 0].astype(np.float32)
    dx = np.abs(np.diff(roi, axis=1)).max()
    dy = np.abs(np.diff(roi, axis=0)).max()
    assert dx <= 2.0 and dy <= 2.0


def test_classic_backend_inpainting_fills_hole_from_observed_pattern(tmp_path: Path):
    pytest.importorskip("cv2")
    # 줄무늬·체커 패턴이 있는 관측 바닥 한가운데 구멍을 classic 백엔드가 주변 패턴으로 자연스럽게 메우는지 시험
    H, W = 160, 160
    yy, xx = np.mgrid[:H, :W]
    pattern = np.zeros((H, W, 3), dtype=np.uint8)
    stripe = ((xx // 8 + yy // 8) % 2) == 0
    pattern[stripe] = [180, 140, 95]
    pattern[~stripe] = [150, 115, 75]

    region = np.ones((H, W), dtype=bool)
    region[:10, :] = False  # 평면 밖 영역(회색 128이지만 inpaint 대상 아님)
    mask = np.zeros((H, W), dtype=bool)
    mask[50:110, 50:110] = True

    albedo = pattern.copy()
    albedo[~region] = 128
    albedo[mask] = 128
    source = np.full((H, W), ESTIMATED_PLANE, dtype=np.uint8)
    source[~region] = UNOBSERVED
    source[mask] = UNOBSERVED

    res = gi.inpaint_plane(
        albedo,
        mask.astype(np.uint8) * 255,
        source,
        role="floor",
        fill_m2=2.5,
        config=gi.InpaintConfig(mode="classic", patch_size=16),
    )
    assert res.applied is True and res.backend_used == "classic"
    # 관측 영역과 평면 밖 영역(mask == 0)은 바이트 불변
    assert np.array_equal(res.image[~mask], albedo[~mask])
    # 마스크 내부가 회색(128)에서 벗어나 관측 바닥 색 범위(갈색 계열, R > G > B)로 채워졌는지 확인
    filled = res.image[mask]
    assert not (filled == 128).all()
    mean_rgb = filled.mean(axis=0)
    assert mean_rgb[0] > mean_rgb[1] > mean_rgb[2]
    assert 145 <= mean_rgb[0] <= 185 and 110 <= mean_rgb[1] <= 145 and 70 <= mean_rgb[2] <= 100
    assert (res.source[mask] == GENERATED).all()
    assert (res.source[~mask] == source[~mask]).all()

    # 양자화(8bit) 설정 및 명시적 파이프라인 해제(unload_diffusion_pipeline) 동작 확인
    cfg_q = gi.InpaintConfig.from_env({"SIMAI_GLB_INPAINT": "diffusion", "SIMAI_INPAINT_QUANTIZATION": "8bit"})
    assert cfg_q.mode == "diffusion"
    assert cfg_q.quantization == "8bit"
    gi.unload_diffusion_pipeline()
