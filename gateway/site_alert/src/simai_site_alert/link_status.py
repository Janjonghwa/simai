"""노트북 장애 상태 기계: Tier 0·영상 중계·EC2 연결 → 현장 표시(site_display)와 laptop_heartbeat 내용 (S15P31A101-91).

노트북 쪽 장애 판단은 이 모듈 한 곳에서만 한다. 화면 표시(display.py)와 노트북 → Tier 0·EC2 하트비트(heartbeat.py)는
여기서 만든 SiteStatus·payload 를 그대로 쓴다(명세 3.2 이중 감시, 3.10 표, 6.5, 계약 laptop_heartbeat).

| 장애 | 노트북 표시 (3.10 표) | 판단 근거 |
|---|---|---|
| RunPod Tier 0 중단 | CONTROL_HALTED "관제 중단" | 제어 채널 끊김, Tier 0 health 3회 연속 누락, 처리 정체(t_latest_global 이 늘지 않음), health 의 system_health 가 UNSAFE_UNKNOWN 이거나 없음 |
| 노트북 → RunPod 영상 단절 | CONTROL_HALTED "관제 중단" | 영상 중계(gateway/relay)가 Tier 0 영상 연결이 하나도 없다고 알림, 중계 상태 보고가 3회 연속 누락 |
| EC2 만 단절 | REMOTE_FAULT "원격 관제 장애" | 노트북 → EC2 하트비트에 3초 동안 2xx 응답 없음 |
| 분석 워커 중단 | AI_DELAY "AI 분석 지연" | 노트북에 오는 신호가 없어 만들지 않는다(MR 남은 일) |

규칙:
- 모든 하트비트·보고는 받는 쪽(노트북) 단조 시계로 잰다. 1초 주기 3회 연속 누락(timeout_s=3초)을 장애로 본다(명세 3.2, 6.5).
  벽시계(now_ms)는 last_message_at·last_response_at 기록에만 쓰고 판단에 쓰지 않는다(시계가 틀려도 판단이 바뀌지 않음).
- Tier 0 정상은 소켓이 아니라 처리 진행(health 의 t_latest_global 이 직전 값보다 늘었는지)으로 본다(3.2). 값이 줄면(노트북
  시계 되돌림·Tier 0 재시작) 기준만 새 값으로 옮기고 진행으로 세지 않는다. 다음에 늘면 진행이다.
- 모르면 정상으로 보지 않는다: 시작 직후(아무것도 받기 전)는 CONTROL_HALTED, EC2 응답을 받기 전은 원격 장애로 본다.
  health 에 system_health 가 없거나 모르는 값이면 판정 불가로 본다.
- 장애로 바꾸는 것은 즉시, 정상으로 되돌리는 것은 정상 조건이 recover_s(2초) 동안 이어질 때만 한다. 깜빡이는 연결에서
  '정상'이 잠깐씩 보이지 않게 한다. 제어 채널이 끊기면 그 연결에서 받은 health·진행 기록을 버린다(재접속 뒤 새로 받아야 정상).
- 두 장애가 겹치면 CONTROL_HALTED 가 먼저다(관제 중단이 더 크다). 화면에는 원인을 모두 쓴다.
- 이 모듈은 현장 경보(소리·표시) 판단에 관여하지 않는다. EC2·영상이 끊겨도 현장 경보 코어는 그대로 돈다.

laptop_heartbeat 필드 (계약):
- tier0.control: 제어 채널이 연결돼 있고 timeout_s 안에 health 를 받았으면 CONNECTED (계약 정의 그대로. 처리 정체는
  site_display=CONTROL_HALTED 로 알린다).
- tier0.last_message_at: Tier 0 에서 마지막으로 받은 메시지의 노트북 수신 시각(벽시계). 받은 적 없으면 null.
- tier0.video_cameras: 중계 보고가 살아 있을 때 Tier 0 영상 연결이 살아 있는 카메라. 모르면 빈 배열(살아 있다고 하지 않음).
- ec2: Tier 0 로 보내는 하트비트에만 싣는다(계약 설명). state 는 timeout_s 안에 2xx 응답을 받았는지.
- cameras: 중계 보고가 살아 있을 때만 싣는다(모르면 비움).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from simai_site_alert.messages import Health

DEFAULT_TIMEOUT_S = 3.0  # 1초 주기 3회 연속 누락 (명세 3.2, 6.5)
DEFAULT_RECOVER_S = 2.0  # 정상으로 되돌리기 전에 정상 조건이 이어져야 하는 시간

TEST_SOURCE_KINDS = ("FILE", "SYNTHETIC")  # 실제 카메라가 아닌 입력 (laptop_heartbeat 1.3.0 cameras[].source_kind)

NORMAL = "NORMAL"
CONTROL_HALTED = "CONTROL_HALTED"
REMOTE_FAULT = "REMOTE_FAULT"
AI_DELAY = "AI_DELAY"


# 원인 코드 (화면 문구는 display.py). 계약 필드가 아니라 표시용 내부 값이다.
class Reason:
    CONTROL_NOT_CONNECTED = "CONTROL_NOT_CONNECTED"  # 제어 채널 연결 전·끊김
    HEALTH_MISSING = "HEALTH_MISSING"  # 연결은 있는데 health 3회 연속 누락(반쯤 열린 연결, Tier 0 멈춤)
    PROCESSING_STALLED = "PROCESSING_STALLED"  # health 는 오는데 t_latest_global 이 늘지 않음
    TIER0_UNSAFE = "TIER0_UNSAFE"  # Tier 0 가 UNSAFE_UNKNOWN(판정 불가)이라고 알림, 또는 system_health 없음
    VIDEO_DOWN = "VIDEO_DOWN"  # 중계가 Tier 0 영상 연결이 하나도 없다고 알림
    VIDEO_UNKNOWN = "VIDEO_UNKNOWN"  # 중계 상태 보고가 끊김(중계가 멈췄거나 보고를 못 받음)
    EC2_UNREACHABLE = "EC2_UNREACHABLE"  # EC2 하트비트 응답(2xx) 3회 연속 없음


@dataclass(frozen=True)
class RelayReport:
    """영상 중계(gateway/relay)가 1초마다 알리는 상태 (relay_status.py 가 해석한 값)."""

    video_cameras: tuple[str, ...]  # Tier 0 영상 TLS 연결이 살아 있는 카메라
    camera_count: int  # 중계가 맡은 카메라 수
    cameras: tuple[dict[str, Any], ...] = ()  # laptop_heartbeat.cameras[] 항목(형식 확인됨)

    @property
    def test_input(self) -> bool:
        """중계 입력에 녹화 파일(FILE)·합성 영상(SYNTHETIC)이 하나라도 있다(S15P31A101-724, AGENTS 규칙 5).

        source_kind 가 없는 카메라(이전 중계)는 RTSP 로 본다. 실제 경보를 끄는 쪽이므로 확실한 값만 시험 입력으로 센다.
        """
        return any(c.get("source_kind") in TEST_SOURCE_KINDS for c in self.cameras)


@dataclass(frozen=True)
class SiteStatus:
    """화면과 하트비트가 함께 쓰는 노트북 장애 상태 (evaluate 결과)."""

    site_display: str
    tier0_reasons: tuple[str, ...]  # 관제 중단 원인(없으면 Tier 0·영상 정상)
    ec2_reasons: tuple[str, ...]  # 원격 장애 원인
    control_link: str  # 계약 tier0.control (CONNECTED / DISCONNECTED)
    ec2_link: str  # 계약 ec2.state
    video_cameras: tuple[str, ...]  # 모르면 ()
    camera_count: int | None  # 중계가 맡은 카메라 수. 모르면 None
    recovering: bool = False  # 원인은 없어졌지만 recover_s 를 기다리는 중


class _Liveness:
    """1초 주기 신호 하나의 생존 판정. last_ok 이후 timeout_s 가 지나면 끊김."""

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        self.last_ok: float | None = None

    def ok(self, now: float) -> None:
        if self.last_ok is None or now >= self.last_ok:
            self.last_ok = now

    def reset(self) -> None:
        self.last_ok = None

    def up(self, now: float) -> bool:
        return self.last_ok is not None and now - self.last_ok <= self._timeout_s


class _Hysteresis:
    """장애는 즉시, 정상 복귀는 정상 조건이 recover_s 동안 이어질 때만. 처음은 장애(모름)."""

    def __init__(self, recover_s: float) -> None:
        self._recover_s = recover_s
        self.faulted = True
        self._ok_since: float | None = None

    def update(self, ok_now: bool, now: float) -> bool:
        """지금 장애로 볼지 돌려준다."""
        if not ok_now:
            self._ok_since = None
            self.faulted = True
        else:
            if self._ok_since is None or now < self._ok_since:
                self._ok_since = now
            if self.faulted and now - self._ok_since >= self._recover_s:
                self.faulted = False
        return self.faulted

    def break_streak(self) -> None:
        """정상 조건이 중간에 끊겼다(평가 사이의 짧은 끊김). 정상 복귀 대기를 처음부터 다시 한다."""
        self._ok_since = None
        self.faulted = True


@dataclass
class _Tier0:
    control_connected: bool = False
    last_message_ms: int | None = None
    last_t_latest_global: int | None = None
    system_health: str | None = None
    health_seen: bool = False
    connected_at: float | None = None
    reset_requested: bool = False


@dataclass
class _Relay:
    reports: _Liveness
    report: RelayReport | None = None


class SiteLinkMonitor:
    """노트북 장애 상태 기계. 입력(on_*)은 어느 스레드에서 불러도 된다(잠근다). 시각은 호출하는 쪽이 넣는다.

    now 는 단조 시계 초(clock.monotonic), now_ms 는 벽시계 UTC ms(clock.now_ms, 기록용)다.
    """

    def __init__(self, *, timeout_s: float = DEFAULT_TIMEOUT_S, recover_s: float = DEFAULT_RECOVER_S) -> None:
        if timeout_s <= 0 or recover_s < 0:
            raise ValueError("timeout_s 는 양수, recover_s 는 0 이상이다")
        self.timeout_s = timeout_s
        self.recover_s = recover_s
        self._lock = threading.Lock()
        self._t = _Tier0()
        self._health = _Liveness(timeout_s)
        self._progress = _Liveness(timeout_s)
        self._relay = _Relay(reports=_Liveness(timeout_s))
        self._ec2 = _Liveness(timeout_s)
        self._ec2_last_response_ms: int | None = None
        self._tier0_h = _Hysteresis(recover_s)
        self._ec2_h = _Hysteresis(recover_s)
        self._seq = 0

    # ---- 입력: Tier 0 제어 채널 -----------------------------------------------------------------------------

    def on_control_connected(self, connected: bool, now: float) -> None:
        with self._lock:
            if connected:
                self._t.control_connected = True
                self._t.connected_at = now
                self._t.reset_requested = False
            else:
                # 끊긴 연결에서 받은 health·진행 기록은 버린다. 재접속 뒤 새 health 로 다시 정상을 확인한다.
                self._t.control_connected = False
                self._t.connected_at = None
                self._health.reset()
                self._progress.reset()
                self._tier0_h.break_streak()

    def on_tier0_message(self, now: float, now_ms: int, health: Health | None = None) -> None:
        """Tier 0 에서 메시지 하나를 받았다(형식이 틀린 것 포함). health 면 해석한 값을 함께 준다."""
        with self._lock:
            if not self._t.control_connected:
                return  # 끊긴 뒤 늦게 처리된 메시지로 정상을 만들지 않는다
            self._t.last_message_ms = now_ms
            if health is None:
                return
            self._t.health_seen = True
            self._health.ok(now)
            self._t.system_health = health.system_health
            if health.system_health not in ("NOMINAL", "DEGRADED"):
                self._tier0_h.break_streak()  # 평가 사이에 지나간 판정 불가도 놓치지 않는다
            t = health.t_latest_global
            if t is not None:
                prev = self._t.last_t_latest_global
                if prev is not None and t > prev:
                    self._progress.ok(now)
                self._t.last_t_latest_global = t  # 줄었으면 기준만 옮긴다(진행으로 세지 않음)

    def needs_control_reset(self, now: float) -> bool:
        """연결은 살아 있는데 health 가 timeout_s 넘게 없으면 True(연결당 한 번). 반쯤 열린 연결을 끊고 다시 붙는다(3.2)."""
        with self._lock:
            t = self._t
            if not t.control_connected or t.reset_requested or t.connected_at is None:
                return False
            since = self._health.last_ok if self._health.last_ok is not None else t.connected_at
            if now - since > self.timeout_s:
                t.reset_requested = True
                return True
            return False

    # ---- 입력: 영상 중계 ----------------------------------------------------------------------------------------

    def on_relay_report(self, report: RelayReport, now: float) -> None:
        with self._lock:
            self._relay.report = report
            self._relay.reports.ok(now)
            if not report.video_cameras:
                self._tier0_h.break_streak()  # 평가 사이에 지나간 영상 끊김도 놓치지 않는다

    # ---- 입력: EC2 하트비트 결과 ---------------------------------------------------------------------------------

    def on_ec2_result(self, ok: bool, now: float, now_ms: int) -> None:
        """EC2 하트비트 POST 결과. ok 는 2xx 응답. 실패 한 번으로 바로 끊김으로 보지 않는다(3회 누락 = timeout_s)."""
        with self._lock:
            if ok:
                self._ec2.ok(now)
                self._ec2_last_response_ms = now_ms

    # ---- 평가 ---------------------------------------------------------------------------------------------------

    def _tier0_reasons(self, now: float) -> list[str]:
        t = self._t
        reasons: list[str] = []
        if not t.control_connected:
            reasons.append(Reason.CONTROL_NOT_CONNECTED)
        elif not self._health.up(now):
            reasons.append(Reason.HEALTH_MISSING)
        else:
            if not self._progress.up(now):
                reasons.append(Reason.PROCESSING_STALLED)
            if t.system_health not in ("NOMINAL", "DEGRADED"):
                reasons.append(Reason.TIER0_UNSAFE)
        report = self._relay.report
        if report is None or not self._relay.reports.up(now):
            reasons.append(Reason.VIDEO_UNKNOWN)
        elif not report.video_cameras:
            reasons.append(Reason.VIDEO_DOWN)
        return reasons

    def evaluate(self, now: float) -> SiteStatus:
        with self._lock:
            tier0_reasons = self._tier0_reasons(now)
            ec2_reasons = [] if self._ec2.up(now) else [Reason.EC2_UNREACHABLE]
            tier0_fault = self._tier0_h.update(not tier0_reasons, now)
            ec2_fault = self._ec2_h.update(not ec2_reasons, now)
            if tier0_fault:
                display = CONTROL_HALTED
            elif ec2_fault:
                display = REMOTE_FAULT
            else:
                display = NORMAL
            relay_up = self._relay.report is not None and self._relay.reports.up(now)
            report = self._relay.report if relay_up else None
            return SiteStatus(
                site_display=display,
                tier0_reasons=tuple(tier0_reasons),
                ec2_reasons=tuple(ec2_reasons),
                control_link=self._control_link(now),
                ec2_link="CONNECTED" if self._ec2.up(now) else "DISCONNECTED",
                video_cameras=report.video_cameras if report else (),
                camera_count=report.camera_count if report else None,
                recovering=(tier0_fault and not tier0_reasons) or (not tier0_fault and ec2_fault and not ec2_reasons),
            )

    def _control_link(self, now: float) -> str:
        return "CONNECTED" if self._t.control_connected and self._health.up(now) else "DISCONNECTED"

    # ---- laptop_heartbeat payload -------------------------------------------------------------------------------

    def heartbeat_payload(
        self, status: SiteStatus, alarm: dict[str, list[str]] | None, *, include_ec2: bool
    ) -> dict[str, Any]:
        """status(방금 evaluate 한 값)로 laptop_heartbeat payload 를 만든다. heartbeat_seq 는 부를 때마다 하나씩 는다.

        include_ec2: Tier 0 로 보내는 하트비트면 True(계약: ec2 는 Tier 0 로 보내는 것에만 의미가 있다).
        """
        with self._lock:
            seq = self._seq
            self._seq += 1
            payload: dict[str, Any] = {
                "heartbeat_seq": seq,
                "tier0": {
                    "control": status.control_link,
                    "last_message_at": self._t.last_message_ms,
                    "video_cameras": sorted(status.video_cameras),
                },
                "site_display": status.site_display,
            }
            if include_ec2:
                payload["ec2"] = {"state": status.ec2_link, "last_response_at": self._ec2_last_response_ms}
            if alarm is not None:
                payload["alarm"] = {
                    "sounding_event_ids": list(dict.fromkeys(alarm["sounding_event_ids"])),
                    "displayed_event_ids": list(dict.fromkeys(alarm["displayed_event_ids"])),
                }
            report = self._relay.report
            if report is not None and status.camera_count is not None and report.cameras:
                payload["cameras"] = [dict(c) for c in report.cameras]
            return payload
