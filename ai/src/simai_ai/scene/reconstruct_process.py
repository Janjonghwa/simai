"""취소·시간 제한을 지키는 단계 프로세스. 자식의 GPU 상한도 워커와 같다."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from pathlib import Path

from .reconstruct import ReconstructionError


class ProcessStages:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)
        # 모델/허브의 묵시적인 다운로드를 막는다. 가중치는 bootstrap 이 준비한다.
        self.env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", YOLO_OFFLINE="true", YOLO_AUTOINSTALL="false")

    def run(self, stage: str, root: Path, *, timeout_s: float, cancelled: threading.Event) -> None:
        if cancelled.is_set():
            raise ReconstructionError("CANCELLED", "작업 취소")
        started = time.monotonic()
        with (root / f"{stage}.log").open("wb") as log:
            try:
                proc = subprocess.Popen([sys.executable, "-m", "simai_ai.scene.reconstruct_tasks", stage, str(root)],
                    stdout=log, stderr=subprocess.STDOUT, env=self.env, start_new_session=os.name != "nt")
            except OSError as e:
                raise ReconstructionError(f"STATIC_{stage.upper()}_TOOL_FAILED", type(e).__name__) from e
            try:
                while proc.poll() is None:
                    if cancelled.is_set():
                        raise ReconstructionError("CANCELLED", "작업 취소")
                    if time.monotonic() - started >= timeout_s:
                        raise ReconstructionError(f"STATIC_{stage.upper()}_TIMEOUT", "단계 시간 초과")
                    cancelled.wait(.05)
                if proc.returncode:
                    error_path = root / f"{stage}.error.json"
                    if error_path.is_file():
                        from .reconstruct import read_json
                        error = read_json(error_path)
                        raise ReconstructionError(error["code"], error["message"])
                    raise ReconstructionError(f"STATIC_{stage.upper()}_TOOL_FAILED", f"종료 코드 {proc.returncode}; {stage}.log 확인")
            finally:
                # bake 의 자식 texrecon 도 함께 종료한다. 정상 종료 후 남은 자식도 정리한다.
                if os.name != "nt":
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                elif proc.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
                proc.wait(timeout=10)
