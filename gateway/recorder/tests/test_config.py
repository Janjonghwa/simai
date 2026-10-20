from __future__ import annotations

import time
from pathlib import Path

import pytest

from simai_recorder.clock import FakeClock
from simai_recorder.config import ConfigError, credentials, default_session_id, load_session
from simai_recorder.sources import RtspConfig, RtspSource, SourceError

ENV = {"RTSP_USER": "simai", "RTSP_PASS": "p@ss:word", "RTSP_PASS_CAM_B": "other"}

RTSP = """
[session]
scenario = "empty_room_10min"
storage_uri = "{storage}"
{consent}
[[cameras]]
camera_id = "cam_A"
kind = "rtsp"
host = "192.168.0.11"
firmware = "1.3.2"

[[cameras]]
camera_id = "cam_B"
kind = "rtsp"
host = "192.168.0.12"
"""

EMPTY_ROOM = """
[consent]
contains_people = false
retention_until = "2027-01-31"
"""


def _write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "session.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _load(tmp_path: Path, consent: str = EMPTY_ROOM, storage: str = "volume://AP-JP-1/simai/recordings",
          env: dict | None = None):
    return load_session(_write(tmp_path, RTSP.format(consent=consent, storage=storage)), FakeClock(),
                        ENV if env is None else env)


def test_rtsp_session_loads_without_leaking_credentials(tmp_path):
    spec = _load(tmp_path)
    assert spec.session_id == "rec-20251013-080000z-empty-room-10min"
    assert [c.camera_id for c in spec.cameras] == ["cam_A", "cam_B"]
    assert spec.cameras[0].firmware == "1.3.2" and spec.cameras[1].firmware is None
    desc = spec.cameras[0].source.describe()
    assert desc == {"kind": "RTSP", "url": "rtsp://192.168.0.11:554/stream1"}
    assert "simai" not in repr(spec.cameras[0].source.__dict__)


def test_per_camera_credentials_override_common():
    assert credentials(ENV, "cam_A") == ("simai", "p@ss:word")
    assert credentials(ENV, "cam_B") == ("simai", "other")
    with pytest.raises(ConfigError, match="RTSP_USER"):
        credentials({}, "cam_A")


def test_rtsp_url_escapes_credentials():
    cfg = RtspConfig(host="10.0.0.5", user="simai", password="p@ss:word")
    assert cfg.url(with_credentials=True) == "rtsp://simai:p%40ss%3Aword@10.0.0.5:554/stream1"
    assert cfg.url(with_credentials=False) == "rtsp://10.0.0.5:554/stream1"


def test_missing_credentials_refuses_to_start(tmp_path):
    with pytest.raises(ConfigError, match="RTSP_USER"):
        _load(tmp_path, env={})


@pytest.mark.parametrize(
    ("consent", "message"),
    [
        ("", "consent"),
        ("[consent]\ncontains_people = false\n", "retention_until"),
        ("[consent]\ncontains_people = true\nretention_until = \"2027-01-31\"\n", "consent_ref"),
        ("[consent]\ncontains_people = true\nretention_until = \"2027-01-31\"\nconsent_ref = \"CF-1\"\n", "scope"),
    ],
)
def test_rtsp_recording_requires_consent_and_retention(tmp_path, consent, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, consent=consent)


def test_people_recording_with_full_consent_loads(tmp_path):
    consent = """
[consent]
contains_people = true
consent_ref = "CF-2026-10-001"
scope = ["dataset_internal", "runpod_ap_jp_1"]
retention_until = "2027-01-31"
"""
    assert _load(tmp_path, consent=consent).consent["consent_ref"] == "CF-2026-10-001"


def test_local_storage_path_is_rejected(tmp_path):
    with pytest.raises(ConfigError, match="storage_uri"):
        _load(tmp_path, storage="C:/Users/someone/videos")


def test_faults_are_not_allowed_on_real_cameras(tmp_path):
    text = RTSP.format(consent=EMPTY_ROOM, storage="volume://AP-JP-1/x") + "faults = { jitter_ms = 10 }\n"
    with pytest.raises(ConfigError, match="faults"):
        load_session(_write(tmp_path, text), FakeClock(), ENV)


def test_default_session_id_is_utc_and_slugged():
    assert default_session_id(FakeClock(1_760_342_400_000), "Walk Calibration!") == (
        "rec-20251013-080000z-walk-calibration"
    )


def test_rtsp_connection_failure_is_source_error():
    """닫힌 포트에 접속하면 SourceError 로 알리고 계정은 메시지에 넣지 않는다(재접속 루프가 이어받는다)."""
    source = RtspSource(RtspConfig(host="127.0.0.1", port=9, user="simai", password="secret", open_timeout_s=2,
                                   read_timeout_s=2))
    t = time.monotonic()
    with pytest.raises(SourceError) as e:
        source.open()
    assert time.monotonic() - t < 10
    assert "secret" not in str(e.value) and "simai:" not in str(e.value)
    source.close()


DEV_TEST = '[consent]\ncontains_people = true\npurpose = "dev_test"\n{retention}'


def test_dev_test_people_recording_loads_without_consent_ref(tmp_path):
    consent = DEV_TEST.format(retention='retention_until = "2027-01-31"\n')
    spec = _load(tmp_path, consent=consent)
    assert spec.consent["purpose"] == "dev_test" and "consent_ref" not in spec.consent


@pytest.mark.parametrize(
    ("consent", "message"),
    [
        (DEV_TEST.format(retention=""), "retention_until"),  # dev_test 도 보관 기한은 필수
        ('[consent]\ncontains_people = true\npurpose = "dataset"\nretention_until = "2027-01-31"\n', "consent_ref"),
        ('[consent]\ncontains_people = true\npurpose = "dataset"\nconsent_ref = "CF-1"\nretention_until = "2027-01-31"\n',
         "scope"),
        ('[consent]\ncontains_people = true\nretention_until = "2027-01-31"\n', "consent_ref"),  # 용도 없음 = 기존 동작
        ('[consent]\ncontains_people = true\npurpose = "demo"\nretention_until = "2027-01-31"\n', "purpose"),
    ],
)
def test_purpose_does_not_loosen_other_recordings(tmp_path, consent, message):
    with pytest.raises(ConfigError, match=message):
        _load(tmp_path, consent=consent)


def test_empty_room_without_purpose_is_unchanged(tmp_path):
    assert "purpose" not in _load(tmp_path).consent
