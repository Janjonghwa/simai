"""Tier 0 health payload 조립 (services.tier0.health, 계약 health 2.0.0, S15P31A101-179).

- T_lag 를 모르면 pipeline_lag_ms 는 null 이고 0·하한을 참값처럼 싣지 않는다. 하한(처리 정체 경과)은 따로 싣는다.
- 카메라 sync_valid 는 근거가 있는 무효 판정만 false, 그 밖은 null(측정 모듈이 없어 둔 True 를 싣지 않음).
- 운영 경고(tier0_warnings)는 정리 주기에 읽은 상태만 싣고, 읽지 못했거나 오래됐으면 필드를 싣지 않는다(모름).
보고 전 자기 반례 시험: 시계 모름·진행 미관측·프레임 없음, 지연을 모르는데 NOMINAL 인 보고, 근거 없는 sync true,
경고가 풀리고 다시 켜지는 순서, 읽기 실패·오래된 기억.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.tier0.health import (  # noqa: E402
    HEALTH_SCHEMA_VERSION,
    WARNINGS_STALE_MS,
    HealthReporter,
    Tier0Warning,
    WarningSources,
    active_warnings,
)
from simai_ai.providers.contract import schema_errors  # noqa: E402
from simai_ai.tracking.system_health import ProgressWatchdog, SystemHealth  # noqa: E402
from simai_ai.tracking.time_model import SAME_CLOCK, CameraClock, ClockOffset  # noqa: E402

T0 = 1_760_342_400_000
MS = 1_000_000


class _Feed:
    def camera_connected(self, camera_id: str) -> bool:
        return True


@dataclass
class _Perception:
    clocks: list[CameraClock]
    feed: _Feed = field(default_factory=_Feed)

    def camera_clocks(self) -> tuple[CameraClock, ...]:
        return tuple(self.clocks)

    def take_camera_stats(self) -> list[Any]:
        return []


class _PassGate:
    """재시작 하한 없이 evaluate_health 결과를 그대로 둔다(복구 하한은 recovery 시험이 본다)."""

    def __init__(self, override: dict[str, Any] | None = None) -> None:
        self.override = override or {}

    def apply(self, report, *, capture_clock):
        return replace(report, **self.override)


@dataclass
class _Clock:
    now: int = T0
    mono: int = 10_000 * MS

    def advance(self, ms: int) -> None:
        self.now += ms
        self.mono += ms * MS


def _reporter(clock: _Clock, *, capture_clock=SAME_CLOCK, clocks=None, gate=None, watchdog=None, re_setup=None) -> HealthReporter:
    clocks = clocks if clocks is not None else [CameraClock(c, T0 - 100, True) for c in ("cam1", "cam2", "cam3", "cam4")]
    return HealthReporter(
        perception=_Perception(clocks),
        now_ms=lambda: clock.now,
        mono_ns=lambda: clock.mono,
        gate=gate or _PassGate(),
        watchdog=watchdog or ProgressWatchdog(),
        ec2_connected=lambda: True,
        gateway_ok=lambda: True,
        capture_clock=None if capture_clock is SAME_CLOCK else (lambda ok: capture_clock),
        laptop_clock_offset=None,
        config_fields=lambda: {"config_version": "cfg-1", "config_sync": "CURRENT"},
        config_active=lambda: True,
        calibration_state=lambda: "CALIBRATED",
        calibration_version=lambda: "cal-1",
        re_setup=re_setup,
    )


def _msg(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "msg_type": "health",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000001",
        "sent_at": T0,
        "source_id": "tier0-01",
        "payload": payload,
    }


# --- pipeline_lag_ms -------------------------------------------------------------------------


def test_known_lag_is_number_without_lower_bound() -> None:
    clock = _Clock()
    payload = _reporter(clock).payload()
    assert payload["pipeline_lag_ms"] == 100 and "pipeline_lag_lower_bound_ms" not in payload
    assert schema_errors(_msg(payload), "health") == []


def test_counterexample_unknown_clock_never_sends_lower_bound_as_lag() -> None:
    """반례 1: 시계 차이를 모르는데 처리가 2.5초 멈췄다. 하한(2500)을 pipeline_lag_ms 로 싣지 않는다."""
    clock = _Clock()
    watchdog = ProgressWatchdog()
    watchdog.observe(T0 - 100, clock.mono)
    clock.advance(2_500)
    payload = _reporter(clock, capture_clock=None, watchdog=watchdog).payload()
    assert payload["pipeline_lag_ms"] is None
    assert payload["pipeline_lag_lower_bound_ms"] == 2500.0
    assert payload["system_health"] == "UNSAFE_UNKNOWN"
    assert schema_errors(_msg(payload), "health") == []
    # 음성 대조: 하한을 pipeline_lag_ms 에 넣고 하한 필드도 두면 스키마가 막는다
    assert schema_errors(_msg(dict(payload, pipeline_lag_ms=2500.0)), "health")


def test_counterexample_unknown_clock_without_progress_has_no_lower_bound_and_no_zero() -> None:
    """반례 2: 처리 진행을 본 적이 없으면 하한도 모른다. 0 을 채우지 않는다."""
    payload = _reporter(_Clock(), capture_clock=None).payload()
    assert payload["pipeline_lag_ms"] is None and "pipeline_lag_lower_bound_ms" not in payload
    assert payload["system_health"] == "UNSAFE_UNKNOWN"
    assert schema_errors(_msg(payload), "health") == []


def test_counterexample_no_frames_is_null_lag() -> None:
    """반례 3: 처리한 프레임이 없다(t_latest_global null). 같은 시계여도 T_lag 는 모른다."""
    clocks = [CameraClock("cam1", None, True), CameraClock("cam2", None, True)]
    payload = _reporter(_Clock(), clocks=clocks).payload()
    assert payload["t_latest_global"] is None and payload["pipeline_lag_ms"] is None
    assert payload["system_health"] == "UNSAFE_UNKNOWN"
    assert schema_errors(_msg(payload), "health") == []


def test_counterexample_unknown_lag_with_nominal_report_is_raised_to_unsafe(caplog) -> None:
    """반례 4: 깨진 입력(지연을 모르는데 NOMINAL 보고)이 와도 health 를 NOMINAL 로 내지 않는다(올리기만)."""
    gate = _PassGate({"lag_ms": None, "system_health": SystemHealth.NOMINAL, "reasons": ()})
    payload = _reporter(_Clock(), gate=gate).payload()
    assert payload["pipeline_lag_ms"] is None and payload["system_health"] == "UNSAFE_UNKNOWN"
    assert "UNSAFE_UNKNOWN" in caplog.text
    assert schema_errors(_msg(payload), "health") == []
    assert schema_errors(_msg(dict(payload, system_health="NOMINAL")), "health")  # 음성 대조: 스키마도 막는다


def test_offset_clock_lag_known() -> None:
    clock = _Clock()
    payload = _reporter(clock, capture_clock=ClockOffset(0.0, 5.0)).payload()
    assert payload["pipeline_lag_ms"] == 100 and "pipeline_lag_lower_bound_ms" not in payload


# --- sync_valid -------------------------------------------------------------------------------


def test_counterexample_placeholder_true_sync_is_reported_as_unknown() -> None:
    """반례 5: 인지 조립이 측정 없이 둔 sync_valid=True 를 true 로 싣지 않는다(null = 근거 없음)."""
    payload = _reporter(_Clock()).payload()
    for cam in payload["cameras"]:
        assert cam["sync_valid"] is None and cam["sync_offset_ms"] is None and cam["status"] == "OK"
    assert schema_errors(_msg(payload), "health") == []
    # 음성 대조: 근거(sync_offset_ms) 없이 true 를 실으면 스키마가 막는다
    bad = dict(payload, cameras=[dict(payload["cameras"][0], sync_valid=True)])
    assert schema_errors(_msg(bad), "health")


def test_invalid_sync_judgement_is_false_and_not_ok() -> None:
    clocks = [CameraClock("cam1", T0 - 100, True), CameraClock("cam2", T0 - 100, False)]
    payload = _reporter(_Clock(), clocks=clocks).payload()
    cam2 = next(c for c in payload["cameras"] if c["id"] == "cam2")
    assert cam2["sync_valid"] is False and cam2["status"] == "SYNC_INVALID"
    assert schema_errors(_msg(payload), "health") == []


# --- tier0_warnings ---------------------------------------------------------------------------


@dataclass
class _Cp:
    stalled_: bool = False
    last_error: BaseException | None = None

    def stalled(self) -> bool:
        return self.stalled_


@dataclass
class _Writer:
    stalled_: bool = False
    failed: BaseException | None = None

    def stalled(self) -> bool:
        return self.stalled_


@dataclass
class _Outbox:
    blocked: Any = None


@dataclass
class _Snapshot:
    broken: str | None = None


def _sources() -> WarningSources:
    return WarningSources(_Cp(), _Writer(), _Outbox(), _Snapshot())


def test_each_source_maps_to_its_code() -> None:
    assert active_warnings(_sources()) == frozenset()
    cases = [
        (lambda s: setattr(s.checkpointer, "stalled_", True), Tier0Warning.CHECKPOINT_STALLED),
        (lambda s: setattr(s.checkpointer, "last_error", OSError("disk")), Tier0Warning.CHECKPOINT_ERROR),
        (lambda s: setattr(s.journal_writer, "stalled_", True), Tier0Warning.JOURNAL_STALLED),
        (lambda s: setattr(s.journal_writer, "failed", OSError("fsync")), Tier0Warning.JOURNAL_FAILED),
        (lambda s: setattr(s.outbox, "blocked", ("7", "no config_version")), Tier0Warning.OUTBOX_BLOCKED),
        (lambda s: setattr(s.snapshot, "broken", "offset 3: gone"), Tier0Warning.SNAPSHOT_BROKEN),
    ]
    for mutate, code in cases:
        src = _sources()
        mutate(src)
        assert active_warnings(src) == frozenset({code})


def test_counterexample_warning_lifecycle_appear_persist_clear_reappear() -> None:
    """반례 6: 켜짐 → 이어짐(since 그대로) → 풀림(빠짐) → 다시 켜짐(새 since). 처음 읽기 전에는 필드가 없다(모름)."""
    clock = _Clock()
    reporter = _reporter(clock)
    assert "tier0_warnings" not in reporter.payload()  # 아직 읽지 않았다: 빈 배열(경고 없음)로 지어내지 않는다
    src = _sources()
    reporter.refresh_warnings(src)
    assert reporter.payload()["tier0_warnings"] == []
    src.outbox.blocked = ("7", "reason")
    clock.advance(1_000)
    reporter.refresh_warnings(src)
    first = clock.now
    assert reporter.payload()["tier0_warnings"] == [{"code": "OUTBOX_BLOCKED", "since_ms": first}]
    clock.advance(1_000)
    src.checkpointer.stalled_ = True
    reporter.refresh_warnings(src)
    payload = reporter.payload()
    assert payload["tier0_warnings"] == [
        {"code": "CHECKPOINT_STALLED", "since_ms": clock.now},
        {"code": "OUTBOX_BLOCKED", "since_ms": first},  # 이어지는 동안 since 는 바뀌지 않는다
    ]
    assert schema_errors(_msg(payload), "health") == []
    src.outbox.blocked = None
    clock.advance(1_000)
    reporter.refresh_warnings(src)
    assert [w["code"] for w in reporter.payload()["tier0_warnings"]] == ["CHECKPOINT_STALLED"]  # 풀린 경고는 빠진다
    src.outbox.blocked = ("9", "reason")
    clock.advance(1_000)
    reporter.refresh_warnings(src)
    again = {w["code"]: w["since_ms"] for w in reporter.payload()["tier0_warnings"]}
    assert again["OUTBOX_BLOCKED"] == clock.now != first  # 다시 켜지면 새 시각


def test_counterexample_unreadable_or_stale_warnings_are_omitted() -> None:
    """반례 7: 읽기 실패·오래된 기억은 '경고 없음'이 아니라 모름(필드 없음)이다."""
    clock = _Clock()
    reporter = _reporter(clock)
    src = _sources()
    src.journal_writer.failed = OSError("fsync")
    reporter.refresh_warnings(src)
    assert [w["code"] for w in reporter.payload()["tier0_warnings"]] == ["JOURNAL_FAILED"]
    clock.advance(WARNINGS_STALE_MS + 1)  # 정리 주기가 멈췄다
    assert "tier0_warnings" not in reporter.payload()
    reporter.refresh_warnings(src)
    assert "tier0_warnings" in reporter.payload()

    class _Broken:
        def stalled(self) -> bool:
            raise RuntimeError("lock 오류")

    with pytest.raises(RuntimeError):
        reporter.refresh_warnings(WarningSources(_Broken(), src.journal_writer, src.outbox, src.snapshot))
    assert "tier0_warnings" not in reporter.payload()
    # 다시 읽히면 그때 확인한 시각이 since 다(모르던 구간을 이어진 것으로 보지 않음)
    clock.advance(500)
    reporter.refresh_warnings(src)
    assert reporter.payload()["tier0_warnings"] == [{"code": "JOURNAL_FAILED", "since_ms": clock.now}]


def test_counterexample_marker_write_failure_warning_appears_and_clears() -> None:
    """반례 9(-197): 표시 파일 쓰기 실패 → LIVE_MARKER_WRITE_FAILED(health 2.1.0 스키마 통과), 실패가 이어지면 since 그대로,
    해소(다음 쓰기 성공, marker_error None) → 빠짐. 경고만 바뀌고 system_health·나머지 필드는 같다(판정·알림 동작 불변)."""
    clock = _Clock()
    reporter = _reporter(clock)
    reporter.refresh_warnings(_sources())
    before = reporter.payload()
    assert before["tier0_warnings"] == []
    failing = WarningSources(_Cp(), _Writer(), _Outbox(), _Snapshot(), "PermissionError: denied")
    reporter.refresh_warnings(failing)
    first = clock.now
    on = reporter.payload()
    assert on["tier0_warnings"] == [{"code": "LIVE_MARKER_WRITE_FAILED", "since_ms": first}]
    assert schema_errors(_msg(on), "health") == []
    assert {k: v for k, v in on.items() if k != "tier0_warnings"} == {k: v for k, v in before.items() if k != "tier0_warnings"}
    clock.advance(1_000)
    reporter.refresh_warnings(WarningSources(_Cp(), _Writer(), _Outbox(), _Snapshot(), "OSError: other"))
    assert reporter.payload()["tier0_warnings"] == [{"code": "LIVE_MARKER_WRITE_FAILED", "since_ms": first}]
    clock.advance(1_000)
    reporter.refresh_warnings(_sources())  # 다음 쓰기 성공
    ref = _reporter(clock)  # 같은 시각, 경고를 한 번도 겪지 않은 송신 측
    ref.refresh_warnings(_sources())
    assert reporter.payload() == ref.payload() and ref.payload()["tier0_warnings"] == []


def test_warning_codes_match_contract_enum() -> None:
    """송신 측 code 목록이 계약의 닫힌 목록과 같다(한쪽만 더하면 health 가 계약 위반이 되거나 경고를 못 싣는다)."""
    import json

    from simai_ai.providers.contract import contracts_dir

    schema = json.loads((contracts_dir() / "messages" / "health.schema.json").read_text(encoding="utf-8"))
    assert set(schema["$defs"]["tier0_warning"]["properties"]["code"]["enum"]) == {str(c) for c in Tier0Warning}


# --- 봉투 버전 ---------------------------------------------------------------------------------


def test_envelope_versions_follow_contract_modules() -> None:
    """반례 8: 송신 봉투가 내용과 다른 버전을 달지 않는다. I1·I0 health 는 HEALTH_SCHEMA_VERSION(2.2.0), I1 world_state 는
    tracking.world_state 의 2.1.0 이다. 2.0.0 내용(null lag)을 단 health 와 UNCERTAIN 값을 단 world_state 가 그 봉투로 검증된다."""
    from services.tier0.i0_control import DOWNSTREAM_VERSIONS
    from services.tier0.i1_client import MESSAGE_SCHEMA_VERSIONS, Envelope
    from simai_ai.tracking.world_state import WORLD_STATE_SCHEMA_VERSION

    assert MESSAGE_SCHEMA_VERSIONS["health"] == DOWNSTREAM_VERSIONS["health"] == HEALTH_SCHEMA_VERSION == "2.2.0"
    assert MESSAGE_SCHEMA_VERSIONS["world_state"] == WORLD_STATE_SCHEMA_VERSION == "2.1.0"

    class _FixedClock:
        def now_ms(self) -> int:
            return T0

    env = Envelope("tier0-01", _FixedClock(), new_msg_id=lambda: "0192a6f0-7c1e-7b3a-9f2d-000000000001")
    payload = _reporter(_Clock(), capture_clock=None).payload()
    msg = env.make("health", payload)
    assert msg["schema_version"] == "2.2.0" and payload["pipeline_lag_ms"] is None
    assert schema_errors(msg, "health") == []


# --- re_setup (계약 2.2.0, S15P31A101-480) ------------------------------------------------------


def test_re_setup_is_reported_without_changing_judgement() -> None:
    """재세팅이 필요한 카메라는 그대로 싣고 system_health·pipeline_lag_ms 는 바꾸지 않는다(경보·판정 불변)."""
    items = [{"camera_id": "cam3", "reason": "매칭 부족", "detected_at": T0 - 5_000}]
    with_items = _reporter(_Clock(), re_setup=lambda: items).payload()
    plain = _reporter(_Clock()).payload()
    assert with_items["re_setup"] == items
    assert {k: v for k, v in with_items.items() if k != "re_setup"} == plain
    assert schema_errors(_msg(with_items), "health") == []


def test_re_setup_empty_means_none_and_missing_means_unknown() -> None:
    """빈 배열 = 재세팅이 필요한 카메라 없음(해소). 출처가 없는 조립은 필드를 싣지 않는다(모름)."""
    empty = _reporter(_Clock(), re_setup=lambda: []).payload()
    assert empty["re_setup"] == [] and schema_errors(_msg(empty), "health") == []
    assert "re_setup" not in _reporter(_Clock()).payload()


def test_re_setup_item_is_a_closed_value_object() -> None:
    """음성 대조: 사유·감지 시각이 빠졌거나 모르는 필드가 있으면 스키마가 막는다."""
    payload = _reporter(_Clock(), re_setup=lambda: [{"camera_id": "cam3", "reason": "x", "detected_at": T0}]).payload()
    assert schema_errors(_msg(payload), "health") == []
    no_reason = dict(payload, re_setup=[{"camera_id": "cam3", "detected_at": T0}])
    extra = dict(payload, re_setup=[{"camera_id": "cam3", "reason": "x", "detected_at": T0, "severity": "HIGH"}])
    bad_camera = dict(payload, re_setup=[{"camera_id": "cam 3", "reason": "x", "detected_at": T0}])
    for bad in (no_reason, extra, bad_camera):
        assert schema_errors(_msg(bad), "health")
