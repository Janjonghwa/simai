"""Risk Engine 골격 (명세 3.2 Risk Engine 배치, 7장).

같은 라이브러리를 RunPod Tier 0(LIVE)와 EC2 옆 Python 컨테이너(PLANNED·REPLAY·WHAT_IF)에 배포한다.
엔진은 주입 시계로 판정 시각을 정하고, 판정 시각에 유효한 구역만 골라 규칙을 돌린 뒤 결과를 정렬해
Evaluation 으로 돌려준다.

LIVE 만 실제 알림 대상이라는 불변 규칙(AGENTS.md 규칙 5, 명세 7.4·11.4-3)은 두 겹으로 드러낸다.
1. Evaluation.alert_candidates() 는 출처가 LIVE 가 아니면 빈 튜플이다.
2. AlertCandidate 는 출처가 LIVE 가 아니면 만들 수 없다(NonLiveAlertError).
알림 채널로 보내는 코드는 AlertCandidate 나 alerting 의 현장 경보 신호(같은 LIVE 검사)만 받는다.
이벤트 생명주기는 lifecycle 이 맡고, 저널·outbox 는 이 패키지 밖(저장·전송 이슈)이다.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from simai_ai.risk._parse import RiskInputError
from simai_ai.risk.clock import Clock
from simai_ai.risk.common import Origin
from simai_ai.risk.monitoring import MonitoringState
from simai_ai.risk.rule import Finding, Rule, RuleContext, RuleSpec, RuleState, ZoneClearEvidence
from simai_ai.risk.work_surface import WorkSurface, check_unique_ids
from simai_ai.risk.world import WorldState
from simai_ai.risk.zone import Zone

# 카메라 영상에서 온 입력. PLANNED·WHAT_IF 는 계획·가상 입력이라 카메라 감시 상태라는 개념이 없다.
_CAMERA_ORIGINS = frozenset({Origin.LIVE, Origin.REPLAY})


class NonLiveAlertError(ValueError):
    """LIVE 가 아닌 판정을 실제 알림 대상으로 만들려 했다."""


class RuleContractError(RuntimeError):
    """규칙이 공통 형식을 어겼다(자기 ID·버전이 아닌 Finding 등)."""


@dataclass(frozen=True)
class AlertCandidate:
    """실제 알림 채널로 보낼 수 있는 판정. 출처가 LIVE 일 때만 존재한다."""

    origin: Origin
    finding: Finding
    observation_time: int
    evaluated_at: int
    calibration_version: str
    scene_version: str

    def __post_init__(self) -> None:
        if not Origin(self.origin).alertable:
            raise NonLiveAlertError(f"{self.origin} 판정은 실제 알림 대상이 아니다(LIVE 만 알린다)")


@dataclass(frozen=True)
class Evaluation:
    """한 번의 판정 결과.

    - evaluated_at: 주입 시계의 판정 시각. observation_time: 입력 World 상태의 t_latest_global.
    - findings: (rule_id, rule_version, zone_id, world_ids) 순으로 정렬. 같은 입력이면 순서까지 같다.
    - zone_clear_evidence: (rule_id, zone_id) 쌍. 그 규칙이 이 판정의 유효 입력으로 그 구역에 조건이 없다고 확인했다
      (rule.ZoneClearEvidence, R7: 정상 관측 카메라 확보). 판정 시각에 유효한 구역만 들어가고, 같은 규칙이 같은 구역에
      판정을 냈으면 넣지 않는다. lifecycle 이 복원한 구역 대상 이벤트의 해소 근거로 쓴다(S15P31A101-174). 직접 만든
      Evaluation 은 기본으로 비어 있다(근거 없음).
    """

    origin: Origin
    evaluated_at: int
    observation_time: int
    calibration_version: str
    scene_version: str
    rules: tuple[RuleSpec, ...]
    zones: tuple[Zone, ...]
    findings: tuple[Finding, ...]
    zone_clear_evidence: frozenset[tuple[str, str]] = frozenset()

    @property
    def alertable(self) -> bool:
        return self.origin.alertable

    def alert_candidates(self) -> tuple[AlertCandidate, ...]:
        if not self.alertable:
            return ()
        return tuple(
            AlertCandidate(
                origin=self.origin,
                finding=f,
                observation_time=self.observation_time,
                evaluated_at=self.evaluated_at,
                calibration_version=self.calibration_version,
                scene_version=self.scene_version,
            )
            for f in self.findings
        )


class RiskEngine:
    def __init__(self, rules: Iterable[Rule], clock: Clock, *, state: RuleState | None = None) -> None:
        """state: 판정 사이 규칙 기억(R4). 없으면 이 엔진만의 새 RuleState 다. 출처마다 칸을 나눠 규칙에 넘긴다.
        구성 묶음이 바뀌어도 기억을 이어야 하면(ConfigSync) 같은 RuleState 를 새 엔진에 넘긴다."""
        self._rules = tuple(sorted(rules, key=lambda r: r.spec.key))
        ids = [r.spec.rule_id for r in self._rules]
        duplicated = sorted({i for i in ids if ids.count(i) > 1})
        if duplicated:
            # 한 엔진에는 규칙 ID 당 한 버전만 둔다(이벤트의 규칙 버전이 하나로 정해져야 한다).
            raise RuleContractError(f"같은 규칙 ID 가 여러 번 들어왔다 {duplicated}")
        self._clock = clock
        self._state = RuleState() if state is None else state

    @property
    def state(self) -> RuleState:
        return self._state

    @property
    def rules(self) -> tuple[RuleSpec, ...]:
        return tuple(r.spec for r in self._rules)

    def evaluate(
        self,
        world: WorldState,
        zones: Iterable[Zone],
        origin: Origin,
        *,
        monitoring: MonitoringState | None = None,
        work_surfaces: Iterable[WorkSurface] = (),
    ) -> Evaluation:
        """monitoring 은 같은 판정 시각의 감시 상태(R7 입력)다. 없으면 None 으로 규칙에 넘긴다.

        work_surfaces 는 활성 구성 묶음의 등록 작업면(명세 7.5, R2·R3·R5 입력)이다. ID 순으로 정렬해 넘기고, ID 가 겹치면
        어느 것이 정본인지 모르므로 RuleContractError 다.

        PLANNED·WHAT_IF 는 카메라가 없는 입력이라 monitoring_expected=False 로 넘긴다. 그때 monitoring 이 없으면
        R7 은 판정하지 않는다(가상 카메라 장애를 보려면 monitoring 을 넘긴다). LIVE·REPLAY 에서 없으면 '모름'(저하)이다.
        """
        origin = Origin(origin)
        now_ms = self._clock.now_ms()
        # 구역은 판정 시각에 유효한 버전만 쓴다. REPLAY·WHAT_IF 는 고정 시계로 그 시각을 준다.
        active = tuple(sorted((z for z in zones if z.is_active(now_ms)), key=lambda z: (z.zone_id, z.zone_version)))
        active_ids = [z.zone_id for z in active]
        if len(set(active_ids)) != len(active_ids):
            raise RuleContractError(f"같은 시각에 유효한 구역 버전이 둘 이상이다 {sorted(active_ids)}")
        surfaces = tuple(sorted(work_surfaces, key=lambda s: s.surface_id))
        try:
            check_unique_ids(surfaces, "work_surfaces")
        except RiskInputError as e:
            raise RuleContractError(str(e)) from None
        ctx = RuleContext(
            now_ms=now_ms,
            world=world,
            zones=active,
            monitoring=monitoring,
            monitoring_expected=origin in _CAMERA_ORIGINS,
            work_surfaces=surfaces,
            state=self._state.view(origin),
        )

        findings: list[Finding] = []
        cleared: set[tuple[str, str]] = set()
        active_set = set(active_ids)
        for rule in self._rules:
            spec = rule.spec
            judged: set[str | None] = set()
            for finding in rule.evaluate(ctx):
                if (finding.rule_id, finding.rule_version) != spec.key:
                    raise RuleContractError(
                        f"규칙 {spec.rule_id} v{spec.rule_version} 가 "
                        f"{finding.rule_id} v{finding.rule_version} 판정을 냈다"
                    )
                findings.append(finding)
                judged.add(finding.zone_id)
            if isinstance(rule, ZoneClearEvidence):
                # 조건이 없다는 근거는 판정 시각에 유효한 구역이고 같은 규칙이 판정을 내지 않은 구역만 받는다(판정이 이긴다).
                cleared |= {
                    (spec.rule_id, zone_id)
                    for zone_id in rule.clear_zones(ctx)
                    if zone_id in active_set and zone_id not in judged
                }

        return Evaluation(
            origin=origin,
            evaluated_at=now_ms,
            observation_time=world.t_latest_global,
            calibration_version=world.calibration_version,
            scene_version=world.scene_version,
            rules=self.rules,
            zones=active,
            findings=tuple(sorted(findings, key=lambda f: f.sort_key)),
            zone_clear_evidence=frozenset(cleared),
        )
