"""프로세스 분리 Re-ID(S15P31A101-744): 크롭·임베딩은 검출 프로세스, 특징 사본·Re-ID 결정(보조 비용·DORMANT 재입장·swap 복원·
분할 판정)은 판정 프로세스의 World Track 그대로.

(a) 분리 켬에서 Re-ID 가 enabled·active 로 뜨고, 검출 프로세스가 만든 특징이 판정 쪽 사본에 들어와 lookup 된다. 자식이 다시 뜨면 사본을
    비우고 새 세대(-g1) 특징만 받는다. 상태는 단일 경로와 같은 키({"enabled", "state", "reason", ...})로 싣는다.
(b) file 재생 결정론: 임베딩을 묶음 안에서 끝내므로(단일·분리 모두) 같은 녹화는 Re-ID 켬에서도 분리 켬/끔 같은 world_state 를 낸다.
    초당 크롭 상한은 벽시계라 이 시험은 상한을 넉넉히 둔다(설계 3절 허용 차이).
(c) DORMANT 재입장·swap 복원: 특징이 경계(pickle)를 넘어 판정 쪽 사본에서 lookup 돼도 단일 경로 시험과 같은 결정을 낸다.
결과는 SIM 상한이다(제품 성능 아님, AGENTS 규칙 7).
"""

from __future__ import annotations

import dataclasses
import functools
import importlib.util
import json
import pickle
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("cv2")

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for p in (AI_DIR, HERE, AI_DIR / "tests" / "io"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import split_fakes  # noqa: E402
from split_fakes import scripted_spec  # noqa: E402
from test_tier0_file_replay import scene  # noqa: E402,F401 - scene 은 fixture

from services.tier0.detect_process import DetectorProcess, RemoteLiveInput, SplitDetectorFeed  # noqa: E402
from services.tier0.split_side_work import ChildSideWork, RemoteReidCollector, SideWorkSpec  # noqa: E402
from simai_ai.reid.collector import ReidCollectorConfig  # noqa: E402

_wtr_spec = importlib.util.spec_from_file_location("_wtr_split", AI_DIR / "tests" / "tracking" / "test_world_track_reid.py")
wtr = importlib.util.module_from_spec(_wtr_spec)
_wtr_spec.loader.exec_module(wtr)
wt = wtr.wt


def _wait(cond, timeout: float = 30.0, every: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(every)
    return bool(cond())


@pytest.fixture(scope="module")
def rig():
    return wt.CameraRig.from_provider(wt._DictCalibrationProvider(wt._calibration_doc()))


# --------------------------------------------------------------------------- (a) 분리 켬 Re-ID·자식 재시작


def test_split_reid_active_features_reach_judge_and_restart_clears_copy() -> None:
    config = ReidCollectorConfig(max_crops_per_second=1000, update_interval_ms=0, stable_update_interval_ms=0,
                                 device="cpu")
    spec = dataclasses.replace(scripted_spec(), input_factory=split_fakes.ScriptedLargeLiveInput,
                               side_work=SideWorkSpec(reid=config, reid_embedder_factory=split_fakes.ColorEmbedder))
    process = DetectorProcess(spec, now_ms=lambda: time.time_ns() // 1_000_000, mono_s=time.monotonic,
                              backoff_s=0.1, backoff_max_s=0.2)
    process.start()
    remote = RemoteReidCollector(process, config)
    feed = SplitDetectorFeed(process, connected=process.connected, source="LIVE", async_mode=True)
    feed.reid_collector = remote
    live = RemoteLiveInput(process)
    try:
        assert remote.active  # 기동 때 알린 상태(하트비트 전)로도 켜져 보인다: Perception 이 lookup 을 고르는 시점
        live.start()
        assert _wait(lambda: remote.cache.lookup("cam_A", "cam_A-t1") is not None, timeout=30)
        feature = remote.lookup("cam_A", "cam_A-t1")
        assert feature.camera_id == "cam_A" and feature.embedding.shape == (64,)
        status = remote.status()
        assert status["enabled"] is True and status["state"] == "active" and status["reason"] is None
        assert _wait(lambda: (remote.status().get("embedded_crops") or 0) > 0, timeout=10)  # 검출 프로세스 보고값
        assert feed.reid_submission_status() is not None
        # 수집 주기 정책은 판정 쪽에서 계산해 명령으로 보낸다(CONFIRMED·HIGH 한 사람 → 그 로컬 키가 stable)
        track = SimpleNamespace(lifecycle="CONFIRMED", identity_flags=(), identity_confidence="HIGH", t_last_valid=5,
                                world_id="w1", position_xy=(0.0, 0.0), local_keys=(("cam_A", "cam_A-t1"),))
        remote.update_sampling(SimpleNamespace(t=5, tracks=[track]))
        assert remote.sampling_sent == 1 and remote.people == 1

        process._gen.process.kill()
        assert _wait(lambda: process.status()["generation"] == 1, timeout=60)
        assert remote.resets == 1  # 새 세대 전에 판정 쪽 사본을 비웠다
        assert remote.cache.lookup("cam_A", "cam_A-t1") is None
        assert _wait(lambda: remote.cache.lookup("cam_A", "cam_A-t1-g1") is not None, timeout=30)
        assert remote.cache.lookup("cam_A", "cam_A-t1") is None  # 옛 세대 키가 다시 생기지 않는다
        assert process._waiting == set()
    finally:
        process.stop()


def test_child_side_reid_start_failure_reports_disabled_not_fatal() -> None:
    def broken():
        raise OSError("COPE 가중치 없음")

    side = ChildSideWork(SideWorkSpec(reid=ReidCollectorConfig(device="cpu"), reid_embedder_factory=broken), ("cam_A",),
                         clock=SimpleNamespace(now_ms=lambda: 0), put=lambda _m: True, put_blocking=lambda _m: None)
    try:
        assert side.reid is None  # 검출 프로세스는 뜨고 Re-ID 만 꺼진다(기하 추적 그대로)
        assert side.status()["reid"] == {"enabled": True, "state": "disabled", "reason": "start_failed"}
    finally:
        side.close()


# --------------------------------------------------------------------------- (b) file 재생 결정론


def test_file_replay_with_reid_matches_single_vs_split(tmp_path, scene, monkeypatch) -> None:  # noqa: F811
    from test_live_input import OneTrackBackend
    from test_tier0_file_replay import CHANNEL_ENV, _config, _lines
    from test_tier0_live_e2e import SimTruthDetector

    from services.tier0.file_input import WORLD_STATE_FILE
    from services.tier0.service import Tier0Service
    from services.tier0.settings import load_settings

    run, frames, session = scene
    # 합성 영상(256×144)은 SIM bbox 해상도보다 작아 크롭이 잘린다. 크롭 최소 크기·가시성 하한만 풀어 같은 경로를 태운다
    # (분리 경로는 이 설정을 검출 프로세스로 넘긴다)
    original_config = Tier0Service._reid_config
    monkeypatch.setattr(Tier0Service, "_reid_config", lambda self: dataclasses.replace(
        original_config(self), min_bbox_width_px=1, min_bbox_height_px=1, min_visibility_ratio=0.0, device="cpu"))

    def replay(tmp: Path, split: bool):
        cfg = _config(tmp, run, session)
        doc = json.loads(cfg.read_text(encoding="utf-8"))
        # 벽시계 초당 상한·주기가 최대 속도 재생 결과에 들어가지 않게 넉넉히(허용 차이 근거: 설계 3절)
        doc["reid"] = {"max_crops_per_second": 10_000, "update_interval_ms": 0, "stable_update_interval_ms": 0}
        doc["clean_background"] = {"enabled": False}
        cfg.write_text(json.dumps(doc), encoding="utf-8")
        env = {**CHANNEL_ENV, "SIMAI_TIER0_PROCESS_SPLIT": "1" if split else "0"}
        n = iter(range(10**9))
        service = Tier0Service(load_settings(cfg, env=env), detector_factory=functools.partial(SimTruthDetector, frames, 0),
                               tracker_backend_factory=OneTrackBackend, reid_embedder_factory=split_fakes.ColorEmbedder,
                               new_msg_id=lambda: f"00000000-0000-4000-8000-{next(n):012d}")
        service.start()
        try:
            assert service.finished.wait(120), service.status()["file_input"]
            status = service.status()
        finally:
            assert service.stop(timeout_s=30) is True
        return [(m["msg_id"], m["payload"]) for m in _lines(tmp / "out" / WORLD_STATE_FILE)], status

    one, one_status = replay(tmp_path / "single", split=False)
    two, two_status = replay(tmp_path / "split", split=True)
    assert one_status["reid"]["state"] == "active" and two_status["reid"]["state"] == "active"
    assert (one_status["reid"]["embedded_crops"] or 0) > 0
    assert two_status["reid"]["embedded_crops"] == one_status["reid"]["embedded_crops"]  # 같은 크롭을 같은 수만큼
    assert two_status["reid"]["features_received"] == two_status["reid"]["embedded_crops"]
    assert two == one  # Re-ID 켬에서도 클러스터·World ID·msg_id 가 같다
    assert two_status["process_split"]["disabled"] == []


# --------------------------------------------------------------------------- (c) DORMANT 재입장·swap 복원(분리 켬)


class _Proc:
    """DetectorProcess 대역: 곁가지 명령을 같은 프로세스의 ChildSideWork 에 넘기고, 검출 쪽이 보낸 특징을 pickle 로 경계를 넘겨
    판정 쪽 사본에 넣는다(실제 IPC 와 같은 직렬화)."""

    spec = SimpleNamespace(mode="file")

    def __init__(self) -> None:
        self.handlers: dict = {}
        self.hooks: list = []
        self.side: ChildSideWork | None = None
        self.built_side = {"reid": {"enabled": True, "state": "active", "reason": None}}

    def on_side(self, kind, fn) -> None:
        self.handlers[kind] = fn

    def on_new_generation(self, hook) -> None:
        self.hooks.append(hook)

    def send_side(self, cmd, *, required=False) -> bool:
        if self.side is not None:
            self.side.handle(0, *cmd)
        return True

    def snapshot(self) -> dict:
        return {}

    def put(self, item) -> bool:
        kind, name, payload = pickle.loads(pickle.dumps(item))
        assert kind == "side"
        self.handlers[name](payload)
        return True


def test_dormant_reentry_through_split_collector_keeps_world_ids(rig) -> None:
    """test_demand_collector_preserves_dormant_reentry_ids 를 분리 경로(검출 쪽 수집기 → 경계 → 판정 쪽 사본)로."""
    from simai_ai.tracking.local_tracker import Observation2DFrame, PoseDetection, TrackedDetection
    from simai_ai.tracking.world_track import WorldIdIssuer, WorldTrackConfig, WorldTrackManager

    class Model:
        def embed(self, crops):
            return np.stack([wtr._ortho_basis(int(crop[0, 0, 0])) for crop in crops])

    proc = _Proc()
    config = ReidCollectorConfig(device="cpu")
    remote = RemoteReidCollector(proc, config)
    side = ChildSideWork(SideWorkSpec(reid=config), wt.ALL[:3], clock=SimpleNamespace(now_ms=lambda: 0),
                         put=proc.put, put_blocking=lambda _m: None)
    side.reid.close()  # 대역 임베더로 다시 만든다(작업 스레드 없이 한 묶음씩)
    from simai_ai.reid.collector import AsyncReidCollector

    mono = [0.0]
    child_cache = type(side.reid.cache)(feature_ttl_ms=config.feature_ttl_ms)
    side.reid = AsyncReidCollector(config, embedder=Model(), start_worker=False, monotonic=lambda: mono[0],
                                   cache=child_cache)
    proc.side = side
    manager = WorldTrackManager(WorldIdIssuer("sampling"), WorldTrackConfig(reid_enabled=True),
                                feature_lookup=remote.lookup)
    cams = wt.ALL[:3]
    people = 2
    locations = [(1.5 + i * 1.8, 3.0) for i in range(people)]

    def collect(t, ids):
        for i, local in enumerate(ids):
            for cam in cams:
                image = np.full((240, 160, 3), i + 1, np.uint8)
                kps = tuple((50.0, 15.0 + j * 10, 0.95) for j in range(17))
                obs = Observation2DFrame(cam, 1, t, t, "SIM", 0.0, (
                    TrackedDetection(local[cam], PoseDetection((10.0, 10.0, 100.0, 200.0), 0.95, kps)),))
                side.reid.submit_observation(SimpleNamespace(image=image), obs)
            side.reid.process_pending_once()
            for feature in list(child_cache._latest.values()):  # 검출 쪽 캐시에 들어간 특징을 경계로 보낸다
                proc.put(("side", "reid_feature", feature))
            mono[0] += 1.0

    def clusters(t, ids):
        return [wtr._cluster(rig, str(i), t, x, y, keys) for i, ((x, y), keys) in enumerate(zip(locations, ids))]

    try:
        initial = [{c: f"{c}-p{i}" for c in cams} for i in range(people)]
        collect(wt.T, initial)
        for k in range(5):
            t = wt.T + k * wt.DT
            frame = manager.update(wt._frame(rig, t, clusters(t, initial)), rig)
            remote.update_sampling(frame)  # 판정 쪽 계산 → 명령 → 검출 쪽 set_sampling
        original = {tr.world_id for tr in frame.tracks}
        assert len(original) == people and side.reid._sampling[0] == frame.t
        manager.update(wt._frame(rig, wt.T + 15_000, []), rig)
        absent = manager.update(wt._frame(rig, wt.T + 16_000, []), rig)
        remote.update_sampling(absent)
        assert not absent.tracks
        mono[0] = 300.0
        t = wt.T + 300_000
        returning = [{c: f"{c}-return{i}" for c in cams} for i in range(people)]
        collect(t, returning)
        frame = manager.update(wt._frame(rig, t, clusters(t, returning)), rig)
        assert {tr.world_id for tr in frame.tracks} == original  # DORMANT 재입장: 기존 World ID 승계
        assert set(frame.reassociations) == original
        assert remote.features_received > 0
    finally:
        side.close()


class _RemoteCache:
    """시험의 LocalTrackFeatureCache 자리: put 한 특징을 pickle 로 경계를 넘겨 판정 쪽 사본(RemoteReidCollector)에 넣는다."""

    def __init__(self, *args, **kwargs) -> None:
        self.remote = RemoteReidCollector(_Proc(), ReidCollectorConfig(device="cpu"))

    def put(self, feature, *, keep_timeline: bool = False) -> None:
        self.remote._receive(pickle.loads(pickle.dumps(feature)))

    def lookup(self, camera_id, local_track_id, now_ms=None, **_kw):
        return self.remote.lookup(camera_id, local_track_id, now_ms)


def test_swap_restoration_with_split_feature_copy(rig, monkeypatch) -> None:
    monkeypatch.setattr(wtr, "LocalTrackFeatureCache", _RemoteCache)
    wtr.test_close_crossing_swap_reverted_by_gallery(rig)


def test_split_adjudication_with_split_feature_copy(rig, monkeypatch) -> None:
    monkeypatch.setattr(wtr, "LocalTrackFeatureCache", _RemoteCache)
    wtr.test_split_adjudication_suppresses_same_person_remainder(rig)
    wtr.test_split_adjudication_preserves_different_person_near_owner_under_count_protection(rig)


# --------------------------------------------------------------------------- 재시작 경계 순서(배경 누적 상태 명령)


def test_clean_background_restart_replay_and_activate_keep_queue_order() -> None:
    """재시작 재전송(받기 스레드)이 옛 문서를 읽은 채 넣기 전에 보정 활성화(판정 스레드)가 끼어도, 새 세대가 최종으로 쓰는 문서는
    최신 문서다(옛 문서의 bg_init 이 새 문서의 bg_activate 뒤에 들어가지 않는다)."""
    import threading

    from services.tier0.split_side_work import RemoteCleanBackground

    queue_: list[tuple] = []
    entered = threading.Event()
    slow_init = {"on": False}

    class Proc:
        def on_new_generation(self, hook) -> None:
            self.hook = hook

        def send_side(self, cmd, *, required=False) -> bool:
            if cmd[0] == "bg_init" and slow_init["on"]:
                entered.set()
                time.sleep(0.3)  # 큐에 넣기 직전에 판정 스레드가 끼어들 틈
            queue_.append(cmd)
            return True

    proc = Proc()
    old = {"calibration_version": "cal-1", "cameras": []}
    new = {"calibration_version": "cal-2", "cameras": []}
    background = RemoteCleanBackground(proc, SimpleNamespace(), Path("."), old, {"source": "LIVE"})
    background.configure_rotation_samples(960)
    queue_.clear()  # 새 세대의 큐(옛 세대 명령은 버려졌다)
    slow_init["on"] = True
    replay = threading.Thread(target=proc.hook)
    replay.start()
    assert entered.wait(5)
    activate = threading.Thread(target=background.activate, args=(new, {"source": "LIVE"}))
    activate.start()
    replay.join(5)
    activate.join(5)

    # 검출 프로세스 쪽 적용 규칙: bg_init 은 없으면 만들고 있으면 활성화, bg_activate 는 만든 뒤에만
    applied = None
    for cmd in queue_:
        if cmd[0] == "bg_init" or (cmd[0] == "bg_activate" and applied is not None):
            applied = cmd[1]["calibration_version"]
    assert applied == "cal-2", queue_
    assert [c[0] for c in queue_] == ["bg_init", "bg_rotation_side", "bg_activate"]
