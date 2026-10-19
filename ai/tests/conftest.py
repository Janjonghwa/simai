"""시험 전체 공용 (S15P31A101-198): Windows WinError 10014 일시 오류 보정(winsock_retry/sitecustomize.py 설명)을 시험 프로세스와
시험이 띄우는 자식 Python 프로세스(PYTHONPATH 의 sitecustomize)에 건다."""

import importlib.util
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    _SHIM = Path(__file__).with_name("winsock_retry")
    _spec = importlib.util.spec_from_file_location("simai_winsock_retry", _SHIM / "sitecustomize.py")
    assert _spec is not None and _spec.loader is not None
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))  # 불러오면 install() 한다
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(_SHIM), os.environ.get("PYTHONPATH")) if p)
