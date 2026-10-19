"""world_state 1.3.0 선택 필드: 트랙의 surface_source(지지 표면 출처)·aliases(병합 별칭) (S15P31A101-170).

- surface_source 는 접지점(ground_anchor_xyz)이 있을 때만, 그때는 늘 싣는다(명세 5.2, AGENTS 규칙 2·7).
- aliases 는 World Track Manager 의 병합 별칭을 순서대로 싣고, Track 이 살아 있는 동안 빠지지 않는다(규칙 6).
- 이전 버전 문서(1.2.0)는 그대로 계약을 통과하고, 소비자(Risk 입력)는 새 필드를 무시한다(하위 호환).
보고 전 자기 반례 시험(공통 규칙)도 여기 둔다: 반례 A·B·C·D.
"""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from simai_ai.risk.world import WorldState
from simai_ai.tracking.world_state import (
    WORLD_STATE_SCHEMA_VERSION,
    SupportContact,
    world_state_message,
    world_state_payload,
    world_track_document,
)
from simai_ai.tracking.world_track import IdentityConfidence, TrackLifecycle, WorldTrackFrame, WorldTrackSnapshot

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE.parents[2] / "contracts" / "examples"
T = 1_760_342_400_000
MSG_ID = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"

_spec = importlib.util.spec_from_file_location("_wt_reconnect", HERE / "test_world_track_reconnect.py")
rc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rc)


@pytest.fixture(scope="module")
def rig():
    return rc.wt.CameraRig.from_provider(rc.wt._DictCalibrationProvider(rc.wt._calibration_doc()))


def _snap(world_id="w-test-0001", aliases=(), revision=0, t=T):
    return WorldTrackSnapshot(
        world_id=world_id,
        t=t,
        t_last_valid=t,
        lifecycle=TrackLifecycle.CONFIRMED,
        identity_confidence=IdentityConfidence.HIGH,
        identity_revision=revision,
        position_xy=(1.0, 2.0),
        position_xy_sigma=(0.03, 0.03),
        velocity=(0.0, 0.0, 0.0),
        pelvis_xyz=(1.0, 2.0, 0.95),
        aliases=aliases,
    )


def _message(docs, t=T):
    payload = {"t_latest_global": t, "calibration_version": "cal-1", "scene_version": "scene-1", "tracks": docs}
    return world_state_message(payload, msg_id=MSG_ID, sent_at=t)


ANCHOR = ((1.0, 2.0, 0.0), (0.01, 0.01, 0.03), "floor", "VALID")


# ---------------------------------------------------------------- surface_source


@pytest.mark.parametrize(
    "contact, expected",
    [
        (SupportContact(*ANCHOR, "VALID", "VALID", surface_source="observed",
                        observed_xy=(1.0, 2.0), observed_xy_sigma=(0.01, 0.01), observed_at=T), "observed"),
        (SupportContact(*ANCHOR, "UNCERTAIN", "UNCERTAIN", surface_source="inferred"), "inferred"),
        (SupportContact(), None),
        # 지금 접지는 모르지만 예전 발목 관측이 남아 있다: 위치 대체에는 쓰여도 출처는 싣지 않는다
        (SupportContact(observed_xy=(1.0, 2.0), observed_xy_sigma=(0.01, 0.01), observed_at=T - 300), None),
        (SupportContact(support_surface_id="podium-1", support_surface_validity="UNCERTAIN"), None),
    ],
)
def test_surface_source_only_with_anchor(contact, expected, contract_errors):
    doc = world_track_document(_snap(), T, contact)
    assert doc.get("surface_source") == expected
    assert ("surface_source" in doc) == (doc["ground_anchor_xyz"] is not None)
    msg = _message([doc])
    assert msg["schema_version"] == WORLD_STATE_SCHEMA_VERSION == "2.0.0"
    assert contract_errors("world_state", msg) == []


def test_counterexample_a_surface_source_never_leaks_without_anchor(contract_errors):
    """반례 A: SupportContact 불변식을 우회해 접지점 없이 출처만 든 값이 와도 문서에는 출처가 실리지 않는다.
    반대로 접지점이 있는데 출처가 빠진 값은 조용히 생략하지 않고 거절한다(모르는 출처를 observed 로 읽게 두지 않음)."""
    leak = SupportContact()
    object.__setattr__(leak, "surface_source", "inferred")
    doc = world_track_document(_snap(), T, leak)
    assert "surface_source" not in doc and doc["ground_anchor_xyz"] is None
    assert contract_errors("world_state", _message([doc])) == []
    # 스키마도 같은 누출을 막는다
    bad = dict(doc, surface_source="inferred")
    assert contract_errors("world_state", _message([bad]))

    missing = SupportContact(*ANCHOR, "UNCERTAIN", "UNCERTAIN", surface_source="inferred")
    object.__setattr__(missing, "surface_source", None)
    with pytest.raises(ValueError, match="surface_source"):
        world_track_document(_snap(), T, missing)


# ---------------------------------------------------------------- aliases


def test_aliases_omitted_when_none_and_kept_in_order(contract_errors):
    assert "aliases" not in world_track_document(_snap(), T)
    doc = world_track_document(_snap(aliases=("w-a", "w-b"), revision=2), T)
    assert doc["aliases"] == ["w-a", "w-b"] and doc["identity_revision"] == 2
    assert contract_errors("world_state", _message([doc])) == []


def test_counterexample_b_duplicate_or_self_alias():
    """반례 B: 입력 별칭이 중복되면 한 번만 싣고(계약 uniqueItems), 자기 ID 가 별칭이면 입력 불일치로 거절한다."""
    doc = world_track_document(_snap(aliases=("w-a", "w-b", "w-a"), revision=2), T)
    assert doc["aliases"] == ["w-a", "w-b"]
    with pytest.raises(ValueError, match="자기"):
        world_track_document(_snap(aliases=("w-test-0001",), revision=1), T)
    with pytest.raises(ValueError, match="빈"):
        world_track_document(_snap(aliases=("",), revision=1), T)


def test_counterexample_c_chained_merges_aliases_never_drop(rig, contract_errors):
    """반례 C: X←Y 병합 → X 가 가려져 LOST → 새 ID Z → X←Z 늦은 재연결 병합. 실제 World Track Manager 의 모든 시각에서
    world_state 별칭은 Track 이 보이는 동안 앞부분이 그대로 남고 뒤에만 붙는다. 중복·자기 ID 가 없고 계약을 통과한다."""
    m = rc._manager()
    frames = []
    x, y = rc._lost_then_new_id(m, rig)
    frames += rc._feed(m, rig, range(50, 61), rc.REVIVED)
    frames += rc._empty(m, rig, 61, 96)
    ids2 = {"cam3": "cam3-r", "cam4": "cam4-r"}
    frames += rc._feed(m, rig, range(96, 101), ids2)
    z = frames[-1].tracks[0].world_id
    frames += rc._feed(m, rig, range(101, 110), dict(ids2, cam1="cam1-p", cam2="cam2-p"))
    assert [(r.previous_world_id, r.world_id) for r in m.revisions] == [(y, x), (z, x)]

    seen: dict[str, list[str]] = {}
    for f in frames:
        msg = world_state_message(world_state_payload(f, f.t, "scene-1"), msg_id=MSG_ID, sent_at=f.t)
        assert contract_errors("world_state", msg) == []
        for doc in msg["payload"]["tracks"]:
            aliases = doc.get("aliases", [])
            assert len(set(aliases)) == len(aliases) and doc["world_id"] not in aliases
            before = seen.get(doc["world_id"], [])
            assert aliases[: len(before)] == before, (f.t, doc["world_id"], before, aliases)  # 빠지거나 바뀌지 않는다
            seen[doc["world_id"]] = aliases
            assert not aliases or doc["identity_revision"] >= 1  # 병합하면 revision 이 오른다(개수와 같다는 보장은 없다)
    assert seen[x] == [y, z]
    # 합쳐진 ID 는 그 뒤 트랙으로 나오지 않는다(별칭으로만 남는다)
    merged_at = m.revisions[-1].t
    assert all(doc["world_id"] != z for f in frames if f.t >= merged_at for doc in world_state_payload(f, f.t, "s")["tracks"])


# ---------------------------------------------------------------- 하위 호환


def test_counterexample_d_backward_compat_old_documents_and_consumer(contract_errors):
    """반례 D: 1.2.0 문서(새 필드 없음)도 계약을 통과하고, Risk 입력(소비자)은 새 필드가 있어도 없어도 같게 읽는다."""
    old = json.loads((EXAMPLES / "world_state.pre_1_3.json").read_text(encoding="utf-8"))
    assert old["schema_version"] == "1.2.0"
    assert contract_errors("world_state", old) == []
    assert all("surface_source" not in tr and "aliases" not in tr for tr in old["payload"]["tracks"])
    new = json.loads((EXAMPLES / "world_state.json").read_text(encoding="utf-8"))
    assert contract_errors("world_state", new) == []
    stripped = copy.deepcopy(new)
    for tr in stripped["payload"]["tracks"]:
        tr.pop("surface_source", None)
        tr.pop("aliases", None)
    assert contract_errors("world_state", stripped) == []  # 선택 필드다
    assert WorldState.from_payload(new["payload"]) == WorldState.from_payload(stripped["payload"])


def test_payload_from_frame_carries_both_fields(contract_errors):
    snap = _snap(aliases=("w-old",), revision=1)
    contact = SupportContact(*ANCHOR, "UNCERTAIN", "UNCERTAIN", surface_source="inferred")
    payload = world_state_payload(WorldTrackFrame(T, "cal-1", (snap,)), T, "scene-1", {snap.world_id: contact})
    [doc] = payload["tracks"]
    assert doc["aliases"] == ["w-old"] and doc["surface_source"] == "inferred"
    assert contract_errors("world_state", world_state_message(payload, msg_id=MSG_ID, sent_at=T)) == []
