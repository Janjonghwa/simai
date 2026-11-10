"""실제 AvatarLoop에 느린 외형을 주입해 포즈 송신 격리를 검증한다(GPU 없음)."""

import threading
import time
import copy
from types import SimpleNamespace

import pytest

from tests.hmr.test_avatar_loop import harness, snapshot


def wait_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.001)
    assert predicate()


def fake_pipeline(update):
    return SimpleNamespace(retain=lambda *_: None, track=lambda *_: None, register=lambda *_: None,
        update=update, config=SimpleNamespace(face_policy="EXCLUDED"))


@pytest.mark.parametrize("delay", [.5, 4.6])
def test_slow_appearance_does_not_block_pose_steps(rig, delay):
    clock, source, sock, sender, _, loop, _ = harness(rig, freeze=1)
    entered, release = threading.Event(), threading.Event()

    def update(*args, **kwargs):
        entered.set()
        release.wait(delay)

    loop.appearance = fake_pipeline(update)
    try:
        start = time.perf_counter()
        loop.step()
        elapsed = time.perf_counter() - start
        assert elapsed < .2, f"외형 {delay}s가 포즈 회차 {elapsed:.3f}s를 막음"
        assert entered.wait(1)
        for _ in range(4):
            clock[0] += 200
            source.item = snapshot(clock[0])
            start = time.perf_counter()
            loop.step()
            assert time.perf_counter() - start < .2
            sender.tick()
        assert sender.sent_batches_total == 4
    finally:
        release.set()
        if hasattr(loop, "close"):
            loop.close()


@pytest.mark.parametrize("correction", ["revision", "lost", "reentry", "rollback"])
def test_inflight_result_cannot_commit_after_identity_or_generation_change(rig, correction):
    clock, source, _, sender, _, loop, _ = harness(rig, freeze=1)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def update(frame, doc, snapshot, images, rig, states, upload, output, clock_ms, admit, masks, *, reserve_version):
        asset = copy.deepcopy(states["w-1"].asset)
        asset["appearance_version"] = reserve_version("w-1")
        entered.set()
        release.wait(2)
        # 취소 콜백을 무시하는 느린 외부 호출이어도 최종 결과를 버려야 한다.
        output.asset(asset)
        finished.set()
    loop.appearance = fake_pipeline(update)
    try:
        loop.step()
        assert entered.wait(1)
        if correction == "rollback":
            loop.reannounce_appearance("w-1", 0)
        else:
            clock[0] += 200
            source.item = snapshot(clock[0], revision=1, uncertain=True)
            if correction in {"lost", "reentry"}:
                source.item[0]["world"]["tracks"] = []
            loop.step()
            if correction == "reentry":
                clock[0] += 200
                source.item = snapshot(clock[0])
                loop.step()
        release.set()
        assert finished.wait(1)
        wait_until(lambda: loop._appearance_worker.discarded_results > 0)
        loop.close()
        loop._commit_appearance()
        assert all(asset["appearance_version"] != 1 for asset in sender._assets.values())
    finally:
        release.set()
        loop.close()


def test_completed_result_commits_atomically_and_pose_uses_new_version(rig):
    clock, source, sock, sender, _, loop, _ = harness(rig, freeze=1)
    def update(frame, doc, snapshot, images, rig, states, upload, output, clock_ms, admit, masks, *, reserve_version):
        asset = copy.deepcopy(states["w-1"].asset)
        asset["appearance_version"] = reserve_version("w-1")
        output.asset(asset)
    loop.appearance = fake_pipeline(update)
    try:
        loop.step()
        wait_until(lambda: loop._appearance_worker._result is not None)
        clock[0] += 200
        source.item = snapshot(clock[0])
        loop.step()
        sender.tick()
        assert loop.people["w-1"].version == 1
        assert loop.people["w-1"].history[1] is loop.people["w-1"].asset
        assert sock.messages[-1]["payload"]["poses"][0]["appearance_version"] == 1
        assert sender._assets["w-1"]["appearance_version"] == 1
    finally:
        loop.close()


def test_completed_result_is_discarded_when_next_scene_changes_revision(rig):
    clock, source, _, sender, _, loop, _ = harness(rig, freeze=1)
    def update(frame, doc, snapshot, images, rig, states, upload, output, clock_ms, admit, masks, *, reserve_version):
        asset = copy.deepcopy(states["w-1"].asset)
        asset["appearance_version"] = reserve_version("w-1")
        output.asset(asset)
    loop.appearance = fake_pipeline(update)
    try:
        loop.step()
        wait_until(lambda: loop._appearance_worker._result is not None)
        clock[0] += 200
        source.item = snapshot(clock[0], revision=1, uncertain=True)
        loop.step()
        assert loop.people["w-1"].revision == 1
        assert loop.people["w-1"].version == 0 and 1 not in loop.people["w-1"].history
        assert sender._assets["w-1"]["appearance_version"] == 0
    finally:
        loop.close()


def test_results_for_different_people_are_kept_until_pose_loop_commits(rig):
    from simai_ai.hmr.appearance_worker import AppearanceWorker

    _, source, _, sender, _, loop, _ = harness(rig, freeze=1)
    loop.step()
    other = copy.deepcopy(loop.people["w-1"])
    other.asset["world_id"] = "w-2"
    loop.people["w-2"] = other
    calls = []
    def update(frame, doc, snapshot, images, rig, states, upload, output, clock_ms, admit, masks, *, reserve_version):
        wid = "w-1" if not calls else "w-2"
        calls.append(wid)
        asset = copy.deepcopy(states[wid].asset)
        asset["appearance_version"] = reserve_version(wid)
        output.asset(asset)
    loop.appearance = fake_pipeline(update)
    worker = loop._appearance_worker = AppearanceWorker(loop)
    meta, images = source.item
    try:
        worker.offer(None, meta["world"], meta, images, None, {})
        wait_until(lambda: worker._result is not None)
        worker.offer(None, meta["world"], meta, images, None, {})
        wait_until(lambda: worker._result is not None and len(worker._result[1]) == 2)
        loop._commit_appearance()
        assert loop.people["w-1"].version == loop.people["w-2"].version == 1
        assert sender._assets.keys() == {"w-1", "w-2"}
    finally:
        loop.close()


def test_latest_queue_and_close_discard_pending_request(rig):
    clock, source, _, _, _, loop, _ = harness(rig, freeze=1)
    entered, release = threading.Event(), threading.Event()
    calls = []
    def update(*args, **kwargs):
        calls.append(args[1]["t"])
        entered.set()
        release.wait(2)
    loop.appearance = fake_pipeline(update)
    try:
        loop.step()
        assert entered.wait(1)
        for _ in range(4):
            clock[0] += 200
            source.item = snapshot(clock[0])
            loop.step()
        worker = loop._appearance_worker
        assert worker._pending[3]["t"] == clock[0] and worker.dropped == 3
        closer = threading.Thread(target=loop.close)
        closer.start()
        wait_until(lambda: worker._stopped)
        assert worker._pending is None and worker._result is None
        release.set()
        closer.join(2)
        assert not closer.is_alive() and not worker._thread.is_alive()
        assert len(calls) == 1
    finally:
        release.set()
        loop.close()


@pytest.mark.parametrize("count", [1, 4])
@pytest.mark.parametrize("delay", [.5, 4.6])
def test_fake_model_segmentation_benchmark(rig, count, delay):
    clock, source, _, sender, _, loop, _ = harness(rig, freeze=1)
    entered = threading.Event()
    def update(*args, **kwargs):
        entered.set()
        time.sleep(delay)
    loop.appearance = fake_pipeline(update)
    loop.segmentation = SimpleNamespace(failed=True, prepare=lambda *_: {})
    def scene():
        meta, images = snapshot(clock[0])
        original = meta["world"]["tracks"][0]
        meta["world"]["tracks"] = [dict(copy.deepcopy(original), world_id=f"w-{i + 1}") for i in range(count)]
        return meta, images
    try:
        source.item = scene()
        loop.step()
        assert entered.wait(1)
        durations = []
        for _ in range(5):
            clock[0] += 200
            source.item = scene()
            start = time.perf_counter()
            loop.step()
            durations.append(time.perf_counter() - start)
            sender.tick()
        limit = .2 if count == 1 else .5
        assert max(durations) < limit
        assert sender.sent_batches_total == 5
        print(f"BENCH people={count} appearance_s={delay} pose_max_ms={max(durations) * 1000:.2f} "
              f"pose_mean_ms={sum(durations) / len(durations) * 1000:.2f}")
    finally:
        loop.close()


def test_real_pipeline_upload_retry_and_atomic_commit_on_worker(rig):
    pytest.importorskip("cv2")
    # 도우미 모듈(test_appearance)이 맨 위에서 torch 를 건너뛰므로, torch 가 없으면 반쯤 import 된 모듈을 읽게 된다(-594).
    pytest.importorskip("torch")
    from simai_ai.hmr.appearance_worker import AppearanceWorker
    from simai_ai.hmr.stream import AvatarLoop, PersonState
    from simai_ai.providers.contract import schema_errors
    from tests.hmr.test_appearance import pipeline_scene

    pipeline, original, messages, uploaded, args, example = pipeline_scene(rig)
    frame, doc, snap, images, cameras, _, upload, sender, clock_ms, _ = args
    sender.discard_poses = lambda: None
    state = PersonState(revision=0, stable=30, version=0, high_version=0,
                        asset=original.asset, baked=original.baked, history=original.history)
    loop = AvatarLoop(None, SimpleNamespace(check=lambda: SimpleNamespace(admit=True)), None, upload, sender,
                      clock_ms=clock_ms, appearance=pipeline)
    loop.people = {"w-1": state}
    initial = copy.deepcopy(state.asset)
    png = upload.png
    def flaky(*params):
        uri = png(*params)
        if len(uploaded) == 2:
            raise OSError("두 번째 PNG 응답 유실")
        return uri
    upload.png = flaky
    worker = loop._appearance_worker = AppearanceWorker(loop)
    try:
        worker.offer(frame, doc, snap, images, cameras, {})
        wait_until(lambda: loop.timing_counts["appearance"] == 1)
        assert not messages and state.asset == initial and state.version == 0
        worker.offer(frame, doc, snap, images, cameras, {})
        wait_until(lambda: worker._result is not None)
        assert not messages and state.version == 0  # 작업자는 자산을 직접 공개하지 않는다.
        loop._commit_appearance()
        assert state.version == 1 and len(messages) == 1
        assert uploaded[0][2] is uploaded[2][2] and uploaded[1][2] is uploaded[3][2]
        example["payload"] = messages[0]
        assert schema_errors(example, "avatar_asset") == []
        assert state.history[0] == initial
    finally:
        loop.close()
