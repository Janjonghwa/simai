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


def test_before_first_operation_deploy_updates_same_version() -> None:
    """첫 운영 배포 전(11.4-1 예외): 같은 버전 값이 다르면 세 칼럼만 갱신하고 NOTICE, 멈추지 않는다(S15P31A101-252)."""
    sql = render_flyway_seed(catalog(), first_operation_deploy=None)
    assert "RAISE EXCEPTION" not in sql
    assert "UPDATE rule_version SET profile = c.profile, thresholds = c.thresholds, default_severity = c.default_severity\n" in sql
    assert "RAISE NOTICE" in sql
    update = sql[sql.index("UPDATE rule_version"):sql.index("RAISE NOTICE")]
    assert "legal_basis" not in update and "reads" not in update and "valid_from" not in update


def test_after_first_operation_deploy_raises() -> None:
    """첫 운영 배포 날짜가 있으면 같은 버전 값 변경을 멈추고 갱신하지 않는다."""
    sql = render_flyway_seed(catalog(), first_operation_deploy="2026-11-02")
    assert "UPDATE rule_version" not in sql and "RAISE NOTICE" not in sql
    assert "RAISE EXCEPTION" in sql and "첫 운영 배포(2026-11-02) 뒤라 같은 버전 값 변경 금지" in sql
    assert "-- 표시: 첫 운영 배포 2026-11-02 뒤" in sql


def test_first_operation_deploy_must_be_iso_date() -> None:
    with pytest.raises(ValueError):
        render_flyway_seed(catalog(), first_operation_deploy="11월 2일")


@pytest.mark.parametrize("deploy", [None, "2026-11-02"])
def test_blocks_in_order(deploy: str | None) -> None:
    """① 시드 → ② 값 → ③ 새 버전. S15P31A101-256 은 ②와 ③ 사이에 블록을 더한다."""
    sql = render_flyway_seed(catalog(), first_operation_deploy=deploy)
    marks = [sql.index(m) for m in ("-- ① ", "-- ② ", "-- ③ ")]
    assert marks == sorted(marks)
    assert sql.index("CREATE TEMP TABLE") > marks[0] and sql.index("INSERT INTO rule_version (") > marks[2]
