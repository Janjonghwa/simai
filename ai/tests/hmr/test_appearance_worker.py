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
    return SimpleNamespace(retain=lambda *_: None, track=lambda *_, **__: None, register=lambda *_: None,
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


def test_owned_images_copy_only_crops_and_survive_ring_buffer_reuse():
    """공유 통로 읽기의 고리 버퍼 칸이 다시 쓰여도 외형 작업자 사본은 그대로다(-755)."""
    import numpy as np
    from simai_ai.hmr.appearance_worker import owned_images

    ring = np.zeros((8, 10, 3), np.uint8)
    ring[1:4, 2:6] = 200
    plain = np.full((2, 2, 3), 7, np.uint8)
    snap = {"frames": {"cam1": {"crops_xyxy": [[2, 1, 6, 4], [-5, -5, 0, 0]]}, "cam2": {}}}
    owned = owned_images(snap, {"cam1": ring, "cam2": plain})
    ring[:] = 0  # 다음 읽기가 같은 칸의 크롭을 지운다
    plain[:] = 0
    assert owned["cam1"][1:4, 2:6].min() == 200 and owned["cam1"].sum() == 200 * 3 * 4 * 3
    assert owned["cam2"].min() == 7


def test_async_appearance_keeps_images_after_source_buffers_are_rewritten(rig):
    clock, source, _, _, _, loop, _ = harness(rig, freeze=1)
    entered, release, seen = threading.Event(), threading.Event(), []
    def update(frame, doc, snapshot, images, *args, **kwargs):
        entered.set()
        release.wait(2)
        seen.append({cid: int(img.min()) for cid, img in images.items()})
    loop.appearance = fake_pipeline(update)
    doc, images = source.item
    for image in images.values():
        image[:] = 90
    try:
        loop.step()
        assert entered.wait(1)
        for image in images.values():
            image[:] = 0  # 고리 버퍼 칸 재사용 흉내
        release.set()
        wait_until(lambda: bool(seen))
        assert seen[0] and all(v == 90 for v in seen[0].values())
    finally:
        release.set()
        loop.close()


def test_worker_default_ignores_throttle_and_defers_only_when_gate_closed():
    """기본값은 감속으로 외형 분할을 미루지 않는다(-808, 아바타 시연 필수). 게이트가 닫혔을 때만 미룬다."""
    from simai_ai.hmr.appearance_worker import DEFAULT_SEG_MAX_INTERVAL_MS, AppearanceWorker

    assert DEFAULT_SEG_MAX_INTERVAL_MS is None
    gate_open = [True]
    loop = SimpleNamespace(
        gate=SimpleNamespace(check=lambda: SimpleNamespace(admit=gate_open[0]), recommended_interval_ms=lambda: 800),
    )
    worker = AppearanceWorker.__new__(AppearanceWorker)
    worker.loop, worker.seg_max_interval_ms = loop, DEFAULT_SEG_MAX_INTERVAL_MS
    assert worker._gate_state(check_throttle=True) == (True, "ok")
    gate_open[0] = False
    assert worker._gate_state(check_throttle=True) == (False, "gate_closed")


@pytest.mark.parametrize("mode", ["gate_closed", "throttled"])
def test_worker_defers_segmentation_when_gate_closed_or_throttled_and_runs_when_reopened(rig, mode):
    """분할 실행 직전에 아바타 게이트·감속 상태를 다시 확인해 닫혀 있으면 작업을 버리지 않고 다음 주기로 미룬다(-808)."""
    from simai_ai.geometry.rig import CameraRig
    from simai_ai.hmr.appearance_worker import AppearanceWorker
    from simai_ai.hmr.stream import SnapshotCalibration

    _, source, _, _, _, loop, _ = harness(rig, freeze=1)
    loop.step()
    gate_open = [mode != "gate_closed"]
    interval_ms = [800 if mode == "throttled" else 200]
    loop.gate = SimpleNamespace(
        check=lambda: SimpleNamespace(admit=gate_open[0]),
        recommended_interval_ms=lambda: interval_ms[0],
    )
    seg_calls = []
    update_done = threading.Event()
    loop.segmentation = SimpleNamespace(
        failed=False,
        prepare=lambda jobs, imgs, r: seg_calls.append(len(jobs)) or {},
    )
    loop.appearance = fake_pipeline(lambda *args, **kwargs: update_done.set())
    worker = loop._appearance_worker = AppearanceWorker(loop, seg_max_interval_ms=200, defer_interval_s=0.01)
    meta, images = source.item
    camera_rig = CameraRig.from_provider(SnapshotCalibration(meta["calibration"]))
    try:
        worker.offer(None, meta["world"], meta, images, camera_rig, {})
        wait_until(lambda: worker.deferred >= 1)
        assert not seg_calls and not update_done.is_set()
        assert worker.deferred_reasons[mode] >= 1
        # 추가 offer 없이 게이트·감속만 풀리면 미뤄 둔 작업이 그대로 실행된다(작업을 버리지 않음).
        gate_open[0] = True
        interval_ms[0] = 200
        assert update_done.wait(1)
        assert len(seg_calls) == 1 and seg_calls[0] >= 1
    finally:
        loop.close()


def test_worker_rechecks_gate_and_throttle_immediately_before_seg_prepare(rig):
    """큐 진입 때는 열려 있었더라도 seg.prepare 직전에 게이트가 닫히거나 감속되면 분할을 미룬다(-808)."""
    from simai_ai.geometry.rig import CameraRig
    from simai_ai.hmr.appearance_worker import AppearanceWorker
    from simai_ai.hmr.stream import SnapshotCalibration

    _, source, _, _, _, loop, _ = harness(rig, freeze=1)
    loop.step()
    interval_ms = [200]
    loop.gate = SimpleNamespace(
        check=lambda: SimpleNamespace(admit=True),
        recommended_interval_ms=lambda: interval_ms[0],
    )
    seg_calls = []
    update_done = threading.Event()
    track_calls = []

    def track_hook(*args, **kwargs):
        track_calls.append(1)
        if len(track_calls) == 1:
            # _run() 진입 게이트를 통과한 뒤 seg.prepare 직전에 Tier 0 감속이 걸린 상황
            interval_ms[0] = 800

    loop.segmentation = SimpleNamespace(
        failed=False,
        prepare=lambda jobs, imgs, r: seg_calls.append(len(jobs)) or {},
    )
    loop.appearance = SimpleNamespace(
        retain=lambda *_: None,
        track=track_hook,
        register=lambda *_: None,
        update=lambda *args, **kwargs: update_done.set(),
        config=SimpleNamespace(face_policy="EXCLUDED"),
    )
    worker = loop._appearance_worker = AppearanceWorker(loop, seg_max_interval_ms=200, defer_interval_s=0.01)
    meta, images = source.item
    camera_rig = CameraRig.from_provider(SnapshotCalibration(meta["calibration"]))
    try:
        worker.offer(None, meta["world"], meta, images, camera_rig, {})
        wait_until(lambda: worker.deferred_reasons["throttled"] >= 1)
        assert not seg_calls and not update_done.is_set()
        interval_ms[0] = 200
        assert update_done.wait(1)
        assert len(seg_calls) == 1
    finally:
        loop.close()


def test_worker_discards_deferred_item_after_identity_change_and_closes_promptly(rig):
    """미뤄 둔 작업의 신원이 바뀌면 폐기하고, 대기 중 close() 해도 지연 없이 종료된다(-808)."""
    from simai_ai.geometry.rig import CameraRig
    from simai_ai.hmr.appearance_worker import AppearanceWorker
    from simai_ai.hmr.stream import SnapshotCalibration

    _, source, _, _, _, loop, _ = harness(rig, freeze=1)
    loop.step()
    gate_open = [False]
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=gate_open[0]))
    seg_calls = []
    loop.segmentation = SimpleNamespace(
        failed=False,
        prepare=lambda jobs, imgs, r: seg_calls.append(len(jobs)) or {},
    )
    loop.appearance = fake_pipeline(lambda *args, **kwargs: seg_calls.append("update"))
    worker = loop._appearance_worker = AppearanceWorker(loop, defer_interval_s=5.0)
    meta, images = source.item
    camera_rig = CameraRig.from_provider(SnapshotCalibration(meta["calibration"]))
    try:
        worker.offer(None, meta["world"], meta, images, camera_rig, {})
        wait_until(lambda: worker.deferred >= 1)
        # 신원 변경 뒤 close()가 5초 대기를 끝까지 기다리지 않고 즉시 끝난다.
        loop.people["w-1"].revision += 1
        started = time.perf_counter()
        loop.close()
        assert time.perf_counter() - started < 1.0
        assert not seg_calls
    finally:
        loop.close()
