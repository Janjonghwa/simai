"""vLLM 준비 전 VLM 작업 유형 보류 (S15P31A101-577). 감시 프로세스가 워커를 vLLM 보다 먼저 띄울 때 VLM 작업이 VLM_UNAVAILABLE 로 시도를 쓰지 않게 한다."""

import copy
import logging
import time

import pytest

from services.worker.runner import Outcome, Worker
from services.worker.vlm_gate import VLM_TYPES, VlmHealthGate, from_env
from simai_ai.providers.testing import load_example

T0 = 1760342400000  # conftest 와 같은 기준 시각
VLM_INPUT = load_example("job_request.vlm_rationale.json")["payload"]["input"]
VLM_RESULT = load_example("job_callback.vlm_rationale.json")["payload"]["result"]
GLTF = {"gltf_uri": "s3://simai-site/scene/v1.glb"}


class Health:
    def __init__(self) -> None:
        self.ok = False
        self.calls = 0

    def __call__(self, _url):
        self.calls += 1
        return self.ok, "HTTP 200" if self.ok else "ConnectionRefusedError: [Errno 111]"


def _worker(client, gate) -> Worker:
    handlers = {"vlm_rationale": lambda ctx: copy.deepcopy(VLM_RESULT), "static_reconstruct": lambda ctx: GLTF}
    return Worker(client, handlers, clock_ms=lambda: T0 + int(time.monotonic() * 1000), monotonic=time.monotonic, wait_ms=0,
                  type_gate=gate, pause_s=0.01)


def test_vlm_jobs_wait_in_queue_until_vllm_health_then_are_leased(ec2, client, caplog):
    health = Health()
    clock = [0.0]
    gate = VlmHealthGate("http://127.0.0.1:8000/health", probe=health, monotonic=lambda: clock[0], ttl_s=5)
    vlm = ec2.add_job("vlm_rationale", copy.deepcopy(VLM_INPUT), priority="VLM_FAST")
    bg = ec2.add_job("static_reconstruct")
    worker = _worker(client, gate)
    with caplog.at_level(logging.INFO, logger="services.worker.vlm_gate"):
        assert worker.run_once() is Outcome.ACCEPTED  # VLM 유형은 빼고 대여: 현장 세팅·정적 맵 쪽은 바로 돈다
        assert ec2.jobs[bg].state == "SUCCEEDED" and ec2.jobs[vlm].state != "SUCCEEDED"
        assert ec2.bodies("lease")[0]["payload"]["job_types"] == ["static_reconstruct"]
        assert worker.run_once() is Outcome.NO_JOB  # 대기열에 남은 VLM 작업은 시도를 쓰지 않는다
        assert health.calls == 1  # ttl 안에서는 다시 묻지 않는다
        health.ok = True
        clock[0] += 5
        assert worker.run_once() is Outcome.ACCEPTED
    assert ec2.jobs[vlm].state == "SUCCEEDED" and ec2.jobs[vlm].attempt == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("VLM 미준비" in m for m in msgs) == 1 and sum("VLM 준비됨" in m for m in msgs) == 1  # 바뀔 때만


def test_gate_does_not_touch_other_types_or_probe_without_vlm_types():
    health = Health()
    gate = VlmHealthGate("u", probe=health)
    assert gate(["site_setup", "vlm_zone_draft"]) == ["site_setup", "vlm_zone_draft"]  # zone draft 는 VLM 없이 기하 후보를 낸다
    assert health.calls == 0
    assert gate(sorted(VLM_TYPES | {"tbm_report"})) == ["tbm_report"]


def test_only_vlm_types_left_pauses_instead_of_empty_lease(ec2, client):
    gate = VlmHealthGate("u", probe=Health())
    worker = Worker(client, {"vlm_rationale": lambda ctx: VLM_RESULT}, clock_ms=lambda: T0, monotonic=time.monotonic, wait_ms=0,
                    type_gate=gate, pause_s=0.01)
    assert worker.run_once() is Outcome.PAUSED and ec2.bodies("lease") == []


@pytest.mark.parametrize("env, url", [
    ({}, None), ({"SIMAI_WORKER_VLM_GATE": "0"}, None),
    ({"SIMAI_WORKER_VLM_GATE": "health"}, "http://127.0.0.1:8000/health"),
    ({"SIMAI_WORKER_VLM_GATE": "health", "SIMAI_VLLM_PORT": "8123"}, "http://127.0.0.1:8123/health"),
])
def test_from_env(env, url):
    gate = from_env(env)
    assert (gate.url if gate else None) == url


def test_from_env_rejects_unknown_mode():
    with pytest.raises(ValueError, match="health"):
        from_env({"SIMAI_WORKER_VLM_GATE": "yes"})
