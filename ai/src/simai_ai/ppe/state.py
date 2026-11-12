"""World ID·identity_revision 별 안전모·조끼 상태 누적 (명세 5.5, 6장 PPE 상태).

근거·칸의 시간은 촬영 시각(UTC ms)이다. 500 ms 칸마다 카메라당 표본 하나를 모아 다시점 비대칭 규칙으로 칸 결과를 정하고,
유효 칸 6개 창으로 상태를 바꾼다. 착용은 창 안 5칸 이상이 착용이고 마지막 유효 칸이 착용일 때, 미착용은 6칸 모두 미착용일 때다.
반대 근거가 나오면 곧바로 반대 상태로 가지 않고 먼저 UNKNOWN 으로 두고 창을 다시 쌓는다(깜빡임 방지).

유효 칸은 그 사람의 ID 대응이 불확실하지 않은 칸이다(S15P31A101-653). 한 프레임이라도 IDENTITY_UNCERTAIN(또는 분할 대기·swap
의심 보류)인 칸은 건너뛴다: 창에 넣지 않고 근거로도 쓰지 않되 확정 상태는 유지한다(근거를 더하지 않으므로 만료 시각은 늘지 않는다).
유효 6칸은 최근 window_span_bins 칸(5초) 안에 있어야 한다. 상태를 바로 초기화하는 것은 identity_revision 이 오를 때, 같은 카메라
local ID 의 주인이 바뀔 때, ID 불확실이 uncertain_reset_ms(3초)를 넘게 이어질 때다. 만료·트랙 정리는 현재 UTC로 판단해 마지막
유효 근거 + 10초가 지나면 UNKNOWN 이다. 이 모듈은 World 위치·ID 를 만들거나 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .association import (
    ITEMS,
    NOT_WORN,
    R_CONFLICTING_CLASSES,
    R_IDENTITY_UNCERTAIN,
    R_KEYPOINTS_NOT_OBSERVED,
    R_NO_DETECTION,
    R_PARTIAL_OVERLAP,
    R_REGION_NOT_CLEAR,
    R_REGION_NOT_OBSERVABLE,
    R_SHARED_DETECTION,
    S_NOT_WORN,
    S_WORN,
    UNKNOWN,
    WORN,
    FrameAssociation,
    Sample,
)
from .config import DEFAULT_CONFIG, PpeConfig

# 칸(500 ms) 결과
B_WORN, B_NOT_WORN, B_CONFLICT, B_UNKNOWN, B_EMPTY = "WORN", "NOT_WORN", "CONFLICT", "UNKNOWN", "EMPTY"

R_ACCUMULATING = "ACCUMULATING"
R_WINDOW_WORN = "WINDOW_WORN"
R_WINDOW_NOT_WORN = "WINDOW_NOT_WORN"
R_CONTRARY = "CONTRARY_EVIDENCE"
R_CROSS_CAMERA_CONFLICT = "CROSS_CAMERA_CONFLICT"
R_EXPIRED = "EVIDENCE_EXPIRED"
R_REVISION_CHANGED = "IDENTITY_REVISION_CHANGED"
R_MAPPING_CHANGED = "IDENTITY_MAPPING_CHANGED"
R_NO_OBSERVATION = "NO_OBSERVATION"

RESET_REASONS = (R_REVISION_CHANGED, R_MAPPING_CHANGED, R_IDENTITY_UNCERTAIN)
# UNKNOWN 으로 다시 쌓는 동안 칸 사유로 덮지 않는 사유. ID 불확실은 유효 칸이 다시 들어오면 그 칸의 사유로 바꾼다
_STICKY_REASONS = (R_CONTRARY, R_CROSS_CAMERA_CONFLICT, R_REVISION_CHANGED, R_MAPPING_CHANGED)
_REASON_PRIORITY = (R_SHARED_DETECTION, R_CONFLICTING_CLASSES, R_PARTIAL_OVERLAP, R_IDENTITY_UNCERTAIN,
                    R_KEYPOINTS_NOT_OBSERVED, R_REGION_NOT_CLEAR, R_REGION_NOT_OBSERVABLE, R_NO_DETECTION)


@dataclass
class ItemState:
    state: str = UNKNOWN
    reason: str = R_ACCUMULATING
    window_start_bin: int | None = None       # 이 칸 이전은 창에 넣지 않는다(초기화·반대 근거 뒤 다시 쌓기)
    last_processed_bin: int | None = None
    bins: dict[int, list[Sample]] = field(default_factory=dict)   # 칸 → 카메라별 첫 표본
    results: dict[int, str] = field(default_factory=dict)
    last_evidence_at: int | None = None
    evidence: list[Sample] = field(default_factory=list)


@dataclass
class TrackState:
    world_id: str
    identity_revision: int
    items: dict[str, ItemState]
    last_seen_ms: int
    skipped_bins: set[int] = field(default_factory=set)   # ID 불확실 프레임이 있던 칸(두 장비 모두 건너뜀)
    uncertain_since: int | None = None   # 지금 이어지는 ID 불확실의 첫 촬영 시각. 확실한 프레임이 오면 None
    uncertain_reset: bool = False        # 이번 불확실 구간에서 이미 초기화했는지


@dataclass(frozen=True)
class ItemView:
    state: str
    reason: str
    evidence: tuple[Sample, ...]
    last_evidence_at: int | None
    expires_at: int | None
    valid_cells: int
    support_ratio: float


class PpeJudge:
    """관측·검출 연결 결과(FrameAssociation)를 받아 사람·장비별 상태를 낸다. 스레드 하나에서 쓴다."""

    def __init__(self, cfg: PpeConfig = DEFAULT_CONFIG) -> None:
        self.cfg = cfg
        self.tracks: dict[str, TrackState] = {}
        self.camera_epoch: dict[str, int] = {}
        self.local_owner: dict[tuple[str, int, str], tuple[str, int]] = {}
        self.dropped: dict[str, int] = {}
        self.last_closed_bin: int | None = None
        self.latest_capture_time_ms = 0  # 접수한 프레임의 촬영 진행 시각. 입력이 멈추면 함께 멈춘다.

    # ---- 입력 -------------------------------------------------------------------------------
    def ingest(self, assoc: FrameAssociation) -> None:
        if assoc.rejected:
            self._drop(assoc.rejected)
            return
        cid, epoch, t = assoc.camera_id, assoc.stream_epoch, assoc.capture_time_ms
        known = self.camera_epoch.get(cid)
        if known is not None and epoch < known:
            self._drop("STALE_STREAM_EPOCH")
            return
        if known is not None and epoch > known:
            # 새 stream_epoch: 그 카메라의 local ID 주인 기록은 무효(Local Tracker 가 새로 시작)
            self.local_owner = {k: v for k, v in self.local_owner.items() if k[0] != cid}
        self.camera_epoch[cid] = epoch
        b = t // self.cfg.bin_ms
        if self.last_closed_bin is not None and b <= self.last_closed_bin:
            self._drop("LATE_FRAME")
            return
        self.latest_capture_time_ms = max(self.latest_capture_time_ms, t)

        accepted: set[str] = set()
        for p in assoc.persons:
            track = self.tracks.get(p.world_id)
            if track is None:
                track = self.tracks[p.world_id] = TrackState(p.world_id, p.identity_revision,
                                                             {i: ItemState() for i in ITEMS}, t)
            if p.identity_revision < track.identity_revision:
                self._drop("STALE_IDENTITY_REVISION")
                continue
            if p.identity_revision > track.identity_revision:
                self._reset(track, R_REVISION_CHANGED, b)
                track.identity_revision = p.identity_revision
            uncertain = p.identity_confidence == "IDENTITY_UNCERTAIN"
            if uncertain:
                # 불확실한 칸은 건너뛰고 확정 상태는 둔다. 불확실이 uncertain_reset_ms 를 넘게 이어지면 그때 초기화한다
                track.skipped_bins.add(b)
                if track.uncertain_since is None or t < track.uncertain_since:
                    track.uncertain_since = t
                if not track.uncertain_reset and t - track.uncertain_since > self.cfg.uncertain_reset_ms:
                    self._reset(track, R_IDENTITY_UNCERTAIN, b)
                    track.uncertain_reset = True
                for item in track.items.values():   # 확정 전이면 사유를 보인다(확정 상태는 건드리지 않는다)
                    if item.state == UNKNOWN and item.reason not in _STICKY_REASONS:
                        item.reason = R_IDENTITY_UNCERTAIN
            elif track.uncertain_since is not None and t > track.uncertain_since:
                track.uncertain_since, track.uncertain_reset = None, False
            key = (cid, epoch, p.local_track_id)
            owner = self.local_owner.get(key)
            if owner is not None and owner[0] != p.world_id and t - owner[1] <= self.cfg.mapping_ttl_ms:
                self._reset(track, R_MAPPING_CHANGED, b)
                if owner[0] in self.tracks:
                    self._reset(self.tracks[owner[0]], R_MAPPING_CHANGED, b)
            self.local_owner[key] = (p.world_id, t)
            track.last_seen_ms = max(track.last_seen_ms, t)
            if not uncertain:   # 불확실한 프레임의 표본은 창에도 근거에도 넣지 않는다
                accepted.add(p.world_id)

        for s in assoc.samples:
            track = self.tracks.get(s.world_id)
            if s.world_id not in accepted or track is None or s.identity_revision != track.identity_revision:
                continue
            item = track.items[s.item]
            if item.window_start_bin is not None and b < item.window_start_bin:
                continue
            per_bin = item.bins.setdefault(b, [])
            if any(x.camera_id == cid for x in per_bin):
                self._drop("SECOND_SAMPLE_SAME_CAMERA_BIN")   # 칸당 카메라 1표본(카메라당 2 Hz 상한과 같은 뜻)
                continue
            per_bin.append(s)

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def _reset(self, track: TrackState, reason: str, start_bin: int) -> None:
        for item in track.items.values():
            item.state, item.reason = UNKNOWN, reason
            item.window_start_bin = start_bin
            item.bins, item.results = {}, {}
            item.last_evidence_at, item.evidence = None, []

    # ---- 칸 결합 ------------------------------------------------------------------------------
    def combine_bin(self, samples: Sequence[Sample]) -> tuple[str, list[Sample]]:
        """다시점 규칙: 착용·미착용이 함께면 충돌, 모호한 근거가 있는 칸은 미착용으로 세지 않는다."""
        worn = [s for s in samples if s.kind == S_WORN]
        not_worn = [s for s in samples if s.kind == S_NOT_WORN]
        if worn and not_worn:
            return B_CONFLICT, worn + not_worn
        decisive = worn or not_worn
        if decisive:
            anchor = max(s.capture_time_ms for s in decisive)
            near = [s for s in decisive if anchor - s.capture_time_ms <= self.cfg.max_camera_skew_ms]
            if worn:
                return B_WORN, near
            ambiguous = [s for s in samples if s.reason in (R_SHARED_DETECTION, R_CONFLICTING_CLASSES,
                                                             R_PARTIAL_OVERLAP)]
            if self.cfg.ambiguity_blocks_not_worn and ambiguous:
                return B_UNKNOWN, []
            return B_NOT_WORN, near
        if not samples:
            return B_EMPTY, []
        return B_UNKNOWN, []

    # ---- 평가 -------------------------------------------------------------------------------
    def evaluate(self, now_ms: int, *, capture_time_ms: int | None = None) -> dict[str, dict[str, ItemView]]:
        """촬영 진행 시각으로 칸을 닫고 현재 UTC(now_ms)로 근거를 만료한다.

        동기 입력·오프라인 호출은 capture_time_ms를 생략해 기존처럼 now_ms까지 칸을 진행할 수 있다.
        비동기 서비스는 latest_capture_time_ms를 넘긴다. 처리 지연이나 입력 중단이 촬영 시계를 앞당기지 않는다.
        """
        cfg = self.cfg
        capture_now = now_ms if capture_time_ms is None else min(capture_time_ms, now_ms)
        last_closed = (capture_now - cfg.bin_close_grace_ms) // cfg.bin_ms - 1
        if self.last_closed_bin is None or last_closed > self.last_closed_bin:
            self.last_closed_bin = last_closed
        last_closed = self.last_closed_bin  # 순서 역전·촬영 시계 정지에도 닫은 칸을 다시 열지 않는다.
        out: dict[str, dict[str, ItemView]] = {}
        for wid in list(self.tracks):
            track = self.tracks[wid]
            if now_ms - track.last_seen_ms > cfg.track_retention_ms:
                del self.tracks[wid]
                continue
            out[wid] = {name: self._evaluate_item(item, track.skipped_bins, last_closed, now_ms)
                        for name, item in track.items.items()}
            # 창 범위보다 오래된 건너뜀 칸은 더 볼 일이 없다
            track.skipped_bins = {k for k in track.skipped_bins if k > last_closed - cfg.window_span_bins}
        return out

    def _evaluate_item(self, item: ItemState, skipped: set[int], last_closed: int, now_ms: int) -> ItemView:
        cfg = self.cfg
        if item.window_start_bin is None and item.bins:
            item.window_start_bin = min(item.bins)
        if item.window_start_bin is not None:
            start = item.window_start_bin if item.last_processed_bin is None else item.last_processed_bin + 1
            for b in range(max(start, item.window_start_bin), last_closed + 1):
                self._step(item, b, skipped)
                item.last_processed_bin = b
        if item.state in (WORN, NOT_WORN) and item.last_evidence_at is not None \
                and now_ms > item.last_evidence_at + cfg.expiry_ms:
            expired_at = item.last_evidence_at
            item.state, item.reason = UNKNOWN, R_EXPIRED
            item.window_start_bin = last_closed + 1
            item.results = {k: v for k, v in item.results.items() if k > last_closed}
            item.evidence, item.last_evidence_at = [], None
            return ItemView(UNKNOWN, R_EXPIRED, (), expired_at, None, 0, 0.0)
        window = self._window(item, last_closed, skipped)
        valid = [r for r in window if r in (B_WORN, B_NOT_WORN)]
        support = 0.0
        if valid and item.state in (WORN, NOT_WORN):
            target = B_WORN if item.state == WORN else B_NOT_WORN
            support = round(sum(r == target for r in valid) / len(valid), 3)
        expires = None if item.last_evidence_at is None or item.state == UNKNOWN \
            else item.last_evidence_at + cfg.expiry_ms
        evidence = tuple(item.evidence) if item.state in (WORN, NOT_WORN) else ()
        return ItemView(item.state, item.reason, evidence, item.last_evidence_at if evidence else None, expires,
                        len(valid), support)

    def _window(self, item: ItemState, last_b: int, skipped: set[int]) -> list[str]:
        """최근 window_span_bins 칸 안에서 건너뛰지 않은 칸의 결과 중 마지막 window_bins 개(오래된 것부터)."""
        if item.window_start_bin is None:
            return []
        lo = max(item.window_start_bin, last_b - self.cfg.window_span_bins + 1)
        valid = [b for b in range(lo, last_b + 1) if b not in skipped]
        return [item.results.get(b, B_EMPTY) for b in valid[-self.cfg.window_bins:]]

    def _step(self, item: ItemState, b: int, skipped: set[int]) -> None:
        cfg = self.cfg
        samples = item.bins.pop(b, [])
        if b in skipped:
            # ID 대응이 불확실한 칸: 결과도 근거도 남기지 않고 확정 상태는 그대로 둔다
            if item.state == UNKNOWN and item.reason not in _STICKY_REASONS:
                item.reason = R_IDENTITY_UNCERTAIN
            return
        result, support = self.combine_bin(samples)
        item.results[b] = result
        for old in [k for k in item.results if k <= b - cfg.window_span_bins]:
            del item.results[old]
        contrary = (item.state == WORN and result in (B_NOT_WORN, B_CONFLICT)) or \
                   (item.state == NOT_WORN and result in (B_WORN, B_CONFLICT))
        if contrary:
            item.state = UNKNOWN
            item.reason = R_CROSS_CAMERA_CONFLICT if result == B_CONFLICT else R_CONTRARY
            item.window_start_bin = b
            item.results = {b: result}
            item.last_evidence_at, item.evidence = None, []
            return
        window = self._window(item, b, skipped)
        full = len(window) == cfg.window_bins
        if item.state != WORN and full and result == B_WORN and window.count(B_WORN) >= cfg.worn_min_bins:
            item.state, item.reason = WORN, R_WINDOW_WORN
        elif item.state != NOT_WORN and full and all(r == B_NOT_WORN for r in window):
            item.state, item.reason = NOT_WORN, R_WINDOW_NOT_WORN
        elif item.state == UNKNOWN and item.reason not in _STICKY_REASONS:
            item.reason = _bin_reason(result, samples)
        if (item.state == WORN and result == B_WORN) or (item.state == NOT_WORN and result == B_NOT_WORN):
            item.last_evidence_at = max(s.capture_time_ms for s in support)
            item.evidence = (list(support) + item.evidence)[: cfg.evidence_max_items]


def _bin_reason(result: str, samples: Sequence[Sample]) -> str:
    """UNKNOWN 으로 쌓는 중의 사유: 결정 칸이면 ACCUMULATING, 아니면 그 칸의 가장 무거운 사유."""
    if result == B_CONFLICT:
        return R_CROSS_CAMERA_CONFLICT
    if result in (B_WORN, B_NOT_WORN):
        return R_ACCUMULATING
    if not samples:
        return R_NO_OBSERVATION
    reasons = {s.reason for s in samples}
    return next((r for r in _REASON_PRIORITY if r in reasons), R_ACCUMULATING)
