"""디코더 인터페이스 (명세 5.1 프레임 최신성 정책: 디코딩과 추론 분리).

디코더는 프레임 의존성 때문에 모든 압축 프레임을 받는다. 기본은 CPU 디코딩이고 GPU 하드웨어 디코더(NVDEC)는 설정으로 고른다
(둘 다 pyav_decoder.PyAvDecoder, NVDEC 은 hwaccel cuda, 근거는 docs/design/tier0-nvdec-throughput.md). 단위 시험은 GPU·영상
없이 FakeDecoder 로 한다.

하드웨어 디코더는 파이프라인 지연이 있고(B 프레임이 있으면 순서도 바뀐다) 입력 하나가 바로 출력 하나로 나오지 않는다.
그래서 입력마다 열쇠(FrameKey)를 함께 넘기고, 출력 그림은 그 열쇠로 헤더와 다시 묶는다.
"마지막으로 넣은 헤더"를 출력에 붙이면 촬영 시각이 다른 프레임에 붙는다(G3 I0 결합 오류).
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import Any

from simai_ai.io.messages import FrameKey, I0CodecInfo


class DecodeError(Exception):
    """압축 프레임 하나를 디코딩하지 못했다. 다음 키프레임까지 그 카메라의 프레임을 버린다."""


@dataclass(frozen=True)
class DecodedPicture:
    """디코더 출력 한 장. key 는 그 그림을 만든 입력에 넘긴 열쇠이고, image 형식은 구현이 정한다."""

    key: FrameKey
    image: Any


class Decoder(ABC):
    """카메라 한 대의 디코더. 카메라마다 인스턴스를 따로 둔다.

    순서: configure → decode(키프레임부터) … → flush/reset. stream_epoch 가 바뀌면 호출자가 reset 후 다시 configure 한다.
    """

    @abstractmethod
    def configure(self, codec: I0CodecInfo) -> None:
        """코덱 정보로 디코더를 (다시) 초기화한다. 내부에 남은 프레임은 버린다."""

    @abstractmethod
    def decode(self, payload: bytes, key: FrameKey, keyframe: bool) -> list[DecodedPicture]:
        """압축 프레임 하나를 넣고, 지금 나온 그림을 돌려준다(없으면 빈 목록, 여러 장일 수도 있다).

        출력 그림의 key 는 반드시 그 그림을 만든 입력의 key 다. 실패하면 DecodeError.
        """

    @abstractmethod
    def flush(self) -> list[DecodedPicture]:
        """내부에 남은 그림을 모두 내보낸다(입력 끝)."""

    @abstractmethod
    def reset(self) -> None:
        """내부 상태와 남은 그림을 버린다. 다시 쓰려면 configure 해야 한다."""


# --------------------------------------------------------------------------- 시험용 가짜 디코더


@dataclass(frozen=True)
class FakeImage:
    """FakeDecoder 의 그림. 압축 데이터 안에 적힌 '실제로 찍힌 프레임'을 그대로 담는다.

    시험은 이 값과 붙은 헤더(Frame.frame_ref, estimated_capture_time)를 비교해 결합 오류를 센다.
    """

    camera_id: str
    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int


def fake_payload(camera_id: str, stream_epoch: int, frame_sequence: int, capture_time_ms: int) -> bytes:
    """FakeDecoder 가 읽는 압축 프레임. 실제 영상의 화소처럼 헤더와 별개로 프레임 내용을 싣는다."""
    return json.dumps(
        {"camera_id": camera_id, "stream_epoch": stream_epoch, "frame_sequence": frame_sequence, "t": capture_time_ms},
        separators=(",", ":"),
    ).encode("utf-8")


class FakeDecoder(Decoder):
    """결정적 가짜 디코더 (GPU 없음).

    - configure 전이나 reset 직후 키프레임이 아닌 프레임을 받으면 DecodeError (실제 디코더의 참조 프레임 의존성).
    - delay: 출력이 입력보다 이만큼 늦게 나온다(하드웨어 파이프라인 지연 흉내).
    - reorder_pairs: True 면 연속한 두 입력의 출력 순서를 바꿔 낸다(B 프레임 표시 순서 흉내).
    - fail_keys: 이 열쇠의 입력은 DecodeError 를 낸다.
    """

    def __init__(self, delay: int = 0, reorder_pairs: bool = False, fail_keys: frozenset[FrameKey] = frozenset()):
        if delay < 0:
            raise ValueError("delay 는 0 이상이어야 한다")
        self.delay = delay
        self.reorder_pairs = reorder_pairs
        self.fail_keys = fail_keys
        self.configured: I0CodecInfo | None = None
        self.configure_count = 0
        self._need_keyframe = True
        self._buffer: deque[DecodedPicture] = deque()
        self._held: DecodedPicture | None = None

    def configure(self, codec: I0CodecInfo) -> None:
        self.reset()
        self.configured = codec
        self.configure_count += 1

    def decode(self, payload: bytes, key: FrameKey, keyframe: bool) -> list[DecodedPicture]:
        if self.configured is None:
            raise DecodeError("configure 전에 decode 를 불렀다")
        if self._need_keyframe and not keyframe:
            raise DecodeError(f"키프레임 전에 참조 프레임을 받았다: {key}")
        if key in self.fail_keys:
            self._need_keyframe = True
            raise DecodeError(f"가짜 디코딩 실패: {key}")
        self._need_keyframe = False
        content = json.loads(payload.decode("utf-8"))
        picture = DecodedPicture(
            key,
            FakeImage(content["camera_id"], content["stream_epoch"], content["frame_sequence"], content["t"]),
        )
        ready: list[DecodedPicture] = []
        if self.reorder_pairs:
            if self._held is None:
                self._held = picture
            else:
                ready += [picture, self._held]
                self._held = None
        else:
            ready.append(picture)
        out: list[DecodedPicture] = []
        for p in ready:
            self._buffer.append(p)
        while len(self._buffer) > self.delay:
            out.append(self._buffer.popleft())
        return out

    def flush(self) -> list[DecodedPicture]:
        out = list(self._buffer)
        if self._held is not None:
            out.append(self._held)
        self._buffer.clear()
        self._held = None
        return out

    def reset(self) -> None:
        self._buffer.clear()
        self._held = None
        self._need_keyframe = True
        self.configured = None
