"""Tier 0 I0 영상 수신: 카메라별 TLS 연결, 기기 토큰 확인, 길이 접두 레코드·헤더 계약 검증, gateway_to_runpod_loss 기록.

명세 12.7(카메라별 독립 TLS over TCP, 길이 접두, 재접속 시 codec_info → 키프레임, frame_sequence 빈 구간 = 노트북 → RunPod
손실, 노트북 송신 큐 skipped 는 손실과 따로), 14(TLS 는 Tier 0 가 직접 종료, 노트북 기기 토큰), 3.12(포트는 대칭 매핑).
계약: contracts/messages/codec_info·frame_header (봉투 없음, contracts/README.md "I0 전송 규칙").

바이트 형식 "simai-i0-video/1" (노트북 쪽 정본 설명은 gateway/relay/src/simai_relay/wire.py, 양쪽 시험이 같은 기준 바이트를 쓴다):

    레코드 = kind(u8) | meta_len(u32 BE) | data_len(u32 BE) | meta(UTF-8 JSON 객체) | data
    1 HELLO {protocol, camera_id, laptop_id, device_token}   2 HELLO_ACK {ok, reason?}
    3 CODEC_INFO (codec_info 계약)   4 FRAME (frame_header 계약 + 압축 프레임)
    5 SKIP_NOTICE {camera_id, stream_epoch, first_sequence, last_sequence}: 노트북 송신 큐가 넘쳐 버린 범위
    6 STREAM_REJECT {reason, stream_epoch, current_epoch}: Tier 0 → 노트북, 받을 수 없는 stream_epoch (보낸 뒤 끊는다)

- 연결마다 스레드 하나(카메라 사이에 잠금을 공유하지 않는다). 한 카메라의 연결이 느리거나 소비자가 늦어도 다른 카메라
  연결은 따로 읽는다. 같은 카메라의 새 연결이 오면(노트북 재접속) 이전 연결을 끊는다.
- 인증: TLS 핸드셰이크 → HELLO(시간 제한) → protocol·camera_id(그 포트에 허용된 카메라)·기기 토큰 확인 → HELLO_ACK.
  기기 토큰은 원문을 두지 않고 SHA-256 지문 목록(.env SIMAI_LAPTOP_TOKEN_SHA256, 회전 기간에는 쉼표로 여러 개)과 상수 시간
  비교한다. 토큰·HELLO meta 는 로그에 넣지 않는다.
- 헤더 검증: 실행 환경에 contracts/ 가 없어도 돌도록 표준 라이브러리로 스키마 규칙(필수·형식·추가 필드 금지)을 본다.
  같은 판정인지는 시험이 계약 스키마(jsonschema)와 맞춰 본다. 틀린 레코드가 오면 그 연결을 끊는다(노트북이 다시 붙어
  codec_info → 키프레임부터 다시 보낸다). 레코드의 camera_id 는 HELLO 의 camera_id 와 같아야 한다.
- 손실(`CameraStream`): 같은 stream_epoch 안 frame_sequence 빈칸 중 SKIP_NOTICE 가 알린 범위는 laptop_skipped, 나머지는
  gateway_to_runpod_loss(재요청하지 않는다). 재접속해도 같은 epoch 면 번호를 이어서 센다. SKIP_NOTICE 범위도 같은 epoch
  안에서는 재접속을 넘어 겹치거나 맞닿은 것끼리 합친 구간 집합으로 남겨 둔다(노트북은 재접속마다 구간 집합을 다시 보낸다.
  같은 알림을 여러 번 받아도 크기가 늘지 않는다). 서로 떨어진 구간이 MAX_SKIP_SPANS 를 넘거나 노트북이 unknown_before 로
  기록을 잘라 냈다고 알리면, 그 앞 빈칸은 손실로 지어내지 않고 unattributed_gap(구분 불가)으로 센다. 처음 보는 epoch 의
  첫 번호 앞은 모른다(지어내지 않는다). 줄어든 epoch 는 stale 로 버리고 경고를 남긴다(같은 (camera_id, stream_epoch,
  frame_sequence) 가 두 번 오면 다른 프레임에 시각이 붙을 수 있다). 그 codec_info 를 받으면 STREAM_REJECT 로 노트북에
  알리고 연결을 끊는다(새 TLS 연결이라는 이유로 epoch·번호 비교를 지우지 않는다: 같은 스트림의 전송 재접속일 수 있다).
  노트북 중계는 카메라별로 쓴 최대 epoch 를 저장해 재시작·시계 역행에도 epoch 가 줄지 않게 한다(gateway/relay EpochStore).
- 넘기기(`VideoConsumer`): codec_info 를 받은 뒤 키프레임부터 압축 프레임과 헤더를 넘긴다. -57 링 버퍼·검출 입력
  (simai_ai.io.I0Ingest 등)은 이 자리에 붙인다. 이 모듈은 디코딩하지 않는다.

환경 변수(.env, 비밀 값은 커밋하지 않는다):

    SIMAI_TIER0_TLS_CERT_FILE      TLS 서버 인증서 PEM (엔드포인트 등록의 공개키 지문과 같은 파일)
    SIMAI_TIER0_TLS_KEY_FILE       TLS 비밀 키 PEM (Network Volume 비밀 폴더)
    SIMAI_TIER0_VIDEO_PORTS        카메라별 영상 포트 요청 번호 "cam1:70001,..." (엔드포인트 등록과 같은 값)
    SIMAI_PUBLIC_PORT_<요청 번호>(없으면 RUNPOD_TCP_PORT_<요청 번호>)  대칭 매핑의 실제 포트. 이 포트에서 듣는다
    SIMAI_LAPTOP_TOKEN_SHA256      받아들일 노트북 기기 토큰의 sha256:<hex 64> (쉼표로 여러 개)
    SIMAI_TIER0_VIDEO_LISTEN_HOST  듣는 주소 (기본 0.0.0.0)

실행: `python -m services.tier0.video_ingest` (ai/ 에서). 소비자 없이 받아서 세기만 하고 카메라별 상태를 주기적으로 로그에 쓴다.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import re
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

log = logging.getLogger("services.tier0.video")

PROTOCOL = "simai-i0-video/1"
HELLO, HELLO_ACK, CODEC_INFO, FRAME, SKIP_NOTICE, STREAM_REJECT = 1, 2, 3, 4, 5, 6
KINDS = frozenset({HELLO, HELLO_ACK, CODEC_INFO, FRAME, SKIP_NOTICE, STREAM_REJECT})
MAX_SKIP_SPANS = 1024  # 카메라별로 기억하는 서로 떨어진 노트북 버림 구간 수(넘으면 오래된 구간을 잘라 내고 구분 불가로)
REJECT_DRAIN_S = 5.0  # STREAM_REJECT 뒤 노트북이 읽고 닫을 때까지 받은 것을 버리며 기다리는 시간
PREFIX = struct.Struct(">BII")
MAX_META = 64 * 1024
MAX_HELLO_META = 4 * 1024
MAX_DATA = 4 * 1024 * 1024
POLL_S = 0.5  # 연결 중 recv 시간 제한: 이만큼마다 멈춤·대체 요청을 본다

_CAMERA_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_TIMEBASE = re.compile(r"^1/[1-9][0-9]*$")
_BASE64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
_TOKEN_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_SOURCES = frozenset({"CAMERA_STREAM", "RECEIVE_ESTIMATED", "FILE", "SIM"})


class WireError(Exception):
    """레코드 형식이 틀렸다(문장에 받은 값을 넣지 않는다)."""


class HeaderError(ValueError):
    """codec_info·frame_header·SKIP_NOTICE 가 계약과 다르다. 문장에는 필드 이름만 넣는다."""


# ---------------------------------------------------------------------------- 레코드


def _reject_constant(name: str) -> Any:
    raise ValueError("NaN·Infinity 는 JSON 값이 아니다")


def encode(kind: int, meta: Mapping[str, Any], data: bytes = b"") -> bytes:
    """레코드 하나(Tier 0 는 HELLO_ACK 만 보낸다. 시험 송신에도 쓴다)."""
    if kind not in KINDS:
        raise WireError("모르는 레코드 종류")
    body = json.dumps(dict(meta), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > (MAX_HELLO_META if kind == HELLO else MAX_META) or len(data) > MAX_DATA:
        raise WireError("레코드 길이가 상한을 넘는다")
    return PREFIX.pack(kind, len(body), len(data)) + body + data


def _recv_exact(sock: Any, n: int, should_stop: Callable[[], bool] | None = None) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(min(n - len(buf), 1 << 20))
        except TimeoutError:
            # 짧은 시간 제한으로 깨어나 멈춤 요청을 본다(Windows 에서는 다른 스레드의 shutdown 이 recv 를 깨우지 않는다)
            if should_stop is None or should_stop():
                raise
            continue
        if not chunk:
            if not buf:
                return None
            raise WireError("레코드 중간에 연결이 닫혔다")
        buf.extend(chunk)
    return bytes(buf)


def read_record(sock: Any, *, max_meta: int = MAX_META, max_data: int = MAX_DATA,
                should_stop: Callable[[], bool] | None = None) -> tuple[int, dict[str, Any], bytes] | None:
    """레코드 하나. 연결이 레코드 경계에서 닫히면 None. 길이를 먼저 보고 상한을 넘으면 본문을 읽지 않고 WireError.

    should_stop 이 있으면 소켓 시간 제한마다 그것을 보고, 참이면 TimeoutError 를 낸다(없으면 시간 제한이 곧 오류).
    """
    prefix = _recv_exact(sock, PREFIX.size, should_stop)
    if prefix is None:
        return None
    kind, meta_len, data_len = PREFIX.unpack(prefix)
    if kind not in KINDS:
        raise WireError("모르는 레코드 종류")
    if meta_len > max_meta or data_len > max_data:
        raise WireError("레코드 길이가 상한을 넘는다")
    raw = _recv_exact(sock, meta_len, should_stop) if meta_len else b""
    data = _recv_exact(sock, data_len, should_stop) if data_len else b""
    if raw is None or data is None:
        raise WireError("레코드 중간에 연결이 닫혔다")
    try:
        meta = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant) if raw else {}
    except (UnicodeDecodeError, ValueError):
        raise WireError("meta 가 UTF-8 JSON 이 아니다") from None
    if not isinstance(meta, dict):
        raise WireError("meta 가 JSON 객체가 아니다")
    return kind, meta, data


# ---------------------------------------------------------------------------- 헤더 계약 검증


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _nonneg_int(m: Mapping[str, Any], key: str) -> None:
    if not _is_int(m.get(key)) or m[key] < 0:
        raise HeaderError(f"{key}: 0 이상 정수여야 한다")


def _camera_id(m: Mapping[str, Any]) -> None:
    v = m.get("camera_id")
    if not isinstance(v, str) or not _CAMERA_ID.match(v):
        raise HeaderError("camera_id 형식이 틀렸다")


def _timebase(m: Mapping[str, Any]) -> None:
    v = m.get("pts_timebase")
    if not isinstance(v, str) or not _TIMEBASE.match(v):
        raise HeaderError("pts_timebase 형식이 틀렸다 (1/N)")


def _closed(m: Mapping[str, Any], required: frozenset[str], optional: frozenset[str], what: str) -> None:
    missing = sorted(required - m.keys())
    if missing:
        raise HeaderError(f"{what}: 필수 필드가 없다 ({', '.join(missing)})")
    extra = sorted(m.keys() - required - optional)
    if extra:
        raise HeaderError(f"{what}: 계약에 없는 필드가 있다 ({len(extra)}개)")  # 이름도 받은 값이라 개수만


_FRAME_REQUIRED = frozenset({"camera_id", "stream_epoch", "frame_sequence", "estimated_capture_time_ms",
                             "timestamp_source", "timestamp_uncertainty_ms", "source_pts", "pts_timebase", "keyframe"})
_FRAME_OPTIONAL = frozenset({"received_at_ms"})
_CODEC_REQUIRED = frozenset({"camera_id", "stream_epoch", "codec", "sps", "pps", "pts_timebase", "width", "height"})
_CODEC_OPTIONAL = frozenset({"profile", "nominal_fps"})
_SKIP_FIELDS = frozenset({"camera_id", "stream_epoch", "first_sequence", "last_sequence"})
_SKIP_OPTIONAL = frozenset({"unknown_before"})  # 노트북이 이 번호 앞의 버린 구간 기록을 잘라 냈다


def validate_frame_header(m: Mapping[str, Any]) -> None:
    """contracts/messages/frame_header.schema.json 규칙. 틀리면 HeaderError."""
    _closed(m, _FRAME_REQUIRED, _FRAME_OPTIONAL, "frame_header")
    _camera_id(m)
    for key in ("stream_epoch", "frame_sequence", "estimated_capture_time_ms"):
        _nonneg_int(m, key)
    if m["timestamp_source"] not in TIMESTAMP_SOURCES:
        raise HeaderError("timestamp_source 값이 틀렸다")
    u = m["timestamp_uncertainty_ms"]
    if isinstance(u, bool) or not isinstance(u, (int, float)) or not math.isfinite(u) or u < 0:
        raise HeaderError("timestamp_uncertainty_ms: 0 이상의 유한한 수여야 한다")
    if m["source_pts"] is not None and not _is_int(m["source_pts"]):
        raise HeaderError("source_pts: 정수나 null 이어야 한다")
    _timebase(m)
    if not isinstance(m["keyframe"], bool):
        raise HeaderError("keyframe: bool 이어야 한다")
    if "received_at_ms" in m:
        _nonneg_int(m, "received_at_ms")


def validate_codec_info(m: Mapping[str, Any]) -> None:
    """contracts/messages/codec_info.schema.json 규칙. 틀리면 HeaderError."""
    _closed(m, _CODEC_REQUIRED, _CODEC_OPTIONAL, "codec_info")
    _camera_id(m)
    _nonneg_int(m, "stream_epoch")
    if m["codec"] != "H264":
        raise HeaderError("codec 은 H264 다")
    for key in ("sps", "pps"):
        v = m[key]
        if not isinstance(v, str) or len(v) < 4 or not _BASE64.match(v):
            raise HeaderError(f"{key}: base64 문자열이어야 한다")
    _timebase(m)
    for key in ("width", "height"):
        if not _is_int(m[key]) or m[key] < 1:
            raise HeaderError(f"{key}: 1 이상 정수여야 한다")
    if "profile" in m and not isinstance(m["profile"], str):
        raise HeaderError("profile: 문자열이어야 한다")
    if "nominal_fps" in m:
        f = m["nominal_fps"]
        if isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) or f <= 0:
            raise HeaderError("nominal_fps: 0 보다 큰 수여야 한다")


def validate_skip_notice(m: Mapping[str, Any]) -> None:
    _closed(m, _SKIP_FIELDS, _SKIP_OPTIONAL, "skip_notice")
    _camera_id(m)
    for key in ("stream_epoch", "first_sequence", "last_sequence"):
        _nonneg_int(m, key)
    if "unknown_before" in m:
        _nonneg_int(m, "unknown_before")
    if m["last_sequence"] < m["first_sequence"]:
        raise HeaderError("skip_notice: last_sequence < first_sequence")


# ---------------------------------------------------------------------------- 기기 토큰


def parse_token_hashes(text: str | None) -> frozenset[str]:
    """`sha256:<hex 64>` 목록(쉼표). 형식이 틀리면 ValueError(값은 문장에 넣지 않는다)."""
    out = set()
    for raw in (text or "").split(","):
        value = raw.strip().lower()
        if not value:
            continue
        if not _TOKEN_SHA.match(value):
            raise ValueError("SIMAI_LAPTOP_TOKEN_SHA256 은 'sha256:<소문자 hex 64자>' 목록이어야 한다")
        out.add(value)
    return frozenset(out)


def token_sha256(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


class TokenVerifier:
    """노트북 기기 토큰 확인. 원문 대신 SHA-256 지문만 갖고, 목록 전체를 상수 시간 비교로 본다."""

    def __init__(self, hashes: frozenset[str]) -> None:
        if not hashes:
            raise ValueError("받아들일 기기 토큰이 없다(.env 의 SIMAI_LAPTOP_TOKEN_SHA256). 인증 없이 열지 않는다")
        self._hashes = tuple(sorted(hashes))

    def __repr__(self) -> str:
        return f"TokenVerifier({len(self._hashes)} tokens)"

    def verify(self, token: Any) -> bool:
        if not isinstance(token, str) or not token:
            return False
        digest = token_sha256(token).encode()
        ok = False
        for h in self._hashes:
            ok |= hmac.compare_digest(digest, h.encode())
        return ok


# ---------------------------------------------------------------------------- 카메라별 스트림 상태와 손실


@dataclass
class CameraReceiveStats:
    connections: int = 0
    superseded: int = 0  # 같은 카메라의 새 연결이 와서 끊은 연결
    codec_infos: int = 0
    frames_received: int = 0  # 번호를 센 프레임(키프레임 대기 중 버린 것 포함)
    frames_forwarded: int = 0
    bytes_forwarded: int = 0
    gateway_to_runpod_loss: int = 0  # frame_sequence 빈칸 중 노트북이 버렸다고 알리지 않은 것 (NETWORK_LOSS)
    laptop_skipped: int = 0  # 노트북 송신 큐 넘침으로 버린 것(SKIP_NOTICE). 손실이 아니다
    unattributed_gap: int = 0  # 구분 불가: 버림 구간 기록을 잘라 내서 손실인지 노트북 버림인지 모르는 빈칸(어느 쪽에도 넣지 않음)
    skip_spans_truncated: int = 0  # 구간 상한을 넘어 잘라 낸 버림 구간 수
    before_codec_info: int = 0  # 그 epoch 의 codec_info 전에 온 프레임(버림)
    before_keyframe: int = 0  # codec_info 뒤 키프레임 전에 온 프레임(버림)
    stale_epoch: int = 0  # 지금보다 작은 stream_epoch 의 기록(버림)
    stale_epoch_frames: int = 0  # 그중 FRAME 기록(health 프레임 집계는 이것만 센다. codec_info 거절은 stream_rejects)
    stream_rejects: int = 0  # 작은 epoch 의 codec_info 로 STREAM_REJECT 를 보내고 끊은 연결
    out_of_order: int = 0  # 이미 지난 frame_sequence (버림)
    ignored_skip_notices: int = 0
    protocol_errors: int = 0  # 형식·계약 위반으로 끊은 연결
    consumer_errors: int = 0


class CameraStream:
    """카메라 한 대의 스트림 상태(연결이 바뀌어도 이어진다). 소켓 없이 시험할 수 있게 판단만 한다.

    on_* 는 그 레코드를 소비자에게 넘길지(True) 돌려준다. 한 카메라의 레코드는 그 카메라의 연결 스레드 하나에서만 온다
    (새 연결은 이전 연결이 끝난 뒤 시작한다).
    """

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self.stats = CameraReceiveStats()
        self.stream_epoch: int | None = None
        self.last_sequence: int | None = None
        self._codec_ok = False  # 이번 연결에서 지금 epoch 의 codec_info 를 받았다
        self._awaiting_keyframe = True
        self._skip_spans: list[list[int]] = []  # 지금 epoch 에서 노트북이 버렸다고 알린 구간(정렬, 서로 떨어짐, 정산 전)
        self._unknown_upto: int | None = None  # 이 번호까지의 빈칸은 구분 불가(버림 구간 기록이 잘렸다)
        self.max_skip_spans = MAX_SKIP_SPANS

    @property
    def ready(self) -> bool:
        """codec_info 와 키프레임이 모두 도착해 관측을 낼 수 있는 상태 (명세 12.7)."""
        return self._codec_ok and not self._awaiting_keyframe

    def connection_started(self) -> None:
        self.stats.connections += 1
        self._codec_ok = False
        self._awaiting_keyframe = True

    def connection_ended(self) -> None:
        self._codec_ok = False
        self._awaiting_keyframe = True

    def on_codec_info(self, codec: Mapping[str, Any]) -> bool:
        epoch = codec["stream_epoch"]
        if self.stream_epoch is not None and epoch < self.stream_epoch:
            self.stats.stale_epoch += 1
            log.warning("%s stream_epoch 이 줄었다(%d < %d). 같은 프레임 식별자가 다시 쓰일 수 있어 이 스트림을 버린다"
                        "(노트북 시계가 뒤로 갔거나 중계를 첫 epoch 0 으로 다시 띄웠다)", self.camera_id, epoch,
                        self.stream_epoch)
            return False
        if self.stream_epoch is None or epoch > self.stream_epoch:
            self.stream_epoch = epoch
            self.last_sequence = None  # 새 epoch: 번호를 새로 센다
            self._skip_spans = []
            self._unknown_upto = None
        self.stats.codec_infos += 1
        self._codec_ok = True
        self._awaiting_keyframe = True
        return True

    def on_skip_notice(self, notice: Mapping[str, Any]) -> None:
        first, last = notice["first_sequence"], notice["last_sequence"]
        if (notice["stream_epoch"] != self.stream_epoch or not self._codec_ok
                or (self.last_sequence is not None and last <= self.last_sequence)):
            self.stats.ignored_skip_notices += 1
            return
        if "unknown_before" in notice and notice["unknown_before"] > 0:
            self._mark_unknown(notice["unknown_before"] - 1)
        _add_span(self._skip_spans, first, last)  # 같은 구간을 여러 번 받아도 크기가 늘지 않는다
        while len(self._skip_spans) > self.max_skip_spans:
            _, dropped = self._skip_spans.pop(0)
            self.stats.skip_spans_truncated += 1
            self._mark_unknown(dropped)

    def _mark_unknown(self, upto: int) -> None:
        self._unknown_upto = upto if self._unknown_upto is None else max(self._unknown_upto, upto)

    def _skipped_in(self, lo: int, hi: int) -> int:
        """lo..hi 중 알린 구간에 드는 번호 수(구간은 이미 합쳐져 서로 떨어져 있다)."""
        return sum(max(0, min(b, hi) - max(a, lo) + 1) for a, b in self._skip_spans)

    def on_frame(self, header: Mapping[str, Any]) -> bool:
        epoch, seq = header["stream_epoch"], header["frame_sequence"]
        if self.stream_epoch is not None and epoch < self.stream_epoch:
            self.stats.stale_epoch += 1
            self.stats.stale_epoch_frames += 1
            return False
        if self.stream_epoch is None or epoch > self.stream_epoch or not self._codec_ok:
            self.stats.before_codec_info += 1
            return False
        if self.last_sequence is not None:
            if seq <= self.last_sequence:
                self.stats.out_of_order += 1
                return False
            gap = seq - self.last_sequence - 1
            if gap:
                lo, hi = self.last_sequence + 1, seq - 1
                skipped = self._skipped_in(lo, hi)
                unknown = 0
                if self._unknown_upto is not None and self._unknown_upto >= lo:
                    u_hi = min(hi, self._unknown_upto)
                    unknown = (u_hi - lo + 1) - self._skipped_in(lo, u_hi)  # 기록이 잘린 쪽은 손실로 지어내지 않는다
                self.stats.laptop_skipped += skipped
                self.stats.unattributed_gap += unknown
                self.stats.gateway_to_runpod_loss += gap - skipped - unknown
        self._skip_spans = [[a, b] for a, b in self._skip_spans if b > seq]  # 정산한 구간은 지운다
        if self._unknown_upto is not None and self._unknown_upto <= seq:
            self._unknown_upto = None
        self.last_sequence = seq
        self.stats.frames_received += 1
        if self._awaiting_keyframe:
            if not header["keyframe"]:
                self.stats.before_keyframe += 1
                return False
            self._awaiting_keyframe = False
        self.stats.frames_forwarded += 1
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"camera_id": self.camera_id, "stream_epoch": self.stream_epoch, "last_frame_sequence": self.last_sequence,
                "ready": self.ready, **asdict(self.stats)}


def _add_span(spans: list[list[int]], first: int, last: int) -> None:
    """정렬된 서로 떨어진 구간 목록에 [first, last] 를 더한다(겹치거나 맞닿은 구간은 합친다)."""
    merged: list[list[int]] = []
    placed = False
    for a, b in spans:
        if b < first - 1:
            merged.append([a, b])
        elif a > last + 1:
            if not placed:
                merged.append([first, last])
                placed = True
            merged.append([a, b])
        else:
            first, last = min(a, first), max(b, last)
    if not placed:
        merged.append([first, last])
    spans[:] = merged


# ---------------------------------------------------------------------------- 넘기는 자리


class VideoConsumer(Protocol):
    """받은 영상의 다음 단계(-57 링 버퍼, 디코딩·검출 입력). 카메라별 연결 스레드에서 불린다(오래 막히면 그 카메라만 밀린다)."""

    def on_codec_info(self, codec_info: Mapping[str, Any]) -> None: ...

    def on_frame(self, header: Mapping[str, Any], data: bytes) -> None: ...

    def on_disconnected(self, camera_id: str) -> None: ...


class NullConsumer:
    """아무 데도 넘기지 않는다(수신·손실 집계만)."""

    def on_codec_info(self, codec_info: Mapping[str, Any]) -> None:
        pass

    def on_frame(self, header: Mapping[str, Any], data: bytes) -> None:
        pass

    def on_disconnected(self, camera_id: str) -> None:
        pass


# ---------------------------------------------------------------------------- TLS 서버


@dataclass(frozen=True)
class ListenSpec:
    """듣는 포트 하나와 그 포트로 들어올 수 있는 카메라 (대칭 매핑이라 카메라가 포트를 같이 쓸 수 있다)."""

    port: int
    cameras: frozenset[str]
    host: str = "0.0.0.0"


def server_context(cert_file: str, key_file: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_file, key_file)
    return ctx


@dataclass
class ServerStats:
    tls_failures: int = 0
    auth_rejections: int = 0
    hello_errors: int = 0
    refused_busy: int = 0


@dataclass
class _Conn:
    tls: ssl.SSLSocket
    camera_id: str
    closed: threading.Event = field(default_factory=threading.Event)


class VideoIngestServer:
    """카메라별 TLS 영상 수신 서버. start() 뒤 포트마다 accept 스레드, 연결마다 수신 스레드가 돈다."""

    def __init__(self, listen: Sequence[ListenSpec], ssl_context: ssl.SSLContext, tokens: TokenVerifier,
                 consumer: VideoConsumer | None = None, *, hello_timeout_s: float = 5.0,
                 max_connections: int = 32) -> None:
        cams = [c for spec in listen for c in spec.cameras]
        if not listen or not cams:
            raise ValueError("들을 포트·카메라가 없다")
        self._listen = list(listen)
        self._ctx = ssl_context
        self._tokens = tokens
        self._consumer = consumer or NullConsumer()
        self._hello_timeout = hello_timeout_s
        self._slots = threading.BoundedSemaphore(max_connections)
        self.streams = {c: CameraStream(c) for c in sorted(set(cams))}
        self._active: dict[str, _Conn] = {}
        self._active_lock = threading.Lock()  # 카메라별 현재 연결 표만 (수신 경로에서는 잡지 않는다)
        self._camera_locks = {c: threading.Lock() for c in self.streams}  # 같은 카메라 연결이 겹치지 않게
        self.stats = ServerStats()
        self._socks: list[socket.socket] = []
        self.ports: list[int] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []  # accept·연결 스레드(종료 때 join)
        self._threads_lock = threading.Lock()

    def _spawn(self, target: Callable[..., None], args: tuple[Any, ...], name: str) -> None:
        t = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._threads_lock:
            self._threads = [x for x in self._threads if x.is_alive()]
            self._threads.append(t)
        t.start()

    def start(self) -> None:
        """포트마다 듣기 시작한다. 중간 포트에서 실패하면 이미 연 포트·스레드를 닫고 예외를 다시 낸다(부분 기동 금지)."""
        try:
            for spec in self._listen:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._socks.append(sock)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((spec.host, spec.port))
                sock.listen(16)
                sock.settimeout(0.5)
                self.ports.append(sock.getsockname()[1])
                self._spawn(self._accept, (sock, spec), f"i0-video-{spec.port}")
        except BaseException:
            self.stop()
            raise
        log.info("I0 영상 수신 시작: 포트 %s, 카메라 %s", self.ports, sorted(self.streams))

    def alive_threads(self) -> list[str]:
        with self._threads_lock:
            return [t.name for t in self._threads if t.is_alive()]

    def join(self, timeout_s: float) -> list[str]:
        """stop 뒤 accept·연결 스레드가 끝나기를 timeout_s 까지 기다린다. 끝나지 않은 스레드 이름을 돌려준다."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._threads_lock:
            threads = list(self._threads)
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        return self.alive_threads()

    def stop(self) -> None:
        """멈춘다. 여러 번 불러도 되고, 부분 기동 상태에서도 된다."""
        self._stop.set()
        for sock in self._socks:
            # Linux 는 accept 스레드가 poll 중이면 close() 만으로는 그 시간 제한까지 LISTEN 이 남는다. shutdown 이 바로 끊고 깨운다
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:  # Windows 는 듣기 소켓 shutdown 을 거절한다(close 만으로 닫힌다). 이미 닫힌 소켓도 여기로 온다
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._active_lock:
            conns = list(self._active.values())
        for conn in conns:
            self._kill(conn)

    def snapshots(self) -> list[dict[str, Any]]:
        with self._active_lock:
            active = set(self._active)
        return [{**s.snapshot(), "connected": c in active} for c, s in self.streams.items()]

    def connected_cameras(self) -> list[str]:
        with self._active_lock:
            return sorted(self._active)

    # ------------------------------------------------------------------ 연결

    def _accept(self, sock: socket.socket, spec: ListenSpec) -> None:
        while not self._stop.is_set():
            try:
                conn, peer = sock.accept()
            except (TimeoutError, OSError):
                continue
            if not self._slots.acquire(blocking=False):
                self.stats.refused_busy += 1
                conn.close()
                continue
            self._spawn(self._handle, (conn, peer, spec), f"i0-video-conn-{spec.port}")

    @staticmethod
    def _kill(conn: _Conn) -> None:
        """그 연결을 끝내라고 표시한다. 수신 스레드가 짧은 시간 제한(POLL_S)마다 보고 스스로 닫는다."""
        conn.closed.set()

    def _reject(self, tls: ssl.SSLSocket, reason: str) -> None:
        try:
            tls.sendall(encode(HELLO_ACK, {"ok": False, "reason": reason}))
        except OSError:
            pass

    def _hello(self, tls: ssl.SSLSocket, spec: ListenSpec, peer: str) -> str | None:
        """HELLO 를 받아 camera_id 를 돌려준다. 거절하면 None (이유를 HELLO_ACK 로 알리고 끊는다)."""
        try:
            rec = read_record(tls, max_meta=MAX_HELLO_META, max_data=0)
        except (WireError, OSError):
            self.stats.hello_errors += 1
            return None
        if rec is None or rec[0] != HELLO:
            self.stats.hello_errors += 1
            self._reject(tls, "bad_hello")
            return None
        meta = rec[1]
        if meta.get("protocol") != PROTOCOL:
            self.stats.hello_errors += 1
            self._reject(tls, "bad_protocol")
            return None
        # 토큰을 먼저 본다: 인증 전에는 어떤 카메라가 있는지 알려 주지 않는다
        if not self._tokens.verify(meta.get("device_token")):
            self.stats.auth_rejections += 1
            log.warning("I0 영상 연결 인증 실패 (%s)", peer)
            self._reject(tls, "unauthorized")
            return None
        camera_id = meta.get("camera_id")
        if not isinstance(camera_id, str) or camera_id not in spec.cameras:
            self.stats.hello_errors += 1
            log.warning("I0 영상 연결: 이 포트에 없는 카메라 (%s)", peer)
            self._reject(tls, "unknown_camera")
            return None
        return camera_id

    def _handle(self, conn: socket.socket, peer_addr: Any, spec: ListenSpec) -> None:
        peer = str(peer_addr[0]) if isinstance(peer_addr, tuple) else "?"
        try:
            conn.settimeout(self._hello_timeout)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                tls = self._ctx.wrap_socket(conn, server_side=True)
            except (OSError, ssl.SSLError):
                self.stats.tls_failures += 1
                conn.close()
                return
            with tls:
                camera_id = self._hello(tls, spec, peer)
                if camera_id is None:
                    return
                self._serve(tls, camera_id, peer)
        finally:
            self._slots.release()

    def _serve(self, tls: ssl.SSLSocket, camera_id: str, peer: str) -> None:
        current = _Conn(tls, camera_id)
        with self._active_lock:
            old = self._active.get(camera_id)
            self._active[camera_id] = current
        if old is not None:  # 노트북 재접속: 새 연결이 이긴다
            self.streams[camera_id].stats.superseded += 1
            self._kill(old)
        lock = self._camera_locks[camera_id]
        if not lock.acquire(timeout=self._hello_timeout + 5):  # 이전 연결 스레드가 끝나기를 기다린다
            self._reject(tls, "superseded")
            return
        stream = self.streams[camera_id]
        try:
            if current.closed.is_set():  # 기다리는 동안 더 새 연결이 왔다
                self._reject(tls, "superseded")
                return
            tls.sendall(encode(HELLO_ACK, {"ok": True}))
            tls.settimeout(POLL_S)
            stream.connection_started()
            log.info("%s I0 영상 연결 (%s)", camera_id, peer)
            self._read_loop(tls, stream, current)
        except (OSError, ssl.SSLError):
            pass
        finally:
            stream.connection_ended()
            with self._active_lock:
                if self._active.get(camera_id) is current:
                    del self._active[camera_id]
            self._notify_disconnected(camera_id)  # 다음 연결의 codec_info 보다 먼저 알린다
            lock.release()
            log.info("%s I0 영상 연결 끝", camera_id)

    @staticmethod
    def _reject_stream(tls: ssl.SSLSocket, epoch: int, current: int, should_stop: Callable[[], bool]) -> None:
        """STREAM_REJECT 를 보내고, 노트북이 읽고 닫을 때까지(최대 REJECT_DRAIN_S) 받은 것을 버린다.

        곧바로 닫으면 읽지 않은 데이터 때문에 RST 가 가서 노트북이 거절 이유를 못 읽을 수 있다.
        """
        try:
            tls.sendall(encode(STREAM_REJECT, {"reason": "stale_epoch", "stream_epoch": epoch, "current_epoch": current}))
            deadline = time.monotonic() + REJECT_DRAIN_S
            while time.monotonic() < deadline and not should_stop():
                try:
                    if read_record(tls) is None:
                        return
                except TimeoutError:
                    continue
        except (OSError, ssl.SSLError, WireError):
            return

    def _notify_disconnected(self, camera_id: str) -> None:
        try:
            self._consumer.on_disconnected(camera_id)
        except Exception as e:  # noqa: BLE001 - 소비자 오류가 수신을 멈추게 하지 않는다
            self.streams[camera_id].stats.consumer_errors += 1
            log.warning("%s 소비자 오류 (%s)", camera_id, type(e).__name__)

    def _read_loop(self, tls: ssl.SSLSocket, stream: CameraStream, conn: _Conn) -> None:
        cam = stream.camera_id
        def should_stop() -> bool:
            return conn.closed.is_set() or self._stop.is_set()

        while not should_stop():
            try:
                rec = read_record(tls, should_stop=should_stop)
                if rec is None:
                    return
                kind, meta, data = rec
                if kind == FRAME:
                    validate_frame_header(meta)
                elif kind == CODEC_INFO:
                    validate_codec_info(meta)
                elif kind == SKIP_NOTICE:
                    validate_skip_notice(meta)
                else:
                    raise WireError("연결 중에 올 수 없는 레코드")
                if meta["camera_id"] != cam:
                    raise HeaderError("camera_id 가 HELLO 와 다르다")
            except (WireError, HeaderError) as e:
                if conn.closed.is_set():
                    return
                stream.stats.protocol_errors += 1
                log.warning("%s I0 레코드 오류로 연결을 끊는다: %s", cam, e)
                return
            if (kind == CODEC_INFO and stream.stream_epoch is not None
                    and meta["stream_epoch"] < stream.stream_epoch):
                current = stream.stream_epoch
                stream.on_codec_info(meta)  # stale 로 세고 경고를 남긴다
                stream.stats.stream_rejects += 1
                self._reject_stream(tls, meta["stream_epoch"], current, should_stop)
                return
            try:
                if kind == FRAME:
                    if stream.on_frame(meta):
                        stream.stats.bytes_forwarded += len(data)
                        self._consumer.on_frame(meta, data)
                elif kind == CODEC_INFO:
                    if stream.on_codec_info(meta):
                        self._consumer.on_codec_info(meta)
                else:
                    stream.on_skip_notice(meta)
            except Exception as e:  # noqa: BLE001 - 소비자 오류가 그 카메라 수신을 끊지 않는다
                stream.stats.consumer_errors += 1
                log.warning("%s 소비자 오류 (%s)", cam, type(e).__name__)


# ---------------------------------------------------------------------------- 설정·실행


def listen_specs_from_env(env: Mapping[str, str]) -> list[ListenSpec]:
    """SIMAI_TIER0_VIDEO_PORTS + 공개 포트 환경 변수 → 듣는 포트별 카메라. 모르는 포트는 지어내지 않는다."""
    from services.tier0.endpoint_registration import parse_video_ports, public_port_env

    text = (env.get("SIMAI_TIER0_VIDEO_PORTS") or "").strip()
    if not text:
        raise ValueError("SIMAI_TIER0_VIDEO_PORTS 가 없다")
    host = (env.get("SIMAI_TIER0_VIDEO_LISTEN_HOST") or "0.0.0.0").strip()
    by_port: dict[int, set[str]] = {}
    problems = []
    for req in parse_video_ports(text):
        port_name, raw = public_port_env(env, req.requested_port)
        if not raw.isdigit() or not 1 <= int(raw) <= 65535:
            problems.append(port_name)
            continue
        assert req.camera_id is not None
        if not _CAMERA_ID.match(req.camera_id):
            problems.append(f"camera_id {req.camera_id!r}")
            continue
        by_port.setdefault(int(raw), set()).add(req.camera_id)
    if problems:
        raise ValueError("영상 포트를 모른다: " + ", ".join(problems))
    return [ListenSpec(port, frozenset(cams), host) for port, cams in sorted(by_port.items())]


def build_server(env: Mapping[str, str], consumer: VideoConsumer | None = None) -> VideoIngestServer:
    cert = (env.get("SIMAI_TIER0_TLS_CERT_FILE") or "").strip()
    key = (env.get("SIMAI_TIER0_TLS_KEY_FILE") or "").strip()
    if not cert or not key:
        raise ValueError("SIMAI_TIER0_TLS_CERT_FILE·SIMAI_TIER0_TLS_KEY_FILE 가 필요하다")
    tokens = TokenVerifier(parse_token_hashes(env.get("SIMAI_LAPTOP_TOKEN_SHA256")))
    return VideoIngestServer(listen_specs_from_env(env), server_context(cert, key), tokens, consumer)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="services.tier0.video_ingest", description="Tier 0 I0 카메라별 TLS 영상 수신")
    parser.add_argument("--report-interval", type=float, default=10.0)
    parser.add_argument("--seconds", type=float, default=None, help="이 시간 뒤 멈춘다 (없으면 Ctrl+C 까지)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        server = build_server(dict(os.environ if env is None else env))
        server.start()
    except (ValueError, OSError, ssl.SSLError) as e:
        print(f"시작 실패: {e}", file=sys.stderr)
        return 2
    end = None if args.seconds is None else time.monotonic() + args.seconds
    try:
        while end is None or time.monotonic() < end:
            time.sleep(args.report_interval if end is None else max(0.0, min(args.report_interval, end - time.monotonic())))
            for snap in server.snapshots():
                log.info("상태 %s", json.dumps(snap, ensure_ascii=False))
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
