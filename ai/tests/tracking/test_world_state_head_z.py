"""world_state 1.4.0 선택 필드 head_z_validity (S15P31A101-172, 사용자 승인 2026-10-18).

- head_z 값은 관절 유효성 게이트가 VALID 로 낸 값만 싣는다(UNCERTAIN 값 싣기는 사용자가 거절함).
- head_z_validity 는 1.4.0 송신 측이 늘 싣는다: VALID(값 있음), UNCERTAIN(UNCERTAIN 귀·눈으로만 값을 만들었다, 값 null),
  UNAVAILABLE(게이트가 값을 못 냄, 이번 시각 클러스터 없음).
- 스키마는 head_z_validity 가 있을 때만 값과 짝을 막는다(VALID ↔ 수, 그 밖 ↔ null). 필드가 없는 이전 문서는 그대로 통과한다.
보고 전 자기 반례 시험: 게이트와 어긋나는 입력(VALID 인데 값 없음·NaN, UNCERTAIN 값 누출, σ 만 누출), 실제 게이트 경로,
이전 버전 문서 하위 호환, Risk 입력이 새 필드를 무시하는지.
"""

import copy
import json
import math
from pathlib import Path

import pytest

from simai_ai.geometry.joint_gate import COCO17_HEAD_JOINTS, GatedJoint, GatedSkeleton, HeadZ, JointValidity
from simai_ai.risk.world import WorldState
from simai_ai.tracking.world_state import WORLD_STATE_SCHEMA_VERSION, world_state_message, world_track_document
from simai_ai.tracking.world_track import IdentityConfidence, TrackLifecycle, WorldTrackSnapshot

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[2] / "contracts" / "examples"
T = 1_760_342_400_000
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"
OFFSET = 0.12


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


def _message(docs):
    payload = {"t_latest_global": T, "calibration_version": "cal-1", "scene_version": "scene-1", "tracks": docs}
    return world_state_message(payload, msg_id=MSG_ID, sent_at=T)


def _head(doc):
    return doc["head_z"], doc["head_z_sigma"], doc["head_z_validity"]


@pytest.mark.parametrize(
    "head, expected",
    [
        (HeadZ(1.7, JointValidity.VALID, 0.02, (1, 2)), (1.7, 0.02, "VALID")),
        (HeadZ(1.7, JointValidity.VALID, None, (1,)), (1.7, None, "VALID")),  # σ 모름: 값은 VALID 그대로
        (HeadZ(1.7, JointValidity.UNCERTAIN, 0.04, (3,)), (None, None, "UNCERTAIN")),  # 값·σ 를 싣지 않는다
        (HeadZ(None, JointValidity.UNAVAILABLE), (None, None, "UNAVAILABLE")),
        (None, (None, None, "UNAVAILABLE")),  # 이번 시각 클러스터가 붙지 않았다
    ],
)
def test_head_z_validity_follows_gate(head, expected, contract_errors):
    doc = world_track_document(_snap(head), T)
    assert _head(doc) == expected
    msg = _message([doc])
    assert msg["schema_version"] == WORLD_STATE_SCHEMA_VERSION == "1.4.0"
    assert contract_errors("world_state", msg) == []


@pytest.mark.parametrize(
    "head",
    [
        HeadZ(None, JointValidity.VALID, 0.02),  # 게이트가 VALID 라는데 값이 없다
        HeadZ(math.nan, JointValidity.VALID, 0.02),
        HeadZ(math.inf, JointValidity.VALID, 0.02),
    ],
)
def test_counterexample_valid_without_finite_value_is_not_valid(head, contract_errors):
    """반례 A: 게이트와 어긋난 VALID(값 없음·NaN·inf)는 VALID 로 싣지 않는다. 모르면 비운다(UNAVAILABLE·null)."""
    doc = world_track_document(_snap(head), T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []
    # 음성 대조: 그대로 VALID·null 로 실으면 스키마가 막는다
    bad = dict(doc, head_z_validity="VALID")
    assert contract_errors("world_state", _message([bad]))


def test_counterexample_nonfinite_sigma_is_dropped_not_value(contract_errors):
    doc = world_track_document(_snap(HeadZ(1.7, JointValidity.VALID, math.nan)), T)
    assert _head(doc) == (1.7, None, "VALID")
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_uncertain_value_or_sigma_never_leaks(contract_errors):
    """반례 B: UNCERTAIN 값은 head_z 로도 head_z_sigma 로도 새지 않는다. 새면 스키마가 막는다(음성 대조)."""
    doc = world_track_document(_snap(HeadZ(1.66, JointValidity.UNCERTAIN, 0.03, (1,))), T)
    assert _head(doc) == (None, None, "UNCERTAIN")
    assert contract_errors("world_state", _message([dict(doc, head_z=1.66)]))
    assert contract_errors("world_state", _message([dict(doc, head_z_sigma=0.03)]))
    # head_z_validity 를 빼면(이전 버전 송신 측 흉내) 값 짝을 스키마가 판단하지 않는다: 하위 호환
    old = {k: v for k, v in doc.items() if k != "head_z_validity"}
    assert contract_errors("world_state", _message([dict(old, head_z=1.66)])) == []


def _skeleton(head_validity):
    joints = []
    for j in range(17):
        if j in COCO17_HEAD_JOINTS:
            joints.append(GatedJoint((1.0, 2.0, 1.60 + 0.01 * j), head_validity, sigma_xyz_m=(0.01, 0.01, 0.02)))
        else:
            joints.append(GatedJoint((1.0, 2.0, 0.9), JointValidity.VALID, sigma_xyz_m=(0.01, 0.01, 0.01)))
    return GatedSkeleton(t=T, joint_layout="COCO17", joints=tuple(joints), calibration_version="cal-1")


@pytest.mark.parametrize("level", [JointValidity.VALID, JointValidity.UNCERTAIN])
def test_counterexample_real_gate_path_matches_skeleton(level, contract_errors):
    """반례 C: 실제 GatedSkeleton.head_z 결과를 넣으면 head_z_validity 는 같은 문서 skeleton 의 귀·눈 유효성과 맞는다."""
    gated = _skeleton(level)
    doc = world_track_document(_snap(gated.head_z(OFFSET), gated), T)
    head_validities = {doc["skeleton"]["validity"][j] for j in COCO17_HEAD_JOINTS}
    assert head_validities == {str(level)} and doc["head_z_validity"] == str(level)
    if level is JointValidity.VALID:
        expected = sum(1.60 + 0.01 * j for j in COCO17_HEAD_JOINTS) / 4 + OFFSET
        assert doc["head_z"] == pytest.approx(expected)
    else:
        assert doc["head_z"] is None and doc["head_z_sigma"] is None
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_head_joints_missing_is_unavailable(contract_errors):
    gated = _skeleton(JointValidity.VALID)
    joints = list(gated.joints)
    for j in COCO17_HEAD_JOINTS:
        joints[j] = GatedJoint(None, JointValidity.UNAVAILABLE)
    gated = GatedSkeleton(t=T, joint_layout="COCO17", joints=tuple(joints), calibration_version="cal-1")
    doc = world_track_document(_snap(gated.head_z(OFFSET), gated), T)
    assert _head(doc) == (None, None, "UNAVAILABLE")
    assert contract_errors("world_state", _message([doc])) == []


def test_previous_versions_still_pass_and_risk_input_ignores_new_field(contract_errors):
    """이전 버전 문서(1.2.0·1.3.0, head_z_validity 없음)는 그대로 통과하고, Risk 입력은 새 필드를 무시한다."""
    for name in ("world_state.pre_1_3.json", "world_state.surface_inferred.json"):
        old = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
        assert old["schema_version"] in {"1.2.0", "1.3.0"}
        assert all("head_z_validity" not in tr for tr in old["payload"]["tracks"])
        assert contract_errors("world_state", old) == []
    new = json.loads((EXAMPLES / "world_state.json").read_text(encoding="utf-8"))
    assert new["schema_version"] == "1.4.0"
    stripped = copy.deepcopy(new)
    for tr in stripped["payload"]["tracks"]:
        tr.pop("head_z_validity")
    assert WorldState.from_payload(new["payload"]) == WorldState.from_payload(stripped["payload"])
