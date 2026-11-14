"""합성 입력·가짜 HMR/EC2. 제품 성능 시험이 아니라 지연 격리·계약·재접속 시험이다."""

import copy
import json
import os
import sys
import threading
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
    return {"world": doc, "calibration": cal, "frames": refs, "produced_at_ms": t}, {
        cid: np.zeros((2, 2, 3), np.uint8) for cid in rig.cameras}


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
    assert len(uploads) == 1 and model.calls == 2
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
    {"samples": 1, "unknown": 0, "lag_ms_max": 1, "queue_wait_exceeded": 3}])
def test_marker_unknown_or_over_budget_never_infers(tmp_path, rig, load):
    clock, source, sock, sender, model, loop, _ = harness(rig)
    marker = tmp_path / "tier0.live"
    if load is not None:
        marker.write_text(json.dumps({"gpu_priority": {"phase": "running", "reserve": {"state": "OK"}, "load": load}}))
    loop.gate = AdmissionGate(marker, AdmissionPolicy(resume_after_s=0), wall_s=lambda: marker.stat().st_mtime,
                              monotonic=lambda: 0)
    loop.step()
    assert model.calls == 0 and loop.skipped == 1 and not sock.messages
    assert sum(loop.skip_reasons.values()) == 1 and "unknown" not in loop.skip_reasons


def test_gate_checked_before_each_person_camera_call(rig):
    clock, source, *_, model, loop, uploads = harness(rig)
    meta, imgs = snapshot(clock[0])
    t0 = meta["world"]["tracks"][0]
    meta["world"]["tracks"] = [t0, {**copy.deepcopy(t0), "world_id": "w-2"}]
    source.item = meta, imgs
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


def merged_snapshot(t, revision=1, aliases=("w-9",), uncertain=False, split_child=False):
    item = snapshot(t, revision=revision, uncertain=uncertain)
    tracks = item[0]["world"]["tracks"]
    tracks[0]["aliases"] = list(aliases)
    if split_child:   # 같은 차수로 w-1 에서 갈라져 나간 Track(병합과 분할이 겹침)
        tracks.append({**copy.deepcopy(tracks[0]), "world_id": "w-5", "split_from": "w-1", "aliases": [],
                       "per_camera": []})
    return item


def test_pure_merge_keeps_samples_and_asset_without_rebake(rig):
    """S15P31A101-661: 순수 병합이면 남는 ID 의 자산을 그대로 쓰고 다시 굽지 않는다. 보낸 자산은 바꾸지 않는다."""
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=2)
    for _ in range(2):
        source.item = snapshot(clock[0])
        loop.step()
        clock[0] += 200
    state = loop.people["w-1"]
    assert len(uploads) == 1 and state.version == 0 and state.shape_frozen
    sent_asset = copy.deepcopy(state.asset)
    source.item = merged_snapshot(clock[0], uncertain=True)   # 병합 직후 LATE_REASSOCIATION 불확실
    loop.step()
    assert state.stable == 0 and len(state.samples) == 0 and state.revision == 1 and state.revision_base == 0
    for _ in range(3):
        clock[0] += 200
        source.item = merged_snapshot(clock[0])
        loop.step()
    sender.tick()
    assert len(uploads) == 1 and state.version == 0 and state.asset == sent_asset
    poses = [p for m in sock.messages if m["msg_type"] == "avatar_pose_batch" for p in m["payload"]["poses"]]
    assert poses[-1]["identity_revision"] == 1 and poses[-1]["appearance_version"] == 0
    assets = [m["payload"] for m in sock.messages if m["msg_type"] == "avatar_asset"]
    assert [a["appearance_version"] for a in assets] == [0] and assets[0]["identity_revision"] == 0
    assert loop.identity_events == {"merge_carried": 1, "merge_asset_kept": 1}


def test_merge_overlapping_split_or_revision_without_alias_rebakes(rig):
    for item_of in (lambda t: merged_snapshot(t, split_child=True), lambda t: merged_snapshot(t, aliases=())):
        clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=2)
        for _ in range(2):
            source.item = snapshot(clock[0])
            loop.step()
            clock[0] += 200
        assert len(uploads) == 1
        source.item = item_of(clock[0])
        loop.step()
        assert loop.people["w-1"].revision_base == 1 and loop.identity_events == {"revision_reset": 1}
        clock[0] += 200
        source.item = item_of(clock[0])
        loop.step()
        assert len(uploads) == 2 and loop.people["w-1"].asset["identity_revision"] == 1


def test_pure_merge_drops_absorbed_person_state(rig):
    clock, source, *_, loop, _ = harness(rig, freeze=3)
    meta, imgs = snapshot(clock[0])
    t0 = meta["world"]["tracks"][0]
    meta["world"]["tracks"] = [t0, {**copy.deepcopy(t0), "world_id": "w-2", "per_camera": []}]
    source.item = meta, imgs
    loop.step()
    assert set(loop.people) == {"w-1", "w-2"}
    clock[0] += 200
    source.item = merged_snapshot(clock[0], aliases=("w-2",))
    loop.step()
    assert set(loop.people) == {"w-1"} and len(loop.people["w-1"].samples) == 2


def test_uncertain_frame_preserves_samples_but_revision_clears_them(rig):
    clock, source, _, _, _, loop, _ = harness(rig, freeze=3)
    loop.step()
    state = loop.people["w-1"]
    assert len(state.samples) == 1 and state.stable == 1
    clock[0] += 200
    source.item = snapshot(clock[0], uncertain=True)
    loop.step()
    # 같은 identity_revision에서 IDENTITY_UNCERTAIN·avatar_update_hold 구간은 stable을 0으로 초기화하지 않고 일시정지한다.
    assert len(state.samples) == 1 and state.stable == 1
    clock[0] += 200
    held = snapshot(clock[0])
    held[0]["world"]["tracks"][0]["avatar_update_hold"] = True
    source.item = held
    loop.step()
    assert len(state.samples) == 1 and state.stable == 1
    clock[0] += 200
    source.item = snapshot(clock[0], revision=1, uncertain=True)
    loop.step()
    assert state.samples == [] and state.stable == 0


def test_stale_frame_and_mismatched_frame_reference_are_not_inferred(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig)
    clock[0] += DEFAULT_MAX_AGE_MS + 1
    loop.step()
    assert model.calls == 0 and loop.stale_inputs == 1
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
        (1000, True),
        (1050, False),
        (DEFAULT_MAX_AGE_MS, True),
        (DEFAULT_MAX_AGE_MS + 1, False),
    ],
)
def test_freshness_since_tier0_produced_boundaries(rig, age_ms, expected_admitted):
    assert DEFAULT_MAX_AGE_MS == 1000
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    assert loop.max_age_ms == DEFAULT_MAX_AGE_MS
    assert sender.max_age_ms == DEFAULT_MAX_AGE_MS
    capture_t = T0 + 1_000
    source.item = snapshot(capture_t)
    clock[0] = capture_t + age_ms
    loop.step()
    sender.tick()
    if expected_admitted:
        assert model.calls == 1 and len(uploads) == 1
        batches = [m for m in sock.messages if m["msg_type"] == "avatar_pose_batch"]
        assert len(batches) == 1
        assert batches[0]["payload"]["poses"][0]["capture_time_ms"] == capture_t
    else:
        assert model.calls == 0 and not uploads and not sock.messages
        assert loop.stale_inputs == 1


def test_old_capture_fresh_tier0_output_is_sent_with_original_capture_time(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    source.item[0]["produced_at_ms"] = clock[0] + 5000
    clock[0] += 5000
    loop.step()
    sender.tick()
    assert model.calls == 1 and sender.sent_batches_total == 1
    assert sock.messages[-1]["payload"]["poses"][0]["capture_time_ms"] == T0


@pytest.mark.parametrize("produced", [None, True, "now"])
def test_unknown_production_time_is_rejected(rig, produced):
    _, source, _, _, model, loop, _ = harness(rig)
    source.item[0]["produced_at_ms"] = produced
    loop.step()
    assert model.calls == 0 and loop.stale_inputs == 1


def test_sender_rejects_pose_when_tier0_output_ages_in_send_queue():
    clock = [T0]
    sock = Socket()
    sender = AvatarSender(lambda: sock, clock_ms=lambda: clock[0], monotonic=lambda: 0)
    sender.tick()
    sender.poses([{"world_id": "w-1", "capture_time_ms": T0 - 5000}], produced_at_ms=T0)
    clock[0] += DEFAULT_MAX_AGE_MS + 1
    sender.tick()
    assert sender.sent_batches_total == 0


def test_avatar_loop_session10_rules_tolerate_queue_wait_close_on_lag_and_resume_after_10s(tmp_path, rig, caplog):
    """S15P31A101-412: 큐 대기 초과 1~2건 창은 추론을 이어가고, 지연 초과 창은 바로 멈추며, 통과 10초 뒤 다시 추론한다."""
    caplog.set_level("INFO")
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=2)
    marker = tmp_path / "tier0.live"
    loop.gate = AdmissionGate(
        marker,
        AdmissionPolicy(),
        wall_s=lambda: clock[0] / 1000.0,
        monotonic=lambda: clock[0] / 1000.0,
    )

    def write_marker(lags, *, qwe=None):
        w = LoadWindow(DEFAULT_LAG_BUDGET_MS)
        for v in lags:
            w.add(v)
        if qwe is not None:
            w.add(lags[-1], queue_wait_exceeded=qwe)
        doc = {
            "started": True,
            "gpu_priority": marker_section(phase="running", reserve={"state": "OK"}, load=w.take(),
                                           written_at_ms=clock[0], pid=1),
        }
        marker.write_text(json.dumps(doc), encoding="utf-8")
        ts = clock[0] / 1000.0
        os.utime(marker, (ts, ts))

    def at(sec, age_ms=300):
        clock[0] = T0 + int(sec * 1_000)
        source.item = snapshot(clock[0] - age_ms)
        loop.step()

    # 0~10s 안정 대기(10차 평시 266~301ms). 5s 창의 큐 대기 초과 2건은 안정 시계를 지우지 않는다.
    write_marker((266.0, 301.0))
    at(0)
    clock[0] = T0 + 5_000
    write_marker((270.0, 290.0), qwe=2)
    at(5)
    assert model.calls == 0
    clock[0] = T0 + 10_000
    write_marker((250.0, 280.0))
    at(10)
    assert model.calls == 1

    # 열린 뒤 큐 대기 초과 1건 창: 계속 추론하고 자산·포즈까지 보낸다.
    clock[0] = T0 + 15_000
    write_marker((260.0, 300.0), qwe=1)
    at(15)
    sender.tick()
    assert model.calls == 2 and len(uploads) == 1
    assert any(m["msg_type"] == "avatar_pose_batch" for m in sock.messages)
    assert loop.pose_enqueued == 1 and loop.uploads == 1 and loop.assets == 1 and sender.sent_batches_total == 1

    # 지연 초과(단발 672ms) 창은 바로 멈춘다.
    clock[0] = T0 + 20_000
    write_marker((300.0, 672.0))
    at(20)
    assert model.calls == 2 and loop.skip_reasons["over_budget"] == 1

    # 통과 창이 다시 오면 10초 안정 뒤 추론을 재개한다(9초 시점은 아직 대기).
    clock[0] = T0 + 25_000
    write_marker((260.0, 290.0))
    at(25)
    at(30)  # 같은 표시 파일이 5초째: 아직 안정 대기
    clock[0] = T0 + 30_000
    write_marker((255.0, 285.0))
    at(34)
    assert model.calls == 2 and loop.skip_reasons["stabilizing"] >= 3
    at(35)
    assert model.calls == 3

    stats = loop.gate.stats()
    assert stats["resets"] == {"over_budget": 1} and stats["queue_wait_tolerated_windows"] == 2
    # 60초 요약 로그에 사유별 차단·실제 송신·업로드 수가 들어간다.
    clock[0] = T0 + 120_000
    loop._report("요약")
    line = [r.getMessage() for r in caplog.records if "아바타 입력 상태: 요약" in r.getMessage()][-1]
    assert "skip_reasons={" in line and "'over_budget': 1" in line and "pose_sent=1" in line and "uploads=1" in line
    assert "gate_resets={'over_budget': 1}" in line


def test_channel_reader_skips_image_loads_on_duplicate_or_stale_snapshots(tmp_path, rig):
    clock, _, sock, sender, model, loop, _ = harness(rig)
    path = tmp_path / "avatar-input.npz"
    writer = LatestChannelWriter(path, clock_ms=lambda: clock[0])
    reader = LatestChannelReader(path)
    loop.source = reader
    meta, imgs = snapshot(clock[0])
    frames = {cid: Frame(cid, clock[0], "FILE", meta["frames"][cid]["frame_ref"], image=img)
              for cid, img in imgs.items()}
    try:
        writer._write({"world": meta["world"], "calibration": meta["calibration"], "produced_at_ms": clock[0]}, frames)
        loop.step()
        assert model.calls == 1
        # 중복 시각(t == last_t)과 신선도 초과(clock - t > DEFAULT_MAX_AGE_MS)에서는 predicate 가 거짓이라 영상을 읽지 않는다.
        calls = []
        orig_latest = reader.latest
        def spy_latest(*, predicate=None):
            res = orig_latest(predicate=predicate)
            calls.append(res[1] if res is not None else None)
            return res
        reader.latest = spy_latest
        loop.step()
        assert calls[-1] == {} and model.calls == 1 and loop.duplicate_inputs == 1 and loop.stale_inputs == 0
        clock[0] += DEFAULT_MAX_AGE_MS + 100
        stale = DEFAULT_MAX_AGE_MS + 1
        meta2, imgs2 = snapshot(clock[0] - stale)
        frames2 = {cid: Frame(cid, clock[0] - stale, "FILE", meta2["frames"][cid]["frame_ref"], image=img)
                   for cid, img in imgs2.items()}
        writer._write({"world": meta2["world"], "calibration": meta2["calibration"],
                       "produced_at_ms": clock[0] - stale}, frames2)
        loop.step()
        assert calls[-1] == {} and model.calls == 1 and loop.duplicate_inputs == 1 and loop.stale_inputs == 1
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


def test_upload_409_conflict_advances_pending_version_without_rebake_and_validates_contract(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    attempts = []
    bake_calls = [0]
    orig_bake = model.bake

    def spy_bake(samples):
        bake_calls[0] += 1
        return orig_bake(samples)

    model.bake = spy_bake

    def upload(wid, epoch, version, glb):
        attempts.append((version, glb))
        if version == 0:
            raise ValueError("아바타 업로드 서명 실패 HTTP=409")
        return f"s3://avatars/{wid}/e{epoch}/v{version}/mesh.glb"

    loop.upload = upload
    with pytest.raises(ValueError, match="HTTP=409"):
        loop.step()
    assert bake_calls[0] == 1 and loop.people["w-1"].pending_version == 1

    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert [v for v, _ in attempts] == [0, 1]
    assert attempts[0][1] is attempts[1][1] and bake_calls[0] == 1
    asset, batch = sock.messages
    assert asset["payload"]["appearance_version"] == 1
    assert asset["payload"]["previous_appearance_version"] == 0
    assert _schema_errors("avatar_asset", asset) == []
    assert _schema_errors("avatar_pose_batch", batch) == []
    assert violations(batch=batch, assets=[asset]) == []


def test_track_lost_and_reentry_preserves_monotonic_version_in_same_epoch(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    versions = []

    def upload(wid, epoch, version, glb):
        versions.append(version)
        return f"s3://avatars/{wid}/e{epoch}/v{version}/mesh.glb"

    loop.upload = upload
    loop.step()
    sender.tick()
    assert versions == [0] and loop.people["w-1"].version == 0

    # 일시적으로 LOST 처리되어 active 에서 빠진 회차
    clock[0] += 200
    lost_meta, lost_imgs = snapshot(clock[0])
    lost_meta["world"]["tracks"][0]["track_lifecycle"] = "LOST"
    source.item = (lost_meta, lost_imgs)
    loop.step()
    assert "w-1" not in loop.people

    # 같은 stream_epoch 안에서 같은 world_id 가 다시 ACTIVE 로 들어오면 v1 로 이어진다.
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert versions == [0, 1]
    assert loop.people["w-1"].version == 1
    assert loop.people["w-1"].asset["previous_appearance_version"] == 0
    assets = [m for m in sock.messages if m["msg_type"] == "avatar_asset"]
    batches = [m for m in sock.messages if m["msg_type"] == "avatar_pose_batch"]
    assert _schema_errors("avatar_asset", assets[-1]) == []
    assert violations(batch=batches[-1], assets=assets) == []


def test_optional_appearance_is_gate_controlled_and_failure_keeps_confirmed_poses(rig, caplog):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=1)
    calls = []
    updated = threading.Event()
    def update(*args, **kwargs):
        calls.append("update")
        updated.set()
        raise OSError("temporary upload error")
    loop.appearance = SimpleNamespace(retain=lambda *args: calls.append("retain"),
        track=lambda *args, **kwargs: calls.append("track"), register=lambda *args: calls.append("register"),
        update=update, config=SimpleNamespace(face_policy="EXCLUDED"))
    try:
        loop.step()
        assert updated.wait(1)
        loop.close()  # 진행 중 예외 처리까지 회수한다.
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
    finally:
        loop.close()


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


def test_representative_camera_selection_prefers_unoccluded_more_keypoints_larger_bbox(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    chosen_cameras = []
    orig_infer = model.infer

    def spy_infer(image, obs, track, cam):
        chosen_cameras.append(cam.camera_id)
        return orig_infer(image, obs, track, cam)

    model.infer = spy_infer
    meta, imgs = snapshot(clock[0])
    track = meta["world"]["tracks"][0]
    # cam_A: 큰 bbox지만 다른 사람(w-2)에 의해 절반 이상 가려짐
    # cam_B: 가림 없음, 관절 10개 관측, 중간 bbox
    # cam_C: 가림 없음, 관절 15개 관측, 가장 큰 bbox -> 대표 카메라로 선택되어야 함
    # cam_D: 가림 없음, 관절 5개 관측, 작은 bbox
    for pc in track["per_camera"]:
        cid = pc["camera_id"]
        if cid == "cam_A":
            pc["bbox"] = [100.0, 100.0, 500.0, 900.0]
            pc["keypoints_2d"] = [[200.0, 200.0, 0.9]] * 17
        elif cid == "cam_B":
            pc["bbox"] = [100.0, 100.0, 300.0, 600.0]
            pc["keypoints_2d"] = [[200.0, 200.0, 0.9]] * 10 + [None] * 7
        elif cid == "cam_C":
            pc["bbox"] = [100.0, 100.0, 450.0, 850.0]
            pc["keypoints_2d"] = [[200.0, 200.0, 0.9]] * 15 + [None] * 2
        elif cid == "cam_D":
            pc["bbox"] = [100.0, 100.0, 200.0, 300.0]
            pc["keypoints_2d"] = [[150.0, 150.0, 0.9]] * 5 + [None] * 12
    occluder = {
        **copy.deepcopy(track),
        "world_id": "w-2",
        "per_camera": [{
            "camera_id": "cam_A",
            "local_track_id": "occ-1",
            "bbox": [100.0, 100.0, 500.0, 900.0],
            "keypoints_2d": [None] * 17,
            "joint_layout": "COCO17",
            "estimated_capture_time": clock[0],
            "frame_ref": {"stream_epoch": 0, "frame_sequence": 1},
        }],
    }
    meta["world"]["tracks"] = [track, occluder]
    source.item = (meta, imgs)
    loop.step()
    assert chosen_cameras == ["cam_C", "cam_A"]


def test_two_stage_baking_publishes_initial_mesh_at_5_and_upgrades_at_30(rig):
    clock, source, sock, sender, model, loop, uploads = harness(rig, freeze=30)
    assert loop.initial_freeze_frames == 5 and loop.freeze_frames == 30
    for step_idx in range(1, 5):
        source.item = snapshot(clock[0])
        loop.step()
        clock[0] += 200
        assert not uploads
    # 근접(avatar_update_hold) 2프레임과 IDENTITY_UNCERTAIN 1프레임이 끼어도 stable은 리셋되지 않고 일시정지된다.
    for hold_idx in range(3):
        item = snapshot(clock[0], uncertain=(hold_idx == 2))
        if hold_idx < 2:
            item[0]["world"]["tracks"][0]["avatar_update_hold"] = True
        source.item = item
        loop.step()
        clock[0] += 200
    assert loop.people["w-1"].stable == 4 and len(loop.people["w-1"].samples) == 4 and not uploads
    # 5번째 정상 샘플: 초기 메시(version 0, frozen=False, frames_used=5) 즉시 발행 및 포즈 송신 시작
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert len(uploads) == 1
    state = loop.people["w-1"]
    assert state.version == 0 and not state.shape_frozen
    assert state.asset["shape"] == {"representation": "MHR", "frozen": False, "frames_used": 5}
    assert _schema_errors("avatar_asset", sock.messages[0]) == []
    assert any(m["msg_type"] == "avatar_pose_batch" for m in sock.messages)
    # 6~29번째 정상 샘플 동안은 추가 업로드 없이 초기 메시(version 0)로 포즈만 계속 보낸다.
    for _ in range(6, 30):
        clock[0] += 200
        source.item = snapshot(clock[0])
        loop.step()
    assert len(uploads) == 1 and state.version == 0 and len(state.samples) == 29
    # 30번째 정상 샘플: 정밀 메시(version 1, previous=0, frozen=True, frames_used=30)로 업그레이드
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    sender.tick()
    assert len(uploads) == 2 and state.version == 1 and state.shape_frozen and state.samples == []
    assert state.asset["appearance_version"] == 1
    assert state.asset["previous_appearance_version"] == 0
    assert state.asset["shape"] == {"representation": "MHR", "frozen": True, "frames_used": 30}
    asset_msgs = [m for m in sock.messages if m["msg_type"] == "avatar_asset"]
    assert _schema_errors("avatar_asset", asset_msgs[-1]) == []


def test_self_throttling_widens_interval_when_tier0_pressure_rises(rig):
    clock, source, sock, sender, model, loop, _ = harness(rig, freeze=1)
    interval = [200]
    loop.gate = SimpleNamespace(
        check=lambda: SimpleNamespace(admit=True),
        recommended_interval_ms=lambda: interval[0],
    )
    # t=T0: 첫 추론 수행
    source.item = snapshot(clock[0])
    loop.step()
    assert model.calls == 1 and loop.throttled == 0
    # 부하 상승으로 권장 주기가 600ms로 넓어지면 +200ms, +400ms 회차는 자체 감속으로 건너뛰고 +600ms에 추론한다.
    interval[0] = 600
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert model.calls == 1 and loop.throttled == 1 and loop.skip_reasons["self_throttle"] == 1
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert model.calls == 1 and loop.throttled == 2
    clock[0] += 200
    source.item = snapshot(clock[0])
    loop.step()
    assert model.calls == 2 and loop.throttled == 2
