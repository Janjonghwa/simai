"""외형 전용 작업자. 대기 요청·완료 결과는 각각 최신 한 회차만 유지한다."""

from collections import Counter
from copy import deepcopy
from dataclasses import replace
import logging
import threading
from types import SimpleNamespace


log = logging.getLogger(__name__)

# 기본은 감속(권장 주기)으로 외형 분할을 미루지 않고, 아바타 게이트가 닫혔을 때만 미룬다(None = 감속 무시).
# 17차처럼 판정 p95 가 오래 100~200 ms 면 권장 주기가 계속 800 ms 라, 감속으로 미루면 새 사람의 외형이 끝내 안 생긴다.
# 아바타는 시연 필수다(Lead 결정 2026-11-19, -808). Tier 0 보호는 게이트(예산 초과 10 s 유지·재개 직후 재차단, -807)가 맡는다.
DEFAULT_SEG_MAX_INTERVAL_MS = None
DEFAULT_DEFER_INTERVAL_S = 0.2


def owned_images(snapshot, images):
    """외형 작업자가 따로 쥘 영상 사본(S15P31A101-755).

    공유 통로 읽기(`LatestChannelReader`)는 카메라마다 3칸 고리 버퍼를 다시 쓴다(-650). 다음 읽기들이 같은 칸의
    이전 크롭을 0으로 지우고 새 크롭을 덮으므로, 비동기 외형 회차가 참조만 쥐고 있으면 누적 도중 검정(0)·다른 시각
    픽셀을 관측으로 적는다(19차 세션: 관측 텍셀의 최대 43%가 RGB 0). 크롭 표(`crops_xyxy`)가 있으면 크롭만 새 배열로
    옮기고(크롭 밖은 원래도 0), 없으면 통째로 복사한다.
    """
    import numpy as np

    frames = snapshot.get("frames") if isinstance(snapshot, dict) else None
    owned = {}
    for cid, image in images.items():
        array = np.asarray(image)
        ref = frames.get(cid) if isinstance(frames, dict) else None
        crops = ref.get("crops_xyxy") if isinstance(ref, dict) else None
        if array.ndim != 3 or not isinstance(crops, (list, tuple)):
            owned[cid] = array.copy()
            continue
        height, width = array.shape[:2]
        copy = np.zeros(array.shape, array.dtype)
        for box in crops:
            if not isinstance(box, (list, tuple)) or len(box) != 4:
                copy = array.copy()
                break
            x0, y0, x1, y1 = (int(v) for v in box)
            x0, x1 = max(0, x0), min(width, x1)
            y0, y1 = max(0, y0), min(height, y1)
            if x1 > x0 and y1 > y0:
                copy[y0:y1, x0:x1] = array[y0:y1, x0:x1]
        owned[cid] = copy
    return owned


class AppearanceWorker:
    def __init__(self, loop, *, seg_max_interval_ms=DEFAULT_SEG_MAX_INTERVAL_MS,
                 defer_interval_s=DEFAULT_DEFER_INTERVAL_S):
        self.loop = loop
        self.pipeline = loop.appearance
        self.seg_max_interval_ms = None if seg_max_interval_ms is None else int(seg_max_interval_ms)
        self._defer_interval_s = float(defer_interval_s)
        self._condition = threading.Condition()
        self._pending = self._result = None
        self._stopped = False
        self.dropped = 0
        self.deferred = 0
        self.deferred_reasons = Counter()
        self.discarded_results = 0
        self._registered = {}
        self._thread = threading.Thread(target=self._run, name="avatar-appearance", daemon=True)
        self._thread.start()

    def offer(self, frame, doc, snapshot, images, rig, masks):
        # rig·baked는 생산 뒤 불변이라 참조를 넘기고 가변 자산만 분리한다. 영상은 읽기 고리 버퍼가 다시 쓰므로 사본을 쥔다(-755).
        images = owned_images(snapshot, images)
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

    def _gate_state(self, *, check_throttle: bool) -> tuple[bool, str]:
        """아바타 게이트·감속 상태를 다시 확인한다(S15P31A101-808).

        check_throttle=True 이고 seg_max_interval_ms 가 있으면(기본 None = 끔) 권장 주기가 그 값을 넘을 때도 분할을 미룬다.
        """
        if not self.loop.gate.check().admit:
            return False, "gate_closed"
        if check_throttle and self.seg_max_interval_ms is not None:
            rec_fn = getattr(self.loop.gate, "recommended_interval_ms", None)
            if callable(rec_fn) and int(rec_fn()) > self.seg_max_interval_ms:
                return False, "throttled"
        return True, "ok"

    def _defer(self, item, reason: str) -> None:
        """닫힌 게이트·감속으로 미룬 작업을 버리지 않고 대기 슬롯에 되돌린 뒤 다음 주기를 기다린다."""
        with self._condition:
            if self._stopped:
                return
            if self._pending is None:
                self._pending = item
            self.deferred += 1
            self.deferred_reasons[reason] += 1
            self._condition.wait(timeout=self._defer_interval_s)

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._stopped or self._pending is not None)
                if self._stopped:
                    return
                item, self._pending = self._pending, None
            tokens, states, frame, doc, snapshot, images, rig, masks = item
            assets = []
            if not self._valid(tokens):
                continue
            ok, reason = self._gate_state(check_throttle=False)
            if not ok:
                self._defer(item, reason)
                continue
            admit = lambda: self._valid(tokens) and self.loop.gate.check().admit
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
                    if seg_jobs:
                        if not self._valid(tokens):
                            continue
                        seg_ok, seg_reason = self._gate_state(check_throttle=True)
                        if not seg_ok:
                            self._defer(item, seg_reason)
                            continue
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
