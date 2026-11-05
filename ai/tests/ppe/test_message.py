"""ppe_state 메시지가 계약(contracts/messages/ppe_state 1.0.0)과 스키마 밖 규칙을 지키는지 본다 (명세 12.3, S15P31A101-491).

스키마 밖 규칙(contracts/README '스키마로 막는 불변 규칙'): 한 메시지에 같은 world_id 하나, expires_at_ms ≥ last_evidence_at_ms,
근거 촬영 시각 ≤ 사람의 capture_time_ms. 실제 계약 예시에도 같은 규칙을 적용한다.
"""

import json

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from simai_ai.ppe.message import build_ppe_state
from simai_ai.ppe.state import PpeJudge
from simai_ai.providers import contract

from .test_association_state import HELMET, NO_HELMET, NO_VEST, closed_after, feed, person, t_of

SHA = "e373a104805765417502e34d21586be2607126bbdd8499b935896ca333eb103c"
PROVENANCE = {
    "frames": {"source": "FILE", "dataset_id": "synthetic/ppe-unit", "camera_ids": ["cam1"]},
    "tracks": {"source": "SIM", "provider_version": "test"},
    "is_upper_bound": True,
}


@pytest.fixture(scope="module")
def validator():
    registry, target = Registry(), None
    for path in contract.contracts_dir().rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        if path.name == "ppe_state.schema.json":
            target = schema
    return Draft202012Validator(target, registry=registry)


def out_of_schema_violations(doc: dict) -> list[str]:
    out, people = [], doc["payload"]["people"]
    ids = [p["world_id"] for p in people]
    if len(ids) != len(set(ids)):
        out.append("같은 world_id 가 두 번")
    for p in people:
        for item in ("helmet", "vest"):
            s = p[item]
            if s["expires_at_ms"] is not None and s["last_evidence_at_ms"] is not None \
                    and s["expires_at_ms"] < s["last_evidence_at_ms"]:
                out.append(f"{p['world_id']}.{item}: 만료가 마지막 근거보다 이르다")
            for e in s["evidence"]:
                if e["capture_time_ms"] > p["capture_time_ms"]:
                    out.append(f"{p['world_id']}.{item}: 근거가 사람 촬영 시각보다 늦다")
    return out


def _message(j, now, **kw):
    return build_ppe_state(j, j.evaluate(now), msg_id="00000000-0000-4000-8000-000000000491", sent_at=now + 50,
                           source_id="tier0-01", stream_epoch=1, state_sequence=0,
                           model_version="T1-v4-ALL-seed20261021-e08-e373a1048057", checkpoint_sha256=SHA,
                           input_provenance=PROVENANCE, **kw)


def test_built_message_passes_contract_and_rules(validator):
    j = PpeJudge()
    for b in range(6):
        last = feed(j, "cam1", t_of(b), [person("w1"), person("w2", 900)], [HELMET(), NO_VEST(), NO_HELMET(900)])
    doc = _message(j, closed_after(5))
    assert [e.message for e in validator.iter_errors(doc)] == []
    assert out_of_schema_violations(doc) == []
    people = {p["world_id"]: p for p in doc["payload"]["people"]}
    assert (people["w1"]["helmet"]["state"], people["w1"]["vest"]["state"]) == ("WORN", "NOT_WORN")
    assert (people["w2"]["helmet"]["state"], people["w2"]["vest"]["state"]) == ("NOT_WORN", "UNKNOWN")
    assert people["w2"]["vest"]["evidence"] == [] and people["w2"]["vest"]["expires_at_ms"] is None
    ev = people["w1"]["helmet"]["evidence"][0]
    assert (ev["candidate_state"], ev["frame_sequence"], ev["stream_epoch"]) == ("WORN", last.frame_sequence, 0)
    assert ev["capture_time_ms"] == t_of(5) and 0 < ev["region_quality"] <= 1
    assert doc["payload"]["checkpoint_sha256"] == "sha256:" + SHA


def test_unknown_only_and_empty_messages_pass(validator):
    j = PpeJudge()
    feed(j, "cam1", t_of(0), [person("w1", face_conf=0.1)], [NO_HELMET()])
    doc = _message(j, closed_after(0))
    assert [e.message for e in validator.iter_errors(doc)] == []
    assert {doc["payload"]["people"][0][i]["state"] for i in ("helmet", "vest")} == {"UNKNOWN"}
    assert [e.message for e in validator.iter_errors(_message(PpeJudge(), 1000))] == []


def test_expired_state_is_reported_unknown_without_evidence(validator):
    j = PpeJudge()
    for b in range(6):
        feed(j, "cam1", t_of(b), [person("w1")], [HELMET()])
    doc = _message(j, t_of(5) + 10_001)
    helmet = doc["payload"]["people"][0]["helmet"]
    assert (helmet["state"], helmet["reason"], helmet["evidence"]) == ("UNKNOWN", "EVIDENCE_EXPIRED", [])
    assert [e.message for e in validator.iter_errors(doc)] == []


def test_too_many_people_is_refused():
    j = PpeJudge()
    views = {f"w{i}": {} for i in range(65)}
    with pytest.raises(ValueError):
        build_ppe_state(j, views, msg_id="x", sent_at=0, source_id="tier0-01", stream_epoch=0, state_sequence=0,
                        model_version="m", checkpoint_sha256=SHA, input_provenance=PROVENANCE)


@pytest.mark.parametrize("name", ["ppe_state.json", "ppe_state.no_people.json"])
def test_contract_examples_follow_out_of_schema_rules(name):
    doc = json.loads((contract.contracts_dir() / "examples" / name).read_text(encoding="utf-8"))
    assert out_of_schema_violations(doc) == []
