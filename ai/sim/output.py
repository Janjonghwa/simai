"""결정적 직렬화. 같은 입력이면 바이트 단위로 같은 파일이 나와야 한다.

- 실수는 자리수를 고정해 반올림하고 -0.0 은 0.0 으로 바꾼다.
- 키 순서는 코드에 쓴 순서 그대로, 줄바꿈은 LF, 인코딩은 UTF-8.
- 벽시계 시각·절대 경로·난수 시드 없는 값은 출력에 넣지 않는다.
"""

import hashlib
import json
from typing import Any

PX = 3
"""픽셀 좌표 소수 자리"""
CONF = 3
M = 5
"""미터 소수 자리 (0.01 mm)"""
MAT = 9
"""R, K 소수 자리"""


def r(x: float, digits: int) -> float:
    return round(x, digits) + 0.0


def rv(v: tuple[float, ...] | list[float], digits: int) -> list[float]:
    return [r(x, digits) for x in v]


def json_bytes(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def jsonl_bytes(rows: list[Any]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows).encode("utf-8")


def sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_sha256(obj: Any) -> str:
    """키 정렬·공백 없는 JSON 의 해시. 파일 서식과 무관하게 같은 내용이면 같은 값이다."""
    return sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
