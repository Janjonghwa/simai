"""WorldState.from_payload_fast 가 from_payload 와 같은 결과·같은 예외를 내는지 (S15P31A101-663).

판정 루프는 매 tick 자기가 만든 world_state payload 를 이 빠른 경로로 옮긴다. 정상 입력은 같은 값의 객체(필드마다 같은 값·같은
타입)여야 하고, 검사에 걸리는 입력은 느린 경로와 같은 RiskInputError(같은 위치 문구)여야 한다.
"""

import copy
import dataclasses
import json
from pathlib import Path

import pytest

from simai_ai.risk import WorldState
from simai_ai.risk._parse import RiskInputError

EXAMPLES = Path(__file__).resolve().parents[3] / "contracts" / "examples"


def _deep_fields(obj):
    """dataclass 를 (필드 이름, 타입, 값) 트리로: == 은 aliases(compare=False)·int/float 차이를 보지 않으므로 직접 비교한다."""
    if dataclasses.is_dataclass(obj):
        return (type(obj).__name__, tuple((f.name, _deep_fields(getattr(obj, f.name))) for f in dataclasses.fields(obj)))
    if isinstance(obj, tuple):
        return tuple(_deep_fields(v) for v in obj)
    return (type(obj).__name__, obj)


def _same(payload):
    slow = WorldState.from_payload(payload)
    fast = WorldState.from_payload_fast(payload)
    assert _deep_fields(fast) == _deep_fields(slow)


@pytest.mark.parametrize("name", sorted(p.name for p in EXAMPLES.glob("world_state*.json")) or ["none"])
def test_contract_examples_match(name):
    path = EXAMPLES / name
    if not path.is_file():
        pytest.skip("저장소 밖(배포 이미지)에서는 contracts/ 예시가 없다")
    _same(json.loads(path.read_text(encoding="utf-8"))["payload"])


def test_int_values_become_float_and_aliases_kept(world_payload, track_json):
    track = track_json(position_xy=[3, 5], head_z=2, frame_age_ms=140, aliases=["w-0001"], velocity=[0, 0, 0])
    _same(world_payload(tracks=[track]))
    fast = WorldState.from_payload_fast(world_payload(tracks=[track])).tracks[0]
    assert type(fast.position_xy[0]) is float and fast.aliases == ("w-0001",)


def _skeleton():
    return {
        "joint_layout": "COCO17",
        "joints": [[3.4, 5.0, 1.6], None, [3, 5, 1]],
        "visible": [True, False, True],
        "validity": ["VALID", "UNAVAILABLE", "UNCERTAIN"],
        "joint_source": ["observed", "observed", "inferred"],
    }


def test_skeleton_and_head_z_uncertain_match(world_payload, track_json):
    uncertain = track_json(world_id="w-2", head_z_validity="UNCERTAIN", head_z=1.5, head_z_sigma=0.2,
                           skeleton=_skeleton())
    _same(world_payload(tracks=[track_json(skeleton=_skeleton(), head_z_validity="VALID"), uncertain]))
    _same(world_payload(tracks=[]))


def _mutations():
    yield "payload-not-mapping", lambda p: [1]
    yield "tracks-missing", lambda p: {k: v for k, v in p.items() if k != "tracks"}
    yield "tracks-not-list", lambda p: p | {"tracks": ()}
    yield "t-latest-float", lambda p: p | {"t_latest_global": 1.0}
    yield "calibration-empty", lambda p: p | {"calibration_version": ""}
    for key, value in [
        ("t_last_valid", 1760342400080.0), ("t_last_valid", True), ("t_last_valid", -1), ("t_last_valid", 2**63),
        ("position_xy", [1.0]), ("position_xy", (1.0, 2.0)), ("pelvis_xyz", [1.0, 2.0]),
        ("observation_state", "SEEN"), ("position_xy", [float("nan"), 5.0]), ("pelvis_xyz", [1.0, 2.0, float("inf")]),
        ("head_z", float("-inf")), ("position_xy", [10**400, 5.0]), ("position_xy", [True, 5.0]),
        ("identity_revision", -1), ("frame_age_ms", -1), ("cameras", ["cam1", ""]), ("support_surface_id", 3),
        ("head_z_validity", None), ("aliases", "w-1"), ("velocity", [1.0, 2.0]),
        ("skeleton", {"joint_layout": "COCO17", "joints": [], "visible": [1], "validity": [], "joint_source": []}),
        ("skeleton", _skeleton() | {"validity": ["VALID", "BAD", "VALID"]}),
        ("skeleton", _skeleton() | {"visible": [True]}),  # __post_init__ 길이 규칙
        ("support_surface_validity", "UNKNOWN"),  # __post_init__ 접지점 규칙
    ]:
        yield f"track-{key}-{value!r}"[:60], lambda p, k=key, v=value: p | {"tracks": [p["tracks"][0] | {k: v}]}
    for key in ("world_id", "head_z", "cameras", "pelvis_xyz"):
        yield f"track-missing-{key}", lambda p, k=key: p | {
            "tracks": [{kk: vv for kk, vv in p["tracks"][0].items() if kk != k}]}
    yield "duplicate-id", lambda p: p | {"tracks": [p["tracks"][0], p["tracks"][0]]}


@pytest.mark.parametrize(("name", "mutate"), list(_mutations()), ids=[n for n, _ in _mutations()])
def test_rejections_match(world_payload, name, mutate):
    payload = mutate(copy.deepcopy(world_payload()))
    with pytest.raises(RiskInputError) as slow:
        WorldState.from_payload(payload)
    with pytest.raises(RiskInputError) as fast:
        WorldState.from_payload_fast(payload)
    assert str(fast.value) == str(slow.value)
