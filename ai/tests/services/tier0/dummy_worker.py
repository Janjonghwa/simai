"""시험용 더미 분석 워커 프로세스 (S15P31A101-60, 부록 D D-G0-08).

실제 Worker 루프와 실제 접수 문(AdmissionGate, Tier 0 표시 파일)을 쓰고, EC2 는 끝없이 vlm_rationale 작업을 내주는 가짜다.
처리기는 CPU 를 돌리고 메모리를 잡는 더미 부하다(GPU 는 쓰지 않는다). 작업을 끝낼 때마다 stdout 에 DONE 을 찍는다.

    python dummy_worker.py <표시 파일> <지연 예산 ms> <작업당 부하 ms> <작업당 메모리 MB>
"""

from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

AI_DIR = Path(__file__).resolve().parents[3]
if str(AI_DIR) not in sys.path:
    sys.path.insert(0, str(AI_DIR))

from services.worker.client import LeaseClient  # noqa: E402
from services.worker.runner import Worker  # noqa: E402
from services.worker.transport import Response  # noqa: E402
from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy  # noqa: E402


def _now() -> int:
    return time.time_ns() // 1_000_000


def _env(msg_type: str, payload: dict) -> dict:
    return {"schema_version": "3.0.0" if msg_type == "job_request" else "2.0.0", "msg_type": msg_type, "msg_id": str(uuid.uuid4()), "sent_at": _now(),
            "source_id": "ec2", "payload": payload}


class EndlessEC2:
    def __init__(self) -> None:
        self._ids = itertools.count(1)

    def post(self, path: str, body: dict, timeout_s: float) -> Response:
        now = _now()
        if path.endswith("/lease"):
            payload = {"job_id": f"job-{next(self._ids)}", "lease_id": str(uuid.uuid4()), "attempt": 1, "max_attempts": 1,
                       "job_type": "vlm_rationale", "priority": "VLM_FAST", "timeout_ms": 60_000, "leased_at": now,
                       "lease_expires_at": now + 30_000, "heartbeat_interval_ms": 10_000, "input": {}}
            return Response(200, _env("job_request", payload))
        p = body["payload"]
        ids = {"job_id": p["job_id"], "lease_id": p["lease_id"]}
        if path.endswith("/heartbeat"):
            return Response(200, _env("job_lease_status", {**ids, "status": "ACTIVE", "lease_expires_at": now + 30_000}))
        print("DONE", flush=True)
        return Response(200, _env("job_lease_status", {**ids, "status": "ACCEPTED"}))


def main() -> int:
    marker, budget_ms, load_ms, mem_mb = Path(sys.argv[1]), int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)

    def dummy(ctx):
        buf = bytearray(mem_mb * 1024 * 1024)  # 메모리 부하
        end = time.monotonic() + load_ms / 1000
        n = 0
        while time.monotonic() < end and not ctx.cancelled.is_set():
            n += sum(range(5000))  # CPU 부하
        return {"n": n, "bytes": len(buf)}

    stop = threading.Event()
    client = LeaseClient(EndlessEC2(), "worker-01", clock_ms=_now, monotonic=time.monotonic, sleep=stop.wait)
    gate = AdmissionGate(marker, AdmissionPolicy(lag_budget_ms=budget_ms, resume_after_s=0.0, live_linger_s=0.0),
                         wall_s=time.time, monotonic=time.monotonic)
    worker = Worker(client, {"vlm_rationale": dummy}, clock_ms=_now, monotonic=time.monotonic, wait_ms=0,
                    admission=gate, pause_s=0.2, live_poll_s=0.2, sleep=stop.wait)
    print("READY", flush=True)
    worker.run(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
