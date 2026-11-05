from simai_site_alert.bench import local_alert, uuid7
from simai_site_alert.sound import HIGH
from link_helpers import health


def test_once_survives_tick_update_and_health(core, sound, clock):
    msg = local_alert(uuid7(), 1, "RAISED", severity="HIGH")
    msg["payload"]["sound_mode"] = "ONCE"
    core.handle(msg)
    assert sound.calls == [("start", "HIGH_ONCE")]
    clock.advance((HIGH.period_ms + 1) / 1000)
    core.tick()
    core.handle(msg)
    assert [c for c in sound.calls if c[0] == "start"] == [("start", "HIGH_ONCE")]


def test_silent_shows_and_legacy_repeats(core, sound):
    msg = local_alert(uuid7(), 1, "RAISED")
    msg["payload"]["sound_mode"] = "SILENT"
    core.handle(msg)
    assert not any(c[0] == "start" for c in sound.calls)
    assert len(core.screen_state().alerts) == 1
    core.handle(local_alert(uuid7(), 1, "RAISED"))
    assert sound.calls[-1] == ("start", "CRITICAL")


def test_health_keeps_once_consumed_and_recovers_missed_alert(core, sound, clock):
    msg = local_alert(uuid7(), 1, "RAISED", severity="HIGH")
    item = {k: v for k, v in msg["payload"].items() if k in (
        "event_id", "event_sequence", "source", "severity", "summary", "acknowledged", "rule_id", "zone_id"
    )}
    item["sound_mode"] = "ONCE"
    snapshot = health(clock.now_ms(), sent_at=clock.now_ms(), alerts=[item])
    core.handle(snapshot)
    clock.advance(2)
    core.tick()
    core.handle(snapshot)
    assert [c for c in sound.calls if c[0] == "start"] == [("start", "HIGH_ONCE")]
    assert core.alarm_status()["sounding_event_ids"] == []
    assert core.alarm_status()["displayed_event_ids"] == [item["event_id"]]


def test_once_failure_retries_and_unknown_mode_is_repeat(core, sound):
    msg = local_alert(uuid7(), 1, "RAISED", severity="HIGH")
    msg["payload"]["sound_mode"] = "ONCE"
    sound.fail_start = True
    core.handle(msg)
    sound.fail_start = False
    core.tick()
    assert sound.calls[-1] == ("start", "HIGH_ONCE")
    msg = local_alert(uuid7(), 1, "RAISED")
    msg["payload"]["sound_mode"] = "FUTURE_MODE"
    core.handle(msg)
    assert sound.calls[-1] == ("start", "CRITICAL")


def test_windows_once_omits_loop_flag(tmp_path, monkeypatch):
    from simai_site_alert.sound import HIGH_ONCE, WinSoundBackend
    class FakeWinSound:
        SND_FILENAME, SND_ASYNC, SND_LOOP, SND_NODEFAULT = 1, 2, 4, 8
        calls = []
        def PlaySound(self, path, flags):
            self.calls.append(flags)
    ws = FakeWinSound()
    monkeypatch.setattr(WinSoundBackend, "_winsound", staticmethod(lambda: ws))
    backend = WinSoundBackend(tmp_path)
    backend.start(HIGH_ONCE)
    backend.start(HIGH)
    assert not ws.calls[0] & ws.SND_LOOP
    assert ws.calls[1] & ws.SND_LOOP
