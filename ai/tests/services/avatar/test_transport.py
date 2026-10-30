"""외부 네트워크 없이 서버 종료·재시작과 인증/연결 실패를 재현한다."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.avatar import transport
from services.avatar.transport import AvatarSender, AvatarUpload
from simai_ai.hmr.diagnostics import exception_summary


class FakeWebSocketServer:
    def __init__(self):
        self.running = True
        self.connections = []
        self.attempts = 0

    def connect(self):
        self.attempts += 1
        if not self.running:
            raise ConnectionRefusedError("test server stopped")
        sock = FakeWebSocket(self)
        self.connections.append(sock)
        return sock

    def shutdown(self):
        self.running = False
        for sock in self.connections:
            sock.closed = True


class FakeWebSocket:
    def __init__(self, server):
        self.server = server
        self.closed = False
        self.messages = []

    def recv(self, timeout):
        assert timeout == 0
        if self.closed:
            raise ConnectionResetError("test server restart")
        raise TimeoutError

    def send(self, raw):
        if self.closed:
            raise ConnectionResetError("test network disconnected")
        self.messages.append(json.loads(raw))

    def close(self):
        self.closed = True


def test_idle_server_restart_then_asset_reannouncement_and_fresh_pose():
    now = [0.]
    server = FakeWebSocketServer()
    sender = AvatarSender(server.connect, clock_ms=lambda: 1000, monotonic=lambda: now[0])
    sender.tick()
    server.shutdown()
    sender.tick()  # 아직 자산이 없어도 끊김을 확인해야 한다.
    now[0] = 1.
    sender.tick()  # 서버가 내려가 있으므로 연결 거부, 다음 시도는 2초 뒤.
    assert server.attempts == 2
    server.running = True
    sender.asset({"world_id": "w-1"})
    now[0] = 2.
    sender.tick()
    assert server.attempts == 2
    now[0] = 3.
    sender.tick()
    assert server.attempts == 3
    second = server.connections[-1]
    assert [m["msg_type"] for m in second.messages] == ["avatar_asset"]
    sender.poses([{"world_id": "w-1", "capture_time_ms": 1000}])
    sender.tick()
    assert second.messages[-1]["msg_type"] == "avatar_pose_batch"
    server.shutdown()
    sender.tick()
    now[0] = 4.
    server.running = True
    sender.tick()
    assert [m["msg_type"] for m in server.connections[-1].messages] == ["avatar_asset"]
    sender.poses([{"world_id": "w-1", "capture_time_ms": 1000}])
    sender.tick()
    assert server.connections[-1].messages[-1]["payload"]["batch_sequence"] == 1


@pytest.mark.parametrize("failure", [ConnectionRefusedError, PermissionError, OSError])
def test_connection_failures_back_off_and_recover(failure):
    now, attempts = [0.], []
    server = FakeWebSocketServer()
    def connect():
        attempts.append(now[0])
        if len(attempts) < 4:
            raise failure("test connection failure")
        return server.connect()
    sender = AvatarSender(connect, clock_ms=lambda: 1000, monotonic=lambda: now[0])
    for second in range(8):
        now[0] = float(second)
        sender.tick()
    assert attempts == [0., 1., 3., 7.]


def test_upload_sign_and_put_match_server_fields_and_never_forward_worker_token(monkeypatch, caplog):
    caplog.set_level("INFO")
    uri = "s3://bucket/avatars/w-1/e1000/v0/mesh.glb"
    signed = {"object_uri": uri, "method": "PUT", "upload_url": "https://storage.test/mesh?sig=private",
              "headers": {"Content-Type": "model/gltf-binary", "x-amz-checksum-sha256": "checksum"}}
    posts, puts = [], []
    def post(path, body, timeout):
        posts.append((path, body, timeout))
        return SimpleNamespace(status=200, body=signed)
    monkeypatch.setattr(transport, "HttpTransport", lambda base, token: SimpleNamespace(post=post))
    class Result:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_request(request, timeout):
        puts.append((request, timeout))
        return Result()
    monkeypatch.setattr(transport, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    upload = AvatarUpload("https://ec2.test", "private-worker-token")
    glb = SimpleNamespace(data=b"glb", size_bytes=3, sha256="sha256:" + "a" * 64)
    assert upload("w-1", 1000, 0, glb) == uri
    assert posts == [("/api/uploads/sign", {"purpose": "AVATAR_ASSET", "world_id": "w-1",
        "stream_epoch": 1000, "appearance_version": 0, "content_type": "model/gltf-binary",
        "size_bytes": 3, "sha256": "a" * 64}, 15)]
    request, timeout = puts[0]
    assert request.method == "PUT" and request.data == b"glb" and timeout == 30
    assert dict((k.lower(), v) for k, v in request.header_items()) == {
        "content-type": "model/gltf-binary", "x-amz-checksum-sha256": "checksum"}
    assert "업로드 성공" in caplog.text
    assert "private" not in caplog.text and "https://" not in caplog.text


def test_upload_failure_logs_status_and_preserves_exception(monkeypatch, caplog):
    monkeypatch.setattr(transport, "HttpTransport", lambda *args: SimpleNamespace(
        post=lambda *args: SimpleNamespace(status=403, body=None)))
    with pytest.raises(ValueError, match="HTTP=403"):
        AvatarUpload("https://ec2.test", "private-worker-token")("w-1", 1000, 0, SimpleNamespace(
            size_bytes=3, sha256="sha256:" + "a" * 64))
    assert "업로드 실패" in caplog.text and "ValueError" in caplog.text and "HTTP=403" in caplog.text


def test_exception_summary_keeps_type_and_message_on_one_line_without_credentials():
    error = ValueError("shape mismatch\nhttps://host.test/a?signature=secret "
                       "Authorization: Bearer abc token='def' password=ghi private-worker-token")
    result = exception_summary(error, secrets=("private-worker-token",))
    assert result.startswith("ValueError: shape mismatch") and "\n" not in result
    for secret in ("https://", "secret", "abc", "def", "ghi", "private-worker-token"):
        assert secret not in result


def test_minute_batch_count_counts_successful_sends_only_and_logs_zero_during_retry(caplog):
    caplog.set_level("INFO")
    now = [0.]
    server = FakeWebSocketServer()
    sender = AvatarSender(server.connect, clock_ms=lambda: 1000, monotonic=lambda: now[0])
    sender.tick()
    sender.poses([{"world_id": "w-1", "capture_time_ms": 1000}])
    sender.tick()
    now[0] = 60.
    sender.tick()
    assert "pose_batches=1" in caplog.text
    caplog.clear()
    server.shutdown()
    sender.tick()
    now[0] = 120.
    sender.tick()
    assert "pose_batches=0" in caplog.text and "connected=False" in caplog.text
