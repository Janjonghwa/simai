"""디코더 인터페이스와 시험용 FakeDecoder (GPU 없음)."""

from dataclasses import FrozenInstanceError
import gc
import weakref

import pytest

from simai_ai.io import DecodedPicture, DecodeError, FakeDecoder, FakeImage, I0CodecInfo
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


def test_decoded_picture_lazy_loader_runs_once_and_releases_closure():
    """-386: DecodedPicture(key, loader=...) 는 .image 를 처음 읽을 때만 변환하고, 변환 뒤에는 클로저(원본 YUV 프레임) 참조를 놓는다."""

    class _RawFrame:
        pass

    raw = _RawFrame()
    raw_ref = weakref.ref(raw)
    calls = 0

    def _make(frame):
        def _load():
            nonlocal calls
            calls += 1
            return ("bgr", frame is not None)

        return _load

    pic = DecodedPicture((0, 7), loader=_make(raw))
    del raw
    assert not pic.converted and calls == 0
    assert "converted=False" in repr(pic)
    assert pic.image == ("bgr", True)
    assert pic.converted and calls == 1
    assert pic.image == ("bgr", True) and calls == 1  # 두 번째 읽기는 캐시된 값을 돌려준다
    gc.collect()
    assert raw_ref() is None  # loader 가 풀려 원본 프레임 참조가 남지 않는다


def test_decoded_picture_validation_and_immutability():
    with pytest.raises(ValueError):
        DecodedPicture((0, 1))
    with pytest.raises(ValueError):
        DecodedPicture((0, 1), "img", loader=lambda: "img")
    pic = DecodedPicture((0, 1), "img")
    assert pic.converted and pic.image == "img"
    with pytest.raises(FrozenInstanceError):
        pic.key = (0, 2)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        pic.image = "other"  # type: ignore[misc]
