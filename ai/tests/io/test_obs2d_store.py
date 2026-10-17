"""2D 관측 Parquet 보관소 시험 (명세 11.2, 11.5, 18.4, 요구사항 FR-141).

시뮬레이터(카메라 4대, 15 fps)로 정시를 가로지르는 표본 구간을 만들어 기록·읽기 왕복, 시간 경계 분할, 재시작 이어쓰기,
덮어쓰기 금지를 보고, 보관 기간 정리는 이름만 맞춘 파일로 본다. 1시간 분량은 시험 시간으로 길어서 표본 구간으로 보고,
용량은 tests/io/obs2d_sim.py 로 재서 docs/design/tier0-obs2d-parquet.md 에 적는다. SIM 입력이다(is_upper_bound).
"""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from simai_ai.io import obs2d_layout as layout
from simai_ai.io.obs2d_layout import (
    DAY_MS,
    HOUR_MS,
    ROOT_ENV,
    Obs2DStoreConfig,
    delete_files,
    file_name,
    list_files,
    parse_file_name,
    plan_expired,
    purge_expired,
)

pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")

from simai_ai.io.obs2d_parquet import (  # noqa: E402
    SCHEMA,
    Obs2DParquetWriter,
    file_metadata,
    iter_frames,
    publish_no_overwrite,
    read_observations,
)

HERE = Path(__file__).resolve().parent
AI_DIR = HERE.parents[1]
BOUNDARY = 1760342400000  # 정시 (UTC 2025-10-13T08:00:00Z)
CAMERAS = ("cam1", "cam2", "cam3", "cam4")
SESSION = "s20251013a"


@pytest.fixture(scope="module")
def sim():
    """tests/io/obs2d_sim.py (용량 측정 스크립트) 모듈. 시험도 같은 변환 함수를 쓴다."""
    spec = importlib.util.spec_from_file_location("obs2d_sim", HERE / "obs2d_sim.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def sim_run(sim, tmp_path_factory):
    """정시 10초 전부터 20초, 카메라 4대 15 fps, 사람 3명."""
    sc = sim.scenario(name="obs2d-boundary", start_time_ms=BOUNDARY - 10_000, duration_ms=20_000, fps=15, people=3)
    out = sim.run_sim(sc, tmp_path_factory.mktemp("obs2d-sim"))
    frames = sim.observation_frames(out)
    return {"out": out, "frames": frames, "payloads": list(sim.payloads(frames)), "provenance": sim.provenance(out)}


def _f32(x):
    return struct.unpack("f", struct.pack("f", x))[0]


def _expected(payloads):
    """입력 payload 를 iter_frames 출력 모양으로 (수는 float32 로 반올림, 정렬은 촬영 시각·카메라·프레임)."""
    out = []
    for p in payloads:
        for f in p["frames"]:
            out.append(
                {
                    "camera_id": f["camera_id"],
                    "stream_epoch": f["stream_epoch"],
                    "frame_sequence": f["frame_sequence"],
                    "estimated_capture_time_ms": f["estimated_capture_time_ms"],
                    "timestamp_source": f["timestamp_source"],
                    "timestamp_uncertainty_ms": float(f["timestamp_uncertainty_ms"]),
                    "calibration_version": p["calibration_version"],
                    "joint_layout": p["joint_layout"],
                    "session_id": SESSION,
                    "detections": [
                        {
                            "local_track_id": d["local_track_id"],
                            "bbox": [_f32(v) for v in d["bbox"]],
                            "score": _f32(d["score"]),
                            "keypoints_2d": [None if kp is None else [_f32(v) for v in kp] for kp in d["keypoints_2d"]],
                            "world_id": d["world_id"],
                        }
                        for d in f["detections"]
                    ],
                }
            )
    out.sort(key=lambda f: (f["estimated_capture_time_ms"], f["camera_id"], f["stream_epoch"], f["frame_sequence"]))
    return out


def _write(root, payloads, provenance, session=SESSION, **config):
    writer = Obs2DParquetWriter(Obs2DStoreConfig(root=root, **config), session_id=session, input_provenance=provenance)
    with writer:
        for p in payloads:
            writer.write_batch(p)
    return writer


# --------------------------------------------------------------------------- 왕복·분할


def test_sim_sample_round_trip(sim_run, tmp_path):
    """기록한 것을 그대로 읽는다 (float32 반올림 기준 완전 일치). 15 fps 입력은 15 Hz 상한에 걸려 빠지지 않는다."""
    writer = _write(tmp_path, sim_run["payloads"], sim_run["provenance"])
    n_frames = sum(len(p["frames"]) for p in sim_run["payloads"])
    assert writer.stats.frames_written == n_frames
    assert writer.stats.skipped_by_rate == writer.stats.skipped_out_of_order == 0
    assert not list(tmp_path.rglob(f"*{layout.TEMP_SUFFIX}"))

    table = read_observations(tmp_path)
    assert table.schema.names == SCHEMA.names
    assert list(iter_frames(table)) == _expected(sim_run["payloads"])


def test_hour_boundary_splits_files(sim_run, tmp_path):
    _write(tmp_path, sim_run["payloads"], sim_run["provenance"])
    files = list_files(tmp_path)
    assert {(f.camera_id, f.window_start_ms) for f in files} == {
        (c, w) for c in CAMERAS for w in (BOUNDARY - HOUR_MS, BOUNDARY)
    }
    assert {f.path.parent.relative_to(tmp_path).as_posix() for f in files} == {
        f"2025-10-13/{h}/{c}" for h in ("07", "08") for c in CAMERAS
    }
    for f in files:
        times = pq.read_table(f.path, columns=["capture_time_ms"])["capture_time_ms"].to_pylist()
        assert times and all(f.window_start_ms <= t < f.window_start_ms + HOUR_MS for t in times)
        meta = file_metadata(f.path)
        assert meta["window_start_ms"] == f.window_start_ms and meta["session_id"] == SESSION
        assert meta["input_provenance"] == sim_run["provenance"]
        assert meta["input_provenance"]["is_upper_bound"] is True  # SIM 입력은 상한 지표


def test_read_selects_time_range_and_cameras(sim_run, tmp_path):
    _write(tmp_path, sim_run["payloads"], sim_run["provenance"])
    start, end = BOUNDARY - 2_000, BOUNDARY + 3_000
    table = read_observations(tmp_path, start_ms=start, end_ms=end, camera_ids=["cam2", "cam4"])
    want = [
        f
        for f in _expected(sim_run["payloads"])
        if start <= f["estimated_capture_time_ms"] < end and f["camera_id"] in ("cam2", "cam4")
    ]
    assert want and list(iter_frames(table)) == want
    cols = read_observations(tmp_path, camera_ids=["cam1"], columns=["capture_time_ms", "keypoints_2d"])
    assert cols.schema.names == ["capture_time_ms", "keypoints_2d"]
    assert read_observations(tmp_path, start_ms=BOUNDARY + HOUR_MS).num_rows == 0
    assert read_observations(tmp_path / "nothing").num_rows == 0


def test_restart_continues_in_new_part(sim_run, tmp_path):
    """같은 세션으로 다시 시작해도 기존 파일을 건드리지 않고 다음 part 로 이어 쓴다. 죽은 기록기의 .inprogress 도 피한다."""
    payloads = sim_run["payloads"]
    half = len(payloads) * 3 // 4  # 정시 뒤 구간에서 끊는다
    _write(tmp_path, payloads[:half], sim_run["provenance"])
    first = {f.path: f.path.read_bytes() for f in list_files(tmp_path)}

    # 프로세스가 죽은 기록기: footer 없이 .inprogress 가 남는다
    crashed = Obs2DParquetWriter(
        Obs2DStoreConfig(root=tmp_path, row_group_rows=1), session_id=SESSION, input_provenance=sim_run["provenance"]
    )
    crashed.write_batch(payloads[half])
    leftovers = crashed.open_paths()
    for f in crashed._open.values():  # 프로세스 종료 흉내: footer 를 쓰지 않고 파일을 놓아 준다(Windows)
        f.writer.is_open = False
        f.handle.close()
    assert len(leftovers) == len(payloads[half]["frames"])

    _write(tmp_path, payloads[half:], sim_run["provenance"])
    for path, data in first.items():
        assert path.read_bytes() == data
    after = list_files(tmp_path, start_ms=BOUNDARY)
    parts = sorted({(f.camera_id, f.part) for f in after})
    assert parts == sorted({(c, p) for c in CAMERAS for p in (0, 2)})  # part 1 은 죽은 기록기가 차지
    assert all(p.exists() for p in leftovers)
    with_temp = list_files(tmp_path, include_in_progress=True)
    assert {f.path for f in with_temp if f.in_progress} == set(leftovers)
    # 읽기는 닫힌 파일만: 죽은 기록기의 묶음은 다시 쓴 쪽에서 한 번만 나온다
    assert list(iter_frames(read_observations(tmp_path))) == _expected(payloads)


def test_never_overwrites_existing_file(sim_run, tmp_path):
    config = Obs2DStoreConfig(root=tmp_path)
    t = sim_run["payloads"][-1]["frames"][0]["estimated_capture_time_ms"]
    taken = config.file_path("cam1", config.window_start(t), SESSION, 0)
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"not mine")
    _write(tmp_path, sim_run["payloads"][-5:], sim_run["provenance"])
    assert taken.read_bytes() == b"not mine"
    assert config.file_path("cam1", config.window_start(t), SESSION, 1).exists()

    temp = tmp_path / "x.inprogress"
    temp.write_bytes(b"new")
    with pytest.raises(FileExistsError):
        publish_no_overwrite(temp, taken)
    assert taken.read_bytes() == b"not mine" and temp.exists()


# --------------------------------------------------------------------------- 솎기·입력 검사


def _frame(t, cam="cam1", seq=0, detections=None):
    return {
        "camera_id": cam,
        "stream_epoch": 0,
        "frame_sequence": seq,
        "estimated_capture_time_ms": t,
        "timestamp_source": "RECEIVE_ESTIMATED",
        "timestamp_uncertainty_ms": 18,
        "detections": [] if detections is None else detections,
    }


def _det(**extra):
    return {
        "local_track_id": "cam1-t1",
        "bbox": [10.0, 20.0, 110.0, 320.0],
        "score": 0.9,
        "keypoints_2d": [[50.0, 40.0, 0.8]] * 16 + [None],
        "world_id": None,
        **extra,
    }


def _payload(frames, **kw):
    return {"calibration_version": "cal-7", "joint_layout": "COCO17", "frames": frames, **kw}


PROV = {"frames": {"source": "LIVE"}, "is_upper_bound": False}


@pytest.mark.parametrize(("hz", "expected"), [(15.0, 150), (5.0, 50)])
def test_store_rate_cap_on_25fps(tmp_path, hz, expected):
    """25 fps 입력(지터 ±3 ms)을 저장 상한으로 솎는다. 긴 구간 평균이 상한을 넘지 않는다 (명세 11.2 저장 5~15 Hz)."""
    jitter = [0, 3, -2, 1, -3, 2]
    frames = [_frame(BOUNDARY + 40 * k + jitter[k % 6], seq=k) for k in range(250)]  # 10초
    w = _write(tmp_path, [_payload([f]) for f in frames], PROV, store_max_hz=hz)
    assert abs(w.stats.frames_written - expected) <= 1
    assert w.stats.frames_written + w.stats.skipped_by_rate == 250


def test_out_of_order_frames_are_skipped(tmp_path):
    w = _write(tmp_path, [_payload([_frame(BOUNDARY + 100, seq=1)]), _payload([_frame(BOUNDARY, seq=0)])], PROV)
    assert w.stats.frames_written == 1 and w.stats.skipped_out_of_order == 1


def test_empty_frame_is_kept_and_envelope_accepted(tmp_path):
    message = {
        "schema_version": "1.2.0",
        "msg_type": "observation_batch",
        "msg_id": "0192a6f0-7c1e-7b3a-9f2d-000000000107",
        "sent_at": BOUNDARY + 500,
        "source_id": "tier0-01",
        "payload": _payload([_frame(BOUNDARY, detections=[_det(world_id="w-0012")]), _frame(BOUNDARY, cam="cam3")]),
    }
    _write(tmp_path, [message], PROV)
    frames = list(iter_frames(read_observations(tmp_path)))
    assert [(f["camera_id"], len(f["detections"])) for f in frames] == [("cam1", 1), ("cam3", 0)]
    assert frames[0]["detections"][0]["world_id"] == "w-0012"
    assert frames[0]["detections"][0]["keypoints_2d"][-1] is None


def test_only_coordinates_are_stored(tmp_path):
    """계약 밖 필드(영상·crop)는 열에 없어서 저장되지 않는다 (명세 11.5 얼굴 픽셀·영상 저장 금지)."""
    marker = "FACE-PIXELS-SHOULD-NOT-BE-STORED"
    frame = _frame(BOUNDARY, detections=[_det(face_crop_jpeg=marker)])
    frame["image"] = marker
    _write(tmp_path, [_payload([frame], preview=marker)], PROV)
    (f,) = list_files(tmp_path)
    assert pq.read_schema(f.path).names == SCHEMA.names
    assert marker.encode() not in f.path.read_bytes()


@pytest.mark.parametrize(
    "bad",
    [
        _payload([{**_frame(BOUNDARY), "timestamp_uncertainty_ms": None}]),  # 모르는 불확실성을 0 으로 채우지 않는다
        _payload([_frame(BOUNDARY, detections=[_det(keypoints_2d=[[1.0, 2.0, 0.5]] * 3)])]),  # COCO17 관절 수
        _payload([_frame(BOUNDARY, detections=[_det(score=1.5)])]),
        _payload([_frame(BOUNDARY), _frame(BOUNDARY, cam="bad id")]),  # 묶음 중 하나라도 틀리면 아무것도 쓰지 않는다
        _payload([]),
        {"msg_type": "world_state", "payload": _payload([_frame(BOUNDARY)])},
        _payload([_frame(1.5e12)]),
    ],
)
def test_invalid_batch_writes_nothing(tmp_path, bad):
    writer = Obs2DParquetWriter(Obs2DStoreConfig(root=tmp_path), session_id=SESSION, input_provenance=PROV)
    with pytest.raises((ValueError, TypeError)):
        writer.write_batch(bad)
    writer.close()
    assert not list(tmp_path.rglob("obs2d.*"))


def test_writer_requires_session_and_provenance(tmp_path):
    config = Obs2DStoreConfig(root=tmp_path)
    with pytest.raises(ValueError):
        Obs2DParquetWriter(config, session_id="a.b", input_provenance=PROV)
    with pytest.raises(ValueError):
        Obs2DParquetWriter(config, session_id=SESSION, input_provenance={"frames": {"source": "LIVE"}})


def test_close_before_closes_idle_camera(tmp_path):
    writer = Obs2DParquetWriter(Obs2DStoreConfig(root=tmp_path), session_id=SESSION, input_provenance=PROV)
    writer.write_batch(_payload([_frame(BOUNDARY - 1000, cam="cam2")]))
    writer.write_batch(_payload([_frame(BOUNDARY + 1000, cam="cam1")]))
    closed = writer.close_before(BOUNDARY + 2000)
    assert [parse_file_name(p.name)[0] for p in closed] == ["cam2"]
    assert len(writer.open_paths()) == 1
    writer.close()
    assert len(list_files(tmp_path)) == 2


# --------------------------------------------------------------------------- 설정·경로


def test_config_defaults_follow_spec(tmp_path):
    c = Obs2DStoreConfig(root=tmp_path)
    assert c.retention_days == 14 and c.store_max_hz == 15.0 and c.roll_interval_ms == HOUR_MS
    for bad in (
        {"retention_days": 6},
        {"retention_days": 15},
        {"store_max_hz": 25.0},
        {"store_max_hz": 4.0},
        {"roll_interval_ms": 7 * 60_000},
        {"dir_template": "../{camera_id}"},
        {"dir_template": "/abs/{camera_id}"},
        {"dir_template": "{date}/{oops}"},
    ):
        with pytest.raises(ValueError):
            Obs2DStoreConfig(root=tmp_path, **bad)


def test_root_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv(ROOT_ENV, str(tmp_path))
    assert Obs2DStoreConfig.from_env().root == tmp_path
    monkeypatch.delenv(ROOT_ENV)
    with pytest.raises(ValueError, match=ROOT_ENV):
        Obs2DStoreConfig.from_env()


def test_custom_template_and_roll_interval(tmp_path):
    c = Obs2DStoreConfig(root=tmp_path, dir_template="{session_id}/{date}", roll_interval_ms=10 * 60_000)
    t = BOUNDARY + 25 * 60_000 + 123
    assert c.window_start(t) == BOUNDARY + 20 * 60_000
    path = c.file_path("cam1", c.window_start(t), SESSION, 3)
    assert path.relative_to(tmp_path).as_posix() == f"{SESSION}/2025-10-13/obs2d.cam1.20251013T0820Z.{SESSION}.0003.parquet"


def test_file_name_round_trip():
    name = file_name("cam_2-a", BOUNDARY, "s-1_x", 12)
    assert parse_file_name(name) == ("cam_2-a", BOUNDARY, "s-1_x", 12, False)
    assert parse_file_name(name + layout.TEMP_SUFFIX) == ("cam_2-a", BOUNDARY, "s-1_x", 12, True)
    for other in ("notes.txt", "obs2d.cam1.parquet", "obs2d.cam1.20251013T0800.s.0000.parquet"):
        assert parse_file_name(other) is None


# --------------------------------------------------------------------------- 보관 기간 정리


def _touch(path: Path, size: int = 10) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def aged_store(tmp_path):
    """now 기준 나이가 다른 파일들. 보관 14일."""
    c = Obs2DStoreConfig(root=tmp_path)
    now = BOUNDARY + 30 * DAY_MS + 17 * 60_000
    cutoff = now - 14 * DAY_MS

    def at(start, part=0, session=SESSION, cam="cam1"):
        return c.file_path(cam, start - start % HOUR_MS, session, part)

    expired_start = cutoff - HOUR_MS - (cutoff % HOUR_MS)  # 구간 끝이 cutoff 이전
    files = {
        "old": _touch(at(cutoff - 3 * DAY_MS)),
        "edge": _touch(at(expired_start)),
        "old_temp": _touch(at(expired_start, part=1).with_name(at(expired_start, part=1).name + layout.TEMP_SUFFIX)),
        "straddle": _touch(at(cutoff)),  # 구간 일부가 보관 기간 안: 남긴다
        "recent": _touch(at(now - HOUR_MS, cam="cam2")),
        "foreign": _touch(at(cutoff - 3 * DAY_MS).parent / "notes.txt"),
    }
    return tmp_path, now, files


def test_purge_dry_run_lists_without_deleting(aged_store):
    root, now, files = aged_store
    report = purge_expired(root, now_ms=now)
    assert report.dry_run is True
    assert {f.path for f in report.files} == {files["old"], files["edge"], files["old_temp"]}
    assert report.total_bytes == 30
    assert all(p.exists() for p in files.values())
    assert report.to_dict()["file_count"] == 3


def test_purge_apply_deletes_expired_only(aged_store):
    root, now, files = aged_store
    report = purge_expired(root, now_ms=now, dry_run=False)
    assert not report.dry_run
    for key in ("old", "edge", "old_temp"):
        assert not files[key].exists()
    for key in ("straddle", "recent", "foreign"):
        assert files[key].exists()
    assert files["edge"].parent in report.removed_dirs  # 비게 된 디렉터리는 지운다
    assert files["foreign"].parent.exists() and root.exists()
    assert plan_expired(root, now_ms=now) == []


def test_retention_7_days_selects_more(aged_store):
    root, now, files = aged_store
    assert files["straddle"] in {f.path for f in plan_expired(root, now_ms=now, retention_days=7)}
    with pytest.raises(ValueError):
        plan_expired(root, now_ms=now, retention_days=30)


def test_session_deletion_for_participant_request(tmp_path):
    """참여자 삭제 요청(18.4): 세션으로 골라 지운다."""
    c = Obs2DStoreConfig(root=tmp_path)
    mine = _touch(c.file_path("cam1", BOUNDARY, "s-a", 0))
    other = _touch(c.file_path("cam1", BOUNDARY, "s-b", 0))
    targets = list_files(tmp_path, session_ids=["s-a"], include_in_progress=True)
    assert delete_files(tmp_path, targets).files == tuple(targets) and mine.exists()
    delete_files(tmp_path, targets, dry_run=False)
    assert not mine.exists() and other.exists()


def test_delete_refuses_files_outside_root(tmp_path):
    c = Obs2DStoreConfig(root=tmp_path / "store")
    outside = _touch(Obs2DStoreConfig(root=tmp_path / "elsewhere").file_path("cam1", BOUNDARY, SESSION, 0))
    (f,) = list_files(tmp_path / "elsewhere")
    with pytest.raises(ValueError):
        delete_files(c.root, [f], dry_run=False)
    assert outside.exists()


def test_purge_cli(aged_store, capsys):
    root, now, files = aged_store
    assert layout.main(["--root", str(root), "--now-ms", str(now)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True and report["file_count"] == 3 and files["old"].exists()
    assert layout.main(["--root", str(root), "--now-ms", str(now), "--apply"]) == 0
    assert not files["old"].exists() and files["recent"].exists()


# --------------------------------------------------------------------------- pyarrow 없는 환경


def test_other_modules_import_without_pyarrow():
    code = """
import sys
sys.modules["pyarrow"] = None
import simai_ai.io, simai_ai.io.obs2d_layout, simai_ai.tracking.local_tracker
try:
    import simai_ai.io.obs2d_parquet
except ImportError as e:
    assert "extra io" in str(e), e
    print("ok")
"""
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=AI_DIR, capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONUTF8="1"),
    )
    assert result.returncode == 0 and result.stdout.strip() == "ok", result.stderr
