"""공용 픽스처: 코어(가짜 소리·가짜 시계)와 계약 스키마 검증기."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from simai_site_alert.clock import FakeClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.sound import FakeSoundBackend

REPO_ROOT = Path(__file__).resolve().parents[3]
LAPTOP_ID = "laptop-01"


def contracts_dir() -> Path:
    return Path(os.environ.get("SIMAI_CONTRACTS_DIR", REPO_ROOT / "contracts"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sound() -> FakeSoundBackend:
    return FakeSoundBackend()


@pytest.fixture
def core(sound: FakeSoundBackend, clock: FakeClock) -> SiteAlertCore:
    return SiteAlertCore(sound, laptop_id=LAPTOP_ID, clock=clock)


def load_example(name: str) -> dict:
    return json.loads((contracts_dir() / "examples" / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def contract_validator():
    """contracts/ 스키마 검증기. validate(스키마 이름, 인스턴스)."""
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    registry = Registry()
    schemas = {}
    for p in contracts_dir().rglob("*.schema.json"):
        schema = json.loads(p.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        schemas[p.name.removesuffix(".schema.json")] = schema

    def validate(name: str, instance: dict) -> None:
        Draft202012Validator(schemas[name], registry=registry).validate(instance)

    return validate
