"""지지 표면 후보 시험: 구성 묶음 Operational Geometry 의 등록 작업면 읽기 (명세 7.5, 3.2)."""

import math
from types import SimpleNamespace

import pytest

from simai_ai.config.bundle import freeze_json
from simai_ai.contact import FLOOR_ID, SupportSurface, SurfaceSet

PODIUM = {"surface_id": "podium-1", "top_height_m": 0.4, "center_xy": [4.5, 6.75], "size_xy": [4.0, 1.1]}


def test_reads_work_surfaces_from_frozen_bundle_geometry():
    geometry = freeze_json(
        {
            "version": "geom-7",
            "work_surfaces": [
                PODIUM,
                {**PODIUM, "surface_id": "desk-1", "top_height_m": 0.74, "yaw_rad": 0.5, "top_height_sigma_m": 0.01},
            ],
            "static_objects": [],
        }
    )
    s = SurfaceSet.from_operational_geometry(geometry)
    assert s.geometry_version == "geom-7" and s.rejected == ()
    assert [x.surface_id for x in s.surfaces] == [FLOOR_ID, "podium-1", "desk-1"]
    desk = s.get("desk-1")
    assert (desk.top_z, desk.sigma_z, desk.yaw_rad) == (0.74, 0.01, 0.5)
    assert s.get("podium-1").sigma_z == pytest.approx(0.02)


def test_from_active_config_uses_bundle_geometry_or_floor_only():
    assert SurfaceSet.from_active_config(None).surfaces == SurfaceSet.floor_only().surfaces
    assert SurfaceSet.from_active_config(None).geometry_version is None
    bundle = SimpleNamespace(operational_geometry=freeze_json({"version": "geom-9", "work_surfaces": [PODIUM]}),
                             geometry_version="geom-9")
    s = SurfaceSet.from_active_config(SimpleNamespace(bundle=bundle))  # ActiveConfig 와 같은 모양
    assert s.geometry_version == "geom-9" and s.get("podium-1") is not None


@pytest.mark.parametrize(
    "item, why",
    [
        ({"surface_id": "desk-1", "top_height_m": 0.74}, "center_xy"),  # footprint 없음 (config 시험의 최소 형식)
        ({**PODIUM, "surface_id": FLOOR_ID}, "바닥"),
        ({**PODIUM, "top_height_m": math.nan}, "top_height_m"),
        ({**PODIUM, "top_height_m": 0.0}, "바닥보다"),
        ({**PODIUM, "size_xy": [4.0, -1.0]}, "size_xy"),
        ({**PODIUM, "center_xy": [1.0]}, "center_xy"),
        ({**PODIUM, "surface_id": ""}, "surface_id"),
        ("podium", "객체"),
    ],
)
def test_malformed_work_surfaces_are_rejected_not_guessed(item, why):
    s = SurfaceSet.from_operational_geometry({"version": "g", "work_surfaces": [item]})
    assert s.work_surfaces == ()
    assert len(s.rejected) == 1 and why in s.rejected[0][1]


def test_duplicate_id_keeps_first_only():
    s = SurfaceSet.from_operational_geometry({"version": "g", "work_surfaces": [PODIUM, {**PODIUM, "top_height_m": 0.2}]})
    assert [x.top_z for x in s.work_surfaces] == [0.4]
    assert s.rejected == (("podium-1", "surface_id 가 겹친다"),)


def test_obb_containment_with_yaw_and_margin():
    s = SupportSurface("desk", 0.74, center_xy=(0.0, 0.0), size_xy=(2.0, 1.0), yaw_rad=math.pi / 2)
    assert s.contains_xy((0.0, 0.9)) and not s.contains_xy((0.9, 0.0))  # 90° 돌아 긴 변이 y 축
    assert s.contains_xy((0.55, 0.0), margin_m=0.1) and not s.contains_xy((0.65, 0.0), margin_m=0.1)
    floor = SurfaceSet().get(FLOOR_ID)
    assert floor.contains_xy((100.0, -5.0))


def test_surface_invariants():
    with pytest.raises(ValueError):
        SupportSurface(FLOOR_ID, 0.1)
    with pytest.raises(ValueError):
        SupportSurface("desk", 0.7)  # footprint 없음
    with pytest.raises(ValueError):
        SurfaceSet((SupportSurface("floor-x", 0.4, center_xy=(0, 0), size_xy=(1, 1)),) * 2)
