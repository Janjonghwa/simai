"""상시 EXP-02 루프. 상류·모델·업로드·송신·시계를 주입하며 Tier 0 값은 읽기만 한다."""

from collections import Counter
from dataclasses import dataclass, field
import logging
import threading
from time import perf_counter
from typing import Protocol

from simai_ai.geometry.rig import CameraRig
from simai_ai.hmr.gltf_export import build_skinned_glb
from simai_ai.hmr.appearance_worker import AppearanceWorker
from simai_ai.hmr.multiview import aggregate_frame
from simai_ai.hmr.pose import mhr_pose

log = logging.getLogger(__name__)

# 촬영 시각은 포즈 출처로 보존한다. 입력·송신 신선도는 Tier 0가 공유 통로에 넘긴 뒤 500ms다.
DEFAULT_MAX_AGE_MS = 500


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
    generation: int = 0
    pending_version: int | None = None


class _CountedUpload:
    """루프의 업로드(GLB 호출·PNG `png`)를 그대로 부르고 성공 수만 센다. loop.upload 를 바꿔 끼워도 따라간다."""

    def __init__(self, loop):
        self._loop = loop

    def __call__(self, *args, **kwargs):
        uri = self._loop.upload(*args, **kwargs)
        with self._loop._timing_lock:
            self._loop.uploads += 1
        return uri

    def png(self, *args, **kwargs):
        uri = self._loop.upload.png(*args, **kwargs)
        with self._loop._timing_lock:
            self._loop.uploads += 1
        return uri


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
        # 계측(판정 불변, S15P31A101-412): 게이트 차단 사유 코드별 회차 수, 송신 대기열에 넣은 포즈 배치 수, 업로드·자산 확정 수.
        # 실제로 소켓에 쓴 배치 수는 송신기(sent_batches_total)가 센다.
        self.skip_reasons = Counter()
        self.pose_enqueued = 0
        self.uploads = 0
        self.assets = 0
        self._counted_upload = _CountedUpload(self)
        self.appearance = appearance
        self.segmentation = segmentation
        self.timings = Counter()
        self.timing_counts = Counter()
        self._state_lock = threading.RLock()
        self._timing_lock = threading.Lock()
        self._appearance_worker = None

    def close(self):
        if self._appearance_worker is not None:
            self._appearance_worker.close()

    def _reserve_version(self, state):
        with self._state_lock:
            state.high_version = max(state.version, state.high_version) + 1
            return state.high_version

    def _commit_appearance(self):
        if self._appearance_worker is None or not self.gate.check().admit:
            return
        result = self._appearance_worker.take()
        if result is None:
            return
        tokens, assets = result
        with self._state_lock:
            for asset in assets:
                wid = asset["world_id"]
                original, revision, generation = tokens[wid]
                state = self.people.get(wid)
                if (state is not original or state.revision != revision or state.generation != generation
                        or state.stable == 0 or asset["appearance_version"] <= max(state.history, default=-1)):
                    continue
                asset = {**asset, "previous_appearance_version": max(state.history, default=None)}
                self.sender.discard_poses()
                self.sender.asset(asset)
                state.asset, state.version = asset, asset["appearance_version"]
                state.history[state.version] = asset
                self.assets += 1

    def _timed(self, stage, fn, *args, **kwargs):
        started = perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            with self._timing_lock:
                self.timings[stage] += (perf_counter() - started) * 1000
                self.timing_counts[stage] += 1

    def reannounce_appearance(self, world_id, version):
        """기록을 덮어쓰지 않고 이전 확정 자산을 다시 선택해 알린다."""
        with self._state_lock:
            state = self.people[world_id]
            asset = state.history[version]
            self.sender.discard_poses()
            self.sender.asset(asset)
            state.asset, state.version = asset, version
            state.generation += 1

    def _skip(self, admission, status):
        """게이트가 받지 않은 회차: 대기 포즈를 버리고 사유 코드별로 센다."""
        self.skipped += 1
        self.skip_reasons[getattr(admission, "code", "") or "unknown"] += 1
        self.sender.discard_poses()
        self._report(f"{status}: " + getattr(admission, "reason", "unknown"))

    def _report(self, status):
        """60초에 한 번 요약한다. status 는 그 순간 회차 하나의 상태이고(송신 횟수가 아니다) 수치는 누계다."""
        now = self.clock_ms()
        if now < self._report_at:
            return
        stats = getattr(self.gate, "stats", None)
        gate = stats() if callable(stats) else {}
        log.info("아바타 입력 상태: %s people=%s samples_max=%s/%s assets=%s gate_skipped=%s skip_reasons=%s "
                 "pose_enqueued=%s pose_sent=%s uploads=%s assets_confirmed=%s gate_resets=%s last_reset=%s(%s) "
                 "queue_wait_tolerated=%s/%s",
                 status, len(self.people), max((len(s.samples) for s in self.people.values()), default=0),
                 self.freeze_frames, sum(s.asset is not None for s in self.people.values()), self.skipped,
                 dict(self.skip_reasons), self.pose_enqueued, getattr(self.sender, "sent_batches_total", None),
                 self.uploads, self.assets, gate.get("resets", {}),
                 gate.get("last_reset_ms"), gate.get("last_reset_code"),
                 gate.get("queue_wait_tolerated_windows"), gate.get("queue_wait_tolerated_frames"))
        with self._timing_lock:
            log.info("아바타 단계 시간 ms 누계=%s 횟수=%s", dict(self.timings), dict(self.timing_counts))
        self._report_at = now + 60_000

    def step(self):
        return self._timed("step", self._step)

    def _step(self):
        admission = self._timed("gate", self.gate.check)
        if not admission.admit:
            self._skip(admission, "AdmissionGate 차단")
            return
        now_ms = self.clock_ms()
        accept = lambda snapshot: (
            snapshot.get("world", {}).get("t", -1) > self.last_t
            and isinstance(snapshot.get("produced_at_ms"), int)
            and not isinstance(snapshot.get("produced_at_ms"), bool)
            and 0 <= now_ms - snapshot["produced_at_ms"] <= self.max_age_ms
        )
        item = (self._timed("shared_read", self.source.latest, predicate=accept)
                if getattr(self.source, "supports_predicate", False)
                else self._timed("shared_read", self.source.latest))
        if item is None:
            self._report("공유 통로 입력 없음/읽기 실패")
            return
        snapshot, images = item
        doc = snapshot["world"]
        t = doc["t"]
        if not accept(snapshot):
            self._report("중복/신선도 초과 입력")
            return
        self.last_t = t
        active = {tr["world_id"] for tr in doc["tracks"] if tr.get("track_lifecycle") != "LOST"}
        with self._state_lock:
            self.people = {wid: st for wid, st in self.people.items() if wid in active}
        self.sender.retain(active)
        rig = CameraRig.from_provider(SnapshotCalibration(snapshot["calibration"]))
        candidates, bodies, jobs = [], {}, []
        for tr in doc["tracks"]:
            wid, rev = tr["world_id"], tr["identity_revision"]
            if wid not in active:
                continue
            state = self.people.setdefault(wid, PersonState(rev))
            with self._state_lock:
                if state.revision != rev:
                    state.revision, state.stable, state.samples = rev, 0, []
                    state.pending_glb = None
                    state.pending_version = None
                    state.generation += 1
            if (
                tr["identity_confidence"] == "IDENTITY_UNCERTAIN"
                or tr.get("avatar_update_hold", False)
                or tr.get("hold_appearance_update", False)
            ):
                # 불확실한 표본은 쓰지 않지만 같은 차수에서 이미 확보한 표본은 보존한다.
                # 교정 뒤 30 프레임 안정 조건은 별도로 다시 센다.
                state.stable = 0
                continue
            state.stable += 1
            for obs in tr.get("per_camera", []):
                cid = obs["camera_id"]
                ref = snapshot["frames"].get(cid)
                if (cid not in images or ref is None or ref["frame_ref"] != obs["frame_ref"]
                        or abs(t - ref["capture_time_ms"]) > 100):
                    continue
                jobs.append((tr, obs))
        self._commit_appearance()
        prepared, masks = {}, {}
        if self.segmentation is not None and jobs:
            if not (admission := self._timed("gate", self.gate.check)).admit:
                self._skip(admission, "seg 전 AdmissionGate 차단")
                return
            prepared = self._timed("segmentation", self.segmentation.prepare, jobs, images, rig)
        for tr, obs in jobs:
            wid, cid = tr["world_id"], obs["camera_id"]
            # seg 배치 뒤에도 매 사람·카메라 추론 전에 Tier 0 부하를 다시 확인한다.
            if not (admission := self._timed("gate", self.gate.check)).admit:
                self._skip(admission, "추론 중 AdmissionGate 차단")
                return
            person_input = prepared.get((wid, cid))
            if self.segmentation is not None and not self.segmentation.failed:
                if person_input is None:  # 중복 World ID·잘못된 bbox는 build_prompts가 거른다.
                    continue
                candidate, body = self._timed("infer", self.model.infer, images[cid], obs, tr, rig.cameras[cid],
                                              prepared=person_input)
                if person_input.original_mask is not None:
                    masks[(wid, cid)] = person_input.original_mask
            else:
                candidate, body = self._timed("infer", self.model.infer, images[cid], obs, tr, rig.cameras[cid])
            candidates.append(candidate)
            bodies[(wid, cid)] = body
        frame = self._timed("aggregate", aggregate_frame, candidates, doc, rig)
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
                        baked = self._timed("bake", self.model.bake, state.samples)
                        if self.appearance is not None:
                            state.baked = baked
                        state.pending_glb = self._timed("glb", build_skinned_glb, baked)
                        state.rig_source = baked.rig.rig_source
                    glb = state.pending_glb
                    if state.pending_version is None:
                        state.pending_version = self._reserve_version(state)
                    version = state.pending_version
                    uri = self._timed("upload", self._counted_upload, person.world_id, self.sender.epoch, version, glb)
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
                    state.pending_version = None
                    if self.appearance is not None:
                        asset["face_policy"] = self.appearance.config.face_policy
                    self.sender.asset(asset)
                    self.assets += 1
            if state.asset is None:
                continue
            poses.append({"world_id": person.world_id, "identity_revision": person.identity_revision,
                          "capture_time_ms": t, "appearance_version": state.version, "joint_source": "inferred",
                          "pose": mhr_pose(person.root_rotation, list(person.joint_rotations))})
        if self.appearance is not None:
            if self._appearance_worker is None:
                self._appearance_worker = AppearanceWorker(self)
            self._appearance_worker.offer(frame, doc, snapshot, images, rig, masks)
        if poses and 0 <= self.clock_ms() - snapshot["produced_at_ms"] <= self.max_age_ms and self.gate.check().admit:
            self._timed("send", self.sender.poses, poses, calibration_version=frame.calibration_version,
                        model_version=self.model.model_version, input_provenance=doc["input_provenance"],
                        produced_at_ms=snapshot["produced_at_ms"])
            self.pose_enqueued += 1
            self._report(f"포즈 대기열 전달 poses={len(poses)}")
        else:
            self._report(f"포즈 없음/신선도·gate 차단 candidates={len(candidates)} "
                         f"selected={len(frame.people)} rejected={len(frame.rejected)}")
