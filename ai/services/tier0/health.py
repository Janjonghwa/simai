"""health payload 조립 (계약 messages/health.schema.json 1.5.0, 명세 12.3, 6.1, 6.2, 3.2, 3.9, 3.10).

System Health 값은 tracking.system_health.evaluate_health 가 정하고, 재시작 하한은 recovery.RecoveryHealthGate 가 덧씌운다.
여기서는 그 입력을 모으고 계약 필드로 옮기기만 한다(판정 로직 없음, ai/CONVENTION.md).

입력:
- 카메라 시각 상태: 이번 프로세스에서 처리한 프레임으로 새로 만든다(Perception). 체크포인트의 촬영 시각을 되살리지 않는다.
- ec2_link_ok: I1 클라이언트가 연결돼 있는지(I1Client.connected). EC2 없이 돌면 False(EC2_LINK_LOST, DEGRADED).
- gateway_link: 노트북 제어 채널 + 하트비트 3회 누락 판정(tracking.gateway_link.GatewayLinkMonitor).
- capture_clock: 노트북 제어 채널이 있으면 시계 교환 추정값(Tier0ClockSync.capture_clock, 모르면 None → UNSAFE_UNKNOWN).
  노트북이 없는 조립(오프라인 SIM)은 SAME_CLOCK 이다. 그 값을 RecoveryHealthGate.apply 에도 그대로 넘긴다
  (SAME_CLOCK 은 시간 동기화 확보가 아니라 재시작 하한이 풀리지 않는다).
- config_active: 적용된 구성 묶음이 있는지(없으면 NO_ACTIVE_CONFIG, UNSAFE_UNKNOWN).
- 처리 정체: ProgressWatchdog(Tier 0 단조 시계).

계약 필드 옮기기:
- pipeline_lag_ms(필수): 보정한 T_lag(HealthReport.lag_ms). 시계 차이를 몰라 비었으면 계약에 정한 값이 없어(명세 19 남은 일)
  처리 정체 경과(참 lag 의 하한, progress_age_ms)를 싣고, 그것도 없으면 0 이다. 그때 system_health 는 늘 UNSAFE_UNKNOWN 이고
  t_latest_global 은 그대로(없으면 null) 싣는다. 이 값만 보고 지연이 작다고 읽지 않는다(MR 남은 일, -172 계약).
- cameras[].status: 입력이 끊겼거나(feed) 받은 프레임이 없거나 t_latest_global 대비 camera_stall_ms 를 넘겨 멈췄으면 NO_SIGNAL,
  아니면 OK. sync_offset_ms 는 잴 모듈이 없어 null(모르면 비움). fps 는 주기 갱신(update_fps)의 처리 프레임 수/경과.
- gateway_link·laptop_clock_offset(끊기면 null)·config_version·config_sync·calibration_state·calibration_version.
- active_site_alerts·as_of 와 ec2_world_state_drops 는 이 함수가 아니라 각 출처(SiteAlertResync.fields, I1Publisher)가 합친다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from services.tier0.perception import Perception
from simai_ai.recovery import RecoveryHealthGate
from simai_ai.tracking.system_health import CalibrationState, HealthReport, ProgressWatchdog, evaluate_health
from simai_ai.tracking.time_model import SAME_CLOCK, CaptureClock, TimeModelConfig

log = logging.getLogger("services.tier0.health")


@dataclass(frozen=True)
class HealthSnapshot:
    report: HealthReport
    capture_clock: CaptureClock
    gateway_ok: bool
    ec2_ok: bool


class HealthReporter:
    """health payload 를 만든다. I1 송신 스레드와 노트북 송신(정리 주기)에서 불러도 된다."""

    def __init__(
        self,
        *,
        perception: Perception,
        now_ms: Callable[[], int],
        mono_ns: Callable[[], int],
        gate: RecoveryHealthGate,
        watchdog: ProgressWatchdog,
        ec2_connected: Callable[[], bool],
        gateway_ok: Callable[[], bool],
        capture_clock: Callable[[bool], CaptureClock] | None,
        laptop_clock_offset: Callable[[bool], Mapping[str, Any] | None] | None,
        config_fields: Callable[[], Mapping[str, Any]],
        config_active: Callable[[], bool],
        calibration_state: Callable[[], str],
        calibration_version: Callable[[], str | None],
        lock: Any = None,
        time_config: TimeModelConfig = TimeModelConfig(),
    ) -> None:
        self._perception = perception
        self._now_ms = now_ms
        self._mono_ns = mono_ns
        self._gate = gate
        self._watchdog = watchdog
        self._ec2_connected = ec2_connected
        self._gateway_ok = gateway_ok
        self._capture_clock = capture_clock
        self._laptop_clock_offset = laptop_clock_offset
        self._config_fields = config_fields
        self._config_active = config_active
        self._calibration_state = calibration_state
        self._calibration_version = calibration_version
        self._lock = lock if lock is not None else threading.RLock()
        self._time_config = time_config
        self._fps_lock = threading.Lock()
        self._fps: dict[str, float] = {}
        self._fps_since_ns: int | None = None
        self.last: HealthSnapshot | None = None

    def update_fps(self) -> None:
        """정리 주기(약 1초)마다 부른다. 직전 호출 뒤 처리한 프레임 수로 카메라별 fps 를 다시 잰다."""
        now = self._mono_ns()
        stats = self._perception.take_camera_stats()
        with self._fps_lock:
            since, self._fps_since_ns = self._fps_since_ns, now
            if since is None or now <= since:
                return
            seconds = (now - since) / 1e9
            self._fps = {s.camera_id: round(s.frames_since / seconds, 2) for s in stats}

    def evaluate(self) -> HealthSnapshot:
        with self._lock:  # 판정 루프와 같은 lock: 카메라 시각·구성 묶음을 한 시점으로 읽는다
            gateway_ok = bool(self._gateway_ok())
            ec2_ok = bool(self._ec2_connected())
            capture_clock: CaptureClock = SAME_CLOCK if self._capture_clock is None else self._capture_clock(gateway_ok)
            progress = self._watchdog.age_ms(self._mono_ns())
            report = evaluate_health(
                self._perception.camera_clocks(),
                CalibrationState(self._calibration_state()),
                gateway_ok,
                ec2_ok,
                self._now_ms(),
                self._time_config,
                capture_clock=capture_clock,
                progress_age_ms=progress,
                config_active=bool(self._config_active()),
            )
            report = self._gate.apply(report, capture_clock=capture_clock)
        snap = HealthSnapshot(report, capture_clock, gateway_ok, ec2_ok)
        self.last = snap
        return snap

    def payload(self) -> dict[str, Any]:
        snap = self.evaluate()
        report = snap.report
        with self._fps_lock:
            fps = dict(self._fps)
        cameras = []
        t_latest = report.t_latest_global
        for clock in self._perception.camera_clocks():
            stalled = (
                clock.last_capture is None
                or t_latest is None
                or t_latest - clock.last_capture > self._time_config.camera_stall_ms
                or not self._perception.feed.camera_connected(clock.camera_id)
            )
            cameras.append(
                {
                    "id": clock.camera_id,
                    "fps": fps.get(clock.camera_id, 0.0),
                    "last_capture": clock.last_capture,
                    "sync_offset_ms": None,
                    "status": "NO_SIGNAL" if stalled else "OK",
                }
            )
        if report.lag_ms is not None:
            lag: float = max(report.lag_ms, 0)
        elif report.progress_age_ms is not None:
            lag = round(report.progress_age_ms, 1)  # 참 lag 의 하한(모듈 설명). system_health 는 UNSAFE_UNKNOWN 이다
        else:
            lag = 0
        payload: dict[str, Any] = {
            "system_health": str(report.system_health),
            "t_latest_global": t_latest,
            "cameras": cameras,
            "pipeline_lag_ms": lag,
            "calibration_state": str(CalibrationState(self._calibration_state())),
            "gateway_link": "CONNECTED" if snap.gateway_ok else "DISCONNECTED",
        }
        version = self._calibration_version()
        if version:
            payload["calibration_version"] = version
        payload.update(self._config_fields())
        if self._laptop_clock_offset is not None:
            payload["laptop_clock_offset"] = self._laptop_clock_offset(snap.gateway_ok)
        return payload
