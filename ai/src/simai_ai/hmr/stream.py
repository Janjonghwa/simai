"""상시 EXP-02 루프. 상류·모델·업로드·송신·시계를 주입하며 Tier 0 값은 읽기만 한다.

World ID 교정(S15P31A101-661): identity_revision 이 바뀌면 표본·안정 수를 비우고 새 차수로 다시 굽는다. 다만 순수 병합
(aliases 에 새 ID 만 붙고 분할 아님, simai_ai.tracking.identity_carry)이면 남는 ID 의 표본·자산을 그대로 두고 다시 굽지
않는다. 안정 수는 0 으로 둬 다시 안정될 때까지 외형 누적을 멈춘다(명세 20.4 '교정 직후 누적 멈춤', 포즈는 계속 보낸다). 이미 보낸 자산은 바꾸지 않는다(같은 버전을 다른 내용으로 보내지 않음): 자산 차수 < 포즈 차수는 설계
'ID 교정 뒤 외형'에서 허용한다. `revision_base` 는 마지막으로 처음부터 다시 쌓기 시작한 차수이고, 자산 차수가 그 이상이면
지금 사람의 자산이다(asset_current). 흡수된 ID 의 상태는 지운다(그 ID 의 자산은 다른 world_id 라 쓰지 않는다).

외형 승계(S15P31A101-743, 표시 전용, Tier 0 ID·위치·판정 불변): 순수 병합이면 흡수 ID 의 외형 누적을 남는 ID 에 합치고
(관측 텍셀 합집합·품질 가중 평균) 체형은 표본이 많은 쪽을 쓴다. LOST 뒤 같은 ID 로 다시 이어지면 그 ID 의 상태를 되살린다.
근거가 모자라 새 ID 가 생긴 경우는 `appearance_carry` 의 기하·시간 조건을 모두 맞을 때만 사라진 ID 의 외형·체형으로 시작한다.
"""

from collections import Counter
from dataclasses import dataclass, field
import logging
import math
import threading
from time import perf_counter
from typing import Protocol

from simai_ai.geometry.rig import CameraRig
from simai_ai.hmr.appearance_carry import AppearanceCarry
from simai_ai.hmr.appearance_worker import AppearanceWorker
from simai_ai.hmr.diagnostics import is_upload_conflict
from simai_ai.hmr.gltf_export import build_skinned_glb
from simai_ai.hmr.multiview import aggregate_frame
from simai_ai.hmr.pose import mhr_pose
from simai_ai.hmr.prompt import valid_box
from simai_ai.tracking.identity_carry import pure_merge_absorbed, split_marks

log = logging.getLogger(__name__)

# 촬영 시각은 포즈 출처로 보존한다.
# 입력·송신 신선도는 Tier 0 공유 주기(200ms) + 자체 감속(최대 800ms) + 전송 여유를 수용하도록 1000ms로 둔다.
# 위치·위험 판정 정본은 언제나 Tier 0 10Hz world_state 다(명세 3.2, 5.2).
DEFAULT_MAX_AGE_MS = 1000
DEFAULT_INITIAL_FREEZE_FRAMES = 5
DEFAULT_FREEZE_FRAMES = 30
OCCLUSION_OVERLAP_THRESHOLD = 0.25


def _bbox_area_and_overlap(bbox, other_bboxes) -> tuple[float, float]:
    if not valid_box(bbox):
        return 0.0, 1.0
    x0, y0, x1, y1 = (float(v) for v in bbox)
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if area <= 0.0:
        return 0.0, 1.0
    max_overlap = 0.0
    for ob in other_bboxes:
        if not valid_box(ob):
            continue
        ox0, oy0, ox1, oy1 = (float(v) for v in ob)
        inter_w = max(0.0, min(x1, ox1) - max(x0, ox0))
        inter_h = max(0.0, min(y1, oy1) - max(y0, oy0))
        inter = inter_w * inter_h
        if inter > 0.0:
            max_overlap = max(max_overlap, inter / area)
    return area, max_overlap


def _observed_keypoint_count(keypoints_2d) -> int:
    if not isinstance(keypoints_2d, (list, tuple)):
        return 0
    count = 0
    for kp in keypoints_2d:
        if isinstance(kp, (list, tuple)) and len(kp) >= 3:
            try:
                u, v, conf = float(kp[0]), float(kp[1]), float(kp[2])
                if math.isfinite(u) and math.isfinite(v) and math.isfinite(conf) and conf >= 0.3:
                    count += 1
            except (TypeError, ValueError):
                pass
    return count


def select_representative_observation(tr, tracks, frame_refs, images, rig, t):
    """사람당 가장 좋은 대표 카메라 1대 관측만 고른다(가림 여부·관측 관절 수·bbox 크기 기준)."""
    wid = tr["world_id"]
    per_cam = tr.get("per_camera") or ()
    cam_counts = Counter(obs.get("camera_id") for obs in per_cam if isinstance(obs, dict))
    best_obs = None
    best_key = None
    for obs in per_cam:
        cid = obs.get("camera_id")
        if cam_counts[cid] > 1:
            continue
        ref = frame_refs.get(cid)
        if (
            cid not in images
            or cid not in rig.cameras
            or ref is None
            or ref["frame_ref"] != obs["frame_ref"]
            or abs(t - ref["capture_time_ms"]) > 100
        ):
            continue
        bbox = obs.get("bbox", ())
        if not valid_box(bbox):
            continue
        other_bboxes = [
            o.get("bbox", ())
            for other in tracks
            if other.get("world_id") != wid and other.get("track_lifecycle") != "LOST"
            for o in other.get("per_camera") or ()
            if o.get("camera_id") == cid and o.get("frame_ref") == ref["frame_ref"]
        ]
        area, max_overlap = _bbox_area_and_overlap(bbox, other_bboxes)
        if area <= 0.0:
            continue
        unoccluded = max_overlap <= OCCLUSION_OVERLAP_THRESHOLD
        kp_count = _observed_keypoint_count(obs.get("keypoints_2d"))
        score_key = (
            1 if unoccluded else 0,
            -round(max_overlap, 2),
            kp_count,
            round(area, 1),
        )
        if best_key is None or score_key > best_key or (score_key == best_key and cid < best_obs["camera_id"]):
            best_key = score_key
            best_obs = obs
    return best_obs


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
    shape_frozen: bool = False
    pending_target_frozen: bool = False
    pending_frames_used: int = 0
    # World ID 교정 승계(S15P31A101-661): 마지막으로 처음부터 다시 쌓기 시작한 차수, 마지막으로 본 aliases·split_from
    revision_base: int | None = None
    aliases: tuple = ()
    split_from: str | None = None
    # 외형 승계(S15P31A101-743): 받은 고정 체형(다음 굽기에 그대로 씀)·그 프레임 수, 받은 체형 표본이면 첫 메시 안정 1 프레임
    inherited_baked: object = None
    inherited_frames: int = 0
    inherited_shape: bool = False

    def __post_init__(self):
        if self.revision_base is None:
            self.revision_base = self.revision


def _object_bytes(obj) -> int:
    if obj is None:
        return 0
    nbytes = getattr(obj, "nbytes", None)
    if isinstance(nbytes, int) and not isinstance(nbytes, bool):
        return max(0, nbytes)
    size = getattr(obj, "size_bytes", None)
    if isinstance(size, int) and not isinstance(size, bool):
        return max(0, size)
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return len(obj)
    return 0


@dataclass
class CarryDonor:
    """소실·흡수된 ID 의 포즈 루프 상태와 외형 누적 사본(없으면 None)."""
    state: PersonState
    appearance: object = None

    def size_bytes(self) -> int:
        """휴면 보관 메모리 상한 계산용 추정 바이트 수(배열·PNG·GLB 버퍼 기준, 공유 MHR 리그 제외)."""
        total = 0
        app = self.appearance
        if app is not None:
            for attr in ("colors", "weights", "inherited", "peaks"):
                total += _object_bytes(getattr(app, attr, None))
            confirmed = getattr(app, "last_confirmed", None)
            if confirmed is not None:
                total += _object_bytes(getattr(confirmed, "albedo", None))
                total += _object_bytes(getattr(confirmed, "surface", None))
        baked = self.state.baked
        if baked is not None:
            for attr in ("rest_vertices_m", "local_translations_m", "local_rotations",
                         "local_scales", "inverse_bind_matrices"):
                total += _object_bytes(getattr(baked, attr, None))
        total += _object_bytes(self.state.pending_glb)
        for sample in self.state.samples:
            if isinstance(sample, (tuple, list)):
                total += sum(_object_bytes(item) for item in sample)
            else:
                total += _object_bytes(sample)
        return total


def asset_current(state) -> bool:
    """지금 사람(마지막 초기화 뒤 같은 사람)의 자산인가. 순수 병합으로 차수만 오른 자산도 포함한다."""
    return state.asset is not None and state.asset["identity_revision"] >= state.revision_base


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
    def __init__(self, source, gate, model, upload, sender, *, clock_ms, freeze_frames=DEFAULT_FREEZE_FRAMES,
                 initial_freeze_frames=None, max_age_ms=DEFAULT_MAX_AGE_MS, appearance=None, segmentation=None,
                 carry_config=None):
        self.source, self.gate, self.model = source, gate, model
        self.upload, self.sender, self.clock_ms = upload, sender, clock_ms
        self.freeze_frames, self.max_age_ms = freeze_frames, max_age_ms
        if initial_freeze_frames is None:
            self.initial_freeze_frames = min(DEFAULT_INITIAL_FREEZE_FRAMES, freeze_frames)
        else:
            self.initial_freeze_frames = max(1, min(int(initial_freeze_frames), freeze_frames))
        self.people = {}
        self._high_versions = {}
        self._last_versions = {}
        self.last_t = -1
        self.last_infer_ms = -1
        self.skipped = 0
        self.throttled = 0
        self.duplicate_inputs = 0
        self.stale_inputs = 0
        self._report_at = 0
        # 계측(판정 불변, S15P31A101-412): 게이트 차단 사유 코드별 회차 수, 송신 대기열에 넣은 포즈 배치 수, 업로드·자산 확정 수.
        # 실제로 소켓에 쓴 배치 수는 송신기(sent_batches_total)가 센다.
        self.skip_reasons = Counter()
        # 계측: 차수 변화 때 초기화(revision_reset)·순수 병합 승계(merge_carried, 자산 유지 merge_asset_kept),
        # 외형 승계(S15P31A101-743): 병합 외형 합침(merge_appearance)·같은 ID 재연결(lost_restored)·새 ID 이어받기(carry_inherited)
        self.identity_events = Counter()
        self.carry = AppearanceCarry(carry_config)
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

    def _reserve_version(self, state, wid=None):
        with self._state_lock:
            high = max(state.version, state.high_version)
            if wid is not None:
                high = max(high, self._high_versions.get(wid, -1))
            state.high_version = high + 1
            if wid is not None:
                self._high_versions[wid] = state.high_version
            return state.high_version

    def _previous_version(self, wid, state, version):
        if version <= 0:
            return None
        if state.history:
            return max(state.history)
        prev = self._last_versions.get(wid, version - 1)
        return prev if 0 <= prev < version else version - 1

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
                version = asset["appearance_version"]
                asset = {**asset, "previous_appearance_version": self._previous_version(wid, state, version)}
                self.sender.discard_poses()
                self.sender.asset(asset)
                state.asset, state.version = asset, version
                state.high_version = max(state.high_version, version)
                self._high_versions[wid] = max(self._high_versions.get(wid, -1), state.high_version)
                self._last_versions[wid] = max(self._last_versions.get(wid, -1), version)
                state.history[state.version] = asset
                self.assets += 1

    def _donor(self, wid, state):
        snapshot = getattr(self.appearance, "snapshot", None)
        return CarryDonor(state, snapshot(wid) if snapshot is not None else None)

    def _carry_in(self, wid, donor, *, inherit, publish=True):
        carry_in = getattr(self.appearance, "carry_in", None)
        if carry_in is not None and donor.appearance is not None:
            carry_in(wid, donor.appearance, inherit=inherit, publish=publish)

    @staticmethod
    def _shape_from(state, donor):
        if donor.shape_frozen and donor.baked is not None:
            state.inherited_baked = donor.baked
            shape = (donor.asset or {}).get("shape") or {}
            state.inherited_frames = int(shape.get("frames_used") or 0)
            return True
        return False

    def _merge_shape(self, state, donor):
        """순수 병합: 체형은 표본이 많은 쪽(고정 체형 = 가장 많음). 남는 쪽이 이미 고정이면 그대로 둔다."""
        if state.shape_frozen or state.pending_glb is not None or self._shape_from(state, donor):
            return
        if len(donor.samples) > len(state.samples):
            state.samples = list(donor.samples)[-self.freeze_frames:]
            state.inherited_shape = True

    def _inherit_shape(self, state, donor):
        """새 ID 이어받기: 아직 자기 메시가 없을 때만 받은 체형으로 시작한다(자기 표본은 뒤에 붙인다)."""
        if asset_current(state) or state.pending_glb is not None or self._shape_from(state, donor):
            return
        if donor.samples:
            state.samples = (list(donor.samples) + state.samples)[-self.freeze_frames:]
            state.inherited_shape = True

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
        log.info("아바타 입력 상태: %s people=%s samples_max=%s/%s assets=%s gate_skipped=%s throttled=%s "
                 "duplicate_inputs=%s stale_inputs=%s skip_reasons=%s "
                 "pose_enqueued=%s pose_sent=%s uploads=%s assets_confirmed=%s gate_resets=%s last_reset=%s(%s) "
                 "queue_wait_tolerated=%s/%s",
                 status, len(self.people), max((len(s.samples) for s in self.people.values()), default=0),
                 self.freeze_frames, sum(s.asset is not None for s in self.people.values()), self.skipped,
                 self.throttled, self.duplicate_inputs, self.stale_inputs,
                 dict(self.skip_reasons), self.pose_enqueued, getattr(self.sender, "sent_batches_total", None),
                 self.uploads, self.assets, gate.get("resets", {}),
                 gate.get("last_reset_ms"), gate.get("last_reset_code"),
                 gate.get("queue_wait_tolerated_windows"), gate.get("queue_wait_tolerated_frames"))
        with self._timing_lock:
            log.info("아바타 단계 시간 ms 누계=%s 횟수=%s", dict(self.timings), dict(self.timing_counts))
        worker_deferred = (
            dict(self._appearance_worker.deferred_reasons)
            if self._appearance_worker is not None and hasattr(self._appearance_worker, "deferred_reasons")
            else None
        )
        log.info("아바타 신원·외형 승계 identity_events=%s carry=%s appearance_carry=%s dormant=%s/%.1fMB worker_deferred=%s",
                 dict(self.identity_events), dict(self.carry.stats), getattr(self.appearance, "carry_events", None),
                 len(self.carry.dormant), self.carry.dormant_bytes / (1024 * 1024), worker_deferred)
        self._report_at = now + 60_000

    def _classify_input(self, snapshot, now_ms: int) -> str | None:
        t = snapshot.get("world", {}).get("t", -1)
        if not isinstance(t, int) or isinstance(t, bool) or t <= self.last_t:
            return "duplicate"
        produced = snapshot.get("produced_at_ms")
        if not isinstance(produced, int) or isinstance(produced, bool) or not (0 <= now_ms - produced <= self.max_age_ms):
            return "stale"
        return None

    def step(self):
        return self._timed("step", self._step)

    def _step(self):
        admission = self._timed("gate", self.gate.check)
        if not admission.admit:
            self._skip(admission, "AdmissionGate 차단")
            return
        now_ms = self.clock_ms()
        rec_fn = getattr(self.gate, "recommended_interval_ms", None)
        target_interval_ms = int(rec_fn()) if callable(rec_fn) else 200
        if target_interval_ms > 200 and self.last_infer_ms >= 0 and 0 <= now_ms - self.last_infer_ms < target_interval_ms:
            self.throttled += 1
            self.skip_reasons["self_throttle"] += 1
            self._report(f"Tier 0 부하 자체 감속(interval={target_interval_ms}ms)")
            return
        accept = lambda snapshot: self._classify_input(snapshot, now_ms) is None
        item = (self._timed("shared_read", self.source.latest, predicate=accept)
                if getattr(self.source, "supports_predicate", False)
                else self._timed("shared_read", self.source.latest))
        if item is None:
            self._report("공유 통로 입력 없음/읽기 실패")
            return
        snapshot, images = item
        doc = snapshot["world"]
        t = doc["t"]
        reject_reason = self._classify_input(snapshot, now_ms)
        if reject_reason is not None:
            if reject_reason == "duplicate":
                self.duplicate_inputs += 1
                self._report("중복 입력")
            else:
                self.stale_inputs += 1
                self._report("신선도 초과 입력")
            return
        self.last_t = t
        active = {tr["world_id"] for tr in doc["tracks"] if tr.get("track_lifecycle") != "LOST"}
        with self._state_lock:
            for wid, st in self.people.items():
                if max(st.version, st.high_version) >= 0:
                    self._high_versions[wid] = max(self._high_versions.get(wid, -1), st.version, st.high_version)
                if st.version >= 0:
                    self._last_versions[wid] = max(self._last_versions.get(wid, -1), st.version)
            gone = {wid: st for wid, st in self.people.items() if wid not in active}
            self.people = {wid: st for wid, st in self.people.items() if wid in active}
        # 빠진 ID(LOST·사라짐·흡수)는 병합·같은 ID 재연결·새 ID 이어받기에 쓰도록 잠시 들고 있는다(S15P31A101-743)
        for wid, st in gone.items():
            self.carry.depart(wid, self._donor(wid, st), t, donor=st.asset is not None)
        self.carry.expire(t)
        self.carry.observe(doc["tracks"], t)
        self.sender.retain(active)
        rig = CameraRig.from_provider(SnapshotCalibration(snapshot["calibration"]))
        candidates, bodies, jobs = [], {}, []
        wid_counts = Counter(tr["world_id"] for tr in doc["tracks"] if tr.get("track_lifecycle") != "LOST")
        marks = split_marks(doc["tracks"])
        for tr in doc["tracks"]:
            wid, rev = tr["world_id"], tr["identity_revision"]
            if wid not in active or wid_counts[wid] > 1:
                continue
            aliases = tuple(tr.get("aliases") or ())
            split_from = tr.get("split_from")
            with self._state_lock:
                state = self.people.get(wid)
                restored = None
                if state is None:
                    # LOST 뒤 같은 ID 로 다시 이어졌으면(Tier 0 가 같은 사람이라고 함) 그 상태를 되살린다
                    restored = self.carry.take(wid)
                    if restored is not None:
                        state = restored.state
                        state.stable = 0
                        state.generation += 1   # 빠지기 전 굽던 외형 결과는 버린다(재입장 무효화 규칙), 누적 텍셀은 아래에서 합친다
                        self.identity_events["lost_restored"] += 1
                    else:
                        state = PersonState(
                            rev,
                            high_version=self._high_versions.get(wid, -1),
                            aliases=aliases,
                            split_from=split_from,
                        )
                        self.carry.receiver(wid)
                    self.people[wid] = state
                reset = False
                if state.revision != rev or state.split_from != split_from:
                    absorbed = (
                        pure_merge_absorbed(wid, state.revision, state.aliases, state.split_from,
                                            rev, aliases, split_from, marks)
                        if state.revision != rev
                        else None
                    )
                    if absorbed is not None:
                        # 순수 병합: 같은 사람이다. 표본·자산·굽는 중인 메시를 잇고 차수만 따른다(다시 굽지 않음).
                        # 안정 수만 0 으로 둬 다시 안정될 때까지(30 프레임) 외형 누적·체형 확정을 멈춘다(명세 20.4)
                        state.revision, state.stable = rev, 0
                        for gone in absorbed:
                            donor = self.carry.take(gone)
                            old = self.people.pop(gone, None)
                            if donor is None and old is not None:
                                donor = self._donor(gone, old)
                            if donor is not None:
                                # 같은 사람이다: 외형 누적을 합치고 체형은 표본이 많은 쪽(S15P31A101-743)
                                self._merge_shape(state, donor.state)
                                self._carry_in(wid, donor, inherit=False)
                                self.identity_events["merge_appearance"] += 1
                        self.identity_events["merge_carried"] += 1
                        if state.asset is not None:
                            self.identity_events["merge_asset_kept"] += 1
                    else:
                        state.revision, state.stable, state.samples = rev, 0, []
                        state.revision_base = rev
                        state.shape_frozen = False
                        state.baked = None
                        state.pending_glb = None
                        state.pending_version = None
                        state.pending_target_frozen = False
                        state.pending_frames_used = 0
                        state.generation += 1
                        state.inherited_baked, state.inherited_shape = None, False
                        reset = True
                        self.identity_events["revision_reset"] += 1
                    state.aliases, state.split_from = aliases, split_from
                if restored is not None and not reset:
                    already_textured = asset_current(state) and state.asset.get("texture") is not None
                    self._carry_in(wid, restored, inherit=False, publish=not already_textured)
                    if asset_current(state):
                        self.sender.asset(state.asset)   # 같은 버전 재알림(빠질 때 송신기가 놓았다), 내용은 처음 그대로
            if (
                tr["identity_confidence"] == "IDENTITY_UNCERTAIN"
                or tr.get("avatar_update_hold", False)
                or tr.get("hold_appearance_update", False)
            ):
                # identity_revision이 유지된 동안의 일시적 근접·불확실 구간에서는 stable 카운터를 0으로 리셋하지 않고 일시정지한다.
                continue
            state.stable += 1
            obs = select_representative_observation(tr, doc["tracks"], snapshot["frames"], images, rig, t)
            if obs is not None:
                jobs.append((tr, obs))
        # 근거 부족으로 생긴 새 ID: 기하·시간 조건을 모두 맞으면 사라진 ID 의 외형·체형으로 시작한다(표시 전용)
        for receiver, donor in self.carry.decide(doc["tracks"], t):
            with self._state_lock:
                state = self.people.get(receiver)
                if state is None:
                    continue
                self._inherit_shape(state, donor.state)
                self._carry_in(receiver, donor, inherit=True)
                self.identity_events["carry_inherited"] += 1
        self._commit_appearance()
        for tr, obs in jobs:
            wid, cid = tr["world_id"], obs["camera_id"]
            if not (admission := self._timed("gate", self.gate.check)).admit:
                self._skip(admission, "추론 중 AdmissionGate 차단")
                return
            candidate, body = self._timed("infer", self.model.infer, images[cid], obs, tr, rig.cameras[cid])
            candidates.append(candidate)
            bodies[(wid, cid)] = body
        if candidates:
            self.last_infer_ms = now_ms
        frame = self._timed("aggregate", aggregate_frame, candidates, doc, rig)
        poses = []
        for person in frame.people:
            if person.root_rotation is None or person.joint_rotations is None:
                continue
            state = self.people[person.world_id]
            needs_bake = not asset_current(state) or not state.shape_frozen
            if needs_bake:
                if state.pending_glb is None:
                    state.samples.append(bodies[(person.world_id, person.representative_camera)])
                    state.samples = state.samples[-self.freeze_frames:]
                need_initial = not asset_current(state)
                ready_final = (
                    not state.shape_frozen
                    and len(state.samples) >= self.freeze_frames
                    and state.stable >= self.freeze_frames
                )
                # 받은 체형 표본(병합·이어받기)은 이미 안정된 사람의 것이라 첫 메시는 이 ID 의 안정 1 프레임으로 굽는다
                ready_initial = (
                    need_initial
                    and len(state.samples) >= self.initial_freeze_frames
                    and state.stable >= (1 if state.inherited_shape else self.initial_freeze_frames)
                )
                inherited = state.inherited_baked is not None
                if state.pending_glb is not None or ready_final or ready_initial or inherited:
                    if state.pending_glb is None:
                        if inherited:
                            # 받은 고정 체형을 그대로 쓴다(다시 추정하지 않음). 파일은 이 world_id 경로로 새로 올린다
                            baked, target_frozen = state.inherited_baked, True
                            frames_used = state.inherited_frames or len(state.samples)
                        else:
                            target_frozen = bool(ready_final or self.initial_freeze_frames >= self.freeze_frames)
                            baked = self._timed("bake", self.model.bake, state.samples)
                            frames_used = len(state.samples)
                        state.inherited_baked, state.inherited_shape = None, False
                        # 외형 누적과 체형 승계(병합·이어받기)가 쓴다. 외형을 꺼도 들고 있는다(작은 리그 배열)
                        state.baked = baked
                        state.pending_glb = self._timed("glb", build_skinned_glb, baked)
                        state.rig_source = baked.rig.rig_source
                        state.pending_target_frozen = target_frozen
                        state.pending_frames_used = frames_used
                    glb = state.pending_glb
                    if state.pending_version is None:
                        state.pending_version = self._reserve_version(state, person.world_id)
                    version = state.pending_version
                    try:
                        uri = self._timed("upload", self._counted_upload, person.world_id, self.sender.epoch, version, glb)
                    except Exception as error:
                        if is_upload_conflict(error):
                            state.pending_version = self._reserve_version(state, person.world_id)
                        raise
                    prev_same_rev = state.asset if asset_current(state) else None
                    asset = {"world_id": person.world_id, "stream_epoch": self.sender.epoch,
                             "identity_revision": state.revision, "appearance_version": version,
                             "previous_appearance_version": self._previous_version(person.world_id, state, version),
                             "created_at_ms": self.clock_ms(), "calibration_version": frame.calibration_version,
                             "model_version": self.model.model_version, "input_provenance": doc["input_provenance"],
                             "mesh": {"format": "GLB_SKINNED", "object_uri": uri, "sha256": glb.sha256,
                                      "size_bytes": glb.size_bytes, "vertex_count": glb.vertex_count,
                                      "face_count": glb.face_count, "axis_convention": "GLTF_Y_UP"},
                             "skeleton": {"rig": "MHR", "joint_count": glb.joint_count,
                                          "joint_parents_sha256": glb.joint_parents_sha256,
                                          "pelvis_joint_index": 1, "rig_source": state.rig_source},
                             "shape": {"representation": "MHR", "frozen": state.pending_target_frozen,
                                       "frames_used": state.pending_frames_used},
                             "texture": None if prev_same_rev is None else prev_same_rev.get("texture"),
                             "surface_source": ({
                                 "mask_uri": None,
                                 "ratios": {"OBSERVED": 0., "INTERPOLATED": 0., "UNOBSERVED": 1.},
                                 "face_ratios": {"OBSERVED": 0., "UNOBSERVED": 1.},
                             } if prev_same_rev is None else prev_same_rev["surface_source"]),
                             "face_policy": "OBSERVED"}
                    state.asset, state.version = asset, version
                    state.high_version = max(state.high_version, version)
                    self._high_versions[person.world_id] = max(
                        self._high_versions.get(person.world_id, -1), state.high_version
                    )
                    self._last_versions[person.world_id] = max(
                        self._last_versions.get(person.world_id, -1), version
                    )
                    state.history[version] = asset
                    if state.pending_target_frozen:
                        state.shape_frozen = True
                        state.samples = []
                    else:
                        state.shape_frozen = False
                    state.pending_glb = None
                    state.pending_version = None
                    state.pending_target_frozen = False
                    state.pending_frames_used = 0
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
            self._appearance_worker.offer(frame, doc, snapshot, images, rig, {})
        if poses and 0 <= self.clock_ms() - snapshot["produced_at_ms"] <= self.max_age_ms and self.gate.check().admit:
            self._timed("send", self.sender.poses, poses, calibration_version=frame.calibration_version,
                        model_version=self.model.model_version, input_provenance=doc["input_provenance"],
                        produced_at_ms=snapshot["produced_at_ms"])
            self.pose_enqueued += 1
            self._report(f"포즈 대기열 전달 poses={len(poses)}")
        else:
            self._report(f"포즈 없음/신선도·gate 차단 candidates={len(candidates)} "
                         f"selected={len(frame.people)} rejected={len(frame.rejected)}")
