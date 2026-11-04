"""분석 워커의 Tier 0 우선 접수 (명세 3.2, 6.6, 20.5). 가짜 EC2 가 모든 메시지를 계약 스키마로 검증한다.

접수 문은 각본대로 답하는 가짜(ScriptedGate)다. 실제 표시 파일 판정은 tests/gpu/test_admission.py, Tier 0 와 함께 도는 것은
tests/services/tier0/test_tier0_gpu_priority.py 가 본다.

반례:
- 지연 초과·모름: 대여를 요청하지 않는다(EC2 대기열에 그대로).
- long-poll 사이에 지연이 넘침: 받은 작업을 시작하지 않고 하트비트로 대여를 지키다(대여 유지 시간보다 길게) 풀리면 시작,
  hold 상한까지 안 풀리면 반납한다(retryable FAILED + consume_attempt false, 계약 job_callback 2.1.0).
- LIVE 시작과 EXP 작업 대여가 동시: 받은 EXP 작업을 시작하지 않고 반납한다. 도는 중이면 취소를 알리고 반납한다.
- 반납은 시도 횟수를 쓰지 않는다: 마지막 시도도 다시 대기열에 가고 같은 attempt 로 다시 대여된다. 처리기 실패는 반납이 아니다.
- 접수 문 자체가 예외: 받지 않는다.
"""

from __future__ import annotations

import copy
import threading
import time

import pytest

from services.worker.client import CALLBACK_SCHEMA_VERSION
from services.worker.handlers import JobContext
from services.worker.runner import Outcome, Worker
from simai_ai.gpu.admission import DEFAULT_LIVE_BLOCKED, Admission, AdmissionPolicy
from simai_ai.providers.testing import load_example

VLM_INPUT = load_example("job_request.vlm_rationale.json")["payload"]["input"]
VLM_RESULT = load_example("job_callback.vlm_rationale.json")["payload"]["result"]
GLTF = {"gltf_uri": "s3://simai-site/scene/v1.glb"}

OPEN = Admission(False, True, "LIVE 아님", DEFAULT_LIVE_BLOCKED)
LIVE_OK = Admission(True, True, "지연 예산 안", DEFAULT_LIVE_BLOCKED)
LIVE_BUSY = Admission(True, False, "Tier 0 지연 900ms > 예산 600ms", DEFAULT_LIVE_BLOCKED)


class ScriptedGate:
    def __init__(self, answer) -> None:
        self.policy = AdmissionPolicy()
        self.answer = answer  # Admission 또는 호출 횟수 → Admission
        self.calls = 0

    def check(self) -> Admission:
        self.calls += 1
        if callable(self.answer):
            return self.answer(self.calls)
        return self.answer


T0 = 1760342400000


@pytest.fixture
def make(client):
    def _make(handlers, gate, **kw) -> Worker:
        kw.setdefault("sleep", lambda s: time.sleep(min(s, 0.01)))
        return Worker(client, handlers, clock_ms=lambda: T0 + int(time.monotonic() * 1000), monotonic=time.monotonic,
                      wait_ms=0, admission=gate, live_poll_s=0.01, pause_s=0.01, **kw)

    return _make


def tune(ec2, **kw):
    for k, v in kw.items():
        setattr(ec2, k, v)
    return ec2


def never(ctx: JobContext):
    raise AssertionError("처리기를 시작하면 안 된다")


def test_busy_tier0_pauses_without_lease_request(ec2, make):
    ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST")
    w = make({"vlm_rationale": never}, ScriptedGate(LIVE_BUSY))
    assert w.run_once() is Outcome.PAUSED
    assert ec2.bodies("lease") == []  # EC2 는 작업을 대기열에 그대로 둔다


def test_gate_exception_is_unknown_and_pauses(ec2, make):
    def broken(_n):
        raise OSError("표시 파일 볼륨 오류(가짜)")

    w = make({"vlm_rationale": never}, ScriptedGate(broken))
    assert w.run_once() is Outcome.PAUSED and ec2.bodies("lease") == []


def test_live_excludes_exp_types_and_shortens_long_poll(ec2, make):
    w = make({"vlm_rationale": never, "static_reconstruct": never, "hmr_refine": never}, ScriptedGate(LIVE_OK))
    w.wait_ms, w.live_wait_ms = 25_000, 3_000
    assert w.run_once() is Outcome.NO_JOB
    (req,) = ec2.bodies("lease")
    assert req["payload"]["job_types"] == ["vlm_rationale"] and req["payload"]["wait_ms"] == 3_000


def test_live_with_only_exp_handlers_pauses(ec2, make):
    w = make({"static_reconstruct": never}, ScriptedGate(LIVE_OK))
    assert w.run_once() is Outcome.PAUSED and ec2.bodies("lease") == []


def test_overload_during_long_poll_holds_lease_beyond_ttl_then_starts(ec2, make):
    """받은 뒤 지연이 넘쳐 있으면 시작하지 않고 하트비트로 대여를 지킨다(대여 유지 200ms 보다 오래). 풀리면 시작한다."""
    tune(ec2, ttl_ms=200, heartbeat_interval_ms=40)
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=10_000)
    t_open = [None]

    def answer(n):
        if n == 1:
            return LIVE_OK  # 대여 요청 전
        if t_open[0] is None:
            t_open[0] = time.monotonic() + 0.6
        return LIVE_OK if time.monotonic() >= t_open[0] else LIVE_BUSY

    started = []

    def vlm(ctx: JobContext):
        started.append(time.monotonic())
        return copy.deepcopy(VLM_RESULT)

    w = make({"vlm_rationale": vlm}, ScriptedGate(answer), hold_max_s=5.0)
    assert w.run_once() is Outcome.ACCEPTED
    assert started and started[0] >= t_open[0]
    assert ec2.jobs[job_id].state == "SUCCEEDED"
    assert len(ec2.leases_of(job_id)) == 1  # 대여를 잃지 않았다
    assert len(ec2.bodies("heartbeat")) >= 5


def test_overload_that_does_not_clear_defers_job_back_to_queue(ec2, make):
    tune(ec2, ttl_ms=300, heartbeat_interval_ms=40, max_attempts=3)
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=10_000)
    w = make({"vlm_rationale": never}, ScriptedGate(lambda n: LIVE_OK if n == 1 else LIVE_BUSY), hold_max_s=0.3)
    assert w.run_once() is Outcome.DEFERRED
    job = ec2.jobs[job_id]
    assert job.state == "QUEUED" and job.attempt == 0  # 반납: 시도 횟수를 쓰지 않고 다시 대기열로
    assert job.error == {"code": "TIER0_PRIORITY", "message": job.error["message"], "retryable": True,
                         "consume_attempt": False}
    (cb,) = ec2.bodies("result")
    assert cb["payload"]["started_at"] is None  # 시작하지 않았다
    assert cb["schema_version"] == CALLBACK_SCHEMA_VERSION  # 판을 박지 않는다(계약 마이너마다 깨지지 않게)


def test_hold_is_bounded_by_half_of_execution_limit(ec2, make):
    tune(ec2, ttl_ms=300, heartbeat_interval_ms=40, max_attempts=3)
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=400)
    w = make({"vlm_rationale": never}, ScriptedGate(lambda n: LIVE_OK if n == 1 else LIVE_BUSY), hold_max_s=60)
    t = time.monotonic()
    assert w.run_once() is Outcome.DEFERRED
    assert time.monotonic() - t < 2.0  # 실행 상한(0.4s)의 절반에서 돌려보냄. TIMED_OUT(재시도 없음)이 되지 않는다
    assert ec2.jobs[job_id].state == "QUEUED"


def test_live_start_racing_exp_lease_does_not_start_it(ec2, make):
    """대여 요청 때는 LIVE 가 아니었는데 받은 순간 LIVE 가 시작됐다: EXP 는 시작하지 않고 바로 돌려보낸다."""
    tune(ec2, max_attempts=3)
    job_id = ec2.add_job("static_reconstruct", GLTF)
    w = make({"static_reconstruct": never}, ScriptedGate(lambda n: OPEN if n == 1 else LIVE_OK))
    assert w.run_once() is Outcome.DEFERRED
    assert ec2.jobs[job_id].error["code"] == "LIVE_EXP_BLOCKED"
    assert ec2.jobs[job_id].state == "QUEUED"
    # LIVE 동안 다음 대여 요청에는 그 유형이 빠지므로 다시 오지 않는다
    assert w.run_once() is Outcome.PAUSED


def test_live_start_preempts_running_exp_job(ec2, make):
    tune(ec2, max_attempts=3)
    job_id = ec2.add_job("static_reconstruct", GLTF, timeout_ms=10_000)
    live = threading.Event()
    cancelled = threading.Event()

    def heavy(ctx: JobContext):
        live.set()  # 도는 중에 Tier 0 LIVE 시작
        assert ctx.cancelled.wait(5.0), "LIVE 시작 뒤 취소 알림이 와야 한다"
        cancelled.set()
        return {"gltf_uri": "s3://never"}

    w = make({"static_reconstruct": heavy}, ScriptedGate(lambda n: LIVE_OK if live.is_set() else OPEN))
    assert w.run_once() is Outcome.DEFERRED
    assert cancelled.wait(5.0)
    job = ec2.jobs[job_id]
    assert job.error["code"] == "LIVE_PREEMPTED" and job.state == "QUEUED"
    assert job.error["consume_attempt"] is False and job.attempt == 0  # 선점은 작업 탓이 아니다: 반납
    (cb,) = ec2.bodies("result")
    assert cb["payload"]["started_at"] is not None  # 시작했던 작업이다


def test_overload_does_not_cancel_running_allowed_job(ec2, make):
    """지연 초과는 새 작업만 멈춘다. 도는 VLM 은 끝까지 둔다(명세 3.2)."""
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST")
    busy = threading.Event()

    def vlm(ctx: JobContext):
        busy.set()
        time.sleep(0.2)
        assert not ctx.cancelled.is_set()
        return copy.deepcopy(VLM_RESULT)

    w = make({"vlm_rationale": vlm}, ScriptedGate(lambda n: LIVE_BUSY if busy.is_set() else LIVE_OK))
    assert w.run_once() is Outcome.ACCEPTED
    assert ec2.jobs[job_id].state == "SUCCEEDED"
    assert w.run_once() is Outcome.PAUSED  # 다음 작업은 받지 않는다


# ---------------------------------------------------------------- 리뷰 R2·R3 회귀 (재현 그대로)


class _Wrap:
    """가짜 EC2 앞에 끼워 대여·하트비트 요청에 장애·지연을 넣는 전송."""

    def __init__(self, ec2, on_post) -> None:
        self.ec2 = ec2
        self.on_post = on_post

    def post(self, path, body, timeout_s):
        out = self.on_post(path, body)
        if out is not None:
            return out
        return self.ec2.post(path, body, timeout_s)


def _raw_worker(transport, handlers, gate, **kw) -> Worker:
    from services.worker.client import LeaseClient

    client = LeaseClient(transport, "worker-01", clock_ms=lambda: T0 + int(time.monotonic() * 1000),
                         monotonic=time.monotonic, sleep=lambda s: None)
    kw.setdefault("sleep", lambda s: time.sleep(min(s, 0.01)))
    return Worker(client, handlers, clock_ms=lambda: T0 + int(time.monotonic() * 1000), monotonic=time.monotonic,
                  wait_ms=0, admission=gate, pause_s=0.01, **kw)


def test_lease_retry_rechecks_gate_and_does_not_burn_last_attempt(ec2):
    """R2: 첫 대여 요청이 503 인 사이 LIVE 가 시작되면, 다시 보내기 전에 접수 문을 본다. EXP 를 대여하지 않는다."""
    from services.worker.transport import Response

    tune(ec2, max_attempts=1)
    job_id = ec2.add_job("static_reconstruct", GLTF)
    state = {"busy": False, "leases": []}
    gate = ScriptedGate(lambda n: LIVE_OK if state["busy"] else OPEN)

    def on_post(path, body):
        if path.endswith("/lease"):
            state["leases"].append((state["busy"], body["payload"]["job_types"]))
            if len(state["leases"]) == 1:
                state["busy"] = True  # 응답을 못 받은 사이 Tier 0 LIVE 시작
                return Response(503, None)
        return None

    w = _raw_worker(_Wrap(ec2, on_post), {"static_reconstruct": never}, gate)
    assert w.run_once() is Outcome.PAUSED
    assert state["leases"] == [(False, ["static_reconstruct"])]  # 닫힌 뒤 다시 보내지 않았다
    job = ec2.jobs[job_id]
    assert job.state == "QUEUED" and job.attempt == 0  # 시도 횟수를 쓰지 않았다
    assert ec2.bodies("result") == []


def test_counterexample_last_attempt_released_while_live_is_leased_again_with_same_attempt(ec2, make):
    """반례 1: 마지막 시도(attempt == max_attempts)가 LIVE 에 막혔다. 반납하면 최종 FAILED 가 아니라 다시 대기열이고,
    LIVE 가 끝난 뒤 같은 작업을 같은 attempt·새 lease_id 로 다시 대여해 끝낸다."""
    tune(ec2, max_attempts=1, ttl_ms=300, heartbeat_interval_ms=40)
    job_id = ec2.add_job("static_reconstruct", GLTF, timeout_ms=10_000)
    live = {"on": False}

    def answer(n):
        if n == 1:
            return OPEN  # 대여 요청 때는 LIVE 아님
        if n == 2:
            live["on"] = True  # 받은 순간 LIVE
        return LIVE_OK if live["on"] else OPEN

    ran = []
    w = make({"static_reconstruct": lambda ctx: ran.append(ctx.attempt) or dict(GLTF)}, ScriptedGate(answer))
    t = time.monotonic()
    assert w.run_once() is Outcome.DEFERRED
    assert time.monotonic() - t < 1.0  # 실행 상한까지 대여를 붙잡고 기다리지 않는다
    job = ec2.jobs[job_id]
    assert job.state == "QUEUED" and job.attempt == 0 and not ran
    assert w.run_once() is Outcome.PAUSED  # LIVE 동안 EXP 는 대여 요청에서 빠진다
    live["on"] = False
    assert w.run_once() is Outcome.ACCEPTED
    first, second = ec2.leases_of(job_id)
    assert first.attempt == second.attempt == 1 and first.lease_id != second.lease_id
    assert ran == [1] and ec2.jobs[job_id].state == "SUCCEEDED"


def test_counterexample_last_attempt_overload_released_after_hold_max(ec2, make):
    """반례 2: 마지막 시도의 지연 초과도 hold_max_s 뒤 반납한다(실행 상한까지 GPU 대기 슬롯을 붙잡지 않음). 최종 FAILED 아님."""
    tune(ec2, max_attempts=1, ttl_ms=300, heartbeat_interval_ms=40)
    job_id = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST", timeout_ms=1500)
    w = make({"vlm_rationale": never}, ScriptedGate(lambda n: LIVE_OK if n == 1 else LIVE_BUSY), hold_max_s=0.05)
    t = time.monotonic()
    assert w.run_once() is Outcome.DEFERRED
    assert time.monotonic() - t < 1.0
    job = ec2.jobs[job_id]
    assert job.error["code"] == "TIER0_PRIORITY" and job.state == "QUEUED" and job.attempt == 0


def test_counterexample_last_attempt_live_preempted_is_released(ec2, make):
    """반례 3: 마지막 시도가 도는 중 LIVE 가 시작됐다. 취소하고 반납한다(이전에는 최종 FAILED). 다시 대여하면 같은 attempt."""
    tune(ec2, max_attempts=1)
    job_id = ec2.add_job("static_reconstruct", GLTF, timeout_ms=10_000)
    live = threading.Event()
    attempts = []

    def heavy(ctx: JobContext):
        attempts.append(ctx.attempt)
        if len(attempts) == 1:
            live.set()
            assert ctx.cancelled.wait(5.0)
            return {"gltf_uri": "s3://never"}
        return dict(GLTF)

    w = make({"static_reconstruct": heavy}, ScriptedGate(lambda n: LIVE_OK if live.is_set() else OPEN))
    assert w.run_once() is Outcome.DEFERRED
    assert ec2.jobs[job_id].state == "QUEUED" and ec2.jobs[job_id].error["code"] == "LIVE_PREEMPTED"
    live.clear()
    assert w.run_once() is Outcome.ACCEPTED
    assert attempts == [1, 1] and ec2.jobs[job_id].state == "SUCCEEDED"


def test_negative_control_handler_failure_after_start_consumes_attempt(ec2, make):
    """음성 대조: 접수 문이 열려 시작한 작업의 retryable 실패는 반납이 아니다. 마지막 시도면 기존대로 최종 FAILED."""
    from services.worker.handlers import JobFailed

    tune(ec2, max_attempts=1)
    job_id = ec2.add_job("static_reconstruct", GLTF, timeout_ms=10_000)

    def fail(ctx: JobContext):
        raise JobFailed("INPUT_FETCH_FAILED", "입력 내려받기 실패", retryable=True)

    w = make({"static_reconstruct": fail}, ScriptedGate(OPEN))
    assert w.run_once() is Outcome.ACCEPTED
    job = ec2.jobs[job_id]
    assert job.state == "FAILED" and job.attempt == 1 and "consume_attempt" not in job.error


def test_slow_heartbeat_does_not_delay_live_preemption(ec2):
    """R3: 하트비트 응답이 3초 늦어도 LIVE 선점 취소는 live_poll_s(기본 2초) 안에 알린다(감시 스레드 분리)."""
    tune(ec2, max_attempts=3, ttl_ms=10_000, heartbeat_interval_ms=50)
    job_id = ec2.add_job("static_reconstruct", GLTF, timeout_ms=20_000)
    live = threading.Event()
    timing = {}

    def on_post(path, body):
        if path.endswith("/heartbeat") and not live.is_set():
            timing["live"] = time.monotonic()
            live.set()
            time.sleep(3.0)

    def heavy(ctx: JobContext):
        assert ctx.cancelled.wait(10)
        timing["cancel"] = time.monotonic()
        return {}

    gate = ScriptedGate(lambda n: LIVE_OK if live.is_set() else OPEN)
    w = _raw_worker(_Wrap(ec2, on_post), {"static_reconstruct": heavy}, gate, sleep=None)
    assert w.live_poll_s == 2.0
    assert w.run_once() is Outcome.DEFERRED
    latency = timing["cancel"] - timing["live"]
    assert latency <= w.live_poll_s + 0.5, latency
    assert ec2.jobs[job_id].error["code"] == "LIVE_PREEMPTED"


def test_long_pause_is_observable(ec2, make):
    """접수 중지가 이어진 시간을 알 수 있다(운영 감시)."""
    w = make({"vlm_rationale": never}, ScriptedGate(LIVE_BUSY))
    assert w.paused_for_s is None
    assert w.run_once() is Outcome.PAUSED
    time.sleep(0.05)
    assert w.run_once() is Outcome.PAUSED
    assert w.paused_for_s >= 0.05
