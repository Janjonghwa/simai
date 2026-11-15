"""시험 전체 공용 (S15P31A101-198): Windows WinError 10014 일시 오류 보정(winsock_retry/sitecustomize.py 설명)을 시험 프로세스와
시험이 띄우는 자식 Python 프로세스(PYTHONPATH 의 sitecustomize)에 건다."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    _SHIM = Path(__file__).with_name("winsock_retry")
    _spec = importlib.util.spec_from_file_location("simai_winsock_retry", _SHIM / "sitecustomize.py")
    assert _spec is not None and _spec.loader is not None
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))  # 불러오면 install() 한다
    os.environ["PYTHONPATH"] = os.pathsep.join(p for p in (str(_SHIM), os.environ.get("PYTHONPATH")) if p)


@pytest.fixture(autouse=True)
def _tier0_single_process_by_default(monkeypatch):
    """Tier 0 운영 기본은 검출·판정 프로세스 분리(-738)다. 시험은 단일 경로를 기본으로 둔다(시험마다 spawn 자식 기동을 피하고, 대역
    팩토리가 pickle 되지 않아도 되게). 분리 경로 시험은 SIMAI_TIER0_PROCESS_SPLIT=1·process_split=true 로 명시한다."""
    settings = sys.modules.get("services.tier0.settings")
    if settings is None:
        try:
            import services.tier0.settings as settings
        except ImportError:
            return
    monkeypatch.setattr(settings, "DEFAULT_PROCESS_SPLIT", False)
