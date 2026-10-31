"""합성 입력·가짜 HMR/EC2. 제품 성능 시험이 아니라 지연 격리·계약·재접속 시험이다."""

import copy
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.avatar.transport import AvatarSender
from simai_ai.gpu.admission import DEFAULT_LAG_BUDGET_MS, AdmissionGate, AdmissionPolicy, LoadWindow, marker_section
from simai_ai.hmr.rig import bake_person_rig_from_model_params
from simai_ai.hmr.stream import (
    DEFAULT_MAX_AGE_MS,
    HMR_STREAM_SLACK_MS,
    TIER0_LAG_BUDGET_MS,
    AvatarLoop,
)
from simai_ai.hmr.runtime import FastBodyModel
from simai_ai.io.avatar_channel import LatestChannelReader, LatestChannelWriter
from simai_ai.providers.base import Frame
from tests.contracts.test_avatar_stream import _schema_errors, violations
from tests.hmr.multiview_scene import PROVENANCE, T0, Person, hmr_candidate, make_rig, track_doc


class Socket:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, raw):
        if self.fail:
            raise OSError("切断")
        self.messages.append(json.loads(raw))

    def close(self):
        pass

    def recv(self, timeout):
        raise TimeoutError


def snapshot(t=T0, revision=0, uncertain=False):
    rig = make_rig()
    p = Person("w-1", (3., 2.5), 0.3, identity_revision=revision)
    doc = track_doc([p], rig, t=t)
    doc["input_provenance"] = copy.deepcopy(PROVENANCE)
    if uncertain:
        doc["tracks"][0]["identity_confidence"] = "IDENTITY_UNCERTAIN"
    cal = {"calibration_version": rig.calibration_version, "coordinate_frame": doc["coordinate_frame"],
           "provider_source": "SIM", "state": "PROVISIONAL", "extrinsic_convention": "OPENCV_WORLD_TO_CAMERA",
           "cameras": [{"camera_id": cid, "K": c.K.tolist(), "R": c.R.tolist(), "t": c.t.tolist(),
                        "image_size": list(c.image_size), "distortion": {"model": c.distortion_model, "coeffs": list(c.distortion_coeffs)}}
                       for cid, c in rig.cameras.items()]}
    refs = {cid: {"frame_ref": {"stream_epoch": 0, "frame_sequence": 1}, "capture_time_ms": t} for cid in rig.cameras}
    return {"world": doc, "calibration": cal, "frames": refs}, {cid: np.zeros((2, 2, 3), np.uint8) for cid in rig.cameras}


class Model:
    model_version = "fake-body"

    def __init__(self, rig):
        self.rig, self.calls = rig, 0

    def infer(self, image, obs, track, cam):
        self.calls += 1
        person = Person(track["world_id"], (3., 2.5), 0.3, identity_revision=track["identity_revision"])
        c = hmr_candidate(person, cam, prompt_world_id=person.world_id, prompt_identity_revision=person.identity_revision)
        return replace(c, joint_rotations=tuple([0., 0., 0.] for _ in range(self.rig.joint_count))), 1

    def bake(self, samples):
        return bake_person_rig_from_model_params(self.rig, np.zeros(len(self.rig.shape_vectors_cm)), np.zeros(204))


def harness(rig, freeze=2):
    clock = [T0]
    source = SimpleNamespace(item=snapshot())
    source.latest = lambda: source.item
    sock = Socket()
    sender = AvatarSender(lambda: sock, clock_ms=lambda: clock[0], monotonic=lambda: clock[0] / 1000)
    sender.tick()
    gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=True))
    uploads = []
    def upload(wid, epoch, version, glb):
        uploads.append(glb)
        return f"s3://avatars/{wid}/e{epoch}/v{version}/mesh.glb"
    model = Model(rig)
    loop = AvatarLoop(source, gate, model, upload, sender, clock_ms=lambda: clock[0], freeze_frames=freeze)
    return clock, source, sock, sender, model, loop, uploads


def test_freeze_upload_then_schema_valid_poses_without_mutating_tier0(rig):
    clock, source, sock, sender, model, loop, uploads = harness(rig)
    before = copy.deepcopy(source.item[0])
    loop.step()
    assert not uploads
    assert source.item[0] == before
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert len(uploads) == 1 and model.calls == 8
    asset, batch = sock.messages
    assert asset["msg_type"] == "avatar_asset" and batch["msg_type"] == "avatar_pose_batch"
    assert _schema_errors("avatar_asset", asset) == []
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert violations(batch=batch, assets=[asset]) == []
    assert batch["payload"]["poses"][0]["capture_time_ms"] == clock[0]
    for i in range(4):
        clock[0] += 200
        source.item = snapshot(clock[0])
        loop.step()
    assert len(uploads) == 1  # 매 프레임 체형·자산 재생성 금지


@pytest.mark.parametrize("load", [None, {"samples": 0, "unknown": 0, "lag_ms_max": None},
    {"samples": 1, "unknown": 0, "lag_ms_max": 601}, {"samples": 1, "unknown": 1, "lag_ms_max": 1},
    {"samples": 1, "unknown": 0, "lag_ms_max": 1, "queue_wait_exceeded": 1}])
def test_marker_unknown_or_over_budget_never_infers(tmp_path, rig, load):
    clock, source, sock, sender, model, loop, _ = harness(rig)
    marker = tmp_path / "tier0.live"
    if load is not None:
        marker.write_text(json.dumps({"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "load": load}}))
    loop.gate = AdmissionGate(marker, AdmissionPolicy(resume_after_s=0), wall_s=lambda: marker.stat().st_mtime,
                              monotonic=lambda: 0)
    loop.step()
    assert model.calls == 0 and loop.skipped == 1 and not sock.messages


def test_gate_checked_before_each_person_camera_call(rig):
    *_, model, loop, uploads = harness(rig)
    checks = iter([True, True, False])
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=next(checks)))
    loop.step()
    assert model.calls == 1 and not uploads


def test_identity_change_freezes_again_and_does_not_inherit_other_id(rig):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=2)
    for _ in range(2):
        source.item = snapshot(clock[0])
        loop.step()
        clock[0] += 200
    assert len(uploads) == 1
    source.item = snapshot(clock[0], revision=1, uncertain=True)
    loop.step()
    assert len(uploads) == 1
    for _ in range(2):
        clock[0] += 200
        source.item = snapshot(clock[0], revision=1)
        loop.step()
    assert len(uploads) == 2 and loop.people["w-1"].version == 1


def test_stale_frame_and_mismatched_frame_reference_are_not_inferred(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig)
    clock[0] += DEFAULT_MAX_AGE_MS + 1
    loop.step()
    assert model.calls == 0
    source.item = snapshot(clock[0])
    for ref in source.item[0]["frames"].values():
        ref["frame_ref"]["frame_sequence"] = 999
    loop.step()
    assert model.calls == 0


@pytest.mark.parametrize(
    "age_ms, expected_admitted",
    [
        (-1, False),
        (0, True),
        (500, True),
        (550, True),
        (TIER0_LAG_BUDGET_MS, True),
        (DEFAULT_MAX_AGE_MS, True),
        (DEFAULT_MAX_AGE_MS + 1, False),
    ],
)
def test_freshness_max_age_ms_boundaries_and_tier0_lag_budget_alignment(rig, age_ms, expected_admitted):
    assert TIER0_LAG_BUDGET_MS == DEFAULT_LAG_BUDGET_MS == 600
    assert HMR_STREAM_SLACK_MS == 500
    assert DEFAULT_MAX_AGE_MS == 1100
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    assert loop.max_age_ms == DEFAULT_MAX_AGE_MS
    assert sender.max_age_ms == DEFAULT_MAX_AGE_MS
    capture_t = T0 + 1_000
    source.item = snapshot(capture_t)
    clock[0] = capture_t + age_ms
    loop.step()
    sender.tick()
    if expected_admitted:
        assert model.calls == 4 and len(uploads) == 1
        batches = [m for m in sock.messages if m["msg_type"] == "avatar_pose_batch"]
        assert len(batches) == 1
        assert batches[0]["payload"]["poses"][0]["capture_time_ms"] == capture_t
    else:
        assert model.calls == 0 and not uploads and not sock.messages


def test_avatar_loop_with_session9_marker_pattern_infers_and_blocks_on_consecutive_or_unknown(tmp_path, rig):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=2)
    marker = tmp_path / "tier0.live"
    loop.gate = AdmissionGate(
        marker,
        AdmissionPolicy(),
        wall_s=lambda: clock[0] / 1000.0,
        monotonic=lambda: clock[0] / 1000.0,
    )

    def write_marker(lags, *, unknown=0):
        w = LoadWindow(DEFAULT_LAG_BUDGET_MS)
        for v in lags:
            w.add(v)
        for _ in range(unknown):
            w.add(None)
        doc = {
            "started": True,
            "gpu_priority": marker_section(
                phase="running",
                reserve={"state": "OK"},
                load=w.take(),
                written_at_ms=clock[0],
                pid=1,
            ),
        }
        marker.write_text(json.dumps(doc), encoding="utf-8")
        ts = clock[0] / 1000.0
        os.utime(marker, (ts, ts))

    # 0~30s 안정 대기 중 26s 에 672ms 1회 단발 초과가 나와도 리셋되지 않고 30s 에 열린다.
    write_marker((320.0, 410.0, 480.0, 520.0, 540.0))
    loop.step()
    assert model.calls == 0
    for sec in range(1, 31):
        clock[0] = T0 + sec * 1_000
        if sec == 26:
            write_marker((350.0, 420.0, 490.0, 530.0, 672.0))
        elif sec % 5 == 0:
            write_marker((330.0, 400.0, 470.0, 515.0, 545.0))
        # 촬영 시각이 현재 시각보다 520ms 전(9차 평시 지연 300~550ms 범위)인 스냅샷 입력
        source.item = snapshot(clock[0] - 520)
        loop.step()
        if sec < 30:
            assert model.calls == 0

    # 30s 시점에 첫 추론이 수행되었고, 31s 에 단발 672ms 표본이 있어도 2번째 추론·자산 업로드·포즈 송신이 완료된다.
    assert model.calls == 4 and not uploads
    clock[0] = T0 + 31_000
    write_marker((360.0, 430.0, 500.0, 535.0, 672.0))
    source.item = snapshot(clock[0] - 540)
    loop.step()
    sender.tick()
    assert model.calls == 8 and len(uploads) == 1
    assert any(m["msg_type"] == "avatar_pose_batch" for m in sock.messages)

    # 다음 표시 파일 창에서도 연속으로 666ms 초과가 발생하면 즉시 차단된다.
    clock[0] = T0 + 36_000
    write_marker((380.0, 440.0, 510.0, 540.0, 666.0))
    source.item = snapshot(clock[0] - 400)
    loop.step()
    assert model.calls == 8

def test_channel_reader_skips_image_loads_on_duplicate_or_stale_snapshots(tmp_path, rig):
    clock, _, sock, sender, model, loop, _ = harness(rig)
    path = tmp_path / "avatar-input.npz"
    writer = LatestChannelWriter(path)
    reader = LatestChannelReader(path)
    loop.source = reader
    meta, imgs = snapshot(clock[0])
    frames = {cid: Frame(cid, clock[0], "FILE", meta["frames"][cid]["frame_ref"], image=img)
              for cid, img in imgs.items()}
    try:
        writer._write({"world": meta["world"], "calibration": meta["calibration"]}, frames)
        loop.step()
        assert model.calls == 4
        # 중복 시각(t == last_t)과 신선도 초과(clock - t > DEFAULT_MAX_AGE_MS)에서는 predicate 가 거짓이라 영상을 읽지 않는다.
        calls = []
        orig_latest = reader.latest
        def spy_latest(*, predicate=None):
            res = orig_latest(predicate=predicate)
            calls.append(res[1] if res is not None else None)
            return res
        reader.latest = spy_latest
        loop.step()
        assert calls[-1] == {} and model.calls == 4
        clock[0] += DEFAULT_MAX_AGE_MS + 100
        stale = DEFAULT_MAX_AGE_MS + 1
        meta2, imgs2 = snapshot(clock[0] - stale)
        frames2 = {cid: Frame(cid, clock[0] - stale, "FILE", meta2["frames"][cid]["frame_ref"], image=img)
                   for cid, img in imgs2.items()}
        writer._write({"world": meta2["world"], "calibration": meta2["calibration"]}, frames2)
        loop.step()
        assert calls[-1] == {} and model.calls == 4
    finally:
        writer.close()


def test_upload_retry_reuses_identical_bytes_for_same_asset_version(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    attempts = []
    def upload(wid, epoch, version, glb):
        attempts.append((version, glb))
        if len(attempts) == 1:
            raise OSError("업로드 응답 유실")
        return f"s3://avatars/{wid}/e{epoch}/v{version}/mesh.glb"
    loop.upload = upload
    with pytest.raises(OSError):
        loop.step()
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert len(attempts) == 2 and attempts[0][0] == attempts[1][0] == 0
    assert attempts[0][1] is attempts[1][1]
    assert loop.people["w-1"].asset is not None


def test_optional_appearance_is_gate_controlled_and_failure_keeps_confirmed_poses(rig, caplog):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    calls = []
    def update(*args):
        calls.append("update")
        raise OSError("temporary upload error")
    loop.appearance = SimpleNamespace(retain=lambda *args: calls.append("retain"),
        track=lambda *args: calls.append("track"), register=lambda *args: calls.append("register"),
        update=update, config=SimpleNamespace(face_policy="EXCLUDED"))
    loop.step()
    sender.tick()
    assert calls == ["retain", "track", "register", "update"]
    assert loop.people["w-1"].asset["face_policy"] == "EXCLUDED"
    assert any(message["msg_type"] == "avatar_pose_batch" for message in sock.messages)
    assert "마지막 확정 외형 유지" in caplog.text
    calls.clear()
    loop.gate = SimpleNamespace(check=lambda: SimpleNamespace(admit=False))
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert not calls


def test_rollback_reannounces_previous_asset_and_keeps_allocated_version(rig):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    loop.step()
    original = copy.deepcopy(loop.people["w-1"].asset)
    clock[0] += 200
    source.item = snapshot(clock[0], revision=1)
    loop.step()
    state = loop.people["w-1"]
    assert state.version == 1
    loop.reannounce_appearance("w-1", 0)
    assert state.asset == original and state.version == 0 and state.high_version == 1
    sender.tick()
    assert sock.messages[-1]["payload"] == original


def test_i5_live_hmr_remains_blocked_with_healthy_marker(tmp_path):
    marker = tmp_path / "tier0.live"
    marker.write_text(json.dumps({"gpu_priority": {"phase": "running", "reserve": {"state": "OK"},
                         "load": {"samples": 1, "unknown": 0, "lag_ms_max": 200}}}))
    gate = AdmissionGate(marker, AdmissionPolicy(resume_after_s=0), wall_s=lambda: marker.stat().st_mtime, monotonic=lambda: 0)
    admission = gate.check()
    assert admission.admit and admission.allowed_types(["hmr_refine", "hmr_appearance_update"]) == []


def test_sender_reconnect_discards_backlog_reannounces_assets_and_keeps_sequence():
    clock, now = [T0], [1.]
    sockets = [Socket(), Socket()]
    attempts = []
    def connect():
        attempts.append(now[0])
        return sockets[len(attempts) - 1]
    sender = AvatarSender(connect, clock_ms=lambda: clock[0], monotonic=lambda: now[0])
    sender.asset({"world_id": "w-1"})
    sender.tick()
    pose = {"world_id": "w-1", "capture_time_ms": clock[0]}
    context = {"calibration_version": "cal-1"}
    sender.poses([pose], **context)
    sender.poses([pose], **context)
    assert sender.dropped == 1 and len(sender._poses) == 1
    sender.tick()
    assert sender.sequence == 1
    sockets[0].fail = True
    sender.poses([pose], **context)
    sender.tick()
    assert sender.sequence == 2
    sender.poses([pose], **context)
    now[0] += .5
    sender.tick()
    assert len(attempts) == 1
    now[0] += .5
    sender.tick()
    assert len(attempts) == 2 and not sender._poses
    assert [m["msg_type"] for m in sockets[1].messages] == ["avatar_asset"]
    sender.poses([pose], **context)
    sender.tick()
    assert sockets[1].messages[-1]["payload"]["batch_sequence"] == 2
    assert sockets[1].messages[-1]["payload"]["stream_epoch"] == T0
    now[0] += 10
    sender.tick()
    assert sockets[1].messages[-1]["msg_type"] == "avatar_asset"


def test_sender_backoff_doubles_to_ten_seconds():
    now, attempts = [0.], []
    def fail():
        attempts.append(now[0])
        raise OSError()
    sender = AvatarSender(fail, clock_ms=lambda: T0, monotonic=lambda: now[0])
    for second in range(50):
        now[0] = float(second)
        sender.tick()
    assert attempts[:7] == [0., 1., 3., 7., 15., 25., 35.]


@pytest.mark.parametrize("native_params", [True, False])
def test_body_adapter_uses_calibration_and_drops_translation(monkeypatch, rig, native_params):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(inference_mode=nullcontext,
                        as_tensor=lambda a, dtype: np.asarray(a, dtype=dtype), float32=np.float32))
    monkeypatch.setitem(sys.modules, "cv2", SimpleNamespace(COLOR_BGR2RGB=1, cvtColor=lambda img, _: img[..., ::-1]))
    camera = make_rig(distorted=False).cameras["cam_A"]
    p = {"pred_keypoints_3d": np.zeros((70, 3)), "pred_cam_t": np.array([2., 3., 4.]),
         "shape_params": np.zeros(len(rig.shape_vectors_cm)), "global_rot": np.zeros(3),
         "body_pose_params": np.zeros(130), "scale_params": np.zeros(28)}
    if native_params:
        p["mhr_model_params"] = np.r_[np.ones(3), np.zeros(201)]
    calls = []
    def process(image, **kwargs):
        np.testing.assert_array_equal(image[0, 0], [30, 20, 10])
        calls.append(kwargs)
        return [p]
    adapter = FastBodyModel.__new__(FastBodyModel)
    adapter.rig = rig
    adapter.estimator = SimpleNamespace(process_one_image=process, model=SimpleNamespace(head_pose=
        SimpleNamespace(scale_mean=np.zeros(68), scale_comps=np.zeros((28, 68)))))
    track = {"world_id": "w-1", "identity_revision": 2}
    image = np.full((1080, 1920, 3), [10, 20, 30], np.uint8)
    candidate, (shape, params) = adapter.infer(image, {"bbox": [100., 100., 500., 800.]}, track, camera)
    np.testing.assert_array_equal(image[0, 0], [10, 20, 30])
    assert calls[0]["inference_type"] == "body"
    np.testing.assert_array_equal(calls[0]["cam_int"][0], camera.K)
    assert params.shape == (204,) and list(params[:3]) == [0., 0., 0.]
    assert candidate.prompt_world_id == "w-1" and candidate.prompt_identity_revision == 2
    assert candidate.joint_rotations[:2] == ([0., 0., 0.], [0., 0., 0.])
    np.testing.assert_array_equal(candidate.keypoints_cam[0], [2., 3., 4.])
