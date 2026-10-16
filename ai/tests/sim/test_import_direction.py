"""sim 은 simai_ai 를 import 해도 되지만 simai_ai 는 sim 을 import 하지 않는다.

import-linter 계약(pyproject.toml)은 공유 파일이라 이 시험으로 먼저 막는다.
"""

import ast
from pathlib import Path

AI_DIR = Path(__file__).resolve().parents[2]


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_simai_ai_does_not_import_sim():
    offenders = [
        p.relative_to(AI_DIR).as_posix()
        for p in sorted((AI_DIR / "src" / "simai_ai").rglob("*.py"))
        if "sim" in _imported_roots(p)
    ]
    assert offenders == []


def test_sim_uses_only_stdlib_and_simai_ai():
    """기본 설치(dependencies 비어 있음)만으로 돌아야 한다."""
    import sys

    allowed = set(sys.stdlib_module_names) | {"sim", "simai_ai"}
    found = {
        p.relative_to(AI_DIR).as_posix(): sorted(_imported_roots(p) - allowed)
        for p in sorted((AI_DIR / "sim").rglob("*.py"))
    }
    assert {k: v for k, v in found.items() if v} == {}
