"""분석 워커 진입점(`python -m services.worker`)을 실제 HTTP 로 끝까지 돌린다 (FR-101 완료 조건, S15P31A101-19).

가짜 EC2(conftest.FakeEC2)를 루프백 HTTP 서버로 띄우고, 환경 변수로 받은 주소·워커 토큰으로 HttpTransport 가 붙는다.
- 샘플 작업이 대여 → 실행 → 연장(하트비트) → 결과 제출로 끝난다.
- 대여가 만료된 뒤 낸 결과는 거부되고, 워커는 그 결과를 다시 보내지 않고 버린다.
"""

from services.worker.handlers import JobContext
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
