"""공용 픽스처: 시험용 H.264 파일과 I0 계약 스키마 검증기 (저장소의 contracts/ 를 직접 읽는다)."""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
from pathlib import Path

import pytest
from videos import make_video

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


@pytest.fixture(autouse=True)
def _epoch_state_in_tmp(tmp_path: Path, monkeypatch):
    """CLI 를 부르는 시험이 사용자 폴더(~/.simai)에 epoch 저장 파일을 만들지 않게 한다."""
    from simai_relay import cli

    monkeypatch.setattr(cli, "DEFAULT_EPOCH_STATE", tmp_path / "relay-epochs.json")


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
