"""공개 데이터 어댑터 시험 공용 설정.

- 합성 표본(`panoptic_mini/`)은 저장소에 있어 늘 돈다.
- 실제 데이터 시험은 marker `dataset` 이고, 시퀀스 디렉터리(`calibration_<seq>.json` 과
  `hdPose3d_stage1_coco19/` 이 있는 곳)를 환경 변수 SIMAI_PANOPTIC_DIR 로 줄 때만 돈다. 데이터는 저장소 밖에 둔다.
  `uv run pytest -m dataset` 로 실제 데이터 시험만 돌린다.
"""

import os
from pathlib import Path

import pytest

PANOPTIC_DIR_ENV = "SIMAI_PANOPTIC_DIR"
PANOPTIC_MINI = Path(__file__).resolve().parent / "panoptic_mini"


def pytest_configure(config):
    config.addinivalue_line("markers", f"dataset: 실제 공개 데이터가 있어야 도는 시험 ({PANOPTIC_DIR_ENV})")


@pytest.fixture
def panoptic_mini():
    return PANOPTIC_MINI


@pytest.fixture(scope="session")
def panoptic_dir():
    value = os.environ.get(PANOPTIC_DIR_ENV)
    if not value:
        pytest.skip(f"{PANOPTIC_DIR_ENV} 가 없어 실제 데이터 시험을 건너뛴다")
    path = Path(value)
    if not path.is_dir():
        pytest.fail(f"{PANOPTIC_DIR_ENV}={value} 가 디렉터리가 아니다")
    return path
