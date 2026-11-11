"""관제 화면 바닥 감시 범위 시험 (S15P31A101-606, 명세 4.4·5.2): 합성 점구름의 바닥 영역 추출과 합성 카메라 배치의 칸별 카메라 수."""

from __future__ import annotations

import numpy as np
import pytest

from simai_ai.calibration.coverage import (
    CoverageConfig,
    coverage_guidance,
    floor_camera_counts,
)
from simai_ai.calibration.floor_coverage import (
    FLOOR_SOURCE,
    FloorMask,
    floor_coverage_from_points,
    floor_coverage_record,
    floor_mask_from_points,
)
from simai_ai.providers.contract import schema_errors

FLOOR_SCHEMA = {"$ref": "https://simai.dev/contracts/messages/site_setup.schema.json#/$defs/floor_coverage"}


def _floor_points(x0, x1, y0, y1, step=0.05, z=0.0, rng=None):
    xs, ys = np.meshgrid(np.arange(x0, x1, step), np.arange(y0, y1, step))
    pts = np.column_stack([xs.ravel(), ys.ravel(), np.full(xs.size, z)])
    if rng is not None:
        pts[:, 2] += rng.normal(0.0, 0.01, len(pts))
    return pts


@pytest.fixture
def doc(cameras):
    return {"calibration_version": "cal-1", "cameras": [c.to_contract() for c in cameras]}


def test_floor_mask_keeps_largest_region_and_fills_holes():
    rng = np.random.default_rng(0)
    room = _floor_points(0.0, 8.0, 0.0, 6.0, rng=rng)
    # 책상 밑(가운데 1 m × 1 m)은 카메라에 바닥 점이 안 잡힌다
    hole = (room[:, 0] > 3.5) & (room[:, 0] < 4.5) & (room[:, 1] > 2.5) & (room[:, 1] < 3.5)
    room = room[~hole]
    far = _floor_points(20.0, 20.5, 20.0, 20.5)  # 떨어진 작은 바닥 조각(창밖 등)은 버린다
    walls = np.column_stack([np.full(500, 0.0), np.linspace(0, 6, 500), np.linspace(0.2, 2.5, 500)])
    table = _floor_points(1.0, 2.0, 1.0, 2.0, z=0.75)  # 책상 윗면은 바닥이 아니다
    mask = floor_mask_from_points(np.vstack([room, far, walls, table]))
    assert mask is not None
    assert mask.origin_xy == pytest.approx((0.0, 0.0))
    assert mask.mask.shape == (60, 80)  # 6 m × 8 m, 10 cm 칸. 먼 조각이 경계를 넓히지 않는다
    assert mask.mask.all()  # 구멍은 방 안이라 메운다
    assert mask.area_m2 == pytest.approx(48.0)


def test_floor_mask_closes_sparse_rows_but_not_beyond_observed_edge():
    # 먼 바닥처럼 줄 간격 25 cm 로 성긴 점도 한 영역으로 잇고, 관측한 바깥으로는 늘리지 않는다
    xs, ys = np.meshgrid(np.arange(0.0, 4.0, 0.05), np.arange(0.0, 4.0, 0.25))
    pts = np.column_stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)])
    mask = floor_mask_from_points(pts)
    assert mask is not None
    assert mask.mask.all()
    assert mask.mask.shape[1] == 40 and mask.origin_xy[0] == pytest.approx(0.0)


def test_floor_mask_unknown_is_empty():
    assert floor_mask_from_points(np.empty((0, 3))) is None
    assert floor_mask_from_points(_floor_points(0, 8, 0, 6, z=0.5)) is None  # 바닥 높이 점이 없다
    assert floor_mask_from_points(_floor_points(0, 0.5, 0, 0.5)) is None  # 1 m² 미만
    assert floor_mask_from_points(np.array([[np.nan, 0.0, 0.0]] * 10)) is None


def test_camera_counts_match_coverage_guidance(doc):
    poly = [(0.0, 0.0), (8.0, 0.0), (8.0, 6.0), (0.0, 6.0)]
    one = {"cameras": doc["cameras"][:1]}
    report = coverage_guidance(one, poly)
    xs = np.arange(0.25, 8.0, 0.5)
    ys = np.arange(0.25, 6.0, 0.5)
    cells = np.array([(x, y) for x in xs for y in ys])
    counts = floor_camera_counts(one, cells)
    assert {k: int((counts == k).sum()) for k in np.unique(counts)} == dict(report.cells_by_camera_count)
    assert floor_camera_counts(doc, np.empty((0, 2))).shape == (0,)


def test_record_encodes_classes_rows_from_min_y(doc):
    # 바닥 칸 둘: cam1 바로 밑 모서리(행 0 = 가장 작은 Y, 열 0)와 (2.85, 2.85) 의 (행 9, 열 9). 나머지는 바닥 모름
    mask = np.zeros((30, 30), bool)
    mask[0:3, 0:3] = True
    mask[27:30, 27:30] = True
    floor = FloorMask(origin_xy=(0.0, 0.0), cell_m=0.1, mask=mask)
    only_cam1 = {"calibration_version": "cal-1", "cameras": doc["cameras"][:1]}
    rec = floor_coverage_record(only_cam1, floor)
    assert rec is not None
    assert (rec["cols"], rec["rows"], rec["cell_m"], rec["origin_xy"]) == (10, 10, 0.3, [0.0, 0.0])
    # cam1 바로 밑은 발이 화면 아래로 잘려 사각(0), 방 쪽 칸은 cam1 한 대만(1)
    assert rec["cells"] == "0" + "." * 98 + "1"
    assert schema_errors(rec, FLOOR_SCHEMA) == []
    assert floor_coverage_record(doc, floor)["cells"] == "2" + "." * 98 + "2"  # 다른 세 대가 모서리를 본다


def test_record_from_synthetic_room(doc):
    room = _floor_points(0.0, 8.0, 0.0, 6.0, rng=np.random.default_rng(1))
    rec = floor_coverage_from_points(doc, room)
    assert rec is not None
    assert schema_errors(rec, FLOOR_SCHEMA) == []
    assert rec["calibration_version"] == "cal-1" and rec["floor_source"] == FLOOR_SOURCE and rec["is_upper_bound"] is True
    assert (rec["cols"], rec["rows"]) == (27, 20)  # 8 m × 6 m, 30 cm 칸
    assert rec["cells"] == "2" * 540  # 네 모서리에서 가운데를 보는 교실은 어디서나 2대 이상
    assert (rec["ankle_height_m"], rec["head_height_m"]) == (CoverageConfig().ankle_height_m, CoverageConfig().head_height_m)
    one = floor_coverage_from_points({"calibration_version": "cal-1", "cameras": doc["cameras"][:1]}, room)
    assert set(one["cells"]) == {"0", "1"}
    assert one["cells"][0] == "0"  # cam1 바로 밑 모서리는 사각


def test_record_none_when_nothing_to_draw(doc):
    floor = FloorMask(origin_xy=(0.0, 0.0), cell_m=0.1, mask=np.eye(3, dtype=bool))  # 과반 미달
    assert floor_coverage_record(doc, floor) is None
    with pytest.raises(ValueError):
        floor_coverage_record(doc, floor, config=CoverageConfig(min_cameras=3))
