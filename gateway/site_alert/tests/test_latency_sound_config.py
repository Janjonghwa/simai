"""수신 → 소리 시작 지연(loopback), WAV 패턴, 설정."""

from __future__ import annotations

import sys
import wave
from pathlib import Path

import pytest

from simai_site_alert.bench import percentile, run_bench
from simai_site_alert.config import load_settings
from simai_site_alert.sound import CRITICAL, HIGH, PATTERNS, TEST, FakeSoundBackend, NullSoundBackend, SoundError, write_wav


def test_receive_to_sound_start_p95_under_budget() -> None:
    """부록 D-G0-04 '이벤트 → 소리 p95 300ms' 중 단말 몫. 실제 WebSocket 수신부터 백엔드 start() 반환까지."""
    result = run_bench(FakeSoundBackend(), count=100, interval_s=0.005)
    assert len(result.samples_ms) == 100
    assert result.p95_ms < 300


def test_percentile_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 95) == 95.0
    assert percentile([5.0], 95) == 5.0


def test_patterns_differ_by_severity() -> None:
    assert len({CRITICAL.tones, HIGH.tones, TEST.tones}) == 3
    assert set(PATTERNS) == {"CRITICAL", "HIGH", "TEST", "HIGH_ONCE", "CRITICAL_ONCE"}
    assert CRITICAL.period_ms < HIGH.period_ms * 2


def test_write_wav_length(tmp_path: Path) -> None:
    path = tmp_path / "high.wav"
    write_wav(path, HIGH, rate=8000)
    with wave.open(str(path)) as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        assert w.getnframes() == pytest.approx(8000 * HIGH.period_ms / 1000, abs=len(HIGH.tones))


def test_null_backend_always_fails() -> None:
    with pytest.raises(SoundError):
        NullSoundBackend().check()
    with pytest.raises(SoundError):
        NullSoundBackend().start(HIGH)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 가 아닐 때만")
def test_winsound_backend_fails_loudly_off_windows(tmp_path: Path) -> None:
    from simai_site_alert.sound import WinSoundBackend

    backend = WinSoundBackend(tmp_path)
    with pytest.raises(SoundError):
        backend.check()


def test_winsound_backend_prepares_wavs(tmp_path: Path) -> None:
    """WAV 를 미리 만들어 둔다(재생은 하지 않는다: 시험이 소리를 내지 않게)."""
    from simai_site_alert.sound import WinSoundBackend

    WinSoundBackend(tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["critical.wav", "critical_once.wav", "high.wav", "high_once.wav", "test.wav"]


SETTING_KEYS = ("SIMAI_LAPTOP_ID", "SIMAI_EC2_BASE_URL", "SIMAI_DEVICE_TOKEN", "SIMAI_TIER0_PINNED_SPKI_SHA256",
                "SIMAI_TIER0_CONTROL_PATH", "SIMAI_TIER0_CA_FILE")


def test_settings_from_env_file(tmp_path: Path, monkeypatch) -> None:
    for key in SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / ".env"
    pin = "sha256:" + "ab" * 32
    env.write_text(f"SIMAI_LAPTOP_ID=laptop-02\nSIMAI_EC2_BASE_URL=https://ec2.example\nSIMAI_DEVICE_TOKEN='s3cret'\n"
                   f"SIMAI_TIER0_PINNED_SPKI_SHA256={pin}\n", encoding="utf-8")
    s = load_settings(env)
    assert (s.laptop_id, s.ec2_base_url, s.device_token, s.pinned_spki, s.control_path, s.ca_file) == (
        "laptop-02", "https://ec2.example", "s3cret", pin, "/control", None)
    assert "s3cret" not in repr(s)
    monkeypatch.setenv("SIMAI_LAPTOP_ID", "laptop-09")
    assert load_settings(env).laptop_id == "laptop-09"


def test_settings_defaults(tmp_path: Path, monkeypatch) -> None:
    for key in SETTING_KEYS:
        monkeypatch.delenv(key, raising=False)
    s = load_settings(tmp_path / "missing.env")
    assert s.laptop_id == "laptop-01" and s.ec2_base_url is None and s.device_token is None
    assert s.pinned_spki is None and s.control_path == "/control"
