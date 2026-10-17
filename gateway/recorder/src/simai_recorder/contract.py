"""녹화 manifest 계약 검증 (`contracts/data/recording_manifest.schema.json`, S15P31A101-158).

형식의 정본은 저장소 루트의 계약 스키마다. 이 도구는 독립 uv 프로젝트라 스키마를 복사하지 않고 저장소의 contracts/ 를
직접 읽는다(환경 변수 SIMAI_CONTRACTS_DIR 이 있으면 그것). 오류 문장에는 받은 값을 넣지 않는다: RTSP 주소처럼
비밀 값이 섞일 수 있는 값이 그대로 화면·로그에 찍히지 않게 경로와 위반 종류만 적는다.
"""

from __future__ import annotations

import json
import os
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry, Resource

CONTRACTS_DIR_ENV = "SIMAI_CONTRACTS_DIR"
MANIFEST_SCHEMA = Path("data") / "recording_manifest.schema.json"
MANIFEST_SCHEMA_ID = "https://simai.dev/contracts/data/recording_manifest.schema.json"


class ContractsNotFoundError(FileNotFoundError):
    """계약 스키마(contracts/)를 찾지 못했다. 설정 오류라 명령줄은 트레이스백 없이 이 문장만 알린다."""


def contracts_dir() -> Path:
    """저장소의 contracts/ 위치. SIMAI_CONTRACTS_DIR 이 있으면 그것, 없으면 이 파일에서 위로 올라가며 찾는다.

    SIMAI_CONTRACTS_DIR 이 manifest 스키마가 없는 경로를 가리키면 그 경로를 적은 ContractsNotFoundError 를 낸다
    (스키마 없이 검증하다 $ref 해석 오류로 끝나지 않게).
    """
    override = os.environ.get(CONTRACTS_DIR_ENV)
    if override:
        root = Path(override)
        if not (root / MANIFEST_SCHEMA).is_file():
            raise ContractsNotFoundError(
                f"{CONTRACTS_DIR_ENV}={override} 에 {MANIFEST_SCHEMA.as_posix()} 가 없다. 저장소의 contracts/ 디렉터리를 가리킨다"
            )
        return root
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "contracts"
        if (candidate / MANIFEST_SCHEMA).is_file():
            return candidate
    raise ContractsNotFoundError(
        f"contracts/{MANIFEST_SCHEMA.as_posix()} 를 찾지 못했다(저장소 밖에서 실행). {CONTRACTS_DIR_ENV} 로 contracts/ 위치를 지정한다"
    )


@cache
def _registry(root: Path) -> Registry:
    registry: Registry = Registry()
    for path in sorted(root.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    return registry


def _validator(fragment: str) -> Draft202012Validator:
    """manifest 스키마 전체(fragment "") 또는 그 안의 정의(예: "#/$defs/consent_requirement") 검증기."""
    registry = _registry(contracts_dir().resolve())
    return Draft202012Validator({"$ref": MANIFEST_SCHEMA_ID + fragment}, registry=registry,
                                format_checker=Draft202012Validator.FORMAT_CHECKER)


def _describe(e: ValidationError) -> str:
    """받은 값(instance)을 넣지 않는 위반 설명."""
    if e.validator in ("required", "additionalProperties"):
        return e.message  # 필드 이름만 들어 있다
    if e.validator is None:
        return "이 입력 종류에서는 쓸 수 없는 필드가 있다"
    if e.validator == "enum":
        return f"{json.dumps(e.validator_value, ensure_ascii=False)} 중 하나여야 한다"
    if e.validator == "format":
        return f"{e.validator_value} 형식이어야 한다"
    if e.validator == "pattern":
        text = "형식이 맞지 않다"
    elif e.validator == "const":
        text = f"{json.dumps(e.validator_value, ensure_ascii=False)} 이어야 한다"
    elif e.validator == "type":
        text = f"형식이 {e.validator_value} 이어야 한다"
    elif e.validator == "contains":
        text = f"{json.dumps(e.validator_value, ensure_ascii=False)} 항목이 있어야 한다"
    else:
        text = f"{e.validator} {json.dumps(e.validator_value, ensure_ascii=False)} 위반"
    desc = e.schema.get("description") if isinstance(e.schema, dict) else None
    return text + (f" ({desc})" if desc else "")


def _leaf(e: ValidationError) -> ValidationError:
    while e.context:
        e = best_match(e.context)
    return e


def schema_errors(instance: Any, fragment: str = "") -> list[str]:
    """계약 스키마 검증. "<경로>: <위반>" 문장 목록(비면 통과)."""
    out: list[str] = []
    for e in sorted(_validator(fragment).iter_errors(instance), key=lambda e: list(map(str, e.absolute_path))):
        leaf = _leaf(e)
        text = f"{'/'.join(map(str, leaf.absolute_path)) or '<root>'}: {_describe(leaf)}"
        if text not in out:
            out.append(text)
    return out
