"""분석 워커 진입점(`python -m services.worker`)을 실제 HTTP 로 끝까지 돌린다 (FR-101 완료 조건, S15P31A101-19).

가짜 EC2(conftest.FakeEC2)를 루프백 HTTP 서버로 띄우고, 환경 변수로 받은 주소·워커 토큰으로 HttpTransport 가 붙는다.
- 샘플 작업이 대여 → 실행 → 연장(하트비트) → 결과 제출로 끝난다.
- 대여가 만료된 뒤 낸 결과는 거부되고, 워커는 그 결과를 다시 보내지 않고 버린다.
"""

import json
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import services.worker.__main__ as entry
from services.worker.handlers import JobContext
from services.worker.transport import TOKEN_HEADER
from simai_ai.providers.testing import load_example

TOKEN = "test-worker-token"
VLM_INPUT = load_example("job_request.vlm_rationale.json")["payload"]["input"]
VLM_RESULT = load_example("job_callback.vlm_rationale.json")["payload"]["result"]


@pytest.fixture
def http_ec2(ec2):
    """FakeEC2 를 HTTP 로 연다. 토큰이 틀리거나 가짜가 계약 위반을 잡으면 400 으로 답해 워커가 멈추게 한다(멈춰 기다리지 않게)."""
    errors: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            try:
                assert self.headers[TOKEN_HEADER] == f"Bearer {TOKEN}", "워커 토큰이 실리지 않았다"
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                resp = ec2.post(self.path, body, 0)
                status, raw = resp.status, b"" if resp.body is None else json.dumps(resp.body).encode()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")
                status, raw = 400, b""
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}", errors
    server.shutdown()
    server.server_close()


@pytest.fixture
def run_main(http_ec2, monkeypatch, tmp_path):
    url, errors = http_ec2
    monkeypatch.setenv("SIMAI_EC2_BASE_URL", url)
    monkeypatch.setenv("SIMAI_WORKER_TOKEN", TOKEN)
    monkeypatch.setenv("SIMAI_WORKER_ID", "worker-01")
    monkeypatch.setenv("SIMAI_TIER0_MARKER", str(tmp_path / "tier0.live"))  # 없음 + 개발 모드 = 관제 없음
    monkeypatch.setenv("SIMAI_WORKER_DEV_NO_LIVE", "1")
    monkeypatch.setattr(signal, "signal", lambda *a: None)  # pytest 의 Ctrl+C 처리를 바꾸지 않는다

    def _run(handlers) -> int:
        monkeypatch.setattr(entry, "build_handlers", lambda config: handlers)
        code = entry.main(["--once", "--wait-ms", "0"])
        assert not errors, errors
        return code

    return _run


def test_sample_job_leases_runs_extends_and_submits(ec2, run_main):
    job_id = ec2.add_job("vlm_rationale", VLM_INPUT, priority="VLM_FAST")

    def handler(ctx: JobContext) -> dict:
        assert ec2.wait_for("heartbeat", 2), "실행 중 대여를 연장해야 한다"
        return VLM_RESULT

    assert run_main({"vlm_rationale": handler}) == 0
    job = ec2.jobs[job_id]
    assert (job.state, job.result, job.attempt) == ("SUCCEEDED", VLM_RESULT, 1)
    [lease] = ec2.leases_of(job_id)
    assert lease.state == "SUBMITTED" and lease.expires_at > lease.leased_at + ec2.ttl_ms  # 하트비트로 늘었다


def test_result_after_lease_expired_is_rejected_and_dropped(ec2, run_main):
    ec2.heartbeat_interval_ms = 4000  # 제출 전에 하트비트가 끼지 않게
    job_id = ec2.add_job("vlm_rationale", VLM_INPUT, priority="VLM_FAST")

    def handler(ctx: JobContext) -> dict:
        ec2.force_expire(ctx.job_id)  # 실행 중 대여가 만료돼 EC2 가 다시 대기열에 넣었다
        return VLM_RESULT

    assert run_main({"vlm_rationale": handler}) == 0
    assert len(ec2.bodies("result")) == 1, "거부된 결과를 다시 보내지 않는다"
    job = ec2.jobs[job_id]
    assert job.state == "QUEUED" and job.result is None  # 늦은 결과는 반영되지 않고 새 대여를 기다린다
    [lease] = ec2.leases_of(job_id)
    assert lease.state == "EXPIRED" and not lease.submissions
