"""HttpTransport: TLS 강제, 토큰 헤더, 응답 해석, 리다이렉트 거부. 루프백 http 개발 서버로 확인한다(외부 접속 없음)."""

import contextlib
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services.worker.client import LeaseClient, RequestRejected
from services.worker.transport import TOKEN_HEADER, HttpTransport, TransportError


@pytest.mark.parametrize(
    "url",
    ["http://ec2.simai.dev", "ftp://ec2.simai.dev", "https://"],
)
def test_rejects_non_tls_or_bad_url(url):
    with pytest.raises(ValueError):
        HttpTransport(url, "token")


def test_rejects_empty_token():
    with pytest.raises(ValueError):
        HttpTransport("https://ec2.simai.dev", "")


@contextlib.contextmanager
def _serve(routes):
    """routes: 경로 → (상태, 본문, 추가 헤더). 받은 요청을 (메서드, 경로, 헤더, 본문)으로 기록한다."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            seen.append((self.command, self.path, dict(self.headers), json.loads(raw) if raw else None))
            code, reply, headers = routes.get(self.path, (404, b"", {}))
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        do_GET = do_POST = _handle  # noqa: N815

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_posts_json_with_bearer_token():
    routes = {
        "/lease": (204, b"", {}),
        "/heartbeat": (409, json.dumps({"msg_type": "job_lease_status"}).encode(), {}),
        "/html": (502, b"<html>bad gateway</html>", {}),
    }
    with _serve(routes) as (base, seen):
        t = HttpTransport(base + "/", "secret-token")
        assert t.post("/lease", {"msg_type": "job_lease_request", "한글": "값"}, 5.0).status == 204
        reply = t.post("/heartbeat", {}, 5.0)
        assert (reply.status, reply.body) == (409, {"msg_type": "job_lease_status"})
        assert t.post("/html", {}, 5.0).body is None  # JSON 이 아닌 본문은 None
    method, path, headers, body = seen[0]
    assert (method, path) == ("POST", "/lease")
    assert headers[TOKEN_HEADER] == "Bearer secret-token"
    assert headers["Content-Type"] == "application/json"
    assert body == {"msg_type": "job_lease_request", "한글": "값"}


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_redirect_is_not_followed_and_token_does_not_leak(code):
    """리다이렉트를 따라가면 토큰이 다른 호스트(여기서는 localhost 다른 포트)로 실린다. 따라가지 않는다."""
    with _serve({"/elsewhere": (200, b"{}", {})}) as (other, other_seen):
        target = other.replace("127.0.0.1", "localhost") + "/elsewhere"
        with _serve({"/api/worker/jobs/lease": (code, b"", {"Location": target})}) as (base, seen):
            transport = HttpTransport(base, "SECRET-TOKEN")
            assert transport.post("/api/worker/jobs/lease", {}, 5.0).status == code
            client = LeaseClient(transport, "worker-01", clock_ms=lambda: 0, monotonic=lambda: 0.0, sleep=lambda s: None)
            with pytest.raises(RequestRejected):  # 3xx 는 다시 보내지 않는 거부
                client.lease_once(["static_reconstruct"], 0)
    assert len(seen) == 2
    assert other_seen == []


def test_connection_refused_is_transport_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(TransportError):
        HttpTransport(f"http://127.0.0.1:{port}", "t").post("/lease", {}, 0.5)
