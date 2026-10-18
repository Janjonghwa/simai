"""contact 시험 공용 fixture: 계약 스키마 검증, 합성 World Track 결과."""

import json
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts"


@pytest.fixture(scope="session")
def contract_errors():
    """contracts/ 스키마로 검증해 오류 목록을 돌려주는 함수 (jsonschema 는 dev 그룹)."""
    import jsonschema
    import referencing

    registry = referencing.Registry()
    by_name = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], referencing.Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema

    def _errors(name: str, doc) -> list[str]:
        validator = jsonschema.Draft202012Validator(by_name[name], registry=registry)
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(doc)]

    return _errors
