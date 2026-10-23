"""한 가상환경(Tier 0 + vLLM + HMR, torch 2.11.0) 잠금의 안전장치 (S15P31A101-235, docs/design/unified-env.md).

- Tier 0 핵심 판이 상·하한으로 묶여 워커 패키지 변경에 끌려가지 않는다.
- Linux GPU 전용 패키지(vLLM·HMR)는 Windows 노트북 개발 환경에 깔리지 않는다(marker).
- 잠금에 torch 는 한 판이고 OpenCV GUI 판(opencv-python)은 어디에도 설치되지 않는다.
- VLM 가중치 목록은 커밋 고정·해시가 있고 Reasoner 가 쓰지 않는 vae·sound_tokenizer 를 받지 않는다.
"""

import json
import re
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement

AI = Path(__file__).resolve().parents[2]
REPO = AI.parent
PYPROJECT = tomllib.loads((AI / "pyproject.toml").read_text(encoding="utf-8"))
LOCK = tomllib.loads((AI / "uv.lock").read_text(encoding="utf-8"))
EXTRAS = PYPROJECT["project"]["optional-dependencies"]


def _reqs(extra: str) -> dict[str, Requirement]:
    return {Requirement(r).name: Requirement(r) for r in EXTRAS[extra] if not r.startswith("simai-ai[")}


def _lock(name: str) -> list[dict]:
    return [p for p in LOCK["package"] if p["name"] == name]


@pytest.mark.parametrize(
    ("name", "spec"),
    [
        ("torch", "==2.11.0"),
        ("torchvision", "==0.26.0"),
        ("onnx", "<1.22,>=1.21"),
        ("nvidia-modelopt", "<0.48,>=0.47"),
        ("tensorrt-cu13", "<12,>=11.4"),
    ],
)
def test_tier0_core_versions_are_bounded(name, spec):
    req = _reqs("tier0")[name]
    assert str(req.specifier) == spec


def test_ultralytics_is_bounded_for_tier0():
    assert str(_reqs("detection")["ultralytics"].specifier) == "<8.5,>=8.4.178"


@pytest.mark.parametrize("extra", ["vlm", "hmr", "scene"])
def test_linux_only_worker_packages_have_marker(extra):
    """torch·torchvision(모든 플랫폼에 같은 판, Windows 는 CPU 휠)만 marker 없이 둔다. 하나라도 빠지면 Windows uv lock·sync 가 깨진다."""
    missing = [n for n, r in _reqs(extra).items() if n not in {"torch", "torchvision"} and str(r.marker) != 'sys_platform == "linux"']
    assert missing == []


def test_constraints_only_apply_on_linux():
    """VLM 쪽 판 제약(constraint-dependencies)이 Windows 노트북 개발 환경의 판까지 묶지 않는다."""
    cons = [Requirement(r) for r in PYPROJECT["tool"]["uv"]["constraint-dependencies"]]
    assert cons
    assert [str(c) for c in cons if str(c.marker) != 'sys_platform == "linux"'] == []


def test_worker_extra_bundles_hmr_and_vlm():
    assert set(EXTRAS["worker"]) == {"simai-ai[scene,hmr,vlm]"}
    assert str(_reqs("vlm")["vllm"].specifier) == "==0.21.0"
    assert str(_reqs("hmr")["torch"].specifier) == "==2.11.0"


def test_lock_has_one_torch_and_vllm_pins():
    assert [p["version"] for p in _lock("torch")] == ["2.11.0"]
    assert [p["version"] for p in _lock("torchvision")] == ["0.26.0"]
    assert [p["version"] for p in _lock("vllm")] == ["0.21.0"]
    flashinfer = {d["name"] for d in _lock("vllm")[0]["dependencies"]} & {"flashinfer-python", "flashinfer-cubin"}
    assert flashinfer == {"flashinfer-python", "flashinfer-cubin"}


def test_opencv_gui_wheel_is_never_installed():
    """ultralytics 의 opencv-python 은 override marker 'never' 로만 남는다(headless 하나로 통일)."""
    assert PYPROJECT["tool"]["uv"]["override-dependencies"] == [
        "opencv-python; sys_platform == 'never'",
        # modelopt[onnx] 의 CUDA 12 빌드 의존성(S15P31A101-246): AutoCast 는 CPU onnxruntime 으로 돈다
        "onnxruntime-gpu; sys_platform == 'never'",
        "cupy-cuda12x; sys_platform == 'never'",
    ]
    edges = [(p["name"], d.get("marker", "")) for p in LOCK["package"] for d in p.get("dependencies", []) if d["name"] == "opencv-python"]
    assert edges and all(m == "sys_platform == 'never'" for _, m in edges), edges
    names = {p["name"] for p in LOCK["package"]}
    assert not {"onnxruntime-gpu", "cupy-cuda12x"} & names and "onnxruntime" in names


def test_git_sources_are_commit_pinned():
    for name in ("vllm-cosmos3", "transformers-cosmos3", "utils3d-moge", "pipeline"):
        src = PYPROJECT["tool"]["uv"]["sources"][name]
        assert re.fullmatch(r"[0-9a-f]{40}", src["rev"]), name


def test_vlm_weights_are_pinned_and_skip_generator_only_files():
    doc = json.loads((REPO / "deploy" / "weights.json").read_text(encoding="utf-8"))
    names = [w["name"] for w in doc["weights"]]
    assert len(names) == len(set(names))
    vlm = [w for w in doc["weights"] if "vlm" in w["sets"]]
    assert len(vlm) == 18
    files = {w["source"]["hf"]["file"] for w in vlm}
    assert {f"transformer/diffusion_pytorch_model-0000{i}-of-00007.safetensors" for i in range(1, 8)} <= files
    assert {"vision_encoder/model.safetensors", "model.safetensors.index.json", "config.json", "tokenizer.json"} <= files
    assert not any(f.startswith(("vae/", "sound_tokenizer/")) for f in files)
    for w in vlm:
        assert w["sets"] == ["vlm"] and w["source"]["hf"]["repo"] == "nvidia/Cosmos3-Nano"
        assert re.fullmatch(r"[0-9a-f]{40}", w["source"]["hf"]["revision"]), w["name"]
        assert re.fullmatch(r"[0-9a-f]{64}", w["sha256"]), w["name"]
        assert w["path"] == "weights/cosmos3-nano/" + w["source"]["hf"]["file"]
