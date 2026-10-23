"""의존 방향 검사(pyproject.toml 의 [tool.importlinter])가 위반을 실제로 잡는지 확인한다.

tmp_path 에 실제 패키지 구조를 본뜬 가짜 트리를 만들고, 위반 import 를 넣은 뒤
실제 설정 그대로 lint-imports 를 돌려 종료 코드를 본다.
"""

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[2]
PYPROJECT = AI_DIR / "pyproject.toml"
SRC_PACKAGES = sorted(p.parent.name for p in (AI_DIR / "src" / "simai_ai").glob("*/__init__.py"))
SERVICES = sorted(p.parent.name for p in (AI_DIR / "services").glob("*/__init__.py"))


def _lint_imports(cwd: Path, pythonpath: list[Path] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, PYTHONUTF8="1")
    if pythonpath is not None:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in pythonpath)
    return subprocess.run(
        [sys.executable, "-c", "from importlinter.cli import lint_imports_command; lint_imports_command()", "--no-cache"],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _fake_tree(root: Path, extra: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """실제와 같은 패키지 목록과 실제 pyproject.toml 로 가짜 트리를 만들고 검사한다."""
    shutil.copy(PYPROJECT, root / "pyproject.toml")
    files = {"src/simai_ai/__init__.py": "", "services/__init__.py": ""}
    files |= {f"src/simai_ai/{name}/__init__.py": "" for name in SRC_PACKAGES}
    files |= {f"services/{name}/__init__.py": "" for name in SERVICES}
    files |= {"experiments/__init__.py": "", "experiments/hmr/__init__.py": "", "experiments/hmr/bench.py": ""}
    files |= {"src/simai_ai/risk/plan.py": ""}  # 계약 risk-engine-no-channels 의 source 모듈(없으면 lint-imports 오류)
    files |= extra
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return _lint_imports(root, pythonpath=[root / "src", root])


def test_real_tree_passes():
    result = _lint_imports(AI_DIR)
    assert result.returncode == 0, result.stdout + result.stderr


def test_fake_tree_without_violation_passes(tmp_path):
    result = _fake_tree(tmp_path, {"src/simai_ai/risk/engine.py": "from simai_ai import contracts\n"})
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("rel", "code"),
    [
        pytest.param("src/simai_ai/risk/engine.py", "import simai_ai.detection\n", id="risk-imports-detection"),
        pytest.param("src/simai_ai/risk/engine.py", "from simai_ai import geometry\n", id="risk-from-imports-geometry"),
        pytest.param("src/simai_ai/risk/engine.py", "import numpy\n", id="risk-imports-numpy"),
        pytest.param("src/simai_ai/scene/x.py", "import experiments.hmr.bench\n", id="src-imports-experiments"),
        pytest.param("services/tier0/app.py", "from experiments.hmr import bench\n", id="services-imports-experiments"),
        pytest.param("src/simai_ai/io/x.py", "import services.tier0\n", id="src-imports-services"),
        pytest.param("src/simai_ai/calibration/x.py", "from simai_recorder import config\n", id="src-imports-gateway"),
        pytest.param("services/tier0/app.py", "import simai_relay.config\n", id="services-imports-gateway"),
    ],
)
def test_violation_fails(tmp_path, rel, code):
    result = _fake_tree(tmp_path, {rel: code})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout, result.stdout + result.stderr


def test_risk_cannot_reach_pydantic_through_contract_models(tmp_path):
    """생성 모델(pydantic)은 기본 설치에 없으므로 risk 가 contracts.models 를 거쳐 불러오면 실패한다(S15P31A101-24)."""
    result = _fake_tree(
        tmp_path,
        {
            "src/simai_ai/contracts/models.py": "import pydantic\n",
            "src/simai_ai/risk/engine.py": "from simai_ai.contracts import models\n",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout, result.stdout + result.stderr


def test_risk_contract_covers_every_feature_package():
    """새 기능 패키지를 만들고 risk 금지 목록에 넣지 않으면 실패한다."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["importlinter"]
    contract = next(c for c in config["contracts"] if c["id"] == "risk-isolated")
    expected = {f"simai_ai.{name}" for name in SRC_PACKAGES} - {"simai_ai.risk", "simai_ai.contracts"}
    assert {m for m in contract["forbidden_modules"] if m.startswith("simai_ai.")} == expected


def test_simai_ai_has_only_subpackages():
    """simai_ai 바로 아래에는 하위 패키지만 둔다. 최상위 모듈이나 __init__.py 없는 디렉터리는
    risk 금지 목록 비교에서 빠지므로 막는다."""
    root = AI_DIR / "src" / "simai_ai"
    strays = [
        p.name
        for p in root.iterdir()
        if p.name not in {"__init__.py", "__pycache__"} and not (p.is_dir() and (p / "__init__.py").is_file())
    ]
    assert strays == []


@pytest.mark.parametrize(
    ("files"),
    [
        pytest.param({"services/tier0/app.py": "import vllm\n"}, id="tier0-imports-vllm"),
        pytest.param({"src/simai_ai/detection/x.py": "from transformers import AutoModel\n"}, id="detection-imports-transformers"),
        pytest.param({"src/simai_ai/tracking/x.py": "import detectron2.config\n"}, id="tracking-imports-detectron2"),
        pytest.param({"src/simai_ai/calibration/x.py": "from moge.model.v2 import MoGeModel\n"}, id="calibration-imports-moge"),
        pytest.param({"src/simai_ai/risk/x.py": "import pyrender\n"}, id="risk-imports-pyrender"),
        # 워커 패키지를 거쳐 간접으로 불러와도 막는다(같은 가상환경이라 import 자체는 성공한다)
        pytest.param(
            {"src/simai_ai/vlm/server.py": "import vllm_cosmos3\n", "src/simai_ai/io/x.py": "from simai_ai.vlm import server\n"},
            id="io-reaches-vllm-cosmos3-through-vlm",
        ),
        # 우리 워커 패키지 자체도 막는다(지금은 비어 있어도 나중에 vLLM·HMR 을 불러오게 된다)
        pytest.param({"services/tier0/app.py": "from simai_ai import vlm\n"}, id="tier0-imports-simai-vlm"),
        pytest.param({"src/simai_ai/tracking/x.py": "import chumpy\n"}, id="tracking-imports-chumpy"),
        # MapAnything 장면 준비 스택(S15P31A101-250): 같은 가상환경에 있어도 Tier 0 경로에서 막는다
        pytest.param({"services/tier0/app.py": "from mapanything.models import MapAnything\n"}, id="tier0-imports-mapanything"),
        pytest.param({"src/simai_ai/calibration/x.py": "import uniception.models\n"}, id="calibration-imports-uniception"),
        pytest.param({"src/simai_ai/geometry/x.py": "import rerun as rr\n"}, id="geometry-imports-rerun"),
        pytest.param(
            {"src/simai_ai/scene/static.py": "import mapanything\n", "src/simai_ai/calibration/x.py": "from simai_ai.scene import static\n"},
            id="calibration-reaches-mapanything-through-scene",
        ),
    ],
)
def test_tier0_cannot_import_worker_stack(tmp_path, files):
    """Tier 0 경로는 한 가상환경에 같이 깔린 VLM·HMR 스택을 import 하지 못한다(S15P31A101-235)."""
    result = _fake_tree(tmp_path, files)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout, result.stdout + result.stderr


def test_worker_package_may_import_worker_stack(tmp_path):
    """분석 워커 쪽(simai_ai.vlm·hmr)은 vLLM·HMR 스택을 써도 된다(Tier 0 금지가 워커까지 막지 않는다)."""
    result = _fake_tree(tmp_path, {"src/simai_ai/vlm/server.py": "import vllm\n", "src/simai_ai/hmr/mesh.py": "import pyrender\n"})
    assert result.returncode == 0, result.stdout + result.stderr


def _simai_subpackages_imported(text: str) -> set[str]:
    """`import simai_ai.x`·`from simai_ai.x import y`·`from simai_ai import x, y as z`·괄호 여러 줄 형태에서 하위 패키지 이름."""
    used = {f"simai_ai.{m}" for m in re.findall(r"^\s*(?:from|import)\s+simai_ai\.(\w+)", text, re.M)}
    for names in re.findall(r"^\s*from\s+simai_ai\s+import\s+(\([^)]*\)|[^\n]+)", text, re.M):
        used |= {f"simai_ai.{n.split()[0]}" for n in names.strip("()").split(",") if n.strip()}
    return used


def test_simai_import_scan_reads_every_form():
    text = "import simai_ai.io\nfrom simai_ai.tracking import x\nfrom simai_ai import geometry, contact as c\nfrom simai_ai import (\n    risk,\n    gpu,\n)\n"
    assert _simai_subpackages_imported(text) == {f"simai_ai.{n}" for n in ("io", "tracking", "geometry", "contact", "risk", "gpu")}


def test_tier0_contract_covers_every_package_tier0_imports():
    """services.tier0 가 import 하는 simai_ai 하위 패키지는 모두 tier0-no-worker-stack 의 source 에 있어야 한다
    (Tier 0 가 새 패키지를 쓰기 시작했는데 목록에 없으면 그 패키지를 거친 워커 스택 import 를 못 잡는다)."""
    config = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["importlinter"]
    contract = next(c for c in config["contracts"] if c["id"] == "tier0-no-worker-stack")
    used = set()
    for path in (AI_DIR / "services" / "tier0").rglob("*.py"):
        used |= _simai_subpackages_imported(path.read_text(encoding="utf-8"))
    assert used, "services/tier0 에서 simai_ai import 를 하나도 찾지 못했다(검사 정규식 확인)"
    assert used <= set(contract["source_modules"]), sorted(used - set(contract["source_modules"]))


@pytest.mark.parametrize(
    ("rel", "code"),
    [
        pytest.param("services/risk_engine/app.py", "import simai_ai.risk.alerting\n", id="api-imports-alerting"),
        pytest.param("services/risk_engine/app.py", "from simai_ai.risk import lifecycle\n", id="api-imports-lifecycle"),
        pytest.param("services/risk_engine/app.py", "import simai_ai.eventlog\n", id="api-imports-journal"),
        pytest.param("services/risk_engine/app.py", "from services.tier0 import i1_publisher\n", id="api-imports-tier0"),
        pytest.param("services/risk_engine/app.py", "import websockets\n", id="api-imports-websockets"),
        pytest.param("services/risk_engine/app.py", "import urllib.request\n", id="api-imports-urllib"),
        pytest.param("services/risk_engine/app.py", "import socket\n", id="api-imports-socket"),
        pytest.param("src/simai_ai/risk/plan.py", "import sqlite3\n", id="plan-imports-sqlite3"),
        pytest.param("src/simai_ai/risk/plan.py", "import simai_ai.risk.site_alerts\n", id="plan-imports-site-alerts"),
    ],
)
def test_risk_engine_container_cannot_reach_alert_channels(tmp_path, rel, code):
    """Risk Engine 컨테이너(PLANNED)는 알림·생명주기·저널·Tier 0 전송·네트워크 클라이언트를 import 하지 못한다(S15P31A101-99, 규칙 5)."""
    stubs = {
        "src/simai_ai/risk/alerting.py": "",
        "src/simai_ai/risk/lifecycle.py": "",
        "src/simai_ai/risk/site_alerts.py": "",
        "services/tier0/i1_publisher.py": "",
    }
    result = _fake_tree(tmp_path, stubs | {rel: code})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "BROKEN" in result.stdout, result.stdout + result.stderr
