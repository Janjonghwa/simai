"""디코더 인터페이스와 시험용 FakeDecoder (GPU 없음)."""

import pytest

from simai_ai.io import DecodeError, FakeDecoder, FakeImage, I0CodecInfo
from simai_ai.io.decoder import fake_payload

CODEC = I0CodecInfo(camera_id="cam1", stream_epoch=0, codec="h264")


def _feed(dec, seq, keyframe=False):
    return dec.decode(fake_payload("cam1", 0, seq, 1000 + seq), (0, seq), keyframe)


def test_needs_configure_and_keyframe():
    dec = FakeDecoder()
    with pytest.raises(DecodeError):
        _feed(dec, 0, keyframe=True)
    dec.configure(CODEC)
    with pytest.raises(DecodeError):
        _feed(dec, 1)
    out = _feed(dec, 2, keyframe=True)
    assert [p.key for p in out] == [(0, 2)]
    assert out[0].image == FakeImage("cam1", 0, 2, 1002)


def test_delay_and_flush():
    dec = FakeDecoder(delay=2)
    dec.configure(CODEC)
    assert _feed(dec, 0, keyframe=True) == []
    assert _feed(dec, 1) == []
    assert [p.key for p in _feed(dec, 2)] == [(0, 0)]
    assert [p.key for p in dec.flush()] == [(0, 1), (0, 2)]


def test_reorder_pairs_keeps_keys_with_pictures():
    dec = FakeDecoder(reorder_pairs=True)
    dec.configure(CODEC)
    out = _feed(dec, 0, keyframe=True) + _feed(dec, 1)
    assert [p.key for p in out] == [(0, 1), (0, 0)]
    assert all(p.key == (p.image.stream_epoch, p.image.frame_sequence) for p in out)


def test_reset_requires_keyframe_again():
    dec = FakeDecoder(delay=1)
    dec.configure(CODEC)
    _feed(dec, 0, keyframe=True)
    dec.reset()
    assert dec.flush() == []
    with pytest.raises(DecodeError):
        _feed(dec, 1, keyframe=True)  # configure 전
