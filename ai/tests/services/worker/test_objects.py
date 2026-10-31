"""HttpObjectStore: 워커 전용 서명 API(S15P31A101-248)로 주소를 받고 서명 주소로 주고받는다.

루프백 http 서버 하나가 EC2 서명 API 와 객체 저장소를 함께 흉내 낸다(외부 접속 없음).
"""

import contextlib
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from services.worker.handlers import JobContext
from services.worker.objects import HttpObjectStore, ObjectStoreError
from services.worker.transport import HttpTransport, Response, TransportError

JOB = "job-1"
SIGN_PATH = f"/api/worker/jobs/{JOB}/objects/sign"


@contextlib.contextmanager
def _server(sign_status=200, storage_status=200, storage_body=b"calibration-bytes", sign_code="LEASE_NOT_ACTIVE"):
    """서명 요청은 같은 서버의 /bucket/<키> 주소를 돌려준다. 받은 요청을 (메서드, 경로, 헤더, 본문)으로 기록한다."""
    seen = []
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def _body(self):
            length = int(self.headers.get("Content-Length") or 0)
            return self.rfile.read(length) if length else b""

        def _reply(self, code, body, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            raw = self._body()
            seen.append(("POST", self.path, dict(self.headers), raw))
            if self.path != SIGN_PATH:
                return self._reply(404, b"")
            if sign_status != 200:
                return self._reply(sign_status, json.dumps({"code": sign_code}).encode())
            req = json.loads(raw)
            base = f"http://127.0.0.1:{state['port']}"
            if req["op"] == "GET":
                key = req["uri"].removeprefix("s3://bucket/")
                reply = {"op": "GET", "uri": req["uri"], "method": "GET", "url": f"{base}/bucket/{key}?sig=secret",
                         "headers": {}, "expires_at_ms": 1}
            else:
                key = f"floor-plans/{JOB}/{req['name']}"
                reply = {"op": "PUT", "uri": f"s3://bucket/{key}", "method": "PUT", "url": f"{base}/bucket/{key}?sig=secret",
                         "headers": {"content-type": req["content_type"], "content-length": str(req["size_bytes"]),
                                     "x-amz-checksum-sha256": "c2lnbmVk"},
                         "expires_at_ms": 1}
            self._reply(200, json.dumps(reply).encode())

        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path, dict(self.headers), b""))
            self._reply(storage_status, storage_body, "application/octet-stream")

        def do_PUT(self):  # noqa: N802
            seen.append(("PUT", self.path, dict(self.headers), self._body()))
            self._reply(storage_status, b"" if storage_status == 200 else storage_body, "application/xml")

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{state['port']}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()


def _ctx():
    return JobContext(job_id=JOB, lease_id="lease-1", job_type="bev_floorplan", attempt=1, input={})


def test_put_signs_with_lease_hash_and_size_then_puts_with_signed_headers():
    content = b"\x89PNG fake image"
    with _server() as (url, seen):
        store = HttpObjectStore(HttpTransport(url, "worker-token"))
        uri = store.put(_ctx(), "bev.png", content, "image/png")

    assert uri == f"s3://bucket/floor-plans/{JOB}/bev.png"
    (_, path, headers, raw), (method, put_path, put_headers, put_body) = seen
    assert path == SIGN_PATH and headers["Authorization"] == "Bearer worker-token"
    assert json.loads(raw) == {"lease_id": "lease-1", "op": "PUT", "name": "bev.png", "content_type": "image/png",
                               "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    assert (method, put_path) == ("PUT", f"/bucket/floor-plans/{JOB}/bev.png?sig=secret")
    assert put_body == content
    assert put_headers["Content-Type"] == "image/png"
    assert put_headers["X-Amz-Checksum-Sha256"] == "c2lnbmVk"
    assert "Authorization" not in put_headers, "워커 토큰을 저장소에 보내지 않는다"


def test_get_signs_input_uri_and_returns_bytes():
    with _server() as (url, seen):
        store = HttpObjectStore(HttpTransport(url, "worker-token"))
        data = store.get(_ctx(), "s3://bucket/calibrations/v7/calibration.json")

    assert data == b"calibration-bytes"
    assert json.loads(seen[0][3]) == {"lease_id": "lease-1", "op": "GET", "uri": "s3://bucket/calibrations/v7/calibration.json"}
    assert seen[1][:2] == ("GET", "/bucket/calibrations/v7/calibration.json?sig=secret")


@pytest.mark.parametrize(("status", "retryable"), [(409, False), (403, False), (400, False), (401, False),
                                                    (429, True), (503, True)])
def test_sign_rejection_classifies_retry(status, retryable):
    with _server(sign_status=status) as (url, seen):
        store = HttpObjectStore(HttpTransport(url, "worker-token"))
        with pytest.raises(ObjectStoreError) as e:
            store.get(_ctx(), "s3://bucket/a.json")
    assert e.value.retryable is retryable
    assert len(seen) == 1, "서명이 거부되면 저장소에 가지 않는다"


def test_storage_not_configured_on_ec2_is_not_retryable():
    with _server(sign_status=503, sign_code="STORAGE_NOT_CONFIGURED") as (url, _):
        store = HttpObjectStore(HttpTransport(url, "worker-token"))
        with pytest.raises(ObjectStoreError) as e:
            store.get(_ctx(), "s3://bucket/a.json")
    assert not e.value.retryable


@pytest.mark.parametrize(("status", "body", "retryable"), [
    (403, b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>", True),
    (403, b"<Error><Code>SignatureDoesNotMatch</Code></Error>", False),
    (403, b"<Error><Code>AccessDenied</Code><Message>Access Denied</Message></Error>", False),
    (500, b"", True),
    (404, b"", False),
])
def test_storage_error_classifies_retry_without_leaking_url(status, body, retryable):
    with _server(storage_status=status, storage_body=body) as (url, _):
        store = HttpObjectStore(HttpTransport(url, "worker-token"))
        with pytest.raises(ObjectStoreError) as e:
            store.put(_ctx(), "bev.png", b"x", "image/png")
    assert e.value.retryable is retryable
    assert "sig=" not in str(e.value)


class _FakeTransport:
    def __init__(self, response=None, error=None):
        self.response, self.error = response, error

    def post(self, path, body, timeout_s):
        if self.error:
            raise self.error
        return self.response


def test_unreachable_ec2_is_retryable():
    store = HttpObjectStore(_FakeTransport(error=TransportError("연결 거부")))
    with pytest.raises(ObjectStoreError) as e:
        store.get(_ctx(), "s3://bucket/a.json")
    assert e.value.retryable


def test_refuses_plain_http_signed_url_off_loopback():
    reply = Response(200, {"op": "GET", "uri": "s3://bucket/a.json", "url": "http://storage.example/a.json", "headers": {}})
    store = HttpObjectStore(_FakeTransport(reply), opener=lambda *a, **k: pytest.fail("열면 안 된다"))
    with pytest.raises(ObjectStoreError) as e:
        store.get(_ctx(), "s3://bucket/a.json")
    assert not e.value.retryable


def test_cancelled_job_does_not_sign_or_transfer():
    ctx = _ctx()
    ctx.cancelled.set()
    store = HttpObjectStore(_FakeTransport(error=AssertionError("서명하면 안 된다")))
    with pytest.raises(ObjectStoreError) as e:
        store.get(ctx, "s3://bucket/a.json")
    assert not e.value.retryable


def test_malformed_signed_headers_is_object_store_error():
    reply = Response(200, {"op": "GET", "uri": "s3://bucket/a.json", "url": "https://storage.example/a", "headers": ["x"]})
    store = HttpObjectStore(_FakeTransport(reply), opener=lambda *a, **k: pytest.fail("열면 안 된다"))
    with pytest.raises(ObjectStoreError):
        store.get(_ctx(), "s3://bucket/a.json")


def test_put_without_object_uri_in_reply_does_not_upload():
    reply = Response(200, {"op": "PUT", "url": "https://storage.example/k", "headers": {}})
    store = HttpObjectStore(_FakeTransport(reply), opener=lambda *a, **k: pytest.fail("올리면 안 된다"))
    with pytest.raises(ObjectStoreError):
        store.put(_ctx(), "bev.png", b"x", "image/png")


def test_sign_and_transfer_clamp_timeouts_to_remaining_budget():
    now = [100.0]
    sign_timeouts: list[float] = []
    data_timeouts: list[float] = []

    class TimedTransport:
        def post(self, path, body, timeout_s):
            sign_timeouts.append(timeout_s)
            now[0] += 1.2  # 서명에 1.2초 소비
            return Response(
                200,
                {"op": "GET", "uri": body["uri"], "url": "https://storage.example/frame.jpg", "headers": {}},
            )

    def fake_opener(request, timeout):
        data_timeouts.append(timeout)
        now[0] += 0.8

        class _Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"jpeg-bytes"

        return _Resp()

    ctx = JobContext(
        job_id=JOB,
        lease_id="lease-1",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        timeout_ms=3000,
        deadline=103.0,
        monotonic=lambda: now[0],
    )
    store = HttpObjectStore(TimedTransport(), sign_timeout_s=10.0, data_timeout_s=20.0, opener=fake_opener)
    assert store.get(ctx, "s3://bucket/frame.jpg") == b"jpeg-bytes"
    assert sign_timeouts == [pytest.approx(3.0)]
    assert data_timeouts == [pytest.approx(1.8)]


def test_expired_budget_before_sign_does_not_call_transport():
    ctx = JobContext(
        job_id=JOB,
        lease_id="lease-1",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        deadline=50.0,
        monotonic=lambda: 50.0,
    )
    store = HttpObjectStore(_FakeTransport(error=AssertionError("예산이 없으면 서명 요청을 보내면 안 된다")))
    with pytest.raises(ObjectStoreError) as e:
        store.get(ctx, "s3://bucket/a.jpg")
    assert e.value.retryable


def test_expired_budget_after_sign_does_not_open_transfer():
    now = [10.0]

    class SlowSignTransport:
        def post(self, path, body, timeout_s):
            now[0] = 15.0  # 서명 직후 예산(15.0) 소진
            return Response(
                200,
                {"op": "GET", "uri": body["uri"], "url": "https://storage.example/a.jpg", "headers": {}},
            )

    ctx = JobContext(
        job_id=JOB,
        lease_id="lease-1",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        deadline=15.0,
        monotonic=lambda: now[0],
    )
    store = HttpObjectStore(
        SlowSignTransport(),
        opener=lambda *a, **k: pytest.fail("서명 뒤 예산이 0 이하면 전송을 시작하면 안 된다"),
    )
    with pytest.raises(ObjectStoreError) as e:
        store.get(ctx, "s3://bucket/a.jpg")
    assert e.value.retryable
