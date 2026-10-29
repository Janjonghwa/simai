"""상시 EXP-02 루프. 상류·모델·업로드·송신·시계를 주입하며 Tier 0 값은 읽기만 한다."""

from dataclasses import dataclass, field
from typing import Protocol

from simai_ai.geometry.rig import CameraRig
from simai_ai.hmr.gltf_export import build_skinned_glb
from simai_ai.hmr.multiview import aggregate_frame
from simai_ai.hmr.pose import mhr_pose


class HmrModel(Protocol):
    model_version: str

    def infer(self, image, observation, track, camera): ...
    def bake(self, samples): ...


class SnapshotCalibration:
    def __init__(self, doc):
        self.doc = doc

    def current(self):
        return self.doc

    def provenance(self):
        return {"source": self.doc["provider_source"], "calibration_version": self.doc["calibration_version"]}


@dataclass
class PersonState:
    revision: int
    samples: list = field(default_factory=list)
    stable: int = 0
    version: int = -1
    asset: dict | None = None
    pending_glb: object = None
    rig_source: str = ""


class AvatarLoop:
    def __init__(self, source, gate, model, upload, sender, *, clock_ms, freeze_frames=30, max_age_ms=500):
        self.source, self.gate, self.model = source, gate, model
        self.upload, self.sender, self.clock_ms = upload, sender, clock_ms
        self.freeze_frames, self.max_age_ms = freeze_frames, max_age_ms
        self.people = {}
        self.last_t = -1
        self.skipped = 0

    def step(self):
        if not self.gate.check().admit:
            self.skipped += 1
            self.sender.discard_poses()
            return
        item = self.source.latest()
        if item is None:
            return
        snapshot, images = item
        doc = snapshot["world"]
        t = doc["t"]
        if t <= self.last_t or not 0 <= self.clock_ms() - t <= self.max_age_ms:
            return
        self.last_t = t
        active = {tr["world_id"] for tr in doc["tracks"] if tr.get("track_lifecycle") != "LOST"}
        self.people = {wid: st for wid, st in self.people.items() if wid in active}
        self.sender.retain(active)
        rig = CameraRig.from_provider(SnapshotCalibration(snapshot["calibration"]))
        candidates, bodies = [], {}
        for tr in doc["tracks"]:
            wid, rev = tr["world_id"], tr["identity_revision"]
            if wid not in active:
                continue
            state = self.people.setdefault(wid, PersonState(rev))
            if state.revision != rev:
                state.revision, state.stable, state.samples = rev, 0, []
                state.pending_glb = None
            if tr["identity_confidence"] == "IDENTITY_UNCERTAIN":
                state.stable, state.samples = 0, []
                state.pending_glb = None
                continue
            state.stable += 1
            for obs in tr.get("per_camera", []):
                cid = obs["camera_id"]
                ref = snapshot["frames"].get(cid)
                if (cid not in images or ref is None or ref["frame_ref"] != obs["frame_ref"]
                        or abs(t - ref["capture_time_ms"]) > 100):
                    continue
                # 카메라별·사람별 호출 전마다 확인한다. 앞 사람 추론 중 Tier 0 부하가 바뀔 수 있다.
                if not self.gate.check().admit:
                    self.skipped += 1
                    self.sender.discard_poses()
                    return
                candidate, body = self.model.infer(images[cid], obs, tr, rig.cameras[cid])
                candidates.append(candidate)
                bodies[(wid, cid)] = body
        frame = aggregate_frame(candidates, doc, rig)
        poses = []
        for person in frame.people:
            if person.root_rotation is None or person.joint_rotations is None:
                continue
            state = self.people[person.world_id]
            if state.asset is None or state.asset["identity_revision"] != state.revision:
                if state.pending_glb is None:
                    state.samples.append(bodies[(person.world_id, person.representative_camera)])
                    state.samples = state.samples[-self.freeze_frames:]
                if len(state.samples) >= self.freeze_frames and state.stable >= self.freeze_frames:
                    if state.pending_glb is None:
                        baked = self.model.bake(state.samples)
                        state.pending_glb = build_skinned_glb(baked)
                        state.rig_source = baked.rig.rig_source
                    glb = state.pending_glb
                    version = state.version + 1
                    uri = self.upload(person.world_id, self.sender.epoch, version, glb)
                    asset = {"world_id": person.world_id, "stream_epoch": self.sender.epoch,
                             "identity_revision": state.revision, "appearance_version": version,
                             "previous_appearance_version": None if state.version < 0 else state.version,
                             "created_at_ms": self.clock_ms(), "calibration_version": frame.calibration_version,
                             "model_version": self.model.model_version, "input_provenance": doc["input_provenance"],
                             "mesh": {"format": "GLB_SKINNED", "object_uri": uri, "sha256": glb.sha256,
                                      "size_bytes": glb.size_bytes, "vertex_count": glb.vertex_count,
                                      "face_count": glb.face_count, "axis_convention": "GLTF_Y_UP"},
                             "skeleton": {"rig": "MHR", "joint_count": glb.joint_count,
                                          "joint_parents_sha256": glb.joint_parents_sha256,
                                          "pelvis_joint_index": 1, "rig_source": state.rig_source},
                             "shape": {"representation": "MHR", "frozen": True, "frames_used": len(state.samples)},
                             "texture": None, "surface_source": {"mask_uri": None,
                             "ratios": {"OBSERVED": 0., "INTERPOLATED": 0., "UNOBSERVED": 1.},
                             "face_ratios": {"OBSERVED": 0., "UNOBSERVED": 1.}}, "face_policy": "OBSERVED"}
                    state.asset, state.version, state.samples = asset, version, []
                    state.pending_glb = None
                    self.sender.asset(asset)
            if state.asset is None:
                continue
            poses.append({"world_id": person.world_id, "identity_revision": person.identity_revision,
                          "capture_time_ms": t, "appearance_version": state.version, "joint_source": "inferred",
                          "pose": mhr_pose(person.root_rotation, list(person.joint_rotations))})
        if poses and 0 <= self.clock_ms() - t <= self.max_age_ms and self.gate.check().admit:
            self.sender.poses(poses, calibration_version=frame.calibration_version,
                              model_version=self.model.model_version, input_provenance=doc["input_provenance"])
