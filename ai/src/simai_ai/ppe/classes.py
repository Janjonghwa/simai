"""PPE 검출 클래스와 체크포인트 이름 검사 (명세 5.5 모델).

체크포인트는 `HELMET, NO_HELMET, VEST, NO_VEST` 순서로 저장돼 있고, 명세 이름은 `helmet_head, no_helmet_head,
vest_torso, no_vest_torso` 다. 순서·의미가 같으므로 불러올 때 정확히 두 목록 중 하나와 순서까지 같은지 검사하고,
출력 이름은 늘 명세 이름으로 쓴다(가중치는 바꾸지 않는다). 그 밖의 이름·순서면 시작하지 않는다.
클래스 이름 처리 방식은 잠정이다(SimAI 준비 묶음 preparation-decision `class_name_mapping`, 확정 전).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math

CHECKPOINT_NAMES = ("HELMET", "NO_HELMET", "VEST", "NO_VEST")
SPEC_NAMES = ("helmet_head", "no_helmet_head", "vest_torso", "no_vest_torso")
# 인덱스 → (명세 이름, 장비, 그 검출이 지지하는 상태)
CLASS_MAP = (
    ("helmet_head", "helmet", "WORN"),
    ("no_helmet_head", "helmet", "NOT_WORN"),
    ("vest_torso", "vest", "WORN"),
    ("no_vest_torso", "vest", "NOT_WORN"),
)
ACCEPTED_NAMES = {CHECKPOINT_NAMES: "CHECKPOINT_NAMES_RENAMED_TO_SPEC", SPEC_NAMES: "SPEC_NAMES_AS_IS"}


@dataclass(frozen=True)
class Candidate:
    """검출 후보 하나. 좌표는 원본 해상도 픽셀 xyxy. 사람 연결 전이라 world_id 가 없다."""

    class_id: int
    class_name: str
    item: str
    candidate_state: str
    confidence: float
    xyxy: tuple[float, float, float, float]


def validate_names(names: Mapping[int, str] | Sequence[str]) -> tuple[str, ...]:
    """체크포인트 클래스 이름이 받아들이는 두 목록 중 하나와 순서까지 같은지 본다. 다르면 ValueError."""
    if isinstance(names, Mapping):
        if sorted(names) != list(range(len(names))):
            raise ValueError(f"PPE 클래스 번호가 0..n-1 이 아니다: {sorted(names)!r}")
        ordered = tuple(names[i] for i in range(len(names)))
    else:
        ordered = tuple(names)
    if ordered not in ACCEPTED_NAMES:
        raise ValueError(f"PPE 클래스 이름·순서가 다르다(명세 5.5): {list(ordered)!r}")
    return ordered


def normalize_candidates(xyxy: Sequence[Sequence[float]], scores: Sequence[float], classes: Sequence[float],
                         width: int, height: int) -> list[Candidate]:
    """추론 원시 출력을 후보로 바꾼다. 형식이 틀리면 ValueError(조용히 고치지 않는다). 검출 없음은 빈 목록이다."""
    if width <= 0 or height <= 0:
        raise ValueError("프레임 크기가 틀렸다")
    out = []
    for box, score, cls in zip(xyxy, scores, classes, strict=True):
        if len(box) != 4 or not all(math.isfinite(float(v)) for v in (*box, score, cls)):
            raise ValueError("검출 값이 수가 아니거나 형식이 틀렸다")
        if int(cls) != cls or int(cls) not in range(len(CLASS_MAP)) or not 0 <= score <= 1:
            raise ValueError("클래스 번호나 confidence 가 틀렸다")
        x1, y1, x2, y2 = (float(v) for v in box)
        if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
            raise ValueError("상자가 원본 이미지 밖이다")
        name, item, state = CLASS_MAP[int(cls)]
        out.append(Candidate(int(cls), name, item, state, float(score), (x1, y1, x2, y2)))
    return out
