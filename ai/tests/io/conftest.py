"""io 시험 공용 픽스처. I0 메시지를 짧게 만드는 함수와 주입 시계를 준다."""

import pytest

from simai_ai.io import FakeDecoder, FreshnessConfig, I0CodecInfo, I0Frame, I0FrameHeader, I0Ingest
from simai_ai.io.decoder import fake_payload
from simai_ai.risk.clock import FixedClock

T0 = 1760342400000


def i0_frame(cam="cam1", epoch=0, seq=0, t=None, keyframe=False, ts="RECEIVE_ESTIMATED", **kw):
    """헤더와 같은 내용을 담은 FakeDecoder 용 압축 프레임. t 를 주지 않으면 T0 + 40 * seq."""
    t = T0 + 40 * seq if t is None else t
    header = I0FrameHeader(
        camera_id=cam,
        stream_epoch=epoch,
        frame_sequence=seq,
        estimated_capture_time_ms=t,
        timestamp_source=ts,
        keyframe=keyframe,
        timestamp_uncertainty_ms=kw.pop("timestamp_uncertainty_ms", 18),
        **kw,
    )
    return I0Frame(header, fake_payload(cam, epoch, seq, t))


def codec(cam="cam1", epoch=0):
    return I0CodecInfo(camera_id=cam, stream_epoch=epoch, codec="h264", pts_timebase="1/90000", sps=b"S", pps=b"P")


@pytest.fixture
def clock():
    return FixedClock(T0)


@pytest.fixture
def make_ingest(clock):
    """I0Ingest 를 만든다. 디코더는 카메라별 FakeDecoder 이고 ingest.decoders 로 꺼내 본다."""

    def _make(cameras=("cam1",), config=FreshnessConfig(), **decoder_kw):
        decoders = {}

        def factory(cam):
            decoders[cam] = FakeDecoder(**decoder_kw)
            return decoders[cam]

        records = []
        ingest = I0Ingest(cameras, factory, clock, config, sink=records.append)
        ingest.decoders = decoders
        ingest.records = records
        return ingest

    return _make


@pytest.fixture
def i0():
    """i0.frame(...), i0.codec(...), i0.T0. 시험 파일에서 conftest 를 import 하지 않으려고 픽스처로 준다."""

    class _I0:
        frame = staticmethod(i0_frame)
        codec = staticmethod(codec)

    _I0.T0 = T0
    return _I0
