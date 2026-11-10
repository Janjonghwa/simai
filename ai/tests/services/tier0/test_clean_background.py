"""-363: 합성 프레임으로 큐 격리·보정 무효화·원자적 내보내기를 검증한다."""

import copy
import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import candidate, recording  # noqa: E402, F401

# 사람 관절 보정 감시(drift_watch)는 -444 이후 기본 꺼짐이라, 감시와 연결된 이 시험들은 켠 site 를 쓴다.
from test_drift_watch import drive, field_calibration, site  # noqa: E402, F401
from test_tier0_service import _settings  # noqa: E402

from services.tier0.clean_background import BackgroundCleanBackground  # noqa: E402
from services.tier0.perception import DetectorFeed  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import (  # noqa: E402
    CleanBackgroundSettings,
    Tier0ConfigError,
    load_settings,
)
from simai_ai.providers.base import Frame  # noqa: E402
from simai_ai.tracking.local_tracker import (  # noqa: E402
    CameraDetections,
    PoseDetection,
)

IDS = ("cam_A", "cam_B", "cam_C", "cam_D")
PROV = {"frames": {"source": "FILE", "dataset_id": "synthetic-clean-background"}, "is_upper_bound": True}


@pytest.fixture(autouse=True)
def _close_clean_bg_workers(monkeypatch):
    created = []
    original = BackgroundCleanBackground.__init__

    def init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(BackgroundCleanBackground, "__init__", init)
    yield
    for item in created:
        item.close()


def document():
    return {"calibration_version": "cal-1", "cameras": [
        {"camera_id": cid, "R": [[1, 0, 0], [0, 1, 0], [0, 0, 1]], "t": [0, 0, 0]}
        for cid in IDS]}


def worker(tmp_path, doc=None, **kwargs):
    settings = replace(CleanBackgroundSettings(), sample_interval_ms=100, min_samples_per_camera=3,
                       max_samples_per_camera=4, max_bytes=100_000, **kwargs)
    return BackgroundCleanBackground(doc or document(), PROV, settings=settings, state_dir=tmp_path,
                                     stop=threading.Event())


def pair(cid, at, *, person=False):
    image = np.full((20, 30, 3), 90, dtype=np.uint8)
    detections = ()
    if person:
        x = (at // 100 % 3) * 10
        image[5:10, x:x + 4] = 220
        detections = (PoseDetection((x, 5, x + 4, 10), .9, (None,) * 17),)
    frame = Frame(cid, at, "FILE", {"stream_epoch": 1, "frame_sequence": at}, image=image)
    return frame, CameraDetections(cid, 1, at, at, "FILE", None, detections)


def collect(w, camera_ids=IDS, start=0):
    for at in range(start, start + 300, 100):
        for cid in camera_ids:
            assert w.offer(*pair(cid, at, person=True))
            assert w.process_one()


def read_status(w):
    return json.loads((w.root / "status.json").read_text(encoding="utf-8"))


def test_interval_queue_budget_and_busy_lock_never_copy_in_caller(tmp_path, monkeypatch):
    w = worker(tmp_path, queue_depth=1, max_pending_bytes=1800)
    monkeypatch.setattr(w.accumulator, "add_frame", lambda *args: pytest.fail("caller copied a frame"))
    assert w.offer(*pair("cam_A", 0))
    assert not w.offer(*pair("cam_A", 99))
    assert not w.offer(*pair("cam_B", 100))
    assert len(w._queue) == 1 and w._pending_bytes == 1800 and w.dropped == 1
    with w._queue_lock:
        assert not w.offer(*pair("cam_C", 100))
    assert w.dropped == 2
    too_small = worker(tmp_path, max_pending_bytes=1799)
    assert not too_small.offer(*pair("cam_A", 0))
    assert too_small.dropped == 1


def test_camera_snapshots_and_atomic_complete_bundle(tmp_path, monkeypatch):
    w = worker(tmp_path)
    publish = w._publish
    seen = []

    def inspect(staging, target, ctx):
        assert not target.exists()
        assert (staging / "clean_backgrounds.json").exists()
        if (staging / "COMPLETE").exists():
            assert len(list(staging.glob("*.jpg"))) == 4
        seen.append(target)
        return publish(staging, target, ctx)

    monkeypatch.setattr(w, "_publish", inspect)
    collect(w, ("cam_A",))
    assert len(seen) == 1 and not list(w.root.rglob("COMPLETE"))
    collect(w, IDS[1:])
    status = read_status(w)
    assert status["complete"] and all(c["sample_count"] == 3 for c in status["cameras"].values())
    assert all(c["never_revealed_ratio"] == 0 for c in status["cameras"].values())
    bundle = Path(status["bundle"])
    meta = json.loads((bundle / "clean_backgrounds.json").read_text(encoding="utf-8"))
    assert meta["input_provenance"] == PROV
    assert meta["complete"] and meta["calibration_version"] == "cal-1"
    import cv2
    for cid in IDS:
        assert (bundle / meta["cameras"][cid]["image"]).exists()
        assert np.max(np.abs(cv2.imread(str(bundle / f"{cid}.jpg")).astype(int) - 90)) <= 1
    assert len(seen) == 5 and len(list(w.root.rglob("COMPLETE"))) == 1
    assert not list(w.root.rglob(".pending-*"))
    assert w.offer(*pair("cam_A", 1000))  # 재생성에 최신 표본을 쓴다.


def test_interval_boundary_and_persistent_occlusion(tmp_path):
    w = worker(tmp_path)
    frame, _ = pair("cam_A", 0)
    box = PoseDetection((0, 0, 30, 20), .9, (None,) * 17)
    for at in (0, 100, 200, 300):
        f = replace(frame, estimated_capture_time=at)
        det = CameraDetections("cam_A", 1, at, at, "FILE", None, (box,))
        assert w.offer(f, det)
        w.process_one()
        assert not w.offer(*pair("cam_A", at + 99))
    status = read_status(w)
    assert status["cameras"]["cam_A"]["sample_count"] == 4
    assert status["cameras"]["cam_A"]["never_revealed_ratio"] == 1
    assert not status["cameras"]["cam_A"]["ready"] and not status["complete"]
    assert not list(w.root.rglob("*.jpg"))


def test_reset_discards_old_queued_frame_and_keeps_other_cameras(tmp_path):
    w = worker(tmp_path)
    for cid in IDS:
        assert w.offer(*pair(cid, 0))
        w.process_one()
    assert w.offer(*pair("cam_B", 100))
    w.reset_camera("cam_B")
    w.process_one()
    status = read_status(w)["cameras"]
    assert status["cam_B"]["sample_count"] == 0
    assert all(status[cid]["sample_count"] == 1 for cid in IDS if cid != "cam_B")
    assert w.offer(*pair("cam_B", 100))
    w.process_one()
    assert read_status(w)["cameras"]["cam_B"]["sample_count"] == 1


def test_version_change_resets_only_changed_camera_and_never_overwrites(tmp_path):
    w = worker(tmp_path)
    collect(w)
    first = w._complete
    before = (first / "clean_backgrounds.json").read_bytes()
    new = copy.deepcopy(document())
    new["calibration_version"] = "cal-2"
    new["cameras"][1]["R"][0][0] = .9
    w.activate(new, PROV)
    w.process_one()
    status = read_status(w)
    assert status["calibration_version"] == "cal-2" and not status["complete"]
    assert status["cameras"]["cam_B"]["sample_count"] == 0
    assert all(status["cameras"][cid]["sample_count"] == 3 for cid in IDS if cid != "cam_B")
    collect(w, ("cam_B",), start=300)
    assert read_status(w)["complete"] and w._complete != first
    assert (first / "clean_backgrounds.json").read_bytes() == before
    new["calibration_version"] = "cal-3"
    w.activate(copy.deepcopy(new), PROV)
    w.process_one()
    assert read_status(w)["complete"]  # 자세 불변 버전은 전부 유지


def test_change_during_export_never_publishes_stale_bundle(tmp_path, monkeypatch):
    w = worker(tmp_path)
    original = w.accumulator.export

    def export(*args, **kwargs):
        result = original(*args, **kwargs)
        w.reset_camera("cam_A")
        return result

    monkeypatch.setattr(w.accumulator, "export", export)
    collect(w, ("cam_A",))
    assert not list(w.root.rglob("*.jpg"))
    assert not list(w.root.rglob("COMPLETE"))
    w.process_one()
    assert read_status(w)["cameras"]["cam_A"]["sample_count"] == 0


def test_same_version_reset_rebuilds_bundle_with_current_generations(tmp_path):
    w = worker(tmp_path)
    collect(w)
    first = w._complete
    w.reset_camera("cam_B")
    w.process_one()
    assert not read_status(w)["complete"]
    collect(w, ("cam_B",), start=300)
    meta = json.loads((w._complete / "clean_backgrounds.json").read_text(encoding="utf-8"))
    assert meta["camera_generations"] == {cid: int(cid == "cam_B") for cid in IDS}
    assert w._complete != first and (first / "COMPLETE").exists()


def test_write_failure_retries_existing_samples_and_clears_error(tmp_path, monkeypatch):
    w = worker(tmp_path)
    collect(w, IDS[:3])
    for at in (0, 100):
        assert w.offer(*pair("cam_D", at))
        w.process_one()
    original = w.accumulator.export
    failed, completed = threading.Event(), threading.Event()

    def export(*args, **kwargs):
        if not failed.is_set():
            failed.set()
            raise OSError("synthetic disk failure")
        return original(*args, **kwargs)

    publish = w._publish

    def signal(staging, target, ctx):
        is_bundle = (staging / "COMPLETE").exists()
        result = publish(staging, target, ctx)
        if result and is_bundle:
            completed.set()
        return result

    monkeypatch.setattr(w.accumulator, "export", export)
    monkeypatch.setattr(w, "_publish", signal)
    assert w.offer(*pair("cam_D", 200))
    thread = threading.Thread(target=w.run)
    thread.start()
    try:
        assert failed.wait(2) and completed.wait(4)
    finally:
        w.stop.set()
        thread.join(3)
    assert not thread.is_alive()
    assert read_status(w)["complete"] and read_status(w)["last_error"] is None
    assert not list(w.root.rglob(".pending-*"))


def test_service_drift_and_calibration_switch_are_connected(site):
    service, doc = field_calibration(site)
    w = worker(site.settings.state_dir, doc)
    service.clean_background = w
    ids = tuple(service.rig.cameras)
    for cid in ids:
        assert w.offer(*pair(cid, 0))
        w.process_one()
    drive(site, service, doc)
    w.process_one()
    assert w.accumulator.camera_status(ids[1]).sample_count == 0
    assert all(w.accumulator.camera_status(cid).sample_count == 1 for cid in ids if cid != ids[1])
    new, report = candidate(service, "cal-clean-next")
    assert service.propose_calibration(new, report)
    site.tick()
    w.process_one()
    assert read_status(w)["calibration_version"] == "cal-clean-next"
    assert w.accumulator.camera_status(ids[0]).sample_count == 0
    assert w.accumulator.camera_status(ids[2]).sample_count == 1


class EmptyTracker:
    def update_batch(self, _batch):
        return []


def make_feed(tickets, camera_ids=("cam1", "cam2", "cam3", "cam4")):
    return DetectorFeed(camera_ids=camera_ids, tickets=lambda: list(tickets),
                        detector=SimpleNamespace(detect=lambda frames: [pair(f.camera_id, f.estimated_capture_time)[1]
                                                                       for f in frames]),
                        tracker=EmptyTracker(), source="FILE", provenance=PROV["frames"])


def test_detector_feed_passes_only_matching_fresh_results(tmp_path):
    w = worker(tmp_path)
    tickets = [pair("cam_A", 100)[0]]
    feed = make_feed(tickets, IDS)
    seen = []
    feed.observe_decoded = lambda *args: seen.append(args)
    feed.poll(100)
    assert len(seen) == 1 and seen[0][0] is tickets[0]
    feed.poll(100)  # 중복/역순 결과는 전달하지 않는다.
    assert len(seen) == 1
    tickets[:] = [pair("cam_A", 200)[0]]
    feed._detector.detect = lambda _frames: [pair("cam_B", 200)[1]]
    feed.poll(200)
    assert len(seen) == 1
    def failure(_frames):
        raise RuntimeError("synthetic detector error")
    feed._detector.detect = failure
    feed.poll(200)
    assert len(seen) == 1


def test_service_ticks_continue_while_accumulator_is_blocked(tmp_path, recording, monkeypatch):
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    settings = replace(_settings(tmp_path, recording, anchor_ms=1000),
                       clean_background=replace(CleanBackgroundSettings(), sample_interval_ms=1, queue_depth=1))
    tickets, now = [], [1000]
    feed = make_feed(tickets)
    service = Tier0Service(settings, feed=feed, wall_ms=lambda: now[0], mono_ns=lambda: now[0] * 1_000_000)
    entered, release = threading.Event(), threading.Event()
    service.start()
    try:
        w = service.clean_background
        assert w is not None and feed.observe_decoded is not None
        original = w.accumulator.add_frame

        def blocked(*args):
            entered.set()
            assert release.wait(5)
            return original(*args)

        monkeypatch.setattr(w.accumulator, "add_frame", blocked)
        tickets[:] = [pair("cam1", now[0])[0]]
        service.judge.tick()
        assert entered.wait(2)
        started = time.perf_counter()
        for _ in range(12):
            now[0] += 100
            tickets[:] = [pair("cam1", now[0])[0]]
            service.judge.tick()
        elapsed = time.perf_counter() - started
        assert elapsed < 1 and w.dropped >= 11
        print(f"clean_background blocked_worker_ticks=12 elapsed_ms={elapsed * 1000:.3f} drops={w.dropped}")
    finally:
        release.set()
        assert service.stop()


def test_disabled_service_has_no_collector_or_callback(tmp_path, recording, monkeypatch):
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    settings = replace(_settings(tmp_path, recording, anchor_ms=1000),
                       clean_background=CleanBackgroundSettings(enabled=False))
    feed = make_feed([])
    service = Tier0Service(settings, feed=feed)
    service.start()
    try:
        assert service.clean_background is None and feed.observe_decoded is None
        assert not any(t.name == "tier0-clean-background" for t in service._threads)
        assert not (settings.state_dir / "clean-background").exists()
    finally:
        assert service.stop()


@pytest.mark.parametrize("value", [{"enabled": "false"}, {"offload": "true"}, {"sample_interval_ms": 0},
                                  {"queue_depth": True}, {"max_bytes": -1}, {"max_pending_bytes": 0},
                                  {"min_samples_per_camera": 61}, []])
def test_invalid_settings_rejected(tmp_path, recording, value):
    _settings(tmp_path, recording, anchor_ms=1000)
    path = tmp_path / "tier0.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["clean_background"] = value
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(Tier0ConfigError):
        load_settings(path, env={})


def test_settings_default_and_relative_output_path(tmp_path, recording):
    settings = _settings(tmp_path, recording, anchor_ms=1000)
    assert settings.clean_background.enabled and settings.clean_background.offload is True
    assert settings.clean_background.sample_interval_ms == 5000
    path = tmp_path / "tier0.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["clean_background"] = {"output_dir": "background-output", "max_bytes": 10000, "offload": False}
    path.write_text(json.dumps(doc), encoding="utf-8")
    settings = load_settings(path, env={})
    assert settings.clean_background.output_dir == tmp_path / "background-output"
    assert settings.clean_background.max_bytes == 10000
    assert settings.clean_background.offload is False


def test_regenerate_exports_new_bundle_and_not_ready(tmp_path):
    w = worker(tmp_path)
    w.control.mkdir(parents=True)
    request = w.control / "regenerate.request.json"
    response = w.control / "regenerate.response.json"
    request.write_text(json.dumps({"request_id": 1, "requested_at_ms": 1}), encoding="utf-8")
    w.process_one()
    assert json.loads(response.read_text(encoding="utf-8"))["status"] == "NOT_READY"
    collect(w)
    original = w._complete
    collect(w, start=400)
    request.write_text(json.dumps({"request_id": 2, "requested_at_ms": 2}), encoding="utf-8")
    w.process_one()
    result = json.loads(response.read_text(encoding="utf-8"))
    assert result["status"] == "READY" and result["request_id"] == 2
    assert Path(result["bundle_path"]) != original
    assert (Path(result["bundle_path"]) / "COMPLETE").is_file()
    assert original.is_dir() and not request.exists()


def test_rolling_update_reference_fallback_and_reset(tmp_path):
    import cv2

    w = worker(tmp_path)
    collect(w)
    reference_dir, reference_meta = w._reference["cam_A"]
    reference_image = (reference_dir / reference_meta["cameras"]["cam_A"]["image"]).read_bytes()
    assert w._regenerate_bundle(w._active)
    meta = json.loads((w._complete / "clean_backgrounds.json").read_text(encoding="utf-8"))
    assert all(record["background_source"] == "REFERENCE_FALLBACK" for record in meta["cameras"].values())
    for at in (300, 400, 500, 600):
        frame, detections = pair("cam_A", at)
        frame.image[:] = 150
        assert w.offer(frame, detections)
        w.process_one()
    assert w._regenerate_bundle(w._active)
    meta = json.loads((w._complete / "clean_backgrounds.json").read_text(encoding="utf-8"))
    assert meta["cameras"]["cam_A"]["background_source"] == "RECENT"
    assert meta["cameras"]["cam_B"]["background_source"] == "REFERENCE_FALLBACK"
    assert cv2.imread(str(w._complete / "cam_A.jpg"))[0, 0, 0] == 150
    assert (reference_dir / "cam_A.jpg").read_bytes() == reference_image
    w.reset_camera("cam_A")
    w.process_one()
    assert w.accumulator.camera_status("cam_A").sample_count == 0
    assert (reference_dir / "cam_A.jpg").read_bytes() == reference_image
    assert not w._regenerate_bundle(w._active)  # 이전 자세의 기준은 현재 보정에 쓸 수 없다.


def test_reveal_count_and_temporal_variance_exclude_pixels(tmp_path):
    import cv2

    w = worker(tmp_path)
    for i in range(4):
        at = i * 100
        frame, _ = pair("cam_A", at)
        frame.image[0:5, 15:20] = 255 if i % 2 else 0
        # 첫 표본만 드러난 영역은 오래 머문 가림으로 취급한다.
        boxes = () if i == 0 else (PoseDetection((0, 0, 4, 4), .9, (None,) * 17),)
        detections = CameraDetections("cam_A", 1, at, at, "FILE", None, boxes)
        assert w.offer(frame, detections)
        w.process_one()
    out = tmp_path / "masked"
    exported = w.accumulator.export(out, calibration_version="cal-1", camera_ids=["cam_A"])
    w._mask_unknown(out, "cam_A", exported.metadata)
    mask = cv2.imread(str(out / "cam_A.valid.png"), cv2.IMREAD_GRAYSCALE)
    assert mask[1, 1] == 0 and mask[2, 17] == 0 and mask[15, 25] == 255


def test_recent_window_expires_old_samples(tmp_path):
    w = worker(tmp_path)
    collect(w, ("cam_A",))
    assert w.offer(*pair("cam_A", 1000))
    w.process_one()
    assert w.accumulator.camera_status("cam_A").sample_count == 1


def test_measure_background_worker_cpu_time(tmp_path):
    """합성 영상 처리 CPU 시간을 출력한다. 현장 성능 합격 기준은 아니다."""
    settings = replace(CleanBackgroundSettings(), sample_interval_ms=1, min_samples_per_camera=3,
                       max_samples_per_camera=4, max_bytes=32_000_000)
    w = BackgroundCleanBackground(document(), PROV, settings=settings, state_dir=tmp_path,
                                  stop=threading.Event())
    started = time.process_time()
    for at in range(4):
        for cid in IDS:
            frame, det = pair(cid, at)
            frame = replace(frame, image=np.full((360, 640, 3), 90, np.uint8))
            assert w.offer(frame, det)
            w.process_one()
    w._regenerate_bundle(w._active)
    print(f"clean_background worker_cpu_seconds={time.process_time() - started:.3f} "
          "cameras=4 frames_per_camera=4 resolution=640x360")


def test_offload_runs_in_spawn_child_and_matches_inprocess_export(tmp_path):
    """-563: offload(spawn 자식 + shared_memory) 경로와 인프로세스(offload=False) 경로의
    export 산출물·status.json·rotation_snapshot·is_ready 결과가 완전히 일치한다."""
    import os

    w_inproc = worker(tmp_path / "inproc", offload=False)
    w_offload = worker(tmp_path / "offload", offload=True)
    w_inproc.configure_rotation_samples(16)
    w_offload.configure_rotation_samples(16)

    assert w_offload._mp_ctx.get_start_method() == "spawn"
    assert not w_inproc.is_ready() and not w_offload.is_ready()

    for idx, at in enumerate((0, 100, 200, 300)):
        for cid in IDS:
            f_in, d_in = pair(cid, at, person=True)
            f_off, d_off = pair(cid, at, person=True)
            # 일부 픽셀에 시간 분산 변화를 넣어 _mask_unknown 경로도 함께 검증한다.
            f_in.image[0:2, 25:28] = 250 if idx % 2 else 10
            f_off.image[0:2, 25:28] = 250 if idx % 2 else 10
            assert w_inproc.offer(f_in, d_in)
            assert w_inproc.process_one()
            assert w_offload.offer(f_off, d_off)
            assert w_offload.process_one()

    assert w_offload.worker_pid is not None and w_offload.worker_pid != os.getpid()
    assert len(w_offload.allocated_shm_names) >= 1
    assert w_inproc.is_ready() and w_offload.is_ready()

    st_in = read_status(w_inproc)
    st_off = read_status(w_offload)
    assert st_in["complete"] and st_off["complete"]
    assert st_in["calibration_version"] == st_off["calibration_version"] == "cal-1"
    assert st_in["camera_generations"] == st_off["camera_generations"]
    assert st_in["queue_dropped"] == st_off["queue_dropped"] == 0

    bundle_in = Path(st_in["bundle"])
    bundle_off = Path(st_off["bundle"])
    assert json.loads((bundle_in / "COMPLETE").read_text(encoding="utf-8")) == json.loads(
        (bundle_off / "COMPLETE").read_text(encoding="utf-8")
    )
    meta_in = json.loads((bundle_in / "clean_backgrounds.json").read_text(encoding="utf-8"))
    meta_off = json.loads((bundle_off / "clean_backgrounds.json").read_text(encoding="utf-8"))
    assert meta_in == meta_off

    for cid in IDS:
        assert (bundle_in / f"{cid}.jpg").read_bytes() == (bundle_off / f"{cid}.jpg").read_bytes()
        assert (bundle_in / f"{cid}.valid.png").read_bytes() == (bundle_off / f"{cid}.valid.png").read_bytes()
        for key in (
            "sample_count",
            "time_range_ms",
            "never_revealed_ratio",
            "mean_person_cover_ratio",
            "image_size",
            "bytes_used",
            "ready",
            "exported",
        ):
            assert st_in["cameras"][cid][key] == st_off["cameras"][cid][key]

    snap_in = w_inproc.rotation_snapshot()
    snap_off = w_offload.rotation_snapshot()
    assert snap_in is not None and snap_off is not None
    for cid in IDS:
        assert snap_in[cid]["signature"] == snap_off[cid]["signature"]
        assert snap_in[cid]["size"] == snap_off[cid]["size"]
        assert snap_in[cid]["sample_times"] == snap_off[cid]["sample_times"]
        for key in ("mean_person_cover_ratio", "never_revealed_ratio"):  # -564 혼잡 보류 근거
            assert snap_off[cid][key] is not None
            assert snap_in[cid][key] == snap_off[cid][key]
        assert len(snap_in[cid]["samples"]) == len(snap_off[cid]["samples"])
        for (img_a, m_a), (img_b, m_b) in zip(snap_in[cid]["samples"], snap_off[cid]["samples"]):
            np.testing.assert_array_equal(img_a, img_b)
            np.testing.assert_array_equal(m_a, m_b)


def test_offload_nonblocking_put_and_full_slot_drop_count(tmp_path):
    """-563: 큐나 공유 메모리 슬롯이 가득 차면 기다리지 않고 버리며 queue_dropped 를 올린다."""
    w = worker(tmp_path, offload=True, queue_depth=2)
    assert w.offer(*pair("cam_A", 0))
    assert w.process_one()
    # 모든 공유 메모리 슬롯이 사용 중인 상태를 모사하면 _worker_add_frame 이 즉시 버리고 dropped 를 올린다.
    for slot in w._shm_slots:
        slot.in_use = True
    try:
        started = time.perf_counter()
        assert not w._worker_add_frame("cam_A", 100, pair("cam_A", 100)[0].image, ())
        assert time.perf_counter() - started < 0.05
        assert w.dropped == 1
    finally:
        for slot in w._shm_slots:
            slot.in_use = False

    # 판정 스레드 offer() 경로도 queue_depth 초과 시 블로킹 없이 즉시 버리고 queue_dropped 에 반영한다.
    started = time.perf_counter()
    assert w.offer(*pair("cam_B", 200))
    assert w.offer(*pair("cam_C", 200))
    for i in range(10):
        assert not w.offer(*pair("cam_D", 200 + i * 100))
    assert time.perf_counter() - started < 0.05
    assert w.dropped == 11
    while w.process_one():
        pass
    assert read_status(w)["queue_dropped"] == 11


def test_offload_child_crash_restarts_once_then_disables_and_unlinks_shm(tmp_path, caplog):
    """-563: 자식 프로세스가 죽으면 로그를 남기고 1회 재시작하며, 다시 죽으면 배경 누적을 끄고 공유 메모리를 해제한다."""
    import os
    from multiprocessing.shared_memory import SharedMemory

    w = worker(tmp_path, offload=True)
    assert w.offer(*pair("cam_A", 0))
    assert w.process_one()
    pid1 = w.worker_pid
    assert pid1 is not None and pid1 != os.getpid()
    assert w.worker_restarts == 0

    # 1차 비정상 종료 -> 경고 로그 + 1회 재시작 후 정상 누적·내보내기
    proc1 = w.worker_process
    assert proc1 is not None
    w._cmd_queue.put_nowait(("crash_for_test", 0, 1))
    proc1.join(timeout=3)
    assert not proc1.is_alive()

    collect(w, start=200)
    pid2 = w.worker_pid
    assert w.worker_restarts == 1
    assert not w.disabled
    assert pid2 is not None and pid2 != pid1
    assert read_status(w)["complete"]
    assert "1회 재시작" in caplog.text

    # 2차 비정상 종료 -> 에러 로그 + 배경 누적 비활성화(disabled=True) + 공유 메모리 unlink
    proc2 = w.worker_process
    assert proc2 is not None
    w._cmd_queue.put_nowait(("crash_for_test", 0, 1))
    proc2.join(timeout=3)
    assert not proc2.is_alive()

    assert w.offer(*pair("cam_A", 1000))
    assert not w.process_one()
    assert w.disabled is True
    assert w.worker_restarts == 1
    assert w.worker_process is None
    assert w.active_shm_names == set()
    assert "배경 누적을 끈다" in caplog.text
    assert read_status(w)["last_error"] is not None

    # 비활성화 뒤에도 판정 스레드 offer() 는 블로킹이나 예외 없이 즉시 False 를 반환한다.
    assert not w.offer(*pair("cam_A", 2000))
    assert not w.process_one()
    for name in w.allocated_shm_names:
        with pytest.raises(FileNotFoundError):
            SharedMemory(name=name, create=False)


def test_offload_close_terminates_worker_and_unlinks_shared_memory(tmp_path):
    """-563: 종료(close) 시 자식 프로세스를 끝내고 생성한 모든 shared_memory 를 unlink 한다."""
    from multiprocessing.shared_memory import SharedMemory

    w = worker(tmp_path, offload=True)
    collect(w, ("cam_A",))
    proc = w.worker_process
    shm_names = set(w.allocated_shm_names)
    assert proc is not None and proc.is_alive()
    assert len(shm_names) >= 1 and w.active_shm_names == shm_names

    w.close()
    assert not proc.is_alive()
    assert w.worker_process is None
    assert w.active_shm_names == set()
    for name in shm_names:
        with pytest.raises(FileNotFoundError):
            SharedMemory(name=name, create=False)


def test_offload_falls_back_to_in_process_when_shm_is_small(tmp_path, monkeypatch):
    """-563: /dev/shm 이 작으면(도커 기본 64 MB 등) 공유 메모리에 쓰다 SIGBUS 로 죽지 않게 인프로세스로 돈다."""
    import services.tier0.clean_background as cb

    monkeypatch.setattr(cb, "_shm_free_bytes", lambda: 8 * 1024 * 1024)
    w = worker(tmp_path, offload=True)
    try:
        assert w.settings.offload is False
        assert w.offer(*pair("cam_A", 0))
        assert w.process_one()
        assert w.worker_process is None
        assert w.accumulator.camera_status("cam_A").sample_count == 1
    finally:
        w.close()
