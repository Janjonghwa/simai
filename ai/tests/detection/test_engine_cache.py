"""TensorRT 엔진 캐시 (simai_ai.detection.engine_cache). GPU·TensorRT 없이 detect·build 를 대역으로 바꿔 돈다.

반례 위주: 다른 GPU 의 엔진을 고르지 않는다, 같은 경로에 키가 다른 파일을 쓰지 않는다, 반쯤 만든 엔진을 쓰지 않는다,
가중치가 없거나 해시가 다르면 GPU 를 건드리기 전에 멈춘다, GPU 가 없으면 빌드하지 않고 명확한 코드로 끝난다.
"""

from __future__ import annotations

import hashlib
import json
from itertools import count
from pathlib import Path

import pytest

from simai_ai.detection import engine_cache as ec

TORCH = "2.11.0+cu130"
H100 = ec.BuildTarget(gpu_arch="sm_90", tensorrt_version="11.4.0.106", ultralytics_version="8.4.178", gpu_name="H100", torch_version=TORCH, modelopt_version="0.47.0")
A40 = ec.BuildTarget(gpu_arch="sm_86", tensorrt_version="11.4.0.106", ultralytics_version="8.4.178", gpu_name="A40", torch_version=TORCH, modelopt_version="0.47.0")
CFG = ec.EngineConfig()


@pytest.fixture
def weights(tmp_path: Path) -> Path:
    p = tmp_path / "weights" / "yolo26x-pose.pt"
    p.parent.mkdir()
    p.write_bytes(b"fake-weights-v1")
    return p


class FakeBuilder:
    """엔진 대신 (가중치 해시, GPU) 를 적은 파일을 만든다. 호출 수를 센다."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[ec.BuildTarget] = []
        self.fail = fail

    def __call__(self, model: ec.ModelRef, cfg: ec.EngineConfig, target: ec.BuildTarget, work: Path) -> Path:
        self.calls.append(target)
        if self.fail:
            (work / "partial.engine").write_bytes(b"half")
            raise self.fail
        out = work / f"{model.stem}.engine"
        out.write_bytes(f"engine:{model.sha256}:{target.gpu_arch}:{cfg.mode}".encode())
        return out


def _clock():
    c = count(1_760_000_000_000, 1000)
    return lambda: next(c)


def _ensure(weights: Path, root: Path, target: ec.BuildTarget, builder: FakeBuilder, **kw) -> ec.EnsureResult:
    return ec.ensure_engine(weights, root, CFG, now_ms=_clock(), detect=lambda: target, build=builder,
                            log=lambda _m: None, **kw)


# --------------------------------------------------------------------------- 경로와 키


def test_path_has_gpu_arch_tensorrt_and_model_hash(weights: Path, tmp_path: Path) -> None:
    model = ec.model_ref(weights)
    p = ec.engine_path(tmp_path / "engines", model, CFG, H100)
    sha = hashlib.sha256(b"fake-weights-v1").hexdigest()
    assert p.parent == tmp_path / "engines" / "yolo26x-pose" / "sm_90" / "trt11.4.0.106"
    assert p.name == f"yolo26x-pose-{sha[:12]}_e2e_i1280_b4_c2304x1296_fp16_ul8.4.178.engine"


def test_profile_matches_detection_baseline() -> None:
    # detection_baseline README: 2304x1296 → imgsz 1280 에서 736x1280, min 1x3x32x32, max 4x3x1280x1280
    assert ec.engine_profile(CFG) == {"min": [1, 3, 32, 32], "opt": [4, 3, 736, 1280], "max": [4, 3, 1280, 1280]}
    assert ec.engine_profile(ec.EngineConfig(imgsz=640))["opt"] == [4, 3, 384, 640]


@pytest.mark.parametrize("change", [
    {"mode": "nms"}, {"imgsz": 640}, {"max_batch": 1}, {"camera_w": 1920, "camera_h": 1080},
])
def test_every_input_setting_changes_the_path(weights: Path, tmp_path: Path, change: dict) -> None:
    model = ec.model_ref(weights)
    other = ec.EngineConfig(**({"mode": "e2e", "imgsz": 1280, "max_batch": 4, "camera_w": 2304, "camera_h": 1296} | change))
    assert ec.engine_path(tmp_path, model, other, H100) != ec.engine_path(tmp_path, model, CFG, H100)


@pytest.mark.parametrize("bad", [
    {"gpu_arch": "90"}, {"gpu_arch": "sm_90/../x"}, {"tensorrt_version": "../11"}, {"ultralytics_version": ""},
])
def test_target_values_cannot_escape_the_path(bad: dict) -> None:
    base = {"gpu_arch": "sm_90", "tensorrt_version": "11.4.0.106", "ultralytics_version": "8.4.178"}
    with pytest.raises(ValueError):
        ec.BuildTarget(**(base | bad))


@pytest.mark.parametrize("bad", [{"mode": "trt"}, {"imgsz": 1000}, {"imgsz": 0}, {"max_batch": 0}, {"camera_w": 0}])
def test_bad_config_rejected(bad: dict) -> None:
    with pytest.raises(ValueError):
        ec.EngineConfig(**bad)


# --------------------------------------------------------------------------- 가중치


def test_missing_weights_stop_before_gpu(tmp_path: Path) -> None:
    def detect():
        raise AssertionError("가중치가 없으면 GPU 를 확인하지 않는다")

    with pytest.raises(ec.WeightsError, match="가중치 없음"):
        ec.ensure_engine(tmp_path / "nope.pt", tmp_path / "engines", CFG, now_ms=_clock(), detect=detect,
                         build=FakeBuilder(), log=lambda _m: None)


def test_expected_hash_mismatch_refused(weights: Path, tmp_path: Path) -> None:
    builder = FakeBuilder()
    with pytest.raises(ec.WeightsError, match="해시 불일치"):
        _ensure(weights, tmp_path / "engines", H100, builder, expect_sha256="0" * 64)
    assert builder.calls == []
    ok = hashlib.sha256(b"fake-weights-v1").hexdigest()
    assert _ensure(weights, tmp_path / "engines", H100, builder, expect_sha256=ok.upper()).built


# --------------------------------------------------------------------------- 캐시


def test_build_once_then_reuse(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    builder = FakeBuilder()
    first = _ensure(weights, root, H100, builder)
    second = _ensure(weights, root, H100, builder)
    assert first.built and not second.built
    assert first.engine == second.engine and len(builder.calls) == 1
    meta = json.loads(ec.metadata_path(first.engine).read_text(encoding="utf-8"))
    assert meta["gpu_arch"] == "sm_90" and meta["engine_sha256"] == ec.sha256_file(first.engine)
    assert [p.name for p in first.engine.parent.iterdir() if p.name.startswith(".build-")] == []


def test_other_gpu_cache_is_never_selected(weights: Path, tmp_path: Path) -> None:
    """A40(sm_86) 캐시만 있는 볼륨을 H100 에 붙이면 그 엔진을 쓰지 않고 H100 엔진을 새로 만든다."""
    root = tmp_path / "engines"
    a40 = _ensure(weights, root, A40, FakeBuilder())
    builder = FakeBuilder()
    h100 = _ensure(weights, root, H100, builder)
    assert h100.built and builder.calls == [H100]
    assert h100.engine != a40.engine and b"sm_90" in h100.engine.read_bytes()
    assert a40.engine.read_bytes().endswith(b"sm_86:e2e")  # A40 캐시는 그대로 남는다


def test_foreign_engine_copied_into_h100_path_is_not_used(weights: Path, tmp_path: Path) -> None:
    """A40 엔진·메타데이터를 H100 경로로 손으로 복사해도 메타데이터 키가 달라 쓰지 않는다(옆으로 옮기고 다시 만든다)."""
    root = tmp_path / "engines"
    a40 = _ensure(weights, root, A40, FakeBuilder())
    model = ec.model_ref(weights)
    h100_path = ec.engine_path(root, model, CFG, H100)
    h100_path.parent.mkdir(parents=True)
    h100_path.write_bytes(a40.engine.read_bytes())
    ec.metadata_path(h100_path).write_text(ec.metadata_path(a40.engine).read_text(encoding="utf-8"), encoding="utf-8")
    assert ec.inspect_cache(h100_path, model, CFG, H100).state == "conflict"

    builder = FakeBuilder()
    result = _ensure(weights, root, H100, builder)
    assert result.built and len(builder.calls) == 1
    assert b"sm_90" in result.engine.read_bytes()
    assert len(result.quarantined) == 2 and all(p.exists() and ".conflict-" in p.name for p in result.quarantined)


def test_tampered_engine_bytes_not_used(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    first = _ensure(weights, root, H100, FakeBuilder())
    first.engine.write_bytes(b"corrupted")
    builder = FakeBuilder()
    again = _ensure(weights, root, H100, builder)
    assert again.built and len(builder.calls) == 1 and again.engine.read_bytes() != b"corrupted"


def test_engine_without_metadata_is_rebuilt(weights: Path, tmp_path: Path) -> None:
    """빌드가 엔진 교체 직후 죽어 메타데이터가 없으면 그 엔진은 쓰지 않는다."""
    root = tmp_path / "engines"
    model = ec.model_ref(weights)
    path = ec.engine_path(root, model, CFG, H100)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"unknown origin")
    assert ec.inspect_cache(path, model, CFG, H100).state == "incomplete"
    builder = FakeBuilder()
    assert _ensure(weights, root, H100, builder).built and len(builder.calls) == 1


def test_new_weights_get_new_engine(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    old = _ensure(weights, root, H100, FakeBuilder())
    weights.write_bytes(b"fake-weights-v2 (fine-tuned)")
    new = _ensure(weights, root, H100, FakeBuilder())
    assert new.built and new.engine != old.engine and old.engine.exists()


def test_tensorrt_upgrade_gets_new_engine(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    old = _ensure(weights, root, H100, FakeBuilder())
    newer = ec.BuildTarget(gpu_arch="sm_90", tensorrt_version="11.5.0.1", ultralytics_version="8.4.178")
    new = _ensure(weights, root, newer, FakeBuilder())
    assert new.built and new.engine.parent.name == "trt11.5.0.1" and old.engine.exists()


def test_torch_change_rebuilds_and_keeps_old_engine_aside(weights: Path, tmp_path: Path) -> None:
    """S15P31A101-246: 한 가상환경으로 torch 가 2.14.1 → 2.11.0 이 됐다. 경로는 같지만 키가 달라 옛 엔진을 쓰지 않는다(지우지 않고 옆으로)."""
    root = tmp_path / "engines"
    old_t = ec.BuildTarget(gpu_arch="sm_90", tensorrt_version="11.4.0.106", ultralytics_version="8.4.178", torch_version="2.14.1+cu130",
                           modelopt_version="0.47.0")
    old = _ensure(weights, root, old_t, FakeBuilder())
    builder = FakeBuilder()
    new = _ensure(weights, root, H100, builder)
    assert new.engine == old.engine and new.built and len(builder.calls) == 1 and new.quarantined
    meta = json.loads(ec.metadata_path(new.engine).read_text(encoding="utf-8"))
    assert meta["torch_version"] == TORCH
    assert not _ensure(weights, root, H100, FakeBuilder()).built  # 같은 torch 면 다시 쓴다


def test_modelopt_change_rebuilds(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    _ensure(weights, root, H100, FakeBuilder())
    newer = ec.BuildTarget(gpu_arch="sm_90", tensorrt_version="11.4.0.106", ultralytics_version="8.4.178", torch_version=TORCH,
                           modelopt_version="0.48.0")
    assert _ensure(weights, root, newer, FakeBuilder()).built


def test_engine_from_before_torch_key_is_not_reused(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    first = _ensure(weights, root, H100, FakeBuilder())
    meta_file = ec.metadata_path(first.engine)
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    del meta["torch_version"]  # -246 전에 만든 캐시(2차 세션, torch 2.14.1)
    meta_file.write_text(json.dumps(meta), encoding="utf-8")
    model = ec.model_ref(weights)
    assert ec.inspect_cache(first.engine, model, CFG, H100).state == "conflict"
    assert _ensure(weights, root, H100, FakeBuilder()).built


def test_concurrent_ensure_builds_once_and_shares_engine(weights: Path, tmp_path: Path) -> None:
    """같은 저장 위치에서 두 프로세스가 같은 엔진을 준비하면 빌드는 한 번, 기다린 쪽은 먼저 만든 엔진을 쓴다(S15P31A101-246 리뷰)."""
    import threading

    root = tmp_path / "engines"
    inside, go = threading.Event(), threading.Event()

    class Slow(FakeBuilder):
        def __call__(self, model, cfg, target, work):
            inside.set()
            assert go.wait(10)
            return super().__call__(model, cfg, target, work)
    builder = Slow()
    out: list = []
    a = threading.Thread(target=lambda: out.append(_ensure(weights, root, H100, builder)))
    a.start()
    assert inside.wait(10)
    b = threading.Thread(target=lambda: out.append(_ensure(weights, root, H100, builder)))
    b.start()
    b.join(0.5)
    assert b.is_alive()  # 빌드가 끝날 때까지 기다린다
    go.set()
    a.join(10)
    b.join(10)
    assert len(builder.calls) == 1 and [r.built for r in out] == [True, False] and out[0].engine == out[1].engine
    assert not out[1].quarantined


def test_failed_build_leaves_no_usable_cache(weights: Path, tmp_path: Path) -> None:
    root = tmp_path / "engines"
    with pytest.raises(ec.BuildError, match="RuntimeError: out of memory"):
        _ensure(weights, root, H100, FakeBuilder(fail=RuntimeError("out of memory")))
    model = ec.model_ref(weights)
    path = ec.engine_path(root, model, CFG, H100)
    assert ec.inspect_cache(path, model, CFG, H100).state == "missing"
    assert list(path.parent.iterdir()) == []  # 임시 빌드 폴더도 남지 않는다


# --------------------------------------------------------------------------- CLI 종료 코드


def test_cli_exit_codes(weights: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "engines"
    common = ["--weights", str(weights), "--cache-root", str(root)]

    def no_gpu():
        raise ec.NoGpuError("CUDA GPU 가 없다")

    assert ec.main(["ensure", "--weights", str(tmp_path / "x.pt"), "--cache-root", str(root)]) == ec.EXIT_WEIGHTS
    assert "가중치 없음" in capsys.readouterr().err
    assert ec.main(["ensure", *common], detect=no_gpu, build=FakeBuilder()) == ec.EXIT_NO_GPU
    assert "CUDA GPU" in capsys.readouterr().err
    assert ec.main(["ensure", *common, "--imgsz", "1000"], detect=lambda: H100, build=FakeBuilder()) == ec.EXIT_USAGE
    capsys.readouterr()
    assert ec.main(["ensure", *common], detect=lambda: H100, build=FakeBuilder(fail=RuntimeError("x"))) == ec.EXIT_BUILD
    capsys.readouterr()
    assert ec.main(["ensure", *common], detect=lambda: H100, build=FakeBuilder()) == ec.EXIT_OK
    built = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert built.is_file()
    assert ec.main(["plan", *common, "--gpu-arch", "sm_90", "--tensorrt", "11.4.0.106", "--ultralytics", "8.4.178", "--torch", TORCH, "--modelopt", "0.47.0"]) == 0
    assert capsys.readouterr().out.strip() == f"valid\t{built}"
    assert ec.main(["plan", *common, "--gpu-arch", "sm_86", "--tensorrt", "11.4.0.106", "--ultralytics", "8.4.178", "--torch", TORCH, "--modelopt", "0.47.0"]) == 0
    assert capsys.readouterr().out.startswith("missing\t")


def test_unwritable_cache_root(weights: Path, tmp_path: Path) -> None:
    blocker = tmp_path / "engines"
    blocker.write_text("파일이라 폴더를 만들 수 없다")
    with pytest.raises(ec.CacheDirError):
        _ensure(weights, blocker, H100, FakeBuilder())
