"""c3 디코딩 처리량(deploy/scripts/session1_live.py) 시험 (S15P31A101-212, 판정 CPU 카메라별 스레드 S15P31A101-236).

GPU 없이 돈다. NVDEC 참고 측정은 CPU 디코더를 NVDEC 자리에 넣거나, NVDEC 이 없는 노트북 그대로 돈다.
- 드라이런: 판정(CPU 카메라별 스레드, 10초 전체) + 참고 측정(CPU 한 스레드, NVDEC 들) 값이 모두 남고 넣은 수 = 나온 수
- 판정은 CPU 다: NVDEC 이 없어도(열리지 않아도) 판정은 그대로이고 실패는 참고 값(nvdec_error)에 남는다
- 반례: 빠르더라도 CPU 가 그림을 덜 내면 FAIL(모르면 통과로 보지 않음). NVDEC 이 그림을 덜 내도 판정은 바뀌지 않는다
- 반례: 병렬 측정에서 한 카메라 스레드가 실패하면 예외가 올라온다(덜 나온 그림을 합계로 세지 않음)
- 반례: 참고 측정이 실패해도 판정은 그대로이고 실패가 값에 남는다
- 경계: 기준 p95 10 ms·100 fps 는 같으면 통과, 넘으면 실패. 판정 측정이 카메라별 스레드(parallel=True)인지
- c0: cgroup 할당량 해석(v2 quota·max, v1, 깨진 값), 자기 그룹 경로 찾기(v1 공동 마운트 cpu,cpuacct·v2 하위 그룹·컨테이너 root·
  조상 한도), 못 읽으면 지어내지 않고 출처(cgroup/affinity/host)를 남김. 호스트 CPU 수를 할당량으로 착각하지 않음
"""

from __future__ import annotations

import pytest

pytest.importorskip("av")

import simai_ai.io as sio  # noqa: E402
from simai_ai.io import DecodeError, PyAvDecoder  # noqa: E402

SMALL = (256, 144)
REFERENCES = ("nvdec_par", "nvdec_thr0", "nvdec_thr1", "nvdec_nv12")


def cpu_as_nvdec(**kwargs):
    return PyAvDecoder(**kwargs)


def test_c3_dry_run_records_judged_and_reference_values(live, monkeypatch):
    monkeypatch.setattr(sio, "NvdecDecoder", cpu_as_nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    assert values["cpu_par_frames_in"] == values["cpu_par_pictures_out"] == 4 * 20  # 판정은 10초 전체(앞 5초 아님)
    assert values["cpu_frames_in"] == values["nvdec_frames_in"] == 4 * 20
    for name in REFERENCES:
        assert f"{name}_error" not in values, values.get(f"{name}_error")
        assert values[f"{name}_frames_in"] == values[f"{name}_pictures_out"] == 4 * 10
        assert values[f"{name}_total_fps"] > 0


def test_missing_nvdec_does_not_change_cpu_judgement(live, monkeypatch):
    def no_nvdec(**_kwargs):
        raise sio.DecoderUnavailable("hwaccel 'cuda' 장치를 열지 못했다")

    monkeypatch.setattr(sio, "NvdecDecoder", no_nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    for name in ("nvdec", *REFERENCES):
        assert "DecoderUnavailable" in values[f"{name}_error"] and f"{name}_total_fps" not in values
    assert "DecoderUnavailable" in detail  # 참고 NVDEC 값 대신 실패 이유가 요약에 남는다


def test_cpu_decoder_that_loses_pictures_fails(live, monkeypatch):
    class Lossy(PyAvDecoder):
        def decode(self, payload, key, keyframe):
            pics = super().decode(payload, key, keyframe)
            return [] if key[1] == 7 else pics  # 한 장을 조용히 잃는다

    monkeypatch.setattr(sio, "PyAvDecoder", Lossy)
    monkeypatch.setattr(sio, "NvdecDecoder", cpu_as_nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "FAIL" and values["cpu_par_pictures_out"] == values["cpu_par_frames_in"] - 4, detail


def test_lossy_nvdec_reference_does_not_change_judgement(live, monkeypatch):
    class Lossy(PyAvDecoder):
        def decode(self, payload, key, keyframe):
            pics = super().decode(payload, key, keyframe)
            return [] if key[1] == 7 else pics

    monkeypatch.setattr(sio, "NvdecDecoder", Lossy)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    assert values["nvdec_pictures_out"] == values["nvdec_frames_in"] - 4  # 참고 값에는 그대로 드러난다


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


def test_failing_reference_is_recorded_and_does_not_change_judgement(live, monkeypatch):
    def nvdec(**kwargs):
        if kwargs.get("output_format") == "nv12":
            raise sio.DecoderUnavailable("nv12 변형 실패 흉내")
        return PyAvDecoder(**kwargs)

    monkeypatch.setattr(sio, "NvdecDecoder", nvdec)
    status, detail, values = live.c3_decode_throughput(seconds=0.8, variant_seconds=0.4, size=SMALL)
    assert status == "PASS", detail
    assert "nv12 변형 실패 흉내" in values["nvdec_nv12_error"] and "nvdec_nv12_total_fps" not in values
    assert values["nvdec_thr0_pictures_out"] == 40  # 다른 참고 측정은 그대로 돈다


@pytest.mark.parametrize(("p95", "fps", "expected"), [
    (10.0, 100.0, "PASS"),
    (10.001, 150.0, "FAIL"),
    (5.0, 99.9, "FAIL"),
    (None, 150.0, "FAIL"),  # 잰 값이 없으면 통과로 보지 않는다
    (5.0, None, "FAIL"),
])
def test_judgement_boundaries_use_cpu_per_camera_threads(live, monkeypatch, p95, fps, expected):
    calls = []

    def fake_bench(make, enc, cams=4, parallel=False):
        calls.append((make, parallel))
        if make is sio.PyAvDecoder and parallel:  # 판정 측정만 기준값을 낸다
            return {"frames_in": 8, "pictures_out": 8, "per_frame_ms_p50": p95, "per_frame_ms_p95": p95,
                    "total_fps": fps, "seconds": 0.1}
        return {"frames_in": 8, "pictures_out": 8, "per_frame_ms_p50": 1.0, "per_frame_ms_p95": 1.0,
                "total_fps": 1000.0, "seconds": 0.1}  # 참고 측정이 아무리 좋아도 판정에 들어가지 않는다

    monkeypatch.setattr(live, "bench_decoders", fake_bench)
    monkeypatch.setattr(live, "encode_stream", lambda frames, size=None: live_encoded(frames))
    status, _, _ = live.c3_decode_throughput(seconds=0.4, variant_seconds=0.2, size=SMALL)
    assert status == expected
    assert calls[0] == (sio.PyAvDecoder, True)  # 판정은 첫 측정: CPU 카메라별 스레드


@pytest.mark.parametrize(("doc", "expected"), [
    ({"frames_in": 0, "pictures_out": 0, "per_frame_ms_p95": 1.0, "total_fps": 500.0}, False),  # 아무것도 안 잼
    ({"frames_in": 8, "pictures_out": 9, "per_frame_ms_p95": 1.0, "total_fps": 500.0}, False),  # 넣은 것보다 많이 나옴
    ({"pictures_out": 8, "per_frame_ms_p95": 1.0, "total_fps": 500.0}, False),
    ({"frames_in": 8, "pictures_out": 8, "per_frame_ms_p95": 1.0, "total_fps": 500.0}, True),
])
def test_c3_judge_needs_complete_measurement(live, doc, expected):
    assert live.c3_judge(doc) is expected


@pytest.mark.parametrize(("kwargs", "expected"), [
    ({"cpu_max": "800000 100000\n"}, 8.0),
    ({"cpu_max": "150000 100000"}, 1.5),
    ({"cpu_max": "max 100000\n"}, None),  # 제한 없음: 호스트 값을 할당량으로 쓰지 않는다
    ({"cpu_max": "garbage"}, None),
    ({"cpu_max": "100000 0"}, None),
    ({"quota_us": "400000", "period_us": "100000"}, 4.0),  # cgroup v1
    ({"quota_us": "-1", "period_us": "100000"}, None),  # v1 제한 없음
])
def test_parse_quota(live, kwargs, expected):
    assert live.parse_quota(**kwargs) == expected


V1_MOUNT = "35 25 0:30 {root} /sys/fs/cgroup/cpu,cpuacct rw,nosuid shared:9 - cgroup cgroup rw,cpu,cpuacct\n"
V2_MOUNT = "30 25 0:26 / /sys/fs/cgroup rw,nosuid shared:4 - cgroup2 cgroup2 rw\n"

CGROUP_CASES = {
    # 리뷰어 재현 1: v1 공동 마운트(cpu,cpuacct)의 8 vCPU 가 호스트 224 로 기록되던 경우
    "v1_comount": ({"/proc/self/cgroup": "4:cpu,cpuacct:/\n3:memory:/\n", "/proc/self/mountinfo": V1_MOUNT.format(root="/"),
                    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "800000\n",
                    "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000\n"}, 8.0),
    # 리뷰어 재현 2: v2 기준점은 max, 자기 하위 그룹 workload 가 8 vCPU
    "v2_subgroup": ({"/proc/self/cgroup": "0::/workload\n", "/proc/self/mountinfo": V2_MOUNT,
                     "/sys/fs/cgroup/cpu.max": "max 100000\n", "/sys/fs/cgroup/workload/cpu.max": "800000 100000\n"}, 8.0),
    # mountinfo 를 못 읽어도 흔한 마운트 지점(cpu,cpuacct·cgroup2 기준점)에서 찾는다
    "v1_no_mountinfo": ({"/proc/self/cgroup": "4:cpu,cpuacct:/\n",
                         "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "800000", "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000"},
                        8.0),
    "v2_no_mountinfo": ({"/proc/self/cgroup": "0::/workload\n", "/sys/fs/cgroup/cpu.max": "max 100000",
                         "/sys/fs/cgroup/workload/cpu.max": "800000 100000"}, 8.0),
    # 컨테이너: 그룹 경로가 이름공간 밖 경로(/docker/abc)이고 마운트 root 도 그 경로면 기준점 자체를 읽는다
    "v1_container_root": ({"/proc/self/cgroup": "4:cpu,cpuacct:/docker/abc\n",
                           "/proc/self/mountinfo": V1_MOUNT.format(root="/docker/abc"),
                           "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "400000", "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000"},
                          4.0),
    # 잎은 4 vCPU, 조상이 2 vCPU: 실제 한도는 더 작은 조상 값
    "v2_ancestor_limit": ({"/proc/self/cgroup": "0::/a/b\n", "/proc/self/mountinfo": V2_MOUNT,
                           "/sys/fs/cgroup/a/b/cpu.max": "400000 100000", "/sys/fs/cgroup/a/cpu.max": "200000 100000"}, 2.0),
    # v1 cpu 컨트롤러가 흔하지 않은 지점에 따로 마운트됨: mountinfo 로만 찾는다
    "v1_custom_mount": ({"/proc/self/cgroup": "5:cpu:/\n",
                         "/proc/self/mountinfo": "40 25 0:31 / /cgroups/cpu rw - cgroup cgroup rw,cpu\n",
                         "/cgroups/cpu/cpu.cfs_quota_us": "300000", "/cgroups/cpu/cpu.cfs_period_us": "100000"}, 3.0),
    "v1_unlimited": ({"/proc/self/cgroup": "4:cpu,cpuacct:/\n", "/proc/self/mountinfo": V1_MOUNT.format(root="/"),
                      "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_quota_us": "-1", "/sys/fs/cgroup/cpu,cpuacct/cpu.cfs_period_us": "100000"},
                     None),
    "nothing": ({}, None),
}


@pytest.mark.parametrize("case", sorted(CGROUP_CASES))
def test_c0_finds_own_cgroup_quota(live, monkeypatch, case):
    files, expected = CGROUP_CASES[case]
    monkeypatch.setattr(live.os, "cpu_count", lambda: 224)
    monkeypatch.setattr(live.os, "sched_getaffinity", lambda _pid: set(range(224)), raising=False)
    status, detail, values = live.c0_cpu_env(read=files.get)
    assert status == "PASS" and values["cgroup_vcpu"] == expected, detail
    if expected is None:  # 못 읽음·제한 없음: 할당량을 지어내지 않고 출처를 남긴다
        assert values["cgroup_vcpu_file"] is None and values["usable_vcpu"] == 224
        assert values["usable_vcpu_source"] == "affinity"
    else:
        assert values["usable_vcpu"] == expected and values["usable_vcpu_source"] == "cgroup"
        assert values["cgroup_vcpu_file"] in files
    assert values["host_cpu_count"] == 224


def test_c0_source_falls_back_to_affinity_then_host(live, monkeypatch):
    monkeypatch.setattr(live.os, "cpu_count", lambda: 224)
    monkeypatch.setattr(live.os, "sched_getaffinity", lambda _pid: set(range(6)), raising=False)
    _, _, values = live.c0_cpu_env(read={}.get)
    assert values["usable_vcpu"] == 6 and values["usable_vcpu_source"] == "affinity" and values["cgroup_vcpu"] is None
    monkeypatch.delattr(live.os, "sched_getaffinity", raising=False)
    _, _, values = live.c0_cpu_env(read={"/proc/cpuinfo": "model name\t: Fake CPU\n"}.get)
    assert values["usable_vcpu"] == 224 and values["usable_vcpu_source"] == "host" and values["affinity_cpus"] is None
    assert values["cpu_model"] == "Fake CPU"


def live_encoded(frames: int):
    import live_video as lv  # session1_live 가 tests/services/tier0 를 sys.path 에 넣는다

    return lv.Encoded(b"", b"", SMALL, tuple((b"", i == 0) for i in range(frames)))
