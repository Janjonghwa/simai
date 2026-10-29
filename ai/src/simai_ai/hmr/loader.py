"""실험·상시 루프가 공유하는 공식 Fast SAM 3D Body 로더. 검출기·FOV는 적재하지 않는다."""

from pathlib import Path
import os
import sys


def load_model(checkpoint: Path, *, device: str = "cuda"):
    repo = os.environ.get("SIMAI_FS3DB_REPO")
    if repo and repo not in sys.path:
        sys.path.insert(0, repo)  # 이미지에 고정된 third_party 사본, experiments는 가져오지 않는다
    from sam_3d_body import load_sam_3d_body

    return load_sam_3d_body(str(checkpoint / "model.ckpt"), device=device,
                            mhr_path=str(checkpoint / "assets" / "mhr_model.pt"))


def prepare_checkpoint(root: Path, repo: Path) -> Path:
    """실험과 같은 Fast config를 가중치 링크 옆에 둔다(가중치 복사 없음)."""
    import shutil
    source = root / "weights/sam-3d-body-dinov3"
    target = root / "weights/sam-3d-body-dinov3-fastcfg"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("model.ckpt", "assets"):
        link = target / name
        if not link.exists():
            link.symlink_to(source / name, target_is_directory=name == "assets")
    shutil.copyfile(repo / "checkpoints/sam-3d-body-dinov3/model_config.yaml", target / "model_config.yaml")
    return target
