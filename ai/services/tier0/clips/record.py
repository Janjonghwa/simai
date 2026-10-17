"""media_clip 기록 (명세 11.3 `media_clip`: 이벤트 연결 영상 구간 주소, 삭제 예정일, 보존 표시. 11.5 보관).

contracts/ 에 media_clip 스키마가 아직 없다. 필드 이름은 11.3·11.5 와 기존 계약 관례(UTC int64 ms, event_id·gateway_id,
source)를 따른 내부 형식이고, 스키마가 생기면 to_payload 결과를 그 스키마로 검증하는 시험을 붙인다(MR 초안에 제안).

- 시각은 UTC int64 ms. 클립 구간은 촬영 시각(capture) 기준, created_at·uploaded_at 은 주입 시계 기준이다.
- delete_after_ms = created_at_ms + 보관 기간(14일, 11.5·16). 실제 삭제는 객체 저장소 수명 주기 규칙이 한다.
- retention_hold: CRITICAL 이벤트 연결분은 자동 보존 표시(11.5). HIGH 로 시작해 CRITICAL 로 오른 이벤트는 아직 보내지
  않은 기록이면 그 값을 올리고, 이미 보낸 기록이면 덮어쓰지 않고 ClipRetentionUpdate 를 추가로 낸다(AGENTS.md 규칙 6).
  아차사고 보존 표시는 관리자가 EC2 에서 붙이고 뗀다.
- 클립 구간을 모르면 지어내지 않는다: 버퍼에 요청 구간이 다 없으면 truncated_start·truncated_end, 구간 안에 손실(빈칸)이
  있으면 has_gaps 로 남긴다.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from services.tier0.clips.media import ExportClip
from services.tier0.clips.ring_buffer import CameraCut
from services.tier0.clips.trigger import ClipTrigger

DAY_MS = 86_400_000
DEFAULT_RETENTION_DAYS = 14  # 명세 11.5, 16 '보관 기간'
SOURCE_LIVE = "LIVE"  # 클립은 LIVE 이벤트에만 만든다(trigger.clip_trigger_for)


@dataclass(frozen=True)
class ClipDraft:
    """업로드 전 기록. 업로드가 끝나면 object_key·uploaded_at_ms 를 채워 MediaClipRecord 가 된다."""

    clip_id: str
    event_id: str
    gateway_id: str
    event_sequence: int
    resume_count: int
    rule_id: str
    severity: str
    camera_id: str
    stream_epochs: tuple[int, ...]
    anchor_capture_ms: int
    requested_start_ms: int
    requested_end_ms: int
    clip_start_capture_ms: int
    clip_end_capture_ms: int
    frame_count: int
    starts_with_keyframe: bool
    truncated_start: bool
    truncated_end: bool
    has_gaps: bool
    content_type: str
    container: str
    size_bytes: int
    sha256: str
    face_redaction: str
    redactor_version: str | None
    created_at_ms: int
    delete_after_ms: int
    retention_hold: bool
    source: str = SOURCE_LIVE

    @classmethod
    def build(cls, *, clip_id: str, trigger: ClipTrigger, camera_id: str, cut: CameraCut, clip: ExportClip,
              created_at_ms: int, retention_days: int = DEFAULT_RETENTION_DAYS, critical_auto_hold: bool = True) -> "ClipDraft":
        return cls(
            clip_id=clip_id,
            event_id=trigger.event_id,
            gateway_id=trigger.gateway_id,
            event_sequence=trigger.event_sequence,
            resume_count=trigger.resume_count,
            rule_id=trigger.rule_id,
            severity=str(trigger.severity),
            camera_id=camera_id,
            stream_epochs=tuple(cut.stream_epochs),
            anchor_capture_ms=trigger.anchor_capture_ms,
            requested_start_ms=cut.requested_start_ms,
            requested_end_ms=cut.requested_end_ms,
            clip_start_capture_ms=cut.packets[0].capture_time_ms,
            clip_end_capture_ms=cut.packets[-1].capture_time_ms,
            frame_count=cut.frame_count,
            starts_with_keyframe=cut.packets[0].keyframe,
            truncated_start=cut.truncated_start,
            truncated_end=cut.truncated_end,
            has_gaps=cut.has_gaps,
            content_type=clip.content_type,
            container=clip.container,
            size_bytes=clip.size_bytes,
            sha256=clip.sha256,
            face_redaction=clip.face_redaction,
            redactor_version=clip.redactor_version,
            created_at_ms=created_at_ms,
            delete_after_ms=created_at_ms + retention_days * DAY_MS,
            retention_hold=critical_auto_hold and str(trigger.severity) == "CRITICAL",
        )

    def to_payload(self) -> dict[str, Any]:
        d = asdict(self)
        d["stream_epochs"] = list(self.stream_epochs)
        return d

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ClipDraft":
        data = dict(payload)
        data["stream_epochs"] = tuple(data["stream_epochs"])
        return cls(**data)


@dataclass(frozen=True)
class MediaClipRecord:
    """업로드가 끝난 클립 기록(11.3 media_clip). EC2 에는 이 기록만 가고 영상은 객체 저장소에만 있다."""

    draft: ClipDraft
    object_key: str
    uploaded_at_ms: int
    spooled: bool  # EC2 단절로 Network Volume 에 먼저 저장했다가 올렸는지(5.1, D-G3-19)
    upload_attempts: int

    def to_payload(self) -> dict[str, Any]:
        return self.draft.to_payload() | {
            "object_key": self.object_key,
            "uploaded_at_ms": self.uploaded_at_ms,
            "spooled": self.spooled,
            "upload_attempts": self.upload_attempts,
        }


RETENTION_REASON_CRITICAL = "CRITICAL_AUTO"


@dataclass(frozen=True)
class ClipRetentionUpdate:
    """이미 보낸 media_clip 의 보존 표시를 붙이는 추가 기록(11.5 CRITICAL 자동 보존). 원래 기록은 바꾸지 않는다."""

    clip_id: str
    event_id: str
    gateway_id: str
    resume_count: int
    severity: str
    retention_hold: bool
    reason: str
    issued_at_ms: int
    source: str = SOURCE_LIVE

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)
