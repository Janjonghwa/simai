#!/usr/bin/env python3
"""실험 이미지(exp02 HMR·mapanything) GPU 점검 (S15P31A101-207). 세션 큐 g0·m0 항목이 그 이미지의 Pod 에서 돌린다.

노트북의 check_image.sh(GPU 없이 이미지 내용)와 짝이다. 여기서는 이 Pod 의 GPU 에서 실제로 도는지만 본다:
  - torch CUDA 사용 가능, 이 GPU 세대(sm_XY)의 커널이 torch 휠에 있음(같은 주 버전의 낮은 부 버전 SASS 또는 낮은 PTX 도 됨)
  - GPU 행렬곱(fp16·bf16) 결과가 유한
  - exp02: Detectron2 CUDA 확장이 이 세대용으로 빌드됨(.detectron2_arch) + 확장 커널 1회(box_iou_rotated), pyrender EGL 오프스크린 렌더
  - mapanything: 패키지·DINOv2 허브 코드 사본, bf16 autocast
결과 JSON 을 표준 출력에 쓰고, 하나라도 실패하면 종료 코드 1 (모르면 통과로 두지 않는다: 아키텍처를 못 읽으면 실패).

    python deploy/scripts/exp_image_check.py            # 이미지의 SIMAI_IMAGE_KIND 로 종류를 고른다
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


def arch_ok(cap: tuple[int, int], arch_list: list[str]) -> str | None:
    """이 GPU(cap)에서 돌 커널이 목록에 있으면 그 항목, 없으면 None.
    SASS sm_XY 는 같은 주 버전 X 의 부 버전 Y 이상에서 돈다(CUDA 호환 규칙). PTX compute_XY 는 XY 이하 모든 세대에서 JIT 된다."""
    major, minor = cap
    sass, ptx = [], []
    for a in arch_list:
        m = re.fullmatch(r"(sm|compute)_(\d+)(\d)", a.strip())
        if m is None:
            continue
        kind, a_major, a_minor = m.group(1), int(m.group(2)), int(m.group(3))
        if kind == "sm" and a_major == major and a_minor <= minor:
            sass.append((a_minor, a))
        elif kind == "compute" and (a_major, a_minor) <= (major, minor):
            ptx.append(((a_major, a_minor), a))
    if sass:
        return max(sass)[1]  # 가장 가까운 SASS (8.9 GPU 에 8.0·8.6 이 있으면 8.6)
    return max(ptx)[1] if ptx else None


def detectron2_arch_list(text: str) -> list[str]:
    """TORCH_CUDA_ARCH_LIST 형식('8.0;8.6;9.0+PTX') → ['sm_80', 'sm_86', 'sm_90', 'compute_90']."""
    out = []
    for item in re.split(r"[;\s]+", text.strip()):
        m = re.fullmatch(r"(\d+)\.(\d)(\+PTX)?", item)
        if m is None:
            continue
        out.append(f"sm_{m.group(1)}{m.group(2)}")
        if m.group(3):
            out.append(f"compute_{m.group(1)}{m.group(2)}")
    return out


def run_checks(kind: str) -> dict:
    out: dict = {"kind": kind, "checks": []}

    def check(name: str, fn) -> None:
        try:
            out["checks"].append({"name": name, "ok": True, "value": fn()})
        except Exception as e:  # noqa: BLE001 - 점검 하나의 실패는 기록하고 다음 점검으로
            out["checks"].append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"})

    import torch

    def cuda():
        assert torch.cuda.is_available(), "torch.cuda.is_available() 가 False"
        cap = torch.cuda.get_device_capability(0)
        return {"torch": torch.__version__, "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(0),
                "capability": f"{cap[0]}.{cap[1]}"}

    check("cuda", cuda)
    if not out["checks"][-1]["ok"]:
        return out

    cap = torch.cuda.get_device_capability(0)

    def torch_arch():
        arch = torch.cuda.get_arch_list()
        hit = arch_ok(cap, arch)
        assert hit, f"sm_{cap[0]}{cap[1]} 커널이 torch 휠에 없다: {arch}"
        return {"match": hit, "arch_list": arch}

    def matmul():
        res = {}
        for dt in (torch.float16, torch.bfloat16):
            a = torch.randn(512, 512, device="cuda", dtype=dt)
            c = a @ a.T
            torch.cuda.synchronize()
            assert torch.isfinite(c).all().item(), f"{dt} 결과에 NaN·Inf"
            res[str(dt)] = "ok"
        return res

    check("torch_arch", torch_arch)
    check("matmul", matmul)

    if kind == "exp02":
        def d2_arch():
            text = Path("/opt/exp02/.detectron2_arch").read_text(encoding="utf-8")
            hit = arch_ok(cap, detectron2_arch_list(text))
            assert hit, f"Detectron2 확장이 sm_{cap[0]}{cap[1]} 용으로 빌드되지 않았다: {text.strip()}"
            return {"match": hit, "built": text.strip()}

        def d2_kernel():
            from detectron2.layers.rotated_boxes import pairwise_iou_rotated
            boxes = torch.tensor([[10.0, 10.0, 4.0, 4.0, 0.0], [10.0, 10.0, 4.0, 4.0, 90.0]], device="cuda")
            iou = pairwise_iou_rotated(boxes, boxes)
            torch.cuda.synchronize()
            assert iou.is_cuda and abs(float(iou[0, 1]) - 1.0) < 1e-3, f"box_iou_rotated 결과 이상: {iou.tolist()}"
            return {"iou_0_1": round(float(iou[0, 1]), 4)}

        def egl_render():
            os.environ.setdefault("PYOPENGL_PLATFORM", "egl")
            import numpy as np
            import pyrender
            import trimesh
            scene = pyrender.Scene()
            scene.add(pyrender.Mesh.from_trimesh(trimesh.creation.box()))
            scene.add(pyrender.PerspectiveCamera(yfov=1.0), pose=np.array(
                [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 3], [0, 0, 0, 1]], dtype=float))
            r = pyrender.OffscreenRenderer(64, 64)
            try:
                color, _depth = r.render(scene)
            finally:
                r.delete()
            return {"shape": list(color.shape)}

        check("detectron2_arch", d2_arch)
        check("detectron2_kernel", d2_kernel)
        check("pyrender_egl", egl_render)
    elif kind == "mapanything":
        def ma():
            import importlib.metadata as m

            from mapanything.models import MapAnything  # noqa: F401
            hub = Path.home() / ".cache" / "torch" / "hub" / "facebookresearch_dinov2_main" / "hubconf.py"
            assert hub.is_file(), f"{hub} 없음(DINOv2 허브 코드 사본)"
            with torch.autocast("cuda", dtype=torch.bfloat16):
                y = torch.nn.functional.scaled_dot_product_attention(*(torch.randn(1, 4, 64, 32, device="cuda"),) * 3)
            assert torch.isfinite(y).all().item()
            return {"mapanything": m.version("mapanything"), "commit": os.environ.get("SIMAI_MAPANYTHING_COMMIT", "")[:7]}

        check("mapanything", ma)
    else:
        out["checks"].append({"name": "kind", "ok": False, "error": f"알 수 없는 SIMAI_IMAGE_KIND={kind!r}"})
    return out


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    kind = (argv[0] if argv else None) or os.environ.get("SIMAI_IMAGE_KIND", "")
    out = run_checks(kind)
    out["ok"] = bool(out["checks"]) and all(c["ok"] for c in out["checks"])
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
