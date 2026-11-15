"""외형 전용 작업자. 대기 요청·완료 결과는 각각 최신 한 회차만 유지한다."""

from copy import deepcopy
from dataclasses import replace
import logging
import threading
from types import SimpleNamespace

log = logging.getLogger(__name__)


class AppearanceWorker:
    def __init__(self, loop):
        self.loop = loop
        self.pipeline = loop.appearance
        self._condition = threading.Condition()
        self._pending = self._result = None
        self._stopped = False
        self.dropped = 0
        self.discarded_results = 0
        self._registered = {}
        self._thread = threading.Thread(target=self._run, name="avatar-appearance", daemon=True)
        self._thread.start()

    def offer(self, frame, doc, snapshot, images, rig, masks):
        # 영상·rig·baked는 생산 뒤 불변. 큰 배열 복사 없이 참조를 넘기고 가변 자산만 분리한다.
        with self.loop._state_lock:
            tokens = {wid: (st, st.revision, st.generation) for wid, st in self.loop.people.items()}
            states = {wid: replace(st, asset=deepcopy(st.asset), history=dict(st.history), samples=[])
                      for wid, st in self.loop.people.items()}
        with self._condition:
            if self._stopped:
                return
            self.dropped += int(self._pending is not None)
            self._pending = (tokens, states, frame, doc, snapshot, images, rig, masks)
            self._condition.notify()

    def _valid(self, tokens):
        with self.loop._state_lock:
            return not self._stopped and all(
                self.loop.people.get(wid) is state and state.revision == revision and state.generation == generation
                for wid, (state, revision, generation) in tokens.items())

    def take(self):
        with self._condition:
            result, self._result = self._result, None
            return result

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    return
                item, self._pending = self._pending, None
            tokens, states, frame, doc, snapshot, images, rig, masks = item
            assets = []
            admit = lambda: self._valid(tokens) and self.loop.gate.check().admit
            if not admit():
                continue
            try:
                active = set(states)
                self.pipeline.retain(active)
                self._registered = {k: v for k, v in self._registered.items() if k in active}
                for track in doc["tracks"]:
                    wid = track["world_id"]
                    if wid not in states:
                        continue
                    state = states[wid]
                    # 순수 병합으로 차수만 오른 사람은 같은 사람이다: 누적을 잇는 기준은 revision_base(S15P31A101-661)
                    base = getattr(state, "revision_base", state.revision)
                    self.pipeline.track(track, doc["t"], identity_key=base)
                    original, revision, generation = tokens[wid]
                    key = (id(original), base, generation, id(state.baked))
                    if state.baked is not None and self._registered.get(wid) != key:
                        self.pipeline.register(wid, state, doc["t"])
                        self._registered[wid] = key
                    # 병합·재연결·이어받기로 받은 텍셀을 합친다(S15P31A101-743). 누적이 아직 없으면 다음 회차까지 기다린다
                    apply_carry = getattr(self.pipeline, "apply_carry", None)
                    if apply_carry is not None:
                        apply_carry(wid)
                    # 유계 큐에서 생략한 회차도 포즈 루프가 센 연속 안정 30프레임에 포함한다.
                    acc = getattr(self.pipeline, "people", {}).get(wid)
                    if acc is not None:
                        acc.stable = state.stable
                        acc.ready = state.stable >= self.pipeline.config.freeze_frames

                seg = getattr(self.loop, "segmentation", None)
                if not masks and seg is not None and not getattr(seg, "failed", False) and rig is not None:
                    has_people_map = isinstance(getattr(self.pipeline, "people", None), dict)
                    pending_map = getattr(self.pipeline, "pending", {}) or {}
                    seg_jobs = []
                    for track in doc["tracks"]:
                        wid = track["world_id"]
                        if wid not in states:
                            continue
                        if (
                            track.get("identity_confidence") == "IDENTITY_UNCERTAIN"
                            or track.get("avatar_update_hold", False)
                            or track.get("hold_appearance_update", False)
                        ):
                            continue
                        if has_people_map:
                            acc = self.pipeline.people.get(wid)
                            if (
                                acc is None
                                or not acc.ready
                                or wid in pending_map
                                or (acc.last_sample >= 0 and doc["t"] - acc.last_sample < acc.config.sample_interval_ms)
                            ):
                                continue
                        for obs in track.get("per_camera", []):
                            cid = obs["camera_id"]
                            ref = (snapshot.get("frames") or {}).get(cid) if isinstance(snapshot, dict) else None
                            if (
                                cid not in images
                                or cid not in rig.cameras
                                or ref is None
                                or ref["frame_ref"] != obs["frame_ref"]
                                or abs(doc["t"] - ref["capture_time_ms"]) > 100
                            ):
                                continue
                            seg_jobs.append((track, obs))
                    if seg_jobs and admit():
                        prepared = self.loop._timed("segmentation", seg.prepare, seg_jobs, images, rig)
                        masks = {
                            key: val.original_mask
                            for key, val in prepared.items()
                            if getattr(val, "original_mask", None) is not None
                        }

                def reserve(wid):
                    with self.loop._state_lock:
                        if not self._valid(tokens):
                            raise RuntimeError("외형 요청의 신원이 바뀜")
                        return self.loop._reserve_version(self.loop.people[wid], wid)

                sender = SimpleNamespace(epoch=self.loop.sender.epoch, asset=assets.append)
                self.loop._timed("appearance", self.pipeline.update, frame, doc, snapshot, images, rig, states,
                                 self.loop._counted_upload, sender, self.loop.clock_ms, admit, masks,
                                 reserve_version=reserve)
            except Exception as error:
                log.warning("외형 갱신 실패: %s; 마지막 확정 외형 유지", type(error).__name__)
            # 부분 성공 뒤 다른 사람 업로드가 실패한 경우에도 성공한 자산은 반영한다.
            if assets:
                valid = admit()
                with self._condition:
                    if valid and not self._stopped:
                        # 다음 대기 회차가 다른 사람만 갱신해도 아직 전달하지 않은 자산을 잃지 않는다.
                        previous_tokens, previous_assets = self._result or ({}, [])
                        merged = {a["world_id"]: a for a in previous_assets if a["world_id"] in states}
                        merged.update({a["world_id"]: a for a in assets})
                        result_tokens = {wid: previous_tokens[wid] for wid in merged if wid in previous_tokens}
                        result_tokens.update({a["world_id"]: tokens[a["world_id"]] for a in assets})
                        self._result = (result_tokens, list(merged.values()))
                    else:
                        self.discarded_results += 1

    def close(self):
        with self._condition:
            self._stopped = True
            self._pending = self._result = None
            self._condition.notify()
        # admit 콜백으로 계산을 중단한다. 진행 중 HTTP는 transport의 30초 timeout 뒤 회수된다.
        self._thread.join()
