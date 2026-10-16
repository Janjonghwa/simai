"""공통 테스트 벡터 실행기. 저장소 벡터(tests/risk/vectors)가 배포 등록부로 모두 통과해야 한다."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.risk import UnknownRuleError, default_registry
from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.vectors import discover, load_vector, main, parse_vector, run_paths, run_vector

VECTOR_DIR = Path(__file__).resolve().parent / "vectors"
VECTOR_FILES = discover([VECTOR_DIR])


def test_vectors_exist():
    assert {p.name for p in VECTOR_FILES} >= {"empty_scene.json", "no_rules.json", "replay_zone_versions.json"}


@pytest.mark.parametrize("path", VECTOR_FILES, ids=lambda p: p.stem)
def test_repository_vector_passes(path):
    result = run_vector(load_vector(path), default_registry())
    assert result.passed, "\n".join(result.mismatches)


def test_vector_ids_are_unique():
    ids = [load_vector(p).vector_id for p in VECTOR_FILES]
    assert len(ids) == len(set(ids))


def test_default_registry_is_empty_until_rules_land():
    # R1~R7 은 후속 이슈에서 등록한다. 등록되면 이 시험을 지우고 규칙별 벡터를 추가한다.
    assert len(default_registry()) == 0


# --- 실행기 자체 ---------------------------------------------------------------


@pytest.fixture
def probe_vector(world_payload, zone_json):
    """PROBE 규칙 벡터: 사람 1명 × 구역 1개 → 판정 1건."""
    return {
        "format_version": "1.0.0",
        "vector_id": "probe",
        "description": "실행기 시험용",
        "origin": "LIVE",
        "rules": [{"rule_id": "PROBE", "rule_version": 1}],
        "zones": [zone_json()],
        "steps": [
            {
                "clock_ms": 1760342400200,
                "world_state": world_payload(),
                "expect": {
                    "findings": [
                        {
                            "rule_id": "PROBE",
                            "rule_version": 1,
                            "severity": "HIGH",
                            "world_ids": ["w-0012"],
                            "zone_id": "z-04",
                            "needs_check": False,
                            "reasons": ["probe"],
                            "metrics": {"staleness_ms": 120},
                        }
                    ]
                },
            }
        ],
    }


def _write(tmp_path: Path, obj, name="v.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return path


def test_runner_passes_matching_vector(probe_vector, probe_registry):
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


@pytest.mark.parametrize("origin", ["PLANNED", "REPLAY", "WHAT_IF"])
def test_runner_passes_same_findings_for_non_live(probe_vector, probe_registry, origin):
    probe_vector["origin"] = origin
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


def test_metric_tolerance(probe_vector, probe_registry):
    finding = probe_vector["steps"][0]["expect"]["findings"][0]
    finding["metrics"]["staleness_ms"] = 120 + 1e-12
    assert run_vector(parse_vector(probe_vector), probe_registry).passed
    finding["metrics"]["staleness_ms"] = 120.001
    assert not run_vector(parse_vector(probe_vector), probe_registry).passed


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("severity", "CRITICAL"),
        ("world_ids", ["w-9999"]),
        ("zone_id", None),
        ("needs_check", True),
        ("reasons", ["other"]),
        ("metrics", {"staleness_ms": 120, "extra_m": 1.0}),
    ],
)
def test_runner_reports_mismatch(probe_vector, probe_registry, field, value):
    probe_vector["steps"][0]["expect"]["findings"][0][field] = value
    result = run_vector(parse_vector(probe_vector), probe_registry)
    assert not result.passed
    assert result.mismatches[0].startswith("steps[0]: findings[0]")


def test_runner_reports_missing_finding(probe_vector, probe_registry):
    probe_vector["steps"][0]["expect"]["findings"] = []
    result = run_vector(parse_vector(probe_vector), probe_registry)
    assert not result.passed
    assert "판정 1건, 기대 0건" in result.mismatches[0]


def test_multi_step_uses_fixed_clock_per_step(probe_vector, probe_registry):
    second = copy.deepcopy(probe_vector["steps"][0])
    second["clock_ms"] = 1760342400600
    second["expect"]["findings"][0]["metrics"]["staleness_ms"] = 520
    probe_vector["steps"].append(second)
    assert run_vector(parse_vector(probe_vector), probe_registry).passed


def test_zone_target_finding_in_vector(probe_registry, zone_json):
    """사람 없는 구역의 시스템 판정(R7 형태)을 벡터 기대 결과로 비교할 수 있다(독립 리뷰 F1 재현 경로)."""
    vector = json.loads((VECTOR_DIR / "empty_scene.json").read_text(encoding="utf-8"))
    vector["rules"] = [{"rule_id": "ZONE_PROBE", "rule_version": 1}]
    vector["zones"] = [zone_json(zone_id="zone-review")]
    expected = {
        "rule_id": "ZONE_PROBE",
        "rule_version": 1,
        "severity": "HIGH",
        "world_ids": [],
        "zone_id": "zone-review",
        "needs_check": False,
        "reasons": ["coverage_lost"],
        "metrics": {"observing_cameras": 1},
    }
    vector["steps"][0]["expect"]["findings"] = [expected]
    assert vector["steps"][0]["world_state"]["tracks"] == []
    assert run_vector(parse_vector(vector), probe_registry).passed

    # 기대값이 틀리면 실패한다.
    expected["world_ids"] = ["w-0012"]
    assert not run_vector(parse_vector(vector), probe_registry).passed


def test_expected_finding_needs_a_target(probe_vector):
    finding = probe_vector["steps"][0]["expect"]["findings"][0]
    finding["world_ids"], finding["zone_id"] = [], None
    with pytest.raises(RiskInputError, match="판정 대상"):
        parse_vector(probe_vector)


def test_non_finite_numbers_in_vector_json_are_rejected(tmp_path, probe_vector):
    # Python json 은 표준 밖 NaN 을 받아 주므로 파서가 막아야 한다.
    text = json.dumps(probe_vector).replace("[3.4, 5.01]", "[NaN, 5.01]", 1)
    assert "NaN" in text
    path = tmp_path / "nan.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(RiskInputError, match="유한"):
        load_vector(path)


def test_unknown_rule_is_an_error(probe_vector):
    with pytest.raises(UnknownRuleError):
        run_vector(parse_vector(probe_vector), default_registry())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda v: v.update(format_version="2.0.0"), id="format-major"),
        pytest.param(lambda v: v.update(typo=1), id="unknown-top-key"),
        pytest.param(lambda v: v.update(steps=[]), id="no-steps"),
        pytest.param(lambda v: v.update(origin="TEST"), id="bad-origin"),
        pytest.param(lambda v: v["steps"][0].update(clock_ms=1.5), id="float-clock"),
        pytest.param(lambda v: v["steps"][0]["expect"]["findings"][0].pop("reasons"), id="finding-missing-key"),
        pytest.param(
            lambda v: v["steps"][0]["world_state"]["tracks"][0].update(support_surface_validity="UNKNOWN"), id="z0-fill"
        ),
        pytest.param(lambda v: v["steps"].append(copy.deepcopy(v["steps"][0]) | {"clock_ms": 1}), id="clock-goes-back"),
    ],
)
def test_bad_vector_is_rejected(probe_vector, mutate):
    mutate(probe_vector)
    with pytest.raises(RiskInputError):
        parse_vector(probe_vector)


def test_run_paths_reports_unreadable_vector(tmp_path, probe_vector):
    probe_vector["format_version"] = "9.0.0"
    results = run_paths([_write(tmp_path, probe_vector)])
    assert len(results) == 1 and not results[0].passed
    assert "format_version" in results[0].mismatches[0]


def test_cli_exit_codes(tmp_path, probe_vector, capsys):
    assert main([str(VECTOR_DIR)]) == 0
    assert f"{len(VECTOR_FILES)}/{len(VECTOR_FILES)} 통과" in capsys.readouterr().out

    # PROBE 는 배포 등록부에 없으므로 실패해야 한다.
    assert main([str(_write(tmp_path, probe_vector))]) == 1
    # 벡터가 하나도 없으면 통과로 치지 않는다.
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty)]) == 1


def test_cli_module_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "simai_ai.risk.vectors", str(VECTOR_DIR)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
