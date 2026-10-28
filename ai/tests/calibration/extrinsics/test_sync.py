"""촬영 시각 바로잡기 (-313): 깨진 PTS 시각선 다시 매기기, 카메라별 시각 보정값 추정, 보정값 적용.

합성 장면(카메라 4대 핀홀, 걷는 사람 관절 17개)이라 무거운 의존성·시뮬레이터 없이 돈다. 지표는 상한이다.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from simai_ai.calibration.extrinsics.keypoints import (
    KEYPOINT_FILE_FORMAT,
    build_observations,
    single_person_samples,
)
from simai_ai.calibration.extrinsics.sync import (
    TIMELINE_SEQUENCE_CLOCK,
    TimeOffsetConfig,
    estimate_time_offsets,
    repair_timelines,
)
from simai_ai.geometry.rig import PinholeCamera, RigCamera

T0 = 1_793_153_300_000
SIZE = (1920, 1080)
F = 1000.0
PERIOD_MS = 40.0
EVERY_N = 3
DURATION_MS = 40_000
# 카메라 위치(방 네 모서리, 높이 2.2 m), 모두 방 가운데 1 m 높이를 본다
CAMERA_POSITIONS = {
    "cam_A": (0.3, 0.3, 2.2),
    "cam_B": (8.7, 0.3, 2.2),
    "cam_C": (8.7, 6.7, 2.2),
    "cam_D": (0.3, 6.7, 2.2),
}
# 서 있는 사람의 관절 상대 위치(COCO17, m): 머리·어깨·팔·엉덩이·무릎·발목
JOINTS = np.array(
    [
        (0.0, 0.08, 1.62), (0.03, 0.07, 1.66), (-0.03, 0.07, 1.66), (0.07, 0.0, 1.64), (-0.07, 0.0, 1.64),
        (0.18, 0.0, 1.42), (-0.18, 0.0, 1.42), (0.22, 0.0, 1.12), (-0.22, 0.0, 1.12), (0.24, 0.02, 0.85),
        (-0.24, 0.02, 0.85), (0.1, 0.0, 0.95), (-0.1, 0.0, 0.95), (0.1, 0.02, 0.5), (-0.1, 0.02, 0.5),
        (0.1, 0.0, 0.08), (-0.1, 0.0, 0.08),
    ]
)


def _look_at(C, target=(4.5, 3.5, 1.0)):
    z = np.asarray(target, float) - C
    z /= np.linalg.norm(z)
    x = np.cross(z, (0.0, 0.0, 1.0))
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    return np.vstack([x, y, z])


def _rigs():
    out = {}
    for cid, pos in CAMERA_POSITIONS.items():
        C = np.array(pos, float)
        R = _look_at(C)
        K = [[F, 0.0, SIZE[0] / 2], [0.0, F, SIZE[1] / 2], [0.0, 0.0, 1.0]]
        out[cid] = RigCamera(cid, PinholeCamera(K, R.tolist(), (-R @ C).tolist()), SIZE)
    return out


def _walker(t_ms):
    """방 안을 1.2 m/s 로 도는 사람(타원 경로)의 관절 World 좌표 (17, 3)."""
    s = (t_ms - T0) / 1000.0
    a = 1.2 * s / 2.6  # 반지름 약 2.6 m
    center = np.array([4.5 + 3.0 * math.cos(a), 3.5 + 2.2 * math.sin(a), 0.0])
    return center + JOINTS


def _rows(rigs, time_error_ms=None, noise_px=0.0, seed=3):
    """카메라별 keypoint 파일 프레임 줄. time_error_ms(camera_id → 함수(frame_sequence) → ms)를 기록 시각에 더한다."""
    rng = np.random.default_rng(seed)
    rows = []
    for i, (cid, rig) in enumerate(sorted(rigs.items())):
        phase = 9.0 * i  # 카메라마다 노출 순간이 다르다
        for seq in range(0, int(DURATION_MS / PERIOD_MS), EVERY_N):
            t_true = T0 + phase + seq * PERIOD_MS
            X = _walker(t_true)
            Xc = rig.to_camera(X)
            uv = Xc[:, :2] / Xc[:, 2:3] * F + np.array(SIZE) / 2 + rng.normal(scale=noise_px, size=(17, 2))
            err = 0.0 if time_error_ms is None or cid not in time_error_ms else time_error_ms[cid](seq)
            rows.append(
                {
                    "camera_id": cid,
                    "stream_epoch": 0,
                    "frame_sequence": seq,
                    "estimated_capture_time_ms": round(t_true + err),
                    "timestamp_source": "RECEIVE_ESTIMATED",
                    "timestamp_uncertainty_ms": 20,
                    "detections": [
                        {"bbox": [0, 0, 10, 10], "score": 0.9, "keypoints_2d": [[float(u), float(v), 0.9] for u, v in uv]}
                    ],
                }
            )
    return rows


def _header(rigs):
    return {
        "format": KEYPOINT_FILE_FORMAT,
        "format_version": "0.1.0",
        "created_at": T0,
        "input_provenance": {"frames": {"source": "SIM"}, "is_upper_bound": True},
        "cameras": [{"camera_id": c, "image_size": list(SIZE)} for c in sorted(rigs)],
    }


def _sawtooth(seq):
    """로비 카메라처럼 13장 묶음마다 촬영 시각이 번갈아 380 ms 이르다(묶음 안 40 ms 간격은 맞다)."""
    return -380.0 if (seq // 13) % 2 else 0.0


@pytest.fixture(scope="module")
def rigs():
    return _rigs()


# --------------------------------------------------------------------------- 시각선


def test_broken_pts_timeline_is_retimed_from_frame_sequence(rigs):
    rows = _rows(rigs, {"cam_B": _sawtooth})
    fixed, record = repair_timelines(rows)
    assert record["cam_B"]["retimed"] is True and record["cam_B"]["method"] == TIMELINE_SEQUENCE_CLOCK
    assert record["cam_B"]["broken_step_ratio"] > 0.1
    assert not any(record[c]["retimed"] for c in ("cam_A", "cam_C", "cam_D"))
    b = [r for r in fixed if r["camera_id"] == "cam_B"]
    t = np.array([r["estimated_capture_time_ms"] for r in b], float)
    seq = np.array([r["frame_sequence"] for r in b], float)
    # 다시 매긴 시각은 frame_sequence 에 선형이고 촬영 순서를 지킨다(절편은 어긋남 만큼 임의)
    assert np.all(np.diff(t) > 0)
    assert np.allclose(np.diff(t) / np.diff(seq), PERIOD_MS, atol=0.5)
    assert all("estimated_capture_time_ms_original" in r for r in b)
    # 깨지지 않은 카메라 행은 그대로다
    a_in = [r for r in rows if r["camera_id"] == "cam_A"]
    a_out = [r for r in fixed if r["camera_id"] == "cam_A"]
    assert a_in == a_out


def test_healthy_timeline_with_jitter_and_frame_reorder_is_kept(rigs):
    rng = np.random.default_rng(5)
    rows = _rows(rigs)
    for r in rows:
        r["estimated_capture_time_ms"] += int(rng.integers(-15, 16))
    # B 프레임처럼 디코딩 순서보다 표시 시각이 두 장(80 ms) 이른 행이 열 줄마다 있어도 시각선은 멀쩡하다
    a = [r for r in rows if r["camera_id"] == "cam_A"]
    for r in a[5::10]:
        r["estimated_capture_time_ms"] -= 80
    fixed, record = repair_timelines(rows)
    assert not any(v["retimed"] for v in record.values())
    assert fixed == rows


# --------------------------------------------------------------------------- 시각 보정값


def _samples(rigs, rows):
    return single_person_samples(_header(rigs), rows, 0.5)[0]


def test_constant_time_offset_of_one_camera_is_recovered(rigs):
    rows = _rows(rigs, {"cam_C": lambda seq: 150.0}, noise_px=1.0)
    result = estimate_time_offsets(_samples(rigs, rows), rigs)
    assert result.usable, result.record
    off = result.offsets_ms
    assert abs(sum(off.values())) <= 2  # 평균 0 게이지
    # cam_C 기록 시각이 150 ms 늦으므로 보정값은 다른 카메라보다 150 ms 작다
    for c in ("cam_A", "cam_B", "cam_D"):
        assert abs((off["cam_C"] - off[c]) + 150) <= 6, off
    st = result.record["cameras"]["cam_C"]
    assert st["score_px"]["best"] < st["score_px"]["best_minus_50ms"] and st["score_px"]["best"] < st["score_px"]["best_plus_50ms"]


def test_offsets_recovered_after_retiming_a_broken_timeline(rigs):
    # 깨진 시각선(cam_B) + 상수 어긋남(cam_D +90 ms): 다시 매긴 뒤 보정값으로 카메라 사이 시각이 맞는다
    rows = _rows(rigs, {"cam_B": _sawtooth, "cam_D": lambda seq: 90.0}, noise_px=1.0)
    fixed, _ = repair_timelines(rows)
    result = estimate_time_offsets(_samples(rigs, fixed), rigs)
    assert result.usable
    true_t = {r["camera_id"]: [] for r in rows}
    for r_old, r_new in zip(rows, fixed, strict=True):
        cid = r_old["camera_id"]
        truth = T0 + 9.0 * sorted(rigs).index(cid) + r_old["frame_sequence"] * PERIOD_MS
        true_t[cid].append(r_new["estimated_capture_time_ms"] + result.offsets_ms[cid] - truth)
    # 보정 뒤 남은 시각 오차(카메라별 평균)가 카메라 사이에서 6 ms 안으로 같다(모두 같이 밀린 몫은 기하와 무관)
    means = [float(np.mean(v)) for v in true_t.values()]
    assert max(means) - min(means) <= 6, means


def test_already_synchronised_cameras_get_zero(rigs):
    result = estimate_time_offsets(_samples(rigs, _rows(rigs, noise_px=1.0)), rigs)
    assert result.usable
    assert all(abs(v) <= 4 for v in result.offsets_ms.values()), result.offsets_ms


def test_two_cameras_cannot_be_offset_and_nothing_is_applied(rigs):
    two = {c: rigs[c] for c in ("cam_A", "cam_B")}
    result = estimate_time_offsets(_samples(two, _rows(two)), two)
    assert not result.usable and "3대 미만" in result.record["reason"]
    assert set(result.offsets_ms) == {"cam_A", "cam_B"} and all(v is None for v in result.offsets_ms.values())


def test_too_few_points_leaves_offset_unknown(rigs):
    rows = _rows(rigs)
    result = estimate_time_offsets(_samples(rigs, rows), rigs, TimeOffsetConfig(min_points=10**7))
    assert not result.usable
    assert all(v["reason"] for v in result.record["cameras"].values())


def test_build_observations_applies_offsets(rigs):
    rows = _rows(rigs, {"cam_C": lambda seq: 150.0})
    header = _header(rigs)
    plain = build_observations(header, rows)
    shifted = build_observations(header, rows, time_offsets_ms={"cam_C": -150})
    assert plain.record["time_offsets_ms"] is None and shifted.record["time_offsets_ms"] == {"cam_C": -150}
    # 옮긴 cam_C 의 같은 시각 관측은 다른 카메라와 같은 순간의 사람을 본다: 엉덩이 중점 위치가 옮기기 전과 다르다
    t = shifted.observations[len(shifted.observations) // 2].t
    a = {o.t: o for o in plain.observations}[t].views["cam_C"][11]
    b = {o.t: o for o in shifted.observations}[t].views["cam_C"][11]
    assert math.hypot(a[0] - b[0], a[1] - b[1]) > 5
