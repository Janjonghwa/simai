"""설정 읽기와 비밀 값: RTSP 계정은 .env 에서만 읽고 로그·오류·상태에 나오지 않는다."""

from __future__ import annotations

import base64
import json
import logging
import socket
import threading
import urllib.parse
from pathlib import Path

import av.logging
import pytest
from simai_recorder.clock import FakeClock
from simai_recorder.recorder import CameraSpec
from simai_recorder.sources import RtspConfig, RtspSource, SyntheticSource

from simai_relay import cli
from simai_relay.config import ConfigError, RedactSecrets, load_cameras, secret_values
from simai_relay.relay import CameraRelay
from simai_relay.sink import MemorySink

USER = "relay-user-S3CRET"
PASSWORD = "p@ss:w/rd-S3CRET"


def _secret_forms() -> list[str]:
    return ["S3CRET", urllib.parse.quote(PASSWORD, safe=""),
            base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()]


class FakeRtspServer:
    """RTSP 서버 흉내: 첫 요청에 401(Basic)을 돌려 클라이언트가 계정을 싣게 하고, 그다음 요청에서 연결을 끊는다."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen()
        self.port = self._sock.getsockname()[1]
        self.requests: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(2)
                try:
                    first = conn.recv(4096)
                    self.requests.append(first)
                    cseq = next((ln.split(b":", 1)[1].strip() for ln in first.split(b"\r\n")
                                 if ln.lower().startswith(b"cseq")), b"1")
                    conn.sendall(b"RTSP/1.0 401 Unauthorized\r\nCSeq: " + cseq +
                                 b"\r\nWWW-Authenticate: Basic realm=\"cam\"\r\n\r\n")
                    self.requests.append(conn.recv(4096))
                except OSError:
                    pass

    def close(self) -> None:
        self._sock.close()


def test_rtsp_failure_never_logs_credentials(caplog, capfd):
    server = FakeRtspServer()
    av.logging.set_level(av.logging.DEBUG)  # FFmpeg 로그까지 켜도 계정이 나오지 않아야 한다
    try:
        caplog.set_level(logging.DEBUG)
        clock = FakeClock()
        source = RtspSource(RtspConfig("127.0.0.1", port=server.port, user=USER, password=PASSWORD,
                                       open_timeout_s=2, read_timeout_s=2))
        relay = CameraRelay(CameraSpec("cam_A", source), MemorySink("cam_A"), clock, threading.Event(),
                            clock.monotonic() + 1.5, reconnect_delays_s=(1.0,))
        relay.run()
    finally:
        av.logging.set_level(None)
        server.close()

    assert relay.error is None
    assert relay.stats.snapshot()["open_failures"] >= 1
    assert any(b"Authorization" in r for r in server.requests), "클라이언트가 계정을 싣는 경로까지 가지 않았다"
    out, err = capfd.readouterr()
    exposed = "\n".join([caplog.text, out, err, json.dumps(relay.stats.snapshot()), json.dumps(source.describe()),
                         repr(source), repr(relay.spec)])
    assert "open_failed" in caplog.text
    for form in _secret_forms():
        assert form not in exposed


def test_redact_filter_masks_plain_and_url_encoded_values():
    records: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(self.format(record))

    handler = ListHandler()
    handler.addFilter(RedactSecrets(secret_values({"RTSP_USER": USER, "RTSP_PASS_CAM_A": PASSWORD, "OTHER": "x"})))
    logger = logging.getLogger("simai_relay.test_redact")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("url rtsp://%s:%s@host/stream1", urllib.parse.quote(USER, safe=""),
                       urllib.parse.quote(PASSWORD, safe=""))
        logger.warning("raw %s", PASSWORD)
    finally:
        logger.removeHandler(handler)
    assert records == ["url rtsp://***:***@host/stream1", "raw ***"]


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "relay.toml"
    path.write_text(text, encoding="utf-8")
    return path


CONFIG = """
[capture_time]
latency_ms = 15

[[cameras]]
camera_id = "cam_A"
kind = "rtsp"
host = "192.168.0.11"

[[cameras]]
camera_id = "cam_B"
kind = "synthetic"
width = 64
height = 48
capture_time = { uncertainty_ms = 25 }
"""


def test_load_cameras_reads_credentials_only_from_env(tmp_path):
    env = {"RTSP_USER": USER, "RTSP_PASS": PASSWORD}
    specs = load_cameras(_write(tmp_path, CONFIG), FakeClock(), env)
    assert [s.camera_id for s in specs] == ["cam_A", "cam_B"]
    assert isinstance(specs[0].source, RtspSource) and isinstance(specs[1].source, SyntheticSource)
    assert specs[0].capture_time.latency_ms == 15 and specs[1].capture_time.uncertainty_ms == 25
    assert specs[0].source.describe() == {"kind": "RTSP", "url": "rtsp://192.168.0.11:554/stream1"}


def test_missing_credentials_error_names_keys_only(tmp_path):
    with pytest.raises(ConfigError) as e:
        load_cameras(_write(tmp_path, CONFIG), FakeClock(), {"RTSP_USER": USER})
    assert "RTSP_PASS" in str(e.value) and "S3CRET" not in str(e.value)


def test_duplicate_camera_id_is_rejected(tmp_path):
    text = CONFIG.replace('camera_id = "cam_B"', 'camera_id = "cam_A"')
    with pytest.raises(ConfigError):
        load_cameras(_write(tmp_path, text), FakeClock(), {"RTSP_USER": USER, "RTSP_PASS": PASSWORD})


def test_shipped_configs_load(tmp_path):
    root = Path(__file__).resolve().parents[1] / "configs"
    env = {"RTSP_USER": USER, "RTSP_PASS": PASSWORD}
    assert len(load_cameras(root / "example.toml", FakeClock(), env)) == 4
    assert len(load_cameras(root / "dry-run.toml", FakeClock(), {})) == 4


def test_cli_run_dry(tmp_path, capfd):
    config = _write(tmp_path, '[[cameras]]\ncamera_id = "cam_A"\nkind = "synthetic"\nwidth = 64\nheight = 48\n')
    env_file = tmp_path / ".env"
    env_file.write_text(f"RTSP_USER={USER}\nRTSP_PASS={PASSWORD}\n", encoding="utf-8")
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    try:
        code = cli.main(["run", "--config", str(config), "--seconds", "0.6", "--env-file", str(env_file),
                         "--report-interval", "0.3"])
    finally:
        root.handlers[:], _ = saved
        root.setLevel(saved[1])
    assert code == 0
    _, err = capfd.readouterr()
    assert "stream_started" in err and '"camera_id": "cam_A"' in err
    assert "S3CRET" not in err
