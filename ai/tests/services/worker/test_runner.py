"""분석 워커 루프 시나리오 (명세 12.5, FR-101). 가짜 EC2 가 모든 메시지를 계약 스키마로 검증한다.

시간은 실제 monotonic 시계를 쓰되 대여·하트비트를 수십 ms 로 줄인다. 결과가 시각 경합에 기대지 않도록
처리기는 가짜 EC2 의 요청 수나 ctx.cancelled 를 기다린다(상한 5초).
"""

import copy
import time

import pytest

from services.worker.client import AuthError, Lease, LeaseClient
from services.worker.handlers import JobContext, JobFailed
from services.worker.runner import HandlerStuck, Outcome, Worker
from simai_ai.providers.testing import load_example

VLM_INPUT = load_example("job_request.vlm_rationale.json")["payload"]["input"]
VLM_RESULT = load_example("job_callback.vlm_rationale.json")["payload"]["result"]
GLTF = {"gltf_uri": "s3://simai-site/scene/v1.glb"}


def _wait_cancel(ctx: JobContext) -> dict:
    assert ctx.cancelled.wait(5.0), "취소 알림이 와야 한다"
    return {}


def test_lease_heartbeat_submit(ec2, make_worker):
    """대여 → 하트비트(2번 이상) → 결과 제출. 하트비트는 같은 대여를 연장한다."""
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST")
    seen = []

    def vlm(ctx: JobContext):
        seen.append((ctx.job_id, ctx.attempt, ctx.input["event_id"]))
        ctx.report_progress(0.5)
        already = len(ec2.bodies("heartbeat"))  # 진행률을 정한 뒤의 하트비트를 센다
        assert ec2.wait_for("heartbeat", already + 2)
        return copy.deepcopy(VLM_RESULT)

    worker = make_worker({"vlm_rationale": vlm})
    assert worker.run_once() is Outcome.ACCEPTED

    job = ec2.jobs[job_id]
    assert job.state == "SUCCEEDED" and job.result == VLM_RESULT
    assert seen == [(job_id, 1, VLM_INPUT["event_id"])]
    (lease,) = ec2.leases_of(job_id)
    beats = ec2.bodies("heartbeat")
    assert len(beats) >= 2
    assert {b["payload"]["lease_id"] for b in beats} == {lease.lease_id}
    assert beats[-1]["payload"]["progress"] == 0.5
    (callback,) = ec2.bodies("result")
    assert callback["payload"]["lease_id"] == lease.lease_id
    assert callback["payload"]["started_at"] <= callback["payload"]["finished_at"]
    assert ec2.bodies("lease")[0]["payload"]["job_types"] == ["vlm_rationale"]


def test_priority_order(ec2, make_worker):
    """EC2 는 VLM_FAST 를 먼저 내준다 (명세 3.2)."""
    bg = ec2.add_job("static_reconstruct")
    fast = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST")
    order = []

    def handler(ctx):
        order.append(ctx.job_id)
        return copy.deepcopy(VLM_RESULT) if ctx.job_type == "vlm_rationale" else GLTF

    worker = make_worker({"vlm_rationale": handler, "static_reconstruct": handler})
    assert worker.run_once() is Outcome.ACCEPTED
    assert worker.run_once() is Outcome.ACCEPTED
    assert order == [fast, bg]


def test_no_job(ec2, make_worker):
    assert make_worker({"static_reconstruct": lambda ctx: GLTF}).run_once() is Outcome.NO_JOB


def test_retryable_failure_is_reassigned_then_final(ec2, make_worker):
    """재시도 가능한 실패는 새 대여(attempt 2)로 다시 오고, 마지막 시도의 실패로 작업이 끝난다."""
    job_id = ec2.add_job("static_reconstruct", {"calibration_version": "cal-7"})

    def fail(ctx):
        raise JobFailed("INPUT_FETCH_FAILED", f"시도 {ctx.attempt} 입력 내려받기 실패", retryable=True)

    worker = make_worker({"static_reconstruct": fail})
    assert worker.run_once() is Outcome.ACCEPTED
    assert ec2.jobs[job_id].state == "QUEUED"
    assert worker.run_once() is Outcome.ACCEPTED
    job = ec2.jobs[job_id]
    assert job.state == "FAILED" and job.attempt == 2
    assert job.error == {"code": "INPUT_FETCH_FAILED", "message": "시도 2 입력 내려받기 실패", "retryable": True}
    first, second = ec2.leases_of(job_id)
    assert first.lease_id != second.lease_id
    assert [b["payload"]["status"] for b in ec2.bodies("result")] == ["FAILED", "FAILED"]


def test_handler_exception_is_reported_not_retryable(ec2, make_worker):
    job_id = ec2.add_job("static_reconstruct")

    def boom(ctx):
        raise RuntimeError("GPU 메모리 부족")

    assert make_worker({"static_reconstruct": boom}).run_once() is Outcome.ACCEPTED
    job = ec2.jobs[job_id]
    assert job.state == "FAILED"
    assert job.error["code"] == "HANDLER_ERROR" and job.error["retryable"] is False
    assert "GPU 메모리 부족" in job.error["message"]


def test_lease_lost_then_reassigned(ec2, make_worker):
    """하트비트가 끊겨 대여가 만료되면 워커는 작업을 버리고, EC2 는 새 대여로 다시 내준다.
    만료된 대여로 낸 제출은 반영되지 않는다."""
    ec2.ttl_ms, ec2.heartbeat_interval_ms = 150, 30
    job_id = ec2.add_job("static_reconstruct")
    contexts = []

    def handler(ctx):
        contexts.append(ctx)
        return _wait_cancel(ctx) if ctx.attempt == 1 else GLTF

    worker = make_worker({"static_reconstruct": handler})
    ec2.down.add("heartbeat")
    assert worker.run_once() is Outcome.LEASE_LOST
    assert contexts[0].cancelled.is_set()
    assert ec2.bodies("result") == []

    ec2.down.clear()
    assert worker.run_once() is Outcome.ACCEPTED
    first, second = ec2.leases_of(job_id)
    assert (first.attempt, first.state) == (1, "EXPIRED")
    assert (second.attempt, second.state) == (2, "SUBMITTED")
    assert ec2.jobs[job_id].state == "SUCCEEDED" and ec2.jobs[job_id].final_lease == second.lease_id

    # 만료된 첫 대여로 늦게 제출하면 REJECTED(LEASE_EXPIRED), 작업 결과는 그대로
    stale = Lease.from_message(ec2_lease_message(ec2, first))
    late = worker.client.callback_message(stale, "SUCCEEDED", started_at=None, finished_at=ec2.now_ms(), result={"x": 1})
    reply = worker.client.submit(stale, late, deadline=float("inf"))
    assert (reply.status, reply.reason) == ("REJECTED", "LEASE_EXPIRED")
    assert ec2.jobs[job_id].result == GLTF


def ec2_lease_message(ec2, rec):
    """가짜 EC2 대여 기록을 job_request 모양으로 (늦은 제출 시험용)."""
    job = ec2.jobs[rec.job_id]
    return {
        "schema_version": "3.0.0",
        "msg_type": "job_request",
        "sent_at": rec.leased_at,
        "payload": {
            "job_id": rec.job_id,
            "lease_id": rec.lease_id,
            "attempt": rec.attempt,
            "max_attempts": ec2.max_attempts,
            "job_type": job.job_type,
            "timeout_ms": job.timeout_ms,
            "leased_at": rec.leased_at,
            "lease_expires_at": rec.expires_at,
            "heartbeat_interval_ms": ec2.heartbeat_interval_ms,
            "input": job.input,
        },
    }


def test_stale_submission_after_reassignment_is_dropped(ec2, make_worker):
    """처리 중에 EC2 가 대여를 만료·재할당하면 그 결과는 REJECTED 로 버려지고, 새 대여의 결과만 남는다."""
    job_id = ec2.add_job("static_reconstruct")

    def handler(ctx):
        if ctx.attempt == 1:
            ec2.force_expire(ctx.job_id)
            return {"gltf_uri": "s3://simai-site/scene/stale.glb"}
        return GLTF

    worker = make_worker({"static_reconstruct": handler})
    assert worker.run_once() is Outcome.REJECTED
    assert ec2.jobs[job_id].state == "QUEUED"
    assert worker.run_once() is Outcome.ACCEPTED
    assert ec2.jobs[job_id].result == GLTF


def test_duplicate_submission_after_lost_response(ec2, make_worker, sleep):
    """제출 응답을 잃으면 같은 메시지를 다시 보내고, EC2 는 DUPLICATE 로 답한다(처음 결과 유지)."""
    job_id = ec2.add_job("static_reconstruct")
    ec2.fail_after["result"] = 1
    assert make_worker({"static_reconstruct": lambda ctx: GLTF}).run_once() is Outcome.DUPLICATE
    first, again = ec2.bodies("result")
    assert first == again  # 같은 msg_id·lease_id 로 다시 보냈다
    assert ec2.jobs[job_id].state == "SUCCEEDED" and ec2.jobs[job_id].result == GLTF
    assert len(ec2.leases_of(job_id)[0].submissions) == 2
    assert sleep.calls == [0.5]


def test_network_down_backoff_is_capped_and_resets(ec2, make_worker, sleep):
    """대여 요청이 계속 끊기면 0.5초부터 두 배씩, 30초 상한으로 기다리며 다시 보낸다. 성공하면 처음부터."""
    ec2.add_job("static_reconstruct")
    ec2.fail_before["lease"] = 8
    worker = make_worker({"static_reconstruct": lambda ctx: GLTF})
    assert worker.run_once() is Outcome.ACCEPTED
    assert sleep.calls == [0.5, 1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    sleep.calls.clear()
    ec2.fail_before["lease"] = 2
    assert worker.run_once() is Outcome.NO_JOB
    assert sleep.calls == [0.5, 1.0]


def test_stop_during_backoff(ec2, make_worker):
    """멈춤 요청이 오면 다시 보내기를 그만둔다."""
    ec2.down.add("lease")
    checks = []

    def should_stop():
        checks.append(1)
        return len(checks) > 3

    worker = make_worker({"static_reconstruct": lambda ctx: GLTF})
    assert worker.run_once(should_stop=should_stop) is Outcome.NO_JOB
    assert len(checks) == 4


def test_timeout_reports_timed_out(ec2, make_worker):
    """실행 상한을 넘기면 처리기에 취소를 알리고 TIMED_OUT 을 제출한다."""
    ec2.heartbeat_interval_ms = 1000  # 상한 전에 하트비트가 없게 해 EC2 의 JOB_TIMED_OUT 과 경합하지 않는다
    job_id = ec2.add_job("static_reconstruct", timeout_ms=100)
    contexts = []

    def slow(ctx):
        contexts.append(ctx)
        return _wait_cancel(ctx)

    assert make_worker({"static_reconstruct": slow}).run_once() is Outcome.ACCEPTED
    assert contexts[0].cancelled.is_set()
    job = ec2.jobs[job_id]
    assert job.state == "TIMED_OUT" and job.error["code"] == "TIMEOUT"
    (callback,) = ec2.bodies("result")
    assert callback["payload"]["status"] == "TIMED_OUT" and "result" not in callback["payload"]


def test_cancelled_job_heartbeat_rejected(ec2, make_worker):
    """EC2 가 작업을 취소하면 다음 하트비트가 REJECTED 이고, 워커는 처리기를 멈추고 결과를 내지 않는다."""
    job_id = ec2.add_job("static_reconstruct")
    contexts = []

    def handler(ctx):
        contexts.append(ctx)
        ec2.cancel(ctx.job_id)
        return _wait_cancel(ctx)

    assert make_worker({"static_reconstruct": handler}).run_once() is Outcome.REJECTED
    assert contexts[0].cancelled.is_set()
    assert ec2.bodies("result") == []
    assert ec2.jobs[job_id].state == "CANCELLED"


def test_submit_gives_up_when_lease_ends(ec2):
    """제출이 계속 끊기면 대여 만료 시각까지만 다시 보내고 버린다(그 뒤는 EC2 가 재할당한다)."""
    clock_ms = lambda: ec2.now_ms()  # noqa: E731
    client = LeaseClient(ec2, "worker-01", clock_ms=clock_ms, monotonic=time.monotonic, sleep=time.sleep)
    worker = Worker(client, {"static_reconstruct": lambda ctx: GLTF}, clock_ms=clock_ms, monotonic=time.monotonic)
    ec2.ttl_ms = 200
    ec2.add_job("static_reconstruct")
    ec2.down.add("result")
    started = time.monotonic()
    assert worker.run_once() is Outcome.GAVE_UP
    assert time.monotonic() - started < 2.0


def test_invalid_result_is_reported(ec2, make_worker):
    job_id = ec2.add_job("static_reconstruct")
    assert make_worker({"static_reconstruct": lambda ctx: ["not", "object"]}).run_once() is Outcome.ACCEPTED
    assert ec2.jobs[job_id].error["code"] == "INVALID_RESULT"


def test_auth_error_is_fatal(ec2, make_worker):
    ec2.token_ok = False
    with pytest.raises(AuthError):
        make_worker({"static_reconstruct": lambda ctx: GLTF}).run_once()


def test_stuck_handler_blocks_next_lease(ec2, client):
    """취소를 무시하는 처리기가 유예 시간 안에 멈추지 않으면 다음 대여를 받지 않는다(GPU 작업 겹침 방지)."""
    import threading

    release = threading.Event()
    ec2.add_job("static_reconstruct")
    ec2.add_job("static_reconstruct")

    def stubborn(ctx):
        ec2.cancel(ctx.job_id)
        release.wait(5.0)  # ctx.cancelled 를 보지 않는다
        return GLTF

    clock_ms = lambda: ec2.now_ms()  # noqa: E731
    worker = Worker(client, {"static_reconstruct": stubborn}, clock_ms=clock_ms, monotonic=time.monotonic, abandon_grace_s=0.05)
    assert worker.run_once() is Outcome.REJECTED
    leases_before = len(ec2.bodies("lease"))
    with pytest.raises(HandlerStuck):
        worker.run_once()
    assert len(ec2.bodies("lease")) == leases_before  # 새 대여를 요청하지 않았다
    release.set()
    worker.abandon_grace_s = 5.0
    assert worker.run_once() is Outcome.REJECTED  # 스레드가 끝나면 다시 받는다(두 번째 작업)


def test_auth_error_during_heartbeat_cancels_handler(ec2, make_worker):
    """처리 중 인증이 끊기면 처리기에 취소를 알리고 오류를 위로 던진다."""
    ec2.add_job("static_reconstruct")
    contexts = []

    def handler(ctx):
        contexts.append(ctx)
        ec2.token_ok = False
        return _wait_cancel(ctx)

    with pytest.raises(AuthError):
        make_worker({"static_reconstruct": handler}).run_once()
    assert contexts[0].cancelled.is_set()


def test_handler_for_unknown_job_type_is_refused_at_start(client):
    # 계약 밖 유형으로 대여를 요청하면 EC2 가 400 으로 거부해 워커가 멈춘다. 시작할 때 막는다
    with pytest.raises(ValueError, match="vlm_rationle"):
        Worker(client, {"vlm_rationle": lambda ctx: {}}, clock_ms=lambda: 0, monotonic=time.monotonic)


def test_worker_process_propagates_timeout_deadline_to_job_context(ec2, make_worker):
    ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=5000)
    observed: list[tuple[int | None, float | None]] = []

    def handler(ctx: JobContext):
        observed.append((ctx.timeout_ms, ctx.remaining_s()))
        return copy.deepcopy(VLM_RESULT)

    worker = make_worker({"vlm_rationale": handler})
    assert worker.run_once() is Outcome.ACCEPTED
    assert len(observed) == 1
    timeout_ms, rem_s = observed[0]
    assert timeout_ms == 5000
    assert rem_s is not None and 0.0 < rem_s <= 5.0


def test_vlm_budget_ends_at_event_deadline_not_lease_plus_timeout(ec2, make_worker):
    """S15P31A101-408: 관측 + 5 s(deadline_at)가 대여 + timeout_ms 보다 이르면 처리기 남은 시간·취소는 마감 기준이다."""
    ec2.heartbeat_interval_ms = 1000
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=5000,
                         deadline_at=ec2.now_ms() + 300)  # 이벤트 뒤 4.7 s 에 대여된 꼴
    seen: list[float | None] = []

    def slow(ctx: JobContext):
        seen.append(ctx.remaining_s())
        return _wait_cancel(ctx)

    started = time.monotonic()
    assert make_worker({"vlm_rationale": slow}).run_once() is Outcome.ACCEPTED
    assert time.monotonic() - started < 2.0  # 5 s 실행 상한까지 끌지 않는다
    assert seen[0] is not None and seen[0] <= 0.3 + 1e-6  # ms→s 부동소수 오차 허용
    job = ec2.jobs[job_id]
    assert job.state == "TIMED_OUT" and "마감" in job.error["message"]


def test_without_deadline_at_budget_is_timeout_only(ec2, make_worker):
    ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=5000)
    seen: list[float | None] = []

    def quick(ctx: JobContext):
        seen.append(ctx.remaining_s())
        return copy.deepcopy(VLM_RESULT)

    assert make_worker({"vlm_rationale": quick}).run_once() is Outcome.ACCEPTED
    assert seen[0] is not None and 4.0 < seen[0] <= 5.0  # 관리용(이벤트 없는) 작업은 예전처럼 실행 상한만


def test_failure_warning_masks_secrets_and_limits_message(ec2, make_worker, caplog, monkeypatch):
    monkeypatch.setenv("SIMAI_TEST_TOKEN", "test-secret-123")
    job_id = ec2.add_job("static_reconstruct")

    def fail(ctx):
        raise JobFailed("INPUT_FETCH_FAILED", "문제 test-secret-123 https://example.com/?signature=abc\n" + "x" * 450)

    assert make_worker({"static_reconstruct": fail}).run_once() is Outcome.ACCEPTED
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "code=INPUT_FETCH_FAILED" in r.getMessage()]
    assert len(warnings) == 1 and job_id in warnings[0]
    assert "test-secret-123" not in warnings[0] and "signature=abc" not in warnings[0]
    assert "<REDACTED>" in warnings[0] and "<URL>" in warnings[0] and "\n" not in warnings[0]
    assert len(warnings[0].split("message=", 1)[1]) == 400
