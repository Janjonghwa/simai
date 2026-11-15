"""ContactEstimator 단위 시험 (명세 5.2 접지 추정·World 위치의 정의, 7.5 작업면, 부록 D-G2-03·05·07).

합성 World Track 결과(골격은 관절 유효성 게이트 결과 형식)를 넣는다. 지지 표면을 모르면 접지점을 비우고 Z=0 으로 채우지
않는지(AGENTS 규칙 2), 단상 위 사람을 바닥으로 바꾸지 않는지, 가림 유지와 그 시간 상한을 본다.
"""

import uuid

import pytest

from simai_ai.contact import (
    ContactConfig,
    ContactEstimator,
    ContactReason,
    SupportSurface,
    SurfaceSet,
    SurfaceSource,
)
from simai_ai.geometry.joint_gate import GatedJoint, GatedSkeleton
from simai_ai.geometry.triangulation import JointValidity
from simai_ai.tracking.world_state import world_state_message, world_state_payload
from simai_ai.tracking.world_track import (
    IdentityConfidence,
    TrackLifecycle,
    WorldTrackFrame,
    WorldTrackSnapshot,
)

T = 1_760_342_400_000
DT = 100
WID = "w-test-0001"
V, U, N = JointValidity.VALID, JointValidity.UNCERTAIN, JointValidity.UNAVAILABLE
PODIUM = SupportSurface("podium-1", 0.4, center_xy=(4.5, 6.75), size_xy=(4.0, 1.1))
SURFACES = SurfaceSet((PODIUM,), "geom-1")
ON_PODIUM = (4.0, 6.7)
ON_FLOOR = (4.0, 4.0)


def _skeleton(t, xy, surface_z, *, ankles=(V, V), ankle_dz=(0.0, 0.0), pelvis=V, pelvis_dz=0.0, sigma=0.01):
    """서 있는 사람 골격: 발목 = 표면 + 0.08 (+ankle_dz), 엉덩이 = 표면 + 0.95 (+pelvis_dz)."""
    x, y = xy
    joints = [GatedJoint(None, N)] * 17
    s = (sigma, sigma, sigma)
    if pelvis is not N:
        joints[11] = GatedJoint((x - 0.1, y, surface_z + 0.95 + pelvis_dz), pelvis, sigma_xyz_m=s)
        joints[12] = GatedJoint((x + 0.1, y, surface_z + 0.95 + pelvis_dz), pelvis, sigma_xyz_m=s)
    for j, side, validity, dz in ((15, -0.1, ankles[0], ankle_dz[0]), (16, 0.1, ankles[1], ankle_dz[1])):
        if validity is not N:
            joints[j] = GatedJoint((x + side, y, surface_z + 0.08 + dz), validity, sigma_xyz_m=s)
    return GatedSkeleton(t, "COCO17", tuple(joints), "cal-1", ("cam1", "cam2", "cam3"))


def _snap(t, skeleton=None, *, world_id=WID, lifecycle=TrackLifecycle.CONFIRMED, revision=0, xy=(4.0, 6.7)):
    pelvis = skeleton.pelvis_xyz() if skeleton is not None else None
    updated = pelvis is not None and skeleton.pelvis_validity() is not N
    return WorldTrackSnapshot(
        world_id=world_id,
        t=t,
        t_last_valid=t if updated else T,
        lifecycle=lifecycle,
        identity_confidence=IdentityConfidence.HIGH,
        identity_revision=revision,
        position_xy=(pelvis[0], pelvis[1]) if pelvis is not None else xy,
        position_xy_sigma=(0.03, 0.03),
        velocity=(0.0, 0.0, 0.0),
        pelvis_xyz=pelvis if updated else None,
        skeleton=skeleton,
    )


def _step(est, t, skeleton=None, surfaces=SURFACES, **kw):
    frame = WorldTrackFrame(t, "cal-1", (_snap(t, skeleton, **kw),))
    return est.update(frame, surfaces).estimates[kw.get("world_id", WID)]


def _stand(est, t0, xy, z, frames, surfaces=SURFACES, **kw):
    """frames 번 서 있는 골격을 넣고 마지막 판단을 돌려준다."""
    e = None
    for k in range(frames):
        e = _step(est, t0 + k * DT, _skeleton(t0 + k * DT, xy, z, **kw), surfaces)
    return e


def test_floor_contact_needs_confirmation_then_valid():
    est = ContactEstimator()
    first = _step(est, T, _skeleton(T, ON_FLOOR, 0.0))
    c = first.contact
    assert (c.support_surface_validity, c.support_surface_id, c.ground_anchor_xyz) == ("UNCERTAIN", "floor", None)
    assert c.z_validity == "UNAVAILABLE" and ContactReason.PENDING_CONFIRMATION in first.reasons
    second = _step(est, T + DT, _skeleton(T + DT, ON_FLOOR, 0.0))
    c = second.contact
    assert (c.support_surface_validity, c.support_surface_id, c.contact_validity, c.z_validity) == (
        "VALID",
        "floor",
        "VALID",
        "VALID",
    )
    assert c.ground_anchor_xyz == pytest.approx((4.0, 4.0, 0.0))
    assert second.surface_source is SurfaceSource.OBSERVED and c.observed_at == T + DT
    assert c.observed_xy == pytest.approx((4.0, 4.0))
    assert c.ground_anchor_sigma[2] == pytest.approx(0.03)  # 바닥 확정의 높이 분해능 (FLOOR_SIGMA_Z_M)


def test_registered_podium_top_is_support_surface_not_floor():
    est = ContactEstimator()
    e = _stand(est, T, ON_PODIUM, 0.4, 5)
    c = e.contact
    assert (c.support_surface_id, c.support_surface_validity) == ("podium-1", "VALID")
    assert c.ground_anchor_xyz[2] == pytest.approx(0.4)  # 상판 높이 (발목 Z 에서 빼지 않는다)
    assert e.geometry_version == "geom-1"


def test_unregistered_podium_stays_unknown_never_floor():
    """작업면이 등록되지 않았으면 단상 위 사람은 UNKNOWN 이다. 바닥으로도 Z=0 으로도 채우지 않는다."""
    est = ContactEstimator()
    for k in range(10):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4), SurfaceSet.floor_only())
        c = e.contact
        assert c.ground_anchor_xyz is None and c.support_surface_id is None
        assert (c.support_surface_validity, c.z_validity) == ("UNKNOWN", "UNAVAILABLE")
        assert ContactReason.NO_MATCHING_SURFACE in e.reasons


def test_ankles_not_valid_gives_no_evidence():
    est = ContactEstimator()
    for k, ankles in enumerate([(U, U), (N, N), (U, N)]):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, ankles=ankles))
        assert e.contact.ground_anchor_xyz is None and e.contact.support_surface_validity == "UNKNOWN"
        assert ContactReason.NO_VALID_ANKLE in e.reasons


def test_single_view_podium_person_holds_height_not_zero():
    """한 카메라에만 보여 골격이 없는 단상 위 사람: 마지막 유효 높이 유지, Z=0 으로 내려앉지 않음 (D-G2-03)."""
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 3)
    last = T + 2 * DT
    sigmas = []
    for k in range(1, 11):
        t = last + k * DT
        e = _step(est, t)  # 골격 없음 (1대 관측 클러스터는 위치를 갱신하지 않는다)
        c = e.contact
        assert (c.support_surface_id, c.support_surface_validity) == ("podium-1", "VALID")
        assert (c.z_validity, c.contact_validity) == ("UNCERTAIN", "UNCERTAIN")
        assert c.ground_anchor_xyz[2] == pytest.approx(0.4)
        assert e.surface_source is SurfaceSource.INFERRED
        assert c.observed_at == last  # 마지막으로 발목으로 확인한 시각 (유지 중에는 늘지 않는다)
        assert ContactReason.HOLD in e.reasons and ContactReason.TRACK_NOT_UPDATED in e.reasons
        sigmas.append(c.ground_anchor_sigma)
    assert all(b[2] > a[2] and b[0] > a[0] for a, b in zip(sigmas, sigmas[1:]))  # 불확실성이 커진다
    assert sigmas[-1][2] == pytest.approx(0.02 + 0.1 * 1.0)


def test_hold_upper_bound_boundary():
    """유지 상한 경계: 마지막 발목 근거 뒤 정확히 hold_max_ms 는 유지, 넘으면 UNKNOWN (바닥 아님)."""
    est = ContactEstimator(ContactConfig(hold_max_ms=300))
    _stand(est, T, ON_PODIUM, 0.4, 2)
    last = T + DT
    at_bound = _step(est, last + 300)
    assert at_bound.contact.support_surface_validity == "VALID" and ContactReason.HOLD in at_bound.reasons
    over = _step(est, last + 400)
    c = over.contact
    assert ContactReason.HOLD_EXPIRED in over.reasons
    assert (c.ground_anchor_xyz, c.support_surface_validity, c.z_validity) == (None, "UNKNOWN", "UNAVAILABLE")
    later = _step(est, last + 500)
    assert later.contact.ground_anchor_xyz is None and later.contact.support_surface_id is None


def test_hold_with_hidden_ankles_uses_pelvis_xy():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, (4.3, 6.7), 0.4, ankles=(N, N)))
    c = e.contact
    assert e.surface_source is SurfaceSource.INFERRED
    assert c.ground_anchor_xyz == pytest.approx((4.3, 6.7, 0.4))
    assert c.ground_anchor_sigma[:2] == pytest.approx((0.03, 0.03))  # 골반 σ


def test_hold_broken_when_pelvis_leaves_footprint_or_drops():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, (4.0, 5.0), 0.4, ankles=(N, N)))  # 골반이 단상 밖 (발은 가려짐)
    assert ContactReason.LEFT_SURFACE_FOOTPRINT in e.reasons
    assert e.contact.ground_anchor_xyz is None and e.contact.support_surface_validity == "UNKNOWN"

    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4, ankles=(N, N), pelvis_dz=-0.4))  # 골반이 40 cm 내려감
    assert ContactReason.PELVIS_HEIGHT_CHANGED in e.reasons
    assert e.contact.ground_anchor_xyz is None


def test_stepping_down_goes_through_uncertain_not_stale_podium():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0))
    assert ContactReason.LEFT_SURFACE_FOOTPRINT in e.reasons
    assert (e.contact.support_surface_validity, e.contact.support_surface_id) == ("UNCERTAIN", "floor")
    assert e.contact.ground_anchor_xyz is None
    e = _step(est, t + DT, _skeleton(t + DT, ON_FLOOR, 0.0))
    assert (e.contact.support_surface_validity, e.contact.support_surface_id) == ("VALID", "floor")


def test_ankles_below_held_podium_contradict():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.0))  # 단상 footprint 안인데 발목이 바닥 높이 (오연결 등)
    assert ContactReason.HOLD_CONTRADICTED in e.reasons
    assert e.contact.support_surface_validity == "UNCERTAIN" and e.contact.ground_anchor_xyz is None


def test_ankle_below_floor_band_does_not_break_floor_hold():
    """-739: 바닥보다 낮은 면은 없다. 바닥 띠 아래 디딤발(삼각측량 오차)은 반증이 아니고, 다음 시각 다시 확인 없이 VALID 다."""
    est = ContactEstimator()
    _stand(est, T, ON_FLOOR, 0.0, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, ankle_dz=(-0.09, -0.07)))  # 발목 Z = -0.01, 0.01
    assert ContactReason.ANKLE_BELOW_FLOOR_BAND in e.reasons and ContactReason.HOLD_CONTRADICTED not in e.reasons
    c = e.contact
    assert (c.support_surface_id, c.z_validity, e.surface_source) == ("floor", "UNCERTAIN", SurfaceSource.INFERRED)
    assert c.ground_anchor_xyz[2] == 0.0
    e = _step(est, t + DT, _skeleton(t + DT, ON_FLOOR, 0.0))
    assert (e.contact.support_surface_validity, e.contact.z_validity) == ("VALID", "VALID")  # 다시 확인 2번이 필요 없다


def test_ankle_below_floor_band_is_not_contact_evidence():
    """띠 아래 발목은 유지 시각을 갱신하지 않는다: 그것만 이어지면 hold_max_ms 뒤 UNKNOWN(바닥으로 채우지 않음)."""
    est = ContactEstimator(ContactConfig(hold_max_ms=300))
    _stand(est, T, ON_FLOOR, 0.0, 2)
    last = T + DT
    seen = set()
    for k in range(2, 7):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, ankle_dz=(-0.1, -0.1)))
        assert (e.contact.ground_anchor_xyz is None) == (t - last > 300)
        seen.update(e.reasons)
    assert ContactReason.HOLD_EXPIRED in seen


def test_below_band_on_podium_still_contradicts_and_step_up_from_floor_is_immediate():
    """띠 아래 예외는 바닥만이다. 작업면에서 내려옴·바닥에서 올라섬은 그 시각에 유지를 끊는다(지연 없음)."""
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4, ankle_dz=(-0.09, -0.09)))
    assert ContactReason.HOLD_CONTRADICTED in e.reasons and e.contact.ground_anchor_xyz is None
    est = ContactEstimator()
    _stand(est, T, ON_FLOOR, 0.0, 2)
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4))
    assert ContactReason.HOLD_CONTRADICTED in e.reasons and e.contact.ground_anchor_xyz is None


def test_session18_floor_walker_next_to_podium_keeps_floor():
    """18차 형태(2026-11-15 재생, 단상 옆 바닥 보행자): VALID 디딤발 높이가 중앙 9 cm 근처이고 약 12 %가 바닥 띠 아래(2 cm 미만).

    -739 전에는 띠 아래 한 시각마다 바닥 유지가 끊기고 다시 잡는 데 확인 2번이 필요해 접지 모름(R2·R3 Z_UNKNOWN 근거)이 반복됐다.
    지금은 처음 확인 뒤 60 시각(6 s) 동안 접지점을 잃지 않는다. 음성 대조: 같은 높이 열을 단상 유지에 넣으면(띠 아래 = 내려옴) 끊긴다.
    """
    dz = [0.01, 0.03, -0.07, 0.0, 0.05, -0.02, 0.02, -0.075]  # 디딤발 Z = 0.09, 0.11, 0.01, 0.08, 0.13, 0.06, 0.10, 0.005
    est = ContactEstimator()
    _stand(est, T, (4.0, 5.9), 0.0, 2)  # 단상(y 6.2~7.3) 가장자리에서 0.3 m 바깥 바닥
    unknown = 0
    for k in range(2, 62):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, (4.0, 5.9), 0.0, ankle_dz=(dz[k % 8], dz[k % 8] + 0.06)))
        unknown += e.contact.ground_anchor_xyz is None
        assert e.contact.support_surface_id == "floor"
    assert unknown == 0
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4, ankle_dz=(dz[2], dz[2] + 0.06)))
    assert e.contact.ground_anchor_xyz is None


def test_raised_single_ankle_keeps_hold_but_other_surface_height_breaks_it():
    est = ContactEstimator()
    _stand(est, T, ON_FLOOR, 0.0, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, ankles=(V, N), ankle_dz=(0.2, 0.0)))  # 든 발 하나
    assert ContactReason.RAISED_SINGLE_ANKLE in e.reasons
    assert (e.contact.support_surface_id, e.surface_source) == ("floor", SurfaceSource.INFERRED)
    # 단상 footprint 안, 단상 높이의 발목 하나: 올라섰는지 모르므로 바닥 유지를 끊는다
    e = _step(est, t + DT, _skeleton(t + DT, ON_PODIUM, 0.0, ankles=(V, N), ankle_dz=(0.4, 0.0)))
    assert ContactReason.HOLD_CONTRADICTED in e.reasons
    assert e.contact.support_surface_validity == "UNCERTAIN" and e.contact.support_surface_id == "podium-1"


def test_two_raised_ankles_contradict_floor_hold():
    est = ContactEstimator()
    _stand(est, T, ON_FLOOR, 0.0, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, ankle_dz=(0.3, 0.3)))
    assert ContactReason.HOLD_CONTRADICTED in e.reasons
    assert e.contact.ground_anchor_xyz is None and e.contact.support_surface_validity == "UNKNOWN"


def test_single_ankle_needs_stationarity():
    est = ContactEstimator()
    one = dict(ankles=(V, N))
    e = _step(est, T, _skeleton(T, ON_FLOOR, 0.0, **one))
    assert ContactReason.NOT_CONFIRMED in e.reasons and e.contact.support_surface_validity == "UNCERTAIN"
    e = _step(est, T + DT, _skeleton(T + DT, ON_FLOOR, 0.0, **one))  # 정지: 확인 1회
    assert ContactReason.PENDING_CONFIRMATION in e.reasons
    e = _step(est, T + 2 * DT, _skeleton(T + 2 * DT, ON_FLOOR, 0.0, **one))
    assert e.contact.support_surface_validity == "VALID" and e.contact.contact_validity == "VALID"

    moving = ContactEstimator()
    for k in range(5):
        t = T + k * DT
        e = _step(moving, t, _skeleton(t, (1.0 + 0.15 * k, 4.0), 0.0, **one))  # 1.5 m/s
        assert e.contact.support_surface_validity == "UNCERTAIN" and e.contact.ground_anchor_xyz is None


def test_moving_match_of_held_surface_refreshes_as_uncertain_contact():
    est = ContactEstimator()
    _stand(est, T, ON_FLOOR, 0.0, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, (4.3, 4.0), 0.0, ankles=(V, N)))  # 30 cm 이동 (3 m/s): 정지 아님
    c = e.contact
    assert (c.support_surface_validity, c.contact_validity, c.z_validity) == ("VALID", "UNCERTAIN", "VALID")
    assert e.surface_source is SurfaceSource.OBSERVED and c.ground_anchor_xyz[0] == pytest.approx(4.2)


def test_ambiguous_surfaces_give_uncertain_without_anchor():
    a = SupportSurface("stage-a", 0.40, center_xy=(4.5, 6.75), size_xy=(4.0, 1.1))
    b = SupportSurface("stage-b", 0.45, center_xy=(4.5, 6.75), size_xy=(4.0, 1.1))
    est = ContactEstimator()
    for k in range(4):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, ON_PODIUM, 0.42), SurfaceSet((a, b), "geom-2"))
        assert ContactReason.AMBIGUOUS_SURFACE in e.reasons
        c = e.contact
        assert (c.support_surface_validity, c.support_surface_id, c.ground_anchor_xyz) == ("UNCERTAIN", None, None)


def test_geometry_change_drops_hold():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    moved = SurfaceSet((SupportSurface("podium-1", 0.3, center_xy=(4.5, 6.75), size_xy=(4.0, 1.1)),), "geom-2")
    e = _step(est, t, None, moved)
    assert ContactReason.SURFACE_UNREGISTERED in e.reasons
    assert e.contact.ground_anchor_xyz is None and e.geometry_version == "geom-2"


def test_identity_revision_resets_history():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    e = _step(est, T + 2 * DT, None, revision=1)
    assert ContactReason.IDENTITY_REVISED in e.reasons and e.contact.ground_anchor_xyz is None


def test_lost_and_absent_tracks_drop_state():
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, None, lifecycle=TrackLifecycle.LOST)
    assert e.contact.support_surface_validity == "VALID"  # 이번 시각은 유지로 낸다
    e = _step(est, t + DT, None)
    assert e.contact.ground_anchor_xyz is None  # LOST 뒤 같은 ID 가 와도 이력이 없다

    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    est.update(WorldTrackFrame(t, "cal-1", ()), SURFACES)
    assert _step(est, t + DT, None).contact.ground_anchor_xyz is None


def test_input_checks():
    est = ContactEstimator()
    _step(est, T, _skeleton(T, ON_FLOOR, 0.0))
    with pytest.raises(ValueError):
        _step(est, T, None)  # 시각이 늘지 않음
    snap = _snap(T + DT)
    with pytest.raises(ValueError):
        est.update(WorldTrackFrame(T + 2 * DT, "cal-1", (snap,)), SURFACES)  # Track 시각 불일치
    with pytest.raises(ValueError):
        ContactConfig(confirm_frames=0)
    with pytest.raises(ValueError):
        ContactConfig(ankle_band_m=float("nan"))


def test_world_state_with_contacts_matches_contract(contract_errors):
    """ContactEstimator 결과를 world_state 에 실은 메시지가 계약을 지킨다 (관측·유지·모름 모두)."""
    est = ContactEstimator(ContactConfig(hold_max_ms=200))
    docs = []
    skeletons = [_skeleton(T + k * DT, ON_PODIUM, 0.4) for k in range(2)] + [None] * 4
    for k, sk in enumerate(skeletons):
        t = T + k * DT
        frame = WorldTrackFrame(t, "cal-1", (_snap(t, sk),))
        contacts = est.update(frame, SURFACES).contacts()
        payload = world_state_payload(frame, t, "scene-1", contacts=contacts)
        msg = world_state_message(payload, msg_id=str(uuid.UUID(int=t)), sent_at=t)
        assert contract_errors("world_state", msg) == []
        docs.append(payload["tracks"][0])
    states = [(d["support_surface_validity"], d["z_validity"]) for d in docs]
    assert states == [
        ("UNCERTAIN", "UNAVAILABLE"),
        ("VALID", "VALID"),
        ("VALID", "UNCERTAIN"),
        ("VALID", "UNCERTAIN"),
        ("UNKNOWN", "UNAVAILABLE"),
        ("UNKNOWN", "UNAVAILABLE"),
    ]
    # 계약 1.3.0: 접지점이 있을 때만 출처를 싣는다(관측 → 유지 → 모름)
    assert [d.get("surface_source") for d in docs] == [None, "observed", "inferred", "inferred", None, None]
    for d in docs:
        assert ("surface_source" in d) == (d["ground_anchor_xyz"] is not None)
        if d["ground_anchor_xyz"] is not None:
            assert d["ground_anchor_xyz"][2] == pytest.approx(0.4) and d["ground_anchor_sigma"] is not None


# ---------------------------------------------------------------- 리뷰 반례 회귀 (F2·F3·F4)


@pytest.mark.parametrize("top", [0.10, 0.11, 0.40])
def test_unregistered_raised_top_is_never_floor(top):
    """리뷰 F2: 미등록 상판(10 cm 이상) 위 발목이 바닥 띠에 걸려 floor/VALID/Z=0 으로 확정되지 않는다."""
    est = ContactEstimator()
    for k in range(6):
        t = T + k * DT
        c = _step(est, t, _skeleton(t, ON_FLOOR, top, sigma=0.007), SurfaceSet.floor_only()).contact
        assert c.support_surface_id != "floor" and c.ground_anchor_xyz is None
        assert c.support_surface_validity == "UNKNOWN"


def test_noisy_ankle_height_is_not_floor_evidence():
    """발목 σz 가 상한(0.02 m)을 넘으면 띠가 넓어져 낮은 상판과 바닥을 가르지 못하므로 근거로 쓰지 않는다."""
    est = ContactEstimator()
    for k in range(4):
        t = T + k * DT
        e = _step(est, t, _skeleton(t, ON_FLOOR, 0.0, sigma=0.03), SurfaceSet.floor_only())
        assert e.contact.ground_anchor_xyz is None and ContactReason.NO_VALID_ANKLE in e.reasons


def test_visible_ankles_on_podium_but_pelvis_far_away_is_uncertain():
    """리뷰 F3: 발목은 단상 높이에 그대로인데 VALID 골반이 1 m 떨어진(작업면 밖) 곳: 접지점을 비우고 UNCERTAIN."""
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 3)
    t = T + 3 * DT
    sk = _skeleton(t, ON_PODIUM, 0.4)
    joints = list(sk.joints)
    for j in (11, 12):  # 골반만 Y 5.7 로 옮긴다 (발목·무릎 가림 중 상체 이동)
        x, _, z = joints[j].xyz
        joints[j] = GatedJoint((x, 5.7, z), V, sigma_xyz_m=(0.01, 0.01, 0.01))
    sk = GatedSkeleton(t, "COCO17", tuple(joints), "cal-1", sk.camera_ids)
    e = _step(est, t, sk)
    c = e.contact
    assert ContactReason.PELVIS_ANKLE_INCONSISTENT in e.reasons
    assert (c.support_surface_validity, c.ground_anchor_xyz, c.z_validity) == ("UNCERTAIN", None, "UNAVAILABLE")
    # 다음 tick 에 골반 없이 발목만 보여도 끊긴 이력을 되살리지 않고 새로 확인한다
    e = _step(est, t + DT, _skeleton(t + DT, ON_PODIUM, 0.4, pelvis=N))
    assert e.contact.support_surface_validity == "UNCERTAIN" and ContactReason.PENDING_CONFIRMATION in e.reasons


def test_crouching_on_podium_is_not_a_contradiction():
    """골반 높이만 내려간 것(웅크림)은 공간 모순이 아니다. 발목이 보이면 그대로 단상 위 VALID."""
    est = ContactEstimator()
    _stand(est, T, ON_PODIUM, 0.4, 2)
    t = T + 2 * DT
    e = _step(est, t, _skeleton(t, ON_PODIUM, 0.4, pelvis_dz=-0.45))
    assert e.contact.support_surface_validity == "VALID" and e.contact.ground_anchor_xyz[2] == pytest.approx(0.4)


def test_expired_surface_not_revived_by_single_unconfirmed_ankle_after_gap():
    """리뷰 F4: 접지 확인 뒤 입력이 끊겼다가(중간 tick 없음) 유지 상한을 넘겨 미확인 발목 하나가 보여도 VALID 로 되살리지 않는다."""
    results = []
    for with_gap_tick in (False, True):
        est = ContactEstimator(ContactConfig(hold_max_ms=300))
        _stand(est, T, ON_PODIUM, 0.4, 2)
        if with_gap_tick:
            _step(est, T + 500)
        e = _step(est, T + 1000, _skeleton(T + 1000, ON_PODIUM, 0.4, ankles=(V, N)))
        c = e.contact
        assert (c.support_surface_validity, c.ground_anchor_xyz) == ("UNCERTAIN", None)
        results.append((c.support_surface_validity, c.support_surface_id, c.ground_anchor_xyz))
    assert results[0] == results[1]  # 중간 tick 유무와 무관하다
    est = ContactEstimator(ContactConfig(hold_max_ms=300))
    _stand(est, T, ON_PODIUM, 0.4, 2)
    assert ContactReason.HOLD_EXPIRED in _step(est, T + 1000, _skeleton(T + 1000, ON_PODIUM, 0.4, ankles=(V, N))).reasons


@pytest.mark.parametrize("y, inside", [(6.105, True), (6.095, False), (7.395, True), (7.405, False)])
def test_footprint_margin_boundary_never_falls_to_floor(y, inside):
    """단상 가장자리(footprint ± 0.10 m) 양옆: 안이면 단상, 밖이면 UNKNOWN 이다(단상 높이 발목을 바닥으로 두지 않는다)."""
    est = ContactEstimator()
    c = _stand(est, T, (4.0, y), 0.4, 3).contact
    if inside:
        assert (c.support_surface_id, c.support_surface_validity) == ("podium-1", "VALID")
    else:
        assert (c.support_surface_validity, c.ground_anchor_xyz) == ("UNKNOWN", None)


def test_rotated_surface_sequence_across_edge():
    """45° 돌린 작업면을 가로지르는 연속 시퀀스: 상판 높이 발목은 footprint 안에서만 그 표면이고 밖에서는 UNKNOWN."""
    import math

    desk = SupportSurface("desk-r", 0.4, center_xy=(4.0, 4.0), size_xy=(2.0, 0.6), yaw_rad=math.pi / 4)
    surfaces = SurfaceSet((desk,), "geom-r")
    est = ContactEstimator(ContactConfig(confirm_frames=1, max_pelvis_ankle_xy_m=1.0, hold_pelvis_margin_m=1.0))
    seen = []
    for k in range(9):
        t = T + k * DT
        d = -0.8 + 0.2 * k  # 짧은 축(대각선 수직 방향)을 따라 움직인다
        xy = (4.0 - d / math.sqrt(2), 4.0 + d / math.sqrt(2))
        c = _step(est, t, _skeleton(t, xy, 0.4), surfaces).contact
        seen.append(c.support_surface_id if c.support_surface_validity == "VALID" else None)
        assert c.support_surface_id != "floor"
    assert seen[0] is None and seen[-1] is None and "desk-r" in seen
