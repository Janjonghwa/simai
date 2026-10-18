"""재시작 복구 시험 공용 도구. Tier 0 흉내(Site)는 강제 종료 자식 프로세스와 함께 쓰려고 site_driver.py 에 둔다."""

import importlib.util
import sys
from pathlib import Path

import pytest

from simai_ai.risk import FixedClock

_spec = importlib.util.spec_from_file_location("recovery_site_driver", Path(__file__).with_name("site_driver.py"))
assert _spec is not None and _spec.loader is not None
site_driver = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = site_driver
_spec.loader.exec_module(site_driver)


@pytest.fixture
def sd():
    """site_driver 모듈(Site, journal_truth, assert_matches_journal, finding 등)."""
    return site_driver


@pytest.fixture
def clock():
    return FixedClock(site_driver.T0)
