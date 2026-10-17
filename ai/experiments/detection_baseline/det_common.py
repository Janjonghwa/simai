"""G0 검출 기준선(S15P31A101-18) 스크립트 공용: 재현 정보 수집, 백분위, 결과 기록.

같은 폴더의 스크립트가 `import det_common` 으로 쓴다(스크립트를 직접 실행하면 이 폴더가 sys.path 첫 항목이 된다).
"""
from __future__ import annotations

import hashlib
import json
import math
import platform
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
REPO = AI_DIR.parent
RUNS = HERE / "runs"  # .gitignore 의 runs/ 패턴으로 커밋되지 않는다
RECORDINGS = AI_DIR / "experiments" / "recordings"
COCO_DIR = RECORDINGS / "coco"
COCO_ANN = COCO_DIR / "annotations" / "person_keypoints_val2017.json"
COCO_IMG = COCO_DIR / "val2017"
WEIGHTS = RECORDINGS / "weights"  # ultralytics 가 받은 가중치를 여기 둔다(커밋 안 함)

SIZES = ("n", "s", "m", "l", "x")
CAM_W, CAM_H = 2304, 1296  # C110 stream1


def now_ms() -> int:
    """UTC epoch 밀리초."""
    return time.time_ns() // 1_000_000


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def pct(ts: list[float]) -> dict[str, float]:
    """지연 표본(ms)의 p50/p95/p99. 최근접 순위(nearest-rank) 방식."""
    s = sorted(ts)
    n = len(s)

    def q(p: float) -> float:
        return round(s[min(n - 1, max(0, int(-(-p * n // 100)) - 1))], 2)

    return {"p50": q(50), "p95": q(95), "p99": q(99), "mean": round(sum(s) / n, 2), "n": n}


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=30, cwd=REPO).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def git_info() -> dict:
    return {
        "commit": _run(["git", "rev-parse", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        # 측정 스크립트(이 폴더의 *.py)가 커밋과 다르면 true. true인 Run은 리뷰 근거로 쓰지 않는다
        "dirty_worktree": bool(_run(["git", "status", "--porcelain", "--", "ai/experiments/detection_baseline/*.py"])),
    }


def gpu_info() -> dict:
    q = "name,driver_version,pstate,power.limit,power.max_limit,clocks.max.sm,temperature.gpu,memory.total"
    row = _run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader"])
    keys = q.split(",")
    info = dict(zip(keys, [v.strip() for v in row.split(",")])) if row else {}
    # 노트북 전원: Win32_Battery.BatteryStatus 2 = AC 연결. 배터리 구동이면 GPU 클럭이 낮아 지연 비교에 쓰지 않는다
    if sys.platform == "win32":
        bat = _run(["powershell", "-NoProfile", "-Command",
                    "(Get-CimInstance Win32_Battery | Select-Object -First 1 BatteryStatus,EstimatedChargeRemaining | ConvertTo-Json -Compress)"])
        try:
            b = json.loads(bat) if bat else {}
            info["ac_power"] = b.get("BatteryStatus") == 2
            info["battery_pct"] = b.get("EstimatedChargeRemaining")
        except json.JSONDecodeError:
            info["ac_power"] = None
    return info


def env_info() -> dict:
    import torch
    import ultralytics

    freeze = _run(["uv", "pip", "freeze", "--python", sys.executable])
    lock = AI_DIR / "uv.lock"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "torch": torch.__version__,
        "torch_cuda": torch.version.cuda,
        "cudnn": torch.backends.cudnn.version(),
        "ultralytics": ultralytics.__version__,
        "gpu": gpu_info(),
        "provider": "LOCAL",
        # 측정 venv 는 ai/uv.lock 이 아니라 `uv pip install` 로 만든다(README 설치 절). 둘 다 해시로 남긴다
        "uv_lock_sha256": sha256_file(lock) if lock.exists() else None,
        "pip_freeze_sha256": sha256_bytes(freeze.encode()) if freeze else None,
        "pip_freeze": freeze.splitlines(),
    }


def weights_path(size: str) -> Path:
    return WEIGHTS / f"yolo26{size}-pose.pt"


def load_model(size: str):
    """가중치를 recordings/weights 에 받아 둔 뒤 불러온다. (모델, 가중치 기록) 반환."""
    from ultralytics import YOLO

    WEIGHTS.mkdir(parents=True, exist_ok=True)
    p = weights_path(size)
    if not p.exists():
        from ultralytics.utils.downloads import attempt_download_asset

        attempt_download_asset(str(p))
    return YOLO(str(p)), {"file": p.name, "sha256": sha256_file(p)}


def engine_path(size: str, mode: str, imgsz: int, max_batch: int = 4) -> Path:
    return WEIGHTS / "engines" / f"yolo26{size}-pose_{mode}_{imgsz}_b{max_batch}_fp16.engine"


def engine_profile(imgsz: int, max_batch: int = 4) -> dict[str, list[int]]:
    """TensorRT 동적 프로파일. opt 는 2304×1296 을 rect 레터박스한 실제 입력(batch max_batch), max 는 imgsz 정사각형.
    ultralytics 기본값(opt = imgsz 정사각형, max = imgsz 의 2배)은 6GB 에서 x·1280 빌드가 메모리 부족으로 실패했다."""
    h = math.ceil(CAM_H * imgsz / CAM_W / 32) * 32
    return {"min": [1, 3, 32, 32], "opt": [max_batch, 3, h, imgsz], "max": [max_batch, 3, imgsz, imgsz]}


def load_backend(size: str, mode: str, imgsz: int, backend: str, max_batch: int = 4):
    """(모델, 가중치 기록). mode: nms | e2e. engine 은 TensorRT FP16(dynamic, 최대 batch max_batch)을
    recordings/weights/engines/ 에 한 번 내보내 두고 다시 쓴다. 내보내기는 export_engine.py 별도 프로세스로 한다."""
    model, w = load_model(size)
    if backend == "pt":
        return model, w
    eng = engine_path(size, mode, imgsz, max_batch)
    if not eng.exists():
        t0 = time.perf_counter()
        subprocess.run([sys.executable, str(HERE / "export_engine.py"), "--size", size, "--mode", mode,
                        "--imgsz", str(imgsz), "--max-batch", str(max_batch)], check=True)
        w["export_s"] = round(time.perf_counter() - t0, 1)
    from ultralytics import YOLO

    return YOLO(str(eng), task="pose"), w | {"engine": eng.name, "engine_sha256": sha256_file(eng),
                                             "trt_profile": engine_profile(imgsz, max_batch)}


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
