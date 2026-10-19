"""메모리 수집(시간 중앙값 배경)과 여러 카메라 실행. 실제 카메라·디스크 영상 없이 가짜 컨테이너와 시험 안에서 만든 영상으로 돈다."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from simai_ai.calibration.intrinsics import (
    METHOD_ANYCALIB,
    METHOD_PLUMB_LINE_VP,
    STATUS_FAILED,
    STATUS_OK,
    CameraInput,
    CaptureConfig,
    IntrinsicsBackendChain,
    LearnedBackendUnavailable,
    PlumbLineVPEstimator,
    RtspCredentials,
    capture_background,
    run_intrinsics,
)
from simai_ai.calibration.intrinsics.capture import CAPTURE_FAILED, CAPTURE_OK, temporal_median
from simai_ai.calibration.intrinsics.runner import frames_source_for
from tests.calibration.intrinsics.test_chain import FakeLearned, truth_params

USER, PASSWORD = "simai-op", "Pw:s3cr3t@x"


class FakeFrame:
    def __init__(self, t, image):
        self.time = t
        self._image = image

    def to_ndarray(self, format):
        assert format == "bgr24"
        return self._image


class FakeContainer:
    """PyAV 컨테이너 흉내: decode 가 프레임을 내고, fail_at 번째에서 계정이 든 메시지의 예외를 낸다."""

    def __init__(self, frames, fail_at=None):
        self.frames = frames
        self.fail_at = fail_at
        self.closed = False

    def decode(self, video=0):
        for i, frame in enumerate(self.frames):
            if self.fail_at is not None and i == self.fail_at:
                raise OSError(f"broken pipe rtsp://{USER}:{PASSWORD}@10.0.0.1/stream1")
            yield frame

    def close(self):
        self.closed = True


def bgr(gray):
    g = np.clip(np.asarray(gray, dtype=float) * 255.0, 0, 255).astype(np.uint8)
    return np.repeat(g[:, :, None], 3, axis=2)


def with_person(base_bgr, k):
    """지나가는 사람 대신 어두운 사각형(프레임마다 자리가 바뀐다)."""
    img = base_bgr.copy()
    h, w = img.shape[:2]
    x0 = (k * 97) % (w - 80)
    y0 = (k * 53) % (h - 160)
    img[y0 : y0 + 160, x0 : x0 + 80] = 20
    return img


def frames_of(images, fps=5.0):
    return [FakeFrame(i / fps, im) for i, im in enumerate(images)]


def file_input(cid):
    return CameraInput(cid, "FILE", file=Path(f"{cid}.mp4"))


def opener_for(containers):
    def open_(url, options, timeout):
        name = Path(url).stem
        if name not in containers:
            raise FileNotFoundError(url)
        return containers[name]

    return open_


def test_temporal_median_removes_passing_person():
    base = bgr(np.tile(np.linspace(0.2, 0.8, 120), (90, 1)))
    frames = [base.copy() for _ in range(9)]
    for k in (1, 3, 5, 7):  # 절반 미만의 프레임에만 사람이 있다
        frames[k][10:60, 20:50] = 0
    out = temporal_median(frames, rows_per_chunk=7)
    assert out.dtype == np.uint8 and np.array_equal(out, base)


def test_capture_samples_by_media_time_and_limits():
    img = bgr(np.full((40, 60), 0.5))
    cont = FakeContainer(frames_of([img] * 50, fps=5.0))  # 10 초
    res = capture_background(file_input("cam1"), CaptureConfig(duration_s=6, interval_s=1, min_frames=3),
                             opener=opener_for({"cam1": cont}))
    assert res.status == CAPTURE_OK and res.frames_used == 6
    assert (res.first_sample_s, res.last_sample_s) == (0.0, 5.0)
    assert res.image_size == (60, 40) and res.background.shape == img.shape
    assert cont.closed
    capped = capture_background(file_input("cam1"),
                                CaptureConfig(duration_s=60, interval_s=0.2, min_frames=2, max_frames=4),
                                opener=opener_for({"cam1": FakeContainer(frames_of([img] * 50))}))
    assert capped.frames_used == 4


def test_capture_with_too_few_frames_is_failed():
    """경계: 고른 프레임이 min_frames 보다 하나 적으면 배경을 만들지 않는다. 같으면 만든다."""
    img = bgr(np.full((40, 60), 0.5))
    cfg = CaptureConfig(duration_s=60, interval_s=1, min_frames=5)
    few = capture_background(file_input("c"), cfg, opener=opener_for({"c": FakeContainer(frames_of([img] * 4, fps=1))}))
    assert few.status == CAPTURE_FAILED and few.background is None and "최소 5장" in few.reasons[0]
    ok = capture_background(file_input("c"), cfg, opener=opener_for({"c": FakeContainer(frames_of([img] * 5, fps=1))}))
    assert ok.status == CAPTURE_OK and ok.frames_used == 5


def test_capture_open_and_decode_errors_hide_credentials():
    cam = CameraInput("cam_A", "RTSP", host="10.0.0.1")
    creds = RtspCredentials(USER, PASSWORD)
    seen = {}

    def refuse(url, options, timeout):
        seen["url"], seen["options"] = url, options
        raise ConnectionRefusedError(f"cannot open {url}")

    res = capture_background(cam, CaptureConfig(), credentials=creds, opener=refuse)
    assert res.status == CAPTURE_FAILED and res.reasons == ["입력을 열지 못했다(ConnectionRefusedError)"]
    assert seen["options"] == {"rtsp_transport": "tcp"}
    assert "simai-op:Pw%3As3cr3t%40x@10.0.0.1:554/stream1" in seen["url"]
    img = bgr(np.full((40, 60), 0.5))
    broken = capture_background(cam, CaptureConfig(duration_s=60, interval_s=1, min_frames=5), credentials=creds,
                                opener=lambda *a: FakeContainer(frames_of([img] * 10, fps=1), fail_at=2))
    assert broken.status == CAPTURE_FAILED
    text = json.dumps(broken.to_record(), ensure_ascii=False) + json.dumps(res.to_record(), ensure_ascii=False)
    text += repr(creds) + json.dumps(cam.describe())
    assert USER not in text and PASSWORD not in text and "s3cr3t" not in text


def test_decode_error_after_enough_frames_keeps_background():
    img = bgr(np.full((40, 60), 0.5))
    res = capture_background(file_input("c"), CaptureConfig(duration_s=60, interval_s=1, min_frames=3),
                             opener=opener_for({"c": FakeContainer(frames_of([img] * 10, fps=1), fail_at=6)}))
    assert res.status == CAPTURE_OK and res.frames_used == 6 and "끊겼다" in res.reasons[0]


def test_resolution_change_mid_capture_is_failed():
    a = bgr(np.full((40, 60), 0.5))
    b = bgr(np.full((48, 64), 0.5))
    res = capture_background(file_input("c"), CaptureConfig(duration_s=60, interval_s=1, min_frames=2),
                             opener=opener_for({"c": FakeContainer(frames_of([a, a, a, b, b], fps=1))}))
    assert res.status == CAPTURE_FAILED and res.background is None and "바뀌었다" in res.reasons[0]


def test_wall_clock_limit_stops_stalled_stream():
    img = bgr(np.full((40, 60), 0.5))
    ticks = iter(range(0, 10_000, 50))  # 프레임마다 50 초가 흐르는 것처럼

    cfg = CaptureConfig(duration_s=45, interval_s=1, min_frames=3, wall_timeout_s=120)
    res = capture_background(file_input("c"), cfg,
                             opener=opener_for({"c": FakeContainer(frames_of([img] * 100, fps=1))}),
                             monotonic=lambda: float(next(ticks)))
    assert res.status == CAPTURE_FAILED and any("벽시계" in r for r in res.reasons)


def test_frames_source_rules():
    rtsp = CameraInput("a", "RTSP", host="h")
    assert frames_source_for([rtsp]) == "LIVE"
    assert frames_source_for([file_input("a")], "SIM") == "SIM"
    with pytest.raises(ValueError):
        frames_source_for([rtsp, file_input("b")])
    with pytest.raises(ValueError):
        frames_source_for([file_input("a")], "LIVE")


def test_run_intrinsics_end_to_end_with_fake_anycalib(wide_case):
    """가짜 AnyCalib(정답 값) + 사람 사각형이 지나가는 영상 2대 + 프레임이 부족한 1대 + 열리지 않는 1대."""
    cams = ["cam1", "cam2"]
    containers = {
        cid: FakeContainer(frames_of([with_person(bgr(wide_case.image(cid)), k) for k in range(12)], fps=1.0))
        for cid in cams
    }
    containers["cam3"] = FakeContainer(frames_of([bgr(wide_case.image("cam1"))] * 2, fps=1.0))
    truth = {cid: truth_params(wide_case.cameras[cid]) for cid in cams}

    class PerCamera(FakeLearned):
        def estimate(self, image, *, camera_id):
            self.params = truth[camera_id]
            return super().estimate(image, camera_id=camera_id)

    any_ = PerCamera(METHOD_ANYCALIB)
    chain = IntrinsicsBackendChain([("anycalib", any_), ("plumbline", PlumbLineVPEstimator())])
    run = run_intrinsics(
        [file_input(c) for c in ("cam1", "cam2", "cam3", "cam4")],
        chain=chain,
        capture_config=CaptureConfig(duration_s=30, interval_s=1, min_frames=5),
        created_at=1_760_000_000_000,
        frames_source="SIM",
        opener=opener_for(containers),
    )
    rec = run.record
    assert rec["format_version"] == "0.2.0"
    assert rec["input_provenance"] == {"frames": {"source": "SIM", "camera_ids": ["cam1", "cam2", "cam3", "cam4"]},
                                       "is_upper_bound": True}
    by_id = {c["camera_id"]: c for c in rec["cameras"]}
    for cid in cams:
        assert by_id[cid]["status"] == STATUS_OK and by_id[cid]["method"] == METHOD_ANYCALIB
        assert by_id[cid]["capture"]["frames_used"] == 12
        assert by_id[cid]["backend"]["straightness_check"]["status"] == "PASSED"
    assert by_id["cam3"]["status"] == STATUS_FAILED and "최소 5장" in by_id["cam3"]["reasons"][0]
    assert by_id["cam4"]["status"] == STATUS_FAILED and by_id["cam4"]["reasons"] == ["입력을 열지 못했다(FileNotFoundError)"]
    assert "backend" not in by_id["cam3"] and '"K"' not in json.dumps(by_id["cam3"])
    assert [e["camera_id"] for e in rec["estimates"]] == cams  # 수집 실패 카메라는 추정에 넣지 않는다
    assert rec["shared_lens"]["common"]["status"] == STATUS_OK
    assert rec["shared_lens"]["common"]["principal_point_assumed"] is False
    assert rec["run"]["images_stored"] is False and rec["run"]["backend_order"] == ["anycalib", "plumbline"]
    assert all(r.capture.background is None for r in run.cameras)  # 배경은 추정 뒤 버린다
    assert any_.calls == 2


def test_run_intrinsics_records_classical_fallback_and_credential_errors(wide_case):
    chain = IntrinsicsBackendChain([
        ("anycalib", FakeLearned(METHOD_ANYCALIB, raises=LearnedBackendUnavailable("anycalib 없음"))),
        ("plumbline", PlumbLineVPEstimator()),
    ])
    containers = {"cam1": FakeContainer(frames_of([bgr(wide_case.image("cam1"))] * 6, fps=1.0))}
    run = run_intrinsics(
        [file_input("cam1"), file_input("cam_B")],
        chain=chain,
        capture_config=CaptureConfig(duration_s=30, interval_s=1, min_frames=3),
        created_at=1_760_000_000_000,
        frames_source="FILE",
        credential_errors={"cam_B": "cam_B: .env 에 RTSP_USER_CAM_B/RTSP_PASS_CAM_B(또는 RTSP_USER/RTSP_PASS)가 없다"},
        opener=opener_for(containers),
    )
    by_id = {c["camera_id"]: c for c in run.record["cameras"]}
    assert by_id["cam1"]["method"] == METHOD_PLUMB_LINE_VP and by_id["cam1"]["backend"]["fallback"] is True
    assert by_id["cam1"]["backend"]["attempts"][0]["outcome"] == "UNAVAILABLE"
    assert run.record["run"]["unavailable_backends"] == {"anycalib": "LearnedBackendUnavailable: anycalib 없음"}
    assert by_id["cam_B"]["status"] == STATUS_FAILED and "RTSP_USER_CAM_B" in by_id["cam_B"]["reasons"][0]
    assert run.record["input_provenance"]["is_upper_bound"] is False


def _tiny_video(path, n=6):
    import av

    with av.open(str(path), "w") as out:
        stream = out.add_stream("libx264", rate=1)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for k in range(n):
            img = np.full((48, 64, 3), 40 * (k % 4), dtype=np.uint8)
            for packet in stream.encode(av.VideoFrame.from_ndarray(img, format="bgr24")):
                out.mux(packet)
        for packet in stream.encode():
            out.mux(packet)


def test_file_input_opens_local_file_by_handle_and_refuses_protocols(tmp_path, monkeypatch):
    """반례(리뷰 F1): `cache:file:` 처럼 FFmpeg 프로토콜을 붙인 경로는 열지 않는다(작업 폴더에 ffcache 가 생기지 않는다).
    로컬 파일은 경로 문자열이 아니라 파이썬 핸들로 PyAV 에 넘긴다(프로토콜 해석 없음)."""
    import av

    video = tmp_path / "v.mp4"
    _tiny_video(video)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    seen = []
    real_open = av.open

    def spy(target, *args, **kwargs):
        seen.append(type(target).__name__)
        return real_open(target, *args, **kwargs)

    monkeypatch.setattr(av, "open", spy)
    cfg = CaptureConfig(duration_s=10, interval_s=1, min_frames=3)
    ok = capture_background(CameraInput("c", "FILE", file=video), cfg)
    assert ok.status == CAPTURE_OK and ok.frames_used == 6
    assert seen and all(name != "str" for name in seen)
    bad = capture_background(CameraInput("c", "FILE", file=Path("cache:file:" + str(video))), cfg)
    assert bad.status == CAPTURE_FAILED and bad.reasons == ["입력을 열지 못했다(FileNotFoundError)"]
    assert list(work.iterdir()) == [] and list(tmp_path.glob("ffcache*")) == []


@pytest.mark.parametrize(
    "host",
    ["review-user:review-password@127.0.0.1", "127.0.0.1:554", "127.0.0.1/stream1", "host name", "@x", ""],
)
def test_rtsp_host_with_credentials_or_extras_is_refused_without_echo(host):
    """반례(리뷰 F3): 계정·포트·경로·공백이 섞인 host 는 CameraInput 을 만들지 않고, 오류 메시지에 값이 없다."""
    with pytest.raises(ValueError) as err:
        CameraInput("cam_X", "RTSP", host=host)
    msg = str(err.value)
    assert "review-password" not in msg and "review-user" not in msg and (not host or host not in msg)
    with pytest.raises(ValueError) as err2:
        CameraInput("cam_X", "RTSP", host="127.0.0.1", path="u:p@stream1")
    assert "u:p" not in str(err2.value)
    assert CameraInput("cam_X", "RTSP", host="cam-a.local").host == "cam-a.local"
    assert CameraInput("cam_X", "RTSP", host="[fe80::1]").host == "[fe80::1]"
