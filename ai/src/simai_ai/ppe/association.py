"""한 카메라·한 프레임에서 PPE 검출을 사람 머리·몸통에 일대일로 맞춘다 (명세 5.5 사람과의 연결).

같은 카메라·같은 프레임(frame_key)·같은 촬영 시각의 Tier 0 관측 2D keypoint 만 쓴다(HMR 추정점·가려진 점 제외).
안전모는 머리(눈·귀) 영역, 조끼는 어깨~골반 영역과 겹쳐야 그 사람의 근거다. 머리 폭은 눈·귀 간격으로 정하되, 두 어깨가
보이면 어깨 폭 × head_min_scale_shoulder_ratio 를 하한으로 둔다(옆·뒷모습에서 눈·귀 간격이 좁게 잡혀 머리에 걸친 검출이
연결되지 않던 것, S15P31A101-737). '선명한 머리'(미착용 근거) 판단은 하한 전의 눈·귀 기준 폭으로 한다. 한 검출이 두 사람 이상에 걸리거나,
같은 사람·같은 장비에 착용·미착용이 함께 나오거나, ID 대응이 불확실하면 그 표본은 UNKNOWN 이다.
어느 사람에게도 맞지 않는 검출(손에 든 안전모, 의자에 걸린 조끼)은 버린다(orphans, 착용으로 세지 않는다).
검출이 없는 것은 미착용 근거가 아니다(NONE).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import math

from .classes import Candidate
from .config import DEFAULT_CONFIG, PpeConfig

# COCO17 순서(contracts common/defs joint_layout)
NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 1, 2, 3, 4
L_SHOULDER, R_SHOULDER, L_HIP, R_HIP = 5, 6, 11, 12

ITEMS = ("helmet", "vest")
WORN, NOT_WORN, UNKNOWN = "WORN", "NOT_WORN", "UNKNOWN"
# 표본(한 카메라·한 프레임·한 사람·한 장비)의 종류
S_WORN, S_NOT_WORN, S_NONE, S_UNKNOWN = "WORN", "NOT_WORN", "NONE", "UNKNOWN"

# 사유 코드(ppe_state.reason). 바꾸면 config.POLICY_VERSION 을 올린다
R_WORN_MATCHED = "WORN_MATCHED"
R_NOT_WORN_MATCHED = "NOT_WORN_MATCHED_CLEAR_REGION"
R_NO_DETECTION = "NO_DETECTION"
R_REGION_NOT_OBSERVABLE = "REGION_NOT_OBSERVABLE"
R_REGION_NOT_CLEAR = "REGION_NOT_CLEAR"
R_SHARED_DETECTION = "SHARED_DETECTION"
R_CONFLICTING_CLASSES = "CONFLICTING_CLASSES"
R_PARTIAL_OVERLAP = "PARTIAL_OPPOSITE_OVERLAP"
R_IDENTITY_UNCERTAIN = "IDENTITY_UNCERTAIN"
R_KEYPOINTS_NOT_OBSERVED = "KEYPOINTS_NOT_OBSERVED"

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class PersonObservation:
    """Tier 0 한 카메라·한 프레임의 사람 관측 1건과 같은 프레임의 local ID → World ID 대응."""

    world_id: str
    identity_revision: int
    local_track_id: str
    keypoints_2d: Sequence[Sequence[float] | None]
    identity_confidence: str = "HIGH"
    joint_layout: str = "COCO17"
    joint_source: str = "observed"
    # World ID 교정 근거(world_state 트랙 값 그대로, S15P31A101-661). 순수 병합이면 누적을 잇는다(state.PpeJudge)
    aliases: tuple[str, ...] = ()
    split_from: str | None = None
    split_source: bool = False   # 같은 world_state 에 이 ID·이 차수로 갈라져 나간 Track 이 있다(identity_carry.split_marks)


@dataclass(frozen=True)
class FrameObservations:
    camera_id: str
    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int
    image_size: tuple[int, int]
    persons: tuple[PersonObservation, ...]


@dataclass(frozen=True)
class FrameDetections:
    """PpeDetector.infer 결과에 그 프레임의 식별자를 붙인 것. 관측과 프레임이 다르면 통째로 버린다."""

    camera_id: str
    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int
    image_size: tuple[int, int]
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True)
class Sample:
    world_id: str
    identity_revision: int
    item: str
    kind: str
    reason: str
    camera_id: str
    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int
    local_track_id: str
    confidence: float | None = None
    region_quality: float | None = None


@dataclass
class FrameAssociation:
    camera_id: str
    stream_epoch: int
    frame_sequence: int
    capture_time_ms: int
    persons: tuple[PersonObservation, ...]
    samples: list[Sample] = field(default_factory=list)
    orphans: list[Candidate] = field(default_factory=list)
    rejected: str | None = None


@dataclass(frozen=True)
class Region:
    box: Box
    scale: float     # 머리: 머리 폭, 몸통: 어깨 폭(px)
    clear: bool      # 미착용 근거로 쓸 만큼 선명한가
    quality: float   # 영역을 만든 keypoint 신뢰도 평균(ppe_state 근거의 region_quality)


def _point(kp, cfg: PpeConfig, image_size) -> tuple[float, float, float] | None:
    if kp is None or len(kp) != 3:
        return None
    u, v, c = (float(x) for x in kp)
    if not all(math.isfinite(x) for x in (u, v, c)) or c < cfg.min_keypoint_confidence:
        return None
    w, h = image_size
    if not (0 <= u < w and 0 <= v < h):
        return None
    return (u, v, c)


def _clip(box: Box, image_size) -> Box:
    w, h = image_size
    return (max(0.0, box[0]), max(0.0, box[1]), min(float(w), box[2]), min(float(h), box[3]))


def _dist(a, b) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def head_region(kps, image_size, cfg: PpeConfig = DEFAULT_CONFIG) -> Region | None:
    pts = {i: _point(kps[i], cfg, image_size) for i in (NOSE, L_EYE, R_EYE, L_EAR, R_EAR)}
    anchors = [pts[i] for i in (L_EYE, R_EYE, L_EAR, R_EAR) if pts[i] is not None]
    if len(anchors) < cfg.head_min_anchor_points:
        return None
    if pts[L_EAR] and pts[R_EAR]:
        scale = _dist(pts[L_EAR], pts[R_EAR])
    elif pts[L_EYE] and pts[R_EYE]:
        scale = _dist(pts[L_EYE], pts[R_EYE]) * cfg.eye_span_to_head_width
    else:
        face = [p for p in pts.values() if p is not None]
        scale = max(_dist(a, b) for a in face for b in face) * cfg.side_span_to_head_width
    scale = max(scale, cfg.head_min_scale_px)
    face_scale = scale   # '선명한 머리' 판단은 눈·귀로 잰 폭으로 한다(어깨 하한으로 작은 얼굴이 선명해지지 않게)
    if cfg.head_min_scale_shoulder_ratio > 0:
        ls, rs = _point(kps[L_SHOULDER], cfg, image_size), _point(kps[R_SHOULDER], cfg, image_size)
        if ls and rs:
            scale = max(scale, cfg.head_min_scale_shoulder_ratio * _dist(ls, rs))
    cx = sum(p[0] for p in anchors) / len(anchors)
    cy = sum(p[1] for p in anchors) / len(anchors)
    box = (cx - cfg.head_expand_side * scale, cy - cfg.head_expand_up * scale,
           cx + cfg.head_expand_side * scale, cy + cfg.head_expand_down * scale)
    face = [p for p in pts.values() if p is not None]
    clear = len(face) >= cfg.head_clear_min_points and face_scale >= cfg.head_clear_min_scale_px
    return Region(_clip(box, image_size), scale, clear, round(sum(p[2] for p in face) / len(face), 3))


def torso_region(kps, image_size, cfg: PpeConfig = DEFAULT_CONFIG) -> Region | None:
    ls, rs = _point(kps[L_SHOULDER], cfg, image_size), _point(kps[R_SHOULDER], cfg, image_size)
    hips = [p for p in (_point(kps[L_HIP], cfg, image_size), _point(kps[R_HIP], cfg, image_size)) if p]
    shoulders = [p for p in (ls, rs) if p]
    if (cfg.torso_require_both_shoulders and len(shoulders) < 2) or not shoulders or len(hips) < cfg.torso_min_hips:
        return None
    pts = shoulders + hips
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    width = _dist(ls, rs) if (ls and rs) else (max(xs) - min(xs))
    height = max(max(p[1] for p in hips) - min(p[1] for p in shoulders), 0.5 * width)
    box = (min(xs) - cfg.torso_expand_side * width, min(ys) - cfg.torso_expand_vertical * height,
           max(xs) + cfg.torso_expand_side * width, max(ys) + cfg.torso_expand_vertical * height)
    return Region(_clip(box, image_size), width, len(pts) == 4, round(sum(p[2] for p in pts) / len(pts), 3))


def _area(b: Box) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _inter(a: Box, b: Box) -> float:
    return _area((max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])))


def overlap_scores(item: str, det: Box, region: Region, cfg: PpeConfig = DEFAULT_CONFIG) -> tuple[float, bool]:
    """(검출 상자 중 영역 안 비율, 일대일 겹침 조건 충족 여부)."""
    det_area = _area(det)
    if det_area <= 0:
        return 0.0, False
    inter = _inter(det, region.box)
    cov = inter / det_area
    ok = cov >= cfg.match_det_coverage
    if item == "helmet":
        lo, hi = cfg.helmet_width_ratio_range
        ok = ok and lo <= (det[2] - det[0]) / region.scale <= hi
    else:
        region_area = _area(region.box)
        ok = ok and region_area > 0 and inter / region_area >= cfg.vest_region_coverage
    return cov, ok


def _frame_mismatch(frame: FrameObservations, det: FrameDetections) -> str | None:
    if det.camera_id != frame.camera_id:
        return "CAMERA_MISMATCH"
    if det.stream_epoch != frame.stream_epoch:
        return "STREAM_EPOCH_MISMATCH"
    if det.frame_sequence != frame.frame_sequence:
        return "FRAME_MISMATCH"
    if det.capture_time_ms != frame.capture_time_ms:
        return "CAPTURE_TIME_MISMATCH"
    if tuple(det.image_size) != tuple(frame.image_size):
        return "IMAGE_SIZE_MISMATCH"
    return None


def associate_frame(frame: FrameObservations, detections: FrameDetections,
                    cfg: PpeConfig = DEFAULT_CONFIG) -> FrameAssociation:
    """같은 프레임의 사람 관측과 검출 후보를 일대일로 맞춰 사람·장비별 표본을 만든다."""
    out = FrameAssociation(frame.camera_id, frame.stream_epoch, frame.frame_sequence, frame.capture_time_ms,
                           frame.persons)
    mismatch = _frame_mismatch(frame, detections)
    if mismatch:
        out.rejected = mismatch
        return out

    persons = frame.persons
    regions: dict[tuple[int, str], Region | None] = {}
    blocked: dict[int, str] = {}
    for pi, p in enumerate(persons):
        if p.identity_confidence == "IDENTITY_UNCERTAIN":
            blocked[pi] = R_IDENTITY_UNCERTAIN
        elif p.joint_layout != "COCO17" or p.joint_source != "observed" or len(p.keypoints_2d) != 17:
            blocked[pi] = R_KEYPOINTS_NOT_OBSERVED
        for item in ITEMS:
            if pi in blocked:
                regions[(pi, item)] = None
            elif item == "helmet":
                regions[(pi, item)] = head_region(p.keypoints_2d, frame.image_size, cfg)
            else:
                regions[(pi, item)] = torso_region(p.keypoints_2d, frame.image_size, cfg)

    matched: dict[tuple[int, str], list[Candidate]] = {}
    partial: dict[tuple[int, str], list[Candidate]] = {}
    shared: set[tuple[int, str]] = set()
    for cand in detections.candidates:
        touched, fits = [], []
        for pi in range(len(persons)):
            region = regions[(pi, cand.item)]
            if region is None:
                continue
            cov, ok = overlap_scores(cand.item, cand.xyxy, region, cfg)
            if cov >= cfg.touch_det_coverage:
                touched.append(pi)
            if ok:
                fits.append(pi)
        if len(touched) >= 2:
            shared.update((pi, cand.item) for pi in touched)
        elif len(fits) == 1:
            matched.setdefault((fits[0], cand.item), []).append(cand)
        elif len(touched) == 1:
            partial.setdefault((touched[0], cand.item), []).append(cand)
        else:
            out.orphans.append(cand)

    for pi, p in enumerate(persons):
        for item in ITEMS:
            kind, reason, best = _person_item(pi, item, blocked, regions, matched, partial, shared)
            region = regions[(pi, item)]
            out.samples.append(Sample(
                world_id=p.world_id, identity_revision=p.identity_revision, item=item, kind=kind, reason=reason,
                camera_id=frame.camera_id, stream_epoch=frame.stream_epoch, frame_sequence=frame.frame_sequence,
                capture_time_ms=frame.capture_time_ms, local_track_id=p.local_track_id,
                confidence=None if best is None else best.confidence,
                region_quality=None if region is None else region.quality,
            ))
    return out


def _person_item(pi, item, blocked, regions, matched, partial, shared) -> tuple[str, str, Candidate | None]:
    if pi in blocked:
        return S_UNKNOWN, blocked[pi], None
    region = regions[(pi, item)]
    if region is None:
        return S_UNKNOWN, R_REGION_NOT_OBSERVABLE, None
    if (pi, item) in shared:
        return S_UNKNOWN, R_SHARED_DETECTION, None
    hits = matched.get((pi, item), [])
    worn = [c for c in hits if c.candidate_state == WORN]
    not_worn = [c for c in hits if c.candidate_state == NOT_WORN]
    if worn and not_worn:
        return S_UNKNOWN, R_CONFLICTING_CLASSES, None
    near = {c.candidate_state for c in partial.get((pi, item), [])}
    if worn:
        if NOT_WORN in near:
            return S_UNKNOWN, R_PARTIAL_OVERLAP, None
        return S_WORN, R_WORN_MATCHED, max(worn, key=lambda c: c.confidence)
    if not_worn:
        if WORN in near:
            return S_UNKNOWN, R_PARTIAL_OVERLAP, None
        if not region.clear:
            return S_UNKNOWN, R_REGION_NOT_CLEAR, None
        return S_NOT_WORN, R_NOT_WORN_MATCHED, max(not_worn, key=lambda c: c.confidence)
    return S_NONE, R_NO_DETECTION, None
