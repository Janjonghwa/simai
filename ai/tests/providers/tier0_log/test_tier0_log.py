"""TIER0_LOG WorldTrackProvider 시험 (명세 20.4 규칙 1·2·3·5·6, 11.4, 12.2 I1).

입력은 contracts/examples 의 world_state·observation_batch 예시(고치지 않고 복사본만 다룸)와,
시험 안에서 만드는 작은 합성 I1 기록 JSONL(시각 역순·중복·결측·카메라 변화·보정 버전 변화 포함)이다.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from simai_ai.providers import build_input_provenance, contract
from simai_ai.providers.testing import WorldTrackProviderContract, assert_no_violations, load_example
from simai_ai.providers.tier0_log import (
    JsonlTier0LogSource,
    Tier0FileModeResult,
    Tier0FileModeRunner,
    Tier0LogError,
    Tier0LogRecord,
    Tier0LogSource,
    Tier0LogWorldTrackProvider,
    tier0_log_provider_from_config,
)
from simai_ai.providers.tier0_log.provider import READER_VERSION

T0 = 1760342400000
COCO17 = 17


# --------------------------------------------------------------------------- 합성 기록


def _skeleton(seen: dict[int, list[float]]) -> dict:
    joints = [seen.get(j) for j in range(COCO17)]
    return {
        "joint_layout": "COCO17",
        "joints": joints,
        "visible": [p is not None for p in joints],
        "validity": ["VALID" if p is not None else "UNAVAILABLE" for p in joints],
        "joint_source": ["observed"] * COCO17,
    }


def _observed(world_id: str, t: int, xy: tuple[float, float], cameras: list[str], *, revision: int = 0) -> dict:
    x, y = xy
    return {
        "world_id": world_id,
        "t_last_valid": t,
        "observation_state": "OBSERVED",
        "track_lifecycle": "CONFIRMED",
        "identity_confidence": "HIGH",
        "identity_revision": revision,
        "frame_age_ms": 120,
        "position_xy": [x, y],
        "position_xy_sigma": [0.08, 0.08],
        "pelvis_xyz": [x, y, 0.92],
        "ground_anchor_xyz": [x, y, 0.0],
        "ground_anchor_sigma": [0.06, 0.06, 0.03],
        "support_surface_id": "floor",
        "support_surface_validity": "VALID",
        "contact_validity": "VALID",
        "z_validity": "VALID",
        "head_z": 1.68,
        "head_z_sigma": 0.05,
        "velocity": [0.9, 0.0, 0.0],
        "cameras": cameras,
        "skeleton": _skeleton({0: [x, y, 1.62], 11: [x - 0.1, y, 0.92], 12: [x + 0.1, y, 0.92]}),
    }


def _surface_unknown(world_id: str, t: int, xy: tuple[float, float], cameras: list[str]) -> dict:
    """골반은 보였지만 발목이 가려 지지 표면을 모르는 트랙: 접지점은 비운다(Z=0 대체 금지)."""
    tr = _observed(world_id, t, xy, cameras)
    tr.update(
        ground_anchor_xyz=None,
        ground_anchor_sigma=None,
        support_surface_id=None,
        support_surface_validity="UNKNOWN",
        contact_validity="UNAVAILABLE",
        z_validity="UNCERTAIN",
    )
    return tr


def _predicted(world_id: str, t_last_valid: int, xy: tuple[float, float]) -> dict:
    """관측이 끊긴 예측 트랙 (결측): 골반·접지·머리·관절 모두 없음."""
    return {
        "world_id": world_id,
        "t_last_valid": t_last_valid,
        "observation_state": "PREDICTED",
        "track_lifecycle": "OCCLUDED",
        "identity_confidence": "MEDIUM",
        "identity_revision": 0,
        "position_xy": list(xy),
        "position_xy_sigma": [0.25, 0.25],
        "pelvis_xyz": None,
        "ground_anchor_xyz": None,
        "support_surface_id": None,
        "support_surface_validity": "UNKNOWN",
        "contact_validity": "UNAVAILABLE",
        "z_validity": "UNAVAILABLE",
        "head_z": None,
        "cameras": [],
    }


def _world_state(msg_id: str, t: int, tracks: list[dict], *, cal="cal-7", sent_at=None, source_id="tier0-01") -> dict:
    return {
        "schema_version": "1.0.0",
        "msg_type": "world_state",
        "msg_id": msg_id,
        "sent_at": (t + 20) if sent_at is None else sent_at,
        "source_id": source_id,
        "payload": {"t_latest_global": t, "calibration_version": cal, "scene_version": "scene-3", "tracks": tracks},
    }


def _det(world_id: str | None, local: str, x: float) -> dict:
    kps = [None] * COCO17
    kps[0] = [x + 50, 320.0, 0.9]
    return {
        "local_track_id": local,
        "bbox": [x, 300.0, x + 200, 860.0],
        "score": 0.9,
        "keypoints_2d": kps,
        "world_id": world_id,
    }


def _frame(camera_id: str, epoch: int, seq: int, time: int, detections: list[dict]) -> dict:
    return {
        "camera_id": camera_id,
        "stream_epoch": epoch,
        "frame_sequence": seq,
        "estimated_capture_time_ms": time,
        "timestamp_source": "RECEIVE_ESTIMATED",
        "timestamp_uncertainty_ms": 18,
        "detections": detections,
    }


def _batch(msg_id: str, frames: list[dict], *, cal="cal-7") -> dict:
    return {
        "schema_version": "1.2.0",
        "msg_type": "observation_batch",
        "msg_id": msg_id,
        "sent_at": max(f["estimated_capture_time_ms"] for f in frames) + 300,
        "source_id": "tier0-01",
        "payload": {"calibration_version": cal, "joint_layout": "COCO17", "frames": frames},
    }


def _health(msg_id: str, t: int) -> dict:
    return {
        "schema_version": "1.0.0",
        "msg_type": "health",
        "msg_id": msg_id,
        "sent_at": t,
        "source_id": "tier0-01",
        "payload": {},
    }


def synthetic_log() -> list[dict]:
    """받은 순서의 I1 기록. 시각 역순, 재전송, 같은 시각 재계산, 400 ms 공백, 카메라 교체, 보정 버전 변경을 담는다.

    | t | 트랙 | 기대 per_camera |
    |---|---|---|
    | 100 | w-1(cam1, cam2) | w-1: cam1, cam2 |
    | 200 | w-1(cam1, cam2), w-2 예측 | w-1: cam1 (cam2 프레임에 w-1 검출이 둘 → 넣지 않음), w-2: 없음 |
    | 300 | w-1(cam1, cam3), w-3 지지 표면 모름 | w-1: cam1, cam3 (cam2 는 80 ms 떨어져 허용 밖), w-3: cam3 |
    | 400 | (기록 없음) | 채우지 않음 |
    | 500 | w-1 재계산(identity_revision 1) | w-1: cam1(stream_epoch 2, 재연결) |
    | 600 | w-1, cal-8 | w-1: cam1(cal-8 프레임). cam2 의 cal-7 프레임은 쓰지 않음 |
    """
    t = {k: T0 + k for k in (100, 200, 300, 500, 600)}
    ws100 = _world_state("ws-100", t[100], [_observed("w-1", t[100], (3.0, 5.0), ["cam1", "cam2"])])
    ws200 = _world_state(
        "ws-200",
        t[200],
        [_observed("w-1", t[200], (3.1, 5.0), ["cam1", "cam2"]), _predicted("w-2", t[100] - 400, (6.1, 2.2))],
    )
    ws300 = _world_state(
        "ws-300",
        t[300],
        [
            _observed("w-1", t[300] - 20, (3.2, 5.0), ["cam1", "cam3"]),
            _surface_unknown("w-3", t[300], (1.5, 1.0), ["cam1", "cam3"]),
        ],
    )
    ws500_first = _world_state("ws-500a", t[500], [_observed("w-1", t[500], (3.4, 5.0), ["cam1", "cam3"])])
    ws500_later = _world_state(
        "ws-500b", t[500], [_observed("w-1", t[500], (3.4, 5.0), ["cam1", "cam3"], revision=1)], sent_at=t[500] + 90
    )
    ws600 = _world_state("ws-600", t[600], [_observed("w-1", t[600], (3.5, 5.0), ["cam1", "cam3"])], cal="cal-8")

    obs_a = _batch(
        "ob-a",
        [
            _frame("cam1", 1, 10, t[100] + 2, [_det("w-1", "cam1-t1", 800)]),
            _frame("cam2", 4, 70, t[100] - 5, [_det("w-1", "cam2-t9", 400), _det(None, "cam2-t10", 1200)]),
            _frame("cam1", 1, 12, t[200] - 1, [_det("w-1", "cam1-t1", 810)]),
            _frame("cam2", 4, 72, t[200] + 4, [_det("w-1", "cam2-t9", 410), _det("w-1", "cam2-t11", 600)]),
        ],
    )
    obs_b = _batch(
        "ob-b",
        [
            _frame("cam1", 1, 14, t[300] + 10, [_det("w-1", "cam1-t1", 820), _det("w-3", "cam1-t4", 100)]),
            _frame("cam2", 4, 74, t[300] - 80, [_det("w-1", "cam2-t9", 420)]),
            _frame("cam3", 0, 5, t[300] - 3, [_det("w-1", "cam3-t2", 900), _det("w-3", "cam3-t3", 300)]),
            _frame("cam1", 2, 0, t[500] + 1, [_det("w-1", "cam1-t1", 840)]),
        ],
    )
    # 같은 프레임(cam1, 2, 0)을 다른 msg_id 로 다시 보낸 묶음: 프레임 중복으로 한 번만 쓴다
    obs_b_resent_frame = _batch("ob-b2", [_frame("cam1", 2, 0, t[500] + 1, [_det("w-1", "cam1-t1", 999)])])
    obs_c8 = _batch("ob-c8", [_frame("cam1", 2, 6, t[600], [_det("w-1", "cam1-t1", 850)])], cal="cal-8")
    obs_c7 = _batch("ob-c7", [_frame("cam2", 4, 80, t[600] + 2, [_det("w-1", "cam2-t9", 430)])])

    return [
        ws100,
        obs_a,
        ws300,  # 시각 역순: 300 이 200 보다 먼저 왔다
        ws200,
        _health("h-1", t[200]),
        obs_b,
        ws300,  # 재전송(같은 msg_id)
        ws500_later,
        ws500_first,  # 같은 시각, 먼저 계산한 것이 나중에 기록됨 → sent_at 이 늦은 쪽을 쓴다
        obs_b_resent_frame,
        obs_a,  # 묶음 재전송(같은 msg_id)
        ws600,
        obs_c8,
        obs_c7,
    ]


def write_jsonl(path: Path, messages: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\n\n", encoding="utf-8")
    return path


@pytest.fixture
def log_path(tmp_path) -> Path:
    return write_jsonl(tmp_path / "i1.jsonl", synthetic_log())


@pytest.fixture
def provider(log_path) -> Tier0LogWorldTrackProvider:
    return Tier0LogWorldTrackProvider.from_paths(log_path, tier0_build="tier0-abc1234")


def _doc_at(provider, dt: int) -> dict:
    return next(d for d in provider.frames() if d["t"] == T0 + dt)


def _track(doc: dict, world_id: str) -> dict:
    return next(tr for tr in doc["tracks"] if tr["world_id"] == world_id)


def _cams(track: dict) -> list[str]:
    return [o["camera_id"] for o in track["per_camera"]]


# --------------------------------------------------------------------------- 공통 계약 시험


class TestTier0LogSynthetic(WorldTrackProviderContract):
    """합성 기록 (fixture `provider`)."""


def _padded_example_log(tmp_path: Path) -> Path:
    """contracts/examples 의 world_state·observation_batch 복사본. 예시는 관절 배열을 줄여 적었으므로(2개)
    COCO17 길이로 늘린다(늘린 관절은 null·UNAVAILABLE·보이지 않음). 그 밖의 값은 예시 그대로다."""
    ws = load_example("world_state.json")
    for tr in ws["payload"]["tracks"]:
        sk = tr.get("skeleton")
        if sk:
            n = COCO17 - len(sk["joints"])
            sk["joints"] += [None] * n
            sk["visible"] += [False] * n
            sk["validity"] += ["UNAVAILABLE"] * n
            sk["joint_source"] += ["observed"] * n
    ob = load_example("observation_batch.json")
    for f in ob["payload"]["frames"]:
        for det in f["detections"]:
            det["keypoints_2d"] += [None] * (COCO17 - len(det["keypoints_2d"]))
    return write_jsonl(tmp_path / "example.jsonl", [ws, ob])


class TestTier0LogContractsExample(WorldTrackProviderContract):
    """contracts/examples 의 I1 예시 (관절 배열 길이만 늘림)."""

    @pytest.fixture
    def provider(self, tmp_path):
        return Tier0LogWorldTrackProvider.from_paths(_padded_example_log(tmp_path))


class TestTier0LogWorldStateOnly(WorldTrackProviderContract):
    """observation_batch 없이 world_state 만 있어도 계약을 지킨다 (per_camera 는 빈 배열)."""

    @pytest.fixture
    def provider(self, tmp_path):
        msgs = [m for m in synthetic_log() if m["msg_type"] == "world_state"]
        return Tier0LogWorldTrackProvider.from_paths(write_jsonl(tmp_path / "ws.jsonl", msgs))


# --------------------------------------------------------------------------- 예시 그대로 옮기기


def test_example_values_pass_through_unchanged(tmp_path):
    """트랙 값은 고치거나 채우지 않는다: 예시의 줄인 관절 배열도 그대로 나오고 per_camera 만 붙는다."""
    ws, ob = load_example("world_state.json"), load_example("observation_batch.json")
    provider = Tier0LogWorldTrackProvider.from_paths(write_jsonl(tmp_path / "e.jsonl", [ob, ws]))
    (doc,) = list(provider.frames())
    assert doc["t"] == ws["payload"]["t_latest_global"]
    assert doc["calibration_version"] == "cal-7" and doc["scene_version"] == "scene-3"
    assert doc["coordinate_frame"] == {"frame": "WORLD", "unit": "m", "handedness": "right", "up_axis": "+Z"}
    for got, src in zip(doc["tracks"], ws["payload"]["tracks"], strict=True):
        assert {k: v for k, v in got.items() if k != "per_camera"} == src
    w12, w13 = doc["tracks"]
    assert w12["per_camera"] == [
        {
            "camera_id": "cam1",
            "local_track_id": "cam1-t7",
            "bbox": [812, 300, 1020, 860],
            "keypoints_2d": [[910, 340, 0.95], None],
            "joint_layout": "COCO17",
            "frame_ref": {"stream_epoch": 12, "frame_sequence": 10324},
            "estimated_capture_time": 1760342400100,
            "timestamp_source": "RECEIVE_ESTIMATED",
            "timestamp_uncertainty_ms": 18,
        }
    ]
    # 예측 트랙: 골반·접지점은 비운 그대로, 관측도 지어내지 않는다
    assert w13["ground_anchor_xyz"] is None and w13["pelvis_xyz"] is None and w13["per_camera"] == []


def test_z0_fill_example_is_rejected(tmp_path):
    """접지 미확인인데 Z=0 으로 채운 기록(contracts/examples/invalid)은 고치지 않고 거부한다."""
    bad = load_example("invalid/world_state.z0_fill.json")
    path = write_jsonl(tmp_path / "bad.jsonl", [load_example("world_state.json"), bad])
    with pytest.raises(Tier0LogError, match=r"bad\.jsonl:2.*Z=0"):
        Tier0LogWorldTrackProvider.from_paths(path)


# --------------------------------------------------------------------------- 시각 순서·중복·결측


def test_time_order_dedup_and_gap(provider):
    assert provider.times == tuple(T0 + dt for dt in (100, 200, 300, 500, 600))
    # 400 ms 공백은 채우지 않는다 (보간 안 함)
    assert T0 + 400 not in provider.times
    s = provider.stats
    assert (s.out_of_order, s.duplicate_messages, s.replaced_same_t) == (1, 2, 1)
    assert s.ignored_types == {"health": 1}
    assert s.duplicate_frames == 1


def test_same_time_keeps_latest_sent_at(provider):
    assert _track(_doc_at(provider, 500), "w-1")["identity_revision"] == 1


def test_missing_values_stay_empty(provider):
    w2 = _track(_doc_at(provider, 200), "w-2")
    assert w2["pelvis_xyz"] is None and w2["ground_anchor_xyz"] is None and w2["head_z"] is None
    assert w2["per_camera"] == [] and w2["observation_state"] == "PREDICTED"
    w3 = _track(_doc_at(provider, 300), "w-3")
    assert w3["ground_anchor_xyz"] is None and w3["support_surface_validity"] == "UNKNOWN"
    assert w3["pelvis_xyz"] == [1.5, 1.0, 0.92]


# --------------------------------------------------------------------------- 카메라·보정 버전


def test_per_camera_follows_camera_changes(provider):
    assert _cams(_track(_doc_at(provider, 100), "w-1")) == ["cam1", "cam2"]
    # cam2 프레임에 w-1 로 연결된 검출이 둘: 같은 카메라의 두 검출을 한 World ID 에 묶지 않는다
    assert _cams(_track(_doc_at(provider, 200), "w-1")) == ["cam1"]
    assert provider.stats.ambiguous_detections == 1
    # cam2 가 빠지고 cam3 가 들어옴. cam2 프레임은 80 ms 떨어져 허용 차이(50 ms) 밖이다
    doc300 = _doc_at(provider, 300)
    assert _cams(_track(doc300, "w-1")) == ["cam1", "cam3"]
    assert _cams(_track(doc300, "w-3")) == ["cam1", "cam3"]
    assert _track(doc300, "w-1")["cameras"] == ["cam1", "cam3"]


def test_per_camera_keeps_frame_identity_and_capture_time(provider):
    (cam1,) = _track(_doc_at(provider, 500), "w-1")["per_camera"]
    # 재연결(stream_epoch 2) 프레임 식별자와 그 프레임의 촬영 시각을 그대로 쓴다 (문서 시각 t 가 아님)
    assert cam1["frame_ref"] == {"stream_epoch": 2, "frame_sequence": 0}
    assert cam1["estimated_capture_time"] == T0 + 501
    assert cam1["timestamp_source"] == "RECEIVE_ESTIMATED" and cam1["timestamp_uncertainty_ms"] == 18
    # 다른 msg_id 로 다시 온 같은 프레임은 처음 것을 쓴다
    assert cam1["bbox"] == [840, 300.0, 1040, 860.0]


def test_calibration_version_preserved_and_matched(provider):
    assert provider.calibration_versions == ("cal-7", "cal-8")
    assert [d["calibration_version"] for d in provider.frames()] == ["cal-7"] * 4 + ["cal-8"]
    # cal-8 문서에는 cal-8 관측만 붙는다 (cam2 의 cal-7 프레임은 시각이 맞아도 쓰지 않음)
    assert _cams(_track(_doc_at(provider, 600), "w-1")) == ["cam1"]


def test_max_obs_offset_is_configurable(log_path):
    wide = Tier0LogWorldTrackProvider.from_paths(log_path, max_obs_offset_ms=100)
    assert _cams(_track(_doc_at(wide, 300), "w-1")) == ["cam1", "cam2", "cam3"]
    none = Tier0LogWorldTrackProvider.from_paths(log_path, max_obs_offset_ms=0)
    assert _cams(_track(_doc_at(none, 600), "w-1")) == ["cam1"]
    assert _cams(_track(_doc_at(none, 100), "w-1")) == []


def test_time_window(log_path):
    p = Tier0LogWorldTrackProvider.from_paths(log_path, t_start_ms=T0 + 200, t_end_ms=T0 + 500)
    assert p.times == (T0 + 200, T0 + 300)
    assert p.stats.out_of_window > 0
    with pytest.raises(Tier0LogError, match="world_state"):
        Tier0LogWorldTrackProvider.from_paths(log_path, t_start_ms=T0 + 400, t_end_ms=T0 + 450)


# --------------------------------------------------------------------------- 출처


def test_provenance_is_not_upper_bound(provider):
    assert provider.source == "TIER0_LOG"
    assert provider.provider_version == f"{READER_VERSION}+tier0-abc1234"
    assert {(d["provider_source"], d["provider_version"]) for d in provider.frames()} == {
        ("TIER0_LOG", provider.provider_version)
    }
    prov = build_input_provenance(
        {"source": "FILE", "camera_ids": ["cam1", "cam2", "cam3"]},
        calibration={"source": "G1_AUTO", "calibration_version": "cal-7"},
        tracks=provider.provenance(),
    )
    assert prov["tracks"] == {"source": "TIER0_LOG", "provider_version": provider.provider_version}
    assert prov["is_upper_bound"] is False
    assert_no_violations(contract.check_input_provenance(prov))


def test_default_provider_version_is_reader_version(log_path):
    assert Tier0LogWorldTrackProvider.from_paths(log_path).provider_version == READER_VERSION


def test_frames_are_fresh_copies(provider):
    first = next(provider.frames())
    first["tracks"][0]["pelvis_xyz"] = [0.0, 0.0, 0.0]
    first["tracks"][0]["per_camera"].clear()
    again = next(provider.frames())
    assert again["tracks"][0]["pelvis_xyz"] == [3.0, 5.0, 0.92]
    assert _cams(again["tracks"][0]) == ["cam1", "cam2"]


# --------------------------------------------------------------------------- 거부하는 기록


def _single(tmp_path, message) -> Path:
    return write_jsonl(tmp_path / "one.jsonl", [message])


def test_rejects_mixed_tier0_sources(tmp_path):
    msgs = synthetic_log()[:1] + [_world_state("other", T0 + 900, [], source_id="tier0-02")]
    with pytest.raises(Tier0LogError, match="source_id"):
        Tier0LogWorldTrackProvider.from_paths(write_jsonl(tmp_path / "mix.jsonl", msgs))


def test_rejects_unsupported_major_version(tmp_path):
    msg = _world_state("v2", T0, [])
    msg["schema_version"] = "2.0.0"
    with pytest.raises(Tier0LogError, match="major"):
        Tier0LogWorldTrackProvider.from_paths(_single(tmp_path, msg))


def test_rejects_malformed_line_with_location(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(_world_state("ok", T0, [])) + '\n{"msg_type": "world_st\n', encoding="utf-8")
    with pytest.raises(Tier0LogError, match=r"broken\.jsonl:2"):
        Tier0LogWorldTrackProvider.from_paths(path)


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda m: m["payload"].pop("calibration_version"), "calibration_version"),
        (lambda m: m["payload"].__setitem__("t_latest_global", 1.5), "t_latest_global"),
        (lambda m: m.pop("msg_id"), "msg_id"),
        (lambda m: m["payload"]["tracks"].append({"position_xy": [0, 0]}), "world_id"),
    ],
)
def test_rejects_contract_violations(tmp_path, mutate, match):
    msg = _world_state("bad", T0, [_observed("w-1", T0, (1.0, 1.0), ["cam1", "cam2"])])
    mutate(msg)
    with pytest.raises(Tier0LogError, match=match):
        Tier0LogWorldTrackProvider.from_paths(_single(tmp_path, msg))


def test_rejects_observation_without_capture_time_contract(tmp_path):
    batch = _batch("ob", [_frame("cam1", 0, 1, T0, [])])
    del batch["payload"]["frames"][0]["timestamp_source"]
    with pytest.raises(Tier0LogError, match="timestamp_source"):
        Tier0LogWorldTrackProvider.from_paths(write_jsonl(tmp_path / "o.jsonl", [_world_state("ws", T0, []), batch]))


def test_no_world_state_is_an_error(tmp_path):
    with pytest.raises(Tier0LogError, match="world_state"):
        Tier0LogWorldTrackProvider.from_paths(_single(tmp_path, _health("h", T0)))


# --------------------------------------------------------------------------- 입력 위치


def test_reads_directory_and_separate_sources(tmp_path):
    """world_state 와 observation_batch 가 다른 파일(저장소)에 있어도 같은 결과를 낸다."""
    logs = tmp_path / "logs"
    logs.mkdir()
    msgs = synthetic_log()
    write_jsonl(logs / "a_world_state.jsonl", [m for m in msgs if m["msg_type"] == "world_state"])
    write_jsonl(logs / "b_observation.jsonl", [m for m in msgs if m["msg_type"] != "world_state"])
    split = Tier0LogWorldTrackProvider.from_paths(logs)
    joined = Tier0LogWorldTrackProvider.from_paths(write_jsonl(tmp_path / "all.jsonl", msgs))
    assert list(split.frames()) == list(joined.frames())


class _MemorySource(Tier0LogSource):
    """Spatial DB 리더 자리(후속): 같은 레코드 형식이면 Provider 는 저장 위치를 모른다."""

    def __init__(self, messages):
        self.messages = messages
        self.windows = []

    def describe(self):
        return "memory"

    def records(self, t_start_ms=None, t_end_ms=None):
        self.windows.append((t_start_ms, t_end_ms))
        for i, m in enumerate(self.messages):
            yield Tier0LogRecord(copy.deepcopy(m), f"memory[{i}]")


def test_custom_source_interface(log_path):
    src = _MemorySource(synthetic_log())
    p = Tier0LogWorldTrackProvider([src], t_start_ms=T0, t_end_ms=T0 + 1000)
    assert src.windows == [(T0, T0 + 1000)]
    assert list(p.frames()) == list(Tier0LogWorldTrackProvider.from_paths(log_path).frames())


def test_jsonl_source_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        JsonlTier0LogSource(tmp_path / "none.jsonl")


# --------------------------------------------------------------------------- 설정·파일 모드 연결 지점


def test_config_log_paths(log_path):
    p = tier0_log_provider_from_config(
        {"log_paths": str(log_path), "tier0_build": "b1", "t_start_ms": T0 + 100, "max_obs_offset_ms": 50}
    )
    assert p.provider_version == f"{READER_VERSION}+b1"
    assert p.times[0] == T0 + 100


@pytest.mark.parametrize(
    "cfg",
    [
        {},
        {"log_paths": ["x"], "file_mode": {"tier0_config": "a", "recording": "b", "output_dir": "c"}},
        {"log_paths": ["x"], "max_offset": 10},
    ],
)
def test_config_rejects_bad_keys(cfg):
    with pytest.raises(ValueError):
        tier0_log_provider_from_config(cfg)


FILE_MODE = {"tier0_config": "tier0.yaml", "recording": "recordings/a01", "output_dir": "runs/a01"}


def test_file_mode_needs_runner():
    with pytest.raises(NotImplementedError, match="log_paths"):
        tier0_log_provider_from_config({"file_mode": FILE_MODE})


def test_file_mode_runner_connection(log_path):
    class FakeRunner(Tier0FileModeRunner):
        def __init__(self):
            self.specs = []

        def run(self, spec):
            self.specs.append(spec)
            return Tier0FileModeResult(log_paths=(log_path,), tier0_build="tier0-deadbeef")

    runner = FakeRunner()
    p = tier0_log_provider_from_config({"file_mode": FILE_MODE}, runner=runner)
    assert runner.specs[0].output_dir == Path("runs/a01")
    assert p.provider_version == f"{READER_VERSION}+tier0-deadbeef"
    with pytest.raises(ValueError, match="tier0_build"):
        tier0_log_provider_from_config({"file_mode": FILE_MODE, "tier0_build": "x"}, runner=runner)
