"""PPE 판정: 전체 화면 4클래스 검출, Tier 0 keypoint 로 사람별 머리·몸통 일대일 연결, World ID 별 상태 누적 (명세 5.5, S15P31A101-491).

Tier 0 와 같은 GPU 인스턴스의 별도 PPE 프로세스가 쓴다. 이 패키지는 World 위치·ID 를 만들거나 바꾸지 않고(AGENTS 규칙 1),
검출 없음을 미착용으로 바꾸지 않으며, R9 이벤트를 내지 않는다(R9 는 Tier 0 Risk Engine, 7.2). 결과는 I1 `ppe_state` 로 보낸다(12.3).
torch·ultralytics 는 `detector.PpeDetector` 를 만들 때만 불러온다(연결·누적·메시지는 순수 Python).
"""

from __future__ import annotations

from .association import (
    FrameDetections,
    FrameObservations,
    PersonObservation,
    associate_frame,
)
from .classes import Candidate
from .message import build_ppe_state
from .state import PpeConfig, PpeJudge

__all__ = [
    "Candidate",
    "FrameDetections",
    "FrameObservations",
    "PersonObservation",
    "PpeConfig",
    "PpeJudge",
    "associate_frame",
    "build_ppe_state",
]
