"""kanana-2-v-decide-3b-2611 벤치마크 실험 패키지 (S15P31A101-538).

- questions.py: 한국어 질문 스키마 정의 (A 위험 이벤트, B PPE, C 맵 크롭 선택) 및 입력 검증
- prepare_inputs.py: 로컬 세션 데이터·VDT 카메라 투영에서 벤치마크 입력 묶음 생성
- run_decide.py: 입력 묶음 → 모델(또는 CPU 가짜 모델) 실행 → 결과 JSON (확률·지연·GPU 메모리)
- compare.py: Cosmos3-Nano VLM·현재 PPE 판정(-519 R9)·기하 선명도 기준선과 확률·교정 곡선 비교
"""

from __future__ import annotations

from .questions import (
    CAMERA_IDS,
    CURRENT_PPE_PIPELINE_SPEC,
    RISK_RULE_CRITERIA,
    T1_V4_ALL_YOLO_SPEC,
    UPGRADE_LEVEL_CRITERIA,
    build_map_crop_questions,
    build_map_crop_state,
    build_ppe_questions,
    build_ppe_state,
    build_risk_event_questions,
    build_risk_event_state,
    combine_yolo_and_decide_ppe,
    noul_to_wear_state,
    score_to_proposed_severity,
    validate_questions_bundle,
    validate_system_one_payload,
)

__all__ = [
    "CAMERA_IDS",
    "CURRENT_PPE_PIPELINE_SPEC",
    "RISK_RULE_CRITERIA",
    "T1_V4_ALL_YOLO_SPEC",
    "UPGRADE_LEVEL_CRITERIA",
    "build_map_crop_questions",
    "build_map_crop_state",
    "build_ppe_questions",
    "build_ppe_state",
    "build_risk_event_questions",
    "build_risk_event_state",
    "combine_yolo_and_decide_ppe",
    "noul_to_wear_state",
    "score_to_proposed_severity",
    "validate_questions_bundle",
    "validate_system_one_payload",
]
