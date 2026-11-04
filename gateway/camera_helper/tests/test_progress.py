import logging
import threading

import pytest

from simai_camera_helper.judge import CAMERAS
from simai_camera_helper.message import DEFAULT_STALE_MS
from simai_camera_helper.progress import Progress, bounded_setting, log_round, watchdog_loop
from simai_camera_helper.service import Frames, load_env

LOGGER = "simai_camera_helper.service"


@pytest.mark.parametrize("name,default,minimum,maximum", [
    ("SIMAI_CAMERA_HELPER_STALE_MS", DEFAULT_STALE_MS, 1000, 10_000),
    ("SIMAI_CAMERA_HELPER_STALL_SECONDS", 30, 5, 300),
])
def test_setting_defaults_boundaries_and_invalid_values(caplog, name, default, minimum, maximum):
    assert bounded_setting({}, name, default, minimum, maximum) == default
    for value in (minimum, default, maximum):
        assert bounded_setting({name: str(value)}, name, default, minimum, maximum) == value
    for value in ("", "wrong-secret-value", "nan", "inf", "5.5", str(minimum - 1), str(maximum + 1)):
        assert bounded_setting({name: value}, name, default, minimum, maximum) == default
    assert len(caplog.records) == 7
    assert "wrong-secret-value" not in caplog.text


def test_load_env_preserves_setting_precedence(tmp_path, monkeypatch):
    config = tmp_path / "synthetic-settings.txt"
    config.write_text("SIMAI_CAMERA_HELPER_STALE_MS=6000\nSIMAI_CAMERA_HELPER_STALL_SECONDS=45\n")
    monkeypatch.setenv("SIMAI_CAMERA_HELPER_STALE_MS", "7000")
    monkeypatch.delenv("SIMAI_CAMERA_HELPER_STALL_SECONDS", raising=False)
    env = load_env(config)
    assert env["SIMAI_CAMERA_HELPER_STALE_MS"] == "7000"
    assert env["SIMAI_CAMERA_HELPER_STALL_SECONDS"] == "45"


@pytest.mark.parametrize("outcome", ["ok", "expired", "failed"])
def test_round_log_has_one_line_with_stable_camera_and_pair_order(caplog, outcome):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        log_round(10_000, 123, {cid: 10_000 - i * 1000 for i, cid in enumerate(CAMERAS)},
                  ["ENOUGH", "WEAK", "ENOUGH", "ENOUGH", "LACK", "UNKNOWN"], outcome)
    assert len(caplog.records) == 1
    assert caplog.records[0].getMessage() == (
        "guide round round_ts_ms=10000 match_ms=123 "
        "frame_age_ms=[cam_A:0 cam_B:1000 cam_C:2000 cam_D:3000] "
        f"grades=[E W E E L U] send={outcome}"
    )


@pytest.mark.parametrize("phase,missing,reason", [
    ("waiting", ["cam_B", "cam_D"], "reason=missing_frames missing=cam_B,cam_D"),
    ("waiting", [], "reason=unchanged_timestamps"),
    ("matching", [], "reason=matching elapsed_ms=30000"),
    ("posting", [], "reason=posting elapsed_ms=30000"),
    ("loading", [], "reason=loading elapsed_ms=30000"),
    ("send_failed", [], "reason=send_failed"),
])
def test_watchdog_warns_at_boundary_and_throttles(caplog, phase, missing, reason):
    now = [0.0]
    progress = Progress(clock=lambda: now[0])
    snapshot = {cid: (0, None) for cid in CAMERAS if cid not in missing}
    progress.phase(phase)
    progress.check(True, snapshot)
    now[0] = 29.999
    progress.check(True, snapshot)
    assert not caplog.records
    now[0] = 30
    progress.check(True, snapshot)
    assert caplog.records[0].getMessage() == f"guide stalled no_result_ms=30000 {reason}"
    now[0] = 30.5
    progress.check(True, snapshot)
    now[0] = 59.999
    progress.check(True, snapshot)
    assert len(caplog.records) == 1
    now[0] = 60
    progress.check(True, snapshot)
    assert len(caplog.records) == 2


def test_watchdog_only_success_resets_result_deadline_and_inactive_time_is_excluded(caplog):
    now = [0.0]
    progress = Progress(5, clock=lambda: now[0])
    snapshot = {cid: (0, None) for cid in CAMERAS}
    progress.check(False, snapshot)
    now[0] = 100
    progress.check(True, snapshot)
    now[0] = 104
    progress.finished("expired")
    progress.check(True, snapshot)
    assert not caplog.records
    now[0] = 105
    progress.check(True, snapshot)
    assert "reason=send_failed" in caplog.text
    progress.finished("ok")
    now[0] = 109.999
    progress.check(True, snapshot)
    assert len(caplog.records) == 1
    now[0] = 110
    progress.check(True, snapshot)
    assert len(caplog.records) == 2
    progress.check(False, snapshot)
    now[0] = 1000
    progress.check(False, snapshot)
    progress.check(True, snapshot)
    assert len(caplog.records) == 2
    now[0] = 1005
    progress.check(True, snapshot)
    assert len(caplog.records) == 3


def test_watchdog_thread_reports_matching_while_main_thread_is_blocked(caplog):
    now = [0.0]
    progress = Progress(clock=lambda: now[0])
    progress.check(True, {})
    progress.phase("matching")
    now[0] = 30
    active = threading.Event()
    active.set()
    stop = threading.Event()
    warned = threading.Event()

    class SignalHandler(logging.Handler):
        def emit(self, record):
            if "reason=matching" in record.getMessage():
                warned.set()

    handler = SignalHandler()
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    worker = threading.Thread(target=watchdog_loop, args=(progress, Frames(), active, stop))
    try:
        worker.start()
        assert warned.wait(2), "watchdog must run independently of the blocked main thread"
    finally:
        stop.set()
        worker.join(2)
        logger.removeHandler(handler)
    assert not worker.is_alive()
    assert "reason=matching elapsed_ms=30000" in caplog.text
