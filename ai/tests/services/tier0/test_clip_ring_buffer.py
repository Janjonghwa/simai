"""카메라별 압축 패킷 링 버퍼(S15P31A101-57): 60초 경계, 키프레임 정렬, 메모리 상한, epoch·손실 처리, 재다중화."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
for p in (AI_DIR, Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from clip_stream import FRAME_MS, GOP, PPS, SPS, T0, Stream, codec, frame  # noqa: E402

from services.tier0.clips.media import ANNEXB_START, AnnexBMuxer, ClipMuxError  # noqa: E402
from services.tier0.clips.ring_buffer import CameraRing, PacketRingBuffer, RingBufferConfig  # noqa: E402

GOP_MS = GOP * FRAME_MS


def _filled(seconds: int, config: RingBufferConfig = RingBufferConfig()) -> tuple[PacketRingBuffer, Stream]:
    ring = PacketRingBuffer(("cam_A",), config)
    s = Stream(("cam_A",))
    ring.receive_many(s.start())
    ring.receive_many(s.advance(seconds * 1000))
    return ring, s


def test_keeps_only_the_last_60_seconds_from_a_keyframe():
    ring, s = _filled(150)
    cam = ring.camera("cam_A")
    packets = cam.packets()
    newest = s.t_ms - FRAME_MS
    assert packets[0].keyframe
    assert cam.newest_capture_ms == newest
    # 창 시작(newest − 60초)을 덮는 가장 짧은 GOP 묶음: 창보다 앞선 영상은 GOP 하나 미만
    assert newest - 60_000 - GOP_MS < packets[0].capture_time_ms <= newest - 60_000
    assert cam.stats.evicted_by_window > 0
    assert cam.stats.evicted_by_memory == 0


def test_window_boundary_holds_after_each_frame():
    ring = PacketRingBuffer(("cam_A",), RingBufferConfig(window_ms=10_000))
    s = Stream(("cam_A",))
    ring.receive_many(s.start())
    for f in s.advance(30_000):
        ring.receive(f)
        cam = ring.camera("cam_A")
        first, newest = cam.packets()[0], cam.newest_capture_ms
        assert first.keyframe
        if newest - T0 >= 10_000:
            assert newest - 10_000 - GOP_MS < first.capture_time_ms <= newest - 10_000


def test_memory_cap_drops_whole_gops_from_the_front():
    one_gop = sum(len(f.payload) for f in Stream(("cam_A",)).advance(GOP_MS))
    cap = int(one_gop * 5.5)
    ring, _ = _filled(60, RingBufferConfig(max_bytes_per_camera=cap))
    cam = ring.camera("cam_A")
    assert cam.size_bytes <= cap
    assert cam.packets()[0].keyframe
    assert cam.stats.evicted_by_memory > 0
    assert cam.size_bytes == sum(p.size for p in cam.packets())


def test_cut_starts_at_keyframe_before_start_and_stops_at_end():
    ring, _ = _filled(50)
    start, end = T0 + 20_330, T0 + 40_010
    cut = ring.camera("cam_A").cut(start, end)
    packets = cut.packets
    assert packets[0].keyframe
    assert start - GOP_MS < packets[0].capture_time_ms <= start
    assert packets[-1].capture_time_ms <= end < packets[-1].capture_time_ms + FRAME_MS
    assert not cut.truncated_start and not cut.truncated_end
    seqs = [p.frame_sequence for p in packets]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    assert cut.frame_count == len(packets)


def test_cut_reports_truncation_instead_of_inventing_footage():
    ring, s = _filled(30)
    cut = ring.camera("cam_A").cut(T0 - 5_000, s.t_ms + 5_000)
    assert cut.truncated_start and cut.truncated_end
    assert cut.packets[0].keyframe and cut.start_capture_ms == T0
    empty = PacketRingBuffer(("cam_B",)).camera("cam_B").cut(T0, T0 + 1000)
    assert empty.empty and empty.truncated_start and empty.truncated_end


def test_cut_is_a_copy_and_leaves_the_buffer_untouched():
    ring, _ = _filled(10)
    before = ring.camera("cam_A").packets()
    ring.camera("cam_A").cut(T0 + 1000, T0 + 5000)
    assert ring.camera("cam_A").packets() == before


def test_frames_before_codec_info_or_keyframe_are_dropped():
    cam = CameraRing("cam_A", RingBufferConfig())
    assert not cam.on_frame(frame("cam_A", 0, T0))  # 코덱 정보 전
    cam.on_codec_info(codec("cam_A"))
    assert not cam.on_frame(frame("cam_A", 1, T0 + 50))  # 키프레임 전
    assert cam.on_frame(frame("cam_A", 20, T0 + 1000))
    assert cam.stats.dropped_no_codec == 1 and cam.stats.dropped_waiting_keyframe == 1


def test_sequence_gap_waits_for_next_keyframe():
    cam = CameraRing("cam_A", RingBufferConfig())
    cam.on_codec_info(codec("cam_A"))
    for seq in range(0, 10):
        cam.on_frame(frame("cam_A", seq, T0 + seq * FRAME_MS))
    for seq in range(12, 25):  # 10·11 손실 → 20(키프레임)까지 버린다
        cam.on_frame(frame("cam_A", seq, T0 + seq * FRAME_MS))
    seqs = [p.frame_sequence for p in cam.packets()]
    assert seqs == list(range(0, 10)) + list(range(20, 25))
    assert cam.stats.sequence_gaps == 1 and cam.stats.dropped_waiting_keyframe == 8
    assert not cam.on_frame(frame("cam_A", 22, T0 + 2000))  # 늦게 온 프레임
    assert cam.stats.dropped_late == 1


def test_new_epoch_needs_codec_info_and_starts_a_new_segment():
    cam = CameraRing("cam_A", RingBufferConfig())
    cam.on_codec_info(codec("cam_A", 0))
    for seq in range(0, 40):
        cam.on_frame(frame("cam_A", seq, T0 + seq * FRAME_MS))
    t1 = T0 + 40 * FRAME_MS
    assert not cam.on_frame(frame("cam_A", 0, t1, epoch=1))  # 새 epoch 코덱 정보 전
    cam.on_codec_info(codec("cam_A", 1))
    for seq in range(0, 40):
        cam.on_frame(frame("cam_A", seq, t1 + seq * FRAME_MS, epoch=1))
    assert not cam.on_frame(frame("cam_A", 40, t1, epoch=0))  # 끝난 epoch
    cut = cam.cut(T0, t1 + 40 * FRAME_MS)
    assert [s.codec.stream_epoch for s in cut.segments] == [0, 1]
    assert all(s.packets[0].keyframe for s in cut.segments)
    assert cut.stream_epochs == (0, 1)
    assert cam.stats.dropped_stale_epoch == 1


def test_expire_drops_footage_when_stream_stops():
    ring, s = _filled(20)
    ring.expire(s.t_ms + 30_000)
    assert ring.camera("cam_A").size_bytes > 0  # 아직 60초 안
    ring.expire(s.t_ms + 61_000)
    assert ring.camera("cam_A").size_bytes == 0 and ring.camera("cam_A").newest_capture_ms is None


def test_unknown_camera_and_config_validation():
    with pytest.raises(ValueError):
        PacketRingBuffer(("cam_A",)).receive(frame("cam_Z", 0, T0))
    with pytest.raises(ValueError):
        RingBufferConfig(window_ms=0)
    with pytest.raises(ValueError):
        PacketRingBuffer(())


def test_annexb_mux_remuxes_without_decoding():
    ring, _ = _filled(10)
    cut = ring.camera("cam_A").cut(T0 + 2_100, T0 + 4_000)
    muxed = AnnexBMuxer().mux(cut)
    expected = ANNEXB_START + SPS + ANNEXB_START + PPS + b"".join(p.payload for p in cut.packets)
    assert muxed.content == expected
    assert muxed.content_type == "video/h264"
    with pytest.raises(ClipMuxError):
        AnnexBMuxer().mux(ring.camera("cam_A").cut(T0 - 9_000, T0 - 8_000))


def test_repeated_identical_codec_info_does_not_break_the_open_gop():
    cam = CameraRing("cam_A", RingBufferConfig())
    cam.on_codec_info(codec("cam_A"))
    for seq in range(0, 5):
        cam.on_frame(frame("cam_A", seq, T0 + seq * FRAME_MS))
    cam.on_codec_info(codec("cam_A"))  # 같은 내용이 다시 왔다
    assert cam.on_frame(frame("cam_A", 5, T0 + 5 * FRAME_MS))
    assert [p.frame_sequence for p in cam.packets()] == list(range(6))
