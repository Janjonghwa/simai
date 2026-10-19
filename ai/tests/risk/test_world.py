"""입력 타입이 world_state 계약을 따르고, 스키마로 못 막는 규칙까지 지키는지 본다."""

import json
from pathlib import Path

import pytest

from simai_ai.risk import WorldState, WorldTrack
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.world import JointSource, ObservationState, SupportSurfaceValidity, Validity

EXAMPLE = Path(__file__).resolve().parents[3] / "contracts" / "examples" / "world_state.json"


@pytest.mark.skipif(not EXAMPLE.is_file(), reason="저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다")
def test_contract_example_parses():
    payload = json.loads(EXAMPLE.read_text(encoding="utf-8"))["payload"]
    world = WorldState.from_payload(payload)

    assert world.t_latest_global == 1760342400100
    assert [t.world_id for t in world.tracks] == ["w-0012", "w-0013"]
    observed, predicted = world.tracks
    assert observed.ground_anchor_xyz == (3.41, 5.02, 0.0)
    assert observed.skeleton is not None and observed.skeleton.joints[1] is None
    # 모르는 값은 None 으로 남는다(Z=0 대체 금지).
    assert predicted.observation_state is ObservationState.PREDICTED
    assert predicted.ground_anchor_xyz is None
    assert predicted.pelvis_xyz is None
    assert predicted.head_z is None
    assert predicted.support_surface_validity is SupportSurfaceValidity.UNKNOWN
    assert predicted.skeleton is None


def test_unknown_fields_are_ignored(world_payload, track_json):
    payload = world_payload(tracks=[track_json(future_field=1)]) | {"another": "x"}
    assert WorldState.from_payload(payload).tracks[0].world_id == "w-0012"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"support_surface_validity": "UNKNOWN"}, id="surface-unknown"),
        pytest.param({"support_surface_validity": "UNCERTAIN"}, id="surface-uncertain"),
        pytest.param({"z_validity": "UNAVAILABLE"}, id="z-unavailable"),
    ],
)
def test_ground_anchor_must_be_null_when_surface_unknown(track_json, overrides):
    # 스키마 allOf 조건과 같다: 지지 표면이 VALID 가 아니면 접지점을 채울 수 없다.
    with pytest.raises(RiskInputError, match="ground_anchor_xyz"):
        WorldTrack.from_json(track_json(**overrides))


def test_ground_anchor_null_is_kept(track_json):
    track = WorldTrack.from_json(track_json(ground_anchor_xyz=None, support_surface_validity="UNKNOWN"))
    assert track.ground_anchor_xyz is None


@pytest.mark.parametrize("key", ["ground_anchor_xyz", "pelvis_xyz", "head_z", "support_surface_id", "cameras"])
def test_required_keys_must_be_present_even_if_null(track_json, key):
    obj = track_json()
    del obj[key]
    with pytest.raises(RiskInputError, match=key):
        WorldTrack.from_json(obj)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("t_last_valid", 1760342400080.0, id="float-timestamp"),
        pytest.param("t_last_valid", True, id="bool-timestamp"),
        pytest.param("t_last_valid", -1, id="negative-timestamp"),
        pytest.param("position_xy", [1.0], id="short-vec2"),
        pytest.param("pelvis_xyz", [1.0, 2.0], id="short-vec3"),
        pytest.param("observation_state", "SEEN", id="bad-enum"),
        pytest.param("position_xy", [float("nan"), 5.0], id="nan-xy"),
        pytest.param("pelvis_xyz", [1.0, 2.0, float("inf")], id="inf-xyz"),
        pytest.param("head_z", float("-inf"), id="inf-head-z"),
        pytest.param("position_xy", [10**400, 5.0], id="huge-int"),
        pytest.param("t_last_valid", 2**63, id="timestamp-over-int64"),
    ],
)
def test_bad_values_are_rejected(track_json, key, value):
    with pytest.raises(RiskInputError):
        WorldTrack.from_json(track_json(**{key: value}))


def test_duplicate_world_id_is_rejected(world_payload, track_json):
    with pytest.raises(RiskInputError, match="중복"):
        WorldState.from_payload(world_payload(tracks=[track_json(), track_json()]))


def _skeleton(**overrides):
    return {
        "joint_layout": "COCO17",
        "joints": [[3.4, 5.0, 1.6], [3.5, 5.0, 1.6], None],
        "visible": [True, True, False],
        "validity": ["VALID", "VALID", "UNAVAILABLE"],
        "joint_source": ["observed", "inferred", "observed"],
    } | overrides


def test_skeleton_array_lengths_must_match(track_json):
    with pytest.raises(RiskInputError, match="길이"):
        WorldTrack.from_json(track_json(skeleton=_skeleton(visible=[True, True])))


def test_inferred_joints_are_not_used_for_risk(track_json):
    skeleton = WorldTrack.from_json(track_json(skeleton=_skeleton())).skeleton
    assert skeleton is not None
    assert skeleton.joint_source == (JointSource.OBSERVED, JointSource.INFERRED, JointSource.OBSERVED)
    assert skeleton.observed_joint(0) == (3.4, 5.0, 1.6)
    # HMR(inferred) 관절은 원본 배열에는 남지만 위험 판정용 접근에서는 빠진다.
    assert skeleton.joints[1] == (3.5, 5.0, 1.6)
    assert skeleton.observed_joint(1) is None
    # 2대 미만에서 보인 관절은 지어내지 않는다.
    assert skeleton.observed_joint(2) is None
    assert skeleton.validity[2] is Validity.UNAVAILABLE


# --- head_z_validity (world_state 1.4.0, S15P31A101-177) ------------------------------------------

EXAMPLES = EXAMPLE.parent


@pytest.mark.skipif(not EXAMPLE.is_file(), reason="저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다")
@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("world_state.json", (Validity.VALID, Validity.UNAVAILABLE)),
        ("world_state.head_z_uncertain.json", (Validity.UNCERTAIN,)),
        ("world_state.pre_2_0.json", (Validity.UNCERTAIN,)),
        ("world_state.pre_1_3.json", None),
    ],
)
def test_head_z_validity_contract_examples(name, expected):
    # 1.4.0 예시는 head_z_validity 를 읽고, 필드가 없는 이전 버전 예시는 None(UNAVAILABLE 로 가정하지 않음)이다
    world = WorldState.from_payload(json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["payload"])
    got = tuple(t.head_z_validity for t in world.tracks)
    if expected is None:
        assert set(got) == {None}
    else:
        assert got == expected


@pytest.mark.skipif(not EXAMPLE.is_file(), reason="저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다")
@pytest.mark.parametrize(
    "name", ["world_state.head_z_unavailable_with_value.json", "world_state.head_z_valid_without_value.json"]
)
def test_head_z_validity_invalid_contract_examples_are_rejected(name):
    # 스키마가 버전과 무관하게 거부하는 예시(UNAVAILABLE 인데 값, VALID 인데 null)는 Risk 입력에서도 거부한다.
    # UNCERTAIN 의 값 유무는 버전별(1.x null, 2.x 수)이고 payload 에는 버전이 없어 Risk 입력은 둘 다 받는다(아래)
    doc = json.loads((EXAMPLES / "invalid" / name).read_text(encoding="utf-8"))
    with pytest.raises(RiskInputError, match="head_z"):
        WorldState.from_payload(doc["payload"])


@pytest.mark.skipif(not EXAMPLE.is_file(), reason="저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다")
@pytest.mark.parametrize(
    ("name", "expected_uncertain"),
    [("world_state.head_z_uncertain.json", 1.67), ("world_state.pre_2_0.json", None)],
)
def test_uncertain_head_z_never_reaches_confirmed_slot(name, expected_uncertain):
    # 2.0.0 의 UNCERTAIN 값은 head_z(확정값, R1·R5 가 읽음)가 아니라 head_z_uncertain 으로 간다(S15P31A101-179).
    # 1.x 문서(UNCERTAIN + null)와 같은 확정값 칸이 되어 R1·R5 판정이 계약 변경 전과 같다.
    (track,) = WorldState.from_payload(json.loads((EXAMPLES / name).read_text(encoding="utf-8"))["payload"]).tracks
    assert track.head_z_validity is Validity.UNCERTAIN
    assert track.head_z is None and track.head_z_sigma is None
    assert track.head_z_uncertain == expected_uncertain
    assert track.head_z_uncertain_sigma == (0.04 if expected_uncertain is not None else None)
