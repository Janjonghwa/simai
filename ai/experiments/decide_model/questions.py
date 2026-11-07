"""kanana-2-v-decide-3b-2611 한국어 질문 설계 및 스키마 검증 (S15P31A101-538).

대상 모델(`kakaocorp/kanana-2-v-decide-3b-2611`)의 `model.system_one(state=..., questions=..., images=...)`
규약(`decision_kanana_v_decide.py`)에 맞춘 3종 질문 묶음:
- (A) 위험 이벤트(`risk_event`): `risk_type`(choice, R1~R9 실제 규칙 정의), `upgrade`(score, 유지·상향만), `person_in_zone`(noul)
- (B) PPE(`ppe`): `helmet`(noul), `vest`(noul) — `-519` R9 현재 PPE 판정 파이프라인과 직접 비교 가능하도록 변환 규칙 포함
- (C) 맵 크롭 선택(`map_crop`): `cleanest`(choice, cam_A~cam_D 중 반사·가림·흐림 없이 가장 선명한 시점)

깨면 안 되는 규칙(AGENTS.md):
- 규칙 1: Tier 0 가 위치·이벤트의 정본이며, 결정형 VLM 결과로 Tier 0 위치·규칙 판정을 덮어쓰지 않는다.
- 규칙 4: AI 는 제안만 한다. `upgrade` 질문에는 하향·해제·무시 선택지를 두지 않고 상향 의견(`proposed_severity`)만 낸다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from simai_ai.vlm.rationale import SEVERITY_RANK, check_proposal

MAX_IMAGES = 10
CAMERA_IDS: tuple[str, ...] = ("cam_A", "cam_B", "cam_C", "cam_D")

# 명세 7.2(`docs/spec/07-risk-rules.md`) 및 `ai/src/simai_ai/risk/rules/` 실제 규칙 정의 반영
RISK_RULE_CRITERIA: dict[str, str] = {
    "R1": (
        "위험 구역 진입: 사람의 확실한 위치(발바닥 접지점 또는 3D 경계 상자)가 "
        "바닥 다각형(GROUND_FOOTPRINT) 또는 3D 부피(VOLUME_3D) 위험·주의 구역 안에 들어감"
    ),
    "R2": (
        "개구부·가장자리 근접: 등록 작업면(OBB) 윗면 둘레 중 인접 낮은 면과 높이차(교실 시험 0.35m, 현장 2.0m 이상)가 있는 "
        "위험 가장자리까지 수평 거리 1.0m 이내로 접근함"
    ),
    "R3": (
        "고소 위치: 바닥보다 높은 등록 작업면(OBB) 위에 올라서 있으며 인접 낮은 면과 높이차(교실 시험 0.35m, 현장 2.0m 이상)가 있고 "
        "안전 난간이 없는 고소 작업 위치에 있음"
    ),
    "R4": (
        "낙상 의심: 1.0초 창 안 머리 높이가 0.8m 이상 급하강한 뒤 지면 기준 0.6m 이하 낮은 자세가 2.0초 이상 유지되거나, "
        "급하강 직후 1.0초 이내에 관측이 끊김"
    ),
    "R5": (
        "위험 구역·가장자리 진입 예측: 현재 이동 속도 0.4m/s 이상으로 이동 중이며 2.0초 이내에 "
        "위험 구역(R1) 또는 위험 가장자리(R2)에 진입할 것으로 선형 예측됨"
    ),
    "R6": (
        "위험 구역 내 관측 상실: 위험 구역 안에서 마지막으로 관측된 사람의 카메라 관측이 끊겨 UNKNOWN 상태로 전환되었으며 "
        "기존 경보를 유지하고 현장 확인이 필요함"
    ),
    "R7": (
        "구역 감시 능력 저하: 카메라 장애·가림 등으로 감시 구역의 2대 이상 다중 시점 관측 조건이 깨져 "
        "해당 구역의 감시 능력이 저하됨"
    ),
    "R8": (
        "이동 장비 근접: 이동 장비와 작업자 사이의 반경 또는 이동 방향 기준 안전 거리 이내로 근접함"
    ),
    "R9": (
        "작업 중 안전모 미착용: 안전모 필수(REQUIRED) 작업이 진행 중일 때 작업자의 머리 영역에서 "
        "안전모 미착용(NOT_WORN)이 지속 관측됨"
    ),
}

# AGENTS.md 규칙 4: 하향·해제 옵션 금지. 오직 현행 등급 유지(0) 또는 상향(1: HIGH 권고, 2: CRITICAL 권고)만 정의.
UPGRADE_LEVEL_CRITERIA: list[str] = [
    "현재 경보 등급 유지 (추가 상향 불필요 — 규칙 엔진이 판정한 현재 등급이 적절함)",
    "한 단계 상향 제안 (HIGH 상향 권고 — 위험 구역·가장자리 근접, 불안정한 자세, 시야 가림 등 추가 주의 요인 관측)",
    "즉시 최고 등급 상향 제안 (CRITICAL 상향 권고 — 즉각적인 추락·충돌·쓰러짐 등 중대 위험 관측)",
]

FORBIDDEN_DOWNWARD_KEYWORDS: tuple[str, ...] = (
    "하향",
    "경보 해제",
    "경보 끄기",
    "무시",
    "오경보 취소",
    "등급 낮춤",
    "정상 전환",
)

# T1-v4-ALL YOLO26x PPE 검출 가중치 명세 (`docs/data/weights.md` "T1-v4-ALL PPE 검출", `deploy/weights.json` `ppe` 묶음)
T1_V4_ALL_YOLO_SPEC: dict[str, Any] = {
    "name": "ppe-t1-v4-all-e08",
    "sets": ["ppe"],
    "path": "weights/ppe/T1-v4-ALL-seed20261021-epoch08.pt",
    "hf_repo": "simai-s15p31a101/simai-ppe-helmet-vest",
    "hf_revision": "36e3ebb10d44c38b46ab430d769b8d4334bdb5f4",
    "hf_file": "T1-v4-ALL-seed20261021-epoch08.pt",
    "sha256": "e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c",
    "size_bytes": 471779845,
    "model_version": "T1-v4-ALL-seed20261021-e08-e373a1048057",
    "env": "SIMAI_PPE_WEIGHTS",
    "checkpoint_classes": ["HELMET", "NO_HELMET", "VEST", "NO_VEST"],
    "spec_classes": ["helmet_head", "no_helmet_head", "vest_torso", "no_vest_torso"],
    "training_split_note": (
        "2026-11-04 H200 에서 내부 PPE 녹화 세 세션 전체(rec-20261028-023200z-ppe-training 포함)로 재학습(epoch 8 고정). "
        "학습에서 뺀 세션이 없으므로 과거 학습 세션 프레임에서 잰 지표는 파이프라인 확인용 상한(is_upper_bound=True)으로만 쓰고 "
        "제품 일반화 성능으로 쓰지 않는다(AGENTS.md 규칙 7)."
    ),
}

# -519 R9 현재 PPE 판정 파이프라인 명세 요약 (비교 기준선 문서화 및 코드 참조용)
CURRENT_PPE_PIPELINE_SPEC: dict[str, Any] = {
    "ticket_chain": ["S15P31A101-491", "S15P31A101-507", "S15P31A101-519"],
    "weights_spec": T1_V4_ALL_YOLO_SPEC,
    "modules": {
        "detector": "simai_ai.ppe.detector.PpeDetector",
        "association": "simai_ai.ppe.association.associate_frame",
        "temporal_state": "simai_ai.ppe.state.PpeJudge",
        "service_loop": "services.ppe.loop.PpeServiceLoop",
        "risk_store": "simai_ai.risk.ppe.PpeStateStore",
        "rule_r9": "simai_ai.risk.rules.r9_helmet.HelmetRequiredRule",
    },
    "input_source": "/dev/shm/simai-avatar-input.npz (Tier 0 공유 프레임 + 관측된 COCO17 2D 관절 + world_id/identity_revision)",
    "detector_spec": {
        "classes": {
            0: "helmet_head (WORN)",
            1: "no_helmet_head (NOT_WORN)",
            2: "vest_torso (WORN)",
            3: "no_vest_torso (NOT_WORN)",
        },
        "imgsz": 640,
        "conf": 0.5,
        "iou": 0.7,
        "rate_hz": 2.0,
    },
    "association_spec": (
        "Tier 0 관측(observed) COCO17 코·눈·귀(0..4)로 머리 영역, 어깨·엉덩이(5,6,11,12)로 상체 영역을 만들고 "
        "4클래스 박스를 1:1 매칭. 여러 사람 공유·반대 클래스 겹침·불명확 영역(not clear)일 때는 NOT_WORN 생성을 차단하고 "
        "테이블·손에 든 고아(orphan) 헬멧 박스는 버림."
    ),
    "temporal_window_spec": (
        "500ms 버킷 6개(3.0초 창). 다중 카메라 비대칭 결합(WORN 이 NONE 에 우선, 충돌 시 CONFLICT). "
        "WORN 확정: 최근 6버킷 중 5개 이상 + 마지막 버킷 WORN. "
        "NOT_WORN 확정: 최근 6버킷 모두(6/6) 깨끗한 NOT_WORN. "
        "확정 상태에서 반대 근거가 오면 즉시 반대 상태로 뒤집지 않고 UNKNOWN 으로 먼저 내림. 10초 미갱신 시 UNKNOWN 만료."
    ),
    "r9_rule_spec": (
        "진행 중 작업의 ppe_policy.helmet == REQUIRED 일 때만 평가. "
        "helmet == NOT_WORN 이면 CAUTION 경보 발생(dwell_s=0, cooldown_s=30.0). "
        "helmet == UNKNOWN 이면 새 이벤트를 열지 않고 기존 R9 이벤트만 PPE_UNKNOWN 으로 보류(HOLDING). "
        "vest 는 ppe_state 메시지로 뷰어에 표시하되 R9 경보 판정에는 넣지 않음."
    ),
}


def build_risk_event_questions() -> dict[str, dict[str, Any]]:
    """(A) 위험 이벤트 크롭(1~4장) + 텍스트/구조화 상태 → 위험 종류, 상향 의견, 구역 내 사람 존재 여부."""
    return {
        "risk_type": {
            "type": "choice",
            "instructions": (
                "첨부된 이벤트 카메라 크롭(1~4장)과 구조화 상태(규칙 ID, 구역, 지속시간, 트랙 수치)를 보고, "
                "현재 상황에 가장 부합하는 simai 위험 규칙(R1~R9)을 하나 선택하라."
            ),
            "criteria": dict(RISK_RULE_CRITERIA),
        },
        "upgrade": {
            "type": "score",
            "instructions": (
                "규칙 엔진이 판정한 현재 경보 등급을 기준으로, 첨부된 카메라 크롭에서 추가로 관측되는 위험 요인에 따라 "
                "경보 등급 상향 제안의 정도를 평가하라. 경보를 낮추거나 끄는 것은 허용되지 않으며 현행 유지 또는 상향만 제안한다."
            ),
            "criteria": list(UPGRADE_LEVEL_CRITERIA),
        },
        "person_in_zone": {
            "type": "noul",
            "instructions": (
                "첨부된 카메라 크롭과 구조화 상태를 보고, 대상 작업자(사람)가 실제로 지정된 위험 구역 내부 "
                "또는 위험 가장자리 경계 안에 위치하는지 판정하라."
            ),
            "criteria": {
                "false": "사람이 위험 구역·가장자리 밖에 있거나 화면에서 구역 내 사람이 관측되지 않음",
                "true": "사람이 위험 구역 내부 또는 위험 가장자리 경계 안에 실제로 위치함",
            },
        },
    }


def build_risk_event_state(event_meta: Mapping[str, Any]) -> dict[str, Any]:
    """위험 이벤트 메타데이터를 `system_one(state=..., state_format="json")` 입력용 정규화 사전으로 변환."""
    rule_id = str(event_meta.get("rule_id", "R1"))
    return {
        "task": "simai_risk_event_assessment",
        "event_id": str(event_meta.get("event_id", "evt-unknown")),
        "rule_id": rule_id,
        "rule_name": RISK_RULE_CRITERIA.get(rule_id, "").split(":", 1)[0],
        "current_severity": str(event_meta.get("severity", "CAUTION")),
        "confidence": str(event_meta.get("confidence", "SOLID")),
        "zone_id": event_meta.get("zone_id"),
        "duration_s": round(float(event_meta.get("duration_s", 0.0)), 2),
        "world_ids": list(event_meta.get("world_ids", [])),
        "metrics": dict(event_meta.get("metrics", {})),
        "cameras": list(event_meta.get("cameras", [])),
        "note": "Tier 0 위치·규칙 판정이 정본이며, 모델은 근거 확인 및 상향 의견(proposed_severity)만 제안한다.",
    }


def build_ppe_questions() -> dict[str, dict[str, Any]]:
    """(B) 사람 크롭(1~4장) → 안전모 착용 확률(noul), 안전조끼 착용 확률(noul)."""
    return {
        "helmet": {
            "type": "noul",
            "instructions": (
                "첨부된 작업자 크롭(1~4장)을 보고, 대상 작업자가 머리에 안전모(산업용 헬멧)를 올바르게 착용하고 있는지 판정하라. "
                "안전모를 손에 들고 있거나 테이블·바닥에 내려놓은 경우는 착용이 아니다."
            ),
            "criteria": {
                "false": "머리에 안전모를 착용하지 않음(맨머리, 일반 모자, 손에 들거나 옆에 놓인 안전모 포함)",
                "true": "머리에 안전모(헬멧)를 올바르게 착용함",
            },
        },
        "vest": {
            "type": "noul",
            "instructions": (
                "첨부된 작업자 크롭(1~4장)을 보고, 대상 작업자가 상체에 안전조끼(형광·반사 작업 조끼)를 올바르게 착용하고 있는지 판정하라."
            ),
            "criteria": {
                "false": "상체에 안전조끼를 착용하지 않음(일반 셔츠·외투만 착용)",
                "true": "상체에 안전조끼(형광·반사 조끼)를 올바르게 착용함",
            },
        },
    }


def build_ppe_state(ppe_meta: Mapping[str, Any]) -> dict[str, Any]:
    """PPE 평가 표본 메타데이터를 `system_one` state 사전으로 변환."""
    return {
        "task": "simai_ppe_check",
        "sample_id": str(ppe_meta.get("sample_id", "ppe-unknown")),
        "world_id": str(ppe_meta.get("world_id", "w-1")),
        "cameras": list(ppe_meta.get("cameras", [])),
        "scene_id": str(ppe_meta.get("scene_id", "")),
        "ppe_policy": dict(ppe_meta.get("ppe_policy", {"helmet": "REQUIRED", "vest": "OPTIONAL"})),
        "note": "첨부된 다중 카메라 인물 크롭에서 안전모(helmet)와 안전조끼(vest) 착용 여부를 판정하라.",
    }


def build_map_crop_questions(camera_ids: Sequence[str] = CAMERA_IDS) -> dict[str, dict[str, Any]]:
    """(C) 같은 벽면·표지판·액자 영역의 4카메라 크롭 → 가장 선명하고 깨끗한 카메라 선택(choice)."""
    if len(camera_ids) < 2:
        raise ValueError("map_crop 질문에는 최소 2대 이상의 카메라 ID 가 필요하다.")
    order_desc = ", ".join(f"{idx + 1}번째 이미지={cid}" for idx, cid in enumerate(camera_ids))
    criteria = {
        cid: (
            f"첨부된 {idx + 1}번째 크롭({cid})이 빛 반사·가림(장애물/사람)·초점 흐림·심한 사선 왜곡 없이 "
            "가장 선명하고 정면에 가까움"
        )
        for idx, cid in enumerate(camera_ids)
    }
    return {
        "cleanest": {
            "type": "choice",
            "instructions": (
                f"동일한 3D 벽면·표지판·액자·구조물 영역을 4대 카메라에서 투영해 잘라낸 크롭({order_desc})을 비교하여, "
                "반사·가림·흐림 없이 디지털 트윈 텍스처로 쓰기에 가장 선명한 카메라를 하나 선택하라."
            ),
            "criteria": criteria,
        }
    }


def build_map_crop_state(region_meta: Mapping[str, Any]) -> dict[str, Any]:
    """맵 크롭 선택 표본 메타데이터를 `system_one` state 사전으로 변환."""
    camera_ids = list(region_meta.get("camera_ids", CAMERA_IDS))
    return {
        "task": "simai_map_crop_selection",
        "region_id": str(region_meta.get("region_id", "region-unknown")),
        "region_label": str(region_meta.get("region_label", "")),
        "wall_id": region_meta.get("wall_id"),
        "center_world_m": list(region_meta.get("center_world_m", [])),
        "image_order": [
            {"image_index": idx + 1, "camera_id": cid}
            for idx, cid in enumerate(camera_ids)
        ],
        "note": "첨부 이미지 순서는 image_order 와 일대일 대응한다. 가장 선명하고 가림·반사가 없는 시점을 고르라.",
    }


def score_to_proposed_severity(
    current_severity: str,
    upgrade_answer: Mapping[str, Any],
    *,
    high_threshold: float = 0.80,
    critical_threshold: float = 1.40,
) -> tuple[str | None, str | None]:
    """`upgrade`(score 0..2) 응답을 `proposed_severity` 로 변환하고 상향 전용 규칙(`check_proposal`)을 강제한다.

    - score 기대값 >= critical_threshold (또는 레벨 2 확률이 최대이고 >= 0.5) → 후보 "CRITICAL"
    - score 기대값 >= high_threshold (또는 레벨 1+2 확률 합 >= 0.55) → 후보 "HIGH"
    - 그 외 → 후보 None (현재 등급 유지)
    - 마지막으로 `simai_ai.vlm.rationale.check_proposal(current_severity, candidate)` 를 통과시켜
      현재 등급보다 높지 않으면 `(None, "NOT_UPWARD")` 로 버린다(AGENTS.md 규칙 4).
    """
    if current_severity not in SEVERITY_RANK:
        raise ValueError(f"알 수 없는 심각도 등급: {current_severity!r}")
    score_val = float(upgrade_answer.get("score", 0.0))
    probs = upgrade_answer.get("probabilities") or {}
    p1 = float(probs.get("1", 0.0))
    p2 = float(probs.get("2", 0.0))

    candidate: str | None = None
    if score_val >= critical_threshold or p2 >= 0.50:
        candidate = "CRITICAL"
    elif score_val >= high_threshold or (p1 + p2) >= 0.55:
        candidate = "HIGH"

    return check_proposal(current_severity, candidate)


def noul_to_wear_state(
    prob_true: float,
    *,
    worn_threshold: float = 0.60,
    not_worn_threshold: float = 0.40,
) -> str:
    """`noul` 착용 확률(0.0~1.0)을 `-519` R9 계약의 `WearState`(`WORN` | `NOT_WORN` | `UNKNOWN`)로 변환한다.

    - `prob_true >= worn_threshold` → `"WORN"`
    - `prob_true <= not_worn_threshold` → `"NOT_WORN"`
    - 중간 불확실 구간(`not_worn_threshold < prob_true < worn_threshold`) → `"UNKNOWN"` (모르면 비운다 / 판정 보류)
    """
    if not (0.0 <= worn_threshold <= 1.0 and 0.0 <= not_worn_threshold <= 1.0):
        raise ValueError("임계값은 0.0~1.0 범위여야 한다.")
    if not_worn_threshold > worn_threshold:
        raise ValueError("not_worn_threshold 는 worn_threshold 이하이어야 한다.")
    p = float(prob_true)
    if p >= worn_threshold:
        return "WORN"
    if p <= not_worn_threshold:
        return "NOT_WORN"
    return "UNKNOWN"


def combine_yolo_and_decide_ppe(
    yolo_state: str,
    yolo_confidence: float | None,
    decide_prob_worn: float,
    *,
    yolo_low_conf_threshold: float = 0.65,
    decide_not_worn_threshold: float = 0.40,
) -> tuple[str, str]:
    """[YOLO26x(기준) + decide-3b 결합] 판정 규칙 (AGENTS.md 규칙 4: 위험 쪽으로만 보조).

    1. YOLO26x 가 `NOT_WORN`(NO_HELMET / NO_VEST)으로 판정한 경우:
       - `decide-3b` 확률이 아무리 높아도 절대 `WORN` 이나 `UNKNOWN` 으로 뒤집지 못한다 (`("NOT_WORN", "YOLO_NOT_WORN_LOCKED")`).
    2. YOLO26x 가 `UNKNOWN`(검출 없음·가림·교차 충돌)인 경우:
       - `decide_prob_worn <= decide_not_worn_threshold`(미착용 확신)일 때만 위험 방향인 `"NOT_WORN"` 으로 보조한다
         (`("NOT_WORN", "DECIDE_ASSIST_NOT_WORN_FROM_UNKNOWN")`).
       - 그 외에는 안전 방향(`WORN`)으로 임의 승격하지 않고 `"UNKNOWN"` 을 유지한다 (`("UNKNOWN", "YOLO_UNKNOWN_KEPT")`).
    3. YOLO26x 가 `WORN` 이지만 저신뢰(`yolo_confidence < yolo_low_conf_threshold`)인 경우:
       - `decide_prob_worn <= decide_not_worn_threshold` 이면 위험 방향인 `"NOT_WORN"` 으로 상향 보조한다
         (`("NOT_WORN", "DECIDE_OVERRIDE_LOW_CONF_WORN_TO_NOT_WORN")`).
       - 그 외에는 `"WORN"` 을 유지한다 (`("WORN", "YOLO_WORN_KEPT")`).
    """
    if yolo_state not in ("WORN", "NOT_WORN", "UNKNOWN"):
        raise ValueError(f"알 수 없는 yolo_state: {yolo_state!r}")
    p = float(decide_prob_worn)

    if yolo_state == "NOT_WORN":
        return "NOT_WORN", "YOLO_NOT_WORN_LOCKED"

    if yolo_state == "UNKNOWN":
        if p <= decide_not_worn_threshold:
            return "NOT_WORN", "DECIDE_ASSIST_NOT_WORN_FROM_UNKNOWN"
        return "UNKNOWN", "YOLO_UNKNOWN_KEPT"

    # yolo_state == "WORN"
    if yolo_confidence is not None and float(yolo_confidence) < yolo_low_conf_threshold and p <= decide_not_worn_threshold:
        return "NOT_WORN", "DECIDE_OVERRIDE_LOW_CONF_WORN_TO_NOT_WORN"
    return "WORN", "YOLO_WORN_KEPT"


def validate_question_schema(qid: str, question: Mapping[str, Any]) -> None:
    """`kakaocorp/kanana-2-v-decide-3b-2611` 의 `validate_question` 규칙 + simai 상향 전용 규칙을 검증한다."""
    if not isinstance(qid, str) or not qid.strip():
        raise ValueError("질문 ID 는 비어 있지 않은 문자열이어야 한다.")
    if not isinstance(question, Mapping):
        raise ValueError(f"질문 '{qid}' 는 매핑 객체여야 한다.")

    extra_keys = set(question) - {"type", "instructions", "criteria"}
    if extra_keys:
        raise ValueError(f"질문 '{qid}' 에 허용되지 않은 키가 있다: {sorted(extra_keys)}")

    qtype = question.get("type")
    if qtype not in ("choice", "noul", "score"):
        raise ValueError(f"질문 '{qid}' 의 type 은 'choice', 'noul', 'score' 중 하나여야 한다: {qtype!r}")

    instructions = question.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError(f"질문 '{qid}' 의 instructions 는 비어 있지 않은 한국어 문자열이어야 한다.")

    criteria = question.get("criteria")
    if qtype == "choice":
        if not isinstance(criteria, Mapping) or len(criteria) < 2:
            raise ValueError(f"choice 질문 '{qid}' 의 criteria 는 2개 이상의 선택지를 가진 사전이어야 한다.")
        for key, desc in criteria.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"choice 질문 '{qid}' 의 선택지 키는 비어 있지 않은 문자열이어야 한다.")
            if not isinstance(desc, str) or not desc.strip():
                raise ValueError(f"choice 질문 '{qid}' 의 선택지 '{key}' 설명이 비어 있다.")
    elif qtype == "noul":
        if criteria is not None:
            if not isinstance(criteria, Mapping) or set(criteria) != {"false", "true"}:
                raise ValueError(f"noul 질문 '{qid}' 의 criteria 는 None 또는 {{'false': ..., 'true': ...}} 이어야 한다.")
            for key in ("false", "true"):
                if not isinstance(criteria[key], str) or not criteria[key].strip():
                    raise ValueError(f"noul 질문 '{qid}' 의 criteria['{key}'] 는 비어 있지 않은 문자열이어야 한다.")
    elif qtype == "score":
        if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)):
            raise ValueError(f"score 질문 '{qid}' 의 criteria 는 리스트/시퀀스여야 한다.")
        if not (2 <= len(criteria) <= 10):
            raise ValueError(f"score 질문 '{qid}' 의 criteria 길이는 2~10 이어야 한다(현재 {len(criteria)}).")
        for idx, item in enumerate(criteria):
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"score 질문 '{qid}' 의 criteria[{idx}] 는 비어 있지 않은 문자열이어야 한다.")

    # AGENTS.md 규칙 4: upgrade 질문에 하향·경보 해제 문구가 들어가면 즉시 거부
    if qid == "upgrade":
        joined = " ".join([instructions, *(criteria if isinstance(criteria, Sequence) else [])])
        for kw in FORBIDDEN_DOWNWARD_KEYWORDS:
            if kw in joined:
                raise ValueError(f"upgrade 질문에 하향/해제 관련 금지어('{kw}')가 포함되어 있다(AGENTS.md 규칙 4 위반).")


def validate_questions_bundle(questions: Mapping[str, Mapping[str, Any]]) -> None:
    """질문 묶음 전체를 검증한다."""
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError("questions 는 비어 있지 않은 사전이어야 한다.")
    for qid, q in questions.items():
        validate_question_schema(qid, q)


def validate_system_one_payload(
    state: Any,
    questions: Mapping[str, Mapping[str, Any]],
    images: Sequence[Any] | None = None,
    state_format: str | None = None,
) -> None:
    """`model.system_one(state=..., questions=..., images=..., state_format=...)` 호출 전 입력 유효성을 검증한다."""
    validate_questions_bundle(questions)
    if state_format is not None and state_format not in ("text", "json", "messages"):
        raise ValueError(f"지원하지 않는 state_format: {state_format!r}")
    if state is None:
        raise ValueError("state 는 None 일 수 없다.")
    if state_format == "text" and (not isinstance(state, str) or not state.strip()):
        raise ValueError("state_format='text' 일 때 state 는 비어 있지 않은 문자열이어야 한다.")
    if state_format == "json" and not isinstance(state, (Mapping, Sequence)):
        raise ValueError("state_format='json' 일 때 state 는 사전 또는 리스트여야 한다.")
    if images is not None:
        if not isinstance(images, Sequence) or isinstance(images, (str, bytes)):
            raise ValueError("images 는 이미지 경로/객체의 시퀀스여야 한다.")
        if len(images) == 0:
            raise ValueError("images 가 주어졌을 때 최소 1장 이상이어야 한다.")
        if len(images) > MAX_IMAGES:
            raise ValueError(f"images 개수는 최대 {MAX_IMAGES}장 이하여야 한다(현재 {len(images)}장).")
