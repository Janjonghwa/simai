"""보고서 계산: fps, 도착 간격 p50/p95, PTS 역행·빈칸, 키프레임 주기, 비트레이트, RTCP 시각 출처 판정.

카메라 없이 관측 목록을 직접 만들어 넣는다(PyAV 입력을 흉내 낸 것과 같은 값).
"""

from __future__ import annotations

from fractions import Fraction

from simai_recorder.sources import StreamInfo

from simai_rtsp_probe.report import (
    CAMERA_STREAM_CANDIDATE,
    MIN_SENDER_REPORTS,
    RECEIVE_ESTIMATED,
    SR_STEP_TOLERANCE_MS,
    Observation,
    summarize,
)

INFO = StreamInfo(codec="H264", width=2304, height=1296, pts_timebase=Fraction(1, 90000), nominal_fps=25.0,
                  profile="High")
T0_MS = 1_792_300_000_000


def observations(arrival_iv_ms: list[float], pts: list[int | None] | None = None, keys: set[int] | None = None,
                 size: int = 5000, camera_us: list[int | None] | None = None,
                 sr: set[int] | None = None) -> list[Observation]:
    n = len(arrival_iv_ms) + 1
    pts = pts if pts is not None else [i * 3600 for i in range(n)]
    keys = keys if keys is not None else set(range(0, n, 50))
    t, out = 0.0, []
    for i in range(n):
        if i:
            t += arrival_iv_ms[i - 1]
        out.append(Observation(received_mono_s=100 + t / 1000, received_at_ms=T0_MS + round(t), pts=pts[i],
                               keyframe=i in keys, size=size,
                               camera_time_us=None if camera_us is None else camera_us[i],
                               sender_report=bool(sr and i in sr)))
    return out


def with_rtcp(n: int, sr_every: int, step_ms: list[float] | None = None, offset_ms: float = -35.0,
              epoch_ms: int = T0_MS) -> tuple[list[int | None], set[int]]:
    """프레임 i 의 카메라 시각: SR k 의 대응(원점 + k 번째 흔들림) + PTS. SR 은 sr_every 프레임마다."""
    cam: list[int | None] = []
    shift = 0.0
    for i in range(n):
        k = i // sr_every
        if i % sr_every == 0 and k > 0 and step_ms:
            shift += step_ms[(k - 1) % len(step_ms)]
        cam.append(round((epoch_ms + offset_ms + shift + i * 40.0) * 1000))
    return cam, set(range(0, n, sr_every))


# --------------------------------------------------------------------------- 프레임 수치


def test_steady_stream_rates_percentiles_keyframes_bitrate():
    iv = [40.0] * 200 + [45.0] * 40 + [70.0] * 10  # 250 간격, 10초 분량 PTS
    m = summarize(observations(iv), INFO)

    assert m["frames"] == 251
    assert m["fps_by_pts"] == 25.0 and m["nominal_interval_basis"] == "pts"
    assert m["fps_by_arrival"] == round(250 / 10.5, 3)
    assert m["arrival_interval_ms"] == {"p50": 40.0, "p95": 45.0, "p99": 70.0, "max": 70.0}
    assert m["camera_to_gateway_gap"] == {"threshold_ms": 60.0, "count": 10, "max_ms": 70.0}
    assert m["pts"]["interval_ms"] == {"p50": 40.0, "p95": 40.0, "max": 40.0}
    assert m["pts"]["regressions"] == 0 and m["pts"]["gaps"] == 0 and m["pts"]["missing_frames_estimate"] == 0
    assert m["keyframes"] == 6 and m["packets_before_first_keyframe"] == 0
    assert m["keyframe_interval_s"] == {"p50": 2.0, "max": 2.0, "basis": "pts"}
    assert m["bitrate_kbps_by_pts"] == round(251 * 5000 * 8 / 10_000, 1)
    # 수신 − 추정 촬영 시각: 창 안 가장 덜 늦게 온 프레임이 기준이라 음수가 없다
    rd = m["receive_minus_estimated_capture_ms"]
    assert rd["p50"] is not None and rd["p50"] >= 0 and rd["max"] == 40 * 5 + 10 * 30  # 늦게 온 만큼이 쌓인다


def test_pts_regression_is_counted_not_hidden_and_swap_is_not_a_gap():
    pts = [i * 3600 for i in range(100)]
    pts[30], pts[31] = pts[31], pts[30]  # 순서 바뀜: +80ms, −40ms, +80ms
    pts[60:] = [p - 9000 for p in pts[60:]]  # 60번째에서 100ms 뒤로 감(카메라 시계 되돌림)
    m = summarize(observations([40.0] * 99, pts=pts), INFO)

    assert m["pts"]["regressions"] == 2
    assert m["pts"]["max_regression_ms"] == 60.0  # 100ms 되돌림에서 한 프레임 간격(40ms)을 뺀 값
    # 순서 바뀜 앞의 +80ms 는 빈칸 하나로 세고, 역행 바로 뒤의 회복(+80ms)은 세지 않는다
    assert m["pts"]["gaps"] == 1
    assert any("PTS 역행 2회" in w for w in m["warnings"])
    assert m["fps_by_pts"] == 25.0  # 공칭 간격은 흔들리지 않는다


def test_camera_skipped_frames_show_as_pts_gaps():
    pts = [i * 3600 for i in range(120) if i not in {40, 41, 42, 90}]
    m = summarize(observations([40.0] * (len(pts) - 1), pts=pts, keys={0}), INFO)

    assert m["pts"]["gaps"] == 2
    assert m["pts"]["missing_frames_estimate"] == 4
    assert m["pts"]["interval_ms"]["max"] == 160.0


def test_stream_without_keyframes_reports_none_not_zero_interval():
    m = summarize(observations([40.0] * 99, keys=set()), INFO)

    assert m["keyframes"] == 0
    assert m["packets_before_first_keyframe"] == 100
    assert m["keyframe_interval_s"] == {"p50": None, "max": None, "basis": None}
    assert "키프레임이 오지 않았다" in m["warnings"]


def test_single_keyframe_has_no_interval():
    m = summarize(observations([40.0] * 99, keys={10}), INFO)
    assert m["keyframes"] == 1 and m["packets_before_first_keyframe"] == 10
    assert m["keyframe_interval_s"]["p50"] is None


def test_missing_pts_falls_back_to_declared_fps_but_never_invents_pts_metrics():
    m = summarize(observations([40.0] * 49 + [100.0], pts=[None] * 51), INFO)

    assert m["fps_by_pts"] is None
    assert m["nominal_interval_basis"] == "declared_fps"
    assert m["camera_to_gateway_gap"] == {"threshold_ms": 60.0, "count": 1, "max_ms": 100.0}
    assert m["pts"]["packets_without_pts"] == 51
    assert m["pts"]["gaps"] is None and m["pts"]["missing_frames_estimate"] is None
    assert m["receive_minus_estimated_capture_ms"]["p50"] is None
    # 키프레임 주기는 도착 시각으로 대신 잰다
    assert m["keyframe_interval_s"]["basis"] == "arrival"


def test_no_pts_and_no_declared_fps_leaves_gap_unknown():
    info = StreamInfo(codec="H264", width=640, height=360, pts_timebase=None, nominal_fps=None)
    m = summarize(observations([40.0] * 20, pts=[None] * 21), info)
    assert m["nominal_interval_ms"] is None
    assert m["camera_to_gateway_gap"]["threshold_ms"] is None and m["camera_to_gateway_gap"]["count"] is None
    assert m["pts"]["regressions"] is None


def test_too_few_frames_fills_nothing():
    m = summarize(observations([])[:1], INFO)
    assert m["frames"] == 1
    assert m["fps_by_arrival"] is None and m["arrival_interval_ms"] is None and m["keyframes"] is None
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
    m0 = summarize([], None)
    assert m0["frames"] == 0 and m0["rtcp"]["present"] is False


# --------------------------------------------------------------------------- RTCP 시각


def test_no_rtcp_is_reported_as_absent_and_never_fabricated():
    m = summarize(observations([40.0] * 99), INFO)
    r = m["rtcp"]
    assert r["present"] is False and r["sender_reports"] == 0 and r["packets_with_camera_time"] == 0
    assert r["camera_minus_receive_ms"] is None and r["mapping_consistency_ms"] is None
    assert r["camera_clock_plausible"] is None and r["sr_interval_s_p50"] is None
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
    assert "오지 않았다" in m["timestamp_source_reason"]


def test_consistent_sender_reports_make_camera_stream_only_a_candidate():
    n = 100
    cam, sr = with_rtcp(n, sr_every=10)
    m = summarize(observations([40.0] * (n - 1), camera_us=cam, sr=sr), INFO)
    r = m["rtcp"]
    assert r["present"] and r["sender_reports"] == 10 and r["packets_with_camera_time"] == n
    assert r["mapping_consistency_ms"]["sender_reports_used"] == 10
    assert r["mapping_consistency_ms"]["step_max"] == 0.0
    assert r["camera_minus_receive_ms"]["p50"] == -35.0
    assert r["sr_interval_s_p50"] == 0.4
    assert m["timestamp_source_recommendation"] == CAMERA_STREAM_CANDIDATE
    assert "G0 LED" in m["timestamp_source_reason"]


def test_jittery_sender_reports_like_c110_keep_receive_estimated():
    n = 200
    cam, sr = with_rtcp(n, sr_every=12, step_ms=[380.0, -410.0, 250.0, -220.0])
    m = summarize(observations([40.0] * (n - 1), camera_us=cam, sr=sr), INFO)
    mc = m["rtcp"]["mapping_consistency_ms"]
    assert mc["step_p95"] >= 380
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
    assert "흔들린다" in m["timestamp_source_reason"]


def test_too_few_sender_reports_are_not_enough_evidence():
    n = 30
    cam, sr = with_rtcp(n, sr_every=15)  # SR 2개: 완벽히 일관돼도 판단하지 않는다
    m = summarize(observations([40.0] * (n - 1), camera_us=cam, sr=sr), INFO)
    assert m["rtcp"]["mapping_consistency_ms"]["sender_reports_used"] == 2 < MIN_SENDER_REPORTS
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
    assert "2개뿐" in m["timestamp_source_reason"]


def test_unset_camera_clock_is_not_a_candidate():
    n = 100
    cam, sr = with_rtcp(n, sr_every=10, epoch_ms=0)  # 1970 년
    m = summarize(observations([40.0] * (n - 1), camera_us=cam, sr=sr), INFO)
    assert m["rtcp"]["camera_clock_plausible"] is False
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED


def test_step_tolerance_boundary():
    n = 100
    for step, expected in ((SR_STEP_TOLERANCE_MS, CAMERA_STREAM_CANDIDATE), (SR_STEP_TOLERANCE_MS + 0.2, RECEIVE_ESTIMATED)):
        cam, sr = with_rtcp(n, sr_every=10, step_ms=[step, -step])
        m = summarize(observations([40.0] * (n - 1), camera_us=cam, sr=sr), INFO)
        assert m["timestamp_source_recommendation"] == expected, step


def test_sender_report_on_packet_without_pts_still_opens_a_new_mapping():
    n = 100
    cam, sr = with_rtcp(n, sr_every=10, step_ms=[300.0])
    pts: list[int | None] = [i * 3600 for i in range(n)]
    for i in sr:
        pts[i] = None  # SR 표시가 붙은 패킷마다 PTS 가 없다
    m = summarize(observations([40.0] * (n - 1), pts=pts, camera_us=cam, sr=sr), INFO)
    mc = m["rtcp"]["mapping_consistency_ms"]
    assert mc["sender_reports_used"] == 10
    assert mc["step_p50"] == 300.0
    assert m["timestamp_source_recommendation"] == RECEIVE_ESTIMATED
