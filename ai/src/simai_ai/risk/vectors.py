"""Risk Engine 공통 테스트 벡터 형식과 실행기 (명세 3.2, 15.1 G3, 21.3).

같은 벡터를 Tier 0 배포 이미지와 EC2 컨테이너 이미지 양쪽에서 돌려 같은 결과가 나오는지 본다.
실행기는 배포 이미지에도 들어가도록 src 에 두고, 벡터 JSON 은 ai/tests/risk/vectors/ 에 둔다.

    python -m simai_ai.risk.vectors ai/tests/risk/vectors     # 모두 통과하면 종료 코드 0

벡터 형식(JSON, format_version 1.x). 형식은 ai/tests/risk/vectors/README.md 에도 적는다.

    {
      "format_version": "1.0.0",
      "vector_id": "empty-scene",
      "description": "무엇을 확인하는지",
      "origin": "LIVE",                                  # LIVE / PLANNED / REPLAY / WHAT_IF
      "rules": [{"rule_id": "R1", "rule_version": 1}],   # 등록부에서 만든다. 빈 배열 가능
      "zones": [ Zone ],                                 # simai_ai.risk.zone.Zone.from_json 형식
      "steps": [
        {
          "clock_ms": 1760342400200,                     # 고정 시계의 판정 시각. 단계마다 줄지 않는다
          "world_state": { I1 world_state payload },     # contracts/messages/world_state.schema.json
          "expect": {"findings": [ Finding ]}            # 순서 무관. 정렬해 비교한다
        }
      ]
    }

Finding 은 rule_id, rule_version, severity, world_ids, zone_id, needs_check, reasons, metrics 를 모두 적는다.
사람 없는 구역·시스템 판정(R7 형태)은 world_ids 를 [] 로, zone_id 를 채운다. 둘 다 비울 수는 없다.
metrics 는 절대 오차 METRIC_ABS_TOL 안에서 같으면 통과다(플랫폼별 libm 끝자리 차이). 나머지는 정확히 같아야 한다.
모든 단계에서 "LIVE 가 아니면 알림 후보 0건" 불변 규칙도 함께 검사한다.
"""

import argparse
import json
import math
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from simai_ai.risk._parse import (
    RiskInputError,
    as_bool,
    as_enum,
    as_int,
    as_list,
    as_mapping,
    as_number,
    as_opt_str,
    as_str,
    as_timestamp_ms,
    reject_unknown_keys,
    require,
)
from simai_ai.risk.clock import FixedClock
from simai_ai.risk.common import Origin, Severity
from simai_ai.risk.engine import Evaluation, RiskEngine, RuleContractError
from simai_ai.risk.registry import RuleRegistry, default_registry
from simai_ai.risk.rule import Finding
from simai_ai.risk.world import WorldState
from simai_ai.risk.zone import Zone

FORMAT_MAJOR = 1
METRIC_ABS_TOL = 1e-9


@dataclass(frozen=True)
class VectorStep:
    clock_ms: int
    world_state: WorldState
    expected: tuple[Finding, ...]


@dataclass(frozen=True)
class Vector:
    vector_id: str
    description: str
    origin: Origin
    rules: tuple[tuple[str, int], ...]
    zones: tuple[Zone, ...]
    steps: tuple[VectorStep, ...]
    path: Path | None = None


@dataclass(frozen=True)
class VectorResult:
    vector_id: str
    path: Path | None
    mismatches: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.mismatches


def parse_vector(obj: Any, path: Path | None = None) -> Vector:
    where = str(path) if path else "vector"
    obj = as_mapping(obj, where)
    reject_unknown_keys(obj, {"format_version", "vector_id", "description", "origin", "rules", "zones", "steps"}, where)

    version = as_str(require(obj, "format_version", where), f"{where}.format_version")
    if version.split(".")[0] != str(FORMAT_MAJOR):
        raise RiskInputError(f"{where}: format_version {version} 은 지원하지 않는다(지원: {FORMAT_MAJOR}.x)")

    rules = []
    for i, r in enumerate(as_list(require(obj, "rules", where), f"{where}.rules")):
        at = f"{where}.rules[{i}]"
        r = as_mapping(r, at)
        reject_unknown_keys(r, {"rule_id", "rule_version"}, at)
        rules.append((as_str(require(r, "rule_id", at), at), as_int(require(r, "rule_version", at), at, minimum=1)))

    steps = []
    last_clock = -1
    for i, s in enumerate(as_list(require(obj, "steps", where), f"{where}.steps")):
        at = f"{where}.steps[{i}]"
        s = as_mapping(s, at)
        reject_unknown_keys(s, {"clock_ms", "world_state", "expect"}, at)
        clock_ms = as_timestamp_ms(require(s, "clock_ms", at), f"{at}.clock_ms")
        if clock_ms < last_clock:
            raise RiskInputError(f"{at}.clock_ms: 단계의 시계가 뒤로 갔다 ({last_clock} → {clock_ms})")
        last_clock = clock_ms
        expect = as_mapping(require(s, "expect", at), f"{at}.expect")
        reject_unknown_keys(expect, {"findings"}, f"{at}.expect")
        findings = as_list(require(expect, "findings", f"{at}.expect"), f"{at}.expect.findings")
        steps.append(
            VectorStep(
                clock_ms=clock_ms,
                world_state=WorldState.from_payload(require(s, "world_state", at), f"{at}.world_state"),
                expected=tuple(
                    sorted(
                        (_parse_finding(f, f"{at}.expect.findings[{j}]") for j, f in enumerate(findings)),
                        key=lambda f: f.sort_key,
                    )
                ),
            )
        )
    if not steps:
        raise RiskInputError(f"{where}.steps: 단계가 하나 이상 있어야 한다")

    return Vector(
        vector_id=as_str(require(obj, "vector_id", where), f"{where}.vector_id"),
        description=as_str(require(obj, "description", where), f"{where}.description"),
        origin=as_enum(Origin, require(obj, "origin", where), f"{where}.origin"),
        rules=tuple(rules),
        zones=tuple(
            Zone.from_json(z, f"{where}.zones[{i}]")
            for i, z in enumerate(as_list(require(obj, "zones", where), f"{where}.zones"))
        ),
        steps=tuple(steps),
        path=path,
    )


def load_vector(path: Path) -> Vector:
    with open(path, encoding="utf-8") as f:
        return parse_vector(json.load(f), path)


def run_vector(vector: Vector, registry: RuleRegistry | None = None) -> VectorResult:
    registry = default_registry() if registry is None else registry
    clock = FixedClock(vector.steps[0].clock_ms)
    engine = RiskEngine((registry.create(rule_id, version) for rule_id, version in vector.rules), clock)

    mismatches: list[str] = []
    for i, step in enumerate(vector.steps):
        clock.set(step.clock_ms)
        evaluation = engine.evaluate(step.world_state, vector.zones, vector.origin)
        mismatches += [f"steps[{i}]: {m}" for m in _compare(evaluation, step)]
    return VectorResult(vector.vector_id, vector.path, tuple(mismatches))


def discover(paths: Iterable[Path]) -> list[Path]:
    """파일은 그대로, 디렉터리는 아래의 *.json 을 이름순으로 모은다."""
    found: list[Path] = []
    for p in paths:
        found += sorted(p.rglob("*.json")) if p.is_dir() else [p]
    return found


def run_paths(paths: Iterable[Path], registry: RuleRegistry | None = None) -> list[VectorResult]:
    results = []
    for path in discover(paths):
        try:
            results.append(run_vector(load_vector(path), registry))
        except (OSError, ValueError, LookupError, RuleContractError) as e:
            # 읽기·형식 오류(RiskInputError, JSON 오류), 미등록 규칙, 규칙 형식 위반은 그 벡터의 실패로 보고한다.
            results.append(VectorResult(path.stem, path, (f"벡터를 실행하지 못했다: {e}",)))
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m simai_ai.risk.vectors", description="Risk Engine 공통 테스트 벡터 실행"
    )
    parser.add_argument("paths", nargs="+", type=Path, help="벡터 JSON 파일 또는 디렉터리")
    args = parser.parse_args(argv)

    results = run_paths(args.paths)
    if not results:
        print("벡터가 없다", file=sys.stderr)
        return 1
    for r in results:
        print(f"{'PASS' if r.passed else 'FAIL'} {r.vector_id} ({r.path})")
        for m in r.mismatches:
            print(f"    {m}")
    failed = sum(not r.passed for r in results)
    print(f"{len(results) - failed}/{len(results)} 통과")
    return 0 if failed == 0 else 1


def _parse_finding(obj: Any, where: str) -> Finding:
    obj = as_mapping(obj, where)
    keys = {"rule_id", "rule_version", "severity", "world_ids", "zone_id", "needs_check", "reasons", "metrics"}
    reject_unknown_keys(obj, keys, where)
    metrics = as_mapping(require(obj, "metrics", where), f"{where}.metrics")
    return Finding(
        rule_id=as_str(require(obj, "rule_id", where), f"{where}.rule_id"),
        rule_version=as_int(require(obj, "rule_version", where), f"{where}.rule_version", minimum=1),
        severity=as_enum(Severity, require(obj, "severity", where), f"{where}.severity"),
        world_ids=tuple(
            as_str(w, f"{where}.world_ids[{i}]")
            for i, w in enumerate(as_list(require(obj, "world_ids", where), f"{where}.world_ids"))
        ),
        zone_id=as_opt_str(require(obj, "zone_id", where), f"{where}.zone_id"),
        needs_check=as_bool(require(obj, "needs_check", where), f"{where}.needs_check"),
        reasons=tuple(
            as_str(r, f"{where}.reasons[{i}]")
            for i, r in enumerate(as_list(require(obj, "reasons", where), f"{where}.reasons"))
        ),
        metrics={k: as_number(v, f"{where}.metrics.{k}") for k, v in metrics.items()},
    )


def _compare(evaluation: Evaluation, step: VectorStep) -> list[str]:
    out: list[str] = []
    if evaluation.evaluated_at != step.clock_ms:
        out.append(f"판정 시각 {evaluation.evaluated_at} != 고정 시계 {step.clock_ms}")
    alerts = evaluation.alert_candidates()
    if not evaluation.origin.alertable and alerts:
        out.append(f"{evaluation.origin} 판정에서 알림 후보 {len(alerts)}건이 나왔다(LIVE 만 알린다)")

    actual, expected = evaluation.findings, step.expected
    if len(actual) != len(expected):
        out.append(f"판정 {len(actual)}건, 기대 {len(expected)}건: 실제 {[_show(f) for f in actual]}")
        return out
    for j, (a, e) in enumerate(zip(actual, expected, strict=True)):
        if not _same(a, e):
            out.append(f"findings[{j}] 실제 {_show(a)} != 기대 {_show(e)}")
    return out


def _same(a: Finding, e: Finding) -> bool:
    plain = ("rule_id", "rule_version", "severity", "world_ids", "zone_id", "needs_check", "reasons")
    if any(getattr(a, k) != getattr(e, k) for k in plain):
        return False
    if a.metrics.keys() != e.metrics.keys():
        return False
    return all(math.isclose(a.metrics[k], e.metrics[k], rel_tol=0.0, abs_tol=METRIC_ABS_TOL) for k in a.metrics)


def _show(f: Finding) -> dict[str, Any]:
    return {
        "rule_id": f.rule_id,
        "rule_version": f.rule_version,
        "severity": str(f.severity),
        "world_ids": list(f.world_ids),
        "zone_id": f.zone_id,
        "needs_check": f.needs_check,
        "reasons": list(f.reasons),
        "metrics": dict(f.metrics),
    }


if __name__ == "__main__":
    raise SystemExit(main())
