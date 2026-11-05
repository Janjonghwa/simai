"""12차 실제 맵(current-scene-textured.glb)에서 -511(회색) 대 -514(classic / diffusion 인페인팅) 비교 실험.

노트북(CPU) 실행:
    cd ai && uv run python -m experiments.texrecon_opt.inpaint_real

GPU 세션(diffusion 포함) 실행:
    uv pip install diffusers transformers accelerate safetensors
    cd ai && SIMAI_GLB_INPAINT=diffusion uv run python -m experiments.texrecon_opt.inpaint_real --include-diffusion
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
from fill_real import label, render, strip_enhanced  # noqa: E402

import cv2
import numpy as np

from simai_ai.scene import glb_enhance as ge
from simai_ai.scene import glb_fill as gf
from simai_ai.scene import glb_inpaint as gi

BASELINE_GLB, VIEWS, load_cameras, lookat_camera = C.BASELINE_GLB, C.VIEWS, C.load_cameras, C.lookat_camera

DEFAULT_OUT = Path("C:/Users/SSAFY/simai-local/plane-inpaint")


def _colorize_source(src: np.ndarray) -> np.ndarray:
    """출처 지도(0=UNOBSERVED 어두운 회색, 3=ESTIMATED_PLANE 초록, 4=GENERATED 주황) 시각화."""
    vis = np.full((*src.shape[:2], 3), 55, dtype=np.uint8)
    vis[src == 3] = [60, 190, 90]   # ESTIMATED_PLANE (사진 투영)
    vis[src == 4] = [240, 150, 50]  # GENERATED (인페인팅 생성)
    return vis


def _side_by_side(imgs: list[np.ndarray], labels: list[str], out_path: Path) -> None:
    h = max(im.shape[0] for im in imgs)
    panels = []
    for im, text in zip(imgs, labels):
        if im.shape[0] != h:
            w_new = max(1, round(im.shape[1] * (h / im.shape[0])))
            im = cv2.resize(im, (w_new, h), interpolation=cv2.INTER_AREA)
        panels.append(label(im, text))
    cat = np.concatenate(panels, axis=1)
    cv2.imwrite(str(out_path), cat[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="12차 실제 맵 평면 구멍 인페인팅(-514) 비교")
    ap.add_argument("--in-glb", type=Path, default=BASELINE_GLB)
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--include-diffusion", action="store_true", help="GPU 세션에서 diffusion 백엔드도 함께 돌려 4단 비교 생성")
    ap.add_argument("--diffusion-model", type=str, default=None)
    args = ap.parse_args(argv)

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_before = strip_enhanced(args.in_glb.read_bytes())
    cams = load_cameras()
    cfg = ge.EnhanceConfig()

    t0 = time.perf_counter()
    res_511 = ge.enhance_glb(raw_before, config=cfg, cameras=cams)
    s_511 = round(time.perf_counter() - t0, 3)
    if res_511.glb is None:
        raise RuntimeError(f"-511 보강 불채택: {res_511.report.get('reason')}")

    glb_before_path = out_dir / "scene_before.glb"
    glb_511_path = out_dir / "scene_511_gray.glb"
    glb_classic_path = out_dir / "scene_514_classic.glb"
    sidecar_classic = out_dir / "scene_514_classic_fill"

    glb_before_path.write_bytes(raw_before)
    glb_511_path.write_bytes(res_511.glb)
    shutil.copyfile(glb_511_path, glb_classic_path)
    gf.write_sidecar(res_511.fills, sidecar_classic)

    gray_albedos = {f.role: f.image.copy() for f in res_511.fills}
    inpaint_masks = {f.role: f.inpaint_mask.copy() for f in res_511.fills}

    # -514 classic 인페인팅 실행
    fill_report_classic = json.loads(json.dumps(res_511.report["fill"]))
    inp_cfg_classic = gi.InpaintConfig.from_env(options={"inpaint": "classic"})
    rep_classic = gi.inpaint_sidecar_to_glb(
        glb_classic_path,
        sidecar_classic,
        config=inp_cfg_classic,
        fill_report=fill_report_classic,
    )

    # 보정 카메라 4대 RENDER_MAE 채택 검사(classic 인페인팅 후에도 사진과 어긋나지 않는지 확인)
    g_orig = ge.parse_glb(raw_before)
    m_orig = ge.load_scene(g_orig)
    g_cls_nolock = ge.parse_glb(strip_enhanced(glb_classic_path.read_bytes()))
    m_cls = ge.load_scene(g_cls_nolock)
    mae_check_classic = ge.run_check(g_cls_nolock, m_cls, m_orig.prims, m_cls.prims, cams, cfg)

    # 평면별 텍스처 비교 이미지 저장: [-511 회색 | 인페인팅 마스크 | classic 인페인팅 | 출처 지도(초록=3, 주황=4)]
    for f in res_511.fills:
        role = f.role
        alb_gray = gray_albedos[role]
        msk_u8 = inpaint_masks[role]
        msk_rgb = np.repeat(msk_u8[:, :, None], 3, axis=2)
        alb_cls = gi.decode_png((sidecar_classic / f"{role}_inpainted.png").read_bytes())
        src_cls = gi.decode_png((sidecar_classic / f"{role}_surface_source.png").read_bytes())
        src_vis = _colorize_source(src_cls)
        assert np.array_equal(alb_cls[msk_u8 == 0], alb_gray[msk_u8 == 0]), f"{role}: mask==0 텍셀 변경됨"
        _side_by_side(
            [alb_gray, msk_rgb, alb_cls, src_vis],
            [f"{role}: -511 gray", f"{role}: mask(255=inpaint)", f"{role}: -514 classic", f"{role}: source(G=3,O=4)"],
            out_dir / f"plane_{role}_compare.jpg",
        )

    # 선택: GPU diffusion 실행
    rep_diff = None
    glb_diff_bytes = None
    if args.include_diffusion:
        glb_diff_path = out_dir / "scene_514_diffusion.glb"
        sidecar_diff = out_dir / "scene_514_diffusion_fill"
        shutil.copyfile(glb_511_path, glb_diff_path)
        gf.write_sidecar(res_511.fills, sidecar_diff)
        fill_report_diff = json.loads(json.dumps(res_511.report["fill"]))
        opts_diff = {"inpaint": "diffusion"}
        if args.diffusion_model:
            opts_diff["inpaint_model_id"] = args.diffusion_model
        inp_cfg_diff = gi.InpaintConfig.from_env(options=opts_diff)
        rep_diff = gi.inpaint_sidecar_to_glb(
            glb_diff_path,
            sidecar_diff,
            config=inp_cfg_diff,
            fill_report=fill_report_diff,
        )
        glb_diff_bytes = glb_diff_path.read_bytes()

    # 자유 시점 4곳(top, obl1, obl2, obl3) 렌더 비교: [보정 전 | -511 회색 | classic 인페인팅 (| diffusion)]
    classic_bytes = glb_classic_path.read_bytes()
    for vn, (e, tg, fov) in VIEWS.items():
        cam = lookat_camera(e, tg, hfov_deg=fov, name=vn)
        r_before = render(raw_before, cam)
        r_511 = render(res_511.glb, cam)
        r_cls = render(classic_bytes, cam)
        cv2.imwrite(str(out_dir / f"free_{vn}_classic.jpg"), r_cls[:, :, ::-1], [cv2.IMWRITE_JPEG_QUALITY, 90])
        imgs = [r_before, r_511, r_cls]
        labels = [f"{vn}: before", f"{vn}: -511 gray", f"{vn}: -514 classic inpaint"]
        if glb_diff_bytes is not None:
            r_diff = render(glb_diff_bytes, cam)
            imgs.append(r_diff)
            labels.append(f"{vn}: -514 diffusion")
        _side_by_side(imgs, labels, out_dir / f"compare_{vn}.jpg")

    # 보정 카메라 4대 시점 비교
    for c in cams:
        cid = c.camera_id
        r_before = render(raw_before, c, long_side=960)
        r_511 = render(res_511.glb, c, long_side=960)
        r_cls = render(classic_bytes, c, long_side=960)
        H, W = r_before.shape[:2]
        photo = cv2.resize(c.image, (W, H), interpolation=cv2.INTER_AREA)
        imgs = [photo, r_before, r_511, r_cls]
        labels = [f"{cid}: photo", f"{cid}: before", f"{cid}: -511 gray", f"{cid}: -514 classic"]
        if glb_diff_bytes is not None:
            imgs.append(render(glb_diff_bytes, c, long_side=960))
            labels.append(f"{cid}: -514 diffusion")
        _side_by_side(imgs, labels, out_dir / f"compare_cam_{cid}.jpg")

    summary = {
        "input_glb": str(args.in_glb),
        "out_dir": str(out_dir),
        "fill_511_seconds": s_511,
        "fill_511_surface_source": res_511.report["fill"]["surface_source"],
        "inpaint_classic": rep_classic,
        "fill_after_classic_surface_source": fill_report_classic["surface_source"],
        "mae_check_classic": mae_check_classic,
        "inpaint_diffusion": rep_diff,
        "comparison_images": sorted(p.name for p in out_dir.glob("*.jpg")),
    }
    (out_dir / "inpaint_real.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "classic_applied": rep_classic["applied"],
        "classic_backend": rep_classic["backend_used"],
        "classic_seconds": rep_classic["seconds"],
        "fill_ratio_before": res_511.report["fill"]["surface_source"]["fill_ratio"],
        "fill_ratio_after": fill_report_classic["surface_source"]["fill_ratio"],
        "mae_check_accepted": mae_check_classic.get("accepted"),
        "mae_mean_before": mae_check_classic.get("mae_mean_before"),
        "mae_mean_after": mae_check_classic.get("mae_mean_after"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
