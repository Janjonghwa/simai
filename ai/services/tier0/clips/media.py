"""잘라낸 패킷을 클립 파일 내용으로 묶기(재다중화)와 선택 사항인 얼굴 흐림 단계 (명세 5.1, 11.5, 1.4).

**재다중화.** AnnexBMuxer 는 디코딩 없이 H.264 Annex B 기본 스트림을 만든다: 구간(ClipSegment)마다 SPS·PPS 를
시작 코드와 함께 앞에 두고, 받은 접근 단위(이미 Annex B, 12.7)를 받은 순서대로 이어 붙인다. 각 구간이 키프레임으로
시작하므로 그대로 재생·디코딩할 수 있다. 녹화기(gateway/recorder)의 epoch_NNNN.h264 와 같은 형식이다.
MP4 같은 컨테이너가 필요하면 ClipMuxer 를 바꿔 끼운다.

**얼굴 흐림.** 사용자 결정(2026-10-18)으로 이벤트 클립은 얼굴을 흐리지 않는다(명세 11.5 '확인 필요' 항목 확정,
명세 반영은 후속 명세 정리). 기본 조립은 흐림 단계 없이 클립을 내보내고 media_clip.face_redaction 에 NONE 을 남긴다.
FaceRedactor 는 나중에 다시 켤 수 있게 선택 사항으로 남긴다. 켜면 업로드·임시 저장 전에 반드시 거치고, 실패하면
흐리지 않은 클립으로 대신하지 않는다. 어느 경우에도 얼굴을 복원·식별하지 않는다(AGENTS.md 규칙 8, 1.4).
업로드·임시 저장 경로는 이 모듈의 prepare_for_export 가 만든 ExportClip 만 받는다(링 버퍼 패킷이 그대로 나가지 않게).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.tier0.clips.ring_buffer import CameraCut

ANNEXB_START = b"\x00\x00\x00\x01"
H264_CONTENT_TYPE = "video/h264"
# media_clip.face_redaction 값. 기본은 흐림 없음(사용자 결정), 흐림 단계를 켜면 BLURRED_BEFORE_EXPORT.
FACE_REDACTION_NONE = "NONE"
FACE_BLUR_BEFORE_EXPORT = "BLURRED_BEFORE_EXPORT"


class ClipMuxError(ValueError):
    """잘라낸 구간을 클립으로 묶을 수 없다(빈 구간, 지원하지 않는 코덱)."""


@dataclass(frozen=True)
class MuxedClip:
    """재다중화한 클립 내용. 메모리에만 있다. prepare_for_export 를 거쳐 ExportClip 이 된 뒤에만 내보낸다."""

    content: bytes = field(repr=False)
    content_type: str
    container: str  # 예: "h264-annexb"


@runtime_checkable
class ClipMuxer(Protocol):
    def mux(self, cut: CameraCut) -> MuxedClip: ...


class AnnexBMuxer:
    """디코딩 없는 재다중화: H.264 Annex B 기본 스트림."""

    container = "h264-annexb"

    def mux(self, cut: CameraCut) -> MuxedClip:
        if cut.empty:
            raise ClipMuxError(f"{cut.camera_id}: 잘라낸 구간이 비었다")
        out = bytearray()
        for seg in cut.segments:
            if seg.codec.codec.upper() != "H264":
                raise ClipMuxError(f"{cut.camera_id}: H.264 만 지원한다 (받은 코덱 {seg.codec.codec!r})")
            if seg.codec.sps:
                out += ANNEXB_START + seg.codec.sps
            if seg.codec.pps:
                out += ANNEXB_START + seg.codec.pps
            for p in seg.packets:
                out += p.payload
        return MuxedClip(bytes(out), H264_CONTENT_TYPE, self.container)


@dataclass(frozen=True)
class ExportClip:
    """내보낼(업로드·Network Volume 임시 저장) 이벤트 클립. face_redaction 이 흐림 여부를 밝힌다.

    흐림 없음(NONE)이면 redactor_version 은 None, 흐림을 거쳤으면 그 단계의 버전이다(어느 쪽인지 지어내지 않는다).
    """

    content: bytes = field(repr=False)
    content_type: str
    container: str
    face_redaction: str = FACE_REDACTION_NONE
    redactor_version: str | None = None

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("ExportClip.content 가 비었다")
        if self.face_redaction == FACE_REDACTION_NONE:
            if self.redactor_version is not None:
                raise ValueError("흐림 없음(NONE) 클립에 redactor_version 이 있다")
        elif self.face_redaction == FACE_BLUR_BEFORE_EXPORT:
            if not self.redactor_version:
                raise ValueError("흐림 클립에 redactor_version 이 없다(어느 흐림 단계를 거쳤는지 남긴다)")
        else:
            raise ValueError(f"모르는 face_redaction: {self.face_redaction!r}")

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


@runtime_checkable
class FaceRedactor(Protocol):
    """선택 사항인 업로드 직전 얼굴 흐림 단계(기본 조립에는 없다). 다시 켤 때 운영 구현을 붙인다.

    켜면 5.4 VLM 입력과 같은 원칙으로 안전모가 보이도록 머리 윗부분은 남긴다. 흐림에 실패하면 예외를 낸다
    (흐리지 않은 클립을 돌려주지 않는다). version 은 media_clip 에 남는다.
    """

    version: str

    def redact(self, clip: MuxedClip, cut: CameraCut) -> MuxedClip: ...


def prepare_for_export(clip: MuxedClip, cut: CameraCut, redactor: FaceRedactor | None = None) -> ExportClip:
    """메모리 밖으로 나갈 클립을 만든다. 흐림 단계가 없으면(기본) 흐림 없이, 있으면 반드시 거친다."""
    if redactor is None:
        return ExportClip(clip.content, clip.content_type, clip.container)
    out = redactor.redact(clip, cut)
    if not isinstance(out, MuxedClip):
        raise TypeError(f"FaceRedactor.redact 는 MuxedClip 을 돌려줘야 한다 (받은 값 {type(out).__name__})")
    return ExportClip(out.content, out.content_type, out.container, FACE_BLUR_BEFORE_EXPORT, str(redactor.version))
