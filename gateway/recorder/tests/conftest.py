"""공용 픽스처: 시험용 H.264 파일과 계약 스키마 검증기."""

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


@pytest.fixture
def raw_video(tmp_path: Path) -> Path:
    return make_video(tmp_path / "input.h264")


def _contracts_dir() -> Path:
    return Path(os.environ.get("SIMAI_CONTRACTS_DIR", REPO_ROOT / "contracts"))


@pytest.fixture
def contract_validator():
    """I0 계약(frame_header, codec_info) 검증기. S15P31A101-20 머지 전이면 건너뛴다.

    머지 전 브랜치 스키마로 돌리려면 SIMAI_CONTRACTS_DIR=<-20 worktree>/contracts 를 준다.
    """
    contracts = _contracts_dir()
    if not (contracts / "messages" / "frame_header.schema.json").exists():
        pytest.skip(f"{contracts}/messages/frame_header.schema.json 이 없다 (S15P31A101-20 머지 전)")
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry()
    schemas = {}
    for p in contracts.rglob("*.schema.json"):
        schema = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        schemas[p.name.removesuffix(".schema.json")] = schema

    def validate(name: str, instance: dict) -> None:
        Draft202012Validator(schemas[name], registry=registry).validate(instance)

    return validate
