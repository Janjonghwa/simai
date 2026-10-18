"""카메라 없이 끝까지: 로컬 RTSP 카메라 흉내(fake_camera) → 실제 PyAV RTSP 수신(gateway/recorder RtspSource) → 보고서.

RTCP SR 유무·흔들림, 카메라가 건너뛴 프레임, 패킷 손실, PTS 역행, 키프레임 없는 스트림, 동시 접속 제한, 접속 실패,
계정이 출력·보고서·녹화 파일에 남지 않는지를 본다. 영상은 메모리에서 만들고 녹화는 tmp_path 에만 쓴다.
"""

from __future__ import annotations

import base64
import json
import socket
import subprocess
import urllib.parse
from pathlib import Path

import av
import av.logging
import pytest
from fake_camera import CameraScene, FakeRtspCamera
from simai_recorder.clock import SystemClock
from simai_recorder.segment import SegmentPaths, read_segment
from simai_recorder.sources import RtspConfig, RtspSource

from simai_rtsp_probe import cli
from simai_rtsp_probe.capture import concurrency_trials, max_concurrent, measure
from simai_rtsp_probe.report import CAMERA_STREAM_CANDIDATE, RECEIVE_ESTIMATED, summarize

USER = "probe-user-S3CRET"
PASSWORD = "p@ss:w/rd-S3CRET"


def _source(cam: FakeRtspCamera, password: str = PASSWORD) -> RtspSource:
    return RtspSource(RtspConfig("127.0.0.1", port=cam.port, user=USER, password=password, open_timeout_s=5,
                                 read_timeout_s=3))


def _camera(scene: CameraScene, **kw) -> FakeRtspCamera:
    return FakeRtspCamera(scene, user=USER, password=PASSWORD, **kw)


def _measure_all(scene: CameraScene, tmp_path: Path | None = None, record_seconds: float = 0.0):
    """스트림이 끝날 때까지 받는다(끝나면 카메라가 끊으므로 ended_by=disconnected)."""
    with _camera(scene) as cam:
        m = measure(_source(cam), SystemClock(), 30, record_dir=tmp_path, camera_id="cam_A",
                    record_seconds=record_seconds)
    assert m.ended_by == "disconnected", m.error
    return m, summarize(m.observations, m.info)


def test_full_stream_metrics_rtcp_and_short_recording(frames_250, tmp_path):
    scene = CameraScene(frames_250[:100], pace=0.2, sr_every=5)
    m, r = _measure_all(scene, tmp_path, record_seconds=0.3)  # 스트림 0.8초 중 앞 0.3초

    assert m.info is not None and (m.info.codec, m.info.width, m.info.height) == ("H264", 160, 96)
    assert r["frames"] == 100
    assert r["fps_by_pts"] == 25.0
    assert r["pts"]["interval_ms"]["p50"] == 40.0 and r["pts"]["regressions"] == 0 and r["pts"]["gaps"] == 0
    assert r["keyframes"] == 4 and r["keyframe_interval_s"]["p50"] == 1.0
    # FFmpeg 은 첫 패킷의 PTS 를 비운다(실측과 같은 경로). 지어내지 않고 센다
    assert r["pts"]["packets_without_pts"] <= 1
    assert r["rtcp"]["present"] and r["rtcp"]["sender_reports"] >= 15
    assert r["rtcp"]["mapping_consistency_ms"]["step_max"] <= 0.1
    assert r["timestamp_source_recommendation"] == CAMERA_STREAM_CANDIDATE

    # 짧은 원본 녹화: 녹화 도구 세그먼트 형식으로 다시 읽히고, 디코딩된다
    rec = m.recording
    assert rec is not None and 0 < rec["frames"] < 100 and rec["dropped_before_keyframe"] == 0
    paths = SegmentPaths.of(tmp_path / "cam_A", 0)
    codec_info, it = read_segment(paths)
    rows = list(it)
    assert codec_info["width"] == 160 and len(rows) == rec["frames"] and rows[0][0]["keyframe"]
    with av.open(str(paths.h264), format="h264") as c:
        assert sum(1 for _ in c.decode(video=0)) == rec["frames"]


def test_jittery_rtcp_like_c110_is_measured_but_not_trusted(frames_250):
    _, r = _measure_all(CameraScene(frames_250[:100], pace=0.2, sr_every=5, sr_jitter_ms=400, seed=3))
    assert r["rtcp"]["present"]
    assert r["rtcp"]["mapping_consistency_ms"]["step_p95"] > 100
    assert r["timestamp_source_recommendation"] == RECEIVE_ESTIMATED


def test_camera_without_rtcp_has_no_camera_time(frames_250):
    m, r = _measure_all(CameraScene(frames_250[:60], pace=0.2, send_rtcp=False))
    assert all(o.camera_time_us is None and not o.sender_report for o in m.observations)
    assert r["rtcp"]["present"] is False and r["rtcp"]["camera_minus_receive_ms"] is None
    assert r["timestamp_source_recommendation"] == RECEIVE_ESTIMATED


def test_skipped_frames_and_lost_packets(frames_250):
    scene = CameraScene(frames_250[:100], pace=0.2, drop_frames={40, 41, 42, 70}, drop_fragments={50, 75})
    _, r = _measure_all(scene)
    # 건너뛴 프레임 4개(빈칸 2곳). IDR 조각을 잃은 키프레임(50, 75)은 남은 SPS/PPS 가 다음 프레임과 합쳐져
    # 한 프레임이 사라진 빈칸으로 보이고 키프레임으로 세지 않는다(FFmpeg 실제 동작). 손실이 키프레임 수에서도 드러난다.
    assert r["pts"]["gaps"] == 4 and r["pts"]["missing_frames_estimate"] == 6
    assert r["frames"] == 94
    assert r["keyframes"] == 2


def test_pts_regression_passes_through_and_is_counted(frames_250):
    def ts(i: int) -> int:
        return round(i * 3600) - (9000 if i >= 50 else 0)  # 50번째부터 100ms 뒤로

    _, r = _measure_all(CameraScene(frames_250[:80], pace=0.2, rtp_ts=ts, send_rtcp=False))
    assert r["pts"]["regressions"] == 1 and r["pts"]["max_regression_ms"] == 60.0
    assert any("PTS 역행" in w for w in r["warnings"])


def test_stream_without_keyframes_is_flagged_and_not_recorded(frames_250, tmp_path):
    m, r = _measure_all(CameraScene(frames_250[:60], pace=0.2, skip_keyframes=True), tmp_path, record_seconds=5)
    assert r["keyframes"] == 0 and r["keyframe_interval_s"]["p50"] is None
    assert "키프레임이 오지 않았다" in r["warnings"]
    assert m.recording is not None and m.recording["frames"] == 0 and m.recording["files"] is None
    assert not list(tmp_path.rglob("*.h264"))


def test_concurrency_third_session_is_rejected(frames_250):
    with _camera(CameraScene(frames_250, pace=0.2), max_sessions=2) as cam:
        trials = concurrency_trials(lambda: _source(cam), SystemClock(), 3, 0.4, open_gap_s=0.05, rest_s=0.3)
    assert [t["opened"] for t in trials] == [1, 2, 2]
    assert [t["received_whole_time"] for t in trials] == [1, 2, 2]
    third = trials[2]["sessions"][2]
    assert third["opened"] is False and "RTSP 접속 실패" in third["error"]
    assert all(s["frames"] > 0 for t in trials for s in t["sessions"] if s["opened"])
    assert max_concurrent(trials) == 2


def _secret_forms() -> list[str]:
    return ["S3CRET", urllib.parse.quote(PASSWORD, safe=""), base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()]


def _assert_no_secrets(texts: list[str]) -> None:
    for text in texts:
        for s in _secret_forms():
            assert s not in text


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    p = tmp_path / "test.env"
    p.write_text(f"RTSP_USER={USER}\nRTSP_PASS={PASSWORD}\n", encoding="utf-8")
    return p


@pytest.mark.parametrize("auth", ["digest", "basic"])
def test_cli_measure_writes_report_without_credentials(frames_250, tmp_path, env_file, capfd, auth):
    out = tmp_path / "out"
    av.logging.set_level(av.logging.DEBUG)  # FFmpeg 로그까지 켜도 계정이 나오지 않아야 한다
    try:
        with _camera(CameraScene(frames_250, pace=0.2), auth=auth) as cam:
            code = cli.main(["measure", "--host", "127.0.0.1", "--port", str(cam.port), "--seconds", "0.8",
                             "--record-seconds", "0.5", "--out", str(out), "--env-file", str(env_file),
                             "--camera-id", "cam_A", "--note", "시험"])
    finally:
        av.logging.set_level(None)
    printed = capfd.readouterr()
    assert code == 0, printed
    [report_path] = list(out.rglob("report.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["ended_by"] == "completed" and report["metrics"]["frames"] > 10
    assert report["source"]["url"] == f"rtsp://127.0.0.1:{cam.port}/stream1"
    assert report["stream"]["width"] == 160 and report["note"] == "시험"
    assert report["recording"]["frames"] > 0
    assert isinstance(report["started_at_ms"], int) and report["ended_at_ms"] >= report["started_at_ms"]
    files = [p.read_bytes().decode("utf-8", "replace") for p in out.rglob("*") if p.is_file()]
    _assert_no_secrets([printed.out, printed.err, *files])


def test_cli_wrong_password_and_unreachable_camera_fail_cleanly(frames_250, tmp_path, env_file, capfd):
    out = tmp_path / "out"
    with FakeRtspCamera(CameraScene(frames_250[:50]), user=USER, password="other") as cam:
        code = cli.main(["measure", "--host", "127.0.0.1", "--port", str(cam.port), "--seconds", "1",
                         "--out", str(out), "--env-file", str(env_file), "--open-timeout", "3"])
    assert code == 1
    with socket.socket() as s:  # 아무도 듣지 않는 포트
        s.bind(("127.0.0.1", 0))
        dead_port = s.getsockname()[1]
    code = cli.main(["measure", "--host", "127.0.0.1", "--port", str(dead_port), "--seconds", "1",
                     "--out", str(out), "--env-file", str(env_file), "--open-timeout", "2"])
    assert code == 1
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.rglob("report.json"))]
    assert len(reports) == 2
    for r in reports:
        assert r["ended_by"] == "open_failed" and r["error"].startswith("RTSP 접속 실패")
        assert r["metrics"]["frames"] == 0 and r["stream"] is None and r["recording"] is None
        assert r["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
    printed = capfd.readouterr()
    files = [p.read_text(encoding="utf-8") for p in out.rglob("*") if p.is_file()]
    _assert_no_secrets([printed.out, printed.err, *files])


def test_cli_concurrency_report(frames_250, tmp_path, env_file, capfd):
    out = tmp_path / "out"
    with _camera(CameraScene(frames_250, pace=0.2), max_sessions=2) as cam:
        code = cli.main(["concurrency", "--host", "127.0.0.1", "--port", str(cam.port), "--max-sessions", "3",
                         "--seconds", "0.4", "--open-gap", "0.05", "--rest", "0.3", "--out", str(out),
                         "--env-file", str(env_file)])
    assert code == 0
    [p] = list(out.rglob("report.json"))
    report = json.loads(p.read_text(encoding="utf-8"))
    assert report["max_concurrent_sessions_ok"] == 2 and len(report["trials"]) == 3
    printed = capfd.readouterr()
    _assert_no_secrets([printed.out, printed.err, p.read_text(encoding="utf-8")])


def test_missing_credentials_is_a_config_error(tmp_path, capfd):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    code = cli.main(["measure", "--host", "127.0.0.1", "--out", str(tmp_path / "out"), "--env-file", str(empty)])
    assert code == 2 and "RTSP_USER" in capfd.readouterr().err
    assert not (tmp_path / "out").exists()


def test_redact_only_touches_strings():
    forms = cli.secret_forms(["12", "pw/x"])
    assert cli.redact({"a": 12, "b": "x12y", "c": ["pw%2Fx", 1.5]}, forms) == {"a": 12, "b": "x***y", "c": ["***", 1.5]}


def test_default_output_is_git_ignored():
    target = cli.DEFAULT_PROBE_OUT / "20261018-000000z-cam_A-measure" / "report.json"
    try:
        res = subprocess.run(["git", "check-ignore", "-q", str(target)], cwd=cli.REPO_ROOT, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git 을 쓸 수 없다")
    assert res.returncode == 0, f"{target} 가 .gitignore 로 제외되지 않는다"
    for code_file in Path(cli.__file__).parent.glob("*.py"):
        assert subprocess.run(["git", "check-ignore", "-q", str(code_file)], cwd=cli.REPO_ROOT).returncode == 1


def test_cli_disconnect_mid_measurement_keeps_report_and_fails(frames_250, tmp_path, env_file):
    """측정 중 끊기면 다시 붙지 않고, 받은 데까지의 보고서를 남기고 1 로 끝난다(끊김도 측정 결과다)."""
    out = tmp_path / "out"
    with _camera(CameraScene(frames_250[:50], pace=0.2)) as cam:
        code = cli.main(["measure", "--host", "127.0.0.1", "--port", str(cam.port), "--seconds", "30",
                         "--record-seconds", "0", "--out", str(out), "--env-file", str(env_file)])
    assert code == 1
    [p] = list(out.rglob("report.json"))
    report = json.loads(p.read_text(encoding="utf-8"))
    assert report["ended_by"] == "disconnected" and report["metrics"]["frames"] == 50
    assert report["recording"] is None and not list(out.rglob("*.h264"))


@pytest.mark.parametrize("argv", [["measure", "--seconds", "0"], ["concurrency", "--max-sessions", "0"],
                                  ["measure", "--camera-id", "cam A"]])
def test_bad_arguments_are_rejected_before_connecting(argv, tmp_path):
    try:
        code = cli.main([*argv, "--host", "127.0.0.1", "--out", str(tmp_path / "out")])
    except SystemExit as e:  # argparse 형식 오류
        code = e.code
    assert code == 2
    assert not (tmp_path / "out").exists()
