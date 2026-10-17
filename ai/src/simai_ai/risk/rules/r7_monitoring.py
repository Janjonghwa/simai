"""R7 구역 감시 능력 저하 (명세 7.2 R7, 6.2, 6.5, 15.1 G3).

카메라 장애·시간 동기화 무효로 어떤 구역이 '2대 이상 관측' 조건을 잃으면 그 구역에 구역 대상 판정
(world_ids 없음, zone_id 있음)을 낸다. lifecycle 은 이것을 (R7, 구역) 키 이벤트로 만들고, 조건이 돌아와
판정이 사라진 뒤 해소 유지 시간이 지나면 해소한다. 사람이 없어도 나는 시스템 판정이며 가짜 World ID 를 만들지 않는다.

- 심각도: CAUTION(시스템, 7.2). 위험이 아니라 시스템 상태 안내라 현장 소리·반장 PWA 로 내지 않는다(위험 경보로
  오인하지 않게). 채널은 alerting.channels_for(7.3 표)로만 정하며 CAUTION 은 관제 화면 안내다. 그래서 현장 경보
  신호(LocalAlert·AlertClear)가 없다. LIVE 가 아니면 채널이 없다(규칙 5). 저하 구역 안 사람의 위험은 각 규칙과
  R6 이 '확인 필요'로 다룬다.
- 정상 카메라: 상태 OK, 받은 프레임이 있고, t_latest_global - last_capture ≤ camera_stall_ms.
  System Health(tracking/system_health)의 수신 중 판단(동기화 유효 + 같은 경과 기준)과 같은 기준에 상태 검사를
  더한 것이다. camera_stall_ms·min_cameras 초기값도 System Health(TimeModelConfig)와 같다(500ms, 2대).
- PLANNED·WHAT_IF 처럼 카메라 개념이 없는 입력(RuleContext.monitoring_expected=False)에서 감시 상태가 없으면
  판정하지 않는다. 계획·What-if 보고서에 의미 없는 R7 이벤트가 구역마다 생기지 않게 한다. 감시 상태를 넘기면
  (가상 카메라 장애 시나리오) 같은 기준으로 판정한다.
- 모르면 저하로 본다. LIVE·REPLAY 에서 감시 상태 입력이 없거나(monitoring None), 구역의 관측 카메라 목록이 없거나, 목록의
  카메라가 상태 보고에 없으면 그 구역·카메라를 정상으로 세지 않는다(명세 6장 머리말).
- 카메라 경과는 t_latest_global 기준이라 영상 전체가 멈추면(모든 카메라가 함께 멈춤) 카메라끼리의 차이가 없다.
  이 경우는 lag 로 잡히며 System Health(UNSAFE_UNKNOWN)가 맡는다. 그동안 활성 R7 이벤트는 lifecycle 검증 정책
  (lag 초과 → UNVERIFIABLE)으로 해소되지 않는다.
- 지연: 카메라가 끊기면 상태가 NO_SIGNAL 로 오는 즉시, 늦어도 마지막 프레임 뒤 camera_stall_ms 가 지나면 판정한다.
  lifecycle 의 발생 지속 조건(6.4, 3프레임 중 2)은 R7 에 기본으로 적용하지 않으므로(LifecyclePolicy.persistence_by_rule
  기본값 R7 즉시, S15P31A101-156) 그 판정 주기에 이벤트가 생긴다. 카메라 장애는 순간 오검출이 아니라 상태 보고와
  camera_stall_ms 로 이미 걸러진 판정이다. G3 '카메라 분리 → 구역 저하 표시 1초 이내'의 규칙 쪽 몫은
  camera_stall_ms + 판정 주기다.

근거(reasons)는 첫 값이 monitoring_degraded 이고 뒤에 원인이 붙는다(coverage_insufficient, 그다음 카메라 ID 순).
| 근거 | 뜻 |
| monitoring_unknown | 감시 상태 입력 없음(LIVE·REPLAY) |
| coverage_unknown | 이 구역의 관측 카메라 목록 없음 |
| coverage_insufficient | 모든 카메라가 정상이어도 관측 카메라가 min_cameras 보다 적음(설치 문제) |
| camera_no_signal:<id> / camera_waiting_keyframe:<id> / camera_sync_invalid:<id> | 카메라 상태 |
| camera_stalled:<id> | 상태는 OK 지만 마지막 프레임이 camera_stall_ms 보다 오래됨 |
| camera_no_frame:<id> | 상태는 OK 지만 처리한 프레임이 없음 |
| camera_unreported:<id> | 관측 카메라 목록에 있으나 상태 보고에 없음 |
수치(metrics): healthy_cameras(정상 관측 카메라 수), required_cameras, covering_cameras(목록을 알 때만).
"""

from collections.abc import Iterator

from simai_ai.risk._parse import as_int
from simai_ai.risk.common import Severity
from simai_ai.risk.monitoring import CameraMonitor, CameraStatus, MonitoringState
from simai_ai.risk.rule import Finding, RuleContext, RuleSpec
from simai_ai.risk.zone import Zone

R7_RULE_ID = "R7"
R7_RULE_VERSION = 1

REASON_DEGRADED = "monitoring_degraded"


class ZoneMonitoringRule:
    """R7 v1. 구역마다 정상 관측 카메라가 min_cameras 보다 적으면 구역 대상 CAUTION(시스템) 판정을 낸다."""

    def __init__(self, *, min_cameras: int = 2, camera_stall_ms: int = 500) -> None:
        self._min_cameras = as_int(min_cameras, "R7.min_cameras", minimum=1)
        self._camera_stall_ms = as_int(camera_stall_ms, "R7.camera_stall_ms", minimum=0)
        self._spec = RuleSpec(
            rule_id=R7_RULE_ID,
            rule_version=R7_RULE_VERSION,
            reads=("zones", "t_latest_global", "monitoring.cameras", "monitoring.zone_cameras"),
        )

    @property
    def spec(self) -> RuleSpec:
        return self._spec

    def evaluate(self, ctx: RuleContext) -> Iterator[Finding]:
        if ctx.monitoring is None and not ctx.monitoring_expected:
            return  # 카메라 개념이 없는 입력(PLANNED·WHAT_IF): 판정하지 않는다
        for zone in ctx.zones:
            finding = self._judge(zone, ctx.monitoring, ctx.world.t_latest_global)
            if finding is not None:
                yield finding

    def _judge(self, zone: Zone, monitoring: MonitoringState | None, t_latest_global: int) -> Finding | None:
        required = float(self._min_cameras)
        if monitoring is None:
            return self._finding(zone, ("monitoring_unknown",), {"healthy_cameras": 0.0, "required_cameras": required})
        covering = monitoring.zone_cameras.get(zone.zone_id)
        if covering is None:
            return self._finding(zone, ("coverage_unknown",), {"healthy_cameras": 0.0, "required_cameras": required})

        causes: list[str] = []
        healthy = 0
        for camera_id in covering:
            cause = self._camera_fault(monitoring.camera(camera_id), t_latest_global)
            if cause is None:
                healthy += 1
            else:
                causes.append(f"{cause}:{camera_id}")
        if healthy >= self._min_cameras:
            return None
        if len(covering) < self._min_cameras:
            causes.insert(0, "coverage_insufficient")
        metrics = {
            "healthy_cameras": float(healthy),
            "required_cameras": required,
            "covering_cameras": float(len(covering)),
        }
        return self._finding(zone, tuple(causes), metrics)

    def _camera_fault(self, camera: CameraMonitor | None, t_latest_global: int) -> str | None:
        """정상이면 None, 아니면 원인 코드."""
        if camera is None:
            return "camera_unreported"
        if camera.status is not CameraStatus.OK:
            return f"camera_{camera.status.value.lower()}"
        if camera.last_capture is None:
            return "camera_no_frame"
        if t_latest_global - camera.last_capture > self._camera_stall_ms:
            return "camera_stalled"
        return None

    def _finding(self, zone: Zone, causes: tuple[str, ...], metrics: dict[str, float]) -> Finding:
        return Finding(
            rule_id=R7_RULE_ID,
            rule_version=R7_RULE_VERSION,
            severity=Severity.CAUTION,
            world_ids=(),
            zone_id=zone.zone_id,
            needs_check=False,
            reasons=(REASON_DEGRADED, *causes),
            metrics=metrics,
        )
