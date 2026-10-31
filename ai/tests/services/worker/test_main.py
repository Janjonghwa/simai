"""분석 워커 진입점(`python -m services.worker`)을 실제 HTTP 로 끝까지 돌린다 (FR-101 완료 조건, S15P31A101-19).

가짜 EC2(conftest.FakeEC2)를 루프백 HTTP 서버로 띄우고, 환경 변수로 받은 주소·워커 토큰으로 HttpTransport 가 붙는다.
- 샘플 작업이 대여 → 실행 → 연장(하트비트) → 결과 제출로 끝난다.
- 대여가 만료된 뒤 낸 결과는 거부되고, 워커는 그 결과를 다시 보내지 않고 버린다.
"""

import services.worker.__main__ as worker_main
from services.worker.handlers import JobContext
from simai_ai.gpu.reserve import FAILED, OK, CapResult
from simai_ai.providers.testing import load_example

VLM_INPUT = load_example("job_request.vlm_rationale.json")["payload"]["input"]
VLM_RESULT = load_example("job_callback.vlm_rationale.json")["payload"]["result"]


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


def test_cap_process_memory_runs_before_build_handlers(run_main, monkeypatch):
    """GPU 메모리 상한(cap_process_memory)은 pose 가중치를 여는 build_handlers 보다 먼저 설정돼야 한다 (S15P31A101-382)."""
    order: list[str] = []
    monkeypatch.setenv("SIMAI_WORKER_TORCH_MEMORY_FRACTION", "0.05")

    def fake_cap(fraction: float, device: int = 0) -> CapResult:
        order.append(f"cap:{fraction}")
        return CapResult(state=OK, fraction=fraction)

    def fake_build(config, store=None, **_kw):
        order.append("build_handlers")
        return {"vlm_rationale": lambda _ctx: VLM_RESULT}

    monkeypatch.setattr(worker_main, "cap_process_memory", fake_cap)
    monkeypatch.setattr(worker_main, "build_handlers", fake_build)
    assert worker_main.main(["--once", "--wait-ms", "0"]) == 0
    assert order == ["cap:0.05", "build_handlers"]


def test_gpu_cap_failure_or_budget_error_skips_build_handlers(run_main, monkeypatch):
    """GPU 예산 설정 오류나 cap_process_memory 실패 시 build_handlers(가중치 적재)를 호출하지 않고 종료 코드 2 를 돌려준다."""
    called = []
    monkeypatch.setattr(worker_main, "build_handlers", lambda *_a, **_kw: called.append(True) or {})

    monkeypatch.setenv("SIMAI_WORKER_TORCH_MEMORY_FRACTION", "0.95")
    assert worker_main.main(["--once", "--wait-ms", "0"]) == 2
    assert not called

    monkeypatch.setenv("SIMAI_WORKER_TORCH_MEMORY_FRACTION", "0.05")
    monkeypatch.setattr(
        worker_main,
        "cap_process_memory",
        lambda fraction, device=0: CapResult(state=FAILED, fraction=fraction, reason="cuda error"),
    )
    assert worker_main.main(["--once", "--wait-ms", "0"]) == 2
    assert not called
