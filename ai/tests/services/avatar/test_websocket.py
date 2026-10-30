"""운영 WebSocket 연결 인자와 실제 라이브러리 HTTP 거절 예외를 네트워크 없이 검증한다."""

import sys
from pathlib import Path

import pytest

pytest.importorskip("websockets")
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response
from websockets.datastructures import Headers
from websockets.sync import client

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.avatar.transport import AvatarSender, websocket_connector


@pytest.mark.parametrize("status", [502, 401, 403])
def test_handshake_rejection_is_retried_and_status_is_logged(monkeypatch, caplog, status):
    now, attempts = [0.], []
    def reject(url, **kwargs):
        attempts.append(now[0])
        assert url == "wss://ec2.test/ws/worker/avatar"
        assert kwargs["additional_headers"] == {"Authorization": "Bearer private-token"}
        assert kwargs["open_timeout"] == 10 and kwargs["close_timeout"] == 1
        assert kwargs["ping_interval"] == kwargs["ping_timeout"] == 10
        raise InvalidStatus(Response(status, "Rejected", Headers()))
    monkeypatch.setattr(client, "connect", reject)
    sender = AvatarSender(websocket_connector("https://ec2.test", "private-token"),
                          clock_ms=lambda: 1000, monotonic=lambda: now[0])
    for second in range(8):
        now[0] = float(second)
        sender.tick()
    assert attempts == [0., 1., 3., 7.]
    assert str(status) in caplog.text and "InvalidStatus" in caplog.text
    assert "private-token" not in caplog.text
