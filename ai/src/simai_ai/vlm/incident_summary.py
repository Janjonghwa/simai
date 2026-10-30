"""incident_summary 프롬프트·클립 프레임 표본 추출·응답 파싱 및 구간 검증 (명세 5.4 용도 2, 12.5, FR-88, S15P31A101-271).

클립 내려받기·디코딩·얼굴 흐림 관문·VLM 호출·작업 실패 코드는 처리기(`services.worker.incident_summary`)에 있고,
여기는 클립 프레임 촬영 시각 보간·장수 상한 표본 추출·프롬프트 생성·응답 파싱(이벤트 구간 검증)만 다루는 순수 함수다.

- 클립 프레임 시각: 각 클립(`clip_start_capture_ms` ~ `clip_end_capture_ms`)에서 디코딩된 N장의 촬영 시각을 등간격으로 계산하고,
  이벤트 구간(`[window_start_ms, window_end_ms]`) 안의 프레임만 남긴 뒤 전체 장수 상한(`DEFAULT_MAX_FRAMES = 16`)으로 균등 표본 추출한다.
- 응답은 `{"summary": 한국어 요약문, "evidence_timestamps_ms": [촬영 시각(UTC ms), ...]}` 하나다(`response_format` `json_schema`).
- 등급(`severity`)이나 상향 의견(`proposed_severity`) 필드는 스키마와 파서 모두에서 받지 않는다(명세 5.4 용도 2).
- 근거 시각(`evidence_timestamps_ms`)은 모두 이벤트 구간(`[window_start_ms, window_end_ms]`) 안이어야 한다:
  구간 밖 시각이 섞여 있으면 버리고, 구간 안에 남는 시각이 하나도 없으면 `ValueError` 로 거부한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .rationale import RULE_NAMES, PromptParts

SUMMARY_MAX_CHARS = 400
MAX_EVIDENCE_TIMESTAMPS = 16
DEFAULT_MAX_FRAMES = 16

SYSTEM_PROMPT = """너는 안전 관제 시스템의 사고·아차사고 구간 요약 보조다.
주어진 이벤트 구간 클립 프레임(시간순)과 구조화 상태를 보고, 이벤트 구간 동안 일어난 상황과 작업자 이동·행동을 시간 순서로 한국어 2~4문장(400자 이내)으로 요약한다.
- 위험 등급(severity)을 판정하거나 상향 의견(proposed_severity)을 내지 않는다. 경보를 낮추거나 끄거나 해소하지 않는다.
- 위치·좌표·구역의 정본은 Tier 0 구조화 상태다. 프레임에서 보이는 것만 쓰고 보이지 않는 것을 지어내지 않는다. 얼굴·신원은 언급하지 않는다.
- 법령·조항 번호를 쓰지 않는다.
- evidence_timestamps_ms 에는 요약의 근거가 되는 프레임의 촬영 시각(UTC ms 정수)을 오름차순으로 1개 이상 적는다. 반드시 주어진 이벤트 구간(window_start_ms 이상 window_end_ms 이하) 안의 시각만 적는다.
- JSON 객체 하나만 출력한다: {"summary": "...", "evidence_timestamps_ms": [1760342400000]}"""

RETRY_NOTE = "\n\n앞 답이 형식에 맞지 않았거나 근거 시각이 이벤트 구간 밖이었다. 이벤트 구간 안의 촬영 시각만 담아 위 JSON 객체 하나만 출력하라."

RESPONSE_FORMAT: Mapping[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "incident_summary",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": SUMMARY_MAX_CHARS},
                "evidence_timestamps_ms": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": MAX_EVIDENCE_TIMESTAMPS,
                    "items": {"type": "integer"},
                },
            },
            "required": ["summary", "evidence_timestamps_ms"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class SampledFrame:
    """클립에서 추출해 얼굴 흐림 관문과 VLM 에 넣을 프레임 표본."""

    source_id: str
    clip_id: str
    camera_id: str
    frame_index: int
    capture_time_ms: int
    image: Any


@dataclass(frozen=True)
class SummaryAnswer:
    summary: str
    evidence_timestamps_ms: tuple[int, ...]
    dropped_timestamps_ms: tuple[int, ...] = ()


def _num(v: Any) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def resolve_event_window(inp: Mapping[str, Any]) -> tuple[int, int]:
    """입력에서 이벤트 구간 `[window_start_ms, window_end_ms]` 를 구하고 역전을 검사한다."""
    clips = inp.get("clips")
    if not isinstance(clips, Sequence) or not clips:
        raise ValueError("clips 가 비었다")
    starts: list[int] = []
    ends: list[int] = []
    for c in clips:
        s = c["clip_start_capture_ms"]
        e = c["clip_end_capture_ms"]
        if e < s:
            raise ValueError(f"클립 {c.get('clip_id', '?')} 의 끝 시각({e})이 시작 시각({s})보다 앞선다")
        starts.append(int(s))
        ends.append(int(e))
    w_start = int(inp["window_start_ms"]) if inp.get("window_start_ms") is not None else min(starts)
    w_end = int(inp["window_end_ms"]) if inp.get("window_end_ms") is not None else max(ends)
    if w_end < w_start:
        raise ValueError(f"window_end_ms({w_end})가 window_start_ms({w_start})보다 앞선다")
    return w_start, w_end


def assign_clip_timestamps(clip_start_ms: int, clip_end_ms: int, frame_count: int) -> list[int]:
    """클립 시작·끝 촬영 시각 사이를 디코딩된 프레임 수만큼 등간격 보간한다."""
    if clip_end_ms < clip_start_ms:
        raise ValueError("clip_end_capture_ms 가 clip_start_capture_ms 보다 앞선다")
    if frame_count <= 0:
        raise ValueError("프레임 수가 1 미만이다")
    if frame_count == 1:
        return [clip_start_ms]
    span = clip_end_ms - clip_start_ms
    return [round(clip_start_ms + span * i / (frame_count - 1)) for i in range(frame_count)]


def uniform_indices(total: int, max_items: int) -> list[int]:
    """`total` 개 중에서 최대 `max_items` 개의 인덱스를 양 끝 포함 등간격으로 고른다."""
    if max_items <= 0:
        raise ValueError("max_frames 는 1 이상이어야 한다")
    if total <= 0:
        return []
    if total <= max_items:
        return list(range(total))
    if max_items == 1:
        return [total // 2]
    return [round(i * (total - 1) / (max_items - 1)) for i in range(max_items)]


def sample_clip_frames(
    clips_with_images: Sequence[tuple[Mapping[str, Any], Sequence[Any]]],
    *,
    window_start_ms: int,
    window_end_ms: int,
    max_frames: int = DEFAULT_MAX_FRAMES,
) -> list[SampledFrame]:
    """클립별 디코딩 프레임에 촬영 시각을 붙이고 이벤트 구간 필터 및 장수 상한(`max_frames`)을 적용한다."""
    if max_frames <= 0:
        raise ValueError("max_frames 는 1 이상이어야 한다")
    candidates: list[SampledFrame] = []
    for clip, images in clips_with_images:
        if not images:
            continue
        clip_id = str(clip["clip_id"])
        camera_id = str(clip["camera_id"])
        timestamps = assign_clip_timestamps(
            int(clip["clip_start_capture_ms"]),
            int(clip["clip_end_capture_ms"]),
            len(images),
        )
        for idx, (ts, img) in enumerate(zip(timestamps, images, strict=True)):
            if window_start_ms <= ts <= window_end_ms:
                candidates.append(
                    SampledFrame(
                        source_id=f"{clip_id}#{idx}",
                        clip_id=clip_id,
                        camera_id=camera_id,
                        frame_index=idx,
                        capture_time_ms=ts,
                        image=img,
                    )
                )
    candidates.sort(key=lambda f: (f.capture_time_ms, f.camera_id, f.clip_id, f.frame_index))
    chosen = uniform_indices(len(candidates), max_frames)
    return [candidates[i] for i in chosen]


def build_prompt(
    inp: Mapping[str, Any],
    frames: Sequence[Mapping[str, Any]],
    *,
    window_start_ms: int,
    window_end_ms: int,
) -> str:
    """incident_summary input + VLM 에 넣는 순서대로의 프레임(`camera_id`·`capture_time_ms`) → 사용자 프롬프트."""
    state = inp.get("structured_state") if isinstance(inp.get("structured_state"), Mapping) else {}
    rule = state.get("rule_id")
    rule_name = RULE_NAMES.get(str(rule)) if rule else None
    lines = [
        f"이벤트 ID: {inp['event_id']} (시퀀스 {inp['event_sequence']})",
        f"이벤트 구간(UTC ms): {window_start_ms} ~ {window_end_ms}",
    ]
    if inp.get("anchor_capture_ms") is not None:
        lines.append(f"기준 시각(UTC ms): {inp['anchor_capture_ms']}")
    if rule:
        lines.append(f"규칙: {rule}{f'({rule_name})' if rule_name else ''}")
    zone_id = state.get("zone_id")
    if zone_id:
        lines.append(f"구역: {zone_id}")
    transitions = state.get("transitions")
    if isinstance(transitions, list) and transitions:
        lines.append("전이 기록: " + " -> ".join(map(str, transitions)))
    metrics = state.get("metrics")
    if isinstance(metrics, Mapping) and metrics:
        lines.append("수치: " + ", ".join(f"{k}={_num(v)}" for k, v in metrics.items()))
    tracks = state.get("tracks")
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
        lines.append("프레임(이미지 순서, 촬영 시각은 이벤트 구간 안):")
    for i, f in enumerate(frames, 1):
        ts = int(f["capture_time_ms"])
        dt = (ts - window_start_ms) / 1000.0
        lines.append(f"{i}. 카메라 {f['camera_id']}, 촬영 시각 {ts} ms (구간 시작 +{dt:.1f}초)")
    lines.append("위 이벤트 구간의 사고·아차사고 상황을 요약하고 근거 촬영 시각(evidence_timestamps_ms)을 적어라.")
    return "\n".join(lines)


@dataclass(frozen=True)
class IncidentSummaryPrompt:
    prompt_id: str = "incident-summary-1"
    placeholder: bool = False

    def build(
        self,
        inp: Mapping[str, Any],
        frames: Sequence[Mapping[str, Any]] = (),
        *,
        window_start_ms: int | None = None,
        window_end_ms: int | None = None,
    ) -> PromptParts:
        if window_start_ms is None or window_end_ms is None:
            w_start, w_end = resolve_event_window(inp)
        else:
            w_start, w_end = window_start_ms, window_end_ms
        return PromptParts(
            build_prompt(inp, frames, window_start_ms=w_start, window_end_ms=w_end),
            SYSTEM_PROMPT,
            RESPONSE_FORMAT,
        )


PROMPT = IncidentSummaryPrompt()


def parse_answer(content: str, *, window_start_ms: int, window_end_ms: int) -> SummaryAnswer:
    """VLM 응답 → `SummaryAnswer`. 형식 불일치·등급 필드 포함·구간 내 근거 시각 없음은 `ValueError`."""
    try:
        doc = json.loads(content)
    except ValueError:
        raise ValueError("JSON 이 아니다") from None
    if not isinstance(doc, dict) or set(doc) != {"summary", "evidence_timestamps_ms"}:
        raise ValueError("키가 summary·evidence_timestamps_ms 가 아니다")
    summary = doc["summary"]
    raw_ts = doc["evidence_timestamps_ms"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > SUMMARY_MAX_CHARS:
        raise ValueError(f"summary 는 1~{SUMMARY_MAX_CHARS}자 문자열이다")
    if not isinstance(raw_ts, list) or not (1 <= len(raw_ts) <= MAX_EVIDENCE_TIMESTAMPS):
        raise ValueError(f"evidence_timestamps_ms 는 1~{MAX_EVIDENCE_TIMESTAMPS}개 배열이다")

    in_window: list[int] = []
    dropped: list[int] = []
    seen: set[int] = set()
    for item in raw_ts:
        if not isinstance(item, int) or isinstance(item, bool):
            raise ValueError("evidence_timestamps_ms 원소가 정수가 아니다")
        if window_start_ms <= item <= window_end_ms:
            if item not in seen:
                seen.add(item)
                in_window.append(item)
        else:
            dropped.append(item)
    if not in_window:
        raise ValueError("이벤트 구간 안의 근거 시각이 없다")
    in_window.sort()
    return SummaryAnswer(
        summary=summary.strip(),
        evidence_timestamps_ms=tuple(in_window),
        dropped_timestamps_ms=tuple(dropped),
    )
