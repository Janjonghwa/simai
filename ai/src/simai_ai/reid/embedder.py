"""Re-ID 임베더 인터페이스와 COPE(CVPR 2026) 백엔드 (S15P31A101-463).

규칙:
- COPE 저장소(`SIMAI_COPE_ROOT`)는 LICENSE 파일이 없으므로 저장소에 코드를 복사하지 않고 외부 경로로 참조한다.
- 가중치(`SIMAI_COPE_WEIGHTS`)와 CLIP(`SIMAI_COPE_CLIP_PATH`)은 로드 전에 SHA-256 해시를 검사하며,
  자동 네트워크 다운로드는 금지한다.
- GPU(`cuda`)가 있으면 우선 쓰고, 없으면 CPU(`cpu`)로 대체한다.
- 입력 사람 crop 은 256×128(H×W)로 맞춰 배치 추론하고, 출력은 L2 정규화된 float32 벡터다.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import os
import sys
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_COPE_ROOT = "C:/Users/SSAFY/simai-local/reid/cope"
DEFAULT_COPE_WEIGHTS = "C:/Users/SSAFY/simai-local/reid/weights/cope_msmt17.pth"
DEFAULT_CLIP_PATH = "C:/Users/SSAFY/simai-local/reid/clip-cache/ViT-B-16.pt"
DEFAULT_COPE_CONFIG_REL = "configs/MSMT17/cope.yml"

DEFAULT_COPE_WEIGHTS_SHA256 = "3cf24e2e542b0465c9e0ad7a0804eb2ae80a16815b3441142a923c29b0e42e9b"
DEFAULT_CLIP_VIT_B_16_SHA256 = "5806e77cd80f8b59890b7e101eabd078d9fb84e6937f9e85e4ecb61988df416f"

COPE_EMBEDDING_DIM = 1280


class WeightVerificationError(ValueError):
    """가중치 파일의 SHA-256 해시가 기대값과 다를 때 발생한다."""


def compute_file_sha256(path: Path | str) -> str:
    """파일의 SHA-256 16진수 문자열(소문자)을 계산한다."""
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError(f"가중치 파일이 없습니다: {file_path}")
    digest = hashlib.sha256()
    with open(file_path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def verify_file_sha256(path: Path | str, expected_sha256: str | None) -> str:
    """가중치 파일 존재와 SHA-256 해시를 검증하고 소문자 해시를 돌려준다."""
    actual = compute_file_sha256(path)
    if expected_sha256 is not None:
        normalized = expected_sha256.strip().lower()
        if normalized.startswith("sha256:"):
            normalized = normalized[len("sha256:") :]
        if actual != normalized:
            raise WeightVerificationError(
                f"가중치 SHA-256 불일치 ({path}): 기대={normalized}, 실제={actual}"
            )
    return actual


def l2_normalize_rows(vectors: Any, eps: float = 1e-12) -> Any:
    """2차원 배열(N, D)의 각 행을 L2 정규화한다. 영벡터 행은 0으로 유지한다."""
    import numpy as np

    arr = np.asarray(vectors, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    if arr.shape[0] == 0:
        return arr.astype(np.float32, copy=False)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    safe_norms = np.where(norms > eps, norms, 1.0)
    out = np.where(norms > eps, arr / safe_norms, 0.0)
    return out.astype(np.float32, copy=False)


class Embedder(ABC):
    """사람 crop 목록을 받아 L2 정규화된 특징 벡터(N, D)를 반환하는 인터페이스."""

    @property
    @abstractmethod
    def embedding_dim(self) -> int:
        """임베딩 차원 수(D)."""

    @property
    @abstractmethod
    def device(self) -> str:
        """추론 디바이스 문자열(예: 'cuda', 'cpu')."""

    @abstractmethod
    def embed(self, crops: Sequence[Any]) -> Any:
        """사람 crop 목록을 받아 shape (N, D), dtype float32 의 L2 정규화 행렬을 반환한다."""


@dataclass(frozen=True)
class COPEConfig:
    """COPE 외형 임베더 설정."""

    cope_root: Path = Path(DEFAULT_COPE_ROOT)
    weights_path: Path = Path(DEFAULT_COPE_WEIGHTS)
    clip_path: Path = Path(DEFAULT_CLIP_PATH)
    config_rel_path: str = DEFAULT_COPE_CONFIG_REL
    weights_sha256: str | None = DEFAULT_COPE_WEIGHTS_SHA256
    clip_sha256: str | None = DEFAULT_CLIP_VIT_B_16_SHA256
    device: str = "auto"
    batch_size: int = 16
    input_size_hw: tuple[int, int] = (256, 128)
    pixel_mean: tuple[float, float, float] = (0.5, 0.5, 0.5)
    pixel_std: tuple[float, float, float] = (0.5, 0.5, 0.5)
    input_format: str = "bgr"

    @classmethod
    def from_env(cls, **overrides: Any) -> COPEConfig:
        """환경변수(`SIMAI_COPE_ROOT`, `SIMAI_COPE_WEIGHTS`, `SIMAI_COPE_CLIP_PATH`)를 읽어 설정을 만든다."""
        cope_root = Path(os.environ.get("SIMAI_COPE_ROOT", DEFAULT_COPE_ROOT))
        weights_path = Path(os.environ.get("SIMAI_COPE_WEIGHTS", DEFAULT_COPE_WEIGHTS))
        clip_env = (
            os.environ.get("SIMAI_COPE_CLIP_PATH")
            or os.environ.get("SIMAI_CLIP_PATH")
            or os.environ.get("SIMAI_COPE_CLIP")
            or DEFAULT_CLIP_PATH
        )
        clip_path = Path(clip_env)
        weights_sha = os.environ.get("SIMAI_COPE_WEIGHTS_SHA256", DEFAULT_COPE_WEIGHTS_SHA256)
        clip_sha = os.environ.get("SIMAI_COPE_CLIP_SHA256", DEFAULT_CLIP_VIT_B_16_SHA256)
        kwargs: dict[str, Any] = {
            "cope_root": cope_root,
            "weights_path": weights_path,
            "clip_path": clip_path,
            "weights_sha256": weights_sha,
            "clip_sha256": clip_sha,
        }
        kwargs.update(overrides)
        return cls(**kwargs)


def resolve_torch_device(requested: str = "auto") -> str:
    """'auto' 또는 요청 디바이스를 검사해 'cuda' 우선, 불가 시 'cpu'를 돌려준다."""
    import torch

    req = (requested or "auto").strip().lower()
    if req == "auto":
        return "cuda" if torch.cuda.is_available() else "cpu"
    if req.startswith("cuda") and not torch.cuda.is_available():
        return "cpu"
    return req


def preprocess_person_crop(
    crop: Any,
    size_hw: tuple[int, int] = (256, 128),
    pixel_mean: tuple[float, float, float] = (0.5, 0.5, 0.5),
    pixel_std: tuple[float, float, float] = (0.5, 0.5, 0.5),
    input_format: str = "bgr",
) -> Any | None:
    """단일 사람 crop(BGR/RGB ndarray 또는 PIL.Image)을 (3, H, W) float32 정규화 텐서용 배열로 변환한다.

    유효하지 않은 빈 crop 이면 None 을 돌려준다.
    """
    import numpy as np
    from PIL import Image

    target_h, target_w = int(size_hw[0]), int(size_hw[1])
    if crop is None:
        return None

    if isinstance(crop, Image.Image):
        if crop.width < 2 or crop.height < 2:
            return None
        pil_img = crop.convert("RGB")
    else:
        arr = np.asarray(crop)
        if arr.ndim != 3 or arr.shape[0] < 2 or arr.shape[1] < 2 or arr.shape[2] < 3:
            return None
        rgb = arr[:, :, :3]
        if input_format.lower() == "bgr":
            rgb = rgb[:, :, ::-1]
        if rgb.dtype != np.uint8:
            rgb = np.clip(rgb, 0, 255).astype(np.uint8)
        pil_img = Image.fromarray(np.ascontiguousarray(rgb), mode="RGB")

    resample = getattr(Image, "Resampling", Image).BICUBIC
    resized = pil_img.resize((target_w, target_h), resample=resample)
    img_f = np.asarray(resized, dtype=np.float32) / 255.0
    mean = np.asarray(pixel_mean, dtype=np.float32).reshape(1, 1, 3)
    std = np.asarray(pixel_std, dtype=np.float32).reshape(1, 1, 3)
    normed = (img_f - mean) / std
    return np.ascontiguousarray(normed.transpose(2, 0, 1), dtype=np.float32)


@contextlib.contextmanager
def _cope_import_context(cope_root: Path):
    """외부 COPE 저장소 경로를 일시적으로 sys.path 맨 앞에 둔다."""
    root_str = str(cope_root.resolve())
    added = False
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
        added = True
    try:
        yield
    finally:
        if added and root_str in sys.path:
            sys.path.remove(root_str)


class COPEEmbedder(Embedder):
    """COPE(CVPR 2026) ViT-B/16 기반 사람 외형 특징 추출기."""

    def __init__(
        self,
        config: COPEConfig | None = None,
        *,
        model: Any | None = None,
    ) -> None:
        self._config = config or COPEConfig.from_env()
        if self._config.batch_size < 1:
            raise ValueError(f"batch_size 는 1 이상이어야 합니다: {self._config.batch_size}")

        if model is not None:
            self._device = "cpu" if self._config.device == "auto" else self._config.device
            self._model = model
            self.weights_sha256 = "injected"
            self.clip_sha256 = "injected"
            return

        cope_root = Path(self._config.cope_root)
        if not cope_root.exists() or not (cope_root / "cope" / "model.py").is_file():
            raise FileNotFoundError(f"COPE 외부 저장소 경로를 찾을 수 없습니다: {cope_root}")

        self.weights_sha256 = verify_file_sha256(self._config.weights_path, self._config.weights_sha256)
        self.clip_sha256 = verify_file_sha256(self._config.clip_path, self._config.clip_sha256)
        self._device = resolve_torch_device(self._config.device)
        self._model = self._load_cope_model(cope_root)

    @property
    def config(self) -> COPEConfig:
        return self._config

    @property
    def embedding_dim(self) -> int:
        return COPE_EMBEDDING_DIM

    @property
    def device(self) -> str:
        return self._device

    def _load_cope_model(self, cope_root: Path) -> Any:
        import torch

        cfg_file = cope_root / self._config.config_rel_path
        if not cfg_file.is_file():
            raise FileNotFoundError(f"COPE 설정 파일이 없습니다: {cfg_file}")

        clip_path_resolved = str(Path(self._config.clip_path).resolve())
        weights_path_resolved = str(Path(self._config.weights_path).resolve())

        with _cope_import_context(cope_root):
            clip_mod = importlib.import_module("clip.clip")
            config_mod = importlib.import_module("config.defaults")
            cope_model_mod = importlib.import_module("cope.model")

            cfg = config_mod._C.clone()
            cfg.merge_from_file(str(cfg_file))
            cfg.INPUT.SIZE_TRAIN = list(self._config.input_size_hw)
            cfg.INPUT.SIZE_TEST = list(self._config.input_size_hw)
            cfg.MODEL.DEVICE = self._device
            cfg.freeze()

            param_dict = torch.load(weights_path_resolved, map_location="cpu", weights_only=False)
            cv_shape = param_dict.get("cv_embed", torch.empty((15, 768))).shape
            cls_shape = param_dict.get("classifier.weight", torch.empty((1041, COPE_EMBEDDING_DIM))).shape
            camera_num = int(cv_shape[0])
            num_classes = int(cls_shape[0])

            def _no_network_download(url: str, root: str = "") -> str:
                del root
                basename = os.path.basename(url)
                if basename != Path(clip_path_resolved).name:
                    raise RuntimeError(
                        f"CLIP 가중치 자동 다운로드는 금지되어 있습니다 (요청={basename}, 허용={Path(clip_path_resolved).name})"
                    )
                if not os.path.isfile(clip_path_resolved):
                    raise FileNotFoundError(f"로컬 CLIP 가중치가 없습니다: {clip_path_resolved}")
                return clip_path_resolved

            orig_download = clip_mod._download
            orig_load_clip = cope_model_mod.load_clip_to_cpu
            orig_tokenize = clip_mod.tokenize

            class _CpuSafeClipWrapper:
                """TransReID.__init__ 내부의 하드코딩된 .to('cuda') 호출을 CPU 환경에서도 안전하게 처리한다."""

                def __init__(self, inner: Any, target_dev: str) -> None:
                    object.__setattr__(self, "_inner", inner)
                    object.__setattr__(self, "_target_dev", target_dev)

                def to(self, dev: Any, *args: Any, **kwargs: Any) -> Any:
                    actual_dev = self._target_dev if str(dev).startswith("cuda") else dev
                    return self._inner.to(actual_dev, *args, **kwargs)

                def __getattr__(self, name: str) -> Any:
                    return getattr(self._inner, name)

            def _patched_load_clip(backbone_name: str, h_res: int, w_res: int, stride: int) -> Any:
                clip_model = orig_load_clip(backbone_name, h_res, w_res, stride)
                if self._device == "cpu":
                    return _CpuSafeClipWrapper(clip_model, "cpu")
                return clip_model

            class _CpuSafeTokenTensor(torch.Tensor):
                def cuda(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ARG002
                    return self

            def _patched_tokenize(*args: Any, **kwargs: Any) -> Any:
                tokens = orig_tokenize(*args, **kwargs)
                if self._device == "cpu":
                    return tokens.as_subclass(_CpuSafeTokenTensor)
                return tokens

            clip_mod._download = _no_network_download
            cope_model_mod.load_clip_to_cpu = _patched_load_clip
            clip_mod.tokenize = _patched_tokenize
            try:
                model = cope_model_mod.make_model(
                    cfg,
                    num_classes=num_classes,
                    camera_num=camera_num,
                    view_num=1,
                )
            finally:
                clip_mod._download = orig_download
                cope_model_mod.load_clip_to_cpu = orig_load_clip
                clip_mod.tokenize = orig_tokenize

            model.eval()
            state = model.state_dict()
            for key, value in param_dict.items():
                clean_key = key.replace("module.", "")
                if "classifier" in clean_key:
                    continue
                if clean_key in state:
                    state[clean_key].copy_(value)

            model.to(self._device)
            if hasattr(model, "prompt_learner") and hasattr(model.prompt_learner, "tokenized_prompts"):
                tok = model.prompt_learner.tokenized_prompts
                if isinstance(tok, _CpuSafeTokenTensor):
                    tok = tok.as_subclass(torch.Tensor)
                model.prompt_learner.tokenized_prompts = tok.to(self._device)
            model.eval()
            return model

    def embed(self, crops: Sequence[Any]) -> Any:
        """사람 crop 목록을 배치 추론해 (N, 1280) L2 정규화 float32 배열로 반환한다."""
        feats, _ = self._infer_batches(crops, with_scores=False)
        return feats

    def embed_with_scores(self, crops: Sequence[Any]) -> tuple[Any, Any]:
        """(L2 정규화 임베딩 (N, 1280), COPE PSS 품질 점수 (N,)) 튜플을 반환한다."""
        return self._infer_batches(crops, with_scores=True)

    def _infer_batches(self, crops: Sequence[Any], *, with_scores: bool) -> tuple[Any, Any]:
        import numpy as np
        import torch

        n = len(crops)
        out_feats = np.zeros((n, self.embedding_dim), dtype=np.float32)
        out_scores = np.zeros((n,), dtype=np.float32)
        if n == 0:
            return out_feats, out_scores

        valid_indices: list[int] = []
        valid_tensors: list[Any] = []
        for idx, crop in enumerate(crops):
            arr = preprocess_person_crop(
                crop,
                size_hw=self._config.input_size_hw,
                pixel_mean=self._config.pixel_mean,
                pixel_std=self._config.pixel_std,
                input_format=self._config.input_format,
            )
            if arr is not None:
                valid_indices.append(idx)
                valid_tensors.append(arr)

        if not valid_tensors:
            return out_feats, out_scores

        bs = self._config.batch_size
        with torch.inference_mode():
            for start in range(0, len(valid_tensors), bs):
                batch_np = np.stack(valid_tensors[start : start + bs], axis=0)
                batch_t = torch.from_numpy(batch_np).to(self._device, dtype=torch.float32)
                if with_scores:
                    res = self._model(batch_t, cam_label=None, view_label=None, get_image=False)
                    if isinstance(res, tuple):
                        raw_feat, raw_score = res[0], res[1]
                    else:
                        raw_feat = res
                        raw_score = torch.ones((batch_t.shape[0],), device=batch_t.device, dtype=torch.float32)
                else:
                    try:
                        raw_feat = self._model(batch_t, cam_label=None, view_label=None, get_image=True)
                    except TypeError:
                        raw_feat = self._model(batch_t)
                    if isinstance(raw_feat, tuple):
                        raw_score = raw_feat[1]
                        raw_feat = raw_feat[0]
                    else:
                        raw_score = torch.ones((batch_t.shape[0],), device=batch_t.device, dtype=torch.float32)

                feat_np = l2_normalize_rows(raw_feat.detach().cpu().numpy())
                score_np = raw_score.detach().cpu().numpy().astype(np.float32).reshape(-1)
                for offset, target_idx in enumerate(valid_indices[start : start + bs]):
                    out_feats[target_idx] = feat_np[offset]
                    out_scores[target_idx] = score_np[offset]

        return out_feats, out_scores
