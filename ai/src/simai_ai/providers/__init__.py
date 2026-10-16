"""WorldTrackProvider 등 입력 대체 계약 구현 (명세 20.4).

- `base`: FrameSource, CalibrationProvider, WorldTrackProvider 인터페이스
- `provenance`: Provider 출처 → 결과물의 input_provenance
- `files`: contracts 형식 파일을 읽는 참조 구현
- `contract`: 공통 계약 시험 검사 함수 (스키마 검증은 jsonschema, dev 그룹)
- `testing`: pytest 용 공통 계약 시험 묶음 (구현 시험이 상속)
"""

from simai_ai.providers.base import (
    CalibrationProvider,
    Detection2D,
    Frame,
    FrameSource,
    WorldTrackProvider,
)
from simai_ai.providers.files import FileCalibrationProvider, FileWorldTrackProvider
from simai_ai.providers.provenance import build_input_provenance, is_upper_bound, provenance_from_providers

__all__ = [
    "CalibrationProvider",
    "Detection2D",
    "FileCalibrationProvider",
    "FileWorldTrackProvider",
    "Frame",
    "FrameSource",
    "WorldTrackProvider",
    "build_input_provenance",
    "is_upper_bound",
    "provenance_from_providers",
]
