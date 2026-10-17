"""보정 문서의 계약 형식 검사: `contracts/providers/calibration.schema.json` 직접 검증 (명세 21.2 계약 우선).

스키마가 형식의 정본이므로 규칙을 코드로 옮기지 않고 스키마 파일을 jsonschema 로 그대로 검증한다. 스키마를 고치면
버전 저장소의 저장 전 검사가 코드 수정 없이 따라간다. `$ref`(common/defs) 는 `providers.contract` 의 레지스트리가
각 스키마의 `$id` 로 해석한다.

- jsonschema 는 extras `calibration` 에 있다(기본 설치에는 없다. Risk Engine 단독 설치 조건). 없으면 이 패키지
  (`simai_ai.calibration`)를 import 할 때 설치 방법을 적은 ImportError 를 낸다. 형식 검사 없이 보정을 저장하는
  경로를 남기지 않기 위해 첫 사용 때가 아니라 import 때 실패한다.
- contracts/ 위치는 `providers.contract.contracts_dir()` 규칙을 따른다(저장소 안에서 찾고, 없으면
  환경 변수 SIMAI_CONTRACTS_DIR).
- 기하 검사(`providers.contract.check_calibration_geometry`)는 스키마 형식이 맞다는 전제로 회전·내부 파라미터·카메라
  중심을 계산한다. 이 검사가 실패한 문서에는 기하 검사를 돌리지 않는다. NaN·무한대는 스키마로 막을 수 없어 기하
  검사가 본다.
"""

from __future__ import annotations

from typing import Any

INSTALL_HINT = (
    "simai_ai.calibration 은 보정 문서를 계약 스키마로 검증하려고 jsonschema 를 쓴다. "
    "extras calibration 으로 설치한다: ai/ 에서 `uv sync --extra calibration` "
    "(또는 `pip install 'simai-ai[calibration]'`). Risk Engine 만 쓰는 설치에는 필요 없다"
)

try:
    import jsonschema  # noqa: F401
    import referencing  # noqa: F401
except ImportError as e:
    raise ImportError(INSTALL_HINT) from e

from simai_ai.providers.contract import check_calibration_schema  # noqa: E402


def check_calibration_structure(doc: Any) -> list[str]:
    """보정 문서를 계약 스키마로 검증한다. 위반 메시지 목록(비면 통과)."""
    return check_calibration_schema(doc)
