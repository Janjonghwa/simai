"""카메라별 latest-frame-wins 대기열과 최신성 측정 (명세 5.1, 15.1 G0)."""

import threading

import pytest

from simai_ai.io import DecodedPicture, FrameDisposition, FreshnessConfig, LatestFrameScheduler, ResultGate, SkipReason
from simai_ai.io.freshness import FrameLog, percentile
from simai_ai.providers import Frame
from simai_ai.risk.clock import FixedClock

T0 = 1760342400000


def _frame(cam, seq, t=None):
    return Frame(
        camera_id=cam,
        estimated_capture_time=T0 + 40 * seq if t is None else t,
        timestamp_source="RECEIVE_ESTIMATED",
        frame_ref={"stream_epoch": 0, "frame_sequence": seq},
        image=object(),
    )


@pytest.fixture
def setup():
    def _make(cameras=("cam1", "cam2"), **cfg):
        clock = FixedClock(T0)
        config = FreshnessConfig(**cfg)
        records = []
        log = FrameLog(cameras, config, records.append)
        return LatestFrameScheduler(cameras, clock, config, log), clock, log, records

    return _make


def _seqs(tickets):
    return [(t.frame.camera_id, t.frame.frame_ref["frame_sequence"]) for t in tickets]


def test_latest_frame_wins(setup):
    sched, clock, log, records = setup()
    for seq in range(3):
        clock.set(T0 + 40 * seq + 30)
        sched.offer(_frame("cam1", seq))
    tickets = sched.take()
    assert _seqs(tickets) == [("cam1", 2)]
    skipped = [(r.frame_sequence, r.reason) for r in records if r.disposition is FrameDisposition.SKIPPED_BY_POLICY]
    assert skipped == [(0, SkipReason.SUPERSEDED), (1, SkipReason.SUPERSEDED)]
    assert log.stats["cam1"].network_loss == 0  # 정책상 건너뜀은 손실이 아니다


def test_frame_age_and_queue_wait(setup):
    sched, clock, _, _ = setup()
    clock.set(T0 + 70)  # 촬영 후 70ms 에 디코딩 끝
    sched.offer(_frame("cam1", 0))
    clock.set(T0 + 120)
    (ticket,) = sched.take()
    assert ticket.queue_wait_ms == 50
    assert ticket.frame_age_ms == 120  # 업로드·디코딩 포함 전체 경과
    assert ticket.dispatched_at_ms == T0 + 120


def test_future_capture_age_is_unknown_and_not_sampled(setup):
    sched, clock, log, records = setup()
    sched.offer(_frame("cam1", 0, T0 + 350))
    (ticket,) = sched.take()
    assert ticket.frame_age_ms is None
    log.deliver()
    assert records[-1].frame_age_ms is None
    assert not log.stats["cam1"].frame_age_ms
    assert log.stats["cam1"].summary()["frame_age_ms_p95"] is None


@pytest.mark.parametrize(("wait", "inferred"), [(150, True), (151, False)])
def test_queue_wait_cap_boundary(setup, wait, inferred):
    """상한을 '넘는' 프레임만 버린다. 150ms 는 추론, 151ms 는 버림."""
    sched, clock, log, records = setup()
    sched.offer(_frame("cam1", 0))
    clock.advance(wait)
    tickets = sched.take()
    assert bool(tickets) is inferred
    if not inferred:
        assert records[-1].reason is SkipReason.QUEUE_WAIT_EXCEEDED
        assert log.stats["cam1"].skipped_by_policy[SkipReason.QUEUE_WAIT_EXCEEDED] == 1


def test_expire_records_drop_time(setup):
    sched, clock, _, records = setup()
    sched.offer(_frame("cam1", 0))
    clock.advance(200)
    sched.expire()
    assert records[-1].at_ms == T0 + 200 and records[-1].reason is SkipReason.QUEUE_WAIT_EXCEEDED
    assert sched.pending("cam1") == 0


def test_one_per_camera_in_camera_order(setup):
    sched, _, _, _ = setup(cameras=("camB", "camA"))
    sched.offer(_frame("camA", 0))
    sched.offer(_frame("camB", 0))
    assert _seqs(sched.take()) == [("camB", 0), ("camA", 0)]
    assert sched.take() == []


def test_depth_two_keeps_two_newest(setup):
    sched, _, _, records = setup(queue_depth=2)
    for seq in range(3):
        sched.offer(_frame("cam1", seq))
    assert [r.frame_sequence for r in records] == [0]
    assert _seqs(sched.take()) == [("cam1", 1)]
    assert _seqs(sched.take()) == [("cam1", 2)]


def test_out_of_order_frame_dropped(setup):
    sched, _, _, records = setup()
    assert sched.offer(_frame("cam1", 5))
    assert not sched.offer(_frame("cam1", 4))
    assert records[-1].reason is SkipReason.OUT_OF_ORDER
    assert _seqs(sched.take()) == [("cam1", 5)]


def test_unknown_camera(setup):
    sched, _, _, _ = setup()
    with pytest.raises(ValueError):
        sched.offer(_frame("cam9", 0))


def test_config_validation():
    with pytest.raises(ValueError):
        FreshnessConfig(queue_depth=3)
    with pytest.raises(ValueError):
        FreshnessConfig(max_queue_wait_ms=-1)
    assert FreshnessConfig().max_queue_wait_ms == 150  # 명세 초기값


def test_stats_summary(setup):
    sched, clock, log, _ = setup()
    for seq in (0, 1, 5):
        clock.set(T0 + 40 * seq + 30)
        sched.offer(_frame("cam1", seq))
        clock.advance(10)
        sched.take()
    s = log.stats["cam1"].summary()
    assert s["inferred"] == 3
    assert s["max_result_interval_ms"] == 160
    assert s["frame_age_ms_p95"] == 40 and s["queue_wait_ms_max"] == 10
    assert s["loss_rate"] is None  # 순서 도착 수는 ingest 가 센다


def test_percentile():
    assert percentile([], 95) is None
    assert percentile(list(range(1, 101)), 95) == 95
    assert percentile([7], 99) == 7


def test_result_gate_drops_older_results():
    """오래된 프레임의 뒤늦은 결과로 LIVE 상태를 갱신하지 않는다 (명세 5.1)."""
    gate = ResultGate()
    assert gate.admit("cam1", T0 + 80)
    assert not gate.admit("cam1", T0 + 40)
    assert not gate.admit("cam1", T0 + 80)
    assert gate.admit("cam2", T0 + 40)  # 카메라별
    assert gate.admit("cam1", T0 + 120)
    assert gate.last_applied("cam1") == T0 + 120


def test_lazy_decoded_picture_is_only_materialized_on_take_and_never_when_skipped(setup):
    """-386: 대기열에서 SUPERSEDED·OUT_OF_ORDER·QUEUE_WAIT_EXCEEDED·STREAM_RESET 으로 버려진 프레임은
    DecodedPicture.image 를 읽지 않고, take/take_one 으로 꺼낸 프레임만 잠금 밖에서 구체화한다."""
    sched, clock, _, _ = setup()
    converted: list[int] = []

    def _lazy_frame(cam: str, seq: int, t: int | None = None) -> tuple[Frame, DecodedPicture]:
        def _load() -> str:
            # 대기열 잠금 밖에서 불리는지 확인(잠금 안이면 pending() 이 데드락에 걸린다)
            assert sched.pending(cam) == 0
            converted.append(seq)
            return f"bgr-{cam}-{seq}"

        pic = DecodedPicture((0, seq), loader=_load)
        return (
            Frame(
                camera_id=cam,
                estimated_capture_time=T0 + 40 * seq if t is None else t,
                timestamp_source="RECEIVE_ESTIMATED",
                frame_ref={"stream_epoch": 0, "frame_sequence": seq},
                image=pic,
            ),
            pic,
        )

    f0, p0 = _lazy_frame("cam1", 0)
    f1, p1 = _lazy_frame("cam1", 1)
    f_old, p_old = _lazy_frame("cam1", 99, t=T0)  # f1(T0+40) 보다 이른 시각 -> OUT_OF_ORDER
    sched.offer(f0)
    sched.offer(f1)  # f0 은 SUPERSEDED
    assert not sched.offer(f_old)
    assert converted == [] and not p0.converted and not p1.converted and not p_old.converted

    (ticket,) = sched.take()
    assert ticket.frame.image == "bgr-cam1-1"
    assert converted == [1] and p1.converted and not p0.converted and not p_old.converted

    # 만료(QUEUE_WAIT_EXCEEDED)와 discard(STREAM_RESET)도 변환하지 않는다
    f2, p2 = _lazy_frame("cam1", 2, t=T0 + 80)
    f3, p3 = _lazy_frame("cam2", 3, t=T0 + 80)
    sched.offer(f2)
    sched.offer(f3)
    sched.discard("cam2", SkipReason.STREAM_RESET)
    clock.advance(200)
    assert sched.take_one() is None
    assert converted == [1] and not p2.converted and not p3.converted


# --------------------------------------------------------------------------- 수신·추론 스레드 경합 (리뷰 R2)


class _PausingClock:
    """consumer 스레드가 시각을 읽은 직후 멈추게 하는 주입 시계(스레드 선점을 결정적으로 흉내 낸다)."""

    def __init__(self, t):
        self.t = t
        self.seen = threading.Event()
        self.resume = threading.Event()

    def now_ms(self):
        t = self.t
        if threading.current_thread().name == "consumer":
            self.seen.set()
            assert self.resume.wait(5)
        return t


def test_take_does_not_judge_frames_offered_after_its_clock_read():
    """take 가 시각을 읽고 멈춘 사이 수신 스레드가 더 늦은 시각에 넣은 프레임을, 앞서 읽은 시각으로 판정하지 않는다.

    그렇게 하면 queue_wait 가 음수가 되어 예외가 나고, 이미 꺼낸 프레임이 기록 없이 사라진다.
    """
    clock = _PausingClock(T0)
    config = FreshnessConfig()
    records = []
    sched = LatestFrameScheduler(("cam1",), clock, config, FrameLog(("cam1",), config, records.append))
    errors, taken = [], []

    def consume():
        try:
            taken.extend(sched.take())
        except Exception as e:  # noqa: BLE001 - 스레드 안 예외를 시험으로 옮긴다
            errors.append(repr(e))

    consumer = threading.Thread(target=consume, name="consumer")
    consumer.start()
    assert clock.seen.wait(5)
    clock.t = T0 + 1
    producer = threading.Thread(target=sched.offer, args=(_frame("cam1", 0, t=T0),), name="producer")
    producer.start()
    producer.join(0.2)  # 고친 뒤에는 take 가 잠금을 쥐고 있어 offer 가 기다린다
    clock.resume.set()
    consumer.join(5)
    producer.join(5)
    assert errors == []
    assert all(0 <= t.queue_wait_ms <= 150 for t in taken)
    assert len(taken) + sched.pending("cam1") == 1  # 프레임이 사라지지 않는다
    assert all(r.disposition is not FrameDisposition.SKIPPED_BY_POLICY for r in records)


class _SteppingClock:
    def __init__(self, t):
        self.t = t

    def now_ms(self):
        return self.t


def test_clock_step_back_does_not_lose_frame():
    """시스템 시계가 뒤로 조정돼도(NTP 등) 대기열 시각은 뒤로 가지 않아 프레임을 잃거나 예외를 내지 않는다."""
    clock = _SteppingClock(T0 + 100)
    config = FreshnessConfig()
    sched = LatestFrameScheduler(("cam1",), clock, config, FrameLog(("cam1",), config))
    sched.offer(_frame("cam1", 0, t=T0))
    clock.t = T0 + 50
    (ticket,) = sched.take()
    assert ticket.queue_wait_ms == 0 and ticket.dispatched_at_ms == T0 + 100


class _PauseOnceClock:
    """consumer 스레드의 첫 시각 읽기 직후 한 번만 멈추게 하는 주입 시계(이미 오래 기다린 프레임의 선점 순서)."""

    def __init__(self, t):
        self.t = t
        self.seen = threading.Event()
        self.resume = threading.Event()

    def now_ms(self):
        t = self.t
        if threading.current_thread().name == "consumer" and not self.seen.is_set():
            self.seen.set()
            assert self.resume.wait(5)
        return t


@pytest.mark.parametrize("method", ["take", "take_one"])
@pytest.mark.parametrize(("paused_at", "resumed_at", "handed"), [(150, 151, False), (149, 150, True)])
def test_preempted_after_clock_read_rechecks_wait_before_handoff(method, paused_at, resumed_at, handed):
    """consumer 가 시각을 읽은 직후 선점돼 그사이 대기가 상한을 넘으면, 넘기지 않고 QUEUE_WAIT_EXCEEDED 로 버린다."""
    clock = _PauseOnceClock(T0)
    config = FreshnessConfig()
    records = []
    sched = LatestFrameScheduler(("cam1",), clock, config, FrameLog(("cam1",), config, records.append))
    sched.offer(_frame("cam1", 0, t=T0))
    clock.t = T0 + paused_at
    out, errors = [], []

    def consume():
        try:
            got = getattr(sched, method)()
            out.extend(got if isinstance(got, list) else [got] if got is not None else [])
        except Exception as e:  # noqa: BLE001 - 스레드 안 예외를 시험으로 옮긴다
            errors.append(repr(e))

    consumer = threading.Thread(target=consume, name="consumer")
    consumer.start()
    assert clock.seen.wait(5)
    clock.t = T0 + resumed_at  # 선점된 동안 1ms 가 지났다
    clock.resume.set()
    consumer.join(5)
    assert errors == []
    if handed:
        (ticket,) = out
        assert (ticket.dispatched_at_ms, ticket.queue_wait_ms) == (T0 + 150, 150)
    else:
        assert out == []
        assert [r.reason for r in records] == [SkipReason.QUEUE_WAIT_EXCEEDED]


def test_inferred_record_reaches_sink_after_handoff():
    """인계(INFERRED) 기록의 sink 는 돌려준 뒤 다음 호출 때 부른다. sink 가 걸린 시간은 다음 인계의 대기에 들어간다."""
    clock = FixedClock(T0)
    config = FreshnessConfig()
    records = []

    def slow_sink(rec):
        records.append(rec)
        if rec.disposition is FrameDisposition.INFERRED:
            clock.advance(1)

    sched = LatestFrameScheduler(("cam1", "cam2"), clock, config, FrameLog(("cam1", "cam2"), config, slow_sink))
    sched.offer(_frame("cam1", 0, t=T0))
    sched.offer(_frame("cam2", 0, t=T0))
    clock.set(T0 + 150)
    first = sched.take_one()
    assert (first.frame.camera_id, first.dispatched_at_ms, clock.now_ms()) == ("cam1", T0 + 150, T0 + 150)
    assert records == []  # 돌려주기 전에 sink 를 부르지 않았다
    assert sched.take_one() is None  # cam1 기록을 sink 로 보내는 1ms 사이 cam2 가 151ms 를 기다렸다
    assert [(r.camera_id, r.disposition, r.reason) for r in records] == [
        ("cam1", FrameDisposition.INFERRED, None),
        ("cam2", FrameDisposition.SKIPPED_BY_POLICY, SkipReason.QUEUE_WAIT_EXCEEDED),
    ]
