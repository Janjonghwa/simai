"""공용 픽스처: 시험용 H.264 접근 단위(메모리에서 인코딩, 파일로 남기지 않음)."""

from __future__ import annotations

import pytest
from fake_camera import encode_frames


@pytest.fixture(scope="session")
def frames_250() -> list[tuple[bytes, bool]]:
    """25 fps 10초, 키프레임 1초(25프레임)마다. 막대 무늬(사람 이미지 없음)."""
    return encode_frames(250, fps=25, gop=25)
