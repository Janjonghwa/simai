"""노트북–Pod 시계 차이 추정 (명세 3.9, 6.1, FR-139, 부록 D-G0-12).

Tier 0 가 I0 제어 채널에서 NTP 식 4-타임스탬프 교환으로 노트북 시계와 자기 시계의 차이를 잰다.

    t1 = Tier 0 요청 송신   t2 = 노트북 요청 수신   t3 = 노트북 응답 송신   t4 = Tier 0 응답 수신
    offset     = ((t2 - t1) + (t3 - t4)) / 2        노트북 시계 - Tier 0 시계 (노트북이 빠르면 양수)
    round_trip = (t4 - t1) - (t3 - t2)

인과율만으로 참 offset 은 [t3 - t4, t2 - t1] 안에 있다(폭 = round_trip, 가운데 = offset). 각 표본의 구간 반폭은
round_trip/2 + 양자화 1ms + 표류(max_drift_ppm × 표본 나이)다. 그래서
- offset 은 추정 창 안에서 round_trip 이 가장 짧은 표본의 값을 쓴다(계약).
- 불확실성은 그 표본의 반폭과, 가장 최근 표본 구간을 덮는 폭(|offset 차이| + 최근 표본 반폭) 중 큰 값이다.
  창 안에서 노트북 시계가 구간 폭보다 작게 뛰면 옛 표본으로는 알아챌 수 없지만, 참값은 언제나 가장 최근
  표본의 구간 안에 있으므로 이렇게 하면 보고한 불확실성이 참값을 놓치지 않는다. 계약의 "round_trip_ms / 2 이상"도 지킨다.
- 새 표본의 구간과 겹치지 않는 옛 표본은 시계가 바뀌기 전의 것이므로 버린다(노트북 시계 점프 처리).
  시계가 그대로면 모든 구간이 참값을 품어 서로 겹치므로 정상 표본을 잘못 버리지 않는다.
- 연결은 살아 있는데 교환만 늦으면 마지막 추정을 offset_hold_ms 동안 쓰고 불확실성을 표류만큼 키운다.
  그보다 오래됐거나 표본이 없거나 연결이 끊기면 비운다(null).

Tier 0 쪽 t1·t4 는 단조 시계(Tier0Clock)로 잰다. 왕복은 벽시계 점프와 무관하고, UTC 값은 추정할 때마다
현재 기준점으로 다시 옮기므로 Tier 0 벽시계가 뛰거나 거꾸로 가도 표본을 버리지 않는다.

계약: contracts/messages/clock_sync_request·clock_sync_response, health.laptop_clock_offset.
현재 시각은 주입받는다(Tier0Clock 이 받은 함수만 읽는다). 전송은 ClockSyncChannel 로 주입한다.
노트북 쪽 응답 생성(build_clock_sync_response)은 전송과 무관한 순수 함수다. 실제 연결은 게이트웨이 몫이다.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from simai_ai.tracking.time_model import ClockOffset, require_ms

SCHEMA_VERSION = "1.3.0"  # clock_sync_request·response, health.laptop_clock_offset 이 들어온 계약 버전
REQUEST_MSG_TYPE = "clock_sync_request"
RESPONSE_MSG_TYPE = "clock_sync_response"
QUANTIZATION_MS = 1.0  # 노트북 시각(t2·t3)이 정수 ms 라 생기는 offset 오차 상한
_NS_PER_MS = 1_000_000
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


class ClockSyncMessageError(ValueError):
    """clock_sync 메시지 형식이 계약과 다르다."""


# --- 공식 (계약 health.clock_offset_estimate) ---


def compute_offset_ms(t1: float, t2: float, t3: float, t4: float) -> float:
    """offset = ((t2 - t1) + (t3 - t4)) / 2. 노트북 시계 - Tier 0 시계."""
    return ((t2 - t1) + (t3 - t4)) / 2.0


def compute_round_trip_ms(t1: float, t2: float, t3: float, t4: float) -> float:
    """round_trip = (t4 - t1) - (t3 - t2). 노트북 처리 시간은 빠진다."""
    return (t4 - t1) - (t3 - t2)


# --- Tier 0 시계 ---


@dataclass(frozen=True)
class Tier0Instant:
    """Tier 0 의 한 순간. mono_ns 는 단조 시계, utc_ms 는 그 순간의 기준점으로 옮긴 UTC ms(내림)."""

    mono_ns: int
    utc_ms: int


class Tier0Clock:
    """단조 시계를 기준으로 UTC ms 를 낸다.

    첫 now() 에서 (벽시계, 단조 시계) 기준점을 잡고, 이후 UTC = 기준 벽시계 + 단조 경과로 계산한다.
    벽시계가 이 값에서 jump_threshold_ms 넘게 벗어나면(NTP step, 수동 변경, 역행) 기준점을 다시 잡고
    epoch 를 1 올린다. 그보다 작은 흔들림·역행은 단조 시계가 흡수하므로 시각이 거꾸로 가지 않는다.
    t_risk_now 와 Risk 판정 시각은 이 시계를 감싼 decision_time.DecisionClock(앞으로만 감)에서 얻는다.

    monotonic_ns: 단조 시계(ns, 예 time.monotonic_ns). wall_ms: 벽시계 UTC ms(예 time.time_ns() // 10**6).
    """

    def __init__(
        self,
        monotonic_ns: Callable[[], int],
        wall_ms: Callable[[], int],
        jump_threshold_ms: float = 50.0,
    ) -> None:
        if not jump_threshold_ms > 0:
            raise ValueError("jump_threshold_ms 는 0 보다 커야 한다")
        self._monotonic_ns = monotonic_ns
        self._wall_ms = wall_ms
        self._threshold = float(jump_threshold_ms)
        self._anchor: tuple[int, int] | None = None  # (wall_ms, mono_ns)
        self._last_mono: int | None = None
        self.epoch = 0

    def to_utc_ms(self, mono_ns: int) -> float:
        """단조 시각을 현재 기준점으로 UTC ms 로 옮긴다(소수 포함)."""
        if self._anchor is None:
            raise RuntimeError("Tier0Clock: now() 를 먼저 불러 기준점을 잡아야 한다")
        wall0, mono0 = self._anchor
        return wall0 + (mono_ns - mono0) / _NS_PER_MS

    def now(self) -> Tier0Instant:
        mono = self._monotonic_ns()
        if isinstance(mono, bool) or not isinstance(mono, int):
            raise TypeError(f"monotonic_ns: int 여야 한다 (받은 값 {mono!r})")
        if self._last_mono is not None and mono < self._last_mono:
            raise ValueError(f"단조 시계가 뒤로 갔다: {self._last_mono} -> {mono}")
        wall = require_ms("wall_ms", self._wall_ms())
        if self._anchor is None:
            self._anchor = (wall, mono)
        elif abs(wall - self.to_utc_ms(mono)) > self._threshold:
            self._anchor = (wall, mono)
            self.epoch += 1
        self._last_mono = mono
        return Tier0Instant(mono_ns=mono, utc_ms=require_ms("utc_ms", math.floor(self.to_utc_ms(mono))))


# --- 설정·표본·추정 ---


@dataclass(frozen=True)
class ClockSyncConfig:
    """교환과 추정의 설정. 초기값과 근거는 docs/design/tier0-clock-offset.md.

    exchange_period_ms: 교환 주기(request_due 기준). 접속·재접속 직후에는 바로 보낸다.
    request_timeout_ms: 이보다 늦게 온 응답은 버린다.
    max_pending: 응답을 기다리는 요청 수 상한(넘으면 가장 오래된 것을 버린다).
    window_ms, max_samples: 추정 창(시간·개수).
    offset_hold_ms: 유지 상한. 마지막 유효 표본이 이보다 오래되면 추정을 비운다(null). 그 전까지는 마지막 추정을
        표류만큼 넓힌 불확실성으로 쓴다(연결은 살아 있고 교환만 늦을 때).
    max_responder_hold_ms: 노트북 처리 시간(t3 - t2) 상한. 응답은 받은 즉시 보내야 하므로 넘으면 표본을 버린다.
    max_drift_ppm: 두 시계의 상대 표류 상한. 표본 나이에 비례해 불확실성을 키우고 점프 판정에도 쓴다.
    """

    exchange_period_ms: int = 1000
    request_timeout_ms: int = 2000
    max_pending: int = 8
    window_ms: int = 60_000
    max_samples: int = 64
    offset_hold_ms: int = 60_000
    max_responder_hold_ms: int = 100
    max_drift_ppm: float = 100.0

    def __post_init__(self) -> None:
        for name in ("exchange_period_ms", "request_timeout_ms", "max_pending", "window_ms", "max_samples"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} 는 0 보다 커야 한다")
        if not 0 < self.offset_hold_ms <= self.window_ms:
            raise ValueError("0 < offset_hold_ms <= window_ms 여야 한다")
        if self.max_responder_hold_ms < 0 or not self.max_drift_ppm >= 0:
            raise ValueError("max_responder_hold_ms, max_drift_ppm 는 0 이상이어야 한다")


@dataclass(frozen=True)
class ClockSample:
    """교환 한 번의 표본. t1·t4 는 단조 시계(ns), t2·t3 는 노트북 시계(UTC ms)."""

    exchange_id: str
    t1_mono_ns: int
    t4_mono_ns: int
    t2_ms: int
    t3_ms: int

    @property
    def round_trip_ms(self) -> float:
        return (self.t4_mono_ns - self.t1_mono_ns) / _NS_PER_MS - (self.t3_ms - self.t2_ms)

    def offset_ms(self, clock: Tier0Clock) -> float:
        """현재 Tier 0 기준점으로 옮긴 offset."""
        t1 = clock.to_utc_ms(self.t1_mono_ns)
        t4 = clock.to_utc_ms(self.t4_mono_ns)
        return compute_offset_ms(t1, self.t2_ms, self.t3_ms, t4)


@dataclass(frozen=True)
class ClockOffsetEstimate:
    """health.laptop_clock_offset (contracts health.schema.json#/$defs/clock_offset_estimate)."""

    offset_ms: float
    uncertainty_ms: float
    round_trip_ms: float
    sample_count: int
    estimated_at: int

    @property
    def offset(self) -> ClockOffset:
        """time_model 의 T_lag 보정에 넘길 값."""
        return ClockOffset(offset_ms=self.offset_ms, uncertainty_ms=self.uncertainty_ms)

    def to_contract(self) -> dict[str, Any]:
        return {
            "offset_ms": self.offset_ms,
            "uncertainty_ms": self.uncertainty_ms,
            "round_trip_ms": self.round_trip_ms,
            "sample_count": self.sample_count,
            "estimated_at": self.estimated_at,
        }


class ClockOffsetEstimator:
    """추정 창의 표본에서 왕복 최소 표본으로 offset 과 불확실성을 낸다. 전송과 무관하다."""

    def __init__(self, clock: Tier0Clock, config: ClockSyncConfig = ClockSyncConfig()) -> None:
        self._clock = clock
        self._config = config
        self._samples: deque[ClockSample] = deque(maxlen=config.max_samples)
        self.laptop_jumps = 0  # 점프로 옛 표본을 버린 횟수 (기록용)

    def __len__(self) -> int:
        return len(self._samples)

    def clear(self) -> None:
        self._samples.clear()

    def _drift_ms(self, age_ns: int) -> float:
        return self._config.max_drift_ppm * 1e-6 * max(age_ns, 0) / _NS_PER_MS

    def _half_width(self, sample: ClockSample, at_mono_ns: int) -> float:
        return sample.round_trip_ms / 2.0 + QUANTIZATION_MS + self._drift_ms(at_mono_ns - sample.t4_mono_ns)

    def _purge(self, now_mono_ns: int) -> None:
        window_ns = self._config.window_ms * _NS_PER_MS
        while self._samples and now_mono_ns - self._samples[0].t4_mono_ns > window_ns:
            self._samples.popleft()

    def add(self, sample: ClockSample) -> bool:
        """표본을 넣는다. 새 표본과 구간이 겹치지 않는 옛 표본을 버렸으면(노트북 시계 점프) True."""
        if sample.round_trip_ms < 0:
            raise ValueError(f"round_trip 이 음수인 표본은 넣지 않는다: {sample}")
        self._purge(sample.t4_mono_ns)
        new_offset = sample.offset_ms(self._clock)
        new_half = sample.round_trip_ms / 2.0 + QUANTIZATION_MS
        kept = [
            s
            for s in self._samples
            if abs(s.offset_ms(self._clock) - new_offset) <= self._half_width(s, sample.t4_mono_ns) + new_half
        ]
        jumped = len(kept) != len(self._samples)
        if jumped:
            self.laptop_jumps += 1
            self._samples.clear()
            self._samples.extend(kept)
        self._samples.append(sample)
        return jumped

    def estimate(self, now: Tier0Instant) -> ClockOffsetEstimate | None:
        """현재 추정. 표본이 없거나 마지막 표본이 offset_hold_ms 보다 오래되면 None (0 으로 채우지 않는다)."""
        self._purge(now.mono_ns)
        if not self._samples:
            return None
        latest = self._samples[-1]
        if now.mono_ns - latest.t4_mono_ns > self._config.offset_hold_ms * _NS_PER_MS:
            return None
        # 왕복이 같으면 최근 표본(표류가 작다)
        best = min(reversed(self._samples), key=lambda s: s.round_trip_ms)
        offset = best.offset_ms(self._clock)
        # 참값은 가장 최근 표본 구간 안에 있다: 그 구간을 덮도록 넓힌다(작은 시계 점프 대비, 모듈 설명)
        cover_latest = abs(latest.offset_ms(self._clock) - offset) + self._half_width(latest, now.mono_ns)
        return ClockOffsetEstimate(
            offset_ms=offset,
            uncertainty_ms=max(self._half_width(best, now.mono_ns), cover_latest),
            round_trip_ms=best.round_trip_ms,
            sample_count=len(self._samples),
            estimated_at=require_ms("estimated_at", math.floor(self._clock.to_utc_ms(best.t4_mono_ns))),
        )


# --- 메시지 ---


def _require_uuid(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        raise ClockSyncMessageError(f"{name}: 소문자 UUID 문자열이어야 한다 (받은 값 {value!r})")
    return value


def _require_msg_ms(name: str, value: Any) -> int:
    try:
        return require_ms(name, value)
    except (TypeError, ValueError) as e:
        raise ClockSyncMessageError(str(e)) from e


def _payload(message: Mapping[str, Any], msg_type: str) -> Mapping[str, Any]:
    if not isinstance(message, Mapping) or message.get("msg_type") != msg_type:
        raise ClockSyncMessageError(f"msg_type 이 {msg_type} 가 아니다")
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        raise ClockSyncMessageError("payload 가 객체가 아니다")
    return payload


def _envelope(msg_type: str, *, msg_id: str, sent_at: int, source_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(source_id, str) or not source_id:
        raise ClockSyncMessageError("source_id 는 빈 문자열이 아니어야 한다")
    return {
        "schema_version": SCHEMA_VERSION,
        "msg_type": msg_type,
        "msg_id": _require_uuid("msg_id", msg_id),
        "sent_at": _require_msg_ms("sent_at", sent_at),
        "source_id": source_id,
        "payload": payload,
    }


def build_clock_sync_request(*, exchange_id: str, t1_ms: int, msg_id: str, source_id: str) -> dict[str, Any]:
    """Tier 0 → 노트북 clock_sync_request. sent_at 은 t1_ms 와 같다."""
    payload = {"exchange_id": _require_uuid("exchange_id", exchange_id), "t1_ms": _require_msg_ms("t1_ms", t1_ms)}
    return _envelope(REQUEST_MSG_TYPE, msg_id=msg_id, sent_at=t1_ms, source_id=source_id, payload=payload)


def build_clock_sync_response(
    request: Mapping[str, Any], *, t2_ms: int, t3_ms: int, msg_id: str, source_id: str
) -> dict[str, Any]:
    """노트북 쪽 응답 생성 (순수 함수, 전송과 무관).

    request: 받은 clock_sync_request. t2_ms: 노트북 시계의 요청 수신 시각(받자마자 잰 값).
    t3_ms: 노트북 시계의 응답 송신 시각(보내기 직전에 잰 값). t1_ms·exchange_id 는 그대로 돌려준다.
    sent_at 은 t3_ms 와 같다. t3_ms < t2_ms 면 ClockSyncMessageError.
    """
    payload = _payload(request, REQUEST_MSG_TYPE)
    exchange_id = _require_uuid("exchange_id", payload.get("exchange_id"))
    t1 = _require_msg_ms("t1_ms", payload.get("t1_ms"))
    t2 = _require_msg_ms("t2_ms", t2_ms)
    t3 = _require_msg_ms("t3_ms", t3_ms)
    if t3 < t2:
        raise ClockSyncMessageError(f"t3_ms({t3}) 가 t2_ms({t2}) 보다 작다")
    body = {"exchange_id": exchange_id, "t1_ms": t1, "t2_ms": t2, "t3_ms": t3}
    return _envelope(RESPONSE_MSG_TYPE, msg_id=msg_id, sent_at=t3, source_id=source_id, payload=body)


# --- Tier 0 교환 세션 ---


class ClockSyncChannel(Protocol):
    """I0 제어 채널의 송신 쪽. 실제 WebSocket 연결은 Tier 0 서비스 조립에서 주입한다."""

    def send(self, message: dict[str, Any]) -> None: ...


class ResponseOutcome(StrEnum):
    """응답 처리 결과."""

    ACCEPTED = "ACCEPTED"
    UNKNOWN_EXCHANGE = "UNKNOWN_EXCHANGE"  # 모르는 exchange_id, 중복 응답, 끊김으로 버린 요청의 응답
    TIMED_OUT = "TIMED_OUT"  # request_timeout_ms 보다 늦게 옴
    T1_MISMATCH = "T1_MISMATCH"  # 돌려준 t1_ms 가 보낸 값과 다름
    INVALID = "INVALID"  # 형식 오류, t3 < t2
    HOLD_TOO_LONG = "HOLD_TOO_LONG"  # t3 - t2 > max_responder_hold_ms
    NEGATIVE_ROUND_TRIP = "NEGATIVE_ROUND_TRIP"  # 교환 중 노트북 시계가 뛴 경우 등


@dataclass(frozen=True)
class ResponseResult:
    outcome: ResponseOutcome
    sample: ClockSample | None = None
    laptop_jump: bool = False  # 이 표본 때문에 옛 표본을 버렸는지


@dataclass(frozen=True)
class _Pending:
    t1_mono_ns: int
    t1_ms: int


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Tier0ClockSync:
    """Tier 0 쪽 시계 교환: 요청 생성(t1), 응답 수신(t4)으로 표본을 만들고 추정한다.

    사용: 노트북 제어 채널이 (재)접속되면 바로 send_request() 하고, 이후 request_due() 일 때마다 보낸다.
    응답은 handle_response() 로 넘긴다(수신 시각을 전송 계층에서 잰 Tier0Instant 를 주면 더 정확하다).
    채널이 끊기면 link_lost() 를 불러 대기 중인 요청과 추정을 비운다(health 는 null).
    """

    def __init__(
        self,
        clock: Tier0Clock,
        channel: ClockSyncChannel,
        *,
        source_id: str,
        config: ClockSyncConfig = ClockSyncConfig(),
        new_uuid: Callable[[], str] = _new_uuid,
    ) -> None:
        self._clock = clock
        self._channel = channel
        self._source_id = source_id
        self._config = config
        self._new_uuid = new_uuid
        self._pending: OrderedDict[str, _Pending] = OrderedDict()
        self._last_sent_mono: int | None = None
        self.estimator = ClockOffsetEstimator(clock, config)

    def _drop_expired(self, now_mono_ns: int) -> None:
        limit = self._config.request_timeout_ms * _NS_PER_MS
        for exchange_id in [k for k, p in self._pending.items() if now_mono_ns - p.t1_mono_ns > limit]:
            del self._pending[exchange_id]

    def request_due(self) -> bool:
        """보낸 적이 없거나(접속 직후 포함) 마지막 요청 뒤 exchange_period_ms 가 지났으면 True."""
        if self._last_sent_mono is None:
            return True
        return self._clock.now().mono_ns - self._last_sent_mono >= self._config.exchange_period_ms * _NS_PER_MS

    def send_request(self) -> str:
        """clock_sync_request 를 만들어 보낸다. t1 은 보내기 직전에 잰다. exchange_id 를 돌려준다."""
        exchange_id = self._new_uuid()
        msg_id = self._new_uuid()
        now = self._clock.now()
        self._drop_expired(now.mono_ns)
        message = build_clock_sync_request(
            exchange_id=exchange_id, t1_ms=now.utc_ms, msg_id=msg_id, source_id=self._source_id
        )
        while len(self._pending) >= self._config.max_pending:
            self._pending.popitem(last=False)
        self._pending[exchange_id] = _Pending(t1_mono_ns=now.mono_ns, t1_ms=now.utc_ms)
        try:
            self._channel.send(message)
        except BaseException:
            self._pending.pop(exchange_id, None)
            raise
        self._last_sent_mono = now.mono_ns
        return exchange_id

    def handle_response(self, message: Mapping[str, Any], received: Tier0Instant | None = None) -> ResponseResult:
        """clock_sync_response 를 처리한다. received 가 없으면 지금을 t4 로 쓴다."""
        t4 = received if received is not None else self._clock.now()
        try:
            payload = _payload(message, RESPONSE_MSG_TYPE)
            exchange_id = _require_uuid("exchange_id", payload.get("exchange_id"))
            t1_echo = _require_msg_ms("t1_ms", payload.get("t1_ms"))
            t2 = _require_msg_ms("t2_ms", payload.get("t2_ms"))
            t3 = _require_msg_ms("t3_ms", payload.get("t3_ms"))
        except ClockSyncMessageError:
            return ResponseResult(ResponseOutcome.INVALID)
        pending = self._pending.pop(exchange_id, None)
        if pending is None:
            return ResponseResult(ResponseOutcome.UNKNOWN_EXCHANGE)
        if t4.mono_ns - pending.t1_mono_ns > self._config.request_timeout_ms * _NS_PER_MS:
            return ResponseResult(ResponseOutcome.TIMED_OUT)
        if t1_echo != pending.t1_ms:
            return ResponseResult(ResponseOutcome.T1_MISMATCH)
        if t3 < t2:
            return ResponseResult(ResponseOutcome.INVALID)
        if t3 - t2 > self._config.max_responder_hold_ms:
            return ResponseResult(ResponseOutcome.HOLD_TOO_LONG)
        sample = ClockSample(exchange_id, pending.t1_mono_ns, t4.mono_ns, t2, t3)
        if sample.round_trip_ms < 0:
            return ResponseResult(ResponseOutcome.NEGATIVE_ROUND_TRIP)
        jumped = self.estimator.add(sample)
        return ResponseResult(ResponseOutcome.ACCEPTED, sample=sample, laptop_jump=jumped)

    def link_lost(self) -> None:
        """제어 채널이 끊겼다. 이전 추정을 유지하지 않는다(health 계약: 끊기면 null)."""
        self._pending.clear()
        self.estimator.clear()
        self._last_sent_mono = None

    def estimate(self) -> ClockOffsetEstimate | None:
        return self.estimator.estimate(self._clock.now())

    @property
    def generation(self) -> int:
        """시계 세대. 노트북 시계 점프를 감지하거나(옛 표본 폐기) Tier 0 기준점이 바뀔 때마다 늘어난다.

        decision_time.ClockAwareVerification 이 점프 뒤 해소 유지 시간을 처음부터 다시 채우게 하는 데 쓴다.
        """
        return self.estimator.laptop_jumps + self._clock.epoch

    def capture_clock(self, gateway_link_ok: bool) -> ClockOffset | None:
        """time_model·system_health 의 capture_clock 값. 연결이 끊겼거나 추정이 없으면 None(모름)."""
        estimate = self.estimate() if gateway_link_ok else None
        return None if estimate is None else estimate.offset

    def health_field(self, gateway_link_ok: bool) -> dict[str, Any] | None:
        """health.laptop_clock_offset 값. 연결이 끊겼거나 추정이 없으면 None."""
        return laptop_clock_offset_field(self.estimate(), gateway_link_ok)


def laptop_clock_offset_field(estimate: ClockOffsetEstimate | None, gateway_link_ok: bool) -> dict[str, Any] | None:
    """health.laptop_clock_offset 을 만든다. 노트북 연결이 끊기면 이전 추정이 있어도 None (모르면 비움)."""
    if not gateway_link_ok or estimate is None:
        return None
    return estimate.to_contract()
