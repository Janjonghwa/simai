"""프레임 최신성 정책: 카메라별 latest-frame-wins 추론 대기열 (명세 5.1, 15.1 G0).

- 카메라마다 디코딩된 프레임을 최대 queue_depth(1~2)장만 둔다. 새 프레임이 오면 가장 오래된 것을 밀어내고
  SKIPPED_BY_POLICY(SUPERSEDED)로 기록한다.
- 추론에 넘기는 순간에 대기 시간(queue_wait_ms)을 재고, 상한(max_queue_wait_ms, 초기값 150ms)을 넘긴 프레임은 추론하지
  않고 SKIPPED_BY_POLICY(QUEUE_WAIT_EXCEEDED)로 버린다. 그래서 상한을 넘는 추론은 구조상 0건이다(G0 기준).
  현재 시각은 대기열 잠금 안에서 읽으므로, 시각을 읽은 뒤 들어온 프레임을 그 시각으로 판정하는 일이 없다.
  넘기기 직전에 시각을 한 번 더 읽어 상한을 다시 따지고, 그 뒤 돌려줄 때까지 외부 sink 호출 같은 지연 작업을 하지 않는다.
- frame_age_ms = 추론 시작 시각 − 촬영 시각(업로드·디코딩 포함), queue_wait_ms = 추론 시작 시각 − 대기열에 들어간 시각.
- 전송 중 사라진 프레임(frame_sequence 빈칸)은 NETWORK_LOSS 이고 G0 손실률에는 이것만 넣는다(ingest.py 가 센다).

현재 시각은 주입받은 Clock 으로만 읽는다(ai/CONVENTION.md). 시각은 UTC int64 ms 다.
"""

from __future__ import annotations

import threading
from collections import Counter, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum

from simai_ai.io.decoder import DecodedPicture
from simai_ai.providers.base import Frame
from simai_ai.risk.clock import Clock
from simai_ai.tracking.time_model import advance_ms, require_ms


class FrameDisposition(StrEnum):
    """프레임 한 장의 처리 결과. G0 에 따로 보고한다."""

    INFERRED = "INFERRED"
    SKIPPED_BY_POLICY = "SKIPPED_BY_POLICY"
    NETWORK_LOSS = "NETWORK_LOSS"
    DECODE_FAILED = "DECODE_FAILED"


class SkipReason(StrEnum):
    """SKIPPED_BY_POLICY 의 세부 원인. 계약 필드가 아니라 기록·진단용 내부 값이다."""

    SUPERSEDED = "SUPERSEDED"  # 같은 카메라의 더 새 프레임에 밀림 (latest-frame-wins)
    QUEUE_WAIT_EXCEEDED = "QUEUE_WAIT_EXCEEDED"  # 대기 상한 초과
    OUT_OF_ORDER = "OUT_OF_ORDER"  # 이미 넘긴 프레임보다 촬영 시각이 이르다
    LATE_ARRIVAL = "LATE_ARRIVAL"  # 같은 epoch 에서 이미 지나간 frame_sequence 가 늦게 옴
    STALE_EPOCH = "STALE_EPOCH"  # 끝난 stream_epoch 의 프레임
    NO_CODEC_INFO = "NO_CODEC_INFO"  # 이 epoch 의 코덱 정보가 아직 없음
    WAITING_FOR_KEYFRAME = "WAITING_FOR_KEYFRAME"  # 재접속·손실·디코딩 실패 뒤 키프레임 대기
    STREAM_RESET = "STREAM_RESET"  # 재접속·코덱 변경으로 정렬 대기·디코더·추론 대기열에 남은 프레임을 버림
    DECODER_DROPPED = "DECODER_DROPPED"  # 디코더가 그림을 내지 않은 채 오래 남은 입력


@dataclass(frozen=True)
class FrameRecord:
    """프레임 처리 기록 한 건. NETWORK_LOSS 는 도착하지 않았으므로 촬영 시각을 모른다(None)."""

    camera_id: str
    stream_epoch: int | None
    frame_sequence: int | None
    disposition: FrameDisposition
    at_ms: int
    reason: SkipReason | None = None
    estimated_capture_time: int | None = None
    frame_age_ms: int | None = None
    queue_wait_ms: int | None = None


RecordSink = Callable[[FrameRecord], None]


@dataclass(frozen=True)
class FreshnessConfig:
    """최신성 정책 설정. 초기값은 명세 5.1·15.1 G0.

    queue_depth: 카메라별 추론 대기열 길이(1~2). 1 이면 순수 latest-frame-wins.
    max_queue_wait_ms: 이 값을 넘게 기다린 프레임은 추론하지 않는다(같으면 추론한다).
    sample_window: frame_age_ms·queue_wait_ms 표본을 카메라별로 최근 몇 개까지 둘지(분위수 계산용).
    max_missing_tracked: 늦게 도착하면 손실에서 빼려고 기억해 둘 빈 frame_sequence 수.
    max_decoder_pending: 디코더 안에 그림 없이 남아 있을 수 있는 입력 수. 넘으면 오래된 것부터 버린다.
    keyframe_after_loss: frame_sequence 빈칸 뒤 다음 키프레임까지 디코딩하지 않는다(참조가 끊긴 깨진 그림을 관측으로
        쓰지 않음). 대신 손실 한 번에 최대 키프레임 간격(C110 2초)만큼 그 카메라 관측이 없다. G0 실측 후 다시 정한다.
    reorder_window_ms: 역순·지연 도착을 정렬해 쓰는 창(명세 12.7, 예 200ms). frame_sequence 빈칸 뒤에 온 프레임을
        도착 후 이 시간까지 붙잡아 두고 빈 번호를 기다린다. 넘기면 빈 번호를 NETWORK_LOSS 로 확정한다. 0 이면 바로 확정한다.
    max_reorder_frames: 정렬을 위해 붙잡아 둘 수 있는 프레임 수. 넘으면 창을 기다리지 않고 손실을 확정한다.
    """

    queue_depth: int = 1
    max_queue_wait_ms: int = 150
    sample_window: int = 10_000
    max_missing_tracked: int = 4096
    max_decoder_pending: int = 32
    keyframe_after_loss: bool = True
    reorder_window_ms: int = 200
    max_reorder_frames: int = 16

    def __post_init__(self) -> None:
        if self.queue_depth not in (1, 2):
            raise ValueError("queue_depth 는 1 또는 2 여야 한다 (명세 5.1)")
        if self.max_queue_wait_ms < 0 or self.reorder_window_ms < 0:
            raise ValueError("max_queue_wait_ms, reorder_window_ms 는 0 이상이어야 한다")
        if min(self.sample_window, self.max_missing_tracked, self.max_decoder_pending, self.max_reorder_frames) < 1:
            raise ValueError(
                "sample_window, max_missing_tracked, max_decoder_pending, max_reorder_frames 는 1 이상이어야 한다"
            )


def percentile(samples: Sequence[int], q: float) -> int | None:
    """최근접 순위 분위수(q 는 0~100). 표본이 없으면 None."""
    if not samples:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q 는 0~100 이어야 한다")
    ordered = sorted(samples)
    rank = max(1, -(-len(ordered) * q // 100))  # ceil(n * q / 100), 최소 1
    return ordered[int(rank) - 1]


@dataclass
class CameraFreshnessStats:
    """카메라 한 대의 G0 최신성 지표.

    loss_rate 는 NETWORK_LOSS / (같은 epoch 안에서 순서대로 도착한 프레임 + NETWORK_LOSS) 이다. SKIPPED_BY_POLICY 는 넣지 않는다.
    max_result_interval_ms 는 연속해 추론한 두 프레임의 촬영 시각 차이 중 최댓값이다(재접속 구간 포함).
    """

    camera_id: str
    received: int = 0
    sequenced: int = 0
    decoded: int = 0
    inferred: int = 0
    network_loss: int = 0
    decode_failed: int = 0
    skipped_by_policy: Counter[SkipReason] = field(default_factory=Counter)
    max_result_interval_ms: int | None = None
    last_inferred_capture: int | None = None
    frame_age_ms: deque[int] = field(default_factory=deque)
    queue_wait_ms: deque[int] = field(default_factory=deque)

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped_by_policy.values())

    @property
    def loss_rate(self) -> float | None:
        expected = self.sequenced + self.network_loss
        return self.network_loss / expected if expected else None

    def summary(self) -> dict[str, object]:
        """health·G0 보고용 요약. 분위수는 최근 sample_window 개 표본 기준이다."""
        return {
            "camera_id": self.camera_id,
            "received": self.received,
            "decoded": self.decoded,
            "inferred": self.inferred,
            "network_loss": self.network_loss,
            "loss_rate": self.loss_rate,
            "decode_failed": self.decode_failed,
            "skipped_by_policy": self.skipped_total,
            "skipped_by_reason": {str(k): v for k, v in sorted(self.skipped_by_policy.items())},
            "max_result_interval_ms": self.max_result_interval_ms,
            "frame_age_ms_p95": percentile(self.frame_age_ms, 95),
            "frame_age_ms_p99": percentile(self.frame_age_ms, 99),
            "queue_wait_ms_max": max(self.queue_wait_ms, default=None),
        }


class FrameLog:
    """처리 기록을 지표에 반영하고 외부 sink(파일·system_metric 등)로 넘긴다. 스레드 안전.

    지표는 record 때 바로 반영한다. sink 는 기록 순서대로 부르되, defer=True 로 받은 기록은 다음 deliver 때 부른다.
    sink 는 얼마나 걸릴지 모르므로, 추론 대기열은 상한 판정과 인계 사이에 sink 를 부르지 않으려고 기록을 미룬다.
    """

    def __init__(self, camera_ids: Sequence[str], config: FreshnessConfig, sink: RecordSink | None = None) -> None:
        self._config = config
        self._sink = sink
        self._lock = threading.Lock()
        self._deliver_lock = threading.Lock()  # sink 를 기록 순서대로, 한 번에 하나씩 부른다
        self._outbox: deque[FrameRecord] = deque()
        self.stats = {c: CameraFreshnessStats(c) for c in camera_ids}

    def _sample(self, samples: deque[int], value: int) -> None:
        samples.append(value)
        if len(samples) > self._config.sample_window:
            samples.popleft()

    def record(self, rec: FrameRecord, *, defer: bool = False) -> None:
        with self._lock:
            s = self.stats[rec.camera_id]
            if rec.disposition is FrameDisposition.INFERRED:
                s.inferred += 1
                assert rec.estimated_capture_time is not None
                assert rec.queue_wait_ms is not None
                if s.last_inferred_capture is not None:
                    interval = rec.estimated_capture_time - s.last_inferred_capture
                    s.max_result_interval_ms = max(interval, s.max_result_interval_ms or 0)
                s.last_inferred_capture = rec.estimated_capture_time
                if rec.frame_age_ms is not None:
                    self._sample(s.frame_age_ms, rec.frame_age_ms)
                self._sample(s.queue_wait_ms, rec.queue_wait_ms)
            elif rec.disposition is FrameDisposition.SKIPPED_BY_POLICY:
                assert rec.reason is not None
                s.skipped_by_policy[rec.reason] += 1
            elif rec.disposition is FrameDisposition.NETWORK_LOSS:
                s.network_loss += 1
            else:
                s.decode_failed += 1
            if self._sink is not None:
                self._outbox.append(rec)
        if not defer:
            self.deliver()

    def deliver(self) -> None:
        """미뤄 둔 기록을 순서대로 sink 에 넘긴다. 대기열 잠금 밖에서 부른다."""
        if self._sink is None:
            return
        with self._deliver_lock:
            while True:
                with self._lock:
                    if not self._outbox:
                        return
                    rec = self._outbox.popleft()
                self._sink(rec)

    def count(self, camera_id: str, *, received: int = 0, sequenced: int = 0, decoded: int = 0) -> None:
        with self._lock:
            s = self.stats[camera_id]
            s.received += received
            s.sequenced += sequenced
            s.decoded += decoded

    def counts(self, camera_id: str) -> dict[str, object]:
        """누적 개수와 최근 표본의 frame_age p95·queue_wait 최댓값을 잠금 안에서 복사한다(health 집계·표시 파일용.
        다른 스레드가 기록하는 중에 읽어도 된다)."""
        with self._lock:
            s = self.stats[camera_id]
            return {
                "received": s.received,
                "sequenced": s.sequenced,
                "decoded": s.decoded,
                "inferred": s.inferred,
                "network_loss": s.network_loss,
                "decode_failed": s.decode_failed,
                "skipped_by_reason": {str(k): v for k, v in s.skipped_by_policy.items()},
                "frame_age_ms_p95": percentile(s.frame_age_ms, 95),
                "queue_wait_ms_max": max(s.queue_wait_ms, default=None),
            }

    def recover_loss(self, camera_id: str) -> None:
        """손실로 센 프레임이 늦게 도착했다. 손실에서 빼고(LATE_ARRIVAL 로 따로 기록됨) 순서 도착으로 센다."""
        with self._lock:
            s = self.stats[camera_id]
            s.network_loss -= 1
            s.sequenced += 1


@dataclass(frozen=True)
class InferenceTicket:
    """추론에 넘기는 프레임 한 장과 최신성 측정값."""

    frame: Frame
    dispatched_at_ms: int
    frame_age_ms: int | None
    queue_wait_ms: int


@dataclass
class _Pending:
    frame: Frame
    enqueued_at_ms: int


def _frame_ids(frame: Frame) -> tuple[int | None, int | None]:
    ref = frame.frame_ref
    return ref.get("stream_epoch"), ref.get("frame_sequence", ref.get("frame_index"))


def _materialize(ticket: InferenceTicket) -> InferenceTicket:
    """추론에 넘기기로 확정된 티켓의 frame.image 가 DecodedPicture 면 실제 그림(BGR ndarray 등)으로 풀어 넣는다(-386)."""
    picture = ticket.frame.image
    if isinstance(picture, DecodedPicture):
        return replace(ticket, frame=replace(ticket.frame, image=picture.image))
    return ticket


class LatestFrameScheduler:
    """카메라별 latest-frame-wins 추론 대기열. offer 는 디코더 쪽, take·take_one 은 추론 루프 쪽에서 부른다. 스레드 안전.

    카메라별로 넘기는 프레임의 촬영 시각은 줄어들지 않는다(FrameSource 계약). 더 이른 프레임은 OUT_OF_ORDER 로 버린다.
    현재 시각은 잠금 안에서만 읽고 앞으로만 옮긴다(시스템 시계가 뒤로 조정돼도 대기 시간이 음수가 되지 않는다).
    처리 기록은 잠금 안에서 지표에만 반영하고 sink 는 잠금을 놓은 뒤 부른다. 인계(INFERRED) 기록의 sink 는 돌려준
    뒤 다음 호출(또는 FrameLog.deliver) 때 부른다.
    """

    def __init__(self, camera_ids: Sequence[str], clock: Clock, config: FreshnessConfig, log: FrameLog) -> None:
        if len(set(camera_ids)) != len(camera_ids) or not camera_ids:
            raise ValueError("camera_ids 는 비어 있지 않고 중복이 없어야 한다")
        self.camera_ids = tuple(camera_ids)
        self._clock = clock
        self._config = config
        self._log = log
        self._lock = threading.Lock()
        self._queues: dict[str, deque[_Pending]] = {c: deque() for c in self.camera_ids}
        # 카메라별로 대기열에 넣은 최신 촬영 시각 (advance_ms 로만 앞으로 간다)
        self._last_offered: dict[str, int | None] = dict.fromkeys(self.camera_ids)
        self._last_now: int | None = None
        self._next_camera = 0  # take_one 이 먼저 볼 카메라 위치(돌아가며 넘긴다)

    def _now_locked(self) -> int:
        """대기열 잠금 안에서 현재 시각을 읽는다. 대기열 상태와 같은 순간의 시각이다."""
        self._last_now = advance_ms(self._last_now, require_ms("clock.now_ms()", self._clock.now_ms()))
        return self._last_now

    def _skip(self, pending: _Pending, reason: SkipReason, now: int) -> None:
        epoch, seq = _frame_ids(pending.frame)
        self._log.record(
            FrameRecord(
                camera_id=pending.frame.camera_id,
                stream_epoch=epoch,
                frame_sequence=seq,
                disposition=FrameDisposition.SKIPPED_BY_POLICY,
                at_ms=now,
                reason=reason,
                estimated_capture_time=pending.frame.estimated_capture_time,
            ),
            defer=True,
        )

    def offer(self, frame: Frame) -> bool:
        """디코딩된 프레임을 대기열에 넣는다. 받아들이면 True, 순서가 어긋나 버리면 False."""
        try:
            return self._offer(frame)
        finally:
            self._log.deliver()

    def _offer(self, frame: Frame) -> bool:
        with self._lock:
            now = self._now_locked()
            pending = _Pending(frame, now)
            cam = frame.camera_id
            if cam not in self._queues:
                raise ValueError(f"모르는 camera_id: {cam!r}")
            last = self._last_offered[cam]
            if last is not None and frame.estimated_capture_time < last:
                self._skip(pending, SkipReason.OUT_OF_ORDER, now)
                return False
            self._last_offered[cam] = advance_ms(last, frame.estimated_capture_time)
            queue = self._queues[cam]
            queue.append(pending)
            while len(queue) > self._config.queue_depth:
                self._skip(queue.popleft(), SkipReason.SUPERSEDED, now)
            return True

    def _expire_locked(self, now: int) -> bool:
        """상한을 넘긴 프레임을 버린다. 하나라도 버렸으면 True."""
        expired = False
        for queue in self._queues.values():
            while queue and now - queue[0].enqueued_at_ms > self._config.max_queue_wait_ms:
                self._skip(queue.popleft(), SkipReason.QUEUE_WAIT_EXCEEDED, now)
                expired = True
        return expired

    def _dispatch_locked(self, cam: str, now: int) -> InferenceTicket:
        """대기열 맨 앞 프레임을 지금(now) 추론에 넘긴다. now 에 상한 안임을 같은 잠금 안에서 확인한 뒤에만 부른다."""
        queue = self._queues[cam]
        queue_wait = now - queue[0].enqueued_at_ms
        if not 0 <= queue_wait <= self._config.max_queue_wait_ms:  # 같은 잠금 안에서 확인했으므로 생기지 않는다
            raise AssertionError(f"queue_wait_ms {queue_wait} 가 상한 밖이다")
        pending = queue.popleft()
        ticket = InferenceTicket(
            frame=pending.frame,
            dispatched_at_ms=now,
            frame_age_ms=(now - pending.frame.estimated_capture_time
                          if now >= pending.frame.estimated_capture_time else None),
            queue_wait_ms=queue_wait,
        )
        epoch, seq = _frame_ids(pending.frame)
        self._log.record(
            FrameRecord(
                camera_id=cam,
                stream_epoch=epoch,
                frame_sequence=seq,
                disposition=FrameDisposition.INFERRED,
                at_ms=now,
                estimated_capture_time=pending.frame.estimated_capture_time,
                frame_age_ms=ticket.frame_age_ms,
                queue_wait_ms=queue_wait,
            ),
            defer=True,
        )
        return ticket

    def _handoff(self, pick: Callable[[], list[str]]) -> list[InferenceTicket]:
        """상한을 넘긴 프레임을 버리고, pick 이 고른 카메라의 맨 앞 프레임을 넘긴다.

        만료 기록의 sink 는 잠금 밖에서 부르고 시각을 다시 읽는다(sink 가 걸린 시간도 대기에 넣는다). 고른 뒤 시각을
        한 번 더 읽어 그사이(선점 등) 상한을 넘겼으면 넘기지 않고 처음부터 다시 한다. 넘기는 시각은 이 마지막 읽기이고,
        그 뒤 돌려줄 때까지 sink 를 부르지 않는다. 다시 할 때마다 프레임을 하나 이상 버리므로 반복은 끝난다.
        추론에 넘기기로 확정된 프레임만 대기열 잠금 밖에서 DecodedPicture.image 를 풀어 BGR 배열로 구체화한다(-386).
        """
        while True:
            self._log.deliver()
            with self._lock:
                if self._expire_locked(self._now_locked()):
                    continue
                cams = pick()
                if not cams:
                    return []
                now = self._now_locked()
                cap = self._config.max_queue_wait_ms
                if any(now - self._queues[cam][0].enqueued_at_ms > cap for cam in cams):
                    continue
                tickets = [self._dispatch_locked(cam, now) for cam in cams]
                break
        return [_materialize(t) for t in tickets]

    def _round_robin_locked(self) -> list[str]:
        n = len(self.camera_ids)
        for i in range(n):
            idx = (self._next_camera + i) % n
            if self._queues[self.camera_ids[idx]]:
                self._next_camera = (idx + 1) % n
                return [self.camera_ids[idx]]
        return []

    def take(self) -> list[InferenceTicket]:
        """지금 추론할 프레임을 카메라마다 최대 한 장씩, camera_ids 순서로 꺼낸다(묶음 추론용).

        돌려준 묶음은 지금 한꺼번에 추론에 넘긴 것으로 기록한다. 한 장씩 차례로 추론한다면 take_one 을 쓴다.
        대기 상한을 넘긴 프레임은 먼저 버린다. 돌려주는 프레임의 queue_wait_ms 는 늘 상한 이하다.
        """
        return self._handoff(lambda: [cam for cam in self.camera_ids if self._queues[cam]])

    def take_one(self) -> InferenceTicket | None:
        """지금 추론할 프레임 한 장. 카메라를 돌아가며 고르고, 넘기는 이 순간에 만료·교체를 다시 따진다.

        한 장씩 추론하는 동안 들어온 더 새 프레임이나 그동안 상한을 넘긴 프레임이 다음 호출에 그대로 반영된다.
        """
        tickets = self._handoff(self._round_robin_locked)
        return tickets[0] if tickets else None

    def expire(self) -> None:
        """추론 루프가 바빠 take 를 못 부르는 동안에도 상한을 넘긴 프레임을 버려 둔다(기록 시각을 정확히 하려고)."""
        with self._lock:
            self._expire_locked(self._now_locked())
        self._log.deliver()

    def discard(self, camera_id: str, reason: SkipReason) -> None:
        """한 카메라의 대기 프레임을 모두 버린다(재접속·코덱 변경으로 그 카메라가 관측 없음이 될 때)."""
        with self._lock:
            now = self._now_locked()
            queue = self._queues[camera_id]
            while queue:
                self._skip(queue.popleft(), reason, now)
        self._log.deliver()

    def pending(self, camera_id: str) -> int:
        with self._lock:
            return len(self._queues[camera_id])


class ResultGate:
    """추론 결과를 LIVE 상태에 반영할지 정한다 (명세 5.1: 오래된 프레임의 뒤늦은 결과로 LIVE 를 갱신하지 않는다).

    카메라별로 이미 반영한 최신 결과의 촬영 시각보다 이르거나 같으면 버린다. 스레드 안전.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._applied: dict[str, int] = {}

    def admit(self, camera_id: str, estimated_capture_time: int) -> bool:
        require_ms("estimated_capture_time", estimated_capture_time)
        with self._lock:
            last = self._applied.get(camera_id)
            if last is not None and estimated_capture_time <= last:
                return False
            self._applied[camera_id] = advance_ms(last, estimated_capture_time)
            return True

    def last_applied(self, camera_id: str) -> int | None:
        with self._lock:
            return self._applied.get(camera_id)
