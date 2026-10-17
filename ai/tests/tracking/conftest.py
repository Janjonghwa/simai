"""tracking 시험 공용 fixture: 계약 스키마 검증, 시뮬레이터 측정 스크립트, 내장 시나리오 출력."""

import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
CONTRACTS = AI_DIR.parent / "contracts"


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


@pytest.fixture(scope="session")
def sim_tracking():
    """tests/tracking/sim_tracking.py (측정 스크립트) 모듈. 시험도 스크립트와 같은 변환·지표 함수를 쓴다."""
    spec = importlib.util.spec_from_file_location("sim_tracking", HERE / "sim_tracking.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sim_outputs(sim_tracking, tmp_path_factory) -> dict[str, Path]:
    """내장 시나리오 basic, podium, crossing 을 한 번씩 돌린 출력 디렉터리."""
    root = tmp_path_factory.mktemp("sim-tracking")
    return {name: sim_tracking.run_sim(name, root) for name in sim_tracking.SCENARIOS}
