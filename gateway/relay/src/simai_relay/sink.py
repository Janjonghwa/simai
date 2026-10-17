"""I0 영상 기록과 전송부 자리 (명세 12.7, contracts/README.md "I0 전송 규칙").

수신 루프(relay.py)는 카메라마다 CameraSink 하나에 기록을 넘긴다. 순서는 새 stream_epoch 마다
codec_info 한 번 → 키프레임부터 (frame_header + 압축 프레임) 이다. 헤더와 프레임은 한 기록(I0Frame)에 같이 싣는다.

RunPod Tier 0 로 보내는 TLS 전송(길이 접두, 카메라별 독립 TLS 연결, 기기 토큰, 공개키 고정, 송신 큐 상한)은
S15P31A101-83 이 이 인터페이스를 구현해 붙인다. 이 티켓에는 시험·점검용 구현(MemorySink, CountingSink)만 있다.

구현 약속:
- SinkFactory(camera_id) 는 카메라마다 독립된 CameraSink 를 만든다. 한 카메라의 전송 지연이 다른 카메라를 막지 않게
  카메라 사이에 잠금·큐를 공유하지 않는다.
- send_* 는 수신 스레드에서 불린다. 오래 막히면 그 카메라의 수신만 밀린다(다른 카메라는 계속 돈다). 네트워크가 잠깐
  끊기는 것은 전송부 안에서 처리하고(재접속, 큐가 넘치면 다음 키프레임까지 버림) 예외로 올리지 않는다.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class I0CodecInfo:
    """contracts/messages/codec_info.schema.json 한 건. 새 stream_epoch 마다 프레임보다 먼저 한 번."""

    codec_info: dict[str, Any]


@dataclass(frozen=True)
class I0Frame:
    """contracts/messages/frame_header.schema.json 헤더와 그 헤더가 가리키는 압축 프레임(H.264 접근 단위, Annex B)."""

    header: dict[str, Any]
    data: bytes


class CameraSink(Protocol):
    def send_codec_info(self, record: I0CodecInfo) -> None: ...

    def send_frame(self, record: I0Frame) -> None: ...

    def close(self) -> None: ...


SinkFactory = Callable[[str], CameraSink]


class MemorySink:
    """시험용: 받은 기록을 순서대로 모은다. 다른 스레드에서 읽어도 되게 잠근다(카메라 하나에만 쓰인다)."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._lock = threading.Lock()
        self._records: list[I0CodecInfo | I0Frame] = []
        self.closed = False

    def send_codec_info(self, record: I0CodecInfo) -> None:
        with self._lock:
            self._records.append(record)

    def send_frame(self, record: I0Frame) -> None:
        with self._lock:
            self._records.append(record)

    def close(self) -> None:
        self.closed = True

    @property
    def records(self) -> list[I0CodecInfo | I0Frame]:
        with self._lock:
            return list(self._records)

    @property
    def frames(self) -> list[I0Frame]:
        return [r for r in self.records if isinstance(r, I0Frame)]


class CountingSink:
    """전송부(-83)가 붙기 전 점검용: 아무 데도 보내지 않고 개수·바이트만 센다."""

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self.codec_infos = 0
        self.frames = 0
        self.bytes = 0

    def send_codec_info(self, record: I0CodecInfo) -> None:
        self.codec_infos += 1

    def send_frame(self, record: I0Frame) -> None:
        self.frames += 1
        self.bytes += len(record.data)

    def close(self) -> None:
        pass
