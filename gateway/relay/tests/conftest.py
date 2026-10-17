"""공용 픽스처: 시험용 H.264 파일과 I0 계약 스키마 검증기 (저장소의 contracts/ 를 직접 읽는다)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from videos import make_video

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def mp4_video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "input.mp4")


def contracts_dir() -> Path:
    return Path(os.environ.get("SIMAI_CONTRACTS_DIR", REPO_ROOT / "contracts"))


@pytest.fixture(scope="session")
def contract_validator():
    """validate(스키마 이름, 값): contracts/**/<이름>.schema.json 으로 검증한다. 실패하면 ValidationError."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry()
    schemas = {}
    for p in contracts_dir().rglob("*.schema.json"):
        schema = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        schemas[p.name.removesuffix(".schema.json")] = schema
    assert "frame_header" in schemas and "codec_info" in schemas, f"{contracts_dir()} 에 I0 스키마가 없다"

    def validate(name: str, instance: dict) -> None:
        Draft202012Validator(schemas[name], registry=registry).validate(instance)

    return validate


@pytest.fixture(scope="session")
def contract_example():
    def load(name: str) -> dict:
        return json.loads((contracts_dir() / "examples" / f"{name}.json").read_text(encoding="utf-8"))

    return load
