"""시뮬레이터 시험 공용 fixture.

sim 은 설치 대상이 아니라 ai/ 를 작업 디렉터리로 두고 `python -m sim.run` 으로 돌린다(services 와 같은 방식).
그래서 시험도 실행 명령을 그대로 돌리고 출력 파일을 읽어 검사한다.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
CONTRACTS = AI_DIR.parent / "contracts"
BUILTIN = ("basic", "podium", "crossing")


def _run(scenario: str | Path, out: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "sim.run", "--scenario", str(scenario), "--out", str(out), *extra],
        cwd=AI_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )


@pytest.fixture(scope="session")
def run_sim():
    """`python -m sim.run --scenario <s> --out <dir> [추가 인자]` 를 돌리는 함수."""
    return _run


@pytest.fixture(scope="session")
def builtin_outputs(tmp_path_factory) -> dict[str, Path]:
    """내장 시나리오 3종을 한 번씩 돌린 출력 디렉터리."""
    outs = {}
    for name in BUILTIN:
        out = tmp_path_factory.mktemp(f"sim-{name}")
        result = _run(name, out)
        assert result.returncode == 0, result.stderr
        outs[name] = out
    return outs


@pytest.fixture
def write_scenario(tmp_path):
    """dict 를 시나리오 JSON 파일로 쓰고 경로를 돌려준다."""

    def _write(doc: dict, name: str = "custom.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    return _write


@pytest.fixture(scope="session")
def contract_errors():
    """contracts/ 스키마로 검증해 오류 목록을 돌려주는 함수. jsonschema 가 dev 그룹에 없어서 없으면 건너뛴다.

    스키마 검증까지 돌리려면: uv run --with jsonschema --with referencing pytest tests/sim
    """
    jsonschema = pytest.importorskip("jsonschema")
    referencing = pytest.importorskip("referencing")
    registry = referencing.Registry()
    by_name = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], referencing.Resource.from_contents(schema))
        by_name[path.name.removesuffix(".schema.json")] = schema

    def _errors(name_or_schema: str | dict, doc) -> list[str]:
        schema = by_name[name_or_schema] if isinstance(name_or_schema, str) else name_or_schema
        validator = jsonschema.Draft202012Validator(schema, registry=registry)
        return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(doc)]

    return _errors
