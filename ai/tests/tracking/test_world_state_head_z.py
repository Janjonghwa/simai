"""world_state head_z·head_z_validity (계약 1.4.0 -172, 2.0.0 의미 변경 -179, 사용자 결정 2026-10-19).

- 2.0.0: head_z_validity 가 VALID 나 UNCERTAIN 이면 head_z·head_z_sigma 에 게이트 값을 싣고, UNAVAILABLE 이면 null 이다
  (접지점과 z_validity 의 관계와 같음). 2026-10-18 의 'VALID 일 때만' 을 대체한다.
- 값은 같은 Track 의 이번 시각 skeleton 에서 관측(observed) 귀·눈으로만 만든다. 근거를 확인할 수 없으면 UNAVAILABLE·null.
- 스키마: VALID ↔ 수, UNAVAILABLE ↔ null, UNCERTAIN 은 2.0.0 이상 ↔ 수·1.x ↔ null(버전별). head_z_validity 가 없는 문서는
  값 짝을 보지 않는다(1.3.0 이하 하위 호환).
보고 전 자기 반례 시험: inferred 관절에서 온 UNCERTAIN 값, 근거 목록·skeleton 이 어긋난 값, 값 없는 UNCERTAIN, 1.x 문서의
UNCERTAIN 값, 버전 경계(1.x ↔ 2.x), 실제 게이트 경로, Risk 입력이 받는 값.
"""

import dataclasses
import json
import math
from pathlib import Path

import pytest

from simai_ai.geometry.joint_gate import COCO17_HEAD_JOINTS, GatedJoint, GatedSkeleton, HeadZ, JointValidity
from simai_ai.risk.world import Validity, WorldState
from simai_ai.tracking.world_state import WORLD_STATE_SCHEMA_VERSION, world_state_message, world_track_document
from simai_ai.tracking.world_track import IdentityConfidence, TrackLifecycle, WorldTrackSnapshot

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[2] / "contracts" / "examples"
T = 1_760_342_400_000
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
OFFSET = 0.12


def _skeleton(head_validity=JointValidity.VALID, *, head_source="observed", head_present=True):
    joints = []
    for j in range(17):
        if j in COCO17_HEAD_JOINTS:
            xyz = (1.0, 2.0, 1.60 + 0.01 * j) if head_present else None
            validity = head_validity if head_present else JointValidity.UNAVAILABLE
            joints.append(GatedJoint(xyz, validity, sigma_xyz_m=(0.01, 0.01, 0.02), joint_source=head_source))
        else:
            joints.append(GatedJoint((1.0, 2.0, 0.9), JointValidity.VALID, sigma_xyz_m=(0.01, 0.01, 0.01)))
    return GatedSkeleton(t=T, joint_layout="COCO17", joints=tuple(joints), calibration_version="cal-1")


def _snap(head_z=None, skeleton=None):
    return WorldTrackSnapshot(
        world_id="w-test-0001",
        t=T,
        t_last_valid=T,
        lifecycle=TrackLifecycle.CONFIRMED,
        identity_confidence=IdentityConfidence.HIGH,
        identity_revision=0,
        position_xy=(1.0, 2.0),
        position_xy_sigma=(0.03, 0.03),
        velocity=(0.0, 0.0, 0.0),
        pelvis_xyz=(1.0, 2.0, 0.95),
        head_z=head_z,
        skeleton=skeleton,
    )


def _gated(level=JointValidity.VALID, **kw):
    """실제 게이트 경로: GatedSkeleton.head_z 결과와 그 skeleton 을 같은 Track 에 넣는다."""
    skel = _skeleton(level, **kw)
    return _snap(skel.head_z(OFFSET), skel)


def _message(docs, version=None):
    payload = {"t_latest_global": T, "calibration_version": "cal-1", "scene_version": "scene-1", "tracks": docs}
    msg = world_state_message(payload, msg_id=MSG_ID, sent_at=T)
    if version is not None:
        msg["schema_version"] = version
    return msg


def _head(doc):
    return doc["head_z"], doc["head_z_sigma"], doc["head_z_validity"]


def _expected_head_z():
    return sum(1.60 + 0.01 * j for j in COCO17_HEAD_JOINTS) / len(COCO17_HEAD_JOINTS) + OFFSET


@pytest.mark.parametrize("level", [JointValidity.VALID, JointValidity.UNCERTAIN])
def test_valid_and_uncertain_values_are_carried(level, contract_errors):
    """2.0.0: 게이트가 VALID·UNCERTAIN 으로 낸 값과 σ 를 싣고 유효성을 함께 싣는다."""
    doc = world_track_document(_gated(level), T)
    value, sigma, validity = _head(doc)
    assert validity == str(level)
    assert value == pytest.approx(_expected_head_z())
    assert sigma == pytest.approx(math.sqrt(4 * 0.02**2) / 4)
    assert {doc["skeleton"]["validity"][j] for j in COCO17_HEAD_JOINTS} == {str(level)}
    assert {doc["skeleton"]["joint_source"][j] for j in COCO17_HEAD_JOINTS} == {"observed"}
    msg = _message([doc])
    assert msg["schema_version"] == WORLD_STATE_SCHEMA_VERSION == "2.1.0"  # 2.1.0(-201) 도 UNCERTAIN 값 규칙은 2.0.0 과 같다
    assert contract_errors("world_state", msg) == []


@pytest.mark.parametrize(
    "snap",
    [
        _snap(None),  # 이번 시각 클러스터가 붙지 않았다
        _snap(HeadZ(None, JointValidity.UNAVAILABLE), _skeleton()),
        _gated(head_present=False),  # 좌표 있는 귀·눈이 없다(게이트가 UNAVAILABLE)
    ],
)
def test_unavailable_is_null(snap, contract_errors):
    doc = world_track_document(snap, T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_uncertain_value_from_inferred_joints_is_not_carried(contract_errors):
    """반례 A: UNCERTAIN 값이 추정(inferred) 귀·눈에서 왔으면 싣지 않는다(관측 관절만, AGENTS 규칙 1·7)."""
    snap = _gated(JointValidity.UNCERTAIN, head_source="inferred")
    assert snap.head_z is not None and snap.head_z.value is not None  # 게이트는 값을 냈다
    doc = world_track_document(snap, T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []
    # 음성 대조: 같은 값을 UNCERTAIN 으로 실으면 스키마는 막지 못한다(출처는 송신 측 계약 시험이 지킨다)
    leaked = dict(doc, head_z=snap.head_z.value, head_z_validity="UNCERTAIN")
    assert contract_errors("world_state", _message([leaked])) == []


def test_counterexample_inferred_valid_joint_is_not_carried_either():
    doc = world_track_document(_gated(JointValidity.VALID, head_source="inferred"), T)
    assert _head(doc) == (None, None, "UNAVAILABLE")


@pytest.mark.parametrize(
    "snap",
    [
        _snap(HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, (1, 2))),  # skeleton 없음: 근거를 확인할 수 없다
        _snap(HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, ()), _skeleton(JointValidity.UNCERTAIN)),  # 근거 목록 없음
        _snap(HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, (1, 2)), _skeleton(JointValidity.VALID)),  # 유효성 불일치
        _snap(HeadZ(1.7, JointValidity.VALID, 0.04, (1, 2)), _skeleton(JointValidity.UNCERTAIN)),  # 유효성 불일치
        _snap(HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, (1, 99)), _skeleton(JointValidity.UNCERTAIN)),  # 범위 밖
        _snap(HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, (1,)), _skeleton(JointValidity.UNCERTAIN, head_present=False)),
    ],
)
def test_counterexample_unverifiable_evidence_is_unavailable(snap, contract_errors):
    """반례 B: 게이트 결과와 같은 Track skeleton 이 어긋나면 값을 싣지 않는다(모르면 비움)."""
    doc = world_track_document(snap, T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_uncertain_claim_with_valid_eye_in_skeleton_is_unavailable(contract_errors):
    """반례 B2: skeleton 에 VALID 귀·눈이 있는데 UNCERTAIN 이라고 하면 싣지 않는다. 실으면 R1·R4 는 skeleton 의 VALID
    근거를 보고 이 head_z 를 확정값으로 읽는다(그들은 head_z_validity 를 아직 읽지 않는다)."""
    skel = _skeleton(JointValidity.UNCERTAIN)
    joints = list(skel.joints)
    joints[COCO17_HEAD_JOINTS[0]] = GatedJoint((1.0, 2.0, 1.61), JointValidity.VALID, sigma_xyz_m=(0.01, 0.01, 0.02))
    skel = GatedSkeleton(t=T, joint_layout="COCO17", joints=tuple(joints), calibration_version="cal-1")
    head = HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, tuple(COCO17_HEAD_JOINTS[1:]))
    doc = world_track_document(_snap(head, skel), T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []
    # 실제 게이트는 같은 skeleton 에서 VALID 귀·눈만으로 VALID 를 낸다
    real = world_track_document(_snap(skel.head_z(OFFSET), skel), T)
    assert real["head_z_validity"] == "VALID" and real["head_z"] == pytest.approx(1.61 + OFFSET)


@pytest.mark.parametrize("level", [JointValidity.VALID, JointValidity.UNCERTAIN])
@pytest.mark.parametrize("value", [None, math.nan, math.inf])
def test_counterexample_level_without_finite_value_is_unavailable(level, value, contract_errors):
    """반례 C: VALID·UNCERTAIN 이라면서 값이 없거나 유한하지 않으면 UNAVAILABLE·null. 값 없는 UNCERTAIN 은 스키마가 막는다."""
    doc = world_track_document(_snap(HeadZ(value, level, 0.02, (1, 2)), _skeleton(level)), T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []
    # 음성 대조: 값 없이 VALID·UNCERTAIN 으로 실으면 2.0.0 스키마가 막는다
    assert contract_errors("world_state", _message([dict(doc, head_z_validity=str(level))]))


def test_counterexample_nonfinite_sigma_drops_only_sigma(contract_errors):
    skel = _skeleton(JointValidity.UNCERTAIN)
    doc = world_track_document(_snap(HeadZ(1.7, JointValidity.UNCERTAIN, math.nan, (1, 2)), skel), T)
    assert _head(doc) == (1.7, None, "UNCERTAIN")
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_version_boundary(contract_errors):
    """반례 D: UNCERTAIN 의 값 규칙은 버전별이다. 1.x 문서의 UNCERTAIN 값·2.x 문서의 값 없는 UNCERTAIN 은 둘 다 막는다."""
    doc = world_track_document(_gated(JointValidity.UNCERTAIN), T)
    assert doc["head_z"] is not None
    assert contract_errors("world_state", _message([doc])) == []  # 2.0.0
    assert contract_errors("world_state", _message([doc], "2.7.3")) == []  # 이후 2.x·3.x 도 같은 규칙
    assert contract_errors("world_state", _message([doc], "1.4.0"))  # 1.x 는 UNCERTAIN 값을 싣지 않았다
    nulled = dict(doc, head_z=None, head_z_sigma=None)
    assert contract_errors("world_state", _message([nulled], "1.4.0")) == []  # 1.x 문서로는 맞다
    assert contract_errors("world_state", _message([nulled]))  # 2.0.0 에서는 값 없는 UNCERTAIN 불가
    # UNAVAILABLE 에 값, VALID 에 null 은 버전과 무관하게 막는다
    for version in ("1.4.0", "2.0.0"):
        assert contract_errors("world_state", _message([dict(doc, head_z_validity="UNAVAILABLE")], version))
        assert contract_errors("world_state", _message([dict(nulled, head_z_validity="VALID")], version))
    # head_z_validity 가 없으면(1.3.0 이하 송신 측 흉내) 값 짝을 보지 않는다
    old = {k: v for k, v in doc.items() if k != "head_z_validity"}
    assert contract_errors("world_state", _message([old], "1.3.0")) == []


def test_examples_by_version(contract_errors):
    """dev 예시: 1.x 문서(pre_2_0, surface_inferred, pre_1_3)는 그대로 통과하고, 2.0.0 UNCERTAIN 예시는 값을 싣는다."""
    for name, version in (
        ("world_state.pre_2_0.json", "1.4.0"),
        ("world_state.surface_inferred.json", "1.3.0"),
        ("world_state.pre_1_3.json", "1.2.0"),
    ):
        old = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
        assert old["schema_version"] == version
        assert contract_errors("world_state", old) == []
    v1 = json.loads((EXAMPLES / "world_state.pre_2_0.json").read_text(encoding="utf-8"))["payload"]["tracks"][0]
    assert (v1["head_z_validity"], v1["head_z"]) == ("UNCERTAIN", None)
    new = json.loads((EXAMPLES / "world_state.head_z_uncertain.json").read_text(encoding="utf-8"))
    tr = new["payload"]["tracks"][0]
    assert new["schema_version"] == "2.0.0" and tr["head_z_validity"] == "UNCERTAIN" and tr["head_z"] is not None
    assert contract_errors("world_state", new) == []
    # 2.0.0 예시를 1.x 로 바꿔 달면 막힌다(같은 문서라도 버전이 의미를 정한다)
    assert contract_errors("world_state", dict(new, schema_version="1.4.0"))


def test_risk_input_keeps_uncertain_value_out_of_confirmed_head_z():
    """Risk 입력(risk.world)은 head_z_validity 를 읽어 UNCERTAIN 값을 확정값 head_z 가 아니라 head_z_uncertain 으로 넘긴다
    (S15P31A101-179). R1·R5 는 head_z 만 읽으므로 1.4.0(UNCERTAIN + null)과 같은 입력을 받고, R4 만 UNCERTAIN 값을 쓴다."""
    doc = world_track_document(_gated(JointValidity.UNCERTAIN), T)
    track = WorldState.from_payload(_message([doc])["payload"]).tracks[0]
    assert track.head_z_validity is Validity.UNCERTAIN
    assert track.head_z is None and track.head_z_sigma is None
    assert track.head_z_uncertain == pytest.approx(_expected_head_z())
    assert all(track.skeleton.validity[j] is Validity.UNCERTAIN for j in COCO17_HEAD_JOINTS)
    v1 = dict(doc, head_z=None, head_z_sigma=None)  # 1.4.0 송신 측이 냈을 문서
    old = WorldState.from_payload(_message([v1], "1.4.0")["payload"]).tracks[0]
    assert dataclasses.replace(track, head_z_uncertain=None, head_z_uncertain_sigma=None) == old
    valid = WorldState.from_payload(_message([world_track_document(_gated(), T)])["payload"]).tracks[0]
    assert valid.head_z == pytest.approx(_expected_head_z()) and valid.head_z_uncertain is None
