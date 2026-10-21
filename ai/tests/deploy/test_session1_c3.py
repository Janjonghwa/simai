"""c3 디코딩 처리량(deploy/scripts/session1_live.py) 시험 (S15P31A101-212). GPU 없이 CPU 디코더를 NVDEC 자리에 넣어 돈다.

- 드라이런: 판정 측정 + 변형(카메라별 스레드, 변환 스레드 0·1, nv12) 값이 모두 남고 넣은 수 = 나온 수
- 반례: 빠르더라도 그림을 덜 내면 FAIL(모르면 통과로 보지 않음)
- 반례: 병렬 측정에서 한 카메라 스레드가 실패하면 예외가 올라온다(덜 나온 그림을 합계로 세지 않음)
- 반례: 비교용 변형이 실패해도 판정은 그대로이고 실패가 값에 남는다
- 경계: 기준 p95 10 ms·100 fps 는 같으면 통과, 넘으면 실패
"""

from __future__ import annotations

import pytest

pytest.importorskip("av")

import simai_ai.io as sio  # noqa: E402
from simai_ai.io import DecodeError, PyAvDecoder  # noqa: E402

SMALL = (256, 144)
VARIANTS = ("nvdec_par", "cpu_par", "nvdec_thr0", "nvdec_thr1", "nvdec_nv12")


def cpu_as_nvdec(**kwargs):
    return PyAvDecoder(**kwargs)


def test_c3_dry_run_records_judged_and_variant_values(live, monkeypatch):
    monkeypatch.setattr(sio, "NvdecDecoder", cpu_as_nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    assert values["nvdec_frames_in"] == values["nvdec_pictures_out"] == 4 * 20
    for name in VARIANTS:
        assert f"{name}_error" not in values, values.get(f"{name}_error")
        assert values[f"{name}_frames_in"] == values[f"{name}_pictures_out"] == 4 * 10
        assert values[f"{name}_total_fps"] > 0


def test_fast_decoder_that_loses_pictures_fails(live, monkeypatch):
    class Lossy(PyAvDecoder):
        def decode(self, payload, key, keyframe):
            pics = super().decode(payload, key, keyframe)
            return [] if key[1] == 7 else pics  # 한 장을 조용히 잃는다

    monkeypatch.setattr(sio, "NvdecDecoder", Lossy)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "FAIL" and values["nvdec_pictures_out"] == values["nvdec_frames_in"] - 4, detail


def test_parallel_bench_raises_when_one_camera_thread_fails(live):
    enc = live.encode_stream(10, size=SMALL)
    made = []

    def make():
        made.append(None)
        if len(made) != 3:
            return PyAvDecoder()

        class Broken(PyAvDecoder):
            def decode(self, payload, key, keyframe):
                if key[1] == 4:
                    raise DecodeError("cam3 깨짐")
                return super().decode(payload, key, keyframe)

        return Broken()

    with pytest.raises(DecodeError):
        live.bench_decoders(make, enc, parallel=True)
    ok = live.bench_decoders(PyAvDecoder, enc, parallel=True)
    assert ok["pictures_out"] == ok["frames_in"] == 40


def test_failing_variant_is_recorded_and_does_not_change_judgement(live, monkeypatch):
    def nvdec(**kwargs):
        if kwargs.get("output_format") == "nv12":
            raise sio.DecoderUnavailable("nv12 변형 실패 흉내")
        return PyAvDecoder(**kwargs)

    monkeypatch.setattr(sio, "NvdecDecoder", nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    assert "nv12 변형 실패 흉내" in values["nvdec_nv12_error"] and "nvdec_nv12_total_fps" not in values
    assert values["nvdec_thr0_pictures_out"] == 40  # 다른 변형은 그대로 돈다


@pytest.mark.parametrize(("p95", "fps", "expected"), [
    (10.0, 100.0, "PASS"),
    (10.001, 150.0, "FAIL"),
    (5.0, 99.9, "FAIL"),
    (None, 150.0, "FAIL"),  # 잰 값이 없으면 통과로 보지 않는다
])
def test_judgement_boundaries(live, monkeypatch, p95, fps, expected):
    def fake_bench(make, enc, cams=4, parallel=False):
        return {"frames_in": 8, "pictures_out": 8, "per_frame_ms_p50": p95, "per_frame_ms_p95": p95,
                "total_fps": fps, "seconds": 0.1}

    monkeypatch.setattr(live, "bench_decoders", fake_bench)
    monkeypatch.setattr(live, "encode_stream", lambda frames, size=None: live_encoded(frames))
    status, _, _ = live.c3_decode_throughput(seconds=0.4, variant_seconds=0.2, size=SMALL)
    assert status == expected


def live_encoded(frames: int):
    import live_video as lv  # session1_live 가 tests/services/tier0 를 sys.path 에 넣는다

    return lv.Encoded(b"", b"", SMALL, tuple((b"", i == 0) for i in range(frames)))
