"""services 진입점 골격이 ai/ 작업 디렉터리에서 실행되는지 확인한다."""

import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("service", ["tier0", "worker"])
def test_entrypoint_help(service):
    result = subprocess.run(
        [sys.executable, "-m", f"services.{service}", "--help"], cwd=AI_DIR, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
