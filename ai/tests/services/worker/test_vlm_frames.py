"""VLM 작업 프레임 관문 (S15P31A101-270): 객체 저장소에서 받은 프레임을 얼굴 처리 뒤에만 돌려주고, 하나도 안 남으면 작업 실패."""

from __future__ import annotations

import numpy as np
import pytest

from services.worker.handlers import JobContext, JobFailed
from services.worker.objects import ObjectStoreError
from services.worker.vlm_frames import decode_image, gate_input_frames
from simai_ai.vlm.face_blur import Person

FACE_KPS = ((110.0, 110.0, 0.9), (104.0, 100.0, 0.9), (116.0, 100.0, 0.9), (95.0, 102.0, 0.8), (125.0, 102.0, 0.8))
PERSON = Person(bbox=(65.0, 60.0, 155.0, 300.0), keypoints=FACE_KPS + (None,) * 12)


def _ctx() -> JobContext:
    return JobContext(job_id="j", lease_id="l", job_type="vlm_rationale", attempt=1, input={})


class Store:
    def __init__(self, objects: dict[str, bytes], error: ObjectStoreError | None = None) -> None:
        self.objects, self.error = objects, error

    def get(self, ctx, uri):
        if self.error is not None:
            raise self.error
        return self.objects[uri]

    def put(self, ctx, name, content, content_type):  # pragma: no cover - 쓰지 않는다
        raise AssertionError("흐린 이미지는 저장소에 올리지 않는다")


class Provider:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def people(self, image):
        if self.error is not None:
            raise self.error
        return [PERSON]


def _decode(data: bytes) -> np.ndarray:
    if data == b"broken":
        raise ValueError("깨진 JPEG")
    return np.full((320, 240, 3), int(data), np.uint8)


def _frames(*uris_policies):
    return [{"camera_id": "cam1", "frame_uri": u, "face_policy": p} for u, p in uris_policies]


def test_each_frame_uses_its_own_policy():
    store = Store({"s3://a": b"10", "s3://b": b"20"})
    result = gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED"), ("s3://b", "EXCLUDED")), Provider(), decode=_decode)
    assert [(f.source, f.face_policy) for f in result.frames] == [("s3://a", "BLURRED"), ("s3://b", "EXCLUDED")]
    assert (result.frames[1].pixels[100:120, 100:120] == 128).all()


def test_partial_failure_keeps_processed_frames():
    store = Store({"s3://a": b"10", "s3://b": b"broken"})
    result = gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED"), ("s3://b", "BLURRED")), Provider(), decode=_decode)
    assert [f.source for f in result.frames] == ["s3://a"]
    assert [(d.source, d.reason) for d in result.dropped] == [("s3://b", "decode")]


def test_no_frames_left_fails_job_not_retryable_for_decode():
    store = Store({"s3://a": b"broken"})
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and not e.value.retryable


def test_no_frames_left_after_detector_failure_is_retryable():
    store = Store({"s3://a": b"10"})
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(RuntimeError("CUDA OOM")), decode=_decode)
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and e.value.retryable


def test_fetch_failure_fails_job_with_store_retryability():
    store = Store({}, error=ObjectStoreError("서명 만료", retryable=True))
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "FRAME_FETCH_FAILED" and e.value.retryable


def test_decode_image_rejects_garbage():
    pytest.importorskip("cv2")
    with pytest.raises(ValueError):
        decode_image(b"not an image")


def test_frames_are_fetched_in_parallel_and_preserve_input_order():
    import threading
    import time

    uris = [f"s3://bucket/f{i}.jpg" for i in range(4)]
    policies = ["BLURRED", "EXCLUDED", "BLURRED", "EXCLUDED"]
    barrier = threading.Barrier(4, timeout=2.0)
    lock = threading.Lock()
    active = 0
    max_active = 0

    class ParallelStore:
        def get(self, ctx, uri):
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            try:
                barrier.wait()  # 4장이 동시에 들어와야 통과한다
                idx = uris.index(uri)
                # 뒤쪽 프레임이 먼저 끝나도 입력 순서를 유지하는지 확인
                time.sleep(0.01 * (3 - idx))
                return str(10 + idx * 10).encode()
            finally:
                with lock:
                    active -= 1

        def put(self, ctx, name, content, content_type):  # pragma: no cover
            raise AssertionError

    frames = [{"camera_id": f"cam{i + 1}", "frame_uri": u, "face_policy": p} for i, (u, p) in enumerate(zip(uris, policies, strict=True))]
    result = gate_input_frames(_ctx(), ParallelStore(), frames, Provider(), decode=_decode)
    assert max_active == 4
    assert [(f.source, f.face_policy) for f in result.frames] == list(zip(uris, policies, strict=True))
    assert result.dropped == ()


def test_face_processing_runs_only_within_remaining_time_budget_and_keeps_fail_closed():
    now = [0.0]
    uris = [f"s3://bucket/f{i}.jpg" for i in range(4)]
    store = Store({u: str(10 + i * 10).encode() for i, u in enumerate(uris)})
    detected: list[int] = []

    class TimedProvider:
        def people(self, image):
            detected.append(int(image[0, 0, 0]))
            now[0] += 2.0  # 프레임당 검출에 2.0초 소비 (예산 3.5초면 2장 처리 뒤 4.0초가 돼 나머지 2장은 건너뛴다)
            return [PERSON]

    ctx = JobContext(
        job_id="j",
        lease_id="l",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        deadline=3.5,
        monotonic=lambda: now[0],
    )
    frames = _frames(*((u, "BLURRED") for u in uris))
    result = gate_input_frames(ctx, store, frames, TimedProvider(), decode=_decode)
    assert detected == [10, 20], "예산 소진 뒤 남은 프레임에는 pose 검출을 돌리지 않는다"
    assert [f.source for f in result.frames] == ["s3://bucket/f0.jpg", "s3://bucket/f1.jpg"]
    assert [(d.source, d.reason) for d in result.dropped] == [
        ("s3://bucket/f2.jpg", "budget"),
        ("s3://bucket/f3.jpg", "budget"),
    ]


def test_all_frames_dropped_by_budget_fails_retryable_without_detector_calls():
    now = [0.0]

    class SlowFetchStore:
        def get(self, ctx, uri):
            now[0] = 5.0  # 다운로드가 끝났을 때 예산(5.0초) 소진
            return b"10"

        def put(self, ctx, name, content, content_type):  # pragma: no cover
            raise AssertionError

    class CountingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def people(self, image):
            self.calls += 1
            return [PERSON]

    ctx = JobContext(
        job_id="j",
        lease_id="l",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        deadline=5.0,
        monotonic=lambda: now[0],
    )
    provider = CountingProvider()
    with pytest.raises(JobFailed) as e:
        gate_input_frames(ctx, SlowFetchStore(), _frames(("s3://a", "BLURRED")), provider, decode=_decode)
    assert e.value.code == "NO_FACE_SAFE_FRAMES" and e.value.retryable
    assert provider.calls == 0


def test_expired_budget_before_fetch_does_not_call_store():
    class ForbiddenStore:
        def get(self, ctx, uri):
            raise AssertionError("시간 예산이 없으면 store.get 을 부르면 안 된다")

        def put(self, ctx, name, content, content_type):  # pragma: no cover
            raise AssertionError

    ctx = JobContext(
        job_id="j",
        lease_id="l",
        job_type="vlm_rationale",
        attempt=1,
        input={},
        deadline=5.0,
        monotonic=lambda: 5.0,
    )
    with pytest.raises(JobFailed) as e:
        gate_input_frames(ctx, ForbiddenStore(), _frames(("s3://a", "BLURRED"), ("s3://b", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "FRAME_FETCH_FAILED" and e.value.retryable


def test_http_object_store_parallel_fetch_over_loopback_within_budget():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from services.worker.objects import HttpObjectStore
    from services.worker.transport import HttpTransport

    barrier = threading.Barrier(4, timeout=2.0)
    state: dict[str, int] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            req = json.loads(raw)
            key = req["uri"].removeprefix("s3://bucket/")
            reply = {
                "op": "GET",
                "uri": req["uri"],
                "method": "GET",
                "url": f"http://127.0.0.1:{state['port']}/bucket/{key}?sig=ok",
                "headers": {},
                "expires_at_ms": 1,
            }
            body = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            barrier.wait()  # 4개 GET 이 동시에 열려야 통과
            idx = int(self.path.split("/bucket/f")[1].split(".")[0])
            body = str(20 + idx * 10).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state["port"] = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        store = HttpObjectStore(HttpTransport(f"http://127.0.0.1:{state['port']}", "worker-token"))
        ctx = JobContext(job_id="job-1", lease_id="lease-1", job_type="vlm_rationale", attempt=1, input={}, timeout_ms=5000)
        frames = _frames(*((f"s3://bucket/f{i}.jpg", "BLURRED") for i in range(4)))
        result = gate_input_frames(ctx, store, frames, Provider(), decode=_decode)
        assert [f.source for f in result.frames] == [f"s3://bucket/f{i}.jpg" for i in range(4)]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- S15P31A101-408 같은 Pod 로컬 사본

EVENT = "0192a6f0-7c1e-7b3a-9f2d-3c4b5a6d7e8f"


def _local(state, name: str, content: bytes, *, sha: bytes | None = None) -> dict:
    import hashlib

    rel = f"event-frames/{EVENT}/{name}.jpg"
    path = state / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content if sha is None else sha).hexdigest()
    return {"path": rel, "sha256": f"sha256:{digest}", "size_bytes": len(content)}


class CountingStore(Store):
    def __init__(self, objects, errors=()):
        super().__init__(objects)
        self.calls: list[str] = []
        self.errors = list(errors)

    def get(self, ctx, uri):
        self.calls.append(uri)
        if self.errors:
            raise self.errors.pop(0)
        return self.objects[uri]


def test_local_copy_is_read_first_and_store_is_not_called(tmp_path):
    from services.worker.vlm_frames import LocalFrames

    frames = _frames(("s3://a", "BLURRED"), ("s3://b", "BLURRED"))
    frames[0]["local_copy"] = _local(tmp_path, "0-cam1-1", b"10")
    frames[1]["local_copy"] = _local(tmp_path, "1-cam1-2", b"20")
    store = CountingStore({})
    result = gate_input_frames(_ctx(), store, frames, Provider(), decode=_decode, local=LocalFrames(tmp_path))
    assert store.calls == []
    assert [f.source for f in result.frames] == ["s3://a", "s3://b"]  # 얼굴 처리 관문은 그대로(VlmFrame 출처는 frame_uri)
    assert all(f.face_policy == "BLURRED" for f in result.frames)


def test_missing_local_copy_falls_back_to_store(tmp_path):
    from services.worker.vlm_frames import LocalFrames

    frames = _frames(("s3://a", "BLURRED"), ("s3://b", "BLURRED"))
    frames[0]["local_copy"] = _local(tmp_path, "0-cam1-1", b"10")
    frames[1]["local_copy"] = {"path": f"event-frames/{EVENT}/gone.jpg", "sha256": "sha256:" + "0" * 64, "size_bytes": 2}
    store = CountingStore({"s3://b": b"20"})
    result = gate_input_frames(_ctx(), store, frames, Provider(), decode=_decode, local=LocalFrames(tmp_path))
    assert store.calls == ["s3://b"]
    assert [f.source for f in result.frames] == ["s3://a", "s3://b"]


def test_without_local_reader_local_copy_is_ignored(tmp_path):
    frames = _frames(("s3://a", "BLURRED"))
    frames[0]["local_copy"] = _local(tmp_path, "0-cam1-1", b"10")
    store = CountingStore({"s3://a": b"30"})
    gate_input_frames(_ctx(), store, frames, Provider(), decode=_decode)  # 다른 Pod 의 워커
    assert store.calls == ["s3://a"]


def test_sha256_or_size_mismatch_rejects_local_copy(tmp_path, caplog):
    from services.worker.vlm_frames import LocalFrames

    local = LocalFrames(tmp_path)
    assert local.read(_local(tmp_path, "0-cam1-1", b"10", sha=b"other")) is None
    assert "reason=sha256" in caplog.text
    copy = _local(tmp_path, "1-cam1-1", b"10")
    copy["size_bytes"] = 3
    assert local.read(copy) is None and "reason=size" in caplog.text
    frames = _frames(("s3://a", "BLURRED"))
    frames[0]["local_copy"] = _local(tmp_path, "2-cam1-1", b"10", sha=b"other")
    store = CountingStore({"s3://a": b"40"})
    result = gate_input_frames(_ctx(), store, frames, Provider(), decode=_decode, local=local)
    assert store.calls == ["s3://a"] and [f.source for f in result.frames] == ["s3://a"]


@pytest.mark.parametrize("path", [
    f"event-frames/{EVENT}/../../journal/x.jpg",
    f"/etc/event-frames/{EVENT}/x.jpg",
    "event-frames\\" + EVENT + "\\x.jpg",
    "journal/x.jpg",
    f"event-frames/{EVENT}/x.png",
])
def test_path_outside_event_frames_is_rejected_by_form(tmp_path, path, caplog):
    from services.worker.vlm_frames import LocalFrames

    (tmp_path / "journal").mkdir()
    (tmp_path / "journal" / "x.jpg").write_bytes(b"10")
    assert LocalFrames(tmp_path).read({"path": path, "sha256": "sha256:" + "0" * 64, "size_bytes": 2}) is None
    assert "reason=form" in caplog.text


def test_symlink_escaping_state_dir_is_rejected(tmp_path, caplog):
    import hashlib

    from services.worker.vlm_frames import LocalFrames

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "0-cam1-1.jpg").write_bytes(b"10")
    state = tmp_path / "state"
    link = state / "event-frames" / EVENT  # 이벤트 폴더가 상태 폴더 밖을 가리킨다
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        try:  # Windows 일반 권한: 심볼릭 링크 대신 디렉터리 정션(resolve 가 따라간다)
            import _winapi

            _winapi.CreateJunction(str(outside), str(link))
        except (ImportError, OSError):
            pytest.skip("이 환경에서는 링크를 만들 수 없다")
    copy = {"path": f"event-frames/{EVENT}/0-cam1-1.jpg", "sha256": "sha256:" + hashlib.sha256(b"10").hexdigest(),
            "size_bytes": 2}
    assert LocalFrames(state).read(copy) is None
    assert "reason=escape" in caplog.text


def test_local_frames_from_env():
    from pathlib import Path

    from services.worker.vlm_frames import LocalFrames

    assert LocalFrames.from_env({"SIMAI_STATE_DIR": "/srv/a/state"}).state_dir == Path("/srv/a/state")
    assert LocalFrames.from_env({"SIMAI_TIER0_MARKER": "/srv/b/state/tier0.live"}).state_dir == Path("/srv/b/state")
    assert LocalFrames.from_env({"SIMAI_ROOT": "/srv/c"}).state_dir == Path("/srv/c/state")


def test_store_not_yet_uploaded_is_retried_briefly():
    store = CountingStore({"s3://a": b"10"}, errors=[ObjectStoreError("GET 응답 403", retryable=False, status=403),
                                                     ObjectStoreError("GET 응답 404", retryable=False, status=404)])
    ctx = JobContext(job_id="j", lease_id="l", job_type="vlm_rationale", attempt=1, input={}, timeout_ms=5000)
    result = gate_input_frames(ctx, store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert store.calls == ["s3://a"] * 3 and [f.source for f in result.frames] == ["s3://a"]


def test_store_retry_gives_up_after_limit_or_on_config_error():
    from services.worker.vlm_frames import S3_TRIES

    errors = [ObjectStoreError("GET 응답 404", retryable=False, status=404) for _ in range(S3_TRIES)]
    store = CountingStore({"s3://a": b"10"}, errors=errors)
    with pytest.raises(JobFailed) as e:
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert e.value.code == "FRAME_FETCH_FAILED" and len(store.calls) == S3_TRIES
    store = CountingStore({"s3://a": b"10"}, errors=[ObjectStoreError("서명 거부 409", retryable=False)])
    with pytest.raises(JobFailed):
        gate_input_frames(_ctx(), store, _frames(("s3://a", "BLURRED")), Provider(), decode=_decode)
    assert len(store.calls) == 1  # 대여 상실 같은 서명 거부는 다시 하지 않는다
