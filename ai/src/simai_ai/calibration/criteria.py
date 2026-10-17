"""CALIBRATED 판정 (명세 4.4 CALIBRATED 기준, 4.3 스케일 근거, 20.4 규칙 4. 기획서 수정 6 기준).

아래를 모두 만족하면 CALIBRATED, 하나라도 미달이면 PROVISIONAL 이다. 값을 모르면 미달로 본다.
기준값과 출처 집합은 `providers.contract` 의 `CalibratedCriteria`·`CALIBRATED_SOURCES` 등을 그대로 쓴다.
문서 검사(`check_calibration_state`)와 판정이 같은 값을 보게 하기 위함이다.

| 항목              | 기준(초기값, `CalibratedCriteria`)                                       |
| 출처              | G1_AUTO 또는 CHARUCO. DATASET_GT·SIM·MODEL_ESTIMATE 는 값과 관계없이 불가 |
| 카메라 집합        | 설치 카메라 전체(기본 4대), camera_id 중복 없음                            |
| 재투영 오차        | 카메라마다 중앙값 2px·p95 5px 이하                                        |
| 바닥 커버리지      | 사람 관측이 바닥 면적의 80% 이상                                          |
| 바닥 평면 차이     | 보정의 카메라 전체를 비교한 바닥 평면 차이 3cm 이하                         |
| 스케일 주 근거     | 서로 다른 실측 정물 2개 이상이 서로 일치, 또는 사람 20명 이상의 키 통계      |
| 스케일 교차 확인   | 주 근거와 다른 근거 하나가 주 근거와 일치                                  |
| 스케일 반영        | 주 근거가 말하는 배율이 1 에 가까움(근거가 보정에 실제로 반영됨)            |
| 독립 검산점        | 보정에 쓰지 않은 서로 다른 지점 3곳 이상                                   |
| 머리 높이 오차     | 검산점마다 15cm 이내 (표본이 적어 p95 가 아니라 최대값)                    |
| 카메라 쌍별 불일치 | 검산점마다 15cm 이내 (최대값)                                             |

MANUAL_PNP(평면도 점 PnP)는 수정 6 에서 대체 경로에서 빠졌으므로 CALIBRATED 출처로 인정하지 않는다.
스케일 반영 항목은 명세 조건의 전제를 확인하려고 둔 것이다. 근거끼리 일치해도 보정 배율이 그와 다르면 미달이다.

스케일 근거 하나(`ScaleEstimate.scale`)는 그 근거가 말하는 "실제 길이 / 보정 World 길이" 배율이다.
1.0 이면 보정 스케일이 그 근거와 맞는다. 실측 정물은 label(정물 목록의 이름)로 정물을 구분하고, 같은 정물을 여러 번
잰 값은 평균해 한 정물로 센다. 사람 키 통계는 사람 수를 sample_count 로 준다.
검산점은 point_id 로 지점을 구분한다. 같은 지점을 여러 번 관측하면 한 곳으로 세고 오차는 그 관측 중 가장 나쁜 값을
쓴다. 한 번이라도 보정에 쓴 지점은 독립 검산점이 아니다.

NaN·무한대·음수(비율은 1 초과도) 같은 유효하지 않은 측정값은 대소 비교가 뜻을 잃으므로 미달로 보고, 모르는 값과
구분해 `CalibrationJudgement.invalid_inputs` 로 알린다. 버전 저장소는 이런 후보를 계약 위반으로 거절한다.

판정은 현재 시각을 읽지 않는다. 결과의 validation 은 `calibration.schema.json` 의 validation 필드로 그대로 쓴다
(유효한 값만 넣는다). 현장에서는 절대 위치를 실측하지 않으므로 `max_position_error_m`(절대 위치 오차)은 채우지 않는다.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from simai_ai.providers.contract import (
    CALIBRATED_SOURCES,
    CROSS_CHECK_SCALE_BASES,
    DEFAULT_CALIBRATED_CRITERIA,
    NEVER_CALIBRATED_SOURCES,
    CalibratedCriteria,
    is_finite_number,
)
from simai_ai.tracking.system_health import CalibrationState


class ScaleBasis(StrEnum):
    """스케일 근거 (contracts calibration.schema.json scale_basis, 명세 4.3)."""

    MEASURED_OBJECT = "MEASURED_OBJECT"
    KNOWN_PERSON_HEIGHT = "KNOWN_PERSON_HEIGHT"
    HUMAN_HEIGHT_STATS = "HUMAN_HEIGHT_STATS"
    STANDARD_OBJECT = "STANDARD_OBJECT"
    MODEL_ESTIMATE = "MODEL_ESTIMATE"
    MARKER = "MARKER"
    DATASET_GT = "DATASET_GT"
    FLOOR_PLAN = "FLOOR_PLAN"  # 수정 6 이후 쓰지 않는다. 이전 문서 호환용


# 명세 4.3 우선순위(앞이 높다). MARKER(ChArUco 보드)는 우선순위 목록에 없어 뒤에 둔다.
SCALE_PRIORITY: tuple[ScaleBasis, ...] = (
    ScaleBasis.MEASURED_OBJECT,
    ScaleBasis.KNOWN_PERSON_HEIGHT,
    ScaleBasis.HUMAN_HEIGHT_STATS,
    ScaleBasis.STANDARD_OBJECT,
    ScaleBasis.MODEL_ESTIMATE,
    ScaleBasis.MARKER,
)
CROSS_CHECK_BASES = frozenset(ScaleBasis(b) for b in CROSS_CHECK_SCALE_BASES)


class Criterion(StrEnum):
    """CALIBRATED 판정 항목. 미달 사유의 분류다."""

    SOURCE = "SOURCE"
    CAMERA_SET = "CAMERA_SET"
    REPROJECTION_MEDIAN = "REPROJECTION_MEDIAN"
    REPROJECTION_P95 = "REPROJECTION_P95"
    FLOOR_COVERAGE = "FLOOR_COVERAGE"
    FLOOR_PLANE = "FLOOR_PLANE"
    SCALE_PRIMARY = "SCALE_PRIMARY"
    SCALE_CROSS_CHECK = "SCALE_CROSS_CHECK"
    SCALE_RESIDUAL = "SCALE_RESIDUAL"
    CHECKPOINT_COUNT = "CHECKPOINT_COUNT"
    HEAD_HEIGHT = "HEAD_HEIGHT"
    PAIRWISE_POSITION = "PAIRWISE_POSITION"


@dataclass(frozen=True)
class ScaleEstimate:
    """스케일 근거 한 건. scale = 실제 길이 / 보정 World 길이.

    label 은 기록용 이름이다. 실측 정물(MEASURED_OBJECT)은 정물 목록의 이름이 정물 식별자이므로 반드시 준다.
    """

    basis: ScaleBasis
    scale: float
    sample_count: int = 1
    label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "basis", ScaleBasis(self.basis))
        if not is_finite_number(self.scale) or not self.scale > 0:
            raise ValueError(f"스케일 배율은 유한한 양수여야 한다: {self.scale}")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 1:
            raise ValueError(f"sample_count 는 1 이상의 정수여야 한다: {self.sample_count}")
        if self.basis is ScaleBasis.MEASURED_OBJECT and not self.label:
            raise ValueError("실측 정물 근거는 정물을 구분하는 label(정물 목록의 이름)이 있어야 한다")


@dataclass(frozen=True)
class HeightCheckpoint:
    """키를 아는 사람이 선 검산점 한 곳의 관측 한 건 (명세 4.4 설치 직후 빠른 검산).

    point_id: 지점 식별자. 같은 지점을 여러 번 관측하면 같은 값을 준다.
    measured_head_height_m: 보정으로 삼각측량한 머리 높이(World Z). 모르면 None.
    pairwise_position_disagreement_m: 카메라 쌍마다 구한 위치끼리의 최대 차이. 모르면 None.
    used_in_calibration: 보정에 쓴 지점이면 True. 독립 검산점으로 세지 않는다.
    """

    point_id: str
    known_head_height_m: float
    measured_head_height_m: float | None
    pairwise_position_disagreement_m: float | None
    used_in_calibration: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.point_id, str) or not self.point_id:
            raise ValueError(f"검산점 point_id 가 비었다: {self.point_id!r}")
        if not is_finite_number(self.known_head_height_m) or not self.known_head_height_m > 0:
            raise ValueError(f"실제 키(known_head_height_m)는 유한한 양수여야 한다: {self.known_head_height_m}")

    @property
    def head_height_error_m(self) -> float | None:
        """|측정 - 실제| 를 마이크로미터 단위로 반올림한다(뺄셈의 부동소수 잔차가 기준 비교·기록에 남지 않게).

        측정값이 유효하지 않으면(NaN·무한대) 그 값을 그대로 돌려 판정이 유효하지 않은 입력으로 보게 한다.
        """
        measured = self.measured_head_height_m
        if measured is None:
            return None
        if not is_finite_number(measured):
            return measured
        return round(abs(measured - self.known_head_height_m), 6)


@dataclass(frozen=True)
class CalibrationEvidence:
    """CALIBRATED 판정 입력 중 카메라 보정 값 밖의 것. 모르는 값은 None·빈 값으로 둔다.

    floor_plane_camera_ids: floor_plane_disagreement_m 을 구할 때 비교한 카메라. 보정의 카메라 전체와 같아야 한다.
    """

    floor_coverage_ratio: float | None = None
    floor_plane_disagreement_m: float | None = None
    floor_plane_camera_ids: tuple[str, ...] = ()
    scale_estimates: tuple[ScaleEstimate, ...] = ()
    checkpoints: tuple[HeightCheckpoint, ...] = ()


@dataclass(frozen=True)
class Failure:
    """미달 사유 하나. invalid 는 값을 모르는 것이 아니라 유효하지 않은 값(NaN·음수 등)이 들어왔다는 뜻이다."""

    criterion: Criterion
    message: str
    invalid: bool = False


@dataclass(frozen=True)
class ScaleDecision:
    """스케일 근거 판정 결과. primary 가 None 이면 주 근거 조건을 만족한 근거가 없다.

    cross_checks 는 주 근거와 일치한 다른 근거(우선순위 순, 중복 없음)이며 문서의 scale_cross_check_basis 가 된다.
    """

    primary: ScaleBasis | None
    primary_scale: float | None
    cross_checks: tuple[ScaleBasis, ...]
    recorded_basis: ScaleBasis | None


@dataclass(frozen=True)
class CalibrationJudgement:
    """판정 결과. state 는 CALIBRATED 또는 PROVISIONAL 이다(UNCALIBRATED 는 추정 전 상태라 판정하지 않는다)."""

    state: CalibrationState
    failures: tuple[Failure, ...]
    scale: ScaleDecision
    validation: dict[str, Any] = field(default_factory=dict)

    @property
    def failed_criteria(self) -> frozenset[Criterion]:
        return frozenset(f.criterion for f in self.failures)

    @property
    def invalid_inputs(self) -> tuple[str, ...]:
        """유효하지 않은 입력에 대한 미달 사유. 비어 있지 않으면 입력을 만든 쪽의 결함이다."""
        return tuple(f.message for f in self.failures if f.invalid)


def judge_calibration(
    provider_source: str,
    cameras: Sequence[Mapping[str, Any]],
    evidence: CalibrationEvidence,
    criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA,
    expected_camera_ids: Collection[str] | None = None,
) -> CalibrationJudgement:
    """후보 보정 하나를 판정한다. cameras 는 계약의 camera_calibration 형식(camera_id·reprojection_error_px 를 읽는다).

    expected_camera_ids 는 설치된 카메라 목록이다. 주면 후보의 카메라가 정확히 그 목록이어야 하고, 주지 않으면
    서로 다른 카메라가 criteria.min_cameras 대 이상이어야 한다.
    미달 항목을 모두 모아 돌려준다. 출처가 CALIBRATED 불가여도 나머지 항목을 끝까지 본다.
    """
    c = criteria
    failures: list[Failure] = []

    if not isinstance(provider_source, str):
        # 리스트·딕셔너리처럼 해시할 수 없는 값이 집합 비교에서 TypeError 를 내지 않게 먼저 거른다
        failures.append(
            Failure(Criterion.SOURCE, f"provider_source 는 문자열이어야 한다: {provider_source!r}", invalid=True)
        )
    elif provider_source in NEVER_CALIBRATED_SOURCES:
        failures.append(
            Failure(
                Criterion.SOURCE, f"{provider_source} 는 현장의 검증된 보정이 아니므로 CALIBRATED 불가 (20.4 규칙 4)"
            )
        )
    elif provider_source not in CALIBRATED_SOURCES:
        failures.append(
            Failure(Criterion.SOURCE, f"{provider_source} 는 CALIBRATED 출처가 아니다 (G1_AUTO·CHARUCO 만, 수정 6)")
        )

    camera_ids = [cam.get("camera_id") for cam in cameras]
    failures += _check_camera_set(camera_ids, expected_camera_ids, c)
    failures += _check_reprojection(cameras, c)

    coverage = _at_least(
        failures,
        Criterion.FLOOR_COVERAGE,
        "바닥 커버리지",
        evidence.floor_coverage_ratio,
        c.min_floor_coverage_ratio,
        upper=1.0,
    )
    floor_plane = _at_most(
        failures,
        Criterion.FLOOR_PLANE,
        "바닥 평면 차이(m)",
        evidence.floor_plane_disagreement_m,
        c.max_floor_plane_disagreement_m,
    )
    failures += _check_floor_plane_cameras(camera_ids, evidence.floor_plane_camera_ids)

    scale, scale_failures = decide_scale(evidence.scale_estimates, c)
    failures += scale_failures

    by_point: dict[str, list[HeightCheckpoint]] = {}
    for p in evidence.checkpoints:
        by_point.setdefault(p.point_id, []).append(p)
    independent = [recs for recs in by_point.values() if not any(r.used_in_calibration for r in recs)]
    if len(independent) < c.min_checkpoints:
        failures.append(
            Failure(
                Criterion.CHECKPOINT_COUNT,
                f"보정에 쓰지 않은 서로 다른 검산점 {len(independent)}곳 < {c.min_checkpoints}곳",
            )
        )
    records = [r for recs in independent for r in recs]
    head_max = _max_at_most(
        failures,
        Criterion.HEAD_HEIGHT,
        "머리 높이 오차 최대(m)",
        [r.head_height_error_m for r in records],
        c.max_head_height_error_m,
    )
    pair_max = _max_at_most(
        failures,
        Criterion.PAIRWISE_POSITION,
        "카메라 쌍별 위치 불일치 최대(m)",
        [r.pairwise_position_disagreement_m for r in records],
        c.max_pairwise_position_disagreement_m,
    )

    validation: dict[str, Any] = {"checkpoint_count": len(independent)}
    if pair_max is not None:
        validation["max_pairwise_position_disagreement_m"] = pair_max
    if head_max is not None:
        validation["max_head_height_error_m"] = head_max
    if floor_plane is not None:
        validation["floor_plane_disagreement_m"] = floor_plane
    if coverage is not None:
        validation["floor_coverage_ratio"] = coverage

    state = CalibrationState.PROVISIONAL if failures else CalibrationState.CALIBRATED
    return CalibrationJudgement(state=state, failures=tuple(failures), scale=scale, validation=validation)


def decide_scale(
    estimates: Iterable[ScaleEstimate], criteria: CalibratedCriteria = DEFAULT_CALIBRATED_CRITERIA
) -> tuple[ScaleDecision, list[Failure]]:
    """스케일 주 근거·교차 확인·반영을 판정한다 (명세 4.3, 4.4).

    주 근거는 실측 정물(서로 다른 정물 2개 이상 일치)을 먼저, 없으면 사람 키 통계(20명 이상)를 본다.
    recorded_basis 는 보정 문서의 scale_basis 로 쓸 값이다: 주 근거, 없으면 받은 근거 중 우선순위가 가장 높은 것.
    """
    c = criteria
    estimates = tuple(estimates)
    failures: list[Failure] = []

    primary: ScaleBasis | None = None
    primary_scale: float | None = None
    by_object: dict[str, list[float]] = {}
    for e in estimates:
        if e.basis is ScaleBasis.MEASURED_OBJECT:
            by_object.setdefault(str(e.label), []).append(e.scale)
    objects = [sum(v) / len(v) for v in by_object.values()]  # 같은 정물을 여러 번 잰 값은 평균해 한 정물로 센다
    if len(objects) >= c.min_measured_objects:
        mean = sum(objects) / len(objects)
        spread = max(abs(s / mean - 1.0) for s in objects)
        if spread <= c.measured_object_agreement:
            primary, primary_scale = ScaleBasis.MEASURED_OBJECT, mean
        else:
            failures.append(
                Failure(
                    Criterion.SCALE_PRIMARY,
                    f"서로 다른 실측 정물 {len(objects)}개의 배율이 서로 {spread:.1%} 어긋난다 "
                    f"(> {c.measured_object_agreement:.1%})",
                )
            )
    if primary is None:
        stats = [e for e in estimates if e.basis is ScaleBasis.HUMAN_HEIGHT_STATS]
        best = max(stats, key=lambda e: e.sample_count, default=None)
        if best is not None and best.sample_count >= c.min_height_stats_people:
            primary, primary_scale = ScaleBasis.HUMAN_HEIGHT_STATS, best.scale
            failures = []  # 정물 불일치는 사람 키 통계가 주 근거가 되면 미달이 아니다
    if primary is None:
        failures.append(
            Failure(
                Criterion.SCALE_PRIMARY,
                f"스케일 주 근거 없음: 서로 다른 실측 정물 {c.min_measured_objects}개 이상 일치 또는 "
                f"사람 {c.min_height_stats_people}명 이상의 키 통계가 필요하다 (정물 {len(objects)}개)",
            )
        )

    agreeing: set[ScaleBasis] = set()
    if primary is not None and primary_scale is not None:
        for e in estimates:
            if e.basis is primary or e.basis not in CROSS_CHECK_BASES:
                continue
            if abs(e.scale / primary_scale - 1.0) <= c.cross_check_tolerance:
                agreeing.add(e.basis)
        if not agreeing:
            failures.append(
                Failure(
                    Criterion.SCALE_CROSS_CHECK,
                    f"주 근거 {primary} 와 {c.cross_check_tolerance:.0%} 안에서 일치하는 다른 근거가 없다",
                )
            )
        residual = abs(primary_scale - 1.0)
        if residual > c.max_scale_residual:
            failures.append(
                Failure(
                    Criterion.SCALE_RESIDUAL,
                    f"주 근거 배율 {primary_scale:.4f} 가 보정 스케일과 {residual:.1%} 다르다 "
                    f"(> {c.max_scale_residual:.1%})",
                )
            )

    recorded = primary
    if recorded is None:
        for e in estimates:
            if e.basis in CROSS_CHECK_BASES:
                recorded = _higher(recorded, e.basis)
    cross_checks = tuple(b for b in SCALE_PRIORITY if b in agreeing)
    return ScaleDecision(primary, primary_scale, cross_checks, recorded), failures


def _higher(current: ScaleBasis | None, candidate: ScaleBasis) -> ScaleBasis:
    if current is None or SCALE_PRIORITY.index(candidate) < SCALE_PRIORITY.index(current):
        return candidate
    return current


def _check_camera_set(
    camera_ids: Sequence[Any], expected: Collection[str] | None, c: CalibratedCriteria
) -> list[Failure]:
    """설치 카메라 전체가 한 번씩 있는지 본다(명세 4.4 "4대의 바닥 평면 차이"). 빠진 카메라는 검증되지 않았다."""
    out: list[Failure] = []
    duplicated = sorted(str(i) for i, n in Counter(camera_ids).items() if n > 1)
    if duplicated:
        out.append(Failure(Criterion.CAMERA_SET, f"camera_id 중복: {duplicated}", invalid=True))
    present = set(camera_ids)
    if expected is not None:
        missing, foreign = set(expected) - present, present - set(expected)
        if missing:
            out.append(Failure(Criterion.CAMERA_SET, f"설치 카메라 {sorted(missing)} 의 보정이 없다"))
        if foreign:
            out.append(Failure(Criterion.CAMERA_SET, f"설치 목록에 없는 카메라 {sorted(map(str, foreign))}"))
    elif len(present) < c.min_cameras:
        out.append(Failure(Criterion.CAMERA_SET, f"서로 다른 카메라 {len(present)}대 < {c.min_cameras}대"))
    return out


def _check_floor_plane_cameras(camera_ids: Sequence[Any], floor_ids: Sequence[str]) -> list[Failure]:
    if not floor_ids:
        return [Failure(Criterion.FLOOR_PLANE, "바닥 평면 차이를 어느 카메라로 구했는지 모른다")]
    missing, foreign = set(camera_ids) - set(floor_ids), set(floor_ids) - set(camera_ids)
    if missing or foreign:
        return [
            Failure(
                Criterion.FLOOR_PLANE,
                f"바닥 평면 차이가 보정의 카메라 전체를 비교한 값이 아니다 (빠짐 {sorted(map(str, missing))}, "
                f"보정에 없음 {sorted(foreign)})",
            )
        ]
    return []


def _check_reprojection(cameras: Sequence[Mapping[str, Any]], c: CalibratedCriteria) -> list[Failure]:
    if not cameras:
        return [Failure(Criterion.REPROJECTION_MEDIAN, "카메라가 없다")]
    out: list[Failure] = []
    for cam in cameras:
        rep = cam.get("reprojection_error_px") or {}
        name = cam.get("camera_id")
        median, p95 = rep.get("median"), rep.get("p95")
        _at_most(
            out, Criterion.REPROJECTION_MEDIAN, f"{name} 재투영 오차 중앙값(px)", median, c.max_reprojection_median_px
        )
        _at_most(out, Criterion.REPROJECTION_P95, f"{name} 재투영 오차 p95(px)", p95, c.max_reprojection_p95_px)
    return out


def _valid(
    out: list[Failure], criterion: Criterion, name: str, value: Any, upper: float | None = None
) -> float | None:
    """value 가 유한한 0 이상의 수(upper 가 있으면 그 이하)면 그대로, 모르거나 유효하지 않으면 미달을 남기고 None."""
    if value is None:
        out.append(Failure(criterion, f"{name} 을 모른다"))
        return None
    if not is_finite_number(value) or value < 0 or (upper is not None and value > upper):
        bound = "" if upper is None else f" {upper} 이하"
        out.append(Failure(criterion, f"{name} = {value} 는 유효한 측정값이 아니다 (유한한 0 이상{bound})", True))
        return None
    return value


def _max_at_most(
    out: list[Failure], criterion: Criterion, name: str, values: Sequence[float | None], limit: float
) -> float | None:
    """관측값의 최대가 limit 이하인지 본다. 관측이 없거나 하나라도 모르면 최대값을 알 수 없어 미달이다."""
    if not values or any(v is None for v in values):
        return _at_most(out, criterion, name, None, limit)
    worst: float | None = None
    for v in values:
        checked = _valid(out, criterion, name, v)
        if checked is None:
            return None
        worst = checked if worst is None else max(worst, checked)
    return _at_most(out, criterion, name, worst, limit)


def _at_most(out: list[Failure], criterion: Criterion, name: str, value: Any, limit: float) -> float | None:
    """상한 검사. 유효한 값이면 (기준을 넘어도) 기록용으로 돌려준다."""
    checked = _valid(out, criterion, name, value)
    if checked is not None and checked > limit:
        out.append(Failure(criterion, f"{name} = {checked} > {limit}"))
    return checked


def _at_least(
    out: list[Failure], criterion: Criterion, name: str, value: Any, limit: float, upper: float | None = None
) -> float | None:
    checked = _valid(out, criterion, name, value, upper)
    if checked is not None and checked < limit:
        out.append(Failure(criterion, f"{name} = {checked} < {limit}"))
    return checked
