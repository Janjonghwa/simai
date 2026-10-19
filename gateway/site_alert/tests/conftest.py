"""공용 픽스처: 코어(가짜 소리·가짜 시계)와 계약 스키마 검증기."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest

from simai_site_alert.clock import FakeClock
from simai_site_alert.core import SiteAlertCore
from simai_site_alert.sound import FakeSoundBackend

REPO_ROOT = Path(__file__).resolve().parents[3]


if sys.platform == "win32":
    # 시험 서버·시험 쪽 asyncio 루프의 Windows WinError 10014 일시 오류 보정(S15P31A101-198 시험 보정을 그대로 불러 건다).
    # 제품 코드는 이 보정 없이 호출 지점에서 simai_site_alert.winsock 으로 다시 시도한다(S15P31A101-199). 그 시험은 오류를
    # 이 보정보다 위(가짜 소켓 클래스·주입 함수)에서 내므로 보정에 가려지지 않는다.
    _shim = importlib.util.spec_from_file_location("simai_winsock_retry", REPO_ROOT / "ai/tests/winsock_retry/sitecustomize.py")
    assert _shim is not None and _shim.loader is not None
    _shim_module = importlib.util.module_from_spec(_shim)
    _shim.loader.exec_module(_shim_module)  # 불러오면 install() 한다(listen·connect·socketpair)
    if not getattr(socket.socket.bind, "_simai_winsock_retry", False):  # 시험 HTTP 서버의 bind 도 가끔 10014
        socket.socket.bind = _shim_module._retrying(socket.socket.bind)

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
