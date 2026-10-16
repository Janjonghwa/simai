# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema>=4.23", "referencing>=0.35"]
# ///
"""contracts/examples 검증.

- examples/<schema>[.<variant>].json 은 contracts/**/<schema>.schema.json 을 통과해야 한다.
- examples/invalid/<schema>.<case>.json 은 실패해야 한다 (불변 규칙이 스키마로 막히는지 확인).
- 밑줄로 시작하는 최상위 키(_expect_error 등)는 검증 전에 제거한다.

실행: uv run contracts/scripts/validate_examples.py
"""

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

CONTRACTS = Path(__file__).resolve().parent.parent
EXAMPLES = CONTRACTS / "examples"


def load_schemas() -> tuple[Registry, dict[str, dict]]:
    registry = Registry()
    by_name: dict[str, dict] = {}
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
        name = path.name.removesuffix(".schema.json")
        if name in by_name:
            sys.exit(f"스키마 이름 중복: {name}")
        by_name[name] = schema
    return registry, by_name


def strip_meta(doc: dict) -> dict:
    return {k: v for k, v in doc.items() if not k.startswith("_")}


def errors_for(path: Path, registry: Registry, by_name: dict[str, dict]) -> list[str]:
    name = path.name.split(".")[0]
    if name not in by_name:
        return [f"대응 스키마 없음: {name}.schema.json"]
    doc = strip_meta(json.loads(path.read_text(encoding="utf-8")))
    validator = Draft202012Validator(by_name[name], registry=registry)
    return [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in validator.iter_errors(doc)]


def main() -> int:
    registry, by_name = load_schemas()
    for schema in by_name.values():
        Draft202012Validator.check_schema(schema)

    failed = False
    for path in sorted(EXAMPLES.glob("*.json")):
        errs = errors_for(path, registry, by_name)
        print(f"{'OK  ' if not errs else 'FAIL'} {path.relative_to(CONTRACTS)}")
        for e in errs:
            print(f"     {e}")
        failed |= bool(errs)

    for path in sorted((EXAMPLES / "invalid").glob("*.json")):
        errs = errors_for(path, registry, by_name)
        print(f"{'OK  ' if errs else 'FAIL'} {path.relative_to(CONTRACTS)} (실패해야 함)")
        if not errs:
            print("     스키마가 이 위반을 막지 못함")
        failed |= not errs

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
