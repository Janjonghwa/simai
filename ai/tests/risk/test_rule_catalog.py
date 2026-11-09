"""규칙 등록부 → DB 규칙 버전 시드 (S15P31A101-211)."""

import importlib
import json
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
    values = _block(sql, "-- ② ", "-- ②' ")
    assert "UPDATE rule_version" not in values and "RAISE NOTICE" not in values
    assert "RAISE EXCEPTION" in values and "첫 운영 배포(2026-11-02) 뒤라 같은 버전 값 변경 금지" in values
    assert "-- 표시: 첫 운영 배포 2026-11-02 뒤" in sql


def test_first_operation_deploy_must_be_iso_date() -> None:
    with pytest.raises(ValueError):
        render_flyway_seed(catalog(), first_operation_deploy="11월 2일")


@pytest.mark.parametrize("deploy", [None, "2026-11-02"])
def test_blocks_in_order(deploy: str | None) -> None:
    """① 시드 → ② 값 → ②' 근거 조항(S15P31A101-256) → ③ 새 버전."""
    sql = render_flyway_seed(catalog(), first_operation_deploy=deploy)
    marks = [sql.index(m) for m in ("-- ① ", "-- ② ", "-- ②' ", "-- ③ ")]
    assert marks == sorted(marks)
    assert sql.index("CREATE TEMP TABLE") > marks[0] and sql.index("INSERT INTO rule_version (") > marks[3]


def _block(sql: str, start: str, end: str) -> str:
    return sql[sql.index(start) : sql.index(end)]


CLAUSES = Path(__file__).resolve().parents[2] / "src/simai_ai/rag/data/clauses.json"


def test_r3_legal_basis_only_on_operation_profile() -> None:
    """R3 v1·R9 v1·R11 v1 만 근거 조항을 갖고, 교실 시험 프로파일(v1001)과 다른 규칙은 비어 있다(S15P31A101-256·-519·-548)."""
    by_key = {(e.rule_id, e.rule_version): e.legal_basis for e in catalog()}
    assert by_key.pop(("R3", 1)) == "산업안전보건기준규칙 제32조; 산업안전보건기준규칙 제44조"
    assert by_key.pop(("R9", 1)) == "산업안전보건기준규칙 제32조"  # ①1호 안전모(명세 7.2 R9)
    assert by_key.pop(("R11", 1)) == "산업안전보건기준규칙 제32조"  # 보호구(조끼, -548)
    assert set(by_key.values()) == {None}


def test_ppe_rules_are_seeded_for_lead_approved_demo() -> None:
    """Lead 시연 예외(-556): G9·G6 통과를 주장하지 않고 R9·R11 v1 을 EC2 시드에 넣는다."""
    from simai_ai.risk.rule_catalog import DISABLED_BY_DEFAULT, seed_entries

    seeded = {(e.rule_id, e.rule_version) for e in seed_entries()}
    assert {("R9", 1), ("R11", 1)} <= seeded
    assert DISABLED_BY_DEFAULT == {}
    assert "'R9'" in render_flyway_seed(seed_entries())
    assert "'R11'" in render_flyway_seed(seed_entries())
    assert seeded | set(DISABLED_BY_DEFAULT) == set(default_registry())


def test_r11_demo_seed_preserves_caution_severity() -> None:
    """시연 예외는 활성화만 바꾼다. 조끼 R11 의 판·심각도는 그대로다(-556)."""
    from simai_ai.risk.rule_catalog import seed_entries

    entry = next(e for e in catalog() if (e.rule_id, e.rule_version) == ("R11", 1))
    assert entry.default_severity == "CAUTION"
    assert ("R11", 1) in {(e.rule_id, e.rule_version) for e in seed_entries()}


def test_legal_basis_ids_exist_in_clause_file() -> None:
    """등록부의 근거 조항 ID 는 조 단위이고 RAG 조항 파일에 있다. risk 는 rag 를 import 하지 않으므로 JSON 만 읽는다."""
    data = json.loads(CLAUSES.read_text(encoding="utf-8"))
    articles = {f"산업안전보건기준규칙 {a['article']}" for a in data["articles"]}
    ids = [i.strip() for e in catalog() if e.legal_basis for i in e.legal_basis.split(";")]
    assert ids
    for clause_id in ids:
        assert "#" not in clause_id, f"청크 접미사 없는 조 단위 ID 여야 한다: {clause_id}"
        assert clause_id in articles, f"조항 파일에 없는 근거 조항: {clause_id}"


@pytest.mark.parametrize("deploy", [None, "2026-11-02"])
def test_legal_basis_block_fills_empty(deploy: str | None) -> None:
    """②': DB 가 비어 있으면 표시와 관계없이 채우고 NOTICE 를 남긴다."""
    block = _block(render_flyway_seed(catalog(), first_operation_deploy=deploy), "-- ②' ", "-- ③ ")
    assert "UPDATE rule_version SET legal_basis = c.legal_basis" in block
    assert "근거 조항(legal_basis)이 비어 있어 등록부 값으로 채웠다" in block


def test_legal_basis_block_before_first_operation_deploy_updates_different() -> None:
    block = _block(render_flyway_seed(catalog(), first_operation_deploy=None), "-- ②' ", "-- ③ ")
    assert "WHERE r.legal_basis IS DISTINCT FROM s.legal_basis" in block
    assert "RAISE EXCEPTION" not in block
    assert "첫 운영 배포 전 규칙 보완 예외(11.4-1): % v% 근거 조항(legal_basis)을 등록부 값으로 갱신했다" in block


def test_legal_basis_block_after_first_operation_deploy_raises_on_different() -> None:
    block = _block(render_flyway_seed(catalog(), first_operation_deploy="2026-11-02"), "-- ②' ", "-- ③ ")
    assert "WHERE r.legal_basis IS NOT NULL AND r.legal_basis IS DISTINCT FROM s.legal_basis" in block
    assert "근거 조항(legal_basis)이 바뀌었는데 rule_version 이 같다" in block
    assert "첫 운영 배포(2026-11-02) 뒤라 같은 버전 값 변경 금지" in block
    assert block.index("RAISE EXCEPTION") < block.index("UPDATE rule_version")  # 멈출 때는 채우기 전에 멈춘다
