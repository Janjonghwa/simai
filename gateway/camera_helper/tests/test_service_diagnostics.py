import logging
import threading
from pathlib import Path

import pytest

from simai_camera_helper import matcher, service
from simai_camera_helper.judge import CAMERAS, PAIRS, PairMetrics


@pytest.mark.parametrize("outcome", ["ok", "expired", "failed", "match_error", "demand_off"])
def test_service_round_logs_actual_outcome_and_configured_freshness(monkeypatch, caplog, outcome):
    stop = threading.Event()
    wall_ns = [1_793_690_000_000 * 1_000_000]
    frame_ts = wall_ns[0] // 1_000_000 - 5500
    frames = service.Frames()
    frames.items = {cid: (frame_ts, "image") for cid in CAMERAS}
    posted = []
    active_events = []

    def fake_demand_loop(client, active, stop_event):
        active_events.append(active)
        active.set()

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, message):
            posted.append(message)
            stop.set()
            if outcome == "failed":
                raise OSError("remote-body-must-not-appear")
            return {"active_demand": True}

    class FakeMatcher:
        def __init__(self, aliked, lightglue, params, intrinsics, *, device):
            self.params = params

        def match(self, images):
            assert set(images) == set(CAMERAS)
            if outcome == "match_error":
                raise RuntimeError("match-error-must-not-appear")
            if outcome == "expired":
                wall_ns[0] += 11_000 * 1_000_000
                stop.set()
            if outcome == "demand_off":
                active_events[0].clear()
                stop.set()
            return [PairMetrics(a, b, 100, 40) for a, b in PAIRS]

    monkeypatch.setattr(service, "Client", FakeClient)
    monkeypatch.setattr(service, "Frames", lambda: frames)
    monkeypatch.setattr(service, "demand_loop", fake_demand_loop)
    monkeypatch.setattr(service, "preview_loop", lambda *args: None)
    monkeypatch.setattr(service, "resolve_weights", lambda env: (Path("a"), Path("b")))
    monkeypatch.setattr(service.time, "time_ns", lambda: wall_ns[0])
    monkeypatch.setattr(matcher, "CameraMatcher", FakeMatcher)
    env = {"SIMAI_CAMERA_HELPER_STALE_MS": "6000", "SIMAI_CAMERA_HELPER_STALL_SECONDS": "5"}
    with caplog.at_level(logging.INFO, logger=service.LOG.name):
        if outcome == "match_error":
            with pytest.raises(RuntimeError):
                service.run(env, stop=stop)
        else:
            service.run(env, stop=stop)

    rounds = [r.getMessage() for r in caplog.records if r.getMessage().startswith("guide round ")]
    assert len(rounds) == 1
    expected = "failed" if outcome in ("match_error", "demand_off") else outcome
    assert rounds[0].endswith(f"send={expected}")
    assert "frame_age_ms=[cam_A:5500 cam_B:5500 cam_C:5500 cam_D:5500]" in rounds[0]
    assert "match_ms=" in rounds[0]
    assert "grades=[U U U U U U]" in rounds[0]  # First round retains existing grade hysteresis.
    assert "must-not-appear" not in caplog.text
    if outcome in ("ok", "failed"):
        assert all(c["status"] == "OK" for c in posted[0]["cameras"])
    else:
        assert not posted
