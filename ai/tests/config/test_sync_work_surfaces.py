"""활성 구성 묶음의 등록 작업면이 R2·R3 판정에 들어간다 (명세 7.5, 3.2 구성 묶음: 한 tick 의 구역·규칙·작업면은 한 묶음)."""

from simai_ai.config import ConfigSync
from simai_ai.risk import Origin, WorldState

PODIUM = {"surface_id": "podium-1", "top_height_m": 0.4, "center_xy": [4.5, 6.75], "size_xy": [4.0, 1.1]}


def _world(t):
    return WorldState.from_payload(
        {
            "t_latest_global": t,
            "calibration_version": "cal-12",
            "scene_version": "scene-1",
            "tracks": [
                {
                    "world_id": "w-1",
                    "t_last_valid": t,
                    "observation_state": "OBSERVED",
                    "track_lifecycle": "CONFIRMED",
                    "identity_confidence": "HIGH",
                    "identity_revision": 0,
                    "position_xy": [4.5, 6.75],
                    "position_xy_sigma": [0.05, 0.05],
                    "pelvis_xyz": [4.5, 6.75, 1.3],
                    "ground_anchor_xyz": [4.5, 6.75, 0.4],
                    "ground_anchor_sigma": [0.05, 0.05, 0.02],
                    "support_surface_id": "podium-1",
                    "support_surface_validity": "VALID",
                    "contact_validity": "VALID",
                    "z_validity": "VALID",
                    "head_z": 2.1,
                    "cameras": ["cam1", "cam2"],
                }
            ],
        }
    )


def test_active_config_passes_work_surfaces_to_height_rules(kit):
    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01")
    sync.on_connected()
    geometry = {"version": "geom-12", "work_surfaces": [PODIUM, {"surface_id": "bad", "top_height_m": 0.4}]}
    assert sync.receive(kit.publish("cfg-12", rule_versions=(("R2", 1001), ("R3", 1001)), geometry=geometry)).accepted
    kit.clock.advance(10)
    active = sync.begin_tick()
    assert [s.surface_id for s in active.work_surfaces.surfaces] == ["podium-1"]
    assert [label for label, _ in active.work_surfaces.rejected] == ["bad"]  # ContactEstimator 와 같이 뺀다
    assert active.work_surfaces.geometry_version == "geom-12"
    evaluation = active.evaluate(_world(kit.clock.now_ms() - 50), Origin.LIVE)
    assert sorted((f.rule_id, f.rule_version) for f in evaluation.findings) == [("R2", 1001), ("R3", 1001)]


def test_without_work_surfaces_height_rules_have_no_basis(kit):
    sync = ConfigSync(kit.verifier(), kit.clock, source_id="tier0-01")
    sync.on_connected()
    geometry = {"version": "geom-12", "work_surfaces": []}
    assert sync.receive(kit.publish("cfg-12", rule_versions=(("R2", 1001), ("R3", 1001)), geometry=geometry)).accepted
    kit.clock.advance(10)
    active = sync.begin_tick()
    # 접지점 Z 0.4 는 알지만(R3 는 바닥 기준 높이차로 판정) 가장자리는 등록 작업면이 없어 R2 근거가 없다
    evaluation = active.evaluate(_world(kit.clock.now_ms() - 50), Origin.LIVE)
    assert [(f.rule_id, f.rule_version) for f in evaluation.findings] == [("R3", 1001)]
