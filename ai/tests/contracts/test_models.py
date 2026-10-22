"""contracts/ 에서 생성한 pydantic 모델(simai_ai.contracts.models)의 계약 시험(명세 21.2, S15P31A101-24).

- 재생성 결과가 커밋된 models.py 와 같다(--check). 손으로 고치면 실패한다.
- 예시(contracts/examples, invalid 제외)는 모두 모델을 거쳐 그대로 돌아온다(없음과 null, 모르는 필드 포함).
- invalid 예시 중 위반이 조건 분기 밖(type·required·enum·const·pattern·범위·닫힌 객체)인 것은 모델도 거부한다.
  if/then/else·not·oneOf/anyOf 로 적은 규칙은 모델이 보지 않는다(jsonschema 가 정본).
"""

import copy
import importlib.util
import json
import subprocess
import sys
import typing
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, RootModel, ValidationError
from referencing import Registry, Resource

from simai_ai.contracts import models

REPO = Path(__file__).resolve().parents[3]
CONTRACTS = REPO / "contracts"
EXAMPLES = CONTRACTS / "examples"
GENERATOR = CONTRACTS / "scripts" / "generate_python_models.py"
# 생성기가 allOf 로 $ref 한 정의를 같은 자리에서 좁히는 제약을 합치지 못해 모델이 놓치는 위반(jsonschema 는 잡는다)
KNOWN_GAPS = {
    "event_ack.site_button_via_ec2.json",  # ack: allOf operator_ack + channel enum 축소 → 모델은 operator_ack 의 넓은 enum
    "observation_summary.data_uri_archive.json",  # archive_uri: allOf uri + pattern → 모델은 uri 만
}
CONDITIONAL = {"if", "then", "else", "not", "oneOf", "anyOf", "uniqueItems", "propertyNames", "contains"}


def _strip_meta(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def _load(path: Path) -> dict:
    return _strip_meta(json.loads(path.read_text(encoding="utf-8")))


def _schema_name(path: Path) -> str:
    return path.name.split(".")[0]


MODEL_BY_SCHEMA = {
    name: next(a for a in typing.get_args(field.annotation) if isinstance(a, type) and issubclass(a, BaseModel))
    for name, field in models.Contracts.model_fields.items()
}


def _model(path: Path) -> type[BaseModel]:
    return MODEL_BY_SCHEMA[_schema_name(path)]


def _dump(obj: BaseModel) -> dict:
    return obj.model_dump(mode="json", by_alias=True)


def _schemas():
    registry, by_name = Registry(), {}
    for path in CONTRACTS.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema
    return registry, by_name


VALID = sorted(EXAMPLES.glob("*.json"))
INVALID = sorted((EXAMPLES / "invalid").glob("*.json"))


def _generator_module():
    spec = importlib.util.spec_from_file_location("generate_python_models", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generated_file_is_current():
    done = subprocess.run([sys.executable, str(GENERATOR), "--check"], capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr


def test_check_fails_when_generated_file_is_edited_or_missing(tmp_path, monkeypatch):
    """음성 대조: 생성물을 손으로 바꾸거나 지우면 --check 가 실패한다."""
    gen = _generator_module()
    committed = (Path(models.__file__)).read_text(encoding="utf-8")
    monkeypatch.setattr(gen, "generate", lambda: committed.replace("\r\n", "\n"))
    edited = tmp_path / "models.py"
    edited.write_text(committed.replace("ge=0", "ge=1", 1), encoding="utf-8")
    monkeypatch.setattr(gen, "OUT", edited)
    assert gen.main(["--check"]) == 1
    edited.unlink()
    assert gen.main(["--check"]) == 1
    edited.write_text(committed, encoding="utf-8")
    assert gen.main(["--check"]) == 0
    assert gen.main(["--bogus"]) == 2


def test_every_schema_has_its_named_model():
    """스키마 이름 world_state → 클래스 WorldState. 이름 충돌로 다른 클래스가 그 이름을 가져가면 실패한다."""
    names = {p.name.removesuffix(".schema.json") for p in CONTRACTS.rglob("*.schema.json")} - {"defs"}
    assert set(MODEL_BY_SCHEMA) == names
    for name, model in MODEL_BY_SCHEMA.items():
        camel = "".join(part.capitalize() for part in name.split("_"))
        assert model.__name__ == camel and getattr(models, camel) is model


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.name)
def test_example_round_trips(path):
    doc = _load(path)
    assert _dump(_model(path).model_validate(doc)) == doc


def test_every_object_model_is_open_or_closed():
    """열린 객체는 모르는 필드를 싣고(allow), 닫힌 객체는 거부한다(forbid). 기본값(ignore)이면 조용히 지운다.
    필드 없는 클래스(조건 규칙만 있는 공통 정의, 예: severity_upward_only)는 다른 모델의 부모로만 쓰여 뺀다."""
    classes = [
        c for c in vars(models).values()
        if isinstance(c, type) and issubclass(c, BaseModel) and not issubclass(c, RootModel)
        and c.__module__ == models.__name__ and c.model_fields
    ]
    assert classes
    assert {c.__name__ for c in classes if c.model_config.get("extra") not in ("allow", "forbid")} == set()


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_structural_violation_is_rejected(path):
    registry, by_name = _schemas()
    doc = _load(path)
    errors = list(Draft202012Validator(by_name[_schema_name(path)], registry=registry).iter_errors(doc))
    assert errors, "invalid 예시가 스키마를 통과한다"
    structural = any(not ({str(p) for p in e.schema_path} & CONDITIONAL) for e in errors)
    try:
        _model(path).model_validate(doc)
        rejected = False
    except ValidationError:
        rejected = True
    if path.name in KNOWN_GAPS:
        assert structural and not rejected, "알려진 빈틈이 메워졌다: KNOWN_GAPS 와 생성기 docstring 에서 뺀다"
    elif structural:
        assert rejected, "조건 분기 밖 위반인데 모델이 받아들인다"


# 반례 시험 -------------------------------------------------------------------


def _world_state() -> dict:
    return _load(EXAMPLES / "world_state.json")


def test_unknown_fields_survive_in_open_objects_and_fail_in_closed():
    doc = _world_state()
    doc["future_envelope_field"] = 1
    doc["payload"]["future_payload_field"] = {"a": [1, 2]}
    doc["payload"]["tracks"][0]["future_track_field"] = "x"
    assert _dump(models.WorldState.model_validate(doc)) == doc

    bad = _world_state()
    bad["payload"]["tracks"][0]["skeleton"]["extra"] = 1  # skeleton 은 닫힌 값 객체
    with pytest.raises(ValidationError):
        models.WorldState.model_validate(bad)


def test_absent_and_null_stay_distinct():
    """모르면 비운다: 접지점 null 은 키째 남고 0 으로 채워지지 않으며, 없는 선택 필드는 null 로 생기지 않는다."""
    doc = _world_state()
    track = next(t for t in doc["payload"]["tracks"] if t["ground_anchor_xyz"] is None)
    assert "surface_source" not in track
    obj = models.WorldState.model_validate(doc)
    out = next(t for t in _dump(obj)["payload"]["tracks"] if t["world_id"] == track["world_id"])
    assert out["ground_anchor_xyz"] is None and "surface_source" not in out

    nulled = copy.deepcopy(doc)
    next(t for t in nulled["payload"]["tracks"] if t["world_id"] == track["world_id"])["surface_source"] = None
    with pytest.raises(ValidationError):  # 선택 필드에 null 을 넣으면 '없음' 이 아니다
        models.WorldState.model_validate(nulled)

    missing = copy.deepcopy(doc)
    del next(t for t in missing["payload"]["tracks"] if t["world_id"] == track["world_id"])["ground_anchor_xyz"]
    with pytest.raises(ValidationError):  # 필수 nullable 키를 빼면 거부(null 로 채우지 않는다)
        models.WorldState.model_validate(missing)


def test_int64_ms_and_float_boundaries_round_trip():
    doc = _world_state()
    doc["sent_at"] = 2**63 - 1  # UTC int64 ms 의 끝값이 float 로 뭉개지지 않는다
    doc["payload"]["t_latest_global"] = 2**53 + 1
    assert _dump(models.WorldState.model_validate(doc)) == doc
    for bad in (-1, 1.5, "1", True):  # 문자열·불리언을 정수로 바꿔 받지 않는다
        broken = _world_state()
        broken["sent_at"] = bad
        with pytest.raises(ValidationError):
            models.WorldState.model_validate(broken)


def test_msg_type_const_and_semver_are_enforced():
    doc = _world_state()
    doc["msg_type"] = "risk_event"
    with pytest.raises(ValidationError):
        models.WorldState.model_validate(doc)
    doc = _world_state()
    doc["schema_version"] = "2.1"
    with pytest.raises(ValidationError):
        models.WorldState.model_validate(doc)
