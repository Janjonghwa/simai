from simai_recorder.annexb import (
    first_vcl_type,
    has_parameter_sets,
    parameter_sets,
    parameter_sets_from_avcc,
    split_annexb,
)

SPS = bytes([0x67, 0x64, 0x00, 0x1F, 0xAC])
PPS = bytes([0x68, 0xEE, 0x3C, 0x80])
IDR = bytes([0x65, 0x88, 0x84, 0x00, 0x10])


def test_split_handles_3_and_4_byte_start_codes():
    data = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x01" + PPS + b"\x00\x00\x00\x01" + IDR
    assert split_annexb(data) == [SPS, PPS, IDR]


def test_parameter_sets_prefer_keyframe_then_extradata():
    key_with = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS + b"\x00\x00\x00\x01" + IDR
    assert parameter_sets(key_with, None) == (SPS, PPS)
    assert has_parameter_sets(key_with)
    key_without = b"\x00\x00\x00\x01" + IDR
    assert not has_parameter_sets(key_without)
    extradata = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS
    assert parameter_sets(key_without, extradata) == (SPS, PPS)
    assert parameter_sets(key_without, None) == (None, None)


def test_avcc_extradata():
    avcc = bytes([1, 0x64, 0, 0x1F, 0xFF, 0xE1]) + len(SPS).to_bytes(2, "big") + SPS + bytes([1]) + len(PPS).to_bytes(
        2, "big"
    ) + PPS
    assert parameter_sets_from_avcc(avcc) == (SPS, PPS)
    assert parameter_sets(b"\x00\x00\x00\x01" + IDR, avcc) == (SPS, PPS)


def test_first_vcl_type_skips_parameter_sets():
    key = b"\x00\x00\x00\x01" + SPS + b"\x00\x00\x00\x01" + PPS + b"\x00\x00\x01" + IDR
    assert first_vcl_type(key) == 5
    assert first_vcl_type(b"\x00\x00\x00\x01" + bytes([0x41, 0x9A])) == 1
    assert first_vcl_type(b"\x00\x00\x00\x01" + SPS) is None
