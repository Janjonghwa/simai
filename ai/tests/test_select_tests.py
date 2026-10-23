"""영향 받는 ai 시험 선택 스크립트(ai/scripts/select_tests.py) 단위·이력 시험 (S15P31A101-255)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

AI_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = AI_DIR.parent
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from scripts.select_tests import (  # noqa: E402
    changed_files_in_commit,
    extract_ast_imports,
    extract_ast_strings,
    main,
    select_tests,
)


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


@pytest.fixture
def synthetic_ai(tmp_path: Path) -> Path:
    """합성 simai_ai / services / experiments / sim / tests 패키지 트리."""
    _write_files(
        tmp_path,
        {
            "scripts/select_tests.json": json.dumps({"always_include": ["tests/architecture"]}),
            "src/simai_ai/__init__.py": "",
            "src/simai_ai/alpha/__init__.py": "",
            "src/simai_ai/alpha/core.py": "VALUE = 1\n",
            "src/simai_ai/alpha/submod.py": "SUB = 2\n",
            "src/simai_ai/alpha/other.py": "OTHER = 3\n",
            "src/simai_ai/beta/__init__.py": "",
            "src/simai_ai/beta/mid.py": "from simai_ai.alpha import core\n",
            "src/simai_ai/gamma/__init__.py": "",
            "src/simai_ai/gamma/unrelated.py": "X = 0\n",
            "services/__init__.py": "",
            "services/tier0/__init__.py": "",
            "services/tier0/top.py": "import simai_ai.beta.mid\n",
            "sim/__init__.py": "",
            "sim/vec.py": "V = 1\n",
            "sim/body.py": "from sim.vec import V\n",
            "experiments/__init__.py": "",
            "experiments/hmr/__init__.py": "",
            "experiments/hmr/fs3db_common.py": "K = 1\n",
            "tests/architecture/test_import_rules.py": "def test_arch(): pass\n",
            "tests/alpha/test_core.py": "import simai_ai.alpha.core\n",
            "tests/alpha/test_submod.py": "from simai_ai.alpha import submod\n",
            "tests/alpha/test_other.py": "from simai_ai.alpha import other\n",
            "tests/beta/test_mid.py": "from simai_ai.beta.mid import core\n",
            "tests/services/test_top.py": "from services.tier0 import top\n",
            "tests/gamma/helper.py": "import simai_ai.gamma.unrelated\n",
            "tests/gamma/test_unrelated.py": "import helper\n",
            "tests/sim/test_body.py": "import sim.body\n",
            "tests/experiments/hmr/test_hmr.py": "import fs3db_common\n",
        },
    )
    return tmp_path


def test_direct_and_indirect_imports_selected(synthetic_ai: Path):
    """바뀐 모듈을 직접 import 하는 시험과 중계 모듈을 거쳐 간접 import 하는 시험이 모두 선택된다."""
    result = select_tests(["ai/src/simai_ai/alpha/core.py"], ai_dir=synthetic_ai)
    assert result == [
        "tests/alpha/test_core.py",
        "tests/architecture",
        "tests/beta/test_mid.py",
        "tests/services/test_top.py",
    ]


def test_downstream_only_does_not_select_upstream_tests(synthetic_ai: Path):
    """상위 조립 모듈(services.tier0.top)만 바뀌면 하위 라이브러리(alpha, beta) 시험은 고르지 않는다."""
    result = select_tests(["ai/services/tier0/top.py"], ai_dir=synthetic_ai)
    assert result == [
        "tests/architecture",
        "tests/services/test_top.py",
    ]


def test_from_package_import_submodule_distinguishes_siblings(synthetic_ai: Path):
    """`from simai_ai.alpha import submod` 처럼 하위 모듈을 import 한 경우 같은 패키지의 다른 하위 모듈과 구분한다."""
    result = select_tests(["ai/src/simai_ai/alpha/submod.py"], ai_dir=synthetic_ai)
    assert result == [
        "tests/alpha/test_submod.py",
        "tests/architecture",
    ]
    assert "tests/alpha/test_other.py" not in result


def test_package_init_change_selects_all_submodule_importers(synthetic_ai: Path):
    """패키지 `__init__.py` 가 바뀌면 그 패키지의 어느 하위 모듈을 import 하는 시험이든 모두 선택한다."""
    result = select_tests(["ai/src/simai_ai/alpha/__init__.py"], ai_dir=synthetic_ai)
    assert result == [
        "tests/alpha/test_core.py",
        "tests/alpha/test_other.py",
        "tests/alpha/test_submod.py",
        "tests/architecture",
        "tests/beta/test_mid.py",
        "tests/services/test_top.py",
    ]


def test_package_init_implicit_dependency_selects_sibling_importers(synthetic_ai: Path):
    """권장 1: `pkg/__init__.py` 가 `core` 를 import 할 때 `core.py` 가 바뀌면 `pkg.other` 를 import 하는 시험도 선택된다."""
    _write_files(
        synthetic_ai,
        {
            "src/simai_ai/alpha/__init__.py": "from simai_ai.alpha import core\n",
        },
    )
    result = select_tests(["ai/src/simai_ai/alpha/core.py"], ai_dir=synthetic_ai)
    assert "tests/alpha/test_other.py" in result
    assert "tests/alpha/test_submod.py" in result


def test_ast_relative_and_submodule_imports():
    """AST 분석기가 상대 import(`from . import`, `from ..sub import`)와 하위 모듈 import 를 정확히 해석한다."""
    code = """
import simai_ai.io.decoder
from simai_ai.geometry import triangulation, rig as r
from . import local_helper
from .sub import child_mod
from ..common import shared_util
"""
    imports = extract_ast_imports(code, current_pkg="simai_ai.tracking.subpkg")
    assert "simai_ai.io.decoder" in imports
    assert "simai_ai.geometry" in imports
    assert "simai_ai.geometry.triangulation" in imports
    assert "simai_ai.geometry.rig" in imports
    assert "simai_ai.tracking.subpkg.local_helper" in imports
    assert "simai_ai.tracking.subpkg.sub" in imports
    assert "simai_ai.tracking.subpkg.sub.child_mod" in imports
    assert "simai_ai.tracking.common" in imports
    assert "simai_ai.tracking.common.shared_util" in imports


def test_experiments_and_sim_ast_propagation(synthetic_ai: Path):
    """grimp 그래프 밖인 `ai/sim/**` 과 `ai/experiments/**` 변경도 AST 역추적으로 시험을 고른다."""
    sim_res = select_tests(["ai/sim/vec.py"], ai_dir=synthetic_ai)
    assert sim_res == [
        "tests/architecture",
        "tests/sim/test_body.py",
    ]

    exp_res = select_tests(["ai/experiments/hmr/fs3db_common.py"], ai_dir=synthetic_ai)
    assert exp_res == [
        "tests/architecture",
        "tests/experiments/hmr/test_hmr.py",
    ]


def test_changed_test_file_and_test_helper_or_data(synthetic_ai: Path):
    """바뀐 시험 파일은 직접 포함하고, 바뀐 시험 보조/데이터 파일은 같은(또는 가장 가까운 상위) 디렉터리 시험을 포함한다."""
    direct = select_tests(["ai/tests/alpha/test_other.py"], ai_dir=synthetic_ai)
    assert direct == [
        "tests/alpha/test_other.py",
        "tests/architecture",
    ]

    # 보조 파일(helper.py) 변경 -> 같은 디렉터리 시험 포함
    helper_res = select_tests(["ai/tests/gamma/helper.py"], ai_dir=synthetic_ai)
    assert helper_res == [
        "tests/architecture",
        "tests/gamma/test_unrelated.py",
    ]

    # 하위 데이터 파일(vectors/sample.json) 변경 -> 가장 가까운 상위 시험 디렉터리(tests/gamma) 시험 포함
    nested_data_res = select_tests(["ai/tests/gamma/vectors/sample.json"], ai_dir=synthetic_ai)
    assert nested_data_res == [
        "tests/architecture",
        "tests/gamma/test_unrelated.py",
    ]


def test_f1_cross_directory_helper_and_conftest_transitive(synthetic_ai: Path):
    """F1 반례: 다른 디렉터리의 시험 보조 모듈 bare import 전이 및 상위 conftest bare helper 참조가 중첩 시험까지 전파된다."""
    _write_files(
        synthetic_ai,
        {
            "tests/io/recording_helper.py": "import simai_ai.alpha.core\n",
            "tests/scenarios/bridge_helper.py": "from recording_helper import VALUE\n",
            "tests/scenarios/test_golden_like.py": "import bridge_helper\n",
            "tests/area/helper_area.py": "import simai_ai.gamma.unrelated\n",
            "tests/area/conftest.py": "import helper_area\n",
            "tests/area/nested/test_nested.py": "def test_n(): pass\n",
            # 같은 stem 의 보조 모듈이 두 디렉터리에 있을 때 둘 다 보수적으로 연결되는지 확인
            "tests/dup_a/shared_util.py": "import simai_ai.alpha.submod\n",
            "tests/dup_b/shared_util.py": "import simai_ai.alpha.other\n",
            "tests/consumer/test_dup_consumer.py": "import shared_util\n",
        },
    )

    # 1. recording_helper.py 직접 변경 -> bridge_helper 를 거쳐 다른 디렉터리의 test_golden_like.py 가 선택됨
    res_helper = select_tests(["ai/tests/io/recording_helper.py"], ai_dir=synthetic_ai)
    assert "tests/scenarios/test_golden_like.py" in res_helper

    # 2. 상위 conftest(tests/area/conftest.py) -> bare `import helper_area` -> 제품 모듈(gamma.unrelated) 변경 시 하위 nested 시험 선택
    res_nested = select_tests(["ai/src/simai_ai/gamma/unrelated.py"], ai_dir=synthetic_ai)
    assert "tests/area/nested/test_nested.py" in res_nested

    # 3. 같은 stem(shared_util) 보조 모듈이 여럿이면 어느 쪽이 바뀌든 consumer 시험이 선택됨
    assert "tests/consumer/test_dup_consumer.py" in select_tests(["ai/tests/dup_a/shared_util.py"], ai_dir=synthetic_ai)
    assert "tests/consumer/test_dup_consumer.py" in select_tests(["ai/tests/dup_b/shared_util.py"], ai_dir=synthetic_ai)
    assert "tests/consumer/test_dup_consumer.py" in select_tests(
        ["ai/src/simai_ai/alpha/submod.py"], ai_dir=synthetic_ai
    )
    assert "tests/consumer/test_dup_consumer.py" in select_tests(
        ["ai/src/simai_ai/alpha/other.py"], ai_dir=synthetic_ai
    )


def test_f1_real_repo_cross_directory_helpers():
    """F1 실제 저장소 반례 4건+: recording_session.py 및 live_video.py 변경 시 다른 디렉터리 소비 시험들이 모두 선택된다."""
    rec_selected = set(select_tests(["ai/tests/io/recording_session.py"], ai_dir=AI_DIR))
    assert "tests/scenarios/test_scenario_golden.py" in rec_selected
    assert "tests/services/tier0/test_tier0_file_replay.py" in rec_selected

    lv_selected = set(select_tests(["ai/tests/services/tier0/live_video.py"], ai_dir=AI_DIR))
    assert "tests/scenarios/test_scenario_golden.py" in lv_selected
    assert "tests/io/test_pyav_decoder.py" in lv_selected
    assert "tests/calibration/extrinsics/test_extract.py" in lv_selected
    assert "tests/deploy/test_session1_c3.py" in lv_selected


def test_f2_external_path_rules_select_deploy_tests(capsys: pytest.CaptureFixture[str]):
    """F2 반례: ai 밖이라도 `deploy/**` 등 path_rules 에 걸리는 변경은 빈 출력이 아니라 `tests/deploy` 관련 시험을 고른다."""
    for path, expected_test in (
        ("deploy/scripts/simai_bootstrap.py", "tests/deploy/test_simai_bootstrap.py"),
        ("deploy/tier0/Dockerfile", "tests/deploy/test_exp_images.py"),
    ):
        selected = select_tests([path], ai_dir=AI_DIR)
        assert selected, f"{path} 선택 결과가 비어 있음"
        assert "tests/deploy" in selected
        assert expected_test in selected

        rc = main([path])
        out = capsys.readouterr().out.strip().splitlines()
        assert rc == 0
        assert "tests/deploy" in out
        assert expected_test in out


def test_f3_string_literals_and_path_rules_select_all_five_cases():
    """F3 반례: review.md F3 표의 5가지 subprocess·문자열·데이터 경로 사례가 모두 빠짐없이 선택된다."""
    cases = [
        ("ai/sim/run.py", ["tests/sim/test_cli.py"]),
        ("ai/sim/scenario.py", ["tests/sim/test_scenarios.py"]),
        ("ai/sim/scenarios/basic.json", ["tests/sim/test_cli.py", "tests/sim/test_scenarios.py"]),
        (
            "ai/experiments/static_scene/run_mapanything.py",
            ["tests/experiments/static_scene/test_known_k_session3.py"],
        ),
        ("ai/services/tier0/video_ingest.py", ["tests/deploy/test_session1_live.py"]),
    ]
    for changed_path, expected_tests in cases:
        selected = set(select_tests([changed_path], ai_dir=AI_DIR))
        for exp_test in expected_tests:
            assert exp_test in selected, f"{changed_path} 변경 시 {exp_test} 가 선택되지 않음: {sorted(selected)}"


def test_f3_synthetic_string_literal_heuristics(synthetic_ai: Path):
    """F3 합성 시험: 파일 stem, 상대 경로, 모듈 점 이름(sim.run) 문자열 리터럴 참조 시험이 선택된다."""
    _write_files(
        synthetic_ai,
        {
            "sim/run.py": "from sim.body import V\n",
            "sim/scenarios/custom_case.json": "{}\n",
            "experiments/static_scene/dynamic_tool.py": "X = 1\n",
            "tests/sim/test_cli_subproc.py": (
                "import subprocess\n"
                "def test_cli():\n"
                "    subprocess.run(['python', '-m', 'sim.run', '--scenario', 'sim/scenarios/custom_case.json'])\n"
            ),
            "tests/experiments/static_scene/test_loader.py": (
                "def _load(name): return name\n"
                "def test_dyn():\n"
                "    _load('dynamic_tool')\n"
            ),
        },
    )
    # 1. 모듈 점 이름(`sim.run`) 및 그 하위 의존(`sim/vec.py` -> `sim/body.py` -> `sim/run.py`) 변경 시 subprocess 시험 선택
    assert "tests/sim/test_cli_subproc.py" in select_tests(["ai/sim/run.py"], ai_dir=synthetic_ai)
    assert "tests/sim/test_cli_subproc.py" in select_tests(["ai/sim/vec.py"], ai_dir=synthetic_ai)

    # 2. 데이터 파일 상대 경로 / stem 문자열 리터럴 매칭
    assert "tests/sim/test_cli_subproc.py" in select_tests(
        ["ai/sim/scenarios/custom_case.json"], ai_dir=synthetic_ai
    )

    # 3. 동적 로드 파일 stem(`dynamic_tool`) 문자열 리터럴 매칭
    assert "tests/experiments/static_scene/test_loader.py" in select_tests(
        ["ai/experiments/static_scene/dynamic_tool.py"], ai_dir=synthetic_ai
    )

    # docstring 의 일반 산문 문자열은 추출 대상에서 제외됨을 확인
    s_lits = extract_ast_strings('"""이 문서는 여러 단어로 된 설명입니다."""\nX = "real_token"\n')
    assert "real_token" in s_lits
    assert not any("설명입니다" in s for s in s_lits)


def test_always_include_when_unreferenced_ai_module_changes(synthetic_ai: Path):
    """어느 시험도 import 하지 않는 모듈이 바뀌어도 항상 포함(`tests/architecture`)은 결과에 들어간다."""
    _write_files(synthetic_ai, {"src/simai_ai/alpha/orphan.py": "ORPHAN = True\n"})
    result = select_tests(["ai/src/simai_ai/alpha/orphan.py"], ai_dir=synthetic_ai)
    assert result == ["tests/architecture"]


@pytest.mark.parametrize(
    "trigger_path",
    [
        pytest.param("ai/uv.lock", id="uv-lock"),
        pytest.param("ai/pyproject.toml", id="pyproject-toml"),
        pytest.param("ai/tests/conftest.py", id="root-conftest"),
        pytest.param("ai/tests/alpha/conftest.py", id="subdir-conftest"),
        pytest.param("contracts/messages/world_state.schema.json", id="contracts-schema"),
        pytest.param("ai/src/simai_ai/contracts/models.py", id="generated-contract-models"),
        pytest.param("ai/scripts/select_tests.py", id="script-itself"),
        pytest.param("ai/scripts/select_tests.json", id="script-config"),
    ],
)
def test_all_conditions_each_return_all(
    synthetic_ai: Path, trigger_path: str, capsys: pytest.CaptureFixture[str]
):
    """ALL 조건 파일이 하나라도 포함되면 `['ALL']` 을 반환하고 stderr 에 원인 파일을 남긴다."""
    assert select_tests([trigger_path], ai_dir=synthetic_ai) == ["ALL"]
    err = capsys.readouterr().err
    assert "ALL:" in err


def test_graph_build_failure_and_bad_cli_fall_back_to_all(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """그래프를 만들 수 없는 오류나 잘못된 CLI 옵션에서도 stderr 에 원인을 남기고 안전하게 `ALL` 과 종료 코드 0 을 반환한다."""
    # 루트 패키지가 전혀 없는 빈 디렉터리에서 src 모듈 변경 요청 -> 그래프 생성 실패 -> ALL
    assert select_tests(["ai/src/simai_ai/alpha/core.py"], ai_dir=tmp_path) == ["ALL"]
    assert "ALL: fallback" in capsys.readouterr().err

    # 정상 ALL 트리거 CLI
    rc = main(["ai/uv.lock"])
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == "ALL"
    assert "ai/uv.lock" in captured.err

    # 잘못된 CLI 옵션도 종료 코드 0 + ALL 로 안전 복귀
    rc_bad = main(["--unknown-option"])
    captured_bad = capsys.readouterr()
    assert rc_bad == 0
    assert captured_bad.out.strip() == "ALL"
    assert "ALL:" in captured_bad.err


def test_empty_output_for_non_ai_changes(synthetic_ai: Path, capsys: pytest.CaptureFixture[str]):
    """규칙에 없는 ai 밖(front, server, gateway, docs 등)만 바뀌면 빈 출력을 낸다."""
    non_ai = [
        "front/src/App.tsx",
        "server/src/main/java/Application.java",
        "gateway/simai_relay/main.py",
        "docs/spec/05-processing.md",
        "AGENTS.md",
    ]
    assert select_tests(non_ai, ai_dir=synthetic_ai) == []
    assert select_tests([], ai_dir=synthetic_ai) == []

    rc = main(non_ai)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_deleted_and_renamed_files_and_special_git_paths(synthetic_ai: Path):
    """삭제·이름 변경 파일, NUL(`-z`) 구분 출력, 공백·비ASCII(8진수 인용) 경로를 정확히 처리한다."""
    _write_files(
        synthetic_ai,
        {
            "tests/alpha/test_removed.py": "from simai_ai.alpha import removed\n",
            "tests/alpha/test_공백 이름.py": "import simai_ai.alpha.core\n",
        },
    )
    res_deleted = select_tests(
        [
            "D\tai/src/simai_ai/alpha/removed.py",
            "D\tai/tests/alpha/test_already_gone.py",
        ],
        ai_dir=synthetic_ai,
    )
    assert "tests/alpha/test_removed.py" in res_deleted
    assert "tests/alpha/test_already_gone.py" not in res_deleted

    # 이름 변경(R100 탭 구분 및 -z NUL 구분): 옛 모듈 이름(core)을 import 하는 시험들이 선택된다
    res_renamed = select_tests(
        ["R100\tai/src/simai_ai/alpha/core.py\tai/src/simai_ai/alpha/core_v2.py"],
        ai_dir=synthetic_ai,
    )
    assert "tests/alpha/test_core.py" in res_renamed
    assert "tests/beta/test_mid.py" in res_renamed
    assert "tests/services/test_top.py" in res_renamed

    res_z = select_tests(
        ["R100\0ai/src/simai_ai/alpha/core.py\0ai/src/simai_ai/alpha/core_v2.py\0"],
        ai_dir=synthetic_ai,
    )
    assert "tests/alpha/test_core.py" in res_z

    # 공백 및 Git C-quoted 비ASCII 경로 처리
    quoted_korean = '"ai/tests/alpha/test_\\352\\263\\265\\353\\260\\261 \\354\\235\\264\\353\\246\\204.py"'
    res_quoted = select_tests([quoted_korean], ai_dir=synthetic_ai)
    assert "tests/alpha/test_공백 이름.py" in res_quoted


def _has_commit_and_parent(rev: str, repo_root: Path = REPO_ROOT) -> bool:
    """커밋과 비교 대상 첫 번째 부모(`<rev>^`)가 모두 존재하는지 확인한다 (F4: 얕은 clone 경계 대비)."""
    for target in (f"{rev}^{{commit}}", f"{rev}^^{{commit}}"):
        res = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", target],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        if res.returncode != 0:
            return False
    return True


def test_f4_shallow_parentless_commit_is_detected_and_not_diffed_as_root(tmp_path: Path):
    """F4 반례: 커밋 객체만 있고 부모(`<sha>^`)가 없는 경우 `_has_commit_and_parent` 가 False 이고 `changed_files_in_commit` 은 예외를 낸다."""
    repo = tmp_path / "shallow_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)

    (repo / "contracts").mkdir()
    (repo / "contracts" / "a.json").write_text("{}", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    c1 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    # 부모가 없는 첫/경계 커밋: _has_commit_and_parent 는 False 여야 하며 --root 로 전체 트리를 읽지 않아야 한다
    assert _has_commit_and_parent(c1, repo_root=repo) is False
    with pytest.raises(RuntimeError, match="parent"):
        changed_files_in_commit(c1, repo_root=repo)

    # 두 번째 커밋은 부모(c1)가 있으므로 True 이고 변경된 파일만 정확히 반환한다
    (repo / "only_second.txt").write_text("hello", encoding="utf-8")
    subprocess.run(["git", "add", "only_second.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=repo, check=True)
    c2 = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert _has_commit_and_parent(c2, repo_root=repo) is True
    assert changed_files_in_commit(c2, repo_root=repo) == ["only_second.txt"]


def test_git_history_commit_6393a88_selects_modified_tests():
    """실제 커밋 6393a88(-245, 삼각측량 비유한 SVD 방어)에서 그 커밋이 고친 시험 파일들이 선택된다."""
    if not _has_commit_and_parent("6393a88"):
        pytest.skip("얕은 clone 등으로 커밋 6393a88 또는 부모(6393a88^)가 없음")

    files = changed_files_in_commit("6393a88", repo_root=REPO_ROOT)
    selected = select_tests(files, ai_dir=AI_DIR)
    assert selected != ["ALL"]
    assert "tests/architecture" in selected
    assert "tests/calibration/test_watch.py" in selected
    assert "tests/geometry/test_geometry_speedup_regression.py" in selected
    assert "tests/geometry/test_nonfinite_lapack.py" in selected

    # 시험 파일 변경을 빼고 src/ 변경만 넣었을 때도 import 그래프를 통해 세 시험이 모두 선택되어야 한다
    src_only = [f for f in files if not f.startswith("ai/tests/")]
    selected_from_src = select_tests(src_only, ai_dir=AI_DIR)
    assert "tests/calibration/test_watch.py" in selected_from_src
    assert "tests/geometry/test_geometry_speedup_regression.py" in selected_from_src
    assert "tests/geometry/test_nonfinite_lapack.py" in selected_from_src


def test_git_history_commit_a420b5a_returns_all():
    """실제 커밋 a420b5a(-249, 계약 변경)는 ALL 을 반환해야 한다."""
    if not _has_commit_and_parent("a420b5a"):
        pytest.skip("얕은 clone 등으로 커밋 a420b5a 또는 부모(a420b5a^)가 없음")

    files = changed_files_in_commit("a420b5a", repo_root=REPO_ROOT)
    assert select_tests(files, ai_dir=AI_DIR) == ["ALL"]


