"""I1 송신 스케줄러와 어댑터: 무엇을 언제 EC2 로 보낼지 (명세 12.2·12.3 I1 위로, 3.2 outbox·재동기화·구성 묶음, 3.12).

| 메시지 | 언제 | 경로 |
|---|---|---|
| health | (재)연결마다 첫 메시지, 그 뒤 1 Hz | 최신 칸. 만들 수 있을 때까지 그 연결의 다른 송신을 열지 않는다 |
| world_state | 10 Hz. 파이프라인이 offer_world_state 로 넣은 최신 payload 만, 새것이 있을 때만 | 최신 칸(밀리면 오래된 것 버림·집계) |
| risk_event_transition | outbox.pump 를 송신 스레드에서 부른다. 배치는 시작할 때의 연결에만 보낸다 | outbox 가 정본 |
| event_state_snapshot | 연결마다 한 번, 그 연결에서 outbox 재전송을 마친 직후(같은 송신 스레드, 같은 연결) | 연결 고정 직접 송신 |
| config_ack | ConfigSync.take_acks 를 tick 마다 꺼내. 대기열이 차면 붙들고 다음 tick 에 다시 | 신뢰 대기열(버리지 않음) |
| calibration_update·event_frame_ready | 만드는 쪽이 send_event 로 넣을 때. 차면 QueueFull 을 돌려준다 | 신뢰 대기열 |
| observation_summary | send_event | 텔레메트리 대기열(차면 오래된 것 버림) |
| tier0_endpoint | (재)연결마다 EndpointRegistrar.build 로 새로 만들어 | 신뢰 대기열(이전 것 교체) |

- payload 내용은 만드는 모듈이 정한다(world_state_payload, ConfigSync, EndpointRegistrar, outbox, 스냅샷 함수). 여기서는
  봉투를 씌우고 순서·주기만 정한다. outbox 로직은 고치지 않고 전송 함수(OutboxI1Transport)·연결 열기 때 reset·pump 만 꽂는다.
- health payload 는 health_payload() 가 만든 기본 필드에 health_fields(ConfigSync.health_fields,
  SiteAlertResync.fields 등)를 합친다. health_payload 가 None 이면(아직 모름) 보내지 않고, 연결 첫 health 라면 만들 수
  있을 때까지 tick 마다 다시 시도한다(그동안 그 연결의 다른 송신은 열리지 않는다).
- 엔드포인트 등록은 HTTPS 임시안(endpoint_registration.HttpTransport)과 이 I1 경로 둘 다 쓸 수 있다. 명세는 I1 이다.
- world_state 송신 버림 누적(health 1.5.0 `ec2_world_state_drops`): I1Publisher.world_state_drops() 가 이 스케줄러를
  만든 뒤의 누적(I1Client.drop_counts, 재연결로 0 이 되지 않음)과 세기 시작한 시각을 돌려주고, with_world_state_drops·
  world_state_drop_fields 가 health payload 에 합친다. 합치는 배선은 Tier 0 조립(service.Tier0Service, S15P31A101-171)이 한다. 10 Hz 주기에 맞춰 건너뛴 것(world_coalesced, 내려 뽑기)은 송신 버림이 아니라 넣지 않는다.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.tier0.endpoint_registration import EndpointConfigError
from services.tier0.i1_client import Connector, Envelope, I1Client, QueueFull, SendFailed, Unserializable
from services.tier0.i1_dispatch import I1Dispatcher, wire_tier0_handlers
from simai_ai.eventlog import TransportError as OutboxTransportError

log = logging.getLogger("services.tier0.i1.publisher")

WORLD_STATE_INTERVAL_S = 0.1  # 10 Hz (명세 12.2)
HEALTH_INTERVAL_S = 1.0  # 1 Hz (명세 12.2)
EVENT_TYPES = frozenset({"observation_summary", "calibration_update", "event_frame_ready"})

WORLD_STATE_DROPS_FIELD = "ec2_world_state_drops"
"""health 1.5.0 선택 필드 이름 (contracts/messages/health.schema.json)"""

PayloadFn = Callable[[], Mapping[str, Any] | None]
FieldsFn = Callable[[], Mapping[str, Any]]
MessageSource = Callable[[], Iterable[Mapping[str, Any]]]


@dataclass(frozen=True)
class WorldStateDrops:
    """Tier 0 → EC2 world_state 송신 버림 누적 (health 1.5.0 `ec2_world_state_drops`).

    superseded: 연결된 동안 보내기 전에 더 새 world_state 에 밀려 버린 수. disconnected: EC2 연결이 없어 버린 수(끊긴
    동안 만든 것, 끊길 때 칸에 남은 것, 보내다 끊긴 것). since_ms: 세기 시작한 시각(UTC ms). 재연결로 0 이 되지 않는다.
    """

    superseded: int
    disconnected: int
    since_ms: int

    def __post_init__(self) -> None:
        for name in ("superseded", "disconnected", "since_ms"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 는 0 이상의 정수다: {value!r}")

    def to_payload(self) -> dict[str, int]:
        return {"superseded": self.superseded, "disconnected": self.disconnected, "since_ms": self.since_ms}


def world_state_drop_fields(drops: WorldStateDrops | None) -> dict[str, Any]:
    """health_fields(FieldsFn)로 쓸 수 있는 꼴. 모르면(None) 빈 dict — 필드를 싣지 않는다(0 으로 지어내지 않음)."""
    if drops is None:
        return {}
    return {WORLD_STATE_DROPS_FIELD: drops.to_payload()}


def with_world_state_drops(payload: Mapping[str, Any] | None, drops: WorldStateDrops | None) -> dict[str, Any] | None:
    """health payload 에 world_state 버림 누적을 합친 새 dict. 원본은 바꾸지 않는다.

    payload 가 None(health 를 아직 모름)이면 None 그대로 — 버림 수만으로 health 를 만들지 않는다. drops 가 None 이면 필드
    없이 돌려준다. payload 에 이미 같은 필드가 있으면 출처가 둘이라 ValueError(어느 값이 맞는지 고르지 않는다).
    """
    if payload is None:
        return None
    if WORLD_STATE_DROPS_FIELD in payload:
        raise ValueError(f"health payload 에 이미 {WORLD_STATE_DROPS_FIELD} 가 있다")
    merged = dict(payload)
    merged.update(world_state_drop_fields(drops))
    return merged


class I1Publisher:
    """world_state 10 Hz·health 1 Hz 송신 스케줄러. start() 뒤 tick 스레드가 돈다(시험은 tick() 을 직접 불러도 된다)."""

    def __init__(
        self,
        client: I1Client,
        envelope: Envelope,
        *,
        health_payload: PayloadFn,
        health_fields: Sequence[FieldsFn] = (),
        world_interval_s: float = WORLD_STATE_INTERVAL_S,
        health_interval_s: float = HEALTH_INTERVAL_S,
        tick_s: float = 0.01,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if world_interval_s <= 0 or health_interval_s <= 0 or tick_s <= 0:
            raise ValueError("간격은 양수다")
        self._client = client
        self._envelope = envelope
        self._health_payload = health_payload
        self._health_fields = list(health_fields)
        self._world_interval = world_interval_s
        self._health_interval = health_interval_s
        self._tick_s = tick_s
        self._mono = monotonic
        self._lock = threading.Lock()
        self._world: tuple[Mapping[str, Any], str | None] | None = None  # 아직 보내지 않은 최신 (payload, msg_id)
        self._next_world = 0.0
        self._next_health: float | None = None  # None: 연결 전
        self._need_first_health = False  # 이 연결의 첫 health 를 아직 못 만들었다
        self._sources: list[MessageSource] = []
        self._carry: deque[dict[str, Any]] = deque()  # 신뢰 대기열이 차서 붙든 메시지(버리지 않음)
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self.world_offered = 0
        self.world_coalesced = 0  # 10 Hz 주기 안에 더 새 payload 가 와서 넘긴 수(내려 뽑기. 송신 지연 버림과 다르다)
        # world_state 송신 버림 누적의 기준: 이 스케줄러를 만든 시각과 그때의 클라이언트 누적(health 1.5.0)
        self._drops_since_ms = int(envelope.clock.now_ms())
        self._drops_base = client.drop_counts("world_state")

    @property
    def source_id(self) -> str:
        return self._envelope.source_id

    @property
    def envelope(self) -> Envelope:
        return self._envelope

    def world_state_drops(self) -> WorldStateDrops:
        """이 스케줄러를 만든 뒤 Tier 0 → EC2 world_state 송신 버림 누적(health 1.5.0 `ec2_world_state_drops`).

        재연결로 0 이 되지 않는다. 내려 뽑기(world_coalesced)는 넣지 않는다. 어느 스레드에서 불러도 된다.
        """
        superseded, disconnected = self._client.drop_counts("world_state")
        return WorldStateDrops(
            superseded=superseded - self._drops_base[0],
            disconnected=disconnected - self._drops_base[1],
            since_ms=self._drops_since_ms,
        )

    # --- 넣기 -----------------------------------------------------------------------

    def offer_world_state(self, payload: Mapping[str, Any], *, msg_id: str | None = None) -> None:
        """최신 world_state payload(tracking.world_state.world_state_payload)를 넣는다. 넣은 뒤 바꾸지 않는다.

        msg_id 를 주면 그 봉투 msg_id 로 보낸다. 판정 루프는 같은 값을 그 tick 판정 전이의 evidence_ref 로 쓴다(-79·-171).
        """
        with self._lock:
            if self._world is not None:
                self.world_coalesced += 1
            self._world = (payload, msg_id)
            self.world_offered += 1

    def send_event(self, msg_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """이벤트성 메시지를 봉투에 넣어 대기열로. 만든 메시지를 돌려준다.

        신뢰 대기열이 차면 QueueFull(메시지는 들어가지 않았다. 호출자가 붙들고 다시 보낸다).
        event_state_snapshot 은 받지 않는다(outbox 재전송 뒤 연결 고정으로 보낸다, attach_outbox).
        """
        if msg_type not in EVENT_TYPES:
            raise ValueError(f"send_event 로 보내는 종류가 아니다: {msg_type}")
        message = self._envelope.make(msg_type, payload)
        self._client.enqueue(message)
        return message

    def add_source(self, source: MessageSource) -> None:
        """tick 마다 불러 나온 봉투 메시지를 대기열에 넣는다(ConfigSync.take_acks 등). 차면 붙들고 다음 tick 에 다시."""
        self._sources.append(source)

    # --- 연결 훅 ---------------------------------------------------------------------

    def on_connected(self) -> None:
        """(재)연결 직후 health 를 즉시 1회 넣는다. 못 만들면 만들 수 있을 때까지 tick 마다 다시 시도한다."""
        now = self._mono()
        self._next_health = now + self._health_interval
        self._need_first_health = not self._emit_health()

    def on_disconnected(self) -> None:
        self._next_health = None
        self._need_first_health = False

    # --- tick -----------------------------------------------------------------------

    def tick(self) -> None:
        now = self._mono()
        self._drain_sources()
        if now >= self._next_world:
            # 밀린 주기를 몰아 보내지 않는다(다음 주기는 지금부터)
            self._next_world = max(self._next_world + self._world_interval, now)
            with self._lock:
                offered, self._world = self._world, None
            if offered is not None:
                payload, msg_id = offered
                try:
                    self._client.put_latest(self._envelope.make("world_state", payload, msg_id=msg_id))
                except Exception:  # noqa: BLE001
                    log.exception("world_state 봉투 만들기 실패")
        if not self._client.connected or self._next_health is None:
            return
        if self._need_first_health:
            if self._emit_health():
                self._need_first_health = False
                self._next_health = now + self._health_interval
        elif now >= self._next_health:
            self._next_health = max(self._next_health + self._health_interval, now)
            self._emit_health()

    def _drain_sources(self) -> None:
        for source in self._sources:
            try:
                self._carry.extend(dict(m) for m in source())
            except Exception:  # noqa: BLE001 - 한 출처 실패가 다른 송신을 멈추지 않는다
                log.exception("I1 메시지 출처 실패")
        while self._carry:
            try:
                self._client.enqueue(self._carry[0])
            except QueueFull:
                return  # 붙들고 다음 tick 에 다시(ConfigSync 는 꺼낸 ACK 를 다시 만들지 않는다)
            except Exception:  # noqa: BLE001
                log.exception("I1 메시지 넣기 실패: %s", self._carry[0].get("msg_type"))
            self._carry.popleft()

    def _emit_health(self) -> bool:
        try:
            base = self._health_payload()
            if base is None:
                return False
            payload = dict(base)
            for fields in self._health_fields:
                payload.update(fields())
            self._client.put_latest(self._envelope.make("health", payload))
            return True
        except Exception:  # noqa: BLE001 - health 실패가 연결을 멈추지 않는다
            log.exception("health 만들기 실패")
            return False

    # --- 시작·정지 -------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("이미 시작했다")
        self._closing.clear()
        self._thread = threading.Thread(target=self._run, name="i1-publisher", daemon=True)
        self._thread.start()

    def close(self, timeout_s: float = 5.0) -> bool:
        self._closing.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout_s)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def _run(self) -> None:
        while not self._closing.is_set():
            self.tick()
            self._closing.wait(self._tick_s)


# ---------------------------------------------------------------------------- 어댑터


class OutboxI1Transport:
    """eventlog.Outbox 의 전송 함수. 배치를 시작할 때 송신이 열린 연결을 잡고, 그 연결에만 보낸다.

    연결이 없거나 배치 도중에 바뀌면(이전 연결의 배치가 새 연결로 이어지지 않게) 남은 메시지를 보내지 않고 outbox 의
    TransportError 를 낸다. outbox 는 커서를 옮기지 않고, 새 연결에서는 연결 열기 때 reset 으로 acked_offset 이후부터
    오름차순으로 다시 보낸다(resent=true).
    """

    def __init__(self, client: I1Client) -> None:
        self._client = client

    def __call__(self, messages: Sequence[Mapping[str, Any]]) -> None:
        try:
            link = self._client.current_link()
            for message in messages:
                self._client.send_on(link, message)
        except (SendFailed, Unserializable) as e:
            raise OutboxTransportError(f"I1 전송 실패: {type(e).__name__}") from None


def attach_outbox(
    client: I1Client,
    outbox: Any,
    *,
    envelope: Envelope | None = None,
    snapshot_payload: PayloadFn | None = None,
    max_batches: int = 1,
) -> None:
    """outbox 를 I1 클라이언트에 꽂는다(outbox 는 OutboxI1Transport 로 만든다).

    - 연결 열기(송신 스레드, 그 연결로 무엇이든 보내기 전): outbox.reset() — acked_offset 이후부터 다시 보낸다.
    - pump(송신 스레드): outbox.pump(max_batches). snapshot_payload 를 주면, 그 연결에서 outbox 를 끝까지 보냈고(drained)
      막힌 기록이 없을 때 event_state_snapshot 을 연결마다 한 번 같은 연결로 보낸다(재전송 → 스냅샷 순서, 3.2 재동기화).
      snapshot_payload 가 None 을 돌려주면(아직 모름) 다음 pump 에 다시 묻는다.
    """
    if snapshot_payload is not None and envelope is None:
        raise ValueError("snapshot_payload 에는 envelope 가 필요하다")
    sent_for: dict[str, int | None] = {"generation": None}

    def pump() -> None:
        outbox.pump(max_batches)
        if snapshot_payload is None or not outbox.drained or outbox.blocked is not None:
            return
        try:
            link = client.current_link()
        except SendFailed:
            return
        if sent_for["generation"] == link.generation:
            return
        payload = snapshot_payload()
        if payload is None:
            return
        assert envelope is not None
        try:
            client.send_on(link, envelope.make("event_state_snapshot", payload))
        except (SendFailed, Unserializable):
            return  # 다음 연결에서 다시 만든다
        sent_for["generation"] = link.generation

    client.add_on_writer_open(outbox.reset)
    client.add_pump(pump)


class EndpointI1Registration:
    """tier0_endpoint 를 I1 메시지로 등록한다. (재)연결마다 새로 만들어(registered_at 갱신) 신뢰 대기열에 넣는다.

    build: 봉투까지 만든 tier0_endpoint (예: lambda: registrar.build(env, read_cert_pem(env))). 값을 모르면
    EndpointConfigError 를 내고, 그때는 보내지 않는다(지어내지 않음). source_id 는 I1 봉투의 source_id 와 같아야 한다.
    """

    REPLACE_KEY = "tier0_endpoint"

    def __init__(self, client: I1Client, build: Callable[[], Mapping[str, Any]], *, source_id: str) -> None:
        self._client = client
        self._build = build
        self._source_id = source_id
        self.failures = 0
        self.last_message: Mapping[str, Any] | None = None

    def register(self) -> Mapping[str, Any] | None:
        try:
            message = dict(self._build())
        except EndpointConfigError as e:
            self.failures += 1
            log.error("엔드포인트 I1 등록 안 함: %s", e)
            return None
        if message.get("msg_type") != "tier0_endpoint" or message.get("source_id") != self._source_id:
            self.failures += 1
            log.error("엔드포인트 I1 등록 안 함: tier0_endpoint 가 아니거나 source_id 가 I1 봉투와 다르다")
            return None
        try:
            self._client.enqueue(message, replace_key=self.REPLACE_KEY)
        except QueueFull:
            self.failures += 1
            log.error("엔드포인트 I1 등록 안 함: 신뢰 대기열이 찼다(다음 연결에서 다시)")
            return None
        self.last_message = message
        return message


# ---------------------------------------------------------------------------- 조립


@dataclass
class I1Link:
    client: I1Client
    publisher: I1Publisher
    dispatcher: I1Dispatcher
    endpoint: EndpointI1Registration | None = None
    outbox: Any | None = None

    def start(self) -> None:
        self.publisher.start()
        self.client.start()

    def close(self, timeout_s: float = 5.0) -> bool:
        """모두 닫는다. 끝나지 않은 스레드가 있으면 False(alive_threads 로 본다)."""
        results = [self.client.close(timeout_s), self.dispatcher.close(timeout_s), self.publisher.close(timeout_s)]
        return all(results)

    def alive_threads(self) -> list[str]:
        return self.client.alive_threads() + self.dispatcher.alive_threads()


def build_i1_link(
    connector: Connector,
    envelope: Envelope,
    *,
    health_payload: PayloadFn,
    health_fields: Sequence[FieldsFn] = (),
    make_outbox: Callable[[OutboxI1Transport], Any] | None = None,
    snapshot_payload: PayloadFn | None = None,
    config_sync: Any | None = None,
    on_event_ack: Callable[[Any], Any] | None = None,
    ack_seen: Callable[[str], bool] | None = None,
    on_event_close: Callable[[Any], Mapping[str, Any]] | None = None,
    on_event_escalate: Callable[[Any], Mapping[str, Any]] | None = None,
    endpoint_build: Callable[[], Mapping[str, Any]] | None = None,
    client_kwargs: Mapping[str, Any] | None = None,
    publisher_kwargs: Mapping[str, Any] | None = None,
) -> I1Link:
    """I1 클라이언트·스케줄러·디스패처를 묶고 연결 훅 순서를 맞춘다. 주지 않은 모듈은 꽂지 않는다.

    make_outbox: 전송 함수(OutboxI1Transport)를 받아 eventlog.Outbox 를 만드는 함수
    (예: lambda transport: Outbox(journal, store, gateway_id=..., transport=transport, clock=clock)).
    on_event_ack 를 주면 ack_seen(예: EventJournal.has_ack_id)도 필수다(연결을 넘는 확인 멱등).
    on_event_close: CloseRequest → event_close_result payload(JudgmentLoop.close). 결과는 신뢰 대기열로 EC2 에 보낸다
    (끊겨도 보관). 종료 전이 자체는 저널 outbox 로 간다(S15P31A101-200).
    on_event_escalate: EscalationRequest → event_escalate_result payload(JudgmentLoop.escalate). on_event_close 와 같다(S15P31A101-272).
    config_sync 의 연결 표시는 연결 훅으로 바로 바꾸고, 처리 중 연결이 바뀐 구성 묶음 뒤에는 디스패처가 다시 맞춘다.
    snapshot_payload: 재전송을 마친 뒤 보낼 event_state_snapshot payload(활성 이벤트 최종 상태, 추적기를 가진 조립이 만든다).
    연결 훅(연결 스레드): 디스패처 중복 기억 비움 → Config Sync 연결 표시 → health 즉시 → 엔드포인트 재등록.
    연결 열기(송신 스레드): outbox.reset. 첫 메시지 관문은 health.
    """
    dispatcher = I1Dispatcher()
    kwargs = {"first_message_type": "health", **dict(client_kwargs or {})}
    client = I1Client(connector, on_message=dispatcher.handle_raw, **kwargs)
    publisher = I1Publisher(client, envelope, health_payload=health_payload, health_fields=health_fields,
                            **dict(publisher_kwargs or {}))
    client.add_on_connected(dispatcher.on_connected)
    client.add_on_disconnected(dispatcher.on_disconnected)
    if config_sync is not None:
        client.add_on_connected(config_sync.on_connected)
        client.add_on_disconnected(config_sync.on_disconnected)
        publisher.add_source(config_sync.take_acks)
    outbox = None
    if make_outbox is not None:
        outbox = make_outbox(OutboxI1Transport(client))
        attach_outbox(client, outbox, envelope=envelope, snapshot_payload=snapshot_payload)
    client.add_on_connected(publisher.on_connected)
    client.add_on_disconnected(publisher.on_disconnected)
    endpoint = None
    if endpoint_build is not None:
        endpoint = EndpointI1Registration(client, endpoint_build, source_id=envelope.source_id)
        client.add_on_connected(endpoint.register)
    close_and_reply = None
    if on_event_close is not None:

        def close_and_reply(request: Any) -> Mapping[str, Any]:
            result = on_event_close(request)
            client.enqueue(envelope.make("event_close_result", result))
            return result

    escalate_and_reply = None
    if on_event_escalate is not None:

        def escalate_and_reply(request: Any) -> Mapping[str, Any]:
            result = on_event_escalate(request)
            client.enqueue(envelope.make("event_escalate_result", result))
            return result

    wire_tier0_handlers(dispatcher, outbox=outbox, config_sync=config_sync, on_event_ack=on_event_ack,
                        ack_seen=ack_seen, on_event_close=close_and_reply, on_event_escalate=escalate_and_reply)
    return I1Link(client, publisher, dispatcher, endpoint, outbox)
