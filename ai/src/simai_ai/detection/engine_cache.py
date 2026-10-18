"""YOLO26-pose TensorRT FP16 엔진 캐시 (명세 3.12 'GPU별 엔진', 18.2 시작 순서).

TensorRT 엔진은 GPU 아키텍처와 TensorRT 버전에 묶인다(A40 sm_86, RTX 4070 sm_89, H100 sm_90).
그래서 이미지를 빌드할 때가 아니라 Pod 가 처음 기동할 때 그 GPU 에서 만들고 Network Volume 에 캐시한다.
가중치(.pt)와 빌드 설정이 정본이고 엔진은 산출물이다.

경로 (명세 3.12 의 `{model}/{gpu_arch}/{tensorrt_version}` 를 따른다)::

    {cache_root}/{모델}/{gpu_arch}/trt{TensorRT}/{모델}-{가중치 sha256 앞 12자}_{mode}_i{imgsz}_b{batch}_c{W}x{H}_fp16_ul{ultralytics}.engine
    (같은 이름의 .json 에 키 전부와 엔진 sha256 을 둔다)

캐시를 쓰는 조건은 메타데이터의 키(가중치 sha256, GPU 아키텍처, TensorRT·ultralytics 버전, 입력 설정)가 지금 것과 모두 같고
엔진 파일 해시가 메타데이터와 같을 때뿐이다. 다른 GPU 에서 만든 엔진은 경로부터 달라 고르지 않고, 같은 경로에 키가 다른
파일이 있으면(손으로 복사 등) 쓰지 않고 옆으로 옮긴 뒤(지우지 않음) 다시 만든다.

CLI (Tier 0 이미지 entrypoint 가 Tier 0 시작 전에 별도 프로세스로 부른다)::

    python -m simai_ai.detection.engine_cache ensure --weights W --cache-root R [--expect-sha256 H]
    python -m simai_ai.detection.engine_cache plan --weights W --cache-root R --gpu-arch sm_90 --tensorrt 11.4.0.106 --ultralytics 8.4.178

`ensure` 는 준비된 엔진 경로를 stdout 마지막 줄에 쓴다. 종료 코드는 `EXIT_*` 상수를 본다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
MODES = ("e2e", "nms")
PRECISION = "fp16"

EXIT_OK = 0
EXIT_WEIGHTS = 2  # 가중치 없음·해시 불일치
EXIT_NO_GPU = 3  # CUDA GPU·TensorRT 없음
EXIT_BAD_CACHE = 4  # 캐시 위치를 쓸 수 없음
EXIT_BUILD = 5  # 엔진 빌드 실패
EXIT_USAGE = 64

_SM = re.compile(r"^sm_[0-9]{2,3}[a-z]?$")
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STEM = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class EngineCacheError(Exception):
    exit_code = EXIT_BUILD


class WeightsError(EngineCacheError):
    exit_code = EXIT_WEIGHTS


class NoGpuError(EngineCacheError):
    exit_code = EXIT_NO_GPU


class CacheDirError(EngineCacheError):
    exit_code = EXIT_BAD_CACHE


class BuildError(EngineCacheError):
    exit_code = EXIT_BUILD


@dataclass(frozen=True)
class EngineConfig:
    """엔진 입력 설정. 기본값은 Tier 0 기준선(명세 5.1, detection_baseline 결정): YOLO26x-pose E2E, imgsz 1280, 카메라 4대 batch."""

    mode: str = "e2e"
    imgsz: int = 1280
    max_batch: int = 4
    camera_w: int = 2304
    camera_h: int = 1296

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode 는 {MODES} 중 하나다: {self.mode!r}")
        if self.imgsz <= 0 or self.imgsz % 32:
            raise ValueError(f"imgsz 는 32 의 양의 배수다: {self.imgsz}")
        if self.max_batch < 1:
            raise ValueError(f"max_batch 는 1 이상이다: {self.max_batch}")
        if self.camera_w <= 0 or self.camera_h <= 0:
            raise ValueError(f"카메라 해상도가 잘못됐다: {self.camera_w}x{self.camera_h}")


@dataclass(frozen=True)
class BuildTarget:
    """엔진을 만들고 쓰는 GPU·소프트웨어. 셋 중 하나라도 다르면 다른 엔진이다."""

    gpu_arch: str
    tensorrt_version: str
    ultralytics_version: str
    gpu_name: str = ""  # 기록용 (같은 sm 의 H100 SXM·PCIe 는 같은 엔진을 쓴다)

    def __post_init__(self) -> None:
        if not _SM.match(self.gpu_arch):
            raise ValueError(f"gpu_arch 형식은 sm_90 같은 값이다: {self.gpu_arch!r}")
        for name in ("tensorrt_version", "ultralytics_version"):
            value = getattr(self, name)
            if not _VERSION.match(value):
                raise ValueError(f"{name} 형식이 잘못됐다: {value!r}")


@dataclass(frozen=True)
class ModelRef:
    path: Path
    sha256: str
    stem: str


@dataclass(frozen=True)
class CacheStatus:
    state: str  # missing | valid | incomplete | conflict
    reason: str = ""


@dataclass
class EnsureResult:
    engine: Path
    built: bool
    quarantined: list[Path] = field(default_factory=list)


# --------------------------------------------------------------------------- 키와 경로


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_ref(weights: Path, expect_sha256: str | None = None) -> ModelRef:
    """가중치 파일을 확인한다. 없거나 기대 해시와 다르면 WeightsError (가중치는 이미지에 없고 Network Volume 에 있다)."""
    if not weights.is_file():
        raise WeightsError(
            f"가중치 없음: {weights}. 가중치는 이미지에 넣지 않는다. Network Volume 에 받아 두고"
            " 해시 목록과 대조한 뒤 SIMAI_TIER0_WEIGHTS 로 위치를 준다 (deploy/README.md)"
        )
    stem = weights.stem
    if not _STEM.match(stem):
        raise WeightsError(f"가중치 파일 이름을 경로 키로 쓸 수 없다: {weights.name!r}")
    digest = sha256_file(weights)
    if expect_sha256:
        expected = expect_sha256.strip().lower()
        if not _SHA256.match(expected):
            raise WeightsError(f"기대 sha256 형식이 잘못됐다: {expect_sha256!r}")
        if digest != expected:
            raise WeightsError(f"가중치 해시 불일치: {weights} sha256={digest}, 기대값={expected}")
    return ModelRef(path=weights, sha256=digest, stem=stem)


def engine_profile(cfg: EngineConfig) -> dict[str, list[int]]:
    """TensorRT 동적 프로파일. opt 는 카메라 프레임을 rect 레터박스한 실제 입력(batch max_batch), max 는 imgsz 정사각형.

    ai/experiments/detection_baseline/det_common.engine_profile 과 같은 규칙이다(src 는 experiments 를 import 하지 않는다).
    """
    if cfg.camera_w >= cfg.camera_h:
        h, w = math.ceil(cfg.camera_h * cfg.imgsz / cfg.camera_w / 32) * 32, cfg.imgsz
    else:
        h, w = cfg.imgsz, math.ceil(cfg.camera_w * cfg.imgsz / cfg.camera_h / 32) * 32
    return {"min": [1, 3, 32, 32], "opt": [cfg.max_batch, 3, h, w], "max": [cfg.max_batch, 3, cfg.imgsz, cfg.imgsz]}


def engine_path(cache_root: Path, model: ModelRef, cfg: EngineConfig, target: BuildTarget) -> Path:
    name = (
        f"{model.stem}-{model.sha256[:12]}_{cfg.mode}_i{cfg.imgsz}_b{cfg.max_batch}"
        f"_c{cfg.camera_w}x{cfg.camera_h}_{PRECISION}_ul{target.ultralytics_version}.engine"
    )
    return cache_root / model.stem / target.gpu_arch / f"trt{target.tensorrt_version}" / name


def metadata_path(engine: Path) -> Path:
    return engine.with_suffix(".json")


def expected_keys(model: ModelRef, cfg: EngineConfig, target: BuildTarget) -> dict[str, Any]:
    """캐시를 쓰려면 메타데이터에서 모두 같아야 하는 값."""
    return {
        "format_version": FORMAT_VERSION,
        "model_stem": model.stem,
        "model_sha256": model.sha256,
        "gpu_arch": target.gpu_arch,
        "tensorrt_version": target.tensorrt_version,
        "ultralytics_version": target.ultralytics_version,
        "precision": PRECISION,
        "config": asdict(cfg),
        "profile": engine_profile(cfg),
    }


def inspect_cache(engine: Path, model: ModelRef, cfg: EngineConfig, target: BuildTarget) -> CacheStatus:
    meta_file = metadata_path(engine)
    has_engine, has_meta = engine.is_file(), meta_file.is_file()
    if not has_engine and not has_meta:
        return CacheStatus("missing")
    if not has_meta:
        return CacheStatus("incomplete", "엔진은 있으나 메타데이터가 없다(빌드가 끝나지 않았거나 손으로 둔 파일)")
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return CacheStatus("conflict", f"메타데이터를 읽지 못했다: {e}")
    if not isinstance(meta, dict):
        return CacheStatus("conflict", "메타데이터 형식이 객체가 아니다")
    for key, want in expected_keys(model, cfg, target).items():
        if meta.get(key) != want:
            return CacheStatus("conflict", f"메타데이터 {key} 가 다르다: {meta.get(key)!r} != {want!r}")
    if not has_engine:
        return CacheStatus("incomplete", "메타데이터는 있으나 엔진 파일이 없다")
    actual = sha256_file(engine)
    if meta.get("engine_sha256") != actual:
        return CacheStatus("conflict", f"엔진 해시가 메타데이터와 다르다: {actual} != {meta.get('engine_sha256')!r}")
    return CacheStatus("valid")


# --------------------------------------------------------------------------- GPU 확인과 빌드 (GPU 이미지에서만)


def detect_target() -> BuildTarget:
    """지금 GPU(장치 0)와 설치된 TensorRT·ultralytics 를 읽는다. GPU 나 TensorRT 가 없으면 NoGpuError."""
    try:
        import torch
    except ImportError as e:
        raise NoGpuError("torch 가 없다. Tier 0 이미지(extras tier0)에서 실행한다") from e
    if not torch.cuda.is_available():
        raise NoGpuError(
            "CUDA GPU 가 없다. TensorRT 엔진은 대상 GPU 에서만 만들 수 있다(명세 3.12). "
            "GPU 없이 이미지를 점검할 때는 entrypoint 를 거치지 않는다 (deploy/README.md)"
        )
    major, minor = torch.cuda.get_device_capability(0)
    try:
        import tensorrt
    except ImportError as e:
        raise NoGpuError("TensorRT(tensorrt-cu13) 가 없다. Tier 0 이미지(extras tier0, Linux)에서 실행한다") from e
    import ultralytics

    return BuildTarget(
        gpu_arch=f"sm_{major}{minor}",
        tensorrt_version=str(tensorrt.__version__),
        ultralytics_version=str(ultralytics.__version__),
        gpu_name=str(torch.cuda.get_device_name(0)),
    )


def build_engine(model: ModelRef, cfg: EngineConfig, target: BuildTarget, work_dir: Path) -> Path:
    """ultralytics export 로 TensorRT FP16 엔진을 work_dir 안에 만들고 그 경로를 돌려준다.

    가중치를 work_dir 로 복사해서 내보낸다(볼륨의 가중치 폴더에 .onnx·.engine 을 남기지 않는다). 동적 프로파일은
    detection_baseline/export_engine.py 와 같이 IOptimizationProfile.set_shape 를 감싸 바꿔 끼운다(ultralytics 8.4.178 의
    onnx2engine 은 프로파일을 인자로 받지 않는다). simplify=False: onnxslim·onnxruntime 을 이미지에 넣지 않는다.
    """
    import tensorrt as trt
    from ultralytics import YOLO

    local = work_dir / f"{model.stem}.pt"
    shutil.copyfile(model.path, local)
    if sha256_file(local) != model.sha256:
        raise BuildError(f"가중치를 복사하는 사이에 내용이 바뀌었다: {model.path}")
    prof = engine_profile(cfg)
    original = trt.IOptimizationProfile.set_shape

    def fixed_set_shape(self, name, min, opt, max):  # noqa: A002 - TensorRT 인자 이름을 그대로 쓴다
        return original(self, name, min=tuple(prof["min"]), opt=tuple(prof["opt"]), max=tuple(prof["max"]))

    trt.IOptimizationProfile.set_shape = fixed_set_shape
    try:
        out = YOLO(str(local)).export(
            format="engine",
            imgsz=cfg.imgsz,
            quantize=16,
            dynamic=True,
            batch=cfg.max_batch,
            nms=False if cfg.mode == "e2e" else None,
            simplify=False,
            device=0,
            verbose=False,
        )
    finally:
        trt.IOptimizationProfile.set_shape = original
    produced = Path(str(out))
    if not produced.is_file():
        raise BuildError(f"ultralytics export 가 엔진을 만들지 않았다: {out!r}")
    return produced


def _quarantine(path: Path, now_ms: int) -> Path:
    dest = path.with_name(f"{path.name}.conflict-{now_ms}")
    n = 0
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.conflict-{now_ms}-{n}")
    os.replace(path, dest)
    return dest


def ensure_engine(
    weights: Path,
    cache_root: Path,
    cfg: EngineConfig,
    *,
    now_ms: Callable[[], int],
    expect_sha256: str | None = None,
    detect: Callable[[], BuildTarget] = detect_target,
    build: Callable[[ModelRef, EngineConfig, BuildTarget, Path], Path] = build_engine,
    log: Callable[[str], None] = lambda msg: print(msg, file=sys.stderr),
) -> EnsureResult:
    """캐시에 맞는 엔진이 있으면 그 경로, 없으면 지금 GPU 에서 만들어 캐시한 경로를 돌려준다.

    순서: 가중치 확인(GPU 를 건드리기 전) → GPU·TensorRT 확인 → 캐시 확인 → 필요하면 빌드.
    빌드 산출물은 임시 폴더에서 만든 뒤 엔진 → 메타데이터 순으로 os.replace 한다. 메타데이터가 없는 엔진은 쓰지 않으므로
    도중에 죽어도 반쯤 쓴 엔진을 고르지 않는다.
    """
    model = model_ref(weights, expect_sha256)
    target = detect()
    engine = engine_path(cache_root, model, cfg, target)
    status = inspect_cache(engine, model, cfg, target)
    result = EnsureResult(engine=engine, built=False)
    if status.state == "valid":
        log(f"엔진 캐시 사용: {engine}")
        return result
    if status.state != "missing":
        log(f"엔진 캐시를 쓰지 않는다({status.state}): {status.reason}")
        for p in (engine, metadata_path(engine)):
            if p.exists():
                moved = _quarantine(p, now_ms())
                result.quarantined.append(moved)
                log(f"  옆으로 옮김: {moved}")

    try:
        engine.parent.mkdir(parents=True, exist_ok=True)
        work = engine.parent / f".build-{os.getpid()}-{now_ms()}"
        work.mkdir()
    except OSError as e:
        raise CacheDirError(f"엔진 캐시 위치를 쓸 수 없다: {engine.parent} ({e})") from e
    log(f"엔진 빌드 시작({target.gpu_arch}, {target.gpu_name or 'GPU 이름 모름'}, TensorRT {target.tensorrt_version}): {engine}")
    started = now_ms()
    try:
        try:
            produced = build(model, cfg, target, work)
        except EngineCacheError:
            raise
        except Exception as e:  # noqa: BLE001 - 빌더 오류는 종류와 상관없이 빌드 실패로 보고한다
            raise BuildError(f"엔진 빌드 실패: {type(e).__name__}: {e}") from e
        digest = sha256_file(produced)
        meta = expected_keys(model, cfg, target) | {
            "engine_sha256": digest,
            "engine_file": engine.name,
            "gpu_name": target.gpu_name,
            "weights_path": str(model.path),
            "built_at_ms": now_ms(),
            "build_s": round((now_ms() - started) / 1000, 1),
        }
        tmp_meta = work / "meta.json"
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(produced, engine)
        os.replace(tmp_meta, metadata_path(engine))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if inspect_cache(engine, model, cfg, target).state != "valid":
        raise BuildError(f"만든 엔진이 캐시 확인을 통과하지 못했다: {engine}")
    log(f"엔진 빌드 완료: {engine}")
    result.built = True
    return result


# --------------------------------------------------------------------------- CLI


def _camera(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"([0-9]+)x([0-9]+)", text)
    if not m:
        raise argparse.ArgumentTypeError(f"카메라 해상도는 WxH 형식이다: {text!r}")
    return int(m[1]), int(m[2])


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m simai_ai.detection.engine_cache", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_text in (("ensure", "엔진을 확인하고 없으면 지금 GPU 에서 만든다"), ("plan", "GPU 없이 엔진 경로만 계산한다")):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--weights", type=Path, required=True, help="YOLO26-pose 가중치 .pt (Network Volume)")
        p.add_argument("--cache-root", type=Path, required=True, help="엔진 캐시 루트 (Network Volume)")
        p.add_argument("--expect-sha256", default=None, help="가중치 기대 sha256. 주면 다를 때 시작하지 않는다")
        p.add_argument("--mode", choices=MODES, default="e2e")
        p.add_argument("--imgsz", type=int, default=1280)
        p.add_argument("--max-batch", type=int, default=4)
        p.add_argument("--camera", type=_camera, default=(2304, 1296), help="카메라 해상도 WxH (opt 프로파일)")
        if name == "plan":
            p.add_argument("--gpu-arch", required=True, help="예: sm_90 (H100)")
            p.add_argument("--tensorrt", required=True, help="TensorRT 버전")
            p.add_argument("--ultralytics", required=True, help="ultralytics 버전")
    return ap


def main(argv: list[str] | None = None, *, detect: Callable[[], BuildTarget] = detect_target,
         build: Callable[[ModelRef, EngineConfig, BuildTarget, Path], Path] = build_engine) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = EngineConfig(mode=args.mode, imgsz=args.imgsz, max_batch=args.max_batch,
                           camera_w=args.camera[0], camera_h=args.camera[1])
        if args.command == "plan":
            target = BuildTarget(gpu_arch=args.gpu_arch, tensorrt_version=args.tensorrt,
                                 ultralytics_version=args.ultralytics)
            model = model_ref(args.weights, args.expect_sha256)
            engine = engine_path(args.cache_root, model, cfg, target)
            print(f"{inspect_cache(engine, model, cfg, target).state}\t{engine}")
            return EXIT_OK
        result = ensure_engine(args.weights, args.cache_root, cfg, expect_sha256=args.expect_sha256,
                               now_ms=lambda: time.time_ns() // 1_000_000, detect=detect, build=build)
    except ValueError as e:
        print(f"engine_cache: 설정 오류: {e}", file=sys.stderr)
        return EXIT_USAGE
    except EngineCacheError as e:
        print(f"engine_cache: {e}", file=sys.stderr)
        return e.exit_code
    print(result.engine)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
