"""Validate a run record against repository schemas without network resolution.

Requires jsonschema and referencing; this validator never imports numerical modules.
"""

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    args = parser.parse_args()
    contracts = Path(__file__).resolve().parents[3] / "contracts"
    registry = Registry()
    for path in contracts.rglob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(schema["$id"], Resource.from_contents(schema))
    schema = json.loads((contracts / "experiments/run_record.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())
    validator.validate(json.loads(args.record.read_text(encoding="utf-8")))
    print(f"Schema OK: {args.record}")


if __name__ == "__main__":
    main()
