"""상시 EXP-02 루프. 상류·모델·업로드·송신·시계를 주입하며 Tier 0 값은 읽기만 한다."""

from dataclasses import dataclass, field
import logging
from typing import Protocol

from simai_ai.geometry.rig import CameraRig
from simai_ai.gpu.admission import DEFAULT_LAG_BUDGET_MS
from simai_ai.hmr.gltf_export import build_skinned_glb
from simai_ai.hmr.multiview import aggregate_frame
from simai_ai.hmr.pose import mhr_pose

log = logging.getLogger(__name__)

# 아바타 루프의 clock_ms() - snapshot["world"]["t"] 는 Pod 현재 시각과 카메라 촬영 시각의 차이이므로
# (1) Tier 0 capture_to_risk 지연 예산(명세 6.6 p95 600ms, simai_ai.gpu.admission.DEFAULT_LAG_BUDGET_MS)과
# (2) 아바타 공유 통로(5 Hz)·루프 폴링(200ms)·YOLO26x-seg·Fast SAM 3D Body 다중 뷰 추론 여유(명세 5.6 2~5 Hz,
#     명세 13.2/20.4 관제 화면 포즈 신선도 상한 500ms)의 합을 기본 신선도 상한으로 둔다.
TIER0_LAG_BUDGET_MS = DEFAULT_LAG_BUDGET_MS
HMR_STREAM_SLACK_MS = 500
DEFAULT_MAX_AGE_MS = TIER0_LAG_BUDGET_MS + HMR_STREAM_SLACK_MS


class HmrModel(Protocol):
    model_version: str

    def infer(self, image, observation, track, camera, *, prepared=None): ...
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
    baked: object = None
    high_version: int = -1
    history: dict = field(default_factory=dict)


class AvatarLoop:
    def __init__(self, source, gate, model, upload, sender, *, clock_ms, freeze_frames=30,
                 max_age_ms=DEFAULT_MAX_AGE_MS, appearance=None, segmentation=None):
        self.source, self.gate, self.model = source, gate, model
        self.upload, self.sender, self.clock_ms = upload, sender, clock_ms
        self.freeze_frames, self.max_age_ms = freeze_frames, max_age_ms
        self.people = {}
        self.last_t = -1
        self.skipped = 0
        self._report_at = 0
        self.appearance = appearance
        self.segmentation = segmentation

    def reannounce_appearance(self, world_id, version):
        """기록을 덮어쓰지 않고 이전 확정 자산을 다시 선택해 알린다."""
        state = self.people[world_id]
        asset = state.history[version]
        self.sender.discard_poses()
        self.sender.asset(asset)
        state.asset, state.version = asset, version
        if self.appearance is not None:
            self.appearance.pending.pop(world_id, None)

    def _report(self, status):
        now = self.clock_ms()
        if now < self._report_at:
            return
        log.info("아바타 입력 상태: %s people=%s samples_max=%s/%s assets=%s gate_skipped=%s",
                 status, len(self.people), max((len(s.samples) for s in self.people.values()), default=0),
                 self.freeze_frames, sum(s.asset is not None for s in self.people.values()), self.skipped)
        self._report_at = now + 60_000

    def step(self):
        admission = self.gate.check()
        if not admission.admit:
            self.skipped += 1
            self.sender.discard_poses()
            self._report("AdmissionGate 차단: " + getattr(admission, "reason", "unknown"))
            return
        item = self.source.latest()
        if item is None:
            self._report("공유 통로 입력 없음/읽기 실패")
            return
        snapshot, images = item
        doc = snapshot["world"]
        t = doc["t"]
        if t <= self.last_t or not 0 <= self.clock_ms() - t <= self.max_age_ms:
            self._report("중복/신선도 초과 입력")
            return
        self.last_t = t
        active = {tr["world_id"] for tr in doc["tracks"] if tr.get("track_lifecycle") != "LOST"}
        self.people = {wid: st for wid, st in self.people.items() if wid in active}
        if self.appearance is not None:
            self.appearance.retain(active)
        self.sender.retain(active)
        rig = CameraRig.from_provider(SnapshotCalibration(snapshot["calibration"]))
        candidates, bodies, jobs = [], {}, []
        for tr in doc["tracks"]:
            wid, rev = tr["world_id"], tr["identity_revision"]
            if wid not in active:
                continue
            state = self.people.setdefault(wid, PersonState(rev))
            if self.appearance is not None:
                self.appearance.track(tr, t)
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
                jobs.append((tr, obs))
        prepared, masks = {}, {}
        if self.segmentation is not None and jobs:
            if not self.gate.check().admit:
                self.skipped += 1
                self.sender.discard_poses()
                self._report("seg 전 AdmissionGate 차단")
                return
            prepared = self.segmentation.prepare(jobs, images, rig)
        for tr, obs in jobs:
            wid, cid = tr["world_id"], obs["camera_id"]
            # seg 배치 뒤에도 매 사람·카메라 추론 전에 Tier 0 부하를 다시 확인한다.
            if not self.gate.check().admit:
                self.skipped += 1
                self.sender.discard_poses()
                self._report("추론 중 AdmissionGate 차단")
                return
            person_input = prepared.get((wid, cid))
            if self.segmentation is not None and not self.segmentation.failed:
                if person_input is None:  # 중복 World ID·잘못된 bbox는 build_prompts가 거른다.
                    continue
                candidate, body = self.model.infer(images[cid], obs, tr, rig.cameras[cid], prepared=person_input)
                if person_input.original_mask is not None:
                    masks[(wid, cid)] = person_input.original_mask
            else:
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
                        if self.appearance is not None:
                            state.baked = baked
                            self.appearance.register(person.world_id, state, t)
                        state.pending_glb = build_skinned_glb(baked)
                        state.rig_source = baked.rig.rig_source
                    glb = state.pending_glb
                    version = max(state.version, state.high_version) + 1
                    uri = self.upload(person.world_id, self.sender.epoch, version, glb)
                    asset = {"world_id": person.world_id, "stream_epoch": self.sender.epoch,
                             "identity_revision": state.revision, "appearance_version": version,
                             "previous_appearance_version": max(state.history) if state.history else None,
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
                    state.high_version = version
                    state.history[version] = asset
                    state.pending_glb = None
                    if self.appearance is not None:
                        asset["face_policy"] = self.appearance.config.face_policy
                    self.sender.asset(asset)
            if state.asset is None:
                continue
            poses.append({"world_id": person.world_id, "identity_revision": person.identity_revision,
                          "capture_time_ms": t, "appearance_version": state.version, "joint_source": "inferred",
                          "pose": mhr_pose(person.root_rotation, list(person.joint_rotations))})
        if self.appearance is not None:
            try:
                self.appearance.update(frame, doc, snapshot, images, rig, self.people, self.upload,
                                       self.sender, self.clock_ms, lambda: self.gate.check().admit, masks)
            except Exception as error:
                # 기존 확정 외형·포즈는 계속 쓴다. 실패한 PNG는 다음 회차에 같은 바이트로 재시도한다.
                log.warning("외형 갱신 실패: %s; 마지막 확정 외형 유지", type(error).__name__)
            for pose in poses:
                pose["appearance_version"] = self.people[pose["world_id"]].version
        if poses and 0 <= self.clock_ms() - t <= self.max_age_ms and self.gate.check().admit:
            self.sender.poses(poses, calibration_version=frame.calibration_version,
                              model_version=self.model.model_version, input_provenance=doc["input_provenance"])
            self._report(f"포즈 대기열 전달 poses={len(poses)}")
        else:
            self._report(f"포즈 없음/신선도·gate 차단 candidates={len(candidates)} "
                         f"selected={len(frame.people)} rejected={len(frame.rejected)}")
