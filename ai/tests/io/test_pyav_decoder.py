"""PyAV H.264 디코더 (S15P31A101-182): 실제 H.264 바이트(시험 안에서 만듦)로 열쇠 묶기·키프레임·실패·NVDEC 자리를 본다.

그림에 새긴 번호와 디코더가 돌려준 열쇠가 맞는지로 결합 오류(G3)를 센다. NVDEC 시험은 SIMAI_TEST_GPU=1 일 때만 돌고(Pod 확인용), CUDA 장치가 없으면 건너뛴다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "tier0"))

import live_video as lv  # noqa: E402

from simai_ai.io import (  # noqa: E402
    DecodeError,
    DecoderUnavailable,
    FreshnessConfig,
    I0CodecInfo,
    I0Frame,
    I0FrameHeader,
    I0Ingest,
    NvdecDecoder,
    PyAvDecoder,
    SkipReason,
)
from simai_ai.risk.clock import FixedClock  # noqa: E402

T0 = 1_760_342_400_000


@pytest.fixture(scope="module")
def video():
    return lv.encode(list(range(500, 540)), gop=10)


def _codec(enc, epoch=0, cam="cam1") -> I0CodecInfo:
    return I0CodecInfo(camera_id=cam, stream_epoch=epoch, codec="H264", pts_timebase="1/90000", sps=enc.sps, pps=enc.pps)


def _feed(dec, enc, seqs):
    out, errors = [], []
    for s in seqs:
        au, key = enc.frames[s]
        try:
            out += dec.decode(au, (0, s), key)
        except DecodeError:
            errors.append(s)
    return out, errors


def test_cpu_decoder_binds_each_picture_to_its_own_key(video) -> None:
    dec = PyAvDecoder()
    dec.configure(_codec(video))
    pictures, errors = _feed(dec, video, range(40))
    pictures += dec.flush()
    assert errors == []
    assert [p.key for p in pictures] == [(0, s) for s in range(40)]
    assert all(lv.read_index(p.image) == 500 + p.key[1] for p in pictures)  # 그림 내용 = 열쇠(결합 오류 0)
    assert pictures[0].image.shape == (lv.SIZE[1], lv.SIZE[0], 3)
    # flush 뒤에도 같은 코덱으로 다시 쓸 수 있다(키프레임부터)
    again, errors = _feed(dec, video, [20, 21])
    assert errors == [] and [p.key for p in again] == [(0, 20), (0, 21)]


def test_codec_info_sps_pps_are_required_and_used(video) -> None:
    """접근 단위에는 SPS·PPS 가 없다(카메라가 다시 싣지 않는 경우). codec_info 의 값을 쓰지 않으면 디코딩할 수 없다."""
    dec = PyAvDecoder()
    dec.configure(I0CodecInfo(camera_id="cam1", stream_epoch=0, codec="H264"))  # SPS·PPS 없음
    _, errors = _feed(dec, video, [0])
    assert errors == [0]


def test_reference_frame_before_keyframe_and_garbage_raise_decode_error(video) -> None:
    dec = PyAvDecoder()
    dec.configure(_codec(video))
    _, errors = _feed(dec, video, [1])  # 키프레임 없이 시작
    assert errors == [1]
    with pytest.raises(DecodeError):
        dec.decode(lv.START + bytes([0x65]) + os.urandom(64), (0, 2), True)
    with pytest.raises(DecodeError):
        dec.decode(video.frames[0][0][:20], (0, 3), True)  # 잘린 키프레임: 메운 그림을 내지 않는다
    assert dec.unmatched == 0


def test_real_b_frames_reorder_output_but_each_picture_keeps_its_own_key() -> None:
    """리뷰 부록 B: 실제 B 프레임(bf=3) H.264. 디코딩 순서와 표시 순서가 달라 출력 열쇠가 단조가 아니어도, 그림마다 그 그림을
    만든 입력의 열쇠가 붙는다(결합 오류 0, 모르는 그림 0)."""
    av = pytest.importorskip("av")
    from fractions import Fraction

    enc = av.CodecContext.create("libx264", "w")
    enc.width, enc.height = lv.SIZE
    enc.pix_fmt = "yuv420p"
    enc.time_base = Fraction(1, 25)
    enc.framerate = Fraction(25)
    enc.options = {"g": "12", "keyint_min": "12", "bf": "3", "preset": "medium", "sc_threshold": "0", "crf": "12",
                   "x264-params": "b-adapt=0"}
    packets = []
    for n in range(40):
        f = av.VideoFrame.from_ndarray(lv._yuv(500 + n, lv.SIZE), format="yuv420p")
        f.pts = n
        f.time_base = enc.time_base
        packets.extend(enc.encode(f))
    packets.extend(enc.encode(None))
    nals = lv._nals(bytes(packets[0]))
    sps = next(n for n in nals if n[0] & 31 == 7)
    pps = next(n for n in nals if n[0] & 31 == 8)
    dec = PyAvDecoder()
    dec.configure(I0CodecInfo(camera_id="cam1", stream_epoch=3, codec="H264", sps=sps, pps=pps))
    expected, pictures = {}, []
    for seq, p in enumerate(packets):
        expected[(3, seq)] = 500 + p.pts  # 이 압축 프레임이 그린 그림의 번호
        payload = b"".join(lv.START + n for n in lv._nals(bytes(p)) if n[0] & 31 not in (6, 7, 8))
        pictures += dec.decode(payload, (3, seq), bool(p.is_keyframe))
    pictures += dec.flush()
    assert [p.pts for p in packets[:5]] != sorted(p.pts for p in packets[:5])  # 실제로 재정렬이 일어나는 영상
    assert any(a.key > b.key for a, b in zip(pictures, pictures[1:]))  # 출력 열쇠가 단조가 아니다
    assert len(pictures) == 40 and dec.unmatched == 0
    assert [(p.key, lv.read_index(p.image)) for p in pictures if lv.read_index(p.image) != expected[p.key]] == []


def test_unconfigured_or_non_h264_decoder_fails_every_frame_without_raising_on_configure(video) -> None:
    dec = PyAvDecoder()
    with pytest.raises(DecodeError):
        dec.decode(video.frames[0][0], (0, 0), True)
    dec.configure(I0CodecInfo(camera_id="cam1", stream_epoch=0, codec="h265"))  # 예외 없이 들고 있다가
    with pytest.raises(DecodeError):
        dec.decode(video.frames[0][0], (0, 0), True)  # decode 마다 실패


def test_unknown_hwaccel_is_unavailable_not_silent_cpu() -> None:
    with pytest.raises(DecoderUnavailable):
        PyAvDecoder(hwaccel="no-such-device")


@pytest.mark.gpu
@pytest.mark.skipif(os.environ.get("SIMAI_TEST_GPU") != "1",
                    reason="GPU 시험은 SIMAI_TEST_GPU=1 일 때만 돈다(기본 실행은 GPU 를 쓰지 않는다)")
def test_nvdec_decodes_same_pictures_when_cuda_is_present(video) -> None:
    try:
        dec = NvdecDecoder()
    except DecoderUnavailable as e:
        pytest.skip(f"NVDEC 없음: {e}")
    dec.configure(_codec(video))
    pictures, errors = _feed(dec, video, range(40))
    pictures += dec.flush()
    assert errors == [] and [p.key for p in pictures] == [(0, s) for s in range(40)]
    assert all(lv.read_index(p.image) == 500 + p.key[1] for p in pictures)


@pytest.mark.gpu
@pytest.mark.skipif(os.environ.get("SIMAI_TEST_GPU") != "1",
                    reason="GPU 시험은 SIMAI_TEST_GPU=1 일 때만 돈다(기본 실행은 GPU 를 쓰지 않는다)")
def test_nvdec_bgr_matches_cpu_bgr(video) -> None:
    """-212: NVDEC(nv12 → BGR, 디코더마다 하나인 변환 문맥)이 CPU(yuv420p → BGR)와 같은 그림을 낸다. H.264 디코딩은 비트 단위로
    같고 변환 경로만 달라 반올림 차이(≤ 2)만 허용한다."""
    import numpy as np

    try:
        nv = NvdecDecoder()
    except DecoderUnavailable as e:
        pytest.skip(f"NVDEC 없음: {e}")
    cpu = PyAvDecoder()
    nv.configure(_codec(video))
    cpu.configure(_codec(video))
    a, _ = _feed(nv, video, range(40))
    b, _ = _feed(cpu, video, range(40))
    a, b = a + nv.flush(), b + cpu.flush()
    assert [p.key for p in a] == [p.key for p in b] and len(a) == 40
    assert max(int(np.abs(x.image.astype(int) - y.image.astype(int)).max()) for x, y in zip(a, b)) <= 2


# ---------------------------------------------------------------------------- 색 변환 문맥 (-212)


@pytest.fixture
def spy_reformatter(monkeypatch) -> list:
    """VideoReformatter 대역: 실제 변환을 하되 만든 문맥과 받은 인자를 남긴다. 돌려주는 목록에 만든 문맥이 쌓인다."""
    import av.video.reformatter as rf

    real, made = rf.VideoReformatter, []

    class Spy:
        def __init__(self) -> None:
            self._real, self.calls = real(), []
            made.append(self)

        def reformat(self, frame, **kwargs):
            self.calls.append(kwargs)
            return self._real.reformat(frame, **kwargs)

    monkeypatch.setattr(rf, "VideoReformatter", Spy)
    return made


def test_one_converter_per_configure_is_reused_for_every_picture(video, spy_reformatter) -> None:
    """반례(-212 원인): 그림마다 swscale 문맥을 새로 만들면(frame.to_ndarray(format=...)) 변환 초기화를 매 그림 치른다.
    configure 한 번에 문맥 하나, 모든 그림이 그것을 쓰고 threads 는 묶은 값으로 넘어간다. 다시 configure 하면 새로 만든다."""
    dec = PyAvDecoder(convert_threads=3)
    dec.configure(_codec(video))
    pictures, errors = _feed(dec, video, range(12))
    assert errors == [] and len(pictures) == 12
    assert len(spy_reformatter) == 1
    assert [c["threads"] for c in spy_reformatter[0].calls] == [3] * 12
    assert all(c["format"] == "bgr24" for c in spy_reformatter[0].calls)
    dec.configure(_codec(video, epoch=1))
    assert len(spy_reformatter) == 2


def test_pictures_kept_by_caller_are_not_overwritten_by_later_conversions(video) -> None:
    """반례: 변환 문맥을 다시 써도 출력 버퍼까지 다시 쓰면 대기열에 남은 이전 그림이 바뀐다(관측이 다른 프레임 내용이 된다)."""
    dec = PyAvDecoder()
    dec.configure(_codec(video))
    kept = []
    for s in range(40):
        au, key = video.frames[s]
        for p in dec.decode(au, (0, s), key):
            kept.append((p, p.image.copy()))
    assert len(kept) == 40
    assert all((p.image == snap).all() and lv.read_index(p.image) == 500 + p.key[1] for p, snap in kept)


def test_in_band_resolution_change_reuses_converter_with_new_size(video) -> None:
    """경계: configure 없이 스트림 안에서 SPS 가 바뀌어 크기가 달라져도(카메라 설정 변경) 같은 변환 문맥이 새 크기로 낸다."""
    other = lv.encode(list(range(700, 712)), gop=6, size=(320, 176))
    inline = b"".join(lv.START + n for n in (other.sps, other.pps))
    dec = PyAvDecoder()
    dec.configure(_codec(video))
    first, errors = _feed(dec, video, range(10))
    out = []
    for i, (au, key) in enumerate(other.frames):
        payload = inline + au if key else au  # 시작은 키프레임, 이 디코더의 configure 접두(이전 SPS)는 붙이지 않는다
        out += dec.decode(payload, (0, 100 + i), False)
    out += dec.flush()
    assert errors == [] and len(first) == 10 and first[-1].image.shape == (lv.SIZE[1], lv.SIZE[0], 3)
    assert [p.key for p in out] == [(0, 100 + i) for i in range(12)]
    assert all(p.image.shape == (176, 320, 3) and lv.read_index(p.image) == 600 + p.key[1] for p in out)


def test_decoders_in_parallel_threads_never_cross_bind() -> None:
    """동시성: LIVE 는 카메라마다 수신·디코딩 스레드가 따로 돈다. 디코더마다 변환 문맥이 따로라 그림이 섞이지 않는다."""
    import threading

    streams = [lv.encode(list(range(1000 * (c + 1), 1000 * (c + 1) + 30)), gop=10) for c in range(4)]
    results: dict[int, list] = {}
    errors: list[BaseException] = []

    def run(c: int) -> None:
        try:
            dec = PyAvDecoder()
            dec.configure(_codec(streams[c], cam=f"cam{c + 1}"))
            pics, errs = _feed(dec, streams[c], range(30))
            assert errs == []
            results[c] = pics + dec.flush()
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run, args=(c,)) for c in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    for c in range(4):
        assert [lv.read_index(p.image) for p in results[c]] == [1000 * (c + 1) + p.key[1] for p in results[c]]
        assert len(results[c]) == 30


def test_convert_threads_must_not_be_negative() -> None:
    with pytest.raises(ValueError):
        PyAvDecoder(convert_threads=-1)
    PyAvDecoder(convert_threads=0)  # 0 은 FFmpeg 자동(경계값 허용)


# ---------------------------------------------------------------------------- CameraIngest + 실제 디코더


def _frame(enc, seq, *, epoch=0, cam="cam1", index_seq=None) -> I0Frame:
    s = seq if index_seq is None else index_seq
    au, key = enc.frames[s]
    return I0Frame(I0FrameHeader(camera_id=cam, stream_epoch=epoch, frame_sequence=seq,
                                 estimated_capture_time_ms=T0 + 40 * seq, timestamp_source="RECEIVE_ESTIMATED",
                                 keyframe=key, timestamp_uncertainty_ms=18), au)


def _ingest(depth=2) -> tuple[I0Ingest, FixedClock]:
    clock = FixedClock(T0)
    cfg = FreshnessConfig(queue_depth=depth, max_queue_wait_ms=150, reorder_window_ms=0)
    return I0Ingest(["cam1"], lambda _c: PyAvDecoder(), clock, cfg), clock


def test_repeated_decode_failures_wait_for_next_good_keyframe_and_never_misbind(video) -> None:
    """디코딩 실패 연속: 깨진 키프레임이 이어져도 그림을 내지 않고, 다음 정상 키프레임부터 제 시각으로 낸다."""
    ingest, clock = _ingest(depth=1)
    ingest.receive(_codec(video))
    broken = I0Frame(I0FrameHeader(camera_id="cam1", stream_epoch=0, frame_sequence=0,
                                   estimated_capture_time_ms=T0, timestamp_source="RECEIVE_ESTIMATED", keyframe=True,
                                   timestamp_uncertainty_ms=18), lv.START + bytes([0x65]) + os.urandom(80))
    ingest.receive(broken)
    for seq in range(1, 10):  # 깨진 키프레임 뒤 참조 프레임: 키프레임 대기로 버림
        ingest.receive(_frame(video, seq))
    assert ingest.next_batch() == []
    broken2 = I0Frame(I0FrameHeader(camera_id="cam1", stream_epoch=0, frame_sequence=10,
                                    estimated_capture_time_ms=T0 + 400, timestamp_source="RECEIVE_ESTIMATED",
                                    keyframe=True, timestamp_uncertainty_ms=18), video.frames[10][0][:24])
    ingest.receive(broken2)
    for seq in range(11, 20):
        ingest.receive(_frame(video, seq))
    assert ingest.next_batch() == [] and not ingest.camera("cam1").ready
    for seq in range(20, 23):
        ingest.receive(_frame(video, seq))
    [ticket] = ingest.next_batch()
    f = ticket.frame
    assert f.frame_ref["frame_sequence"] == 22 and f.estimated_capture_time == T0 + 40 * 22
    assert lv.read_index(f.image) == 522
    counts = ingest.log.counts("cam1")
    assert counts["decode_failed"] == 2
    assert counts["skipped_by_reason"][str(SkipReason.WAITING_FOR_KEYFRAME)] == 18


def test_codec_change_mid_stream_drops_old_frames_and_restarts_at_new_keyframe(video) -> None:
    """같은 epoch 에서 코덱(해상도)이 바뀌면 대기 중인 이전 코덱 그림을 버리고 새 키프레임부터 새 크기로 낸다."""
    other = lv.encode(list(range(700, 720)), gop=10, size=(320, 176))
    ingest, clock = _ingest(depth=2)
    ingest.receive(_codec(video))
    for seq in range(0, 5):
        ingest.receive(_frame(video, seq))
    assert ingest.camera("cam1").ready
    ingest.receive(_codec(other))  # 코덱 변경: 대기열의 이전 그림(seq 3·4)은 STREAM_RESET
    assert ingest.next_batch() == [] and not ingest.camera("cam1").ready
    # 새 코덱의 키프레임 전 프레임(번호는 이어짐): 버림
    for seq, idx in ((5, 1), (6, 2)):
        au, _ = other.frames[idx]
        ingest.receive(I0Frame(I0FrameHeader(camera_id="cam1", stream_epoch=0, frame_sequence=seq,
                                             estimated_capture_time_ms=T0 + 40 * seq,
                                             timestamp_source="RECEIVE_ESTIMATED", keyframe=False,
                                             timestamp_uncertainty_ms=18), au))
    au, key = other.frames[10]
    assert key
    ingest.receive(I0Frame(I0FrameHeader(camera_id="cam1", stream_epoch=0, frame_sequence=7,
                                         estimated_capture_time_ms=T0 + 280, timestamp_source="RECEIVE_ESTIMATED",
                                         keyframe=True, timestamp_uncertainty_ms=18), au))
    [ticket] = ingest.next_batch()
    assert ticket.frame.frame_ref["frame_sequence"] == 7 and ticket.frame.estimated_capture_time == T0 + 280
    assert ticket.frame.image.shape == (176, 320, 3) and lv.read_index(ticket.frame.image) == 710
    reasons = ingest.log.counts("cam1")["skipped_by_reason"]
    assert reasons[str(SkipReason.STREAM_RESET)] >= 1 and reasons[str(SkipReason.WAITING_FOR_KEYFRAME)] == 2
