"""contracts/ JSON Schema 에서 ai 의 pydantic v2 모델(ai/src/simai_ai/contracts/models.py)을 만든다(명세 21.2).

실행(ai/ 에서, 생성기는 ai dev 그룹에 판을 고정해 둔다):
  uv run python ../contracts/scripts/generate_python_models.py          다시 만든다
  uv run python ../contracts/scripts/generate_python_models.py --check  다시 만든 결과가 커밋된 파일과 다르면 종료 1

- 모든 스키마를 한 문서의 $defs 로 묶어 datamodel-code-generator 에 넘긴다. 생성기가 파일을 건너가는 상대 $ref 와
  https $id 를 제대로 풀지 못해서다(봉투를 allOf 로 가져온 뒤의 ../common/defs 를 잘못된 경로로 찾는다).
- 설명문(title·description·$comment·examples)과 format 은 뺀다. 검증 정본(jsonschema)도 format 을 검사하지 않는다.
- 열린 객체(properties 가 있고 additionalProperties 를 적지 않은 객체)는 additionalProperties: true 로 적어 모르는 필드를
  지우지 않고 그대로 싣게 한다(extra='allow'). 닫힌 객체는 생성기가 extra='forbid' 로 만든다.
- contains 는 뺀다. 생성기가 allOf 안의 contains 를 필드 없는 모델과의 합집합으로 만들어 배열 자리에 객체를 받게 된다.
- 모델은 if/then/else·not·조건 분기로 적은 불변 규칙을 검사하지 않는다. 계약 검증은 jsonschema 가 정본이다.
  allOf 로 $ref 한 정의를 같은 자리에서 좁히는 제약(event_ack 의 ack.channel enum, observation_summary 의 archive_uri
  pattern)도 생성기가 합치지 못해 모델에 없다(ai/tests/contracts/test_models.py 의 KNOWN_GAPS).
"""

from __future__ import annotations

import json
import posixpath
import subprocess
import sys
import tempfile
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parent.parent
OUT = CONTRACTS.parent / "ai" / "src" / "simai_ai" / "contracts" / "models.py"
DEFS_FILE = "common/defs.schema.json"
ROOT_CLASS = "Contracts"
HEADER = (
    "# contracts/ 에서 생성한 파일이다. 손으로 고치지 않는다.\n"
    "# 다시 만들기(ai/ 에서): uv run python ../contracts/scripts/generate_python_models.py\n"
    f"# 스키마 이름 → 모델: {ROOT_CLASS}.model_fields['<스키마 이름>'] (예: world_state → WorldState)"
)
ANNOTATIONS = {"title", "description", "$comment", "examples", "format", "$id", "$schema", "contains"}
VALUE_KEYWORDS = {"const", "enum", "default"}
NAME_MAPS = {"properties", "$defs", "patternProperties"}
CONDITIONALS = {"if", "then", "else", "not"}
BRANCHES = {"allOf", "anyOf", "oneOf"}
CODEGEN_ARGS = [
    "--input-file-type", "jsonschema",
    "--output-model-type", "pydantic_v2.BaseModel",
    "--target-python-version", "3.11",
    "--formatters", "builtin",
    "--disable-timestamp",
    "--use-standard-collections",
    "--use-union-operator",
    "--use-annotated",
    "--field-constraints",
    "--enum-field-as-literal", "all",
    "--class-name", ROOT_CLASS,
    # 선택 필드는 없으면 MISSING(pydantic 2.14), null 은 스키마가 허용할 때만. 없음과 null 을 섞지 않는다
    "--use-missing-sentinel",
    # 문자열 "1"·true 를 정수로 바꿔 받지 않는다(jsonschema 처럼 JSON 타입 그대로). float 자리는 정수도 받는다
    "--strict-types", "str", "int", "float", "bool",
    # 생성기는 실험 모듈 경로로 가져오는데 2.14 부터 그 경로는 경고를 낸다
    "--import-overrides", '{"MISSING": "pydantic"}',
]


def schema_name(rel: str) -> str:
    return posixpath.basename(rel).removesuffix(".schema.json")


def bundled_ref(ref: str, rel: str) -> str:
    """파일 사이 $ref 를 묶음 문서 안 #/$defs/<스키마>[__<정의>] 로 바꾼다."""
    file, _, fragment = ref.partition("#")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), file)) if file else rel
    if fragment in ("", "/"):
        if target == DEFS_FILE:
            raise ValueError(f"{rel}: 공통 정의 파일 전체를 $ref 할 수 없다: {ref}")
        return f"#/$defs/{schema_name(target)}"
    if not fragment.startswith("/$defs/") or "/" in fragment.removeprefix("/$defs/"):
        raise ValueError(f"{rel}: $defs 의 정의 하나만 $ref 할 수 있다: {ref}")
    return f"#/$defs/{schema_name(target)}__{fragment.removeprefix('/$defs/')}"


def rewrite(node, rel: str, conditional: bool = False, branch_of_closed: bool = False):
    """branch_of_closed: 닫힌 객체의 allOf/anyOf/oneOf 갈래. type 없는 갈래는 부모 필드를 좁히는 조각이라 열지 않는다
    (열면 생성기가 부모의 additionalProperties: false 를 갈래의 true 로 덮는다)."""
    if isinstance(node, list):
        return [rewrite(item, rel, conditional, branch_of_closed) for item in node]
    if not isinstance(node, dict):
        return node
    closed = node.get("additionalProperties") is False
    out = {}
    for key, value in node.items():
        if key in ANNOTATIONS:
            continue
        if key == "$ref":
            out[key] = bundled_ref(value, rel)
        elif key in VALUE_KEYWORDS:
            out[key] = value
        elif key in NAME_MAPS:
            out[key] = {name: rewrite(sub, rel, conditional) for name, sub in value.items()}
        elif key in BRANCHES:
            parts = [part for part in rewrite(value, rel, conditional, closed) if part]
            if parts:
                out[key] = parts
        else:
            out[key] = rewrite(value, rel, conditional or key in CONDITIONALS)
    opens = out.get("type") == "object" or ("type" not in out and not branch_of_closed)
    if not conditional and opens and "properties" in out and "additionalProperties" not in out:
        out["additionalProperties"] = True
    return out


def bundle() -> dict:
    defs: dict[str, dict] = {}
    roots: list[str] = []
    for path in sorted(CONTRACTS.rglob("*.schema.json")):
        rel = path.relative_to(CONTRACTS).as_posix()
        schema = rewrite(json.loads(path.read_text(encoding="utf-8")), rel)
        name = schema_name(rel)
        for sub, value in schema.pop("$defs", {}).items():
            defs[f"{name}__{sub}"] = value
        if rel != DEFS_FILE:
            if name in defs:
                raise ValueError(f"스키마 이름 중복: {name}")
            defs[name] = schema
            roots.append(name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "properties": {name: {"$ref": f"#/$defs/{name}"} for name in sorted(roots)},
        "$defs": dict(sorted(defs.items())),
    }


def generate() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / "contracts.json", Path(tmp) / "models.py"
        src.write_text(json.dumps(bundle(), ensure_ascii=False, indent=1), encoding="utf-8")
        cmd = [sys.executable, "-m", "datamodel_code_generator", "--input", str(src), "--output", str(dst)]
        cmd += [*CODEGEN_ARGS, "--custom-file-header", HEADER]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if done.returncode != 0:
            raise RuntimeError(f"datamodel-codegen 실패(종료 {done.returncode}):\n{done.stderr[-4000:]}")
        return dst.read_text(encoding="utf-8").replace("\r\n", "\n")


def main(argv: list[str]) -> int:
    unknown = [arg for arg in argv if arg != "--check"]
    if unknown:
        print(f"모르는 옵션: {' '.join(unknown)} (--check 만 받는다)", file=sys.stderr)
        return 2
    text = generate()
    if "--check" in argv:
        current = OUT.read_text(encoding="utf-8").replace("\r\n", "\n") if OUT.exists() else None
        if current != text:
            print(f"생성 파일이 contracts/ 와 다르다: {OUT.as_posix()}", file=sys.stderr)
            print("ai/ 에서 uv run python ../contracts/scripts/generate_python_models.py 로 다시 만들어 커밋한다.", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
