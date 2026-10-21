"""keypoint 시퀀스 파일과 한 명 걷기 대응·공통 시각 정렬 (-214). 사람 대응·시각 정렬이 부족하면 오류·경고를 낸다."""

from __future__ import annotations

import pytest

from simai_ai.calibration.extrinsics.keypoints import (
    KEYPOINT_FILE_FORMAT,
    CorrespondenceConfig,
    CorrespondenceError,
    build_observations,
    read_keypoint_file,
    write_keypoint_file,
)

T0 = 1_792_552_246_000


def _header(cams=("cam_A", "cam_B")):
    return {
        "format": KEYPOINT_FILE_FORMAT,
        "format_version": "0.1.0",
        "created_at": T0,
        "input_provenance": {"frames": {"source": "FILE", "dataset_id": "recording/t"}, "is_upper_bound": False},
        "cameras": [{"camera_id": c, "image_size": [2304, 1296]} for c in cams],
    }


def _person(u, score=0.9):
    return {"bbox": [u - 50, 100, u + 50, 900], "score": score, "keypoints_2d": [[u + j, 100.0 + 40 * j, 0.9] for j in range(17)]}


def _row(cam, t, people, unc=20):
    return {
        "camera_id": cam,
        "stream_epoch": 0,
        "frame_sequence": 0,
        "estimated_capture_time_ms": t,
        "timestamp_source": "RECEIVE_ESTIMATED",
        "timestamp_uncertainty_ms": unc,
        "detections": people,
    }


def _walk(cam, offset_ms, n=40, period=40, u0=500.0, speed=2.0, extra=None):
    rows = []
    for i in range(n):
        t = T0 + offset_ms + i * period
        people = [_person(u0 + speed * (t - T0))]
        if extra and extra(i):
            people.append(_person(1500.0))
        rows.append(_row(cam, t, people))
    return rows


def test_file_round_trip_and_no_overwrite(tmp_path):
    path = tmp_path / "kp.jsonl"
    rows = _walk("cam_A", 0, n=3) + _walk("cam_B", 13, n=3)
    assert write_keypoint_file(path, _header(), rows) == 6
    header, back = read_keypoint_file(path)
    assert header["cameras"][0]["camera_id"] == "cam_A" and back == rows
    with pytest.raises(FileExistsError):
        write_keypoint_file(path, _header(), rows)


def test_header_and_rows_are_checked(tmp_path):
    bad = _header()
    bad["input_provenance"] = {"frames": {"source": "SIM"}, "is_upper_bound": False}  # SIM 인데 상한 아님
    with pytest.raises(ValueError, match="input_provenance"):
        write_keypoint_file(tmp_path / "a.jsonl", bad, [])
    path = tmp_path / "b.jsonl"
    write_keypoint_file(path, _header(), [_row("cam_X", T0, [])])
    with pytest.raises(ValueError, match="cam_X"):
        read_keypoint_file(path)


def test_common_time_interpolation_between_offset_cameras():
    rows = _walk("cam_A", 0) + _walk("cam_B", 17)
    report = build_observations(_header(), rows, CorrespondenceConfig(sample_period_ms=100))
    obs = report.observations[3]
    # 두 카메라가 17 ms 어긋나 찍었어도 공통 시각 t 의 u 는 같은 직선 위 값이다(선형 보간)
    for cid in ("cam_A", "cam_B"):
        assert obs.views[cid][0][0] == pytest.approx(500.0 + 2.0 * (obs.t - T0), abs=1e-6)
    assert report.record["rule"] == "SINGLE_PERSON_PER_FRAME"


def test_frames_with_two_people_are_not_used_and_warned():
    # cam_B 는 홀수 프레임마다 두 번째 사람이 보인다: 그 프레임은 쓰지 않는다
    rows = _walk("cam_A", 0) + _walk("cam_B", 17, extra=lambda i: i % 2 == 1)
    report = build_observations(_header(), rows, CorrespondenceConfig(sample_period_ms=100))
    counts = report.record["cameras"]["cam_B"]
    assert counts["ambiguous"] == 20 and counts["single"] == 20
    assert any("두 명 이상" in w for w in report.warnings)
    for obs in report.observations:  # 두 번째 사람(u=1500) 값이 관측에 섞이지 않는다(걷는 사람 직선 위)
        assert obs.views["cam_B"][0][0] == pytest.approx(500.0 + 2.0 * (obs.t - T0), abs=1e-6)


def test_only_one_camera_with_single_person_is_an_error():
    rows = _walk("cam_A", 0) + _walk("cam_B", 17, extra=lambda i: True)
    with pytest.raises(CorrespondenceError, match="2대 미만"):
        build_observations(_header(), rows)


def test_no_common_time_is_an_error():
    rows = _walk("cam_A", 0, n=10) + _walk("cam_B", 60_000, n=10)
    with pytest.raises(CorrespondenceError, match="관측이 없다"):
        build_observations(_header(), rows)


def test_uncertain_timestamps_and_low_scores_are_excluded():
    rows = _walk("cam_A", 0) + [dict(r, timestamp_uncertainty_ms=45) for r in _walk("cam_B", 17)]
    with pytest.raises(CorrespondenceError):
        build_observations(_header(), rows)  # cam_B 의 시각 불확실성 45ms > 25ms: 공통 시각에 맞출 수 없다
    low = _walk("cam_A", 0) + [dict(r, detections=[_person(700, score=0.2)]) for r in _walk("cam_B", 17)]
    with pytest.raises(CorrespondenceError, match="2대 미만"):
        build_observations(_header(), low)


def test_few_observations_warn():
    rows = _walk("cam_A", 0, n=10) + _walk("cam_B", 17, n=10)
    report = build_observations(_header(), rows, CorrespondenceConfig(sample_period_ms=100))
    assert 0 < len(report.observations) < 30
    assert any("관측이" in w for w in report.warnings)
