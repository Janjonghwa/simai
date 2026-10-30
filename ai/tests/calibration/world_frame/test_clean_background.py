"""MapAnything known-K 입력 준비(-358): 관제 중 카메라별 사람 지운 배경 누적기(`CleanBackgroundAccumulator`).

- 합성 배경 위를 움직이는 사람의 박스를 넓혀 가린 뒤 픽셀별 중앙값으로 사람이 깨끗이 지워지는지 확인
- 계속 한 자리에 서 있는 사람의 영역은 "못 채운 비율"(`never_revealed_ratio`)로 보고되고 준비 미달(`ready=False`)이 되는지 확인
- `reset_camera` 로 특정 카메라만 비워지고 다시 쌓을 수 있는지 확인
- 카메라별 최대 표본 수(`max_samples_per_camera`)와 전체 메모리 상한(`max_bytes`)을 넘을 때 오래된 표본부터 버리는지 확인
- 내보내기(`export` / `export_prepared`)가 배경 사진과 메타 JSON(해상도, 표본 수, 시각 범위, 못 채운 비율, 보정 버전)을 만들고
  `world_frame.prepare.prepare_inputs` 와 바로 맞물리는지 확인
"""

from __future__ import annotations

import json

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")  # extras calibration (-348)
from simai_ai.calibration.world_frame.clean_background import (  # noqa: E402
    CLEAN_BACKGROUND_FORMAT,
    CLEAN_BACKGROUND_VERSION,
    CleanBackgroundAccumulator,
    CleanBackgroundConfig,
)
from simai_ai.calibration.world_frame.inputs import load_prepared  # noqa: E402
from simai_ai.calibration.world_frame.prepare import prepare_inputs  # noqa: E402
from simai_ai.tracking.local_tracker import (  # noqa: E402
    Observation2DFrame,
    PoseDetection,
    TrackedDetection,
)

from .conftest import intrinsics_record  # noqa: E402

PROV = {"frames": {"source": "SIM", "dataset_id": "sim/clean-background"}, "is_upper_bound": True}
SIZE = (320, 180)  # (W, H)


def _background(w: int = SIZE[0], h: int = SIZE[1]) -> np.ndarray:
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack(
        [
            60 + ((x * 3 + y) % 120),
            90 + ((x + y * 2) % 110),
            120 + ((x * 2 + y * 3) % 100),
        ],
        axis=-1,
    )
    return img.astype(np.uint8)


def _draw_person(bg: np.ndarray, box: tuple[float, float, float, float], color: tuple[int, int, int] = (10, 240, 250)) -> np.ndarray:
    frame = bg.copy()
    x1, y1, x2, y2 = (int(round(v)) for v in box)
    frame[max(0, y1) : min(frame.shape[0], y2), max(0, x1) : min(frame.shape[1], x2)] = color
    return frame


def test_moving_person_is_removed_and_status_reports_metrics():
    w, h = SIZE
    bg = _background(w, h)
    cfg = CleanBackgroundConfig(
        sample_interval_ms=1_000,
        min_samples_per_camera=6,
        max_samples_per_camera=10,
        pad_x_ratio=0.15,
        pad_y_ratio=0.10,
    )
    acc = CleanBackgroundAccumulator(cfg, camera_ids=("cam_A",))

    # 8장 동안 사람이 가로로 이동해 모든 픽셀이 최소 6번 이상 드러난다
    for i in range(8):
        x1 = 10 + i * 36
        box = (float(x1), 40.0, float(x1 + 24), 130.0)
        frame = _draw_person(bg, box)
        accepted = acc.add_frame("cam_A", 1_000_000 + i * 1_000, frame, [box])
        assert accepted is True

    # 간격 미달 프레임과 과거 시각 프레임은 채택하지 않는다
    assert acc.add_frame("cam_A", 1_007_500, bg, []) is False
    assert acc.add_frame("cam_A", 1_006_000, bg, []) is False

    st = acc.camera_status("cam_A")
    assert st.sample_count == 8
    assert st.time_range_ms == (1_000_000, 1_007_000)
    assert st.never_revealed_ratio == 0.0
    assert 0.02 < (st.mean_person_cover_ratio or 0.0) < 0.15
    assert st.image_size == (w, h)
    assert st.ready is True and acc.is_ready() is True

    synthesized, _ = acc.synthesize_camera("cam_A")
    assert np.array_equal(synthesized, bg)


def test_stationary_person_reported_as_never_revealed_and_blocks_ready():
    w, h = SIZE
    bg = _background(w, h)
    cfg = CleanBackgroundConfig(
        sample_interval_ms=1_000,
        min_samples_per_camera=5,
        max_samples_per_camera=10,
        max_unrevealed_ratio=0.02,
    )
    acc = CleanBackgroundAccumulator(cfg)

    # 폭 60 × 높이 80 박스(넓힌 영역이 전체 320×180 의 약 11.7% > 2%)에 사람이 내내 서 있다
    stationary_box = (100.0, 40.0, 160.0, 120.0)
    for i in range(6):
        frame = _draw_person(bg, stationary_box)
        obs = Observation2DFrame(
            camera_id="cam_A",
            stream_epoch=0,
            frame_sequence=i,
            estimated_capture_time_ms=10_000 + i * 1_000,
            timestamp_source="SIM",
            timestamp_uncertainty_ms=0.0,
            detections=(
                TrackedDetection(
                    local_track_id="cam_A-t1",
                    detection=PoseDetection(
                        bbox=stationary_box,
                        score=0.95,
                        keypoints_2d=((130.0, 80.0, 0.9),) * 17,
                    ),
                ),
            ),
        )
        assert acc.add_frame("cam_A", obs.estimated_capture_time_ms, frame, obs) is True

    st = acc.camera_status("cam_A")
    assert st.sample_count == 6
    assert st.never_revealed_ratio is not None and st.never_revealed_ratio > 0.08
    assert st.ready is False
    assert acc.is_camera_ready("cam_A") is False
    assert acc.is_ready() is False


def test_reset_camera_clears_only_target_camera():
    bg = _background()
    cfg = CleanBackgroundConfig(sample_interval_ms=500, min_samples_per_camera=2, max_samples_per_camera=5)
    acc = CleanBackgroundAccumulator(cfg)

    for i in range(3):
        acc.add_frame("cam_A", 1_000 + i * 500, bg, [])
        acc.add_frame("cam_B", 1_000 + i * 500, bg, [])

    assert acc.is_ready() is True
    acc.reset_camera("cam_A")
    assert acc.camera_status("cam_A").sample_count == 0
    assert acc.camera_status("cam_A").ready is False
    assert acc.camera_status("cam_B").sample_count == 3
    assert acc.camera_status("cam_B").ready is True
    assert acc.is_ready() is False

    # DRIFTED 후 해상도가 바뀌어도 reset_camera 뒤에는 새 해상도로 다시 쌓을 수 있다
    small_bg = _background(160, 90)
    assert acc.add_frame("cam_A", 5_000, small_bg, []) is True
    assert acc.camera_status("cam_A").image_size == (160, 90)


def test_memory_cap_and_max_samples_evict_oldest_samples():
    w, h = 100, 80
    bg = _background(w, h)
    # 한 표본 크기 = 100*80*3 (24,000) + 100*80*1 (8,000) = 32,000 bytes
    sample_bytes = w * h * 4
    cfg = CleanBackgroundConfig(
        sample_interval_ms=100,
        min_samples_per_camera=2,
        max_samples_per_camera=4,
        max_bytes=sample_bytes * 5,  # 두 카메라 합쳐 최대 5장만 수용
    )
    acc = CleanBackgroundAccumulator(cfg)

    for i in range(4):
        acc.add_frame("cam_A", 1_000 + i * 100, bg, [])
    assert acc.camera_status("cam_A").sample_count == 4
    assert acc.camera_status("cam_A").time_range_ms == (1_000, 1_300)

    # cam_A 에 5번째를 넣으면 max_samples_per_camera(4) 로 가장 오래된 1_000 ms 가 버려진다
    acc.add_frame("cam_A", 1_400, bg, [])
    assert acc.camera_status("cam_A").sample_count == 4
    assert acc.camera_status("cam_A").time_range_ms == (1_100, 1_400)

    # cam_B 에 3장을 넣으면 전체 7장 > 5장 상한이므로 오래된 표본이 버려져 총 바이트가 max_bytes 이하로 유지된다
    for i in range(3):
        acc.add_frame("cam_B", 2_000 + i * 100, bg, [])
    assert acc.total_bytes <= cfg.max_bytes
    total_samples = acc.camera_status("cam_A").sample_count + acc.camera_status("cam_B").sample_count
    assert total_samples == 5
    assert acc.camera_status("cam_A").time_range_ms == (1_300, 1_400)
    assert acc.camera_status("cam_B").time_range_ms == (2_000, 2_200)


def test_export_and_prepare_inputs_integration(tmp_path):
    w, h = SIZE
    bg_a = _background(w, h)
    bg_b = np.flip(bg_a, axis=1).copy()
    cfg = CleanBackgroundConfig(sample_interval_ms=1_000, min_samples_per_camera=4, max_samples_per_camera=8)
    acc = CleanBackgroundAccumulator(cfg, camera_ids=("cam_A", "cam_B"))

    for i in range(5):
        box = (float(20 + i * 50), 30.0, float(45 + i * 50), 110.0)
        acc.add_frame("cam_A", 10_000 + i * 1_000, _draw_person(bg_a, box), [box])
        acc.add_frame("cam_B", 10_000 + i * 1_000, _draw_person(bg_b, box), [box])

    rec = intrinsics_record(size=SIZE, cams=("cam_A", "cam_B"))
    bg_dir = tmp_path / "clean_bg"
    prep_dir = tmp_path / "prepared"

    exported, prepared_doc = acc.export_prepared(
        bg_dir,
        prep_dir,
        rec,
        calibration_version="cal-live-007",
        input_provenance=PROV,
        segmenter=None,
    )

    assert exported.meta_path.is_file()
    meta = json.loads(exported.meta_path.read_text(encoding="utf-8"))
    assert meta["format"] == CLEAN_BACKGROUND_FORMAT
    assert meta["format_version"] == CLEAN_BACKGROUND_VERSION
    assert meta["calibration_version"] == "cal-live-007"
    assert meta["ready"] is True
    for cid in ("cam_A", "cam_B"):
        cmeta = meta["cameras"][cid]
        assert cmeta["image_size"] == list(SIZE)
        assert cmeta["sample_count"] == 5
        assert cmeta["time_range_ms"] == [10_000, 14_000]
        assert cmeta["never_revealed_ratio"] == 0.0
        assert cmeta["calibration_version"] == "cal-live-007"
        assert (bg_dir / cmeta["image"]).is_file()

    # prepare.py 출력 형식(`simai.mapanything_inputs`)과 직접 호환되는지 검증
    loaded_prep = load_prepared(prep_dir)
    assert loaded_prep["format"] == "simai.mapanything_inputs"
    assert [v["camera_id"] for v in prepared_doc["views"]] == ["cam_A", "cam_B"]

    # 덮어쓰기 금지 및 미준비 상태에서 require_ready=True 거부 확인
    with pytest.raises(FileExistsError):
        acc.export(bg_dir, calibration_version="cal-live-007")

    acc.reset_camera("cam_A")
    acc.add_frame("cam_A", 20_000, bg_a, [])
    with pytest.raises(ValueError, match="충분하지 않다"):
        acc.export(tmp_path / "clean_bg_unready", calibration_version="cal-live-007", require_ready=True)
