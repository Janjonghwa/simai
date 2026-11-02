"""vlm_rationale 프롬프트·응답 형식·상향 의견 강제 (명세 5.4 빠른 경로, 12.5, S15P31A101-25, S15P31A101-273).

이미지·VLM 호출·작업 실패 코드는 처리기(`services.worker.vlm_rationale`)에 있고, 여기는 문자열·JSON 만 다루는 순수 함수다.

- 프롬프트는 규칙 결과(ID·이름·등급·신뢰도·확인 필요)·구역·수치·트랙 위치·프레임별 카메라와 상대 시각을 텍스트로 준다.
  위치의 정본은 Tier 0 값이고 VLM 은 고치지 않는다(AGENTS 1). 프레임 순서는 이미지 파트 순서와 같다.
- 응답은 `{"rationale": 한국어 1~3문장, "proposed_severity": 등급 | null}` 하나. response_format json_schema 로 건다.
- 상향만: VLM 이 같거나 낮은 등급을 내면 null 로 바꾸고 사유 코드 `NOT_UPWARD` 를 돌려준다(계약 severity_upward_only, AGENTS 4). 조항 ID 는 응답에서 받지 않는다.
- `PROMPT` 는 처리기와 G7 평가 도구(`experiments/g7_eval/g7_answers.py --prompt simai_ai.vlm.rationale:PROMPT`)가 같이 쓴다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

SEVERITY_RANK = {"INFO": 0, "CAUTION": 1, "HIGH": 2, "CRITICAL": 3}
SEVERITIES = ("CRITICAL", "HIGH", "CAUTION", "INFO")
PROPOSAL_DISCARD_REASON = "NOT_UPWARD"  # 현재 등급보다 높지 않은 상향 의견을 버린 사유 코드(계약 event_escalate_result 와 동일)
RATIONALE_MAX_CHARS = 150  # 1~3문장. 한글은 글자당 토큰이 1 안팎이라 클라이언트 max_tokens 기본 256 안에 JSON 이 끝나게

RULE_NAMES = {  # 명세 7.1 규칙 표
    "R1": "위험 구역 진입",
    "R2": "개구부·가장자리 근접",
    "R3": "고소 위치",
    "R4": "낙상 의심",
    "R5": "진입 예측",
    "R6": "위험 구역 내 관측 상실",
}

SYSTEM_PROMPT = """너는 교실 안전 관제 시스템의 이벤트 근거 설명 보조다.
규칙 엔진이 이미 위험 이벤트를 판정했다. 너는 주어진 프레임과 구조화 상태를 보고 왜 위험한지 한국어 1~3문장으로 설명한다.
- 등급을 내리거나 경보를 끄거나 해소할 수 없다. 위치·좌표·경보를 바꾸지 않는다. 구조화 상태의 위치·수치가 정본이다.
- 프레임에서 보이는 것만 쓴다. 보이지 않는 것을 지어내지 않는다. 얼굴·신원은 언급하지 않는다.
- proposed_severity 는 프레임에서 규칙 등급보다 더 위험하다는 근거가 보일 때만 더 높은 등급을 쓴다. 그 밖에는 null 이다.
- 법령·조항 번호를 쓰지 않는다.
- JSON 객체 하나만 출력한다: {"rationale": "...", "proposed_severity": "CRITICAL" | "HIGH" | "CAUTION" | "INFO" | null}"""

RETRY_NOTE = "\n\n앞 답이 형식에 맞지 않았다. 위 JSON 객체 하나만 출력하라."

RESPONSE_FORMAT: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "vlm_rationale",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "rationale": {"type": "string", "minLength": 1, "maxLength": RATIONALE_MAX_CHARS},
                "proposed_severity": {"enum": [*SEVERITIES, None]},
            },
            "required": ["rationale", "proposed_severity"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class PromptParts:
    text: str
    system: str | None = None
    response_format: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class Answer:
    rationale: str
    proposed_severity: str | None


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def build_prompt(inp: Mapping[str, Any], frames: Sequence[Mapping[str, Any]]) -> str:
    """vlm_rationale input + VLM 에 넣는 순서대로의 프레임(camera_id·estimated_capture_time_ms) → 사용자 질문.

    frames 가 비면 프레임 줄을 뺀다(G7 도구는 frames 없이 부른다). confidence_reasons 가 있으면 함께 적는다(G7 표본 입력).
    """
    obs = inp["observation_time"]
    rule = inp["rule_id"]
    name = RULE_NAMES.get(rule)
    lines = [
        f"규칙: {rule}{f'({name})' if name else ''} v{inp['rule_version']}",
        f"규칙 등급: {inp['severity']}, 신뢰도: {inp['confidence']}, 확인 필요: {'예' if inp['needs_check'] else '아니오'}",
    ]
    if isinstance(inp.get("confidence_reasons"), list) and inp["confidence_reasons"]:
        lines.append("신뢰도 사유: " + ", ".join(map(str, inp["confidence_reasons"])))
    zone = inp["structured_state"].get("zone") if isinstance(inp["structured_state"].get("zone"), Mapping) else {}
    zone_id = inp.get("zone_id")
    lines.append(f"구역: {zone_id or '없음'}" + (f", 판정 방식 {zone['judge_type']}" if zone.get("judge_type") else ""))
    if inp["metrics"]:
        lines.append("수치: " + ", ".join(f"{k}={_num(v)}" for k, v in inp["metrics"].items()))
    tracks = inp["structured_state"].get("tracks")
    for t in tracks if isinstance(tracks, list) else []:
        if not isinstance(t, Mapping):
            continue
        parts = [f"사람 {t.get('world_id', '?')}"]
        if isinstance(t.get("position_xy"), list):
            parts.append("위치(m) (" + ", ".join(_num(v) for v in t["position_xy"]) + ")")
        if t.get("head_z") is not None:
            parts.append(f"머리 높이 {_num(t['head_z'])} m")
        lines.append(", ".join(parts))
    if frames:
        lines.append("프레임(이미지 순서):")
    for i, f in enumerate(frames, 1):
        dt = (f["estimated_capture_time_ms"] - obs) / 1000
        lines.append(f"{i}. 카메라 {f['camera_id']}, 이벤트 시점 기준 {dt:+.1f}초")
    if inp.get("overlay_applied"):
        lines.append("빨간 영역은 위험 구역, 노란 박스 또는 표시는 대상자다.")
    lines.append("위 이벤트의 근거를 설명하라.")
    return "\n".join(lines)


@dataclass(frozen=True)
class RationalePrompt:
    """G7 도구의 Prompt 꼴(prompt_id·placeholder·build). 프롬프트·스키마를 바꾸면 prompt_id 를 올린다."""

    prompt_id: str = "vlm-rationale-2"
    placeholder: bool = False

    def build(self, inp: Mapping[str, Any]) -> PromptParts:
        frames = inp.get("frames")
        return PromptParts(build_prompt(inp, frames if isinstance(frames, list) else []), SYSTEM_PROMPT, RESPONSE_FORMAT)


PROMPT = RationalePrompt()


def parse_answer(content: str) -> Answer:
    """VLM 응답 → Answer. 형식이 어긋나면 ValueError(원문은 메시지에 넣지 않는다)."""
    try:
        doc = json.loads(content)
    except ValueError:
        raise ValueError("JSON 이 아니다") from None
    if not isinstance(doc, dict) or set(doc) != {"rationale", "proposed_severity"}:
        raise ValueError("키가 rationale·proposed_severity 가 아니다")
    rationale, proposed = doc["rationale"], doc["proposed_severity"]
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > RATIONALE_MAX_CHARS:
        raise ValueError(f"rationale 은 1~{RATIONALE_MAX_CHARS}자 문자열이다")
    if proposed is not None and proposed not in SEVERITY_RANK:
        raise ValueError("proposed_severity 가 등급이 아니다")
    return Answer(rationale.strip(), proposed)


def check_proposal(severity: str, proposed: str | None) -> tuple[str | None, str | None]:
    """상향 의견을 검증해 (남길 값, 버린 사유 코드)를 돌려준다 (명세 5.4, S15P31A101-273).

    계약·명세 등급 순서(INFO < CAUTION < HIGH < CRITICAL)로 비교해 현재 등급보다 높을 때만 남기고,
    같거나 낮으면 (None, "NOT_UPWARD")로 버린다. 의견이 원래 None 이면 버린 것이 아니므로 (None, None)이다.
    """
    if proposed is None:
        return None, None
    if SEVERITY_RANK[proposed] <= SEVERITY_RANK[severity]:
        return None, PROPOSAL_DISCARD_REASON
    return proposed, None


def upward_only(severity: str, proposed: str | None) -> str | None:
    """규칙 등급보다 높은 의견만 남긴다. 같거나 낮으면 None(하향 불가)."""
    return check_proposal(severity, proposed)[0]
