"""늦은 카메라 하나가 융합 시각을 끌어내리지 않는다 (S15P31A101-219, docs/design/tier0-fusion-time.md).

SIM 2D keypoint 를 카메라별 도착 지연을 붙여 Perception 에 넣고 tick(100 ms)마다 인지 결과를 본다.
- 400 ms 늦은 cam3 는 융합 시각 계산과 그 tick 관측에서 빠지고(Perceived.lagging, 트랙 cameras 에 없음) 나머지로 OBSERVED 다.
- 반례: 늦은 카메라와 한 대만 사람을 보는 구간에 위치를 지어내지 않는다(외삽 금지, 2대 미만). 늦은 카메라와 한 대만 보는
  관절은 null 이다. 지연이 풀리면 다시 합류한다.
- 음성 대조: 늦은 카메라의 마지막 프레임을 t 로 끌어 쓰면(외삽) 두 반례 시험이 실패한다. 융합 시각을 최솟값으로 되돌리면
  트랙이 PREDICTED 가 된다.
SIM 상한 결과다(AGENTS 규칙 7).
"""

from __future__ import annotations

import dataclasses
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[2]
for _p in (AI_DIR, HERE):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import tier0_site as ts  # noqa: E402

from services.tier0 import perception as perception_module  # noqa: E402
from services.tier0.perception import Perception, Perceived, sim_frame_to_observation  # noqa: E402
from simai_ai.contact import SurfaceSet  # noqa: E402
from simai_ai.geometry.interpolation import InterpolatedView  # noqa: E402
from simai_ai.geometry.rig import CameraRig  # noqa: E402
from simai_ai.io import SimObservationFrameSource  # noqa: E402
from simai_ai.providers.files import FileCalibrationProvider  # noqa: E402
from simai_ai.tracking.cross_view import LocalView  # noqa: E402
from simai_ai.tracking.local_tracker import Observation2DFrame, TrackedDetection  # noqa: E402
from simai_ai.tracking.time_model import FusionTime  # noqa: E402

START = 1_760_342_400_000
DURATION = 6000
TICK = 100
BASE_LAG = 30  # 노트북 수신 지연(golden.RECEIVE_LAG_MS 와 같음)
SLOW = "cam3"
R_ARM = (8, 10)  # COCO17 오른 팔꿈치·손목


@pytest.fixture(scope="module")
def run_dir(tmp_path_factory):
    """한 사람이 교실 가운데에 서 있다(4대 모두 본다)."""
    doc = ts.scenario(DURATION, start_time_ms=START)
    doc["people"] = [{"id": "stander", "height_m": 1.72, "waypoints": [{"t_ms": 0, "xy": [4.0, 3.5]}]}]
    return ts.run_sim(tmp_path_factory.mktemp("fusion"), doc)


Edit = Callable[[Observation2DFrame], Observation2DFrame]
Lag = Callable[[str, int], int]  # (카메라, 장면 상대 촬영 시각) → 도착 지연 ms


class LagFeed:
    """SIM 관측을 카메라별 도착 지연으로 내는 FrameFeed. 지연이 바뀌면 역순 도착도 생긴다(Perception 이 정렬해 받는다)."""

    source = "SIM"

    def __init__(self, run: Path, lag: Lag, edit: Edit = lambda f: f) -> None:
        src = SimObservationFrameSource(run)
        self.camera_ids = src.camera_ids
        self._provenance = src.provenance()
        frames = [edit(sim_frame_to_observation(f, 0)) for f in src.frames()]
        arrive = {id(f): f.estimated_capture_time_ms + lag(f.camera_id, f.estimated_capture_time_ms - START)
                  for f in frames}
        self._pending = sorted(frames, key=lambda f: (arrive[id(f)], f.camera_id))
        self._arrive = arrive

    def poll(self, now_ms: int) -> list[Observation2DFrame]:
        out = []
        while self._pending and self._arrive[id(self._pending[0])] <= now_ms:
            out.append(self._pending.pop(0))
        return out

    def camera_connected(self, camera_id: str) -> bool:
        return True

    def provenance(self):
        return self._provenance

    def close(self) -> None:
        return None


def lag_of(slow_ms: int, window: tuple[int, int] = (0, DURATION)) -> Lag:
    return lambda cam, rel: BASE_LAG + (slow_ms if cam == SLOW and window[0] <= rel < window[1] else 0)


def hide(cameras: tuple[str, ...], window: tuple[int, int] = (0, DURATION), joints: tuple[int, ...] | None = None) -> Edit:
    """그 카메라·구간의 검출을 지운다(joints 를 주면 그 관절만 지운다)."""

    def edit(f: Observation2DFrame) -> Observation2DFrame:
        rel = f.estimated_capture_time_ms - START
        if f.camera_id not in cameras or not window[0] <= rel < window[1]:
            return f
        if joints is None:
            return dataclasses.replace(f, detections=())
        dets = []
        for d in f.detections:
            kps = tuple(None if i in joints else k for i, k in enumerate(d.detection.keypoints_2d))
            dets.append(TrackedDetection(d.local_track_id, dataclasses.replace(d.detection, keypoints_2d=kps)))
        return dataclasses.replace(f, detections=tuple(dets))

    return edit


def perceive(run: Path, lag: Lag, edit: Edit = lambda f: f,
             patch: Callable[[Perception], None] | None = None) -> tuple[Perception, list[Perceived]]:
    rig = CameraRig.from_provider(FileCalibrationProvider(run / "calibration.json"))
    feed = LagFeed(run, lag, edit)
    p = Perception(rig, feed, session="fusiontest", scene_version="scene-sim", input_provenance=feed.provenance())
    if patch is not None:
        patch(p)
    out = []
    for now in range(START, START + DURATION + 600, TICK):
        r = p.step(now, SurfaceSet())
        if r is not None:
            out.append(r)
    return p, out


def tracks(results: list[Perceived], window: tuple[int, int] = (0, DURATION + 600)):
    """(상대 시각, 트랙 문서) — 결과 t 가 구간 안인 것."""
    return [(r.t - START, tr) for r in results if window[0] <= r.t - START < window[1] for tr in r.payload["tracks"]]


def hold_last(p: Perception) -> None:
    """음성 대조용 결함: t 뒤 프레임이 없는 트랙의 마지막 프레임을 t 시각 view 로 끌어 쓴다(외삽)."""
    buffer = p.buffer
    original = buffer.views_at

    def views_at(t: int):
        views, drops = original(t)
        have = {(v.camera_id, v.local_track_id) for v in views}
        for key in buffer.keys:
            samples = buffer._buffers[key].samples
            if key not in have and samples and samples[-1].estimated_capture_time < t:
                s = samples[-1]
                views.append(LocalView(key[0], key[1], InterpolatedView(
                    key[0], t, s.keypoints, s.timestamp_source, float(s.timestamp_uncertainty_ms or 0.0),
                    (s.estimated_capture_time,))))
        return views, drops

    buffer.views_at = views_at  # type: ignore[method-assign]


# --------------------------------------------------------------------------- 늦은 카메라 빼기


def test_lagging_camera_is_left_out_and_others_stay_observed(run_dir) -> None:
    p, results = perceive(run_dir, lag_of(400))
    steady = tracks(results, (1000, DURATION))
    assert steady and all(tr["observation_state"] == "OBSERVED" for _, tr in steady)
    assert all(SLOW not in tr["cameras"] and len(tr["cameras"]) >= 2 for _, tr in steady)
    assert {r.lagging for r in results if r.t - START >= 1000} == {(SLOW,)}
    assert p.lagging_ticks[SLOW] >= 40 and all(v == 0 for c, v in p.lagging_ticks.items() if c != SLOW)


def test_negative_control_min_fusion_time_makes_tracks_predicted(run_dir, monkeypatch) -> None:
    """음성 대조: 융합 시각을 수신 중 카메라 최솟값(이전 구현)으로 되돌리면 같은 입력의 트랙이 PREDICTED 가 된다."""

    def oldest(cameras, config):
        times = [c.last_capture for c in cameras if c.last_capture is not None]
        if not times:
            return None
        return FusionTime(min(t for t in times if max(times) - t <= config.camera_stall_ms))

    monkeypatch.setattr(perception_module, "choose_fusion_time", oldest)
    _, results = perceive(run_dir, lag_of(400))
    steady = tracks(results, (1000, DURATION))
    assert steady and sum(tr["observation_state"] == "PREDICTED" for _, tr in steady) > len(steady) // 2


def test_lagging_camera_rejoins_when_it_catches_up(run_dir) -> None:
    """반례(재연결·복귀): 1~3 s 에만 늦으면 그동안만 빠지고, 지연이 풀린 뒤 4대가 다시 기여한다(역순 도착 포함)."""
    _, results = perceive(run_dir, lag_of(400, (1000, 3000)))
    late = tracks(results, (2000, 3000))
    after = tracks(results, (4000, DURATION))
    assert late and all(SLOW not in tr["cameras"] for _, tr in late)
    assert after and all(SLOW in tr["cameras"] and tr["observation_state"] == "OBSERVED" for _, tr in after)
    assert all(r.lagging == () for r in results if r.t - START >= 4000)


# --------------------------------------------------------------------------- 지어내지 않기 (반례)


GAP = (2000, 4000)  # cam2·cam4 가 사람을 못 보는 구간(수신은 계속)


def _no_position_while_only_lagging_and_one_see(results) -> list[str]:
    bad = []
    for rel, tr in tracks(results, (GAP[0] + 200, GAP[1])):
        if tr["t_last_valid"] - START > GAP[0] or SLOW in tr["cameras"]:
            bad.append(f"{rel}: t_last_valid {tr['t_last_valid'] - START} cameras {tr['cameras']}")
    return bad


def test_only_lagging_camera_and_one_other_see_the_person_no_position_is_made(run_dir) -> None:
    """반례(외삽 금지·2대 미만): cam2·cam4 가 못 보는 동안 사람을 보는 것은 cam1 과 늦은 cam3 뿐이다. 늦은 cam3 를 그 시각으로
    끌어오지 않으므로 그 구간에 새 관측이 없고(t_last_valid 가 앞으로 가지 않음) 시간 모델대로 PREDICTED → UNKNOWN 이 된다."""
    _, results = perceive(run_dir, lag_of(400), hide(("cam2", "cam4"), GAP))
    assert tracks(results, (GAP[0] + 200, GAP[1]))  # 트랙은 남아 있다(빈 시험이 아니다)
    assert _no_position_while_only_lagging_and_one_see(results) == []
    states = {tr["observation_state"] for _, tr in tracks(results, (GAP[0] + 600, GAP[1]))}
    assert states == {"UNKNOWN"}, states


def test_negative_control_extrapolated_lagging_view_is_caught(run_dir) -> None:
    """음성 대조: 늦은 카메라의 마지막 프레임을 t 로 끌어 쓰는 결함을 넣으면 위 반례 시험의 검사가 위치를 잡아낸다."""
    _, results = perceive(run_dir, lag_of(400), hide(("cam2", "cam4"), GAP), patch=hold_last)
    assert _no_position_while_only_lagging_and_one_see(results) != []


def _arm_joints(results, window=(1000, DURATION)):
    return [tr["skeleton"]["joints"][j] for _, tr in tracks(results, window) if "skeleton" in tr for j in R_ARM]


def test_joint_seen_only_by_lagging_camera_and_one_other_is_null(run_dir) -> None:
    """반례(2대 미만 관절): 오른팔 관절을 cam2·cam4 에서 지우면 그 관절은 cam1·cam3 만 본다. cam3 가 늦으면 그 관절은 null 이고
    (지어내지 않음) 나머지 관절은 남는다. 대조: 늦지 않으면 같은 관절이 cam1·cam3 로 삼각측량된다(빈 시험이 아니다)."""
    edit = hide(("cam2", "cam4"), joints=R_ARM)
    _, normal = perceive(run_dir, lag_of(0), edit)
    assert sum(j is not None for j in _arm_joints(normal)) > len(_arm_joints(normal)) // 2
    _, lagging = perceive(run_dir, lag_of(400), edit)
    arm = _arm_joints(lagging)
    assert arm and all(j is None for j in arm)
    sk = [tr["skeleton"] for _, tr in tracks(lagging, (1000, DURATION)) if "skeleton" in tr]
    assert sk and all(s["visible"][j] is False and s["validity"][j] == "UNAVAILABLE" for s in sk for j in R_ARM)
    assert all(s["joints"][11] is not None for s in sk)  # 왼 엉덩이는 3대가 본다


def test_negative_control_extrapolated_joint_is_caught(run_dir) -> None:
    _, results = perceive(run_dir, lag_of(400), hide(("cam2", "cam4"), joints=R_ARM), patch=hold_last)
    assert any(j is not None for j in _arm_joints(results))


def test_fusion_is_deterministic(run_dir) -> None:
    a = [(r.t, r.lagging, r.payload["tracks"]) for r in perceive(run_dir, lag_of(400))[1]]
    b = [(r.t, r.lagging, r.payload["tracks"]) for r in perceive(run_dir, lag_of(400))[1]]
    assert a == b
