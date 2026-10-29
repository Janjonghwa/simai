"""합성 입력·가짜 HMR/EC2. 제품 성능 시험이 아니라 지연 격리·계약·재접속 시험이다."""

import copy
import json
import sys
from dataclasses import replace
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.avatar.transport import AvatarSender
from simai_ai.gpu.admission import AdmissionGate, AdmissionPolicy
from simai_ai.hmr.rig import bake_person_rig_from_model_params
from simai_ai.hmr.stream import AvatarLoop
from simai_ai.hmr.runtime import FastBodyModel
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
    clock[0] += 501
    loop.step()
    assert model.calls == 0
    source.item = snapshot(clock[0])
    for ref in source.item[0]["frames"].values():
        ref["frame_ref"]["frame_sequence"] = 999
    loop.step()
    assert model.calls == 0


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
        calls.append(kwargs)
        return [p]
    adapter = FastBodyModel.__new__(FastBodyModel)
    adapter.rig = rig
    adapter.estimator = SimpleNamespace(process_one_image=process, model=SimpleNamespace(head_pose=
        SimpleNamespace(scale_mean=np.zeros(68), scale_comps=np.zeros((28, 68)))))
    track = {"world_id": "w-1", "identity_revision": 2}
    candidate, (shape, params) = adapter.infer(np.zeros((1080, 1920, 3), np.uint8), {"bbox": [100., 100., 500., 800.]}, track, camera)
    assert calls[0]["inference_type"] == "body"
    np.testing.assert_array_equal(calls[0]["cam_int"][0], camera.K)
    assert params.shape == (204,) and list(params[:3]) == [0., 0., 0.]
    assert candidate.prompt_world_id == "w-1" and candidate.prompt_identity_revision == 2
    assert candidate.joint_rotations[:2] == ([0., 0., 0.], [0., 0., 0.])
    np.testing.assert_array_equal(candidate.keypoints_cam[0], [2., 3., 4.])
