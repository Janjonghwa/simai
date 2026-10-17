"""결정적 I0 시나리오 시험 (명세 15.1 G3 'I0 재접속·역순·누락에서 촬영 시각이 다른 프레임에 결합' 0건, G0 최신성).

카메라 4대(25 fps)의 I0 메시지를 도착 시각과 함께 만들고, 주입 시계로 수신(디코딩)과 느린 추론 루프를 번갈아 돌린다.
FakeDecoder 의 그림에는 압축 데이터에 적힌 '실제 프레임'이 들어 있으므로, 추론에 넘긴 프레임의 헤더(촬영 시각·
frame_ref)가 그 그림을 보낸 헤더와 다르면 결합 오류다. 같은 입력이면 기록이 매번 같아야 한다.
"""

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import pairwise

import pytest

from simai_ai.io import (
    DecodedPicture,
    FakeDecoder,
    FrameDisposition,
    FreshnessConfig,
    I0CodecInfo,
    I0Frame,
    I0FrameHeader,
    I0Ingest,
    SkipReason,
)
from simai_ai.io.decoder import fake_payload
from simai_ai.risk.clock import FixedClock

T0 = 1760342400000
FRAME_MS = 40  # 25 fps
CAMERAS = ("cam1", "cam2", "cam3", "cam4")


@dataclass
class Scenario:
    n_frames: int = 400  # 카메라당 16초
    keyint: int = 50  # 키프레임 2초
    drop_rate: float = 0.02  # 노트북 → RunPod 손실
    reconnect_at: tuple[int, ...] = (150, 300)  # 이 프레임 번호에서 재접속(새 stream_epoch)
    reconnect_gap: int = 12  # 재접속 동안 못 보낸 프레임 수
    swap_rate: float = 0.0  # 이웃 프레임 도착 순서가 바뀌는 비율(다중화 연결 비교군)
    stall_at: int = 220  # 이 프레임 앞뒤로 cam2 업로드가 크게 늦는다
    queue_depth: int = 1
    decoder: dict = field(default_factory=dict)
    seed: int = 21


@dataclass
class Sent:
    events: list  # (arrival_ms, order, message)
    headers: dict  # (cam, epoch, seq) → I0FrameHeader
    dropped: set  # 보내지 못한 (cam, epoch, seq)


def build_streams(sc: Scenario) -> Sent:
    rng = random.Random(sc.seed)
    events, headers, dropped = [], {}, set()
    order = 0
    for ci, cam in enumerate(CAMERAS):
        offset = 7 * ci  # 카메라마다 노출 순간이 다르다
        epoch, seq, last_arrival = 0, 0, 0
        pending_codec = True
        skip_until = -1
        per_cam = []
        for k in range(sc.n_frames):
            if k in sc.reconnect_at:
                epoch, seq, pending_codec, skip_until = epoch + 1, 0, True, k + sc.reconnect_gap
            if k < skip_until:
                continue
            capture = T0 + offset + FRAME_MS * k
            header = I0FrameHeader(
                camera_id=cam,
                stream_epoch=epoch,
                frame_sequence=seq,
                estimated_capture_time_ms=capture + rng.randint(-15, 15),
                timestamp_source="RECEIVE_ESTIMATED",
                keyframe=seq % sc.keyint == 0,
                timestamp_uncertainty_ms=18,
                source_pts=90 * FRAME_MS * k,
                pts_timebase="1/90000",
            )
            headers[(cam, epoch, seq)] = header
            delay = rng.randint(20, 60)
            if cam == "cam2" and abs(k - sc.stall_at) < 10:
                delay += 600
            arrival = max(last_arrival, capture + delay)
            last_arrival = arrival
            if pending_codec:
                per_cam.append([arrival, I0CodecInfo(cam, epoch, "h264", "1/90000", b"S", b"P")])
                pending_codec = False
            if seq > 0 and rng.random() < sc.drop_rate:
                dropped.add((cam, epoch, seq))
            else:
                per_cam.append([arrival, I0Frame(header, fake_payload(cam, epoch, seq, capture))])
            seq += 1
        for i in range(1, len(per_cam) - 1):
            a, b = per_cam[i], per_cam[i + 1]
            if isinstance(a[1], I0Frame) and isinstance(b[1], I0Frame) and rng.random() < sc.swap_rate:
                a[1], b[1] = b[1], a[1]
        for arrival, msg in per_cam:
            events.append((arrival, order, msg))
            order += 1
    events.sort(key=lambda e: (e[0], e[1]))
    return Sent(events, headers, dropped)


def run(sc: Scenario, decoder_cls=FakeDecoder):
    """수신은 도착 시각에, 추론은 앞 추론이 끝난 시각에 한다(디코딩과 추론 분리)."""
    sent = build_streams(sc)
    clock = FixedClock(T0)
    records, tickets = [], []
    ingest = I0Ingest(
        CAMERAS,
        lambda cam: decoder_cls(**sc.decoder),
        clock,
        FreshnessConfig(queue_depth=sc.queue_depth),
        sink=records.append,
    )
    rng = random.Random(sc.seed + 1)
    i, t_free = 0, T0
    while True:
        while i < len(sent.events) and sent.events[i][0] <= t_free:
            clock.set(max(clock.now_ms(), sent.events[i][0]))
            ingest.receive(sent.events[i][2])
            i += 1
        clock.set(max(clock.now_ms(), t_free))
        batch = ingest.next_batch()
        if batch:
            tickets += batch
            t_free = clock.now_ms() + rng.choice([35, 45, 60, 90, 220])  # 느린 추론(가끔 상한보다 김)
        elif i < len(sent.events):
            t_free = sent.events[i][0]
        else:
            break
    ingest.close()
    tickets += ingest.next_batch()
    ingest.log.deliver()  # 마지막 인계 기록을 sink 로
    return sent, ingest, records, tickets


def run_sequential(sc: Scenario):
    """추론 루프가 한 장씩 차례로 추론한다(next_frame). 추론하는 동안에도 수신·디코딩은 도착 시각에 계속된다.

    넘겨받은 순간의 시계와 ticket 의 dispatched_at_ms 가 같은지도 함께 모은다(보고한 대기 = 실제 대기).
    """
    sent = build_streams(sc)
    clock = FixedClock(T0)
    records, tickets, stale = [], [], []
    ingest = I0Ingest(
        CAMERAS,
        lambda cam: FakeDecoder(**sc.decoder),
        clock,
        FreshnessConfig(queue_depth=sc.queue_depth),
        sink=records.append,
    )
    rng = random.Random(sc.seed + 2)
    i, t_free = 0, T0
    while True:
        while i < len(sent.events) and sent.events[i][0] <= t_free:
            clock.set(max(clock.now_ms(), sent.events[i][0]))
            ingest.receive(sent.events[i][2])
            i += 1
        clock.set(max(clock.now_ms(), t_free))
        ticket = ingest.next_frame()
        if ticket is not None:
            tickets.append(ticket)
            if ticket.dispatched_at_ms != clock.now_ms():
                stale.append(ticket)
            t_free = clock.now_ms() + rng.choice([20, 30, 45, 60, 110])  # 한 장 추론(가끔 상한 가까이)
        elif i < len(sent.events):
            t_free = sent.events[i][0]
        else:
            break
    ingest.close()
    while (ticket := ingest.next_frame()) is not None:
        tickets.append(ticket)
    return sent, ingest, records, tickets, stale


def mismatches(sent: Sent, tickets) -> list[str]:
    """추론에 넘긴 프레임에 다른 프레임의 촬영 시각·식별자가 붙은 경우."""
    out = []
    for t in tickets:
        img, f = t.frame.image, t.frame
        truth = sent.headers[(img.camera_id, img.stream_epoch, img.frame_sequence)]
        if (
            f.camera_id != img.camera_id
            or f.frame_ref != {"stream_epoch": img.stream_epoch, "frame_sequence": img.frame_sequence}
            or f.estimated_capture_time != truth.estimated_capture_time_ms
            or f.timestamp_uncertainty_ms != truth.timestamp_uncertainty_ms
        ):
            out.append(f"{f.camera_id} {f.frame_ref} t={f.estimated_capture_time} <- 그림 {img}")
    return out


SCENARIOS = {
    "per-camera-tcp": Scenario(),
    "nvdec-pipeline-delay": Scenario(decoder={"delay": 3}),
    "decoder-reorder": Scenario(decoder={"reorder_pairs": True}),
    "multiplexed-out-of-order": Scenario(swap_rate=0.03),
    "queue-depth-2": Scenario(queue_depth=2, decoder={"delay": 1}),
    "heavy-loss": Scenario(drop_rate=0.15, reconnect_at=(60, 120, 180, 240, 300, 360)),
}


@pytest.fixture(scope="module", params=sorted(SCENARIOS))
def outcome(request):
    sc = SCENARIOS[request.param]
    return sc, *run(sc)


def test_no_capture_time_bound_to_another_frame(outcome):
    """G3: 재접속·역순·누락·디코더 지연에서 촬영 시각이 다른 프레임에 결합되는 경우 0건."""
    _, sent, _, _, tickets = outcome
    assert tickets
    assert mismatches(sent, tickets) == []


def test_no_inference_over_queue_wait_cap(outcome):
    """G0: queue_wait_ms 가 대기 상한(150ms)을 넘는 추론 0건."""
    _, _, ingest, records, tickets = outcome
    assert max(t.queue_wait_ms for t in tickets) <= ingest.config.max_queue_wait_ms
    inferred = [r for r in records if r.disposition is FrameDisposition.INFERRED]
    assert len(inferred) == len(tickets)
    assert all(r.queue_wait_ms <= 150 for r in inferred)


def test_capture_time_increases_per_camera(outcome):
    _, _, _, _, tickets = outcome
    by_cam = defaultdict(list)
    for t in tickets:
        by_cam[t.frame.camera_id].append(t.frame.estimated_capture_time)
    assert set(by_cam) == set(CAMERAS)
    for times in by_cam.values():
        assert all(a < b for a, b in pairwise(times))


def test_every_frame_accounted_once(outcome):
    """도착한 프레임은 처리 결과가 정확히 하나, 손실은 NETWORK_LOSS 로만 센다."""
    _, sent, ingest, records, _ = outcome
    final = defaultdict(list)
    loss = set()
    for r in records:
        key = (r.camera_id, r.stream_epoch, r.frame_sequence)
        if r.disposition is FrameDisposition.NETWORK_LOSS:
            loss.add(key)
        else:
            final[key].append(r.disposition)
    delivered = set(sent.headers) - sent.dropped
    assert set(final) == delivered
    assert all(len(v) == 1 for v in final.values()), {k: v for k, v in final.items() if len(v) != 1}
    # 늦게 와서 손실에서 뺀 번호를 빼면, 센 손실은 실제로 사라진 프레임과 같다.
    late = {(r.camera_id, r.stream_epoch, r.frame_sequence) for r in records if r.reason is SkipReason.LATE_ARRIVAL}
    counted = sum(s.network_loss for s in ingest.log.stats.values())
    assert loss - late <= sent.dropped
    assert counted == len(loss - late)
    # epoch 끝에서 사라진 프레임은 뒤 번호가 없어 셀 수 없다. 그 밖의 손실은 모두 센다.
    last_seq = defaultdict(int)
    for cam, epoch, seq in set(sent.headers) - sent.dropped:
        last_seq[(cam, epoch)] = max(last_seq[(cam, epoch)], seq)
    detectable = {k for k in sent.dropped if k[2] < last_seq[(k[0], k[1])]}
    assert loss - late == detectable


def test_policy_skips_reported_separately(outcome):
    _, _, ingest, _, _ = outcome
    summary = {s["camera_id"]: s for s in ingest.summary()}
    reasons = set()
    for s in ingest.log.stats.values():
        reasons |= set(s.skipped_by_policy)
    assert SkipReason.SUPERSEDED in reasons
    assert SkipReason.WAITING_FOR_KEYFRAME in reasons
    cam2 = summary["cam2"]
    assert cam2["max_result_interval_ms"] >= 400  # 업로드가 막힌 구간이 최대 결과 간격으로 보인다
    assert cam2["frame_age_ms_p99"] >= cam2["frame_age_ms_p95"]
    for s in summary.values():
        assert s["loss_rate"] is not None and s["loss_rate"] < 0.5


def test_deterministic():
    sc = SCENARIOS["multiplexed-out-of-order"]
    _, _, a, ta = run(sc)
    _, _, b, tb = run(sc)
    assert a == b
    assert [(t.frame.frame_ref, t.dispatched_at_ms) for t in ta] == [
        (t.frame.frame_ref, t.dispatched_at_ms) for t in tb
    ]


class MislabelingDecoder(FakeDecoder):
    """음성 대조군: 지연 출력에 '방금 넣은 입력'의 열쇠를 붙이는 잘못된 디코더."""

    def decode(self, payload, key, keyframe):
        return [DecodedPicture(key, p.image) for p in super().decode(payload, key, keyframe)]

    def flush(self):
        super().flush()
        return []


def test_checker_detects_mislabeling_decoder():
    """결합 오류 검사가 실제로 오류를 잡는지 확인한다(검사가 늘 0 을 내는 것이 아님)."""
    sc = Scenario(decoder={"delay": 2}, n_frames=120, reconnect_at=())
    sent, _, _, tickets = run(sc, decoder_cls=MislabelingDecoder)
    assert mismatches(sent, tickets)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_sequential_handoff_keeps_freshness(name):
    """한 장씩 느리게 추론해도 넘기는 순간 대기 상한 초과 0건, 결합 오류 0건, 프레임마다 처리 결과 하나(G0·G3)."""
    sent, ingest, records, tickets, stale = run_sequential(SCENARIOS[name])
    assert tickets and stale == []
    assert max(t.queue_wait_ms for t in tickets) <= ingest.config.max_queue_wait_ms
    assert mismatches(sent, tickets) == []
    final = defaultdict(list)
    for r in records:
        if r.disposition is not FrameDisposition.NETWORK_LOSS:
            final[(r.camera_id, r.stream_epoch, r.frame_sequence)].append(r.disposition)
    assert set(final) == set(sent.headers) - sent.dropped
    assert all(len(v) == 1 for v in final.values())
    inferred = Counter(r.camera_id for r in records if r.disposition is FrameDisposition.INFERRED)
    assert set(inferred) == set(CAMERAS)  # 카메라를 돌아가며 넘기므로 어느 카메라도 굶지 않는다


def test_multiplexed_reorder_within_window_is_not_dropped():
    """다중화 연결의 이웃 프레임 역순 도착은 보간 창 안이므로 정렬해 쓴다(명세 12.7). 손실로 세지 않는다."""
    sent, ingest, records, _ = run(SCENARIOS["multiplexed-out-of-order"])
    late = [r for r in records if r.reason is SkipReason.LATE_ARRIVAL]
    # epoch 첫 프레임보다 늦게 온 앞 번호만 남는다. epoch 가 몇 번부터 시작하는지는 명세에 없어 가정하지 않는다.
    assert all(r.frame_sequence == 0 for r in late)
    swapped = sum(
        1 for a, b in pairwise(e[2] for e in sent.events if isinstance(e[2], I0Frame))
        if a.header.camera_id == b.header.camera_id and a.header.key > b.header.key
    )
    assert swapped > len(late)  # 역순 도착이 실제로 있었고 대부분 정렬해 썼다
    counted = sum(s.network_loss for s in ingest.log.stats.values())
    assert counted <= len(sent.dropped)
