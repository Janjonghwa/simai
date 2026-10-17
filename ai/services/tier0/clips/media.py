"""잘라낸 패킷을 클립 파일 내용으로 묶기(재다중화)와 얼굴 흐림 단계 (명세 5.1, 11.5, 5.4, 1.4).

**재다중화.** AnnexBMuxer 는 디코딩 없이 H.264 Annex B 기본 스트림을 만든다: 구간(ClipSegment)마다 SPS·PPS 를
시작 코드와 함께 앞에 두고, 받은 접근 단위(이미 Annex B, 12.7)를 받은 순서대로 이어 붙인다. 각 구간이 키프레임으로
시작하므로 그대로 재생·디코딩할 수 있다. 녹화기(gateway/recorder)의 epoch_NNNN.h264 와 같은 형식이다.
MP4 같은 컨테이너가 필요하면 ClipMuxer 를 바꿔 끼운다.

**얼굴 흐림.** 명세 11.5 는 클립의 얼굴을 흐림 처리한다고 정했지만 '저장 전에 할지 열람 때 할지'는 확인 필요다.
결정 전에는 안전한 쪽을 기본으로 한다: **메모리 밖(Network Volume 임시 저장, 객체 저장소 업로드)으로 나가는 클립은
모두 이 단계(FaceRedactor)를 거친다.** 흐림 단계가 조립되지 않으면 클립을 내보내지 않는다(EventClipService 가 막는다).
업로드·임시 저장 경로는 RedactedClip 만 받는다. 얼굴을 복원·식별하지 않는다(1.4).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.tier0.clips.ring_buffer import CameraCut

ANNEXB_START = b"\x00\x00\x00\x01"
H264_CONTENT_TYPE = "video/h264"
# 업로드 직전 흐림을 거친 클립의 처리 방식 표시(media_clip.face_redaction). 열람 때 흐림으로 정해지면 값이 늘어난다.
FACE_BLUR_BEFORE_EXPORT = "BLURRED_BEFORE_EXPORT"


class ClipMuxError(ValueError):
    """잘라낸 구간을 클립으로 묶을 수 없다(빈 구간, 지원하지 않는 코덱)."""


@dataclass(frozen=True)
class MuxedClip:
    """흐림 전 클립 내용. 메모리에만 있다. 이 형태 그대로는 업로드·임시 저장하지 않는다."""

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
class RedactedClip:
    """얼굴 흐림을 거친 클립. 업로드·Network Volume 임시 저장은 이 형태만 받는다."""

    content: bytes = field(repr=False)
    content_type: str
    container: str
    face_redaction: str
    redactor_version: str

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("RedactedClip.content 가 비었다")
        if not self.redactor_version:
            raise ValueError("RedactedClip.redactor_version 이 비었다(어느 흐림 단계를 거쳤는지 남긴다)")

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()


@runtime_checkable
class FaceRedactor(Protocol):
    """업로드 직전 얼굴 흐림 단계. 운영 구현(디코딩 → 머리 영역 흐림 → 재인코딩)은 후속 티켓이다.

    5.4 VLM 입력과 같은 원칙으로 안전모가 보이도록 머리 윗부분은 남기는 것을 기본으로 한다.
    흐림에 실패하면 예외를 낸다(흐리지 않은 클립을 돌려주지 않는다). version 은 media_clip 에 남는다.
    """

    version: str

    def redact(self, clip: MuxedClip, cut: CameraCut) -> MuxedClip: ...


class FaceRedactionUnavailable(RuntimeError):
    """흐림 단계가 조립되지 않았다. 결정(11.5) 전 기본값으로 클립을 내보내지 않는다."""


def redact_for_export(clip: MuxedClip, cut: CameraCut, redactor: FaceRedactor | None) -> RedactedClip:
    """메모리 밖으로 나갈 클립을 만든다. 흐림 단계가 없으면 FaceRedactionUnavailable."""
    if redactor is None:
        raise FaceRedactionUnavailable("얼굴 흐림 단계가 없어 클립을 내보내지 않는다(명세 11.5 흐림 시점 결정 전 기본값)")
    out = redactor.redact(clip, cut)
    if not isinstance(out, MuxedClip):
        raise TypeError(f"FaceRedactor.redact 는 MuxedClip 을 돌려줘야 한다 (받은 값 {type(out).__name__})")
    return RedactedClip(out.content, out.content_type, out.container, FACE_BLUR_BEFORE_EXPORT, str(redactor.version))
