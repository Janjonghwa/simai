"""risk 패키지의 격리 규칙을 기본 설치 환경(import-linter 없음)에서도 확인한다.

- 현재 시각을 읽어도 되는 곳은 clock.py 의 SystemClock.now_ms 하나다(ai/CONVENTION.md 코드 절, 명세 21.3).
  clock.py 파일 전체를 빼지 않는다. FixedClock 이나 모듈 수준에서 시각을 읽어도 잡는다.
- 표준 라이브러리와 simai_ai.risk·simai_ai.contracts 만 import 한다(lint-imports 계약 risk-isolated 와 같은 취지).

시각 읽기 검사는 AST 로 한다. import 만 보지 않고 이름·속성·문자열 상수를 함께 봐서 아래 우회를 잡는다.
- 재노출: `from simai_ai.risk.clock import time`, `from simai_ai.risk import clock` 뒤 `clock.time.time()`
- 동적 import: `__import__("datetime")`, `importlib.import_module("time")`, `sys.modules["time"]`, `getattr(m, "time")`
검사가 실제로 우회를 잡는지는 아래 음성 대조 시험이 실제 소스에 우회 코드를 넣어 확인한다.
"""

import ast
import sys
from pathlib import Path

import pytest

RISK_DIR = Path(__file__).resolve().parents[2] / "src" / "simai_ai" / "risk"

# 시각을 읽을 수 있는 표준 모듈과 시각 함수 이름.
TIME_MODULES = {"time", "datetime", "calendar", "zoneinfo"}
TIME_FUNCS = {
    "time_ns",
    "monotonic",
    "monotonic_ns",
    "perf_counter",
    "perf_counter_ns",
    "process_time",
    "process_time_ns",
    "thread_time",
    "thread_time_ns",
    "clock_gettime",
    "clock_gettime_ns",
    "localtime",
    "gmtime",
    "ctime",
    "asctime",
    "now",
    "utcnow",
    "today",
}
# 동적 import·모듈 우회 수단. risk 에서는 어디서도 쓰지 않는다.
DYNAMIC_MODULES = {"importlib", "builtins"}
DYNAMIC_NAMES = {"__import__", "import_module", "modules", "__builtins__", "eval", "exec", "compile"}
WATCHED_WORDS = TIME_MODULES | TIME_FUNCS | DYNAMIC_NAMES

# 유일한 허용 범위: clock.py 모듈 수준의 `import time` 과 SystemClock.now_ms 안의 `time.time_ns`.
ALLOWED_FILE = "clock.py"
ALLOWED_CLASS = "SystemClock"
ALLOWED_METHOD = "now_ms"


def _allowed_nodes(tree: ast.Module) -> set[int]:
    for cls in tree.body:
        if isinstance(cls, ast.ClassDef) and cls.name == ALLOWED_CLASS:
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == ALLOWED_METHOD:
                    return {id(n) for n in ast.walk(fn)}
    return set()


def time_read_violations(source: str, filename: str) -> list[str]:
    """소스에서 SystemClock.now_ms 밖의 시각 읽기·동적 import 를 찾는다. 위반 설명 목록을 돌려준다."""
    tree = ast.parse(source)
    allowed = _allowed_nodes(tree) if filename == ALLOWED_FILE else set()
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    out: list[str] = []

    def bad(node: ast.AST, what: str) -> None:
        out.append(f"{filename}:{getattr(node, 'lineno', '?')} {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                module_level_time = (
                    filename == ALLOWED_FILE and node in tree.body and alias.name == "time" and alias.asname is None
                )
                if (top in TIME_MODULES and not module_level_time) or top in DYNAMIC_MODULES:
                    bad(node, f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in TIME_MODULES | DYNAMIC_MODULES:
                bad(node, f"from {node.module} import ...")
            for alias in node.names:
                if alias.name in WATCHED_WORDS:  # 재노출: from simai_ai.risk.clock import time
                    bad(node, f"from {node.module} import {alias.name}")
        elif isinstance(node, ast.Name) and node.id in TIME_MODULES | DYNAMIC_NAMES:
            if not (id(node) in allowed and node.id == "time"):
                bad(node, f"이름 {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in WATCHED_WORDS:
            allowed_call = (
                id(node) in allowed
                and node.attr == "time_ns"
                and isinstance(node.value, ast.Name)
                and node.value.id == "time"
            )
            if not allowed_call:
                bad(node, f"속성 .{node.attr}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if node.value in WATCHED_WORDS:  # getattr(m, "time"), __import__("datetime") 등
                bad(node, f"문자열 {node.value!r}")
    return out


def _sources() -> list[Path]:
    return sorted(RISK_DIR.rglob("*.py"))


def test_risk_sources_exist():
    assert (RISK_DIR / ALLOWED_FILE).is_file()
    assert _allowed_nodes(ast.parse((RISK_DIR / ALLOWED_FILE).read_text(encoding="utf-8")))


def test_only_system_clock_reads_time():
    violations = [v for p in _sources() for v in time_read_violations(p.read_text(encoding="utf-8"), p.name)]
    assert violations == [], "risk 는 현재 시각을 직접 읽지 않는다. Clock 을 주입받는다"


# --- 음성 대조: 실제 소스에 우회 코드를 넣으면 검사가 실패해야 한다 -----------------------

_FIXED_NOW = "    def now_ms(self) -> int:\n        return self._t_ms\n"

BYPASSES = {
    # 독립 리뷰(S15P31A101-68) F2 표의 네 변형
    "engine-import-time": ("engine.py", None, "\nimport time\n_T = time.monotonic()\n"),
    "engine-reexport-from-clock": (
        "engine.py",
        None,
        "\nfrom simai_ai.risk.clock import time\n_T = (time.time(), time.monotonic(), time.perf_counter())\n",
    ),
    "engine-dunder-import": ("engine.py", None, "\n_T = __import__('datetime').date.today()\n"),
    "fixed-clock-reads-monotonic": (
        "clock.py",
        _FIXED_NOW,
        "    def now_ms(self) -> int:\n        time.monotonic()\n        return self._t_ms\n",
    ),
    # 그 밖의 우회
    "engine-clock-module-attr": ("engine.py", None, "\nfrom simai_ai.risk import clock\n_T = clock.time.time()\n"),
    "engine-importlib": ("engine.py", None, "\nimport importlib\n_T = importlib.import_module('time')\n"),
    "engine-sys-modules": ("engine.py", None, "\nimport sys\n_T = sys.modules['time']\n"),
    "engine-getattr": ("engine.py", None, "\nfrom simai_ai.risk import clock\n_T = getattr(clock, 'time')\n"),
    "engine-from-datetime": ("engine.py", None, "\nfrom datetime import date\n_T = date.today()\n"),
    "engine-import-alias": ("engine.py", None, "\nimport time as t\n_T = t.time()\n"),
    "clock-module-level-read": ("clock.py", None, "\n_T = time.time_ns()\n"),
    "system-clock-other-func": (
        "clock.py",
        "        return time.time_ns() // 1_000_000\n",
        "        return int(time.monotonic() * 1000)\n",
    ),
}


@pytest.mark.parametrize("name", sorted(BYPASSES))
def test_bypass_is_detected(name):
    filename, old, new = BYPASSES[name]
    source = (RISK_DIR / filename).read_text(encoding="utf-8")
    if old is None:
        patched = source + new
    else:
        assert source.count(old) == 1, f"{filename} 에서 바꿀 자리를 찾지 못했다(음성 대조를 고친다)"
        patched = source.replace(old, new)
    assert time_read_violations(source, filename) == []
    assert time_read_violations(patched, filename) != []


# --- 의존 범위 -------------------------------------------------------------------------


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.append(node.module)
    return mods


def test_risk_uses_only_stdlib_and_contracts():
    offenders = {}
    for p in _sources():
        for m in _imports(p):
            top = m.split(".")[0]
            if top == "simai_ai":
                ok = m.startswith(("simai_ai.risk", "simai_ai.contracts"))
            else:
                ok = top in sys.stdlib_module_names
            if not ok:
                offenders.setdefault(p.name, []).append(m)
    assert offenders == {}
