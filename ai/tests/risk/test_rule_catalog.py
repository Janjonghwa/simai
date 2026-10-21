"""규칙 등록부 → DB 규칙 버전 시드 (S15P31A101-211)."""

import importlib
import pkgutil
from pathlib import Path

import pytest

from simai_ai.risk.registry import default_registry
import simai_ai.risk.rules as rules_pkg
from simai_ai.risk.rule_catalog import DEFAULT_SEVERITY, catalog, main, render_flyway_seed

SEED = Path(__file__).resolve().parents[3] / "server/src/main/resources/db/migration/R__rule_versions.sql"


def test_catalog_covers_registry() -> None:
    entries = catalog()
    assert [(e.rule_id, e.rule_version) for e in entries] == list(default_registry())


def test_profiles_follow_rule_version() -> None:
    by_key = {(e.rule_id, e.rule_version): e for e in catalog()}
    assert by_key[("R2", 1)].profile == "OPERATION"
    assert by_key[("R2", 1001)].profile == "CLASSROOM_TEST"
    assert by_key[("R2", 1)].thresholds["profile"]["height_threshold_m"] == 2.0
    assert by_key[("R2", 1001)].thresholds["profile"]["height_threshold_m"] == 0.35


def test_wrapped_rule_keeps_r6_settings() -> None:
    r1 = next(e for e in catalog() if e.rule_id == "R1")
    assert "r6" in r1.thresholds
    assert r1.default_severity is None  # 구역 심각도를 따른다


def test_render_is_stable() -> None:
    assert render_flyway_seed(catalog()) == render_flyway_seed(catalog())


def test_committed_seed_is_up_to_date() -> None:
    if not SEED.exists():
        pytest.skip("server/ 가 없는 설치(ai 만 설치한 환경)")
    assert main(["--check", str(SEED)]) == 0, "등록부가 바뀌었다. rule_catalog --write 로 R__rule_versions.sql 을 다시 만든다"


def test_every_severity_constant_is_listed() -> None:
    """규칙 모듈에 {규칙 ID}_SEVERITY 상수가 생기면 DEFAULT_SEVERITY 에도 있어야 한다."""
    found = {}
    for info in pkgutil.iter_modules(rules_pkg.__path__):
        module = importlib.import_module(f"{rules_pkg.__name__}.{info.name}")
        for name, value in vars(module).items():
            if name.endswith("_SEVERITY") and name[:-len("_SEVERITY")].startswith("R"):
                found[name[: -len("_SEVERITY")]] = value
    assert found == DEFAULT_SEVERITY
