"""변경된 파일 목록으로부터 영향 받는 ai pytest 시험 경로만 고르는 스크립트 (S15P31A101-255).

사용법:
  - ai/ 디렉터리에서: `uv run python -m scripts.select_tests`
  - 저장소 루트에서: `uv run --project ai python ai/scripts/select_tests.py`
  - 특정 커밋 기준: `uv run python -m scripts.select_tests --commit 6393a88`
  - 기준 브랜치 지정: `uv run python -m scripts.select_tests --base origin/dev`

출력:
  - 전체 실행이 필요한 경우: `ALL` 한 줄 (원인은 stderr 에 기록)
  - 영향 받는 ai/경로 규칙 변경이 없는 경우: 빈 출력
  - 선택된 시험이 있는 경우: `ai/` 기준 경로(정렬, 한 줄에 하나)
  - 종료 코드는 언제나 0 (인자·git·내부 분석 오류 발생 시 stderr 에 원인을 남기고 안전하게 `ALL` 출력 후 0 종료)
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
AI_DIR = SCRIPT_PATH.parents[1]
REPO_ROOT = AI_DIR.parent
DEFAULT_CONFIG_PATH = SCRIPT_PATH.with_name("select_tests.json")

_AI_SUBPREFIXES = (
    "src/simai_ai/",
    "services/",
    "tests/",
    "experiments/",
    "sim/",
    "scripts/",
)
_AI_ROOT_FILES = {"pyproject.toml", "uv.lock", "CONVENTION.md", ".python-version"}

DEFAULT_PATH_RULES: dict[str, list[str]] = {
    "deploy/**": ["tests/deploy"],
    "ai/sim/**": ["tests/sim"],
    "ai/experiments/<name>/**": ["tests/experiments/<name>"],
    "ai/services/tier0/**": [
        "tests/deploy/test_session1_live.py",
        "tests/deploy/test_crash_recovery_g3.py",
    ],
    # 현장 경보 단말을 실제 Tier 0 에 붙이는 왕복 시험(S15P31A101-289)은 단말 소스를 import 한다
    "gateway/site_alert/**": ["tests/services/tier0/test_site_alert_clock_sync.py"],
}


def _unquote_git_path(text: str) -> str:
    """Git C-style quoted 경로(`"..."`, 8진수 이스케이프 포함)를 복원한다."""
    s = text.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        inner = s[1:-1]
        out_bytes = bytearray()
        i = 0
        n = len(inner)
        while i < n:
            ch = inner[i]
            if ch == "\\" and i + 1 < n:
                nxt = inner[i + 1]
                if nxt in "01234567" and i + 3 < n and all(c in "01234567" for c in inner[i + 1 : i + 4]):
                    out_bytes.append(int(inner[i + 1 : i + 4], 8))
                    i += 4
                    continue
                escape_map = {"n": ord("\n"), "t": ord("\t"), "r": ord("\r"), '"': ord('"'), "\\": ord("\\")}
                if nxt in escape_map:
                    out_bytes.append(escape_map[nxt])
                    i += 2
                    continue
            out_bytes.extend(ch.encode("utf-8"))
            i += 1
        return out_bytes.decode("utf-8", errors="replace")
    return s


def _is_git_status_token(tok: str) -> bool:
    if not tok:
        return False
    if tok in {"??", "!!"}:
        return True
    head = tok[0]
    tail = tok[1:]
    return head in "ACDMRTUXB" and (not tail or tail.isdigit() or (len(tail) == 1 and tail in "ACDMRTUXB"))


def _parse_git_z_name_status(output: str) -> list[str]:
    """`git diff --name-status -z` 또는 `git diff-tree --name-status -z` NUL 구분 출력을 파싱한다."""
    tokens = output.split("\0")
    paths: list[str] = []
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        if _is_git_status_token(tok):
            if tok[0] in ("R", "C"):
                if i + 1 < n and tokens[i + 1]:
                    paths.append(tokens[i + 1])
                if i + 2 < n and tokens[i + 2]:
                    paths.append(tokens[i + 2])
                i += 3
                continue
            if i + 1 < n and tokens[i + 1]:
                paths.append(tokens[i + 1])
            i += 2
            continue
        paths.append(tok)
        i += 1
    return paths


def _normalize_single_path(item: str) -> str:
    p = _unquote_git_path(item).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    if not p:
        return ""
    if not p.startswith("ai/") and (p.startswith(_AI_SUBPREFIXES) or p in _AI_ROOT_FILES):
        p = f"ai/{p}"
    return p


def _normalize_entry(raw: str) -> list[str]:
    """단일 파일 경로, `-z` NUL 출력, 또는 `git diff --name-status` 한 줄을 저장소 루트 기준 POSIX 경로 목록으로 변환한다."""
    if "\0" in raw:
        candidates = _parse_git_z_name_status(raw)
    else:
        line = raw.strip()
        if not line:
            return []
        if "\t" in line:
            parts = [p.strip() for p in line.split("\t") if p.strip()]
            if parts and _is_git_status_token(parts[0]):
                candidates = parts[1:]
            else:
                candidates = parts
        elif " -> " in line:
            # git status --short rename 형식 (예: `R  old path -> new path` 또는 `old path -> new path`)
            left, right = line.split(" -> ", 1)
            left_stripped = left.strip()
            left_tokens = left_stripped.split(maxsplit=1)
            if len(left_tokens) == 2 and _is_git_status_token(left_tokens[0]):
                left_path = left_tokens[1]
            else:
                left_path = left_stripped
            candidates = [left_path, right.strip()]
        else:
            tokens = line.split(maxsplit=1)
            if len(tokens) == 2 and _is_git_status_token(tokens[0]):
                candidates = [tokens[1]]
            else:
                candidates = [line]

    normalized: list[str] = []
    for item in candidates:
        p = _normalize_single_path(item)
        if p:
            normalized.append(p)
    return normalized


def _parse_changed_files(changed_files: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in changed_files:
        for norm in _normalize_entry(raw):
            if norm not in seen:
                seen.add(norm)
                out.append(norm)
    return out


def _load_config(ai_dir: Path, config_path: Path | None) -> tuple[list[str], dict[str, list[str]]]:
    if config_path is not None:
        target = config_path
    else:
        candidate = ai_dir / "scripts" / "select_tests.json"
        target = candidate if candidate.is_file() else DEFAULT_CONFIG_PATH

    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("always_include"), list):
        raise ValueError(f"Invalid select_tests config format in {target}")
    always_include: list[str] = []
    for item in data["always_include"]:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"Invalid always_include item in {target}: {item!r}")
        always_include.append(item.replace("\\", "/").strip("/"))

    raw_rules = data.get("path_rules")
    if raw_rules is None:
        path_rules = dict(DEFAULT_PATH_RULES)
    elif not isinstance(raw_rules, dict):
        raise ValueError(f"Invalid path_rules in {target}: expected object")
    else:
        path_rules = {}
        for k, v in raw_rules.items():
            if not isinstance(k, str) or not k.strip() or not isinstance(v, list):
                raise ValueError(f"Invalid path_rules entry in {target}: {k!r} -> {v!r}")
            cleaned_targets: list[str] = []
            for t in v:
                if not isinstance(t, str) or not t.strip():
                    raise ValueError(f"Invalid path_rules target in {target}: {t!r}")
                cleaned_targets.append(t.replace("\\", "/").strip("/"))
            path_rules[k.replace("\\", "/").strip("/")] = cleaned_targets

    return always_include, path_rules


def _match_path_rule(repo_path: str, pattern: str, targets: list[str]) -> list[str]:
    """단일 `repo_path` 가 `path_rules` 의 `pattern` 에 매칭되면 치환된 대상 시험 경로 목록을 반환한다."""
    norm_pat = pattern.replace("\\", "/").strip("/")
    for suffix in ("/**", "/*"):
        if norm_pat.endswith(suffix):
            norm_pat = norm_pat[: -len(suffix)].rstrip("/")

    if not norm_pat.startswith("ai/") and (
        norm_pat.startswith(_AI_SUBPREFIXES) or norm_pat in {"sim", "experiments", "services", "tests", "scripts"}
    ):
        norm_pat = f"ai/{norm_pat}"

    placeholders = ("<name>", "<이름>", "*")
    if any(ph in norm_pat for ph in placeholders):
        pat_parts = [p for p in norm_pat.split("/") if p]
        path_parts = [p for p in repo_path.split("/") if p]
        if len(path_parts) < len(pat_parts):
            return []
        captured: str | None = None
        for pp, rp in zip(pat_parts, path_parts):
            if pp in placeholders:
                captured = rp
            elif pp != rp:
                return []
        if not captured:
            return []
        resolved: list[str] = []
        for t in targets:
            out_t = t
            for ph in placeholders:
                out_t = out_t.replace(ph, captured)
            resolved.append(out_t.replace("\\", "/").strip("/"))
        return resolved

    if repo_path == norm_pat or repo_path.startswith(f"{norm_pat}/"):
        return [t.replace("\\", "/").strip("/") for t in targets]
    return []


def _is_all_trigger(repo_path: str, ai_dir: Path, config_path: Path | None) -> bool:
    """전체 시험(ALL)을 돌려야 하는 변경인지 판정한다."""
    if repo_path == "contracts" or repo_path.startswith("contracts/"):
        return True

    if Path(repo_path).name == "conftest.py":
        return True

    if not repo_path.startswith("ai/"):
        return False

    rel = repo_path[3:]
    if rel in {"uv.lock", "pyproject.toml"}:
        return True

    if rel == "src/simai_ai/contracts" or rel.startswith("src/simai_ai/contracts/"):
        return True

    if rel in {"scripts/select_tests.py", "scripts/select_tests.json", "scripts/__init__.py"}:
        return True

    if Path(rel).name in {"select_tests.py", "select_tests.json"}:
        return True

    if config_path is not None:
        try:
            resolved_cfg = config_path.resolve()
            if (ai_dir / rel).resolve() == resolved_cfg or (ai_dir.parent / repo_path).resolve() == resolved_cfg:
                return True
        except OSError:
            return True

    return False


def _py_rel_to_module(rel_parts: tuple[str, ...]) -> tuple[str, str, bool]:
    """`(module_name, current_package, is_package_init)` 을 반환한다."""
    if not rel_parts:
        return "", "", False
    if rel_parts[-1] == "__init__.py":
        pkg = ".".join(rel_parts[:-1])
        return pkg, pkg, True
    stem = rel_parts[-1][:-3] if rel_parts[-1].endswith(".py") else rel_parts[-1]
    mod_parts = [*rel_parts[:-1], stem]
    pkg = ".".join(rel_parts[:-1])
    return ".".join(mod_parts), pkg, False


def _module_context_for_path(py_path: Path, ai_dir: Path) -> tuple[str, str, bool]:
    rel = py_path.relative_to(ai_dir)
    parts = rel.parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    return _py_rel_to_module(parts)


def _is_package_module(mod_name: str, ai_dir: Path) -> bool:
    """`mod_name` 이 `__init__.py` 를 가진 패키지인지 확인한다."""
    if not mod_name:
        return False
    parts = mod_name.split(".")
    if parts[0] == "simai_ai":
        return (ai_dir / "src" / Path(*parts) / "__init__.py").is_file()
    return (ai_dir / Path(*parts) / "__init__.py").is_file()


def extract_ast_imports(source: str, current_pkg: str = "", *, filename: str = "<unknown>") -> set[str]:
    """파이썬 소스 문자열에서 정적 import 대상 모듈 후보들을 추출한다.

    - `import a.b` -> `a.b`
    - `from x import y` -> `x`, `x.y` (`y`가 하위 모듈인 경우 포함)
    - 상대 import (`from . import y`, `from ..sub import z`) -> `current_pkg` 기준 절대 모듈 이름으로 변환
    """
    tree = ast.parse(source, filename=filename)
    imported: set[str] = set()
    pkg_parts = [p for p in current_pkg.split(".") if p]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    imported.add(node.module)
                    for alias in node.names:
                        if alias.name and alias.name != "*":
                            imported.add(f"{node.module}.{alias.name}")
            else:
                up = node.level - 1
                if up <= len(pkg_parts):
                    base_parts = pkg_parts[: len(pkg_parts) - up]
                    mod_parts = [p for p in node.module.split(".") if p] if node.module else []
                    target_parts = [*base_parts, *mod_parts]
                    target_mod = ".".join(target_parts)
                    if target_mod:
                        imported.add(target_mod)
                        for alias in node.names:
                            if alias.name and alias.name != "*":
                                imported.add(f"{target_mod}.{alias.name}")
                    else:
                        for alias in node.names:
                            if alias.name and alias.name != "*":
                                imported.add(alias.name)
    return imported


def extract_ast_strings(source: str, *, filename: str = "<unknown>") -> set[str]:
    """파이썬 소스 문자열에서 문자열 리터럴(`ast.Constant` str) 집합을 추출한다 (산문 docstring 제외)."""
    tree = ast.parse(source, filename=filename)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
                and (" " in first.value.value or "\n" in first.value.value)
            ):
                docstring_nodes.add(id(first.value))

    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstring_nodes:
            val = node.value.strip()
            if val:
                literals.add(val)
    return literals


_AST_CACHE: dict[tuple[str, int, int], tuple[set[str], set[str]]] = {}
_GRIMP_CACHE: dict[tuple[str, int, int], object] = {}


def _extract_file_imports_and_strings(py_path: Path, ai_dir: Path) -> tuple[set[str], set[str]]:
    try:
        st = py_path.stat()
        cache_key = (str(py_path), st.st_mtime_ns, st.st_size)
    except OSError:
        cache_key = None

    if cache_key is not None and cache_key in _AST_CACHE:
        cached_imps, cached_strs = _AST_CACHE[cache_key]
        return set(cached_imps), set(cached_strs)

    _, current_pkg, _ = _module_context_for_path(py_path, ai_dir)
    source = py_path.read_text(encoding="utf-8")
    fname = str(py_path)
    imps = extract_ast_imports(source, current_pkg=current_pkg, filename=fname)
    strs = extract_ast_strings(source, filename=fname)
    if cache_key is not None:
        _AST_CACHE[cache_key] = (set(imps), set(strs))
    return imps, strs


def _extract_file_imports(py_path: Path, ai_dir: Path) -> set[str]:
    imps, _ = _extract_file_imports_and_strings(py_path, ai_dir)
    return imps


def _tree_signature(ai_dir: Path) -> tuple[str, int, int]:
    count = 0
    max_mtime = 0
    for sub in (ai_dir / "src" / "simai_ai", ai_dir / "services"):
        if not sub.is_dir():
            continue
        for p in sub.rglob("*.py"):
            try:
                st = p.stat()
            except OSError:
                continue
            count += 1
            if st.st_mtime_ns > max_mtime:
                max_mtime = st.st_mtime_ns
    return (str(ai_dir.resolve()), count, max_mtime)


def _build_grimp_graph(ai_dir: Path):
    """`simai_ai`, `services` 루트 패키지에 대한 grimp 의존 그래프를 생성한다."""
    import grimp

    roots: list[str] = []
    if (ai_dir / "src" / "simai_ai" / "__init__.py").is_file():
        roots.append("simai_ai")
    if (ai_dir / "services" / "__init__.py").is_file():
        roots.append("services")
    if not roots:
        raise RuntimeError(f"No grimp roots found under {ai_dir}")

    sig = _tree_signature(ai_dir)
    if sig in _GRIMP_CACHE:
        return _GRIMP_CACHE[sig]

    search_paths: list[str] = []
    src_dir = ai_dir / "src"
    if src_dir.is_dir():
        search_paths.append(str(src_dir))
    search_paths.append(str(ai_dir))

    orig_sys_path = list(sys.path)
    saved_modules: dict[str, object] = {}
    try:
        for p in reversed(search_paths):
            if p in sys.path:
                sys.path.remove(p)
            sys.path.insert(0, p)

        # 이미 로드된 simai_ai / services 모듈의 __spec__ 캐시 때문에 tmp_path 대신 실제 경로가 잡히는 현상을 막는다.
        for mod_name in list(sys.modules):
            if mod_name in {"simai_ai", "services"} or mod_name.startswith(("simai_ai.", "services.")):
                saved_modules[mod_name] = sys.modules.pop(mod_name)

        importlib.invalidate_caches()
        graph = grimp.build_graph(*roots)
        _GRIMP_CACHE[sig] = graph
        return graph
    finally:
        sys.path[:] = orig_sys_path
        sys.modules.update(saved_modules)
        importlib.invalidate_caches()


def _is_test_file(path: Path) -> bool:
    name = path.name
    return (name.startswith("test_") and name.endswith(".py")) or name.endswith("_test.py")


def _matches_affected(
    imports: set[str],
    affected_modules: set[str],
    affected_pkg_prefixes: set[str],
) -> bool:
    if imports & affected_modules:
        return True
    for pkg in affected_pkg_prefixes:
        prefix = f"{pkg}."
        if any(imp == pkg or imp.startswith(prefix) for imp in imports):
            return True
    return False


def _resolve_helper_refs(
    imports: set[str],
    strings: set[str],
    helper_imports_by_mod: dict[str, set[str]],
    helper_mods_by_stem: dict[str, set[str]],
) -> set[str]:
    """import 집합 및 문자열 리터럴 집합에서 참조하는 `tests/**` 보조 모듈들을 모두 반환한다."""
    refs: set[str] = set()
    for imp in imports:
        if imp in helper_imports_by_mod:
            refs.add(imp)
        head = imp.split(".")[0]
        if head in helper_mods_by_stem:
            refs |= helper_mods_by_stem[head]

    for s in strings:
        s_norm = s.replace("\\", "/").strip()
        if not s_norm or " " in s_norm or "\n" in s_norm:
            continue
        if s_norm in helper_mods_by_stem:
            refs |= helper_mods_by_stem[s_norm]
        elif s_norm.endswith(".py"):
            stem = Path(s_norm).stem
            if stem in helper_mods_by_stem:
                refs |= helper_mods_by_stem[stem]
    return refs


def _contains_token(haystack: str, needle: str, *, allow_slash_before: bool = False) -> bool:
    """`haystack` 안에 `needle` 이 독립 토큰(또는 경로 말단)으로 등장하는지 확인한다."""
    if not needle or needle not in haystack:
        return False
    if haystack == needle:
        return True
    start = 0
    n_len = len(needle)
    h_len = len(haystack)
    while True:
        idx = haystack.find(needle, start)
        if idx == -1:
            return False
        before_ok = idx == 0 or (
            (allow_slash_before and haystack[idx - 1] == "/")
            or not (haystack[idx - 1].isalnum() or haystack[idx - 1] in "_.")
        )
        end = idx + n_len
        after_ok = end == h_len or not (haystack[end].isalnum() or haystack[end] in "_.")
        if before_ok and after_ok:
            return True
        start = idx + 1


def _matches_string_literals(
    strings: set[str],
    changed_stems: set[str],
    changed_filenames: set[str],
    changed_rel_paths: set[str],
    dotted_mod_needles: set[str],
) -> bool:
    """시험·보조 모듈·conftest 의 문자열 리터럴이 바뀐 파일/모듈 후보와 매칭되는지 판정한다."""
    if not strings or not (changed_stems or changed_filenames or changed_rel_paths or dotted_mod_needles):
        return False

    for raw in strings:
        s_norm = raw.replace("\\", "/").strip()
        if not s_norm:
            continue
        if s_norm in changed_stems or s_norm in changed_filenames:
            return True
        for stem in changed_stems:
            if s_norm.endswith(f"/{stem}"):
                return True
        for fn in changed_filenames:
            if _contains_token(s_norm, fn, allow_slash_before=True):
                return True
        for rel_p in changed_rel_paths:
            if rel_p and rel_p in s_norm:
                return True
        for dmod in dotted_mod_needles:
            if _contains_token(s_norm, dmod, allow_slash_before=False):
                return True
    return False


def _nearest_tests_in_or_above(start_dir: Path, tests_root: Path, ai_dir: Path) -> set[str]:
    """`start_dir` 에 시험 파일이 있으면 그 시험들을, 없으면 상위 디렉터리(최대 `tests_root`)로 올라가며 가장 가까운 시험들을 반환한다."""
    cur = start_dir
    while True:
        if cur.is_dir():
            found = {
                p.relative_to(ai_dir).as_posix()
                for p in cur.glob("*.py")
                if p.is_file() and _is_test_file(p)
            }
            if found:
                return found
        if cur == tests_root or tests_root not in cur.parents:
            break
        cur = cur.parent
    return set()


def _expand_rule_target(target_rel: str, ai_dir: Path, *, is_external_rule: bool) -> set[str]:
    """`path_rules` 대상 경로를 `selected` 항목들로 변환한다."""
    out: set[str] = set()
    full = ai_dir / target_rel
    if full.is_file():
        out.add(target_rel)
    elif full.is_dir():
        found = {
            p.relative_to(ai_dir).as_posix()
            for p in full.rglob("*.py")
            if p.is_file() and _is_test_file(p)
        }
        out |= found
        if is_external_rule or not found:
            out.add(target_rel)
    elif is_external_rule:
        out.add(target_rel)
    return out


_GENERIC_FILENAMES = {"dockerfile", "makefile", "readme.md", "readme", "cmakelists.txt"}


def select_tests(
    changed_files: Iterable[str] | None = None,
    *,
    ai_dir: Path = AI_DIR,
    config_path: Path | None = None,
    base: str = "origin/dev",
) -> list[str]:
    """변경된 파일 목록으로부터 실행할 시험 경로 목록(또는 `['ALL']`, `[]`)을 반환한다."""
    try:
        if changed_files is None:
            entries = changed_files_from_git(base=base, repo_root=ai_dir.parent)
        else:
            entries = _parse_changed_files(changed_files)

        if not entries:
            return []

        always_include, path_rules = _load_config(ai_dir, config_path)

        for repo_path in entries:
            if _is_all_trigger(repo_path, ai_dir, config_path):
                print(f"[select_tests] ALL: trigger file {repo_path}", file=sys.stderr)
                return ["ALL"]

        selected: set[str] = set()
        rule_matched_entries: list[str] = []

        for repo_path in entries:
            is_ext = not repo_path.startswith("ai/")
            for pattern, targets in path_rules.items():
                matched_targets = _match_path_rule(repo_path, pattern, targets)
                if matched_targets:
                    if is_ext and repo_path not in rule_matched_entries:
                        rule_matched_entries.append(repo_path)
                    for target_rel in matched_targets:
                        selected |= _expand_rule_target(target_rel, ai_dir, is_external_rule=is_ext)

        ai_entries = [p[3:] for p in entries if p.startswith("ai/") and len(p) > 3]
        if not ai_entries and not rule_matched_entries and not selected:
            return []

        seed_modules: set[str] = set()
        seed_init_packages: set[str] = set()
        seed_exp_sim_modules: set[str] = set()
        seed_exp_sim_packages: set[str] = set()
        affected_test_helpers: set[str] = set()

        changed_stems: set[str] = set()
        changed_filenames: set[str] = set()
        changed_rel_paths: set[str] = set()
        direct_dotted_mods: set[str] = set()

        tests_root = ai_dir / "tests"

        for ext_path in rule_matched_entries:
            ext_obj = Path(ext_path)
            if ext_obj.name.lower() not in _GENERIC_FILENAMES:
                if ext_obj.stem and ext_obj.stem != "__init__" and len(ext_obj.stem) >= 3:
                    changed_stems.add(ext_obj.stem)
                if "." in ext_obj.name and len(ext_obj.name) >= 5:
                    changed_filenames.add(ext_obj.name)
            changed_rel_paths.add(ext_path)

        for rel in ai_entries:
            rel_path = Path(rel)
            parts = rel_path.parts
            if not parts:
                continue

            top = parts[0]
            if top != "tests":
                if rel_path.name.lower() not in _GENERIC_FILENAMES:
                    if rel_path.stem and rel_path.stem != "__init__" and len(rel_path.stem) >= 3:
                        changed_stems.add(rel_path.stem)
                    if "." in rel_path.name and len(rel_path.name) >= 5:
                        changed_filenames.add(rel_path.name)
                if len(parts) >= 2:
                    changed_rel_paths.add(rel)
                    changed_rel_paths.add(f"ai/{rel}")

            if top == "src" and len(parts) >= 2 and parts[1] == "simai_ai":
                if rel.endswith(".py"):
                    mod, _, is_init = _py_rel_to_module(parts[1:])
                    if mod:
                        seed_modules.add(mod)
                        if "." in mod:
                            direct_dotted_mods.add(mod)
                        if is_init:
                            seed_init_packages.add(mod)
                else:
                    pkg_parts = parts[1:-1]
                    if pkg_parts:
                        pkg = ".".join(pkg_parts)
                        seed_modules.add(pkg)
                        seed_init_packages.add(pkg)

            elif top == "services":
                if rel.endswith(".py"):
                    mod, _, is_init = _py_rel_to_module(parts)
                    if mod:
                        seed_modules.add(mod)
                        if "." in mod:
                            direct_dotted_mods.add(mod)
                        if is_init:
                            seed_init_packages.add(mod)
                else:
                    pkg_parts = parts[:-1]
                    if pkg_parts:
                        pkg = ".".join(pkg_parts)
                        seed_modules.add(pkg)
                        seed_init_packages.add(pkg)

            elif top in {"experiments", "sim"}:
                if rel.endswith(".py"):
                    mod, _, is_init = _py_rel_to_module(parts)
                    if mod:
                        seed_exp_sim_modules.add(mod)
                        if "." in mod:
                            direct_dotted_mods.add(mod)
                        if is_init:
                            seed_exp_sim_packages.add(mod)
                else:
                    pkg_parts = parts[:-1]
                    if pkg_parts:
                        pkg = ".".join(pkg_parts)
                        seed_exp_sim_modules.add(pkg)
                        seed_exp_sim_packages.add(pkg)

            elif top == "tests":
                full_test_path = ai_dir / rel
                if _is_test_file(rel_path):
                    if full_test_path.is_file():
                        selected.add(rel_path.as_posix())
                    t_mod, _, _ = _py_rel_to_module(parts)
                    if t_mod:
                        affected_test_helpers.add(t_mod)
                    if rel_path.stem:
                        affected_test_helpers.add(rel_path.stem)
                else:
                    selected |= _nearest_tests_in_or_above(full_test_path.parent, tests_root, ai_dir)
                    if rel.endswith(".py") and rel_path.name != "__init__.py":
                        helper_mod, _, _ = _py_rel_to_module(parts)
                        if helper_mod:
                            affected_test_helpers.add(helper_mod)
                        if rel_path.stem:
                            changed_stems.add(rel_path.stem)
                        if len(rel_path.name) >= 5:
                            changed_filenames.add(rel_path.name)

        need_module_analysis = bool(
            seed_modules
            or seed_init_packages
            or seed_exp_sim_modules
            or seed_exp_sim_packages
            or affected_test_helpers
            or changed_stems
            or changed_filenames
            or changed_rel_paths
            or direct_dotted_mods
        )

        if need_module_analysis:
            affected_modules: set[str] = set(seed_modules) | set(seed_exp_sim_modules)
            affected_pkg_prefixes: set[str] = set(seed_init_packages) | set(seed_exp_sim_packages)

            if seed_modules or seed_init_packages:
                graph = _build_grimp_graph(ai_dir)
                graph_modules: set[str] = set(graph.modules)

                # 삭제·이름 변경 등으로 그래프에 없는 모듈을 여전히 import 하는 src/services 모듈도 AST 로 보완한다.
                missing_seeds = {m for m in seed_modules if m not in graph_modules}
                if missing_seeds or seed_init_packages:
                    for root_dir in (ai_dir / "src" / "simai_ai", ai_dir / "services"):
                        if not root_dir.is_dir():
                            continue
                        for py_file in root_dir.rglob("*.py"):
                            mod_name, _, _ = _module_context_for_path(py_file, ai_dir)
                            if not mod_name:
                                continue
                            imps = _extract_file_imports(py_file, ai_dir)
                            if _matches_affected(imps, missing_seeds, seed_init_packages):
                                seed_modules.add(mod_name)

                for mod in list(seed_modules):
                    affected_modules.add(mod)
                    if mod in graph_modules:
                        affected_modules |= graph.find_downstream_modules(mod, as_package=False)

                for pkg in list(seed_init_packages):
                    affected_modules.add(pkg)
                    affected_pkg_prefixes.add(pkg)
                    prefix = f"{pkg}."
                    for m in graph_modules:
                        if m == pkg or m.startswith(prefix):
                            affected_modules.add(m)
                            affected_modules |= graph.find_downstream_modules(m, as_package=False)

                # 영향을 받은 모듈 중 패키지(__init__.py)가 있으면 그 패키지의 하위 모듈을 import 하는 시험도 선택한다.
                for mod in list(affected_modules):
                    if _is_package_module(mod, ai_dir):
                        affected_pkg_prefixes.add(mod)

            # experiments/ 및 sim/ 모듈들의 AST import 분석 (내부 전파 및 src/services 변경 전파)
            exp_sim_imports: dict[str, set[str]] = {}
            exp_sim_is_pkg: set[str] = set()
            for folder in ("experiments", "sim"):
                folder_path = ai_dir / folder
                if not folder_path.is_dir():
                    continue
                for py_file in folder_path.rglob("*.py"):
                    mod_name, current_pkg, is_init = _module_context_for_path(py_file, ai_dir)
                    if not mod_name:
                        continue
                    if is_init:
                        exp_sim_is_pkg.add(mod_name)
                    imps = _extract_file_imports(py_file, ai_dir)
                    if current_pkg:
                        for sib in py_file.parent.glob("*.py"):
                            if sib.stem != "__init__" and sib.stem in imps:
                                imps.add(f"{current_pkg}.{sib.stem}")
                    exp_sim_imports[mod_name] = imps

            changed = True
            while changed:
                changed = False
                for mod_name, imps in exp_sim_imports.items():
                    if mod_name in affected_modules:
                        continue
                    if _matches_affected(imps, affected_modules, affected_pkg_prefixes):
                        affected_modules.add(mod_name)
                        if mod_name in exp_sim_is_pkg:
                            affected_pkg_prefixes.add(mod_name)
                        changed = True

            dotted_mod_needles: set[str] = set(direct_dotted_mods)
            for mod_name in affected_modules | affected_pkg_prefixes:
                if "." in mod_name and mod_name.startswith(("sim.", "experiments.", "services.")):
                    dotted_mod_needles.add(mod_name)

            # experiments/<subpkg>/<stem>.py 가 영향 받으면 tests/experiments/<subpkg>/ 에서 bare `<stem>` import 도 매칭
            bare_exp_by_test_dir: dict[str, set[str]] = {}
            for mod_name in affected_modules:
                mod_parts = mod_name.split(".")
                if len(mod_parts) == 3 and mod_parts[0] == "experiments":
                    _, subpkg, stem = mod_parts
                    test_sub = f"tests/experiments/{subpkg}"
                    bare_exp_by_test_dir.setdefault(test_sub, set()).add(stem)

            # tests/ 내 보조 모듈(.py) 및 conftest.py 의 AST import·문자열 리터럴 수집
            if tests_root.is_dir():
                conftest_imports_by_dir: dict[Path, set[str]] = {}
                conftest_strings_by_dir: dict[Path, set[str]] = {}
                helper_imports_by_mod: dict[str, set[str]] = {}
                helper_strings_by_mod: dict[str, set[str]] = {}
                helper_mods_by_stem: dict[str, set[str]] = {}
                test_files: list[Path] = []

                for h_mod in affected_test_helpers:
                    if h_mod.startswith("tests."):
                        h_stem = h_mod.rsplit(".", 1)[-1]
                        helper_mods_by_stem.setdefault(h_stem, set()).add(h_mod)
                        helper_imports_by_mod.setdefault(h_mod, set())
                        helper_strings_by_mod.setdefault(h_mod, set())

                for py_file in tests_root.rglob("*.py"):
                    if py_file.name == "conftest.py":
                        imps, strs = _extract_file_imports_and_strings(py_file, ai_dir)
                        conftest_imports_by_dir[py_file.parent] = imps
                        conftest_strings_by_dir[py_file.parent] = strs
                    elif _is_test_file(py_file):
                        test_files.append(py_file)
                    else:
                        h_mod, _, is_init = _module_context_for_path(py_file, ai_dir)
                        if h_mod:
                            imps, strs = _extract_file_imports_and_strings(py_file, ai_dir)
                            helper_imports_by_mod[h_mod] = imps
                            helper_strings_by_mod[h_mod] = strs
                            if not is_init:
                                helper_mods_by_stem.setdefault(py_file.stem, set()).add(h_mod)

                # 보조 모듈의 bare import / 문자열 참조를 디렉터리와 무관하게 보조 모듈 색인(stem)으로 연결
                for h_mod in list(helper_imports_by_mod):
                    refs = _resolve_helper_refs(
                        helper_imports_by_mod[h_mod],
                        helper_strings_by_mod.get(h_mod, set()),
                        helper_imports_by_mod,
                        helper_mods_by_stem,
                    )
                    helper_imports_by_mod[h_mod] |= refs

                # 보조 모듈 간 전파 (전이 닫힘)
                h_changed = True
                while h_changed:
                    h_changed = False
                    for h_mod, imps in list(helper_imports_by_mod.items()):
                        expanded_imps = set(imps)
                        expanded_strs = set(helper_strings_by_mod.get(h_mod, set()))
                        for imp in list(imps):
                            if imp in helper_imports_by_mod:
                                expanded_imps |= helper_imports_by_mod[imp]
                                expanded_strs |= helper_strings_by_mod.get(imp, set())
                            head = imp.split(".")[0]
                            if head in helper_mods_by_stem:
                                for target_h in helper_mods_by_stem[head]:
                                    expanded_imps.add(target_h)
                                    expanded_imps |= helper_imports_by_mod.get(target_h, set())
                                    expanded_strs |= helper_strings_by_mod.get(target_h, set())
                        if len(expanded_imps) > len(imps) or len(expanded_strs) > len(
                            helper_strings_by_mod.get(h_mod, set())
                        ):
                            helper_imports_by_mod[h_mod] = expanded_imps
                            helper_strings_by_mod[h_mod] = expanded_strs
                            h_changed = True

                for t_file in test_files:
                    rel_posix = t_file.relative_to(ai_dir).as_posix()
                    if rel_posix in selected or rel_posix == "tests/test_select_tests.py":
                        continue

                    eff_imports, eff_strings = _extract_file_imports_and_strings(t_file, ai_dir)

                    # 상위 conftest.py (루트 tests/conftest.py 포함)의 import 및 문자열 리터럴 합산
                    cur_dir = t_file.parent
                    while True:
                        if cur_dir in conftest_imports_by_dir:
                            eff_imports |= conftest_imports_by_dir[cur_dir]
                        if cur_dir in conftest_strings_by_dir:
                            eff_strings |= conftest_strings_by_dir[cur_dir]
                        if cur_dir == tests_root or tests_root not in cur_dir.parents:
                            break
                        cur_dir = cur_dir.parent

                    # 디렉터리와 무관하게 stem 색인 또는 tests.* 경로로 참조한 시험 보조 모듈 합산 (전이 포함)
                    resolved_helpers = _resolve_helper_refs(
                        eff_imports,
                        eff_strings,
                        helper_imports_by_mod,
                        helper_mods_by_stem,
                    )
                    for h_mod in resolved_helpers:
                        eff_imports.add(h_mod)
                        eff_imports |= helper_imports_by_mod.get(h_mod, set())
                        eff_strings |= helper_strings_by_mod.get(h_mod, set())

                    if eff_imports & affected_test_helpers:
                        selected.add(rel_posix)
                        continue

                    if _matches_affected(eff_imports, affected_modules, affected_pkg_prefixes):
                        selected.add(rel_posix)
                        continue

                    matched_exp = False
                    for test_prefix, bare_stems in bare_exp_by_test_dir.items():
                        if rel_posix.startswith(f"{test_prefix}/") and (eff_imports & bare_stems):
                            selected.add(rel_posix)
                            matched_exp = True
                            break
                    if matched_exp:
                        continue

                    if _matches_string_literals(
                        eff_strings,
                        changed_stems,
                        changed_filenames,
                        changed_rel_paths,
                        dotted_mod_needles,
                    ):
                        selected.add(rel_posix)
                        continue

        for item in always_include:
            selected.add(item)

        return sorted(selected)
    except Exception as exc:
        print(f"[select_tests] ALL: fallback on {type(exc).__name__}: {exc}", file=sys.stderr)
        return ["ALL"]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )


def changed_files_in_commit(commit: str, *, repo_root: Path = REPO_ROOT) -> list[str]:
    """단일 커밋(머지 커밋 포함: 첫 번째 부모 기준)에서 바뀐 파일 경로 목록을 반환한다."""
    _run_git(["cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo_root)
    parent_chk = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    has_parent = (
        subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{commit}^^{{commit}}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).returncode
        == 0
    )
    if parent_chk.returncode != 0 or not has_parent:
        raise RuntimeError(f"Commit {commit} or its parent {commit}^ is unavailable (shallow clone)")

    res = _run_git(
        ["diff-tree", "-m", "--first-parent", "--no-commit-id", "--name-status", "-r", "-M", "-z", commit],
        cwd=repo_root,
    )
    return _parse_changed_files([res.stdout])


def changed_files_from_git(base: str = "origin/dev", *, repo_root: Path = REPO_ROOT) -> list[str]:
    """`<base>...HEAD` 변경분과 현재 작업 트리(staged, unstaged, untracked) 변경분을 합쳐 반환한다."""
    chunks: list[str] = []

    resolved_base = base
    chk = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", resolved_base],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if chk.returncode != 0 and resolved_base == "origin/dev":
        chk_dev = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "dev"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        if chk_dev.returncode == 0:
            resolved_base = "dev"

    diff_branch = _run_git(["diff", "--name-status", "-M", "-z", f"{resolved_base}...HEAD"], cwd=repo_root)
    chunks.append(diff_branch.stdout)

    diff_staged = _run_git(["diff", "--name-status", "-M", "-z", "--cached"], cwd=repo_root)
    chunks.append(diff_staged.stdout)

    diff_worktree = _run_git(["diff", "--name-status", "-M", "-z"], cwd=repo_root)
    chunks.append(diff_worktree.stdout)

    untracked = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo_root)
    chunks.extend(p for p in untracked.stdout.split("\0") if p)

    return _parse_changed_files(chunks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="영향 받는 ai pytest 시험 경로만 선택한다.")
    parser.add_argument("--base", default="origin/dev", help="비교 기준 브랜치/커밋 (기본: origin/dev)")
    parser.add_argument("--commit", default=None, help="특정 커밋의 변경 파일만 분석")
    parser.add_argument("files", nargs="*", help="직접 전달할 변경 파일 목록 (생략 시 git diff 사용)")
    try:
        args = parser.parse_args(argv)
        if args.files:
            result = select_tests(args.files)
        elif args.commit:
            files = changed_files_in_commit(args.commit)
            result = select_tests(files)
        else:
            files = changed_files_from_git(base=args.base)
            result = select_tests(files)
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        print(f"[select_tests] ALL: invalid CLI arguments (exit {exc.code})", file=sys.stderr)
        result = ["ALL"]
    except Exception as exc:
        print(f"[select_tests] ALL: fallback on {type(exc).__name__}: {exc}", file=sys.stderr)
        result = ["ALL"]

    if result:
        print("\n".join(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
