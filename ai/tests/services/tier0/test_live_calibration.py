"""-347: 관절 공급 → 후보 → 검증 → tick 전환. 합성 지표는 제품 성능이 아니다."""

import json
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")
pytest.importorskip("scipy")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2], HERE.parents[1] / "calibration"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from test_calibration_swap import candidate, recording, site as calibration_site  # noqa: E402, F401
import test_calibration_swap  # noqa: E402
from test_drift_watch import drive, field_calibration  # noqa: E402
from test_live_candidate import synthetic_rig_and_frames, _perturb_camera_rotation  # noqa: E402, F401
from test_tier0_service import _settings  # noqa: E402
from services.tier0.service import Tier0Service  # noqa: E402
from services.tier0.settings import LiveCalibrationSettings, Tier0ConfigError, load_settings  # noqa: E402
from simai_ai.calibration.live_candidate import LiveCandidateConfig  # noqa: E402
from simai_ai.calibration.document import content_hash  # noqa: E402


@pytest.fixture
def site(tmp_path, recording, monkeypatch):
    original = test_calibration_swap._settings
    monkeypatch.setattr(test_calibration_swap, "_settings",
                        lambda *args, **kwargs: replace(original(*args, **kwargs), human_calibration_enabled=True))
    yield from calibration_site.__wrapped__(tmp_path, recording, monkeypatch)


def test_common_reprojection_matches_time_camera_local_track_and_joint(monkeypatch, synthetic_rig_and_frames):
    from simai_ai.calibration import live_candidate as lc
    from simai_ai.calibration.extrinsics.session_tracks import reprojection_metrics
    truth, _, _, observations = synthetic_rig_and_frames
    buffer = lc.LiveJointBuffer()
    for frame in observations:
        buffer.append_frame(frame)
    tracks = buffer.to_session_tracks()
    rig = lc.session_rig(truth)
    frames = lc.associate_session(tracks, rig)
    errors = {}
    reprojection_metrics(frames, rig, observation_errors=errors)
    assert errors
    # 두 번째 연관에서 시각 하나, cam_A 로컬 트랙, cam_B 관절 0이 달라진다.
    changed = []
    for frame in frames:
        clusters = []
        for cluster in frame.clusters:
            views = []
            for view in cluster.views:
                if view.camera_id == "cam_A":
                    view = replace(view, local_track_id=view.local_track_id + "-other")
                elif view.camera_id == "cam_B":
                    keypoints = list(view.view.keypoints)
                    keypoints[0] = None
                    view = replace(view, view=replace(view.view, keypoints=tuple(keypoints)))
                views.append(view)
            clusters.append(replace(cluster, views=tuple(views)))
        changed.append(replace(frame, t=frame.t + 1 if frame is frames[0] else frame.t,
                               clusters=tuple(clusters)))
    after_errors = {}
    reprojection_metrics(changed, rig, observation_errors=after_errors)
    replies = iter((frames, changed))
    monkeypatch.setattr(lc, "associate_session", lambda *args: next(replies))
    result = lc.common_reprojection_metrics(tracks, truth, truth)
    common = errors.keys() & after_errors.keys()
    assert result["method"] == "common_observations"
    assert result["cameras"]["cam_A"]["observations"] == 0
    assert result["cameras"]["cam_A"]["before_median_px"] is None
    assert not any(key[0] == frames[0].t or (key[1] == "cam_B" and key[3] == 0) for key in common)
    for cid, stats in result["cameras"].items():
        keys = [key for key in common if key[1] == cid]
        assert stats["observations"] == len(keys)
        if keys:
            assert stats["before_median_px"] == np.median([errors[key] for key in keys])
            assert stats["after_median_px"] == np.median([after_errors[key] for key in keys])


class FrameFeed:
    source = "SIM"

    def __init__(self, frames, provenance):
        self.frames = list(frames)
        self.camera_ids = tuple(sorted({f.camera_id for f in frames}))
        self._provenance = provenance["frames"]

    def poll(self, now):
        ready = [f for f in self.frames if f.estimated_capture_time_ms <= now]
        self.frames = [f for f in self.frames if f.estimated_capture_time_ms > now]
        return ready

    def provenance(self):
        return self._provenance

    def camera_connected(self, _cid):
        return True

    def close(self):
        pass


@pytest.mark.parametrize("with_heights", [True, False])
def test_synthetic_perception_candidate_switch_and_next_world_version(tmp_path, recording, monkeypatch,
                                                                      synthetic_rig_and_frames, with_heights):
    truth, heights, provenance, frames = synthetic_rig_and_frames
    # -352: 빠른 길은 상대 통과율이 최상 카메라의 80% 미만인 카메라만 시도한다.
    # 1도는 D/max=0.926이므로, 명확한 틀어짐(5도, D/max=0.273)으로 전환 경로를 검증한다.
    initial = _perturb_camera_rotation(truth, "cam_D", 5.)
    # 실제 직전 보정처럼 녹화 도구의 반올림한 기본 렌즈와 약간 달라도 값을 보존해야 한다.
    for camera in initial["cameras"]:
        camera["K"][0][0] += .015
        camera["K"][1][1] += .015
    initial["content_hash"] = content_hash(initial)
    cal_file = tmp_path / "calibration.json"
    cal_file.write_text(json.dumps(initial), encoding="utf-8")
    now = [frames[0].estimated_capture_time_ms]
    settings = replace(_settings(tmp_path, recording, anchor_ms=now[0], bootstrap=False),
                       calibration_file=cal_file, human_calibration_enabled=True, drift_watch_enabled=False,
                       live_calibration=LiveCalibrationSettings(measured_camera_heights=heights if with_heights else {}))
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    # 실행만 시험 스레드에서 돌린다. 후보 계산·검증·전환 경로는 실제 구현을 쓴다.
    from services.tier0.live_calibration import BackgroundLiveCalibration
    monkeypatch.setattr(BackgroundLiveCalibration, "run", lambda self: None)
    service = Tier0Service(settings, feed=FrameFeed(frames, provenance),
                           wall_ms=lambda: now[0], mono_ns=lambda: (now[0] - frames[0].estimated_capture_time_ms) * 1_000_000)
    service.start()
    try:
        live = service.live_calibration
        live.runner._config = LiveCandidateConfig(use_subprocess=False, rounds=2)
        for f in frames[::4]:
            now[0] = f.estimated_capture_time_ms
            service.judge.tick()
        assert live.buffer.summary()["total_frames"] == len(frames)
        assert live.buffer.summary()["total_samples"] == 2 * len(frames)
        result = live.run_once()
        assert result.proposed, result.report.get("failure_reason")
        assert service._pending_calibration is None
        assert live.confirmation.count == 1
        # 새 관측을 15초 뒤 추가하고 같은 활성 보정 대비 개선을 한 번 더 확인한다.
        extra = [replace(f, frame_sequence=f.frame_sequence + 60,
                         estimated_capture_time_ms=f.estimated_capture_time_ms + 18_000) for f in frames[:200]]
        service.perception.feed.frames.extend(extra)
        now[0] += 15_000
        service.judge.tick()
        result = live.run_once()
        assert result.proposed, result.report.get("failure_reason")
        assert result.calibration["scale_basis"] == initial["scale_basis"]
        assert result.report["world_alignment"]["fixed_center_rotation_cameras"] == ["cam_D"]
        assert all(a["K"] == b["K"] and a["distortion"] == b["distortion"]
                   for a, b in zip(initial["cameras"], result.calibration["cameras"], strict=True))
        assert service.calibration_status.get("pending_version") == result.calibration["calibration_version"], (service.calibration_status, result.report["holdout"])
        assert service.rig.calibration_version == initial["calibration_version"]
        assert live.runner.current_calibration()["calibration_version"] == initial["calibration_version"]
        next_frames = [replace(f, frame_sequence=110, estimated_capture_time_ms=now[0] + 300) for f in frames[-4:]]
        service.perception.feed.frames.extend(next_frames)
        now[0] += 300
        tick = service.judge.tick()
        version = result.calibration["calibration_version"]
        assert tick.perceived.payload["calibration_version"] == version
        assert tick.perceived.frame.calibration_version == version
        assert live.runner.current_calibration()["calibration_version"] == version
        assert live.document is service._drift_document
        assert live.provenance["calibration"]["calibration_version"] == version
        assert service.calibration.transitions[-1]["payload"]["reason"] == "RECALIBRATED"
    finally:
        assert service.stop()


def _crash_worker(*_args):
    raise SystemExit(17)


@pytest.mark.parametrize("failure", ["exception", "timeout", "death"])
def test_worker_failures_do_not_block_judgment(site, monkeypatch, failure):
    service = site.service
    live = service.live_calibration
    site.tick()
    entered = threading.Event()
    original = live.runner._run_in_subprocess

    def run(*args):
        entered.set()
        if failure == "exception":
            raise RuntimeError("worker unavailable")
        return original(*args)

    monkeypatch.setattr(live.runner, "_run_in_subprocess", run)
    if failure == "death":
        import simai_ai.calibration.live_candidate as module
        monkeypatch.setattr(module, "_subprocess_worker_entry", _crash_worker)
        live.runner._config = replace(live.runner.config, timeout_s=10.)
    else:
        live.runner._config = replace(live.runner.config, timeout_s=.25)
        live.runner._simulate_sleep_s = 10.
    thread = threading.Thread(target=live.run_once)
    thread.start()
    assert entered.wait(3)
    before = service.judge.stats.perceived
    start = time.perf_counter()
    for _ in range(12):
        site.tick()
    elapsed_ms = (time.perf_counter() - start) * 1000
    thread.join(15)
    assert not thread.is_alive()
    assert service.judge.stats.perceived > before
    assert elapsed_ms < 3000
    assert live.last_status == ("TIMEOUT" if failure == "timeout" else "FAILED")
    assert live.last_error
    assert service.status()["calibration"]["live_candidate"]["last_status"] == live.last_status
    print(f"candidate_failure={failure} ticks=12 elapsed_ms={elapsed_ms:.3f}")


def test_drifted_camera_only_is_reset_and_recollects(site):
    service, doc = field_calibration(site)
    frames = []
    original = service.perception.observe_frame
    service.perception.observe_frame = lambda f: (frames.append(f), original(f))
    for _ in range(12):
        site.tick()
    live = service.live_calibration
    before = live.buffer.summary()["camera_frame_counts"]
    drive(site, service, doc)
    drifted = list(service.rig.cameras)[1]
    after = live.buffer.summary()["camera_frame_counts"]
    assert drifted not in after
    assert after == {cid: count for cid, count in before.items() if cid != drifted}
    assert live.blocked_reason is None
    frame = next(f for f in reversed(frames) if f.camera_id == drifted)
    assert live.buffer.append_frame(replace(frame, frame_sequence=frame.frame_sequence + 1,
                                          estimated_capture_time_ms=service.clock.now_ms()))
    assert live.buffer.summary()["camera_frame_counts"][drifted] == 1


def test_buffer_offer_latency_and_snapshot_content(site):
    service = site.service
    samples = []
    original = service.perception.observe_frame

    def offer(frame):
        start = time.perf_counter_ns()
        original(frame)
        samples.append((time.perf_counter_ns() - start) / 1e6)

    service.perception.observe_frame = offer
    tick_times = []
    for _ in range(30):
        start = time.perf_counter_ns()
        site.tick()
        tick_times.append((time.perf_counter_ns() - start) / 1e6)
    assert len(samples) >= 100
    p95 = float(np.percentile(samples, 95))
    assert p95 < 5
    assert service.live_calibration.buffer.to_session_tracks() is not None
    print(f"candidate_offer_n={len(samples)} p95_ms={p95:.6f} tick_p95_ms={np.percentile(tick_times, 95):.3f}")


def test_ambiguous_drift_blocks_candidate_without_reset(site):
    service, doc = field_calibration(site)
    for _ in range(12):
        site.tick()
    live = service.live_calibration
    before = live.buffer.summary()["camera_frame_counts"]
    drive(site, service, doc, cameras=3)
    assert service._calibration_state() == "DRIFTED"
    assert live.buffer.summary()["camera_frame_counts"] == before
    assert "귀속 불확실" in live.blocked_reason
    assert live.run_once().status == "DEFERRED"


def test_stream_change_blocks_candidates_and_old_lens_recollection(site):
    from simai_ai.calibration.drift import CameraObservation
    from test_drift_watch import association
    service, doc = field_calibration(site)
    live = service.live_calibration
    cid = next(iter(service.rig.cameras))
    service.drift_watch._measure(doc, association(service, service.clock.now_ms(), empty=True),
                                 (CameraObservation(cid, image_size=(1, 1)),))
    assert "내부 파라미터" in live.blocked_reason
    drive(site, service, doc)
    assert "내부 파라미터" in live.blocked_reason
    assert live.run_once().status == "DEFERRED"


def test_reset_rejects_inflight_candidate_and_discards_queued_candidate(site, caplog):
    service, doc = field_calibration(site)
    live = service.live_calibration
    generation = live.generation
    proposal, report = candidate(service, "cal-auto-waiting")
    report.update(improvement_decision={"accepted": True}, validation_end_ms=service.clock.now_ms())
    assert not service._propose_live_calibration(proposal, report, generation)
    report["validation_end_ms"] += 1
    assert service._propose_live_calibration(proposal, report, generation)
    assert service._pending_calibration is not None
    drive(site, service, doc)
    assert live.generation != generation
    assert service._pending_calibration is None
    assert not service._propose_live_calibration(proposal, report, generation)
    assert "재수집/활성 버전 변경" in caplog.text


def test_shutdown_cancels_candidate_process(site, monkeypatch):
    import multiprocessing
    service = site.service
    site.tick()
    live = service.live_calibration
    live.runner._simulate_sleep_s = 30.
    live.runner._config = replace(live.runner.config, timeout_s=60.)
    entered = threading.Event()
    original = live.runner._run_in_subprocess

    def run(*args):
        entered.set()
        return original(*args)

    monkeypatch.setattr(live.runner, "_run_in_subprocess", run)
    previous = {p.pid for p in multiprocessing.active_children()}
    thread = threading.Thread(target=live.run_once)
    thread.start()
    assert entered.wait(3)
    assert service.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert live.last_status == "DEFERRED"
    assert {p.pid for p in multiprocessing.active_children()} <= previous


def test_setting_disables_collection_and_worker(tmp_path, recording, monkeypatch):
    monkeypatch.setattr(Tier0Service, "_run_judge", lambda self: None)
    monkeypatch.setattr(Tier0Service, "_run_housekeeping", lambda self: None)
    settings = _settings(tmp_path, recording, anchor_ms=1760342400000)
    config = tmp_path / "tier0.json"
    doc = json.loads(config.read_text(encoding="utf-8"))
    doc["live_calibration"] = {"enabled": False}
    config.write_text(json.dumps(doc), encoding="utf-8")
    service = Tier0Service(load_settings(config, env={"SIMAI_TIER0_HUMAN_CALIBRATION": "1"}))
    service.start()
    try:
        assert service.live_calibration is None
        assert service.perception.observe_frame is None
        assert not any(t.name == "tier0-live-calibration" for t in service._threads)
    finally:
        assert service.stop()
    for value in ({"enabled": "false"}, {"timeout_s": float("nan")}, {"interval_ms": 0},
                  {"buffer_duration_ms": 1}, {"measured_camera_heights": {"cam_A": -1}},
                  {"safety_enabled": "false"}):
        doc["live_calibration"] = value
        config.write_text(json.dumps(doc), encoding="utf-8")
        with pytest.raises(Tier0ConfigError):
            load_settings(config, env={})
