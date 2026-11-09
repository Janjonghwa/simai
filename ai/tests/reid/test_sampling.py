"""-499: 실제 수집 경로에서 갱신 주기·상한·작업 스레드/락 경계를 확인한다."""

from dataclasses import replace
import threading
from types import SimpleNamespace

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from simai_ai.reid.collector import AsyncReidCollector, ReidCollectorConfig
from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection


class Model:
    def embed(self, crops):
        return np.tile(np.array([1, 0], dtype=np.float32), (len(crops), 1))


def observation(t, keys=("a",)):
    bbox = (10., 10., 100., 200.)
    kps = tuple((50., 15. + i * 10, .95) for i in range(17))
    return Observation2DFrame("cam", 1, t, t, "SIM", 0., tuple(
        TrackedDetection(key, PoseDetection(bbox, .95, kps)) for key in keys))


def world(t, *, keys=("a",), lifecycle="CONFIRMED", flags=(), confidence="HIGH", near=False):
    tr = SimpleNamespace(world_id="w", lifecycle=lifecycle, identity_flags=flags,
                         identity_confidence=confidence, t_last_valid=t, position_xy=(1., 1.),
                         local_keys=tuple(("cam", k) for k in keys))
    others = [SimpleNamespace(**{**vars(tr), "world_id": "other", "position_xy": (1.5, 1.)})] if near else []
    return SimpleNamespace(t=t, tracks=[tr, *others])


def test_stable_confirmed_track_uses_three_seconds_but_new_key_is_immediate():
    col = AsyncReidCollector(embedder=Model(), start_worker=False)
    frame = SimpleNamespace(image=np.zeros((240, 160, 3), np.uint8))
    try:
        assert col.submit_observation(frame, observation(1000)) == 1
        assert col.process_pending_once() == 1
        # 같은 키가 확정돼도 처음 임베딩이 없으면 빠른 수집을 유지한다.
        col.update_sampling(world(1800))
        assert col.submit_observation(frame, observation(1800)) == 0
        col.update_sampling(world(3999))
        assert col.submit_observation(frame, observation(3999)) == 0
        col.update_sampling(world(4000))
        assert col.submit_observation(frame, observation(4000)) == 1
        assert col.submit_observation(frame, observation(4000, ("reentry",))) == 1
    finally:
        col.close()


@pytest.mark.parametrize("kw", [{"lifecycle": "TENTATIVE"}, {"near": True},
                                {"flags": ("TWO_VIEW_COMPETITOR_JOINT",)},
                                {"confidence": "IDENTITY_UNCERTAIN"}])
def test_urgent_cases_keep_fast_update_interval(kw):
    col = AsyncReidCollector(embedder=Model(), start_worker=False)
    frame = SimpleNamespace(image=np.zeros((240, 160, 3), np.uint8))
    try:
        col.submit_observation(frame, observation(1000))
        col.process_pending_once()
        col.update_sampling(world(1800, **kw))
        assert col.submit_observation(frame, observation(1800)) == 1
    finally:
        col.close()


def test_rate_cap_is_global_and_discards_oldest_and_recovers_next_second():
    mono = [0.]
    col = AsyncReidCollector(ReidCollectorConfig(batch_size=3, max_crops_per_second=4),
                             embedder=Model(), start_worker=False, monotonic=lambda: mono[0])
    frame = SimpleNamespace(image=np.zeros((240, 160, 3), np.uint8))
    try:
        col.submit_observation(frame, observation(1000, tuple(str(i) for i in range(6))))
        assert col.process_pending_once() == 3
        assert col.process_pending_once() == 1
        assert col.lookup("cam", "0", 1000) is None  # 오래된 둘은 추론 전에 버림
        assert col.lookup("cam", "5", 1000) is not None
        assert col.status()["dropped_rate_limit"] == 2
        col.submit_observation(frame, replace(observation(1100, ("fresh",)), camera_id="other"))
        assert col.process_pending_once() == 0
        mono[0] = 1.
        assert col.submit_observation(frame, observation(1200, ("fresh",))) == 1
        assert col.process_pending_once() == 1
        assert col.embedded_crops == 5
    finally:
        col.close()


def test_crop_runs_in_worker_and_lookup_status_submit_do_not_wait_for_it(monkeypatch):
    import simai_ai.reid.collector as module

    entered, release = threading.Event(), threading.Event()
    original = module.cv2.cvtColor
    crop_threads = []

    def blocked(*args):
        crop_threads.append(threading.current_thread().name)
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(module.cv2, "cvtColor", blocked)
    col = AsyncReidCollector(embedder=Model())
    image = np.zeros((240, 160, 3), np.uint8)
    frame = SimpleNamespace(image=image)
    done = threading.Event()

    def probe():
        assert col.lookup("cam", "a", 1000) is None
        col.status()
        col.submit_observation(frame, observation(1800, ("b",)))
        done.set()

    try:
        col.submit_observation(frame, observation(1000))
        assert entered.wait(5)
        # producer 참조를 버려도 큐/작업이 프레임을 보존한다.
        frame.image = None
        worker = threading.Thread(target=probe)
        worker.start()
        assert done.wait(1), "crop 동안 수집기 lock을 잡으면 상태/다음 제출이 막힌다"
        assert crop_threads == ["tier0-reid"]
    finally:
        release.set()
        assert col.flush(5)
        col.close()
        if 'worker' in locals():
            worker.join(5)
    assert col.lookup("cam", "a", 1000) is not None
