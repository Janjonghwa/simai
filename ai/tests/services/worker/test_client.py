"""I5 대여 클라이언트: 계약 예시 해석, HTTP 상태 분류, 백오프, 만든 메시지의 계약 적합성."""

import copy
import json

import pytest

from services.worker.client import (
    CALLBACK_SCHEMA_VERSION,
    JOB_TYPES,
    SCHEMA_VERSION,
    AuthError,
    Backoff,
    Lease,
    LeaseClient,
    LeaseStatus,
    RequestRejected,
    RetryableError,
)
from services.worker.transport import (
    Response,
    TransportError,
    heartbeat_path,
    result_path,
)
from simai_ai.providers.contract import contracts_dir, schema_errors
from simai_ai.providers.testing import load_example


class Scripted:
    """정해 둔 응답(또는 예외)을 차례로 돌려주는 전송."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.sent = []

    def post(self, path, body, timeout_s):
        self.sent.append((path, body, timeout_s))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _client(transport, sleep=lambda s: None):
    return LeaseClient(transport, "worker-01", clock_ms=lambda: 1760342400450, monotonic=lambda: 0.0, sleep=sleep)


def test_backoff_doubles_to_cap():
    b = Backoff()
    assert [b.delay(n) for n in range(1, 10)] == [0.5, 1, 2, 4, 8, 16, 30, 30, 30]
    with pytest.raises(ValueError):
        Backoff(initial_s=0)
    with pytest.raises(ValueError):
        Backoff(initial_s=2, max_s=1)


def test_lease_from_contract_example():
    """남은 시간은 EC2 시계끼리의 차이(lease_expires_at − sent_at)로만 계산한다."""
    lease = Lease.from_message(load_example("job_request.vlm_rationale.json"))
    assert (lease.job_id, lease.attempt, lease.job_type) == ("job-20261017-0042", 1, "vlm_rationale")
    assert (lease.ttl_ms, lease.time_left_ms, lease.heartbeat_interval_ms) == (30000, 5000, 10000)


def test_job_request_2x_is_refused_not_retried():
    """job_request 2.x 의 attempt 는 대여 순번이라 반납(시도 번호)과 섞을 수 없다(3.0.0, -197). 받지 않고 다시 보내지도 않는다.
    대여 요청 오류(RequestRejected)라 Worker.run 이 멈춘다(설정 오류: EC2 와 워커 계약 버전이 다르다)."""
    old = load_example("job_request.static_reconstruct.json")
    old["schema_version"] = "2.0.0"
    t = Scripted(Response(200, old))
    with pytest.raises(RequestRejected, match="2.0.0"):
        _client(t).lease_once(["static_reconstruct"], 0)
    assert len(t.sent) == 1  # 다시 보내지 않았다
    assert Lease.from_message(load_example("job_request.static_reconstruct.json")).attempt >= 1  # 3.0.0 은 받는다


def test_lease_status_from_contract_examples():
    assert LeaseStatus.from_message(load_example("job_lease_status.active.json")) == LeaseStatus("ACTIVE", None, 30000)
    # 2.1.0(-597): site_setup 대여의 작업 시작 신호와 주기
    live = LeaseStatus.from_message(load_example("job_lease_status.active_live_work.json"))
    assert live.live_work_active is True and live.heartbeat_interval_ms == 5000 and live.status == "ACTIVE"
    assert LeaseStatus.from_message(load_example("job_lease_status.accepted.json")).live_work_active is False
    decided = LeaseStatus.from_message(load_example("job_lease_status.active_height_decision.json"))
    assert decided.height_decision == {"decision": "CORRECTED",
                                       "camera_heights_m": {"cam_A": 1.5, "cam_B": 1.5, "cam_C": 1.38, "cam_D": 1.5}}
    assert LeaseStatus.from_message(load_example("job_lease_status.active.json")).height_decision is None
    bad = copy.deepcopy(load_example("job_lease_status.active_height_decision.json"))
    bad["payload"]["height_decision"]["camera_heights_m"]["cam_C"] = -1
    assert LeaseStatus.from_message(bad).height_decision is None  # 선택 값이 틀리면 대여는 두고 결정만 버린다
    started = LeaseStatus.from_message(load_example("job_lease_status.active_camera_check_start.json"))
    assert started.camera_check_start == 2 and started.heartbeat_interval_ms == 2000
    assert LeaseStatus.from_message(load_example("job_lease_status.active.json")).camera_check_start is None
    bad = copy.deepcopy(load_example("job_lease_status.active_camera_check_start.json"))
    bad["payload"]["camera_check_start"]["attempt"] = True
    assert LeaseStatus.from_message(bad).camera_check_start is None  # 선택 값이 틀리면 대여는 두고 요청만 버린다
    assert LeaseStatus.from_message(load_example("job_lease_status.duplicate.json")).status == "DUPLICATE"
    rejected = LeaseStatus.from_message(load_example("job_lease_status.rejected_expired.json"))
    assert (rejected.status, rejected.reason) == ("REJECTED", "LEASE_EXPIRED")


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"lease_expires_at": 1760342400500}, id="expires-at-leased"),
        pytest.param({"lease_expires_at": 1760342400000}, id="expires-before-leased"),
        pytest.param({"heartbeat_interval_ms": 30000}, id="interval-not-below-hold"),
        pytest.param({"attempt": 3}, id="attempt-over-max"),
        pytest.param({"max_attempts": None}, id="no-max-attempts"),
    ],
)
def test_inconsistent_lease_is_not_started(change):
    """어긋난 대여(이미 만료, 주기 ≥ 유지 시간, attempt > max_attempts)로는 작업을 시작하지 않는다."""
    msg = copy.deepcopy(load_example("job_request.vlm_rationale.json"))
    for k, v in change.items():
        if v is None:
            del msg["payload"][k]
        else:
            msg["payload"][k] = v
    with pytest.raises(RetryableError):
        Lease.from_message(msg)


def test_broken_reply_is_retryable():
    bad = copy.deepcopy(load_example("job_lease_status.active.json"))
    del bad["payload"]["lease_expires_at"]
    with pytest.raises(RetryableError):
        LeaseStatus.from_message(bad)
    with pytest.raises(RetryableError):
        Lease.from_message({"msg_type": "job_request", "payload": {}})


def test_lease_request_is_valid_and_long_polls():
    t = Scripted(Response(204, None))
    assert _client(t).lease_once(["vlm_rationale", "static_reconstruct"], 25000) is None
    path, body, timeout_s = t.sent[0]
    assert path == "/api/worker/jobs/lease"
    assert schema_errors(body, "job_lease_request") == []
    assert body["schema_version"] == SCHEMA_VERSION and body["source_id"] == "worker-01"
    assert timeout_s > 25.0  # 읽기 시간 제한은 long-poll 대기보다 길다


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        pytest.param(Response(401, None), AuthError, id="401"),
        pytest.param(Response(403, None), AuthError, id="403"),
        pytest.param(Response(400, {"error": "bad"}), RequestRejected, id="400"),
        pytest.param(Response(503, None), RetryableError, id="503"),
        pytest.param(Response(429, None), RetryableError, id="429"),
        pytest.param(Response(200, None), RetryableError, id="200-no-body"),
        pytest.param(TransportError("끊김"), RetryableError, id="network"),
    ],
)
def test_status_classification(reply, error):
    with pytest.raises(error):
        _client(Scripted(reply)).lease_once(["static_reconstruct"], 0)


def test_heartbeat_and_submit_messages_are_valid():
    lease = Lease.from_message(load_example("job_request.vlm_rationale.json"))
    active = load_example("job_lease_status.active.json")
    rejected = load_example("job_lease_status.rejected_expired.json")
    t = Scripted(Response(200, active), Response(409, rejected))
    client = _client(t)

    assert client.heartbeat(lease, 0.25).status == "ACTIVE"
    result = load_example("job_callback.vlm_rationale.json")["payload"]["result"]
    msg = client.callback_message(lease, "SUCCEEDED", started_at=1, finished_at=2, result=result)
    reply = client.submit(lease, msg, deadline=10.0)
    assert (reply.status, reply.reason) == ("REJECTED", "LEASE_EXPIRED")  # 409 도 본문 상태로 해석한다

    (hb_path, hb, _), (res_path, res, _) = t.sent
    assert hb_path == heartbeat_path(lease.job_id) and res_path == result_path(lease.job_id)
    assert schema_errors(hb, "job_heartbeat") == []
    assert schema_errors(res, "job_callback") == []
    failed = client.callback_message(lease, "FAILED", started_at=None, finished_at=2, error={"code": "X", "message": ""})
    assert schema_errors(failed, "job_callback") == []
    assert res["schema_version"] == failed["schema_version"] == CALLBACK_SCHEMA_VERSION and hb["schema_version"] == SCHEMA_VERSION
    released = client.callback_message(lease, "FAILED", started_at=None, finished_at=2, error={
        "code": "TIER0_PRIORITY", "message": "", "retryable": True, "consume_attempt": False})
    assert schema_errors(released, "job_callback") == []
    # 반납은 Tier 0 우선 사유만: 처리기 실패를 반납으로 보내면 계약 위반이다
    misuse = client.callback_message(lease, "FAILED", started_at=1, finished_at=2, error={
        "code": "HANDLER_ERROR", "message": "", "retryable": True, "consume_attempt": False})
    assert schema_errors(misuse, "job_callback") != []


def test_job_id_is_escaped_in_path():
    assert heartbeat_path("a/b c") == "/api/worker/jobs/a%2Fb%20c/heartbeat"


def _examples(prefix):
    from simai_ai.providers.testing import example_path

    folder = example_path("job_request.vlm_rationale.json").parent
    return {p.name: load_example(p.name) for p in sorted(folder.glob(f"{prefix}*.json"))}


def test_contract_examples_tell_consistent_story():
    """I5 예시끼리 계약 규칙과 맞는지: 대여마다 lease_id 가 다르고, 하트비트·ACTIVE 는 실행 상한·대여 안,
    성공으로 끝난 작업은 다시 대여되지 않고, 재할당은 앞 대여의 재시도 가능한 실패 뒤에 온다."""
    requests = _examples("job_request.")
    callbacks = _examples("job_callback.")
    statuses = _examples("job_lease_status.")
    beats = _examples("job_heartbeat")

    owner = {}  # lease_id → job_id
    for name, msg in [*requests.items(), *callbacks.items(), *statuses.items(), *beats.items()]:
        p = msg["payload"]
        assert owner.setdefault(p["lease_id"], p["job_id"]) == p["job_id"], f"{name}: 다른 작업의 lease_id"
    attempts = {}
    for name, msg in requests.items():
        lease = Lease.from_message(msg)  # 값 사이 관계 확인 포함
        assert attempts.setdefault((lease.job_id, lease.attempt), lease.lease_id) == lease.lease_id, name

    by_lease = {m["payload"]["lease_id"]: m["payload"] for m in requests.values()}
    for name, msg in [*beats.items(), *[(n, m) for n, m in statuses.items() if m["payload"]["status"] == "ACTIVE"]]:
        req = by_lease[msg["payload"]["lease_id"]]
        assert msg["sent_at"] < req["leased_at"] + req["timeout_ms"], f"{name}: 실행 상한이 지난 대여"
        assert req["leased_at"] < msg["sent_at"], name

    succeeded = {m["payload"]["job_id"]: m["sent_at"] for m in callbacks.values() if m["payload"]["status"] == "SUCCEEDED"}
    for name, msg in requests.items():
        p = msg["payload"]
        if p["job_id"] in succeeded:
            assert msg["sent_at"] < succeeded[p["job_id"]], f"{name}: 성공으로 끝난 작업을 다시 대여"
        if p["attempt"] > 1:
            prior = [
                c for c in callbacks.values()
                if c["payload"]["job_id"] == p["job_id"] and c["payload"]["lease_id"] != p["lease_id"]
                and c["payload"]["status"] == "FAILED" and c["payload"]["error"].get("retryable") and c["sent_at"] < msg["sent_at"]
            ]
            assert prior, f"{name}: 재할당 앞에 재시도 가능한 실패가 없다"

    for name, msg in statuses.items():
        p = msg["payload"]
        if p["status"] in ("ACCEPTED", "DUPLICATE"):
            subs = [c for c in callbacks.values() if c["payload"]["lease_id"] == p["lease_id"] and c["sent_at"] <= msg["sent_at"]]
            assert subs, f"{name}: 앞선 제출이 없다"


def test_job_types_match_contract():
    schema = json.loads((contracts_dir() / "messages" / "job_request.schema.json").read_text(encoding="utf-8"))
    assert JOB_TYPES == frozenset(schema["$defs"]["job_type"]["enum"])

@pytest.mark.parametrize("status", [404, 409])
def test_heartbeat_http_lease_loss(status):
    lease = Lease.from_message(load_example("job_request.vlm_rationale.json"))
    reply = _client(Scripted(Response(status, None))).heartbeat(lease)
    assert reply.status == "REJECTED"


def test_backoff_many_failures_stays_capped():
    assert Backoff().delay(10000) == 30
