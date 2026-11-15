"""S15P31A101-746: 회전 복구가 CPU 예산 안에서 15초 주기마다 끝나고(카메라 단위 이월·새 표본만 받기), 쓰러짐 감지를 유지한다.

18차(2304x1296 4대, 표본 5 s)에서 옛 작업자는 새 표본이 들어올 때마다 서명이 바뀌어, 예산을 넘긴 주기에 끝낸 카메라 특징까지
버리고 다음 주기에 4대를 처음부터 다시 뽑았다(이월 51회·완료 0). 계산 중 새 표본이 온 주기도 결과를 버렸다.
합성 자료의 CPU·동작 확인이며 제품 정확도 근거가 아니다(AGENTS 규칙 7).
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parents[2]):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import bench_rotation_recovery as bench  # noqa: E402

from services.tier0.clean_background import omit_rotation_samples  # noqa: E402
from services.tier0.settings import RotationRecoverySettings  # noqa: E402
from simai_ai.calibration.rotation_recovery import RotationConfig, sample_key  # noqa: E402
from simai_ai.calibration.rotation_worker import RotationComputation  # noqa: E402

CAMS = ("cam_A", "cam_B", "cam_C", "cam_D")


# --------------------------------------------------------------------------- 작업자: 카메라 단위 이월


def _fake_request(cycle: int, *, budget_ms: int = 500, generation: int = 0, max_age_ms: int = 30_000) -> dict:
    """매 주기 새 표본(서명 끝 시각 +15 s)이 들어오는 4대 요청. 영상 계산은 monkeypatch 로 대신한다."""
    cameras = [{"camera_id": c, "image_size": [4, 4], "K": [1]} for c in CAMS]
    end = 100_000 + cycle * 15_000
    return {
        "document": {"calibration_version": "v1", "cameras": cameras},
        "references": {c: {"key": ("ref", c), "camera": cam, "image": "i", "mask": "m"}
                       for c, cam in zip(CAMS, cameras)},
        "recent": {c: {"signature": (generation, end - 45_000, end), "size": (4, 4), "sample_times": [end],
                       "samples": [(np.zeros((4, 4), np.uint8), np.ones((4, 4), bool))]} for c in CAMS},
        "recent_ms": {c: end for c in CAMS}, "now_ms": end, "version": f"v{cycle}", "config": RotationConfig(),
        "budget_ms": budget_ms, "max_feature_age_ms": max_age_ms,
    }


@pytest.fixture
def costly_worker(monkeypatch):
    """기준·최근 특징 추출이 각각 0.3 s(작업자 CPU 시계)인 작업자. 판정은 STABLE 로 대신하며 받은 끝 시각을 남긴다."""
    clock = [0.0]
    decided: list[dict] = []

    def extract(*_args, **_kwargs):
        clock[0] += 0.3
        return SimpleNamespace(points=np.zeros((100, 2)))

    def candidate(_doc, _refs, features, recent, **_kw):
        decided.append(dict(recent))
        return None, {"status": "STABLE", "recent_samples_ms": dict(recent), "rotations": {}}

    monkeypatch.setattr(cv2, "imread", lambda *_a, **_k: np.zeros((4, 4), np.uint8))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.reference_features",
                        lambda *_a, **_k: (extract(), RotationConfig(max_image_side=320)))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.extract_features", extract)
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.synthesize_samples",
                        lambda samples: (samples[0][0], samples[0][1].astype(np.uint8) * 255))
    monkeypatch.setattr("simai_ai.calibration.rotation_worker.make_candidate", candidate)
    return RotationComputation(cpu_clock=lambda: clock[0]), decided


def test_carry_over_never_livelocks_when_new_samples_keep_arriving(costly_worker) -> None:
    """4대 x 0.3 s > 예산 0.5 s 인데 주기마다 새 표본이 온다. 옛 작업자는 영원히 이월했다. 지금은 2주기 안에 판정하고
    그 뒤로 매 주기 판정하며, 이어쓴 특징은 최대 나이(30 s) 안이고 카메라마다 2주기 안에 다시 뽑힌다."""
    computation, decided = costly_worker
    results = [computation.compute(_fake_request(cycle)) for cycle in range(8)]
    statuses = [r["status"] for r in results]
    assert statuses[:3] == ["BUDGET_DEFERRED", "BUDGET_DEFERRED", "BUDGET_DEFERRED"]  # 기준 4대 + 최근 4대 = 2.4 s
    assert statuses[3:] == ["COMPLETED"] * 5, statuses
    last_refresh = {}
    for cycle, result in enumerate(results):
        for cid in result["refreshed_cameras"]:
            last_refresh[cid] = cycle
        if result["status"] == "COMPLETED":
            assert all(cycle - last_refresh[c] <= 1 for c in CAMS), (cycle, last_refresh)
            assert set(result["carried_cameras"]) | set(result["refreshed_cameras"]) == set(CAMS)
            assert result["pending_cameras"] == []
    for recent in decided:
        end = max(recent.values())
        assert all(0 <= end - ms <= 30_000 for ms in recent.values())
    # 예산은 카메라 시작 전에만 본다: 넘긴 양은 한 카메라 작업(0.3 s) 이하다
    assert max(r["timings"]["total"] for r in results) <= 500 + 300 + 1e-6


def test_first_unit_always_progresses_even_when_budget_is_already_spent(costly_worker) -> None:
    """역직렬화만으로 예산을 넘긴 주기라도 카메라 하나는 진행한다(새 표본이 계속 와도 언젠가 끝난다)."""
    computation, _ = costly_worker
    first = computation.compute(_fake_request(0, budget_ms=1), started=-10.0)
    assert first["status"] == "BUDGET_DEFERRED"
    assert len(computation.references) == 1


def test_carried_features_expire_and_camera_reset_invalidates(costly_worker) -> None:
    computation, _ = costly_worker
    assert computation.compute(_fake_request(0, budget_ms=10_000))["status"] == "COMPLETED"
    # 예산이 1대분이면 1대만 새로 뽑고, 나머지는 표본 끝 시각 차이 15 s 라 이어써서 판정한다
    fresh = computation.compute(_fake_request(1, budget_ms=1))
    assert fresh["status"] == "COMPLETED" and len(fresh["refreshed_cameras"]) == 1
    assert len(fresh["carried_cameras"]) == 3
    # 최대 나이(30 s)를 넘은 특징은 이어쓰지 않는다: 판정하지 않고 다음 주기로 넘긴다
    stale = computation.compute(_fake_request(4, budget_ms=1))
    assert stale["status"] == "BUDGET_DEFERRED" and len(stale["refreshed_cameras"]) == 1
    assert len(stale["pending_cameras"]) == 3
    # 카메라 리셋(세대 변경) 전의 특징은 나이와 무관하게 쓰지 않는다
    assert computation.compute(_fake_request(4, budget_ms=10_000))["status"] == "COMPLETED"
    reset = computation.compute(_fake_request(5, budget_ms=1, generation=1))
    assert reset["status"] == "BUDGET_DEFERRED" and len(reset["pending_cameras"]) == 3


def test_worker_reuses_received_samples_and_reports_cache_miss() -> None:
    computation = RotationComputation()
    frame = (np.zeros((4, 4), np.uint8), np.ones((4, 4), bool))
    view = {"signature": (0, 1, 2), "size": (4, 4), "sample_times": [1, 2], "samples": [frame, frame]}
    assert computation._receive("a", view) == [frame, frame]
    omitted = omit_rotation_samples({"a": view}, {"a": {sample_key(0, 1, (4, 4))}})["a"]
    assert omitted["samples"] == [None, frame] and view["samples"] == [frame, frame]  # 원본 스냅샷은 그대로
    assert computation._receive("a", omitted) == [frame, frame]
    with pytest.raises(ValueError, match="SAMPLE_CACHE_MISS"):
        computation._receive("a", {**omitted, "signature": (1, 1, 2)})  # 다른 세대의 빈 자리


# --------------------------------------------------------------------------- 부모: 결과 유지·로그·상태


class _StubRecovery:
    """부모만 시험한다: 작업자 응답을 대본으로 준다."""

    @staticmethod
    def make(tmp: Path, results: list[dict], **settings):
        recovery, background, now = bench.make_rig(tmp, bench.synthetic_images(count=4), budget_ms=500, **settings)
        replies = iter(results)

        def compute(request):
            reply = dict(next(replies))
            reply.setdefault("timings", {"total": 600.0})
            reply.setdefault("cached_samples", {})
            hook = reply.pop("during", None)
            if hook:
                hook()
            return reply

        recovery._compute = compute
        return recovery, background, now


def _stable_report(background) -> dict:
    return {"status": "STABLE", "reason": "STABLE",
            "rotations": {c: {"status": "ROTATION", "angle_rad": 0.0002, "rotation_residual_p95_px": 0.4,
                              "matches": 300} for c in CAMS},
            "recent_samples_ms": {c: background.recent_signature(c)[2] for c in CAMS}, "validation_end_ms": 1}


def test_parent_keeps_result_when_new_samples_arrive_during_compute(tmp_path) -> None:
    recovery, background, now = _StubRecovery.make(tmp_path, [])
    bench.advance(background, now)
    report = _stable_report(background)

    def new_sample():
        now[0] += 1_000
        background.add_sample("cam_B", now[0])  # 같은 세대의 새 표본: 서명 끝 시각만 바뀐다

    recovery._compute = lambda request: {"status": "COMPLETED", "candidate": None, "report": report,
                                         "timings": {"total": 100.0}, "cached_samples": {}, "during": new_sample()}
    assert recovery.run_once()["status"] == "STABLE" and recovery.cycles == 1

    def reset():
        background.reset_camera("cam_C")  # 세대가 바뀌면 그 창의 결과는 버린다
        bench.advance(background, now)

    bench.advance(background, now)
    recovery._compute = lambda request: {"status": "COMPLETED", "candidate": None, "report": _stable_report(background),
                                         "timings": {"total": 100.0}, "cached_samples": {}, "during": reset()}
    assert recovery.run_once()["status"] == "WAITING_NEW_SAMPLES" and recovery.cycles == 1


def test_consecutive_deferral_warning_completion_log_and_status(tmp_path, caplog) -> None:
    deferred = {"status": "BUDGET_DEFERRED", "reason": "CPU 시간 예산 초과 (500ms), 다음 주기 카메라: cam_D",
                "timings_by_camera": {"cam_A": {"recent_extract": 300.0}}, "refreshed_cameras": ["cam_A"],
                "pending_cameras": ["cam_D"]}
    recovery, background, now = _StubRecovery.make(tmp_path, [deferred] * 4)
    with caplog.at_level(logging.INFO, logger="services.tier0.rotation_recovery"):
        for _ in range(4):
            bench.advance(background, now)
            assert recovery.run_once()["status"] == "BUDGET_DEFERRED"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "이월 4회 연속" in warnings[0].getMessage()
        assert "cam_A=300ms" in warnings[0].getMessage()
        assert recovery.status()["consecutive_deferrals"] == 4
        bench.advance(background, now)
        report = _stable_report(background)
        recovery._compute = lambda request: {
            "status": "COMPLETED", "candidate": None, "report": report, "timings": {"total": 420.0},
            "timings_by_camera": {c: {"recent_extract": 100.0} for c in CAMS}, "refreshed_cameras": list(CAMS),
            "carried_cameras": [], "pending_cameras": [], "cached_samples": {}}
        assert recovery.run_once()["status"] == "STABLE"
    done = [r.getMessage() for r in caplog.records if "회전 복구 완료" in r.getMessage()]
    assert len(done) == 1 and "이월 4회 뒤" in done[0] and "cam_A=ROTATION 0.01° p95 0.40px m300" in done[0]
    status = recovery.status()
    assert status["consecutive_deferrals"] == 0 and status["last_completed_ms"] == now[0]
    assert status["last_rotations"]["cam_D"]["angle_deg"] == pytest.approx(0.011, abs=1e-3)
    summary = recovery.timing_summary()
    assert summary["cpu_ms_by_camera"] == {c: 100.0 for c in CAMS} and summary["budget_deferrals"] == 4
    assert summary["rotations"]["cam_A"] == {"status": "ROTATION", "angle_deg": 0.011}


# --------------------------------------------------------------------------- 실제 작업자: 18차 크기 4대


def test_four_cameras_2304_complete_each_cycle_and_detect_fall(tmp_path) -> None:
    """2304x1296 4대·표본 5 s 를 실제 작업자 프로세스(기본 예산)로 돌린다: 매 주기 완료, 기준 특징은 한 번만, 받은 표본은 다시
    보내지 않는다. 그 뒤 cam_C 를 30° 돌리면(쓰러짐) 창이 새 자세로 찬 뒤 2주기 연속 확인으로 REQUIRES_SETUP 이 확정된다."""
    budget = RotationRecoverySettings().cpu_budget_ms
    recovery, background, now = bench.make_rig(tmp_path, bench.synthetic_images(), budget_ms=budget)
    try:
        rows = []
        for _ in range(4):
            bench.advance(background, now)
            report = recovery.run_once() or {}
            rows.append((report.get("status"), dict(recovery.last_cpu_ms), recovery.last_carried))
        assert [r[0] for r in rows[1:]] == ["STABLE"] * 3, rows
        assert all(r[1]["reference_extract"] == 0 and r[1]["reference_read"] == 0 for r in rows[1:]), rows
        assert recovery.feature_sides == {c: 320 for c in CAMS}
        assert recovery.samples_omitted > 0 and recovery.sample_resyncs == 0
        assert all(abs(row["angle_deg"]) < 0.5 for row in recovery.last_rotations.values())

        background.scene["cam_C"] = bench.roll(background.scene["cam_C"], 30.0)
        confirmed_after = None
        for cycle in range(1, 8):
            bench.advance(background, now)
            recovery.run_once()
            if "cam_C" in background.resets:
                confirmed_after = cycle
                break
        # 창 10장(50 s) 중 과반이 새 자세가 되기 전에는 NO_EVIDENCE 로 보류하고, 창이 차면 2주기 연속 확인(설계 -540)
        assert confirmed_after is not None and confirmed_after <= 6, recovery.status()
        assert recovery.last_status == "REQUIRES_SETUP" and "cam_C" in recovery.drifted_cameras
        assert recovery.consecutive_deferrals == 0
    finally:
        recovery.close()


def test_worker_restart_resends_full_samples(tmp_path) -> None:
    recovery, background, now = bench.make_rig(tmp_path, bench.synthetic_images(), budget_ms=5_000)
    try:
        for _ in range(2):
            bench.advance(background, now)
            assert recovery.run_once()["status"] == "STABLE"
        assert recovery._worker_samples
        recovery._process.kill()
        recovery._process.wait(5)
        bench.advance(background, now)
        sent_before = recovery.samples_sent
        assert recovery.run_once()["status"] == "STABLE"
        assert recovery.samples_sent - sent_before == 4 * bench.WINDOW  # 새 작업자에는 비운 자리 없이 다 보낸다
        # 같은 작업자의 캐시가 어긋나도(대역 계산기에서 비움) 한 번 다시 보내 이어간다
        computation = RotationComputation()
        recovery._compute = computation.compute
        bench.advance(background, now)
        assert recovery.run_once()["status"] in ("STABLE", "BUDGET_DEFERRED")
        assert recovery.sample_resyncs == 1
    finally:
        recovery.close()


# --------------------------------------------------------------------------- 분리 켬(-738): 판정 프로세스 회전 복구 + 검출 프로세스 표본 파일


class _LoopbackProcess:
    """DetectorProcess 대역: 곁가지 명령·요청을 같은 프로세스의 ChildSideWork 로 바로 넘긴다(경계 형식은 그대로)."""

    def __init__(self, side) -> None:
        self.side = side
        self.snap: dict = {}
        self.dump_cmds: list[tuple] = []

    def on_new_generation(self, _fn) -> None:
        return None

    def send_side(self, cmd: tuple, *, required: bool = False) -> bool:
        self.side.handle(0, cmd[0], *cmd[1:])
        return True

    def side_request(self, cmd: tuple, timeout_s: float):
        replies: list[tuple] = []
        self.side._put_blocking = replies.append
        self.dump_cmds.append(cmd)
        self.side.handle(0, cmd[0], 7, *cmd[1:])
        return tuple(replies[0][2:])

    def snapshot(self) -> dict:
        self.snap.update(self.side.status())
        return self.snap


def test_split_mode_rotation_reads_detect_process_file_with_only_new_samples(tmp_path) -> None:
    from services.tier0.rotation_recovery import BackgroundRotationRecovery
    from services.tier0.split_side_work import ChildSideWork, RemoteCleanBackground, SideWorkSpec

    with tempfile.TemporaryDirectory(dir=tmp_path) as root:
        local, background, now = bench.make_rig(Path(root), bench.synthetic_images(), budget_ms=5_000)
        local.close()
        side = ChildSideWork(SideWorkSpec(), CAMS, clock=None, put=lambda _item: True, put_blocking=lambda _i: None)
        side.clean_background = background  # 검출 프로세스의 배경 누적(표본·기준 배경)
        process = _LoopbackProcess(side)
        remote = RemoteCleanBackground(process, background.settings, tmp_path / "state", local.document, {})
        recovery = BackgroundRotationRecovery(
            local.document, {}, remote, settings=RotationRecoverySettings(cpu_budget_ms=5_000),
            now_ms=lambda: now[0], stop=local._stop, propose=lambda *_: False)
        try:
            for _ in range(3):
                bench.advance(background, now)
                assert recovery.run_once()["status"] == "STABLE"
            assert len(process.dump_cmds) == 3
            assert len(process.dump_cmds[0]) == 3  # 처음에는 다 쓴다
            omit = process.dump_cmds[-1][3]
            assert set(omit) == set(CAMS) and all(len(v) == bench.WINDOW for v in omit.values())  # 작업자가 가진 표본 키
            assert recovery.samples_omitted == 2 * 4 * (bench.WINDOW - 3)
            assert not list((tmp_path / "state" / "clean_background" / "rotation-views").glob("*.pkl"))
        finally:
            recovery.close()
