"""deploy/scripts 시험 공용 (S15P31A101-183). 스크립트는 패키지가 아니라 파일 경로로 불러온다. GPU·RunPod 를 쓰지 않는다."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "deploy" / "scripts"


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"deploy_scripts_{name}", SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gs():
    """deploy/scripts/gpu_session1.py"""
    return load_script("gpu_session1")


@pytest.fixture
def g3():
    """deploy/scripts/crash_recovery_g3.py"""
    return load_script("crash_recovery_g3")


@pytest.fixture
def live():
    """deploy/scripts/session1_live.py"""
    return load_script("session1_live")


@pytest.fixture
def wt():
    """deploy/scripts/wait_tier0_ready.py"""
    return load_script("wait_tier0_ready")


@pytest.fixture
def bs():
    """deploy/scripts/simai_bootstrap.py (S15P31A101-205)"""
    return load_script("simai_bootstrap")


@pytest.fixture
def pc():
    """deploy/scripts/pod_check.py"""
    return load_script("pod_check")


@pytest.fixture
def gp():
    """deploy/scripts/session1_gpu_priority.py"""
    return load_script("session1_gpu_priority")


@pytest.fixture(autouse=True)
def restore_environ():
    """gpu_session1.scrub_env 가 이 프로세스의 비밀 환경 변수를 지우므로 시험마다 되돌린다."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture
def xc():
    """deploy/scripts/exp_image_check.py (S15P31A101-207)"""
    return load_script("exp_image_check")


@pytest.fixture
def ej():
    """deploy/scripts/exp_job.py (S15P31A101-207 리뷰 M2)"""
    return load_script("exp_job")
