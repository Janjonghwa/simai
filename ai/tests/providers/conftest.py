"""Provider 시험 공용 픽스처. 예시는 저장소의 contracts/examples 를 읽는다(고치지 않음)."""

import pytest

from simai_ai.providers.testing import load_example


@pytest.fixture
def calibration_doc():
    return load_example("calibration.json")


@pytest.fixture
def tracks_doc():
    return load_example("world_tracks.json")
