"""시험 함수를 새 Python 프로세스에서 시간 제한을 두고 돌린다 (LAPACK 멈춤 회귀, S15P31A101-245).

Linux OpenBLAS 의 SVD 는 ±inf 가 든 행렬에서 끝나지 않는다. C 호출 안에서 멈추므로 같은 프로세스에서는 끊을 수 없고
pytest-timeout 은 의존성에 없다. 시험 파일을 새 프로세스에서 경로로 불러 그 함수를 부르고, 제한 시간을 넘기면 실패다.
"""

from __future__ import annotations

import os
import subprocess
import sys

# 프로세스 시작·import 를 넉넉히 덮는다. 계산 자체는 1초 안쪽이고 멈추면 끝나지 않는다
TIMEOUT_S = 60.0

_CODE = """
import importlib.util, sys
path, name = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("_bounded_target", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
getattr(module, name)()
"""


def run_bounded(path: str, name: str, timeout: float = TIMEOUT_S) -> None:
    """path 시험 파일의 함수 name 을 새 프로세스에서 부른다. 예외로 끝나거나 timeout 초 안에 끝나지 않으면 실패."""
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        out = subprocess.run(
            [sys.executable, "-c", _CODE, str(path), name],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise AssertionError(f"{name}: {timeout:.0f}초 안에 끝나지 않았다 (LAPACK 멈춤)") from None
    assert out.returncode == 0, f"{name} 실패 (rc={out.returncode})\n{out.stdout[-2000:]}\n{out.stderr[-4000:]}"
