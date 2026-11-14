"""외부 네트워크 없이 서버 종료·재시작과 인증/연결 실패를 재현한다."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from services.avatar import transport
from services.avatar.transport import AvatarSender, AvatarUpload
from simai_ai.hmr.diagnostics import exception_summary, is_upload_conflict
from simai_ai.hmr.stream import AvatarLoop
from tests.hmr.conftest import make_rig
from tests.hmr.test_avatar_loop import Model, Socket, snapshot


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


@pytest.mark.parametrize("file_name", ["albedo.png", "surface_source.png"])
def test_png_uses_distinct_signed_versioned_key_and_exact_bytes(monkeypatch, file_name):
    posts, puts = [], []
    uri = f"s3://avatars/w-1/e1000/v2/{file_name}"
    def post(path, body, timeout):
        posts.append(body)
        return SimpleNamespace(status=200, body={"upload_url": "https://storage.test/put",
            "method": "PUT", "object_uri": uri, "headers": {"Content-Type": "image/png"}})
    monkeypatch.setattr(transport, "HttpTransport", lambda *args: SimpleNamespace(post=post))
    class Result:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def put(request, timeout):
        puts.append(request)
        return Result()
    monkeypatch.setattr(transport, "build_opener", lambda *args: SimpleNamespace(open=put))
    asset = SimpleNamespace(data=b"png-bytes", size_bytes=9, sha256="sha256:" + "a" * 64)
    upload = AvatarUpload("https://ec2.test", "private-worker-token")
    assert upload.png("w-1", 1000, 2, file_name, asset) == uri
    assert posts == [{"purpose": "AVATAR_ASSET", "world_id": "w-1", "stream_epoch": 1000,
        "appearance_version": 2, "content_type": "image/png", "size_bytes": 9, "sha256": "a" * 64,
        "file_name": file_name}]
    assert puts[0].data == asset.data and not puts[0].has_header("Authorization")
    with pytest.raises(ValueError, match="PNG 이름"):
        upload.png("w-1", 1000, 2, "../surface.png", asset)


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


def test_upload_same_hash_is_idempotent_and_409_conflict_advances_version_in_loop(monkeypatch):
    grants = {}
    sign_versions = []
    put_failures = [True]

    def post(path, body, timeout):
        assert path == "/api/uploads/sign"
        leaf = body.get("file_name", "mesh.glb")
        key = f"avatars/{body['world_id']}/e{body['stream_epoch']}/v{body['appearance_version']}/{leaf}"
        sign_versions.append(body["appearance_version"])
        fingerprint = (body["content_type"], body["size_bytes"], body["sha256"])
        if key in grants and grants[key] != fingerprint:
            return SimpleNamespace(status=409, body={"code": "UPLOAD_KEY_CONFLICT"})
        grants[key] = fingerprint
        return SimpleNamespace(
            status=200,
            body={
                "object_uri": f"s3://bucket/{key}",
                "method": "PUT",
                "upload_url": f"https://storage.test/{key}?sig=private",
                "headers": {"Content-Type": body["content_type"]},
            },
        )

    class Result:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def open_request(request, timeout):
        if put_failures and put_failures.pop(0):
            raise OSError("일시적 PUT 실패")
        return Result()

    monkeypatch.setattr(transport, "HttpTransport", lambda base, token: SimpleNamespace(post=post))
    monkeypatch.setattr(transport, "build_opener", lambda *args: SimpleNamespace(open=open_request))

    upload = AvatarUpload("https://ec2.test", "private-worker-token")
    glb_same = SimpleNamespace(data=b"glb-a", size_bytes=5, sha256="sha256:" + "a" * 64)
    with pytest.raises(OSError):
        upload("w-0", 1000, 0, glb_same)
    # 같은 해시·크기 재요청은 같은 버전(v0)으로 멱등 서명되어 성공한다.
    assert upload("w-0", 1000, 0, glb_same) == "s3://bucket/avatars/w-0/e1000/v0/mesh.glb"
    assert sign_versions == [0, 0]

    # 이미 v0 에 다른 해시가 등록된 상태(Session 15 재현): 첫 회차는 409 로 실패하되 pending_version 이 1 로 전진한다.
    sign_versions.clear()
    grants["avatars/w-1/e1000/v0/mesh.glb"] = ("model/gltf-binary", 999, "0" * 64)
    rig = make_rig()
    clock = [1000]
    source = SimpleNamespace(item=snapshot(clock[0]))
    source.latest = lambda: source.item
    sock = Socket()
    sender = AvatarSender(lambda: sock, clock_ms=lambda: clock[0], monotonic=lambda: clock[0] / 1000, stream_epoch=1000)
    sender.tick()
    gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=True))
    model = Model(rig)
    loop = AvatarLoop(source, gate, model, upload, sender, clock_ms=lambda: clock[0], freeze_frames=1)

    with pytest.raises(ValueError, match="HTTP=409") as exc_info:
        loop.step()
    assert is_upload_conflict(exc_info.value)
    assert loop.people["w-1"].pending_version == 1

    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert sign_versions == [0, 1]
    assert loop.people["w-1"].version == 1
    assert loop.people["w-1"].asset["previous_appearance_version"] == 0

    # 트랙이 LOST 로 빠졌다가 같은 stream_epoch 에 다시 들어와도 v0 으로 돌아가지 않고 v2 로 진행한다.
    clock[0] += 200
    empty_meta, empty_imgs = snapshot(clock[0])
    empty_meta["world"]["tracks"] = []
    source.item = (empty_meta, empty_imgs)
    loop.step()
    assert "w-1" not in loop.people

    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert sign_versions == [0, 1, 2]
    assert loop.people["w-1"].version == 2
    assert loop.people["w-1"].asset["previous_appearance_version"] == 1

