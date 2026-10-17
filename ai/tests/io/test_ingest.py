"""I0 수신 → 디코딩 → 대기열 (명세 12.7 재접속·누락·역순, 5.1 SKIPPED_BY_POLICY/NETWORK_LOSS)."""

import pytest

from simai_ai.io import FakeDecoder, FrameDisposition, FreshnessConfig, I0CodecInfo, I0Ingest, SkipReason
from simai_ai.io.decoder import DecodedPicture


def _skips(ingest, reason):
    return [(r.stream_epoch, r.frame_sequence) for r in ingest.records if r.reason is reason]


def _taken(ingest):
    return [(t.frame.frame_ref["stream_epoch"], t.frame.frame_ref["frame_sequence"]) for t in ingest.next_batch()]


def test_codec_then_keyframe_then_frames(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    assert ingest.ready_cameras() == ()  # 키프레임 전에는 관측 없음
    ingest.receive(i0.frame(seq=0, keyframe=True))
    assert ingest.ready_cameras() == ("cam1",)
    (ticket,) = ingest.next_batch()
    f = ticket.frame
    assert (f.camera_id, f.estimated_capture_time, f.timestamp_source) == ("cam1", i0.T0, "RECEIVE_ESTIMATED")
    assert f.frame_ref == {"stream_epoch": 0, "frame_sequence": 0}
    assert f.timestamp_uncertainty_ms == 18
    assert f.image.frame_sequence == 0


def test_frames_before_codec_info_are_not_observations(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=1))  # 코덱 정보 뒤지만 키프레임 전
    ingest.receive(i0.frame(seq=2, keyframe=True))
    assert _skips(ingest, SkipReason.NO_CODEC_INFO) == [(0, 0)]
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 1)]
    assert _taken(ingest) == [(0, 2)]
    assert ingest.log.stats["cam1"].network_loss == 0


def test_sequence_gap_is_network_loss_and_waits_for_keyframe(make_ingest, i0, clock):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(seq=3))  # 1, 2 손실
    ingest.receive(i0.frame(seq=4))
    ingest.receive(i0.frame(seq=5, keyframe=True))
    assert ingest.next_batch() == [] and _loss(ingest) == []  # 보간 창(200ms) 안에서는 1, 2 를 기다린다
    clock.advance(201)
    loss = _loss(ingest)
    assert loss == []  # 확정은 수신 또는 추론 루프(poll)가 시각을 볼 때 한다
    taken = _taken(ingest)
    assert _loss(ingest) == [1, 2]
    assert all(r.estimated_capture_time is None for r in ingest.records if r.frame_sequence in (1, 2))
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 3), (0, 4)]
    assert taken == [(0, 5)]
    stats = ingest.log.stats["cam1"]
    assert stats.network_loss == 2 and stats.skipped_total == 2
    assert stats.loss_rate == pytest.approx(2 / 6)  # 손실률에는 NETWORK_LOSS 만


def test_late_arrival_is_not_loss(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.receive(i0.frame(seq=2))
    ingest.receive(i0.frame(seq=1))  # 늦게 도착(창 안이라 정렬해 쓴다)
    ingest.receive(i0.frame(seq=2))  # 중복
    stats = ingest.log.stats["cam1"]
    assert stats.network_loss == 0
    assert stats.loss_rate == 0
    assert _skips(ingest, SkipReason.LATE_ARRIVAL) == [(0, 2)]
    assert stats.decoded == 3  # 0, 1, 2 를 순서대로 디코딩했다


def test_reconnect_resets_decoder_and_drops_old_epoch(make_ingest, i0):
    ingest = make_ingest(delay=1)
    ingest.receive(i0.codec(epoch=0))
    ingest.receive(i0.frame(epoch=0, seq=0, keyframe=True))
    ingest.receive(i0.frame(epoch=0, seq=1))  # 0 이 나오고 1 은 디코더 안에 남음
    dec = ingest.decoders["cam1"]
    assert dec.configure_count == 1
    ingest.receive(i0.codec(epoch=1))
    assert dec.configure_count == 2
    assert _skips(ingest, SkipReason.STREAM_RESET) == [(0, 1), (0, 0)]  # 디코더 안, 추론 대기열 순
    ingest.receive(i0.frame(epoch=0, seq=2))  # 끊긴 연결에서 늦게 온 프레임
    ingest.receive(i0.frame(epoch=1, seq=0))  # 새 epoch 인데 키프레임이 아님
    ingest.receive(i0.frame(epoch=1, seq=1, keyframe=True, t=i0.T0 + 5000))
    ingest.receive(i0.frame(epoch=1, seq=2, t=i0.T0 + 5040))
    assert _skips(ingest, SkipReason.STALE_EPOCH) == [(0, 2)]
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(1, 0)]
    assert _taken(ingest) == [(1, 1)]
    ingest.receive(i0.codec(epoch=0))  # 끝난 연결의 코덱 정보는 무시
    assert dec.configure_count == 2


def test_new_epoch_frame_without_codec_info_resets(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec(epoch=0))
    ingest.receive(i0.frame(epoch=0, seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(epoch=1, seq=0, keyframe=True, t=i0.T0 + 3000))
    assert ingest.ready_cameras() == ()
    assert _skips(ingest, SkipReason.NO_CODEC_INFO) == [(1, 0)]
    ingest.receive(i0.codec(epoch=1))
    ingest.receive(i0.frame(epoch=1, seq=1, keyframe=True, t=i0.T0 + 3040))
    assert _taken(ingest) == [(1, 1)]


def test_repeated_codec_info_is_noop_but_changed_codec_restarts(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.receive(i0.codec())
    assert ingest.decoders["cam1"].configure_count == 1 and ingest.ready_cameras() == ("cam1",)
    changed = i0.codec().__class__(camera_id="cam1", stream_epoch=0, codec="hevc")
    ingest.receive(changed)
    assert ingest.decoders["cam1"].configure_count == 2 and ingest.ready_cameras() == ()


def test_decode_failure_waits_for_keyframe(make_ingest, i0):
    ingest = make_ingest(fail_keys=frozenset({(0, 1)}))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(seq=1))
    ingest.receive(i0.frame(seq=2))
    ingest.receive(i0.frame(seq=3, keyframe=True))
    failed = [r.frame_sequence for r in ingest.records if r.disposition is FrameDisposition.DECODE_FAILED]
    assert failed == [1]
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 2)]
    assert _taken(ingest) == [(0, 3)]
    assert ingest.log.stats["cam1"].network_loss == 0


def test_decoder_pending_bound(make_ingest, i0):
    ingest = make_ingest(config=FreshnessConfig(max_decoder_pending=2), delay=3)
    ingest.receive(i0.codec())
    for seq in range(4):
        ingest.receive(i0.frame(seq=seq, keyframe=seq == 0))
    assert _skips(ingest, SkipReason.DECODER_DROPPED) == [(0, 0), (0, 1)]
    ingest.close()  # 버린 프레임의 그림이 뒤늦게 나와도 헤더를 붙이지 않는다
    assert _taken(ingest) == [(0, 3)]


def test_unknown_decoder_key_is_an_error(make_ingest, i0):
    """디코더가 넣지 않은 열쇠의 그림을 내면 헤더를 추측해 붙이지 않고 오류로 멈춘다."""
    ingest = make_ingest()
    ingest.receive(i0.codec())
    dec = ingest.decoders["cam1"]
    dec.decode = lambda payload, key, keyframe: [DecodedPicture((0, 99), None)]
    with pytest.raises(RuntimeError):
        ingest.receive(i0.frame(seq=0, keyframe=True))


def test_wrong_camera_and_unknown_messages(make_ingest, i0):
    ingest = make_ingest()
    with pytest.raises(ValueError):
        ingest.receive(i0.frame(cam="cam9", seq=0))
    with pytest.raises(ValueError):
        ingest.camera("cam1").on_frame(i0.frame(cam="cam2", seq=0))
    with pytest.raises(TypeError):
        ingest.receive({"camera_id": "cam1"})


def test_every_arrived_frame_has_one_disposition(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    msgs = [i0.frame(seq=s, keyframe=s % 5 == 0) for s in (0, 1, 2, 4, 5, 6, 3, 7)]
    for m in msgs:
        ingest.receive(m)
    ingest.close()
    ingest.next_batch()
    ingest.log.deliver()  # 인계 기록은 다음 호출 때 sink 로 가므로 마지막 것을 보낸다
    arrived = [r.frame_sequence for r in ingest.records if r.disposition is not FrameDisposition.NETWORK_LOSS]
    assert sorted(arrived) == list(range(8))
    assert ingest.log.stats["cam1"].network_loss == 0  # 3 은 창 안에 왔으므로 정렬해 썼다
    assert _skips(ingest, SkipReason.LATE_ARRIVAL) == []


def test_keep_decoding_after_loss_when_configured(make_ingest, i0, clock):
    ingest = make_ingest(config=FreshnessConfig(keyframe_after_loss=False))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(seq=2))
    clock.advance(201)
    assert _taken(ingest) == [(0, 2)]
    assert ingest.log.stats["cam1"].network_loss == 1


def test_reorder_window_zero_confirms_loss_at_once(make_ingest, i0):
    """카메라별 TCP 처럼 순서가 바뀌지 않는 연결이면 창을 0 으로 두어 빈칸을 바로 손실로 확정할 수 있다."""
    ingest = make_ingest(config=FreshnessConfig(reorder_window_ms=0, keyframe_after_loss=False))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(seq=2))
    assert _loss(ingest) == [1]
    assert _taken(ingest) == [(0, 2)]


def test_reorder_buffer_bound_confirms_loss_early(make_ingest, i0):
    """붙잡은 프레임이 max_reorder_frames 를 넘으면 창을 기다리지 않고 손실을 확정한다."""
    ingest = make_ingest(config=FreshnessConfig(max_reorder_frames=2))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    for seq in (2, 3):
        ingest.receive(i0.frame(seq=seq))
    assert _loss(ingest) == []
    ingest.receive(i0.frame(seq=4, keyframe=True))
    assert _loss(ingest) == [1]
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 2), (0, 3)]
    assert _taken(ingest) == [(0, 4)]


def test_close_confirms_pending_gap(make_ingest, i0):
    """입력이 끝나면 정렬을 기다리던 빈 번호는 손실로 확정하고 뒤 프레임을 내보낸다."""
    ingest = make_ingest(config=FreshnessConfig(keyframe_after_loss=False))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(seq=2))
    ingest.close()
    assert _loss(ingest) == [1]
    assert _taken(ingest) == [(0, 2)]


def test_new_epoch_abandons_frames_waiting_for_reorder(make_ingest, i0):
    """정렬을 기다리던 이전 epoch 프레임은 새 epoch 가 오면 디코딩하지 않고 버리고, 앞 빈 번호는 손실로 센다."""
    ingest = make_ingest()
    ingest.receive(i0.codec(epoch=0))
    ingest.receive(i0.frame(epoch=0, seq=0, keyframe=True))
    ingest.next_batch()
    ingest.receive(i0.frame(epoch=0, seq=2))
    ingest.receive(i0.codec(epoch=1))
    assert _loss(ingest) == [1]
    assert _skips(ingest, SkipReason.STREAM_RESET) == [(0, 2)]
    assert ingest.next_batch() == []


# --------------------------------------------------------------------------- 재초기화와 대기열 (리뷰 R3)


@pytest.mark.parametrize("depth", [1, 2])
def test_new_epoch_codec_invalidates_queued_frames(make_ingest, i0, depth):
    """새 epoch 의 코덱 정보만 온 시점: 이전 epoch 에서 이미 디코딩돼 대기열에 있던 프레임도 관측으로 내지 않는다."""
    ingest = make_ingest(config=FreshnessConfig(queue_depth=depth))
    ingest.receive(i0.codec(epoch=0))
    ingest.receive(i0.frame(epoch=0, seq=0, keyframe=True))
    ingest.receive(i0.frame(epoch=0, seq=1))
    ingest.receive(i0.codec(epoch=1))
    assert ingest.ready_cameras() == ()
    assert ingest.next_batch() == []
    queued = [(0, 1)] if depth == 1 else [(0, 0), (0, 1)]
    assert _skips(ingest, SkipReason.STREAM_RESET) == queued
    assert ingest.scheduler.pending("cam1") == 0


@pytest.mark.parametrize("depth", [1, 2])
def test_new_epoch_frame_before_codec_invalidates_queued_frames(make_ingest, i0, depth):
    """새 epoch 프레임이 코덱 정보보다 먼저 온 시점에도 이전 epoch 대기 프레임은 버린다."""
    ingest = make_ingest(config=FreshnessConfig(queue_depth=depth))
    ingest.receive(i0.codec(epoch=0))
    ingest.receive(i0.frame(epoch=0, seq=0, keyframe=True))
    ingest.receive(i0.frame(epoch=1, seq=0, keyframe=True, t=i0.T0 + 3000))
    assert ingest.ready_cameras() == ()
    assert ingest.next_batch() == []
    assert _skips(ingest, SkipReason.STREAM_RESET) == [(0, 0)]
    assert _skips(ingest, SkipReason.NO_CODEC_INFO) == [(1, 0)]


def test_changed_codec_in_same_epoch_invalidates_queued_frames(make_ingest, i0):
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.receive(i0.codec().__class__(camera_id="cam1", stream_epoch=0, codec="hevc"))
    assert ingest.ready_cameras() == ()
    assert ingest.next_batch() == []
    assert _skips(ingest, SkipReason.STREAM_RESET) == [(0, 0)]


# --------------------------------------------------------------------------- 보간 창 안 역순 정렬 (리뷰 R4)


def _loss(ingest):
    return [r.frame_sequence for r in ingest.records if r.disposition is FrameDisposition.NETWORK_LOSS]


def test_out_of_order_within_window_is_reordered(make_ingest, i0, clock):
    """0, 2, 1, 3 순서 도착(1 은 41ms 늦음, 창 200ms 안): 정렬해 디코딩하고 손실·키프레임 대기로 바꾸지 않는다."""
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    clock.set(i0.T0 + 80)
    ingest.receive(i0.frame(seq=2))
    assert ingest.next_batch() == []  # 1 을 기다린다
    assert _loss(ingest) == []
    clock.set(i0.T0 + 81)
    ingest.receive(i0.frame(seq=1))
    assert ingest.ready_cameras() == ("cam1",)
    (ticket,) = ingest.next_batch()
    f = ticket.frame
    assert f.frame_ref == {"stream_epoch": 0, "frame_sequence": 2}
    assert (f.estimated_capture_time, f.image.frame_sequence) == (i0.T0 + 80, 2)  # 시각이 제 그림에 붙는다
    assert _skips(ingest, SkipReason.SUPERSEDED) == [(0, 1)]  # 1 은 디코딩됐고 2 에 밀렸다
    clock.set(i0.T0 + 120)
    ingest.receive(i0.frame(seq=3))
    assert [t.frame.frame_ref["frame_sequence"] for t in ingest.next_batch()] == [3]
    assert _skips(ingest, SkipReason.LATE_ARRIVAL) == []
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == []
    stats = ingest.log.stats["cam1"]
    assert stats.network_loss == 0 and stats.loss_rate == 0


def test_out_of_order_beyond_window_is_loss_then_late(make_ingest, i0, clock):
    """창(200ms)을 넘기도록 오지 않으면 그때 손실로 확정한다. 그 뒤에 온 프레임은 버리고 기록만 한다."""
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    clock.set(i0.T0 + 80)
    ingest.receive(i0.frame(seq=2))
    clock.set(i0.T0 + 280)  # 2 가 도착한 지 200ms: 아직 창 안
    assert ingest.next_batch() == [] and _loss(ingest) == []
    clock.set(i0.T0 + 281)
    assert ingest.next_batch() == []
    assert _loss(ingest) == [1]
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 2)]
    ingest.receive(i0.frame(seq=1))
    assert _skips(ingest, SkipReason.LATE_ARRIVAL) == [(0, 1)]
    assert ingest.log.stats["cam1"].network_loss == 0  # 늦게라도 왔으므로 손실에서 뺀다
    ingest.receive(i0.frame(seq=3, keyframe=True))
    assert [t.frame.frame_ref["frame_sequence"] for t in ingest.next_batch()] == [3]


# --------------------------------------------------------------------------- 재리뷰 F2·F3


def _hevc():
    return I0CodecInfo(camera_id="cam1", stream_epoch=0, codec="hevc")


@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize(
    "held", [((2, True),), ((2, False), (3, True))], ids=["held-keyframe", "held-reference-then-keyframe"]
)
def test_changed_codec_in_same_epoch_abandons_frames_waiting_for_reorder(make_ingest, i0, clock, depth, held):
    """같은 epoch 코덱 변경 때 정렬을 기다리던 이전 코덱의 압축 프레임도 버린다(새 코덱 디코더에 넣지 않는다).

    새 코덱의 키프레임이 올 때까지 그 카메라는 관측 없음이고, 앞 빈 번호는 새 epoch 때와 같이 손실로 확정한다.
    """
    ingest = make_ingest(config=FreshnessConfig(queue_depth=depth))
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    clock.set(i0.T0 + 80)
    for seq, keyframe in held:
        ingest.receive(i0.frame(seq=seq, keyframe=keyframe))  # 1 이 없어 정렬을 기다린다
    ingest.receive(_hevc())
    assert ingest.ready_cameras() == ()
    assert _skips(ingest, SkipReason.STREAM_RESET) == [(0, seq) for seq, _ in held]
    assert _loss(ingest) == [1]
    clock.set(i0.T0 + 281)  # 새 코덱 프레임은 아직 없다
    assert ingest.next_batch() == []
    assert ingest.ready_cameras() == ()
    ingest.receive(i0.frame(seq=1))  # 변경 전 번호가 늦게 옴
    assert _skips(ingest, SkipReason.LATE_ARRIVAL) == [(0, 1)]
    assert ingest.log.stats["cam1"].network_loss == 0
    nxt = held[-1][0] + 1
    ingest.receive(i0.frame(seq=nxt))  # 새 코덱의 참조 프레임: 키프레임 전이라 쓰지 않는다
    assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, nxt)]
    ingest.receive(i0.frame(seq=nxt + 1, keyframe=True))  # 새 코덱의 키프레임으로 복구
    assert ingest.ready_cameras() == ("cam1",)
    assert [t.frame.frame_ref["frame_sequence"] for t in ingest.next_batch()] == [nxt + 1]


@pytest.mark.parametrize("poll_first", [False, True], ids=["no-poll", "poll"])
@pytest.mark.parametrize("elapsed", [200, 201])
def test_reorder_window_judged_same_with_or_without_poll(make_ingest, i0, clock, elapsed, poll_first):
    """0, 2, 1, 3 도착. 1 이 2 보다 창(200ms) 안에 오면 정렬해 쓰고, 넘겨 오면 버린다. 추론 루프의 poll 시점과 무관하다."""
    ingest = make_ingest()
    ingest.receive(i0.codec())
    ingest.receive(i0.frame(seq=0, keyframe=True))
    ingest.next_batch()
    clock.set(i0.T0 + 80)
    ingest.receive(i0.frame(seq=2))
    clock.set(i0.T0 + 80 + elapsed)
    if poll_first:
        ingest.poll()
    ingest.receive(i0.frame(seq=1))
    out = [t.frame.frame_ref["frame_sequence"] for t in ingest.next_batch()]
    reordered = elapsed <= 200
    if reordered:
        assert out == [2] and ingest.ready_cameras() == ("cam1",)
        assert _loss(ingest) == [] and _skips(ingest, SkipReason.LATE_ARRIVAL) == []
    else:
        assert out == [] and ingest.ready_cameras() == ()
        assert _loss(ingest) == [1]
        assert _skips(ingest, SkipReason.WAITING_FOR_KEYFRAME) == [(0, 2)]
        assert _skips(ingest, SkipReason.LATE_ARRIVAL) == [(0, 1)]
    ingest.receive(i0.frame(seq=3, keyframe=not reordered))
    assert [t.frame.frame_ref["frame_sequence"] for t in ingest.next_batch()] == [3]
    assert ingest.log.stats["cam1"].network_loss == 0


# --------------------------------------------------------------------------- 재리뷰 F1 (인계 직전 대기 상한 재검사)


@pytest.mark.parametrize("method", ["next_frame", "next_batch"])
@pytest.mark.parametrize("gap, handed", [(1, False), (2, True)], ids=["151ms-dropped", "150ms-handed"])
def test_slow_expiry_sink_rechecks_wait_before_handoff(clock, i0, method, gap, handed):
    """다른 카메라의 만료 기록을 받는 sink 가 느려도(1ms), 넘기는 프레임의 대기는 넘기는 순간의 시각으로 잰다."""
    records = []

    def slow_sink(rec):
        records.append(rec)
        if rec.reason is SkipReason.QUEUE_WAIT_EXCEEDED:
            clock.advance(1)

    ingest = I0Ingest(("cam1", "cam2"), lambda _: FakeDecoder(), clock, sink=slow_sink)
    for cam in ("cam1", "cam2"):
        ingest.receive(i0.codec(cam=cam))
    ingest.receive(i0.frame(cam="cam1", seq=0, keyframe=True))
    clock.advance(gap)
    ingest.receive(i0.frame(cam="cam2", seq=0, keyframe=True))
    clock.set(i0.T0 + 151)
    out = getattr(ingest, method)()
    tickets = out if isinstance(out, list) else [out] if out is not None else []
    for t in tickets:
        assert t.dispatched_at_ms == clock.now_ms()  # 보고한 인계 시각 = 실제로 돌려받은 시각
        assert t.queue_wait_ms <= 150
    assert [t.frame.camera_id for t in tickets] == (["cam2"] if handed else [])
    expired = [r.camera_id for r in records if r.reason is SkipReason.QUEUE_WAIT_EXCEEDED]
    assert expired == (["cam1"] if handed else ["cam1", "cam2"])
    if handed:
        assert tickets[0].queue_wait_ms == 150  # 정확히 150ms 는 추론한다
