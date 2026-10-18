"""I0 영상 연결의 길이 접두 레코드 형식 "simai-i0-video/1" (명세 12.7 전송, contracts/README.md "I0 전송 규칙").

노트북 → Tier 0 카메라별 TLS 연결 하나에 레코드를 차례로 싣는다. 헤더(JSON)와 압축 프레임을 한 레코드에 같이 싣는다
(영상과 시각을 다른 채널로 보내지 않는다, 12.7). Tier 0 쪽 해석기는 ai/services/tier0/video_ingest.py 이고, 두 쪽이 같은
바이트를 쓰는지는 양쪽 시험의 같은 기준 바이트(GOLDEN)로 확인한다.

    레코드 = kind(u8) | meta_len(u32 BE) | data_len(u32 BE) | meta(UTF-8 JSON 객체) | data(바이트)

| kind | 방향 | meta | data |
|---|---|---|---|
| 1 HELLO | 노트북 → Tier 0 | {protocol, camera_id, laptop_id, device_token} | 없음 |
| 2 HELLO_ACK | Tier 0 → 노트북 | {ok, reason?} | 없음 |
| 3 CODEC_INFO | 노트북 → Tier 0 | contracts codec_info | 없음 |
| 4 FRAME | 노트북 → Tier 0 | contracts frame_header | 압축 프레임(H.264 접근 단위, Annex B) |
| 5 SKIP_NOTICE | 노트북 → Tier 0 | {camera_id, stream_epoch, first_sequence, last_sequence} | 없음 |
| 6 STREAM_REJECT | Tier 0 → 노트북 | {reason, stream_epoch, current_epoch} | 없음 |

- 연결 순서: TLS 핸드셰이크 → 노트북이 서버 공개키 지문 확인(고정값) → HELLO(기기 토큰) → HELLO_ACK(ok) →
  CODEC_INFO(그 카메라의 현재 stream_epoch) → 키프레임부터 FRAME. 재접속마다 같은 순서다.
- SKIP_NOTICE: 노트북 송신 큐가 넘쳐 다음 키프레임까지 버린 frame_sequence 범위(노트북 skipped). Tier 0 는 이 범위의
  번호 빈칸을 전송 손실(gateway_to_runpod_loss)로 세지 않는다. 이 범위를 끝내는 키프레임 바로 앞에 보낸다.
- STREAM_REJECT: Tier 0 가 그 stream_epoch 를 받을 수 없을 때(이미 본 epoch 보다 작음: 같은 식별자가 다시 쓰일 수
  있다) 보내고 곧 끊는다. 노트북은 연결 중에 이것을 읽고, 더 큰 epoch 의 codec_info 가 올 때까지 다시 붙지 않는다.
- 상한: meta 64 KiB(HELLO 는 4 KiB), data 4 MiB. 넘으면 받는 쪽이 연결을 끊는다.
- 기기 토큰은 HELLO 의 meta 에만 실린다. 로그·오류 문장에 meta 를 그대로 넣지 않는다.
"""

from __future__ import annotations

import json
import struct
from typing import Any

PROTOCOL = "simai-i0-video/1"

HELLO = 1
HELLO_ACK = 2
CODEC_INFO = 3
FRAME = 4
SKIP_NOTICE = 5
STREAM_REJECT = 6
KINDS = frozenset({HELLO, HELLO_ACK, CODEC_INFO, FRAME, SKIP_NOTICE, STREAM_REJECT})

PREFIX = struct.Struct(">BII")
MAX_META = 64 * 1024
MAX_HELLO_META = 4 * 1024
MAX_DATA = 4 * 1024 * 1024


class WireError(Exception):
    """레코드 형식이 틀렸다(문장에 받은 값을 넣지 않는다)."""


def encode(kind: int, meta: dict[str, Any], data: bytes = b"") -> bytes:
    if kind not in KINDS:
        raise WireError("모르는 레코드 종류")
    body = json.dumps(meta, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > (MAX_HELLO_META if kind == HELLO else MAX_META):
        raise WireError("meta 가 상한을 넘는다")
    if len(data) > MAX_DATA:
        raise WireError("data 가 상한을 넘는다")
    return PREFIX.pack(kind, len(body), len(data)) + body + data


def _recv_exact(sock: Any, n: int) -> bytes | None:
    """n 바이트를 다 받으면 그 바이트, 첫 바이트 전에 연결이 닫히면 None. 중간에 닫히면 WireError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if not buf:
                return None
            raise WireError("레코드 중간에 연결이 닫혔다")
        buf.extend(chunk)
    return bytes(buf)


def read_record(sock: Any, *, max_meta: int = MAX_META, max_data: int = MAX_DATA) -> tuple[int, dict[str, Any], bytes] | None:
    """소켓에서 레코드 하나. 연결이 레코드 경계에서 닫히면 None."""
    prefix = _recv_exact(sock, PREFIX.size)
    if prefix is None:
        return None
    kind, meta_len, data_len = PREFIX.unpack(prefix)
    if kind not in KINDS:
        raise WireError("모르는 레코드 종류")
    if meta_len > max_meta or data_len > max_data:
        raise WireError("레코드 길이가 상한을 넘는다")
    raw = _recv_exact(sock, meta_len) if meta_len else b""
    data = _recv_exact(sock, data_len) if data_len else b""
    if raw is None or data is None:
        raise WireError("레코드 중간에 연결이 닫혔다")
    try:
        meta = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WireError("meta 가 UTF-8 JSON 이 아니다") from None
    if not isinstance(meta, dict):
        raise WireError("meta 가 JSON 객체가 아니다")
    return kind, meta, data
