"""health payload 조립 (계약 messages/health.schema.json 2.2.0, 명세 12.3, 6.1, 6.2, 6.5, 3.2, 3.9, 3.10).

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
- pipeline_lag_ms(필수 키, 2.0.0 null 허용): 보정한 T_lag(HealthReport.lag_ms). 시계 차이를 모르거나 맞지 않거나
  t_latest_global 이 없어 T_lag 를 모르면 null 이다(0·하한으로 채우지 않음, S15P31A101-179). 그때 처리 정체 경과
  (progress_age_ms, 참 lag 의 하한)를 알면 pipeline_lag_lower_bound_ms 로 따로 싣는다. T_lag 를 모르면 evaluate_health 가
  늘 UNSAFE_UNKNOWN 을 내지만(NO_VALID_FRAMES·CLOCK_OFFSET_*), 그 짝이 깨진 입력이 와도 지연을 모르는 health 를 NOMINAL·
  DEGRADED 로 내지 않게 UNSAFE_UNKNOWN 으로 올린다(계약 불변 규칙, 내리지는 않음).
- cameras[].status: 입력이 끊겼거나(feed) 받은 프레임이 없거나 t_latest_global 대비 camera_stall_ms 를 넘겨 멈췄으면 NO_SIGNAL,
  아니면 동기화 무효(CameraClock.sync_valid False)면 SYNC_INVALID, 아니면 OK. sync_offset_ms 는 잴 모듈이 없어 null(모르면
  비움). sync_valid(2.0.0)는 무효 판정이면 false, 그 밖에는 null 이다: 카메라 간 동기화 측정 모듈이 아직 없어 인지 조립
  (Perception.camera_clocks)이 sync_valid 를 True 로 두는데, 그것은 측정 근거가 아니므로 true 로 싣지 않는다(지어내지 않음).
  fps 는 주기 갱신(update_fps)의 처리 프레임 수/경과.
- tier0_warnings(2.0.0): 정리 주기(service._check_warnings)가 refresh_warnings 로 Checkpointer·JournalWriter·outbox·
  EventStateSnapshot 상태와 표시 파일 쓰기 실패(service.marker_error, 2.1.0 LIVE_MARKER_WRITE_FAILED)를 읽어(읽기만) 켜진 경고와 처음 확인한 시각을 기억하고, payload 는 그 기억을 싣는다. health 는
  판정 lock 안에서도 만들어지므로(현장 경보 재동기화) 저널 I/O 동안 lock 을 쥘 수 있는 outbox·스냅샷 상태를 여기서 직접
  읽지 않는다. 한 번도 읽지 못했거나, 마지막 읽기가 실패했거나, 마지막 읽기가 WARNINGS_STALE_MS 보다 오래됐으면 필드를
  싣지 않는다(모름. 빈 배열 = 경고 없음과 구분). 원인 상태가 풀리면 다음 읽기에서 빠지고, 다시 켜지면 since_ms 가 새 시각이다.
- gateway_link·laptop_clock_offset(끊기면 null)·config_version·config_sync·calibration_state·calibration_version.
- re_setup(2.2.0, -480): 회전 자동 복구가 '현장 세팅 다시 하기'를 요구한 카메라(camera_id·reason·detected_at, 해소되면 빈 배열).
  출처(Tier0Service.re_setup_field)가 정한 값을 그대로 싣기만 한다(판정·system_health 는 바꾸지 않음). 출처를 주지 않는 조립은 필드를 싣지 않는다(모름).
- active_site_alerts·as_of 와 ec2_world_state_drops 는 이 함수가 아니라 각 출처(SiteAlertResync.fields, I1Publisher)가 합친다.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.tier0.perception import Perception
from simai_ai.recovery import RecoveryHealthGate
from simai_ai.tracking.system_health import CalibrationState, HealthReport, ProgressWatchdog, evaluate_health
from simai_ai.tracking.time_model import SAME_CLOCK, CaptureClock, TimeModelConfig

log = logging.getLogger("services.tier0.health")

HEALTH_SCHEMA_VERSION = "2.3.0"
"""health 계약이 마지막으로 바뀐 버전 (2.0.0: pipeline_lag_ms null·하한, tier0_warnings, cameras[].sync_valid, -179.
2.1.0: tier0_warnings code LIVE_MARKER_WRITE_FAILED, -197. 2.2.0: re_setup 재세팅 필요 카메라, -480).
봉투에 이 값을 쓰는 것은 송신 모듈(i1_client.MESSAGE_SCHEMA_VERSIONS, i0_control.DOWNSTREAM_VERSIONS)이다"""

WARNINGS_STALE_MS = 5_000
"""운영 경고를 마지막으로 읽은 지 이만큼 지나면 모른다고 보고 tier0_warnings 를 싣지 않는다(정리 주기 1초의 5배)"""


class Tier0Warning(StrEnum):
    """health.tier0_warnings 의 code (계약 2.1.0 닫힌 목록, 명세 6.5·3.2)."""

    CHECKPOINT_STALLED = "CHECKPOINT_STALLED"
    CHECKPOINT_ERROR = "CHECKPOINT_ERROR"
    JOURNAL_STALLED = "JOURNAL_STALLED"
    JOURNAL_FAILED = "JOURNAL_FAILED"
    OUTBOX_BLOCKED = "OUTBOX_BLOCKED"
    SNAPSHOT_BROKEN = "SNAPSHOT_BROKEN"
    LIVE_MARKER_WRITE_FAILED = "LIVE_MARKER_WRITE_FAILED"  # 2.1.0 정보 경고: 판정·알림은 그대로


@dataclass(frozen=True)
class WarningSources:
    """운영 경고의 출처(읽기만). checkpointer: stalled()·last_error, journal_writer: stalled()·failed,
    outbox: blocked, snapshot: broken (recovery.Checkpointer, eventlog.JournalWriter, SnapshotOutbox, EventStateSnapshot),
    marker_error: 지금 표시 파일 쓰기 실패(Tier0Service.marker_error, 없으면 None)."""

    checkpointer: Any
    journal_writer: Any
    outbox: Any
    snapshot: Any
    marker_error: str | None = None


def active_warnings(sources: WarningSources) -> frozenset[Tier0Warning]:
    """지금 켜진 운영 경고. 출처의 상태를 읽기만 한다(고치거나 다시 시도하지 않음)."""
    cp, writer = sources.checkpointer, sources.journal_writer
    on = {
        Tier0Warning.CHECKPOINT_STALLED: bool(cp.stalled()),
        Tier0Warning.CHECKPOINT_ERROR: cp.last_error is not None,
        Tier0Warning.JOURNAL_STALLED: bool(writer.stalled()),
        Tier0Warning.JOURNAL_FAILED: writer.failed is not None,
        Tier0Warning.OUTBOX_BLOCKED: sources.outbox.blocked is not None,
        Tier0Warning.SNAPSHOT_BROKEN: sources.snapshot.broken is not None,
        Tier0Warning.LIVE_MARKER_WRITE_FAILED: sources.marker_error is not None,
    }
    return frozenset(code for code, active in on.items() if active)


@dataclass(frozen=True)
class HealthSnapshot:
    report: HealthReport
    capture_clock: CaptureClock
    gateway_ok: bool
    ec2_ok: bool


class HealthReporter:
    """health payload 를 만든다. I1·노트북 송신(판정 스레드·접속 훅)과 정리 주기 평가에서 불러도 된다."""

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
        calibration_issue: Callable[[], bool] | None = None,
        re_setup: Callable[[], list[dict[str, Any]]] | None = None,
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
        self._calibration_issue = calibration_issue
        self._re_setup = re_setup
        self._lock = lock if lock is not None else threading.RLock()
        self._time_config = time_config
        self._fps_lock = threading.Lock()
        self._fps: dict[str, float] = {}
        self._fps_since_ns: int | None = None
        self._warn_lock = threading.Lock()
        self._warning_since: dict[Tier0Warning, int] = {}
        self._warnings_read_ns: int | None = None
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

    def refresh_warnings(self, sources: WarningSources) -> None:
        """정리 주기에서 부른다(판정 lock 밖). 운영 경고 상태를 읽어 켜진 경고와 처음 확인한 시각을 기억한다.

        읽기가 실패하면 기억을 지우고(다음 health 는 tier0_warnings 를 싣지 않음, 모름) 예외를 그대로 낸다.
        """
        try:
            active = active_warnings(sources)
        except BaseException:
            with self._warn_lock:
                self._warning_since = {}
                self._warnings_read_ns = None
            raise
        now_ms, now_ns = self._now_ms(), self._mono_ns()
        with self._warn_lock:
            self._warning_since = {code: self._warning_since.get(code, now_ms) for code in active}
            self._warnings_read_ns = now_ns

    def warnings_field(self) -> list[dict[str, Any]] | None:
        """health.tier0_warnings 값. 모르면(읽은 적 없음·실패·오래됨) None 이고 그때는 필드를 싣지 않는다."""
        now_ns = self._mono_ns()
        with self._warn_lock:
            read = self._warnings_read_ns
            if read is None or now_ns < read or (now_ns - read) / 1e6 > WARNINGS_STALE_MS:
                return None
            return [{"code": str(code), "since_ms": since} for code, since in sorted(self._warning_since.items())]

    def evaluate(self) -> HealthSnapshot:
        with self._lock:  # 카메라 시각·구성 묶음·게이트를 직렬화해 읽는다(판정 lock 을 기다리지 않는다, -672)
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
        with self._lock:
            return self._payload()

    def _payload(self) -> dict[str, Any]:
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
            sync_invalid = clock.sync_valid is False
            cameras.append(
                {
                    "id": clock.camera_id,
                    "fps": fps.get(clock.camera_id, 0.0),
                    "last_capture": clock.last_capture,
                    "sync_offset_ms": None,
                    # 무효 판정만 근거가 있다. True 는 측정 모듈이 없어 둔 값이라 싣지 않는다(모듈 설명)
                    "sync_valid": False if sync_invalid else None,
                    "status": "NO_SIGNAL" if stalled else ("SYNC_INVALID" if sync_invalid else "OK"),
                }
            )
        system_health = str(report.system_health)
        if self._calibration_issue is not None and self._calibration_issue() and system_health == "NOMINAL":
            system_health = "DEGRADED"
        lag: float | None = None
        if report.lag_ms is not None:
            lag = max(report.lag_ms, 0)
        elif system_health != "UNSAFE_UNKNOWN":
            # 지연을 모르는데 NOMINAL·DEGRADED 로 내지 않는다(계약 2.0.0 불변 규칙, 올리기만 한다)
            log.error("T_lag 를 모르는데 system_health 가 %s 다: UNSAFE_UNKNOWN 으로 보낸다", system_health)
            system_health = "UNSAFE_UNKNOWN"
        payload: dict[str, Any] = {
            "system_health": system_health,
            "t_latest_global": t_latest,
            "cameras": cameras,
            "pipeline_lag_ms": lag,
            "calibration_state": str(CalibrationState(self._calibration_state())),
            "gateway_link": "CONNECTED" if snap.gateway_ok else "DISCONNECTED",
        }
        if lag is None and report.progress_age_ms is not None:
            # 참 T_lag 의 하한(처리 정체 경과). T_lag 가 아니므로 pipeline_lag_ms 에 넣지 않는다
            payload["pipeline_lag_lower_bound_ms"] = round(report.progress_age_ms, 1)
        warnings = self.warnings_field()
        if warnings is not None:
            payload["tier0_warnings"] = warnings
        version = self._calibration_version()
        if version:
            payload["calibration_version"] = version
        if self._re_setup is not None:
            payload["re_setup"] = self._re_setup()
        payload.update(self._config_fields())
        if self._laptop_clock_offset is not None:
            payload["laptop_clock_offset"] = self._laptop_clock_offset(snap.gateway_ok)
        return payload
