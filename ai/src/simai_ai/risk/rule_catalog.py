"""규칙 등록부 → DB 규칙 버전 시드 (S15P31A101-211).

EC2 DB 의 `rule_version` 테이블(기획서 11.1, docs/design/db-tables.md)은 이 모듈이 등록부(`default_registry`)에서
뽑아 만든 Flyway 반복 마이그레이션(`server/src/main/resources/db/migration/R__rule_versions.sql`)으로 채운다.
임계값을 손으로 옮겨 적지 않으므로 Tier 0·EC2 Risk Engine 과 DB 의 값이 어긋나지 않는다.

- 값은 등록부가 만든 규칙 객체의 설정(생성자 인자가 들어간 속성, 높이 프로파일)이다. 판정 중에 바뀌는 상태는 없다.
- R6(관측 상실 유지)으로 감싼 규칙은 감싼 규칙의 값과 `r6` 값을 함께 적는다.
- 프로파일: 버전이 시험 프로파일 범위면 CLASSROOM_TEST, 아니면 OPERATION(7.4).
- 기본 심각도: 규칙 모듈의 `{규칙 ID}_SEVERITY` 상수(DEFAULT_SEVERITY). 없으면 비운다(R1 은 구역 심각도, 그 밖은 규칙이 판정마다 정함).
- 규칙 버전은 고치지 않고 새 버전을 더한다(11.4-1). 같은 (ID, 버전)의 값(profile·thresholds·default_severity)이 DB 와 다르면
  첫 운영 배포 전(`FIRST_OPERATION_DEPLOY` 가 None)에는 그 행을 등록부 값으로 갱신하고 RAISE NOTICE 를 남기며(규칙 보완 예외,
  S15P31A101-252), 첫 운영 배포 뒤에는 마이그레이션이 멈춘다. reads·valid_from 은 이미 있는 행에서 고치지 않는다.
- 근거 조항(legal_basis)은 DB 가 비어 있으면 채운다. DB 에 다른 값이 있으면 위와 같이 첫 운영 배포 전에는 갱신하고 NOTICE,
  뒤에는 멈춘다(S15P31A101-256).

등록부를 바꾸면 다시 만든다. `ai/` 에서:

    python -m simai_ai.risk.rule_catalog --write ../server/src/main/resources/db/migration/R__rule_versions.sql

`--check` 는 파일이 최신인지만 본다(테스트 tests/risk/test_rule_catalog.py 가 같은 검사를 한다).
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import json
import math
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from simai_ai.risk.registry import RuleRegistry, default_registry
from simai_ai.risk.rules.height_common import is_test_profile_version
from simai_ai.risk.rules.r2_edge_proximity import R2_RULE_ID, R2_SEVERITY
from simai_ai.risk.rules.r3_elevated_position import R3_RULE_ID, R3_SEVERITY
from simai_ai.risk.rules.r4_fall_suspect import R4_RULE_ID, R4_SEVERITY
from simai_ai.risk.rules.r5_entry_prediction import R5_RULE_ID, R5_SEVERITY
from simai_ai.risk.rules.r9_helmet import R9_RULE_ID, R9_RULE_VERSION, R9_SEVERITY
from simai_ai.risk.rules.r11_vest import R11_RULE_ID, R11_RULE_VERSION, R11_SEVERITY

_SKIP_ATTRS = {"_spec", "_inner"}
# 규칙 모듈의 고정 심각도 상수({규칙 ID}_SEVERITY). risk 는 동적 import 를 쓰지 않으므로 직접 가져온다.
# 새 규칙이 이 상수를 두면 여기에 더한다(tests/risk/test_rule_catalog.py 가 빠진 것을 잡는다).
DEFAULT_SEVERITY = {
    R2_RULE_ID: R2_SEVERITY,
    R3_RULE_ID: R3_SEVERITY,
    R4_RULE_ID: R4_SEVERITY,
    R5_RULE_ID: R5_SEVERITY,
    R9_RULE_ID: R9_SEVERITY,
    R11_RULE_ID: R11_SEVERITY,
}
# 기본 꺼진 규칙: 등록부에는 있어(두 배포가 같은 규칙 버전·공통 벡터) 구성 묶음에 적히면 돌지만, 시드에서 빼서 EC2 DB 의
# rule_version 에 들어가지 않으므로 EC2 가 구성 묶음 rules 에 싣지 않는다(ConfigBundleService 는 유효한 규칙을 모두 싣는다).
# 켜는 것은 여기서 빼고 --write 로 시드를 다시 만드는 MR 이다(명세 8.10·15.1, S15P31A101-519).
DISABLED_BY_DEFAULT: dict[tuple[str, int], str] = {
    (R9_RULE_ID, R9_RULE_VERSION): "G9·G6-R9 통과 전(명세 15.1). 통과 뒤 이 줄을 지우고 시드를 다시 만들어 켠다",
    (R11_RULE_ID, R11_RULE_VERSION): "G9(조끼)·G6-R11 통과 전(S15P31A101-548). 통과 뒤 이 줄을 지우고 시드를 다시 만들어 켠다",
}
# 첫 실제 운영 배포 날짜(ISO, 예 "2026-11-02"). None 이면 첫 운영 배포 전이라 같은 버전의 값 변경을 시드가 갱신한다(11.4-1 예외).
# 첫 운영 배포 MR 이 날짜를 넣고 --write 로 시드를 다시 만들고 명세 README 개정 이력에 같은 날짜를 적는다. 시점은 Lead 가 정한다
# (docs/design/rule-seed-preop-exception.md).
FIRST_OPERATION_DEPLOY: str | None = None


@dataclasses.dataclass(frozen=True)
class RuleCatalogEntry:
    rule_id: str
    rule_version: int
    profile: str
    thresholds: dict[str, Any]
    default_severity: str | None
    legal_basis: str | None
    reads: tuple[str, ...]


def _value(v: Any) -> Any:
    """설정값을 JSON 값으로. 숫자·문자열·불·None·Enum·dataclass 와 그 묶음(집합은 정렬한 목록)만 받는다."""
    if v is None or isinstance(v, bool | str):
        return v
    if isinstance(v, frozenset | set):
        return sorted(_value(x) for x in v)
    if isinstance(v, tuple | list):
        return [_value(x) for x in v]
    if isinstance(v, int | float):
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError(f"유한하지 않은 설정값: {v!r}")
        return v
    if isinstance(v, enum.Enum):
        return _value(v.value)
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: _value(getattr(v, f.name)) for f in dataclasses.fields(v)}
    raise TypeError(f"시드에 담을 수 없는 설정값: {type(v).__name__}")


def _settings(rule: object) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, v in sorted(vars(rule).items()):
        if name in _SKIP_ATTRS:
            continue
        out[name.lstrip("_")] = _value(v)
    return out


def _default_severity(rule_id: str) -> str | None:
    severity = DEFAULT_SEVERITY.get(rule_id)
    return None if severity is None else str(_value(severity))


def seed_entries(registry: RuleRegistry | None = None) -> list[RuleCatalogEntry]:
    """시드에 넣는 항목: 등록부 전체에서 기본 꺼진 규칙(DISABLED_BY_DEFAULT)을 뺀 것."""
    return [e for e in catalog(registry) if (e.rule_id, e.rule_version) not in DISABLED_BY_DEFAULT]


def catalog(registry: RuleRegistry | None = None) -> list[RuleCatalogEntry]:
    registry = default_registry() if registry is None else registry
    entries = []
    for rule_id, rule_version in registry:
        rule = registry.create(rule_id, rule_version)
        inner = getattr(rule, "_inner", None)
        base = rule if inner is None else inner
        thresholds = _settings(base)
        if inner is not None:
            thresholds["r6"] = _settings(rule)
        entries.append(
            RuleCatalogEntry(
                rule_id=rule_id,
                rule_version=rule_version,
                profile="CLASSROOM_TEST" if is_test_profile_version(rule_version) else "OPERATION",
                thresholds=thresholds,
                default_severity=_default_severity(rule_id),
                legal_basis=rule.spec.legal_basis,
                reads=tuple(rule.spec.reads),
            )
        )
    return entries


def _sql(v: str | None) -> str:
    return "NULL" if v is None else "'" + v.replace("'", "''") + "'"


def _sql_array(values: Iterable[str]) -> str:
    return "ARRAY[" + ", ".join(_sql(x) for x in values) + "]::text[]"


def _seed_table_block(entries: list[RuleCatalogEntry]) -> list[str]:
    """① 등록부 값을 담은 임시 시드 테이블."""
    rows = []
    for e in entries:
        thresholds = json.dumps(e.thresholds, ensure_ascii=False, sort_keys=True)
        rows.append(
            f"    ({_sql(e.rule_id)}, {e.rule_version}, {_sql(e.profile)}, {_sql(thresholds)}::jsonb, "
            f"{_sql(e.default_severity)}, {_sql(e.legal_basis)}, {_sql_array(e.reads)})"
        )
    return [
        "-- ① 등록부 값",
        "CREATE TEMP TABLE rule_version_seed (",
        "    rule_id text NOT NULL,",
        "    rule_version integer NOT NULL,",
        "    profile text NOT NULL,",
        "    thresholds jsonb NOT NULL,",
        "    default_severity text,",
        "    legal_basis text,",
        "    reads text[] NOT NULL",
        ") ON COMMIT DROP;",
        "",
        "INSERT INTO rule_version_seed (rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads) VALUES",
        ",\n".join(rows) + ";",
        "",
    ]


def _value_changed(indent: str) -> str:
    """값 블록이 보는 칼럼(profile·thresholds·default_severity)이 DB 와 다른 조건."""
    return (
        "r.profile IS DISTINCT FROM s.profile OR r.thresholds IS DISTINCT FROM s.thresholds\n"
        f"{indent}   OR r.default_severity IS DISTINCT FROM s.default_severity"
    )


def _value_block(first_operation_deploy: str | None) -> list[str]:
    """② 이미 있는 (rule_id, rule_version) 의 값(profile·thresholds·default_severity)이 등록부와 다를 때.

    첫 운영 배포 전이면 그 행을 갱신하고 NOTICE, 뒤면 멈춘다(11.4-1).
    """
    if first_operation_deploy is None:
        return [
            "-- ② 첫 운영 배포 전 규칙 보완 예외(11.4-1): 같은 버전의 값이 다르면 등록부 값으로 갱신한다(legal_basis·reads·valid_from 은 그대로).",
            "DO $$",
            "DECLARE c record;",
            "BEGIN",
            "    FOR c IN SELECT s.* FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)",
            f"             WHERE {_value_changed(' ' * 13)}",
            "             ORDER BY s.rule_id, s.rule_version",
            "    LOOP",
            "        UPDATE rule_version SET profile = c.profile, thresholds = c.thresholds, default_severity = c.default_severity",
            "        WHERE rule_id = c.rule_id AND rule_version = c.rule_version;",
            "        RAISE NOTICE '첫 운영 배포 전 규칙 보완 예외(11.4-1): % v% 값을 등록부 값으로 갱신했다', c.rule_id, c.rule_version;",
            "    END LOOP;",
            "END $$;",
            "",
        ]
    return [
        f"-- ② 첫 운영 배포({first_operation_deploy}) 뒤: 같은 버전의 값이 다르면 멈춘다(11.4-1).",
        "DO $$",
        "DECLARE changed text;",
        "BEGIN",
        "    SELECT string_agg(s.rule_id || ' v' || s.rule_version, ', ' ORDER BY s.rule_id, s.rule_version) INTO changed",
        "    FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)",
        f"    WHERE {_value_changed(' ' * 4)};",
        "    IF changed IS NOT NULL THEN",
        f"        RAISE EXCEPTION '규칙 값이 바뀌었는데 rule_version 이 같다: %. 첫 운영 배포({first_operation_deploy}) 뒤라 같은 버전 값 변경 금지, "
        "등록부에서 버전을 올린다', changed;",
        "    END IF;",
        "END $$;",
        "",
    ]


def _legal_basis_fill_loop(where: str, notice: list[str]) -> list[str]:
    """legal_basis 를 시드 값으로 바꾸고 행마다 NOTICE 를 남기는 루프(②' 블록 안)."""
    return [
        "    FOR c IN SELECT s.rule_id, s.rule_version, s.legal_basis, r.legal_basis AS db_legal_basis",
        "             FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)",
        f"             WHERE {where}",
        "             ORDER BY s.rule_id, s.rule_version",
        "    LOOP",
        "        UPDATE rule_version SET legal_basis = c.legal_basis WHERE rule_id = c.rule_id AND rule_version = c.rule_version;",
        *notice,
        "    END LOOP;",
    ]


_FILLED_NOTICE = "RAISE NOTICE '근거 조항(legal_basis)이 비어 있어 등록부 값으로 채웠다: % v%', c.rule_id, c.rule_version;"


def _legal_basis_block(first_operation_deploy: str | None) -> list[str]:
    """②' 이미 있는 (rule_id, rule_version) 의 근거 조항(legal_basis)이 등록부와 다를 때(S15P31A101-256).

    DB 가 비어 있으면 채운다. DB 에 다른 값이 있으면 ② 와 같이 첫 운영 배포 전이면 갱신하고 NOTICE, 뒤면 멈춘다(11.4-1).
    """
    if first_operation_deploy is None:
        return [
            "-- ②' 근거 조항(legal_basis): 비어 있으면 채우고, 다르면 첫 운영 배포 전 규칙 보완 예외(11.4-1)로 등록부 값으로 갱신한다.",
            "DO $$",
            "DECLARE c record;",
            "BEGIN",
            *_legal_basis_fill_loop(
                "r.legal_basis IS DISTINCT FROM s.legal_basis",
                [
                    "        IF c.db_legal_basis IS NULL THEN",
                    f"            {_FILLED_NOTICE}",
                    "        ELSE",
                    "            RAISE NOTICE '첫 운영 배포 전 규칙 보완 예외(11.4-1): % v% 근거 조항(legal_basis)을 등록부 값으로 갱신했다', c.rule_id, c.rule_version;",
                    "        END IF;",
                ],
            ),
            "END $$;",
            "",
        ]
    return [
        f"-- ②' 근거 조항(legal_basis): 비어 있으면 채운다. 첫 운영 배포({first_operation_deploy}) 뒤라 다른 값이 있으면 멈춘다(11.4-1).",
        "DO $$",
        "DECLARE changed text; c record;",
        "BEGIN",
        "    SELECT string_agg(s.rule_id || ' v' || s.rule_version, ', ' ORDER BY s.rule_id, s.rule_version) INTO changed",
        "    FROM rule_version_seed s JOIN rule_version r USING (rule_id, rule_version)",
        "    WHERE r.legal_basis IS NOT NULL AND r.legal_basis IS DISTINCT FROM s.legal_basis;",
        "    IF changed IS NOT NULL THEN",
        f"        RAISE EXCEPTION '근거 조항(legal_basis)이 바뀌었는데 rule_version 이 같다: %. 첫 운영 배포({first_operation_deploy}) 뒤라 같은 버전 값 변경 금지, "
        "등록부에서 버전을 올린다', changed;",
        "    END IF;",
        *_legal_basis_fill_loop("r.legal_basis IS NULL AND s.legal_basis IS NOT NULL", [f"        {_FILLED_NOTICE}"]),
        "END $$;",
        "",
    ]


def _insert_block() -> list[str]:
    """③ 새 (rule_id, rule_version) 만 더한다."""
    return [
        "-- ③ 새 (rule_id, rule_version) 만 더한다.",
        "INSERT INTO rule_version (rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads, valid_from)",
        "SELECT rule_id, rule_version, profile, thresholds, default_severity, legal_basis, reads, now() FROM rule_version_seed",
        "ON CONFLICT (rule_id, rule_version) DO NOTHING;",
        "",
    ]


def render_flyway_seed(entries: list[RuleCatalogEntry], first_operation_deploy: str | None = FIRST_OPERATION_DEPLOY) -> str:
    if first_operation_deploy is not None:
        # risk 는 datetime 을 import 하지 않는다(tests/risk/test_isolation.py). 형식만 본다
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", first_operation_deploy):
            raise ValueError(f"FIRST_OPERATION_DEPLOY 는 ISO 날짜(YYYY-MM-DD)다: {first_operation_deploy!r}")
        state = f"첫 운영 배포 {first_operation_deploy} 뒤: 같은 (rule_id, rule_version) 인데 값이 다르면 멈춘다. 규칙 값을 바꿀 때는 rule_version 을 올린다."
    else:
        state = "첫 운영 배포 전(FIRST_OPERATION_DEPLOY = None): 같은 (rule_id, rule_version) 인데 값이 다르면 등록부 값으로 갱신하고 NOTICE 를 남긴다."
    header = [
        "-- 자동 생성 파일이다. 손으로 고치지 않는다.",
        "-- 만든 곳: ai/src/simai_ai/risk/rule_catalog.py (규칙 등록부 default_registry). 등록부를 바꾸면 ai/ 에서",
        "--   python -m simai_ai.risk.rule_catalog --write ../server/src/main/resources/db/migration/R__rule_versions.sql",
        "-- Flyway 반복 마이그레이션이라 내용이 바뀔 때마다 다시 실행된다. 새 (rule_id, rule_version) 은 더한다(기획서 11.4-1).",
        f"-- 표시: {state}",
    ]
    return "\n".join(
        header
        + _seed_table_block(entries)
        + _value_block(first_operation_deploy)
        + _legal_basis_block(first_operation_deploy)
        + _insert_block()
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="규칙 등록부에서 DB 규칙 버전 시드(Flyway 반복 마이그레이션)를 만든다")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", type=Path, help="이 경로에 쓴다")
    group.add_argument("--check", type=Path, help="이 파일이 최신인지 확인한다(다르면 종료 코드 1)")
    args = parser.parse_args(argv)
    text = render_flyway_seed(seed_entries())
    if args.write:
        args.write.write_text(text, encoding="utf-8", newline="\n")
        return 0
    current = args.check.read_text(encoding="utf-8") if args.check.exists() else None
    if current != text:
        print(f"{args.check} 가 등록부와 다르다. --write 로 다시 만든다", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
