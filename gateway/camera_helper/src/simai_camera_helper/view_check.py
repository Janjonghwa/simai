"""재사용 시점 확인(S15P31A101-712): 이전 맵 기준 사진 ↔ 지금 키프레임을 같은 카메라끼리 비교한다.

「이전 맵 재사용」 대상 맵(현재 관제 맵)을 구운 카메라별 사람 지운 배경 사진을 EC2 가 수요 응답 `reuse_reference` 로 준다(서명 주소,
그 Scene 보정의 렌즈 K·왜곡). 같은 카메라라면 두 장면 사이는 단일 호모그래피이고, 카메라가 그대로면 거의 항등이다.

- SIFT 상호 최근접(Lowe 0.75) 매칭 → 호모그래피 RANSAC → K⁻¹HK 의 가까운 회전 = Tier 0 배경 회전 감시(-540
  `simai_ai.calibration.rotation_recovery.fit_rotation`)와 같은 방법·한도(매칭 40, 인라이어 0.6, 특이값 비 0.98)다.
- 회전 한도는 재사용 World 다시 맞추기(-584 `ReanchorConfig.max_orientation_change_rad`) 10° 를 쓴다(그 이상이면 워커가 어차피 거부한다).
- 한 호모그래피로 설명되지 않으면(시차) 위치 이동(MOVED), 같은 배경 특징점이 거의 안 맞으면 VIEW_DIFFERENT 다.
- 가림·조명으로 보이는 경우(맞는 점은 그대로인데 수가 적음)는 UNKNOWN(OCCLUDED)이라 막지 않는다.
- 한 번의 CHANGED 는 확인 중(UNKNOWN·CONFIRMING)이고, 그 카메라의 다른 키프레임에서 한 번 더 CHANGED 여야 CHANGED 로 싣는다.

S15P31A101-714: 재사용 후보 맵 여러 개(EC2 수요 응답 `reuse_references`, 최대 5개)를 맵마다 따로 비교한다(`MultiViewChecker`).
맵마다 기준 사진·렌즈가 달라 ViewChecker 를 맵(scene·보정)별로 하나씩 두고, 목록에서 빠진 맵의 것은 버린다.

이 모듈은 위치·자세·보정을 만들지 않는다(재사용 선택을 막을지의 근거만). 영상·특징점은 메시지에 싣지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .judge import CAMERAS

LOG = logging.getLogger(__name__)

MAX_REFERENCE_BYTES = 16 * 1024 * 1024
REFERENCE_RETRY_S = 30.0
MAX_REFERENCES = 5
"""한 라운드에 비교하는 후보 맵 수 상한(coverage_guide 1.2.0 reuse_view_checks maxItems 와 같다)"""
CHANGED_REASONS = ("ROTATED", "MOVED", "VIEW_DIFFERENT")
DISTORTION_MODELS = ("NONE", "OPENCV", "OPENCV_PINHOLE", "OPENCV_FISHEYE")


@dataclass(frozen=True)
class ViewCheckConfig:
    max_image_side: int = 960
    """분석 해상도(긴 변 px). -540 RotationConfig.max_image_side 와 같다"""
    max_features: int = 1600
    min_matches: int = 40
    min_reference_features: int = 80
    min_inlier_ratio: float = 0.6
    max_residual_px: float = 3.0
    """분석 해상도 px. -540 은 원본 해상도 px 로 재지만, 누적 배경이 아닌 키프레임 한 장은 압축·잡음으로 점 위치가 더 흔들려 분석 해상도
    기준(1296p 원본의 약 7 px)으로 둔다. 15 cm 이동(-584 배치 잔차 한도)의 시차는 깊이 3~8 m 방에서 분석 해상도 약 12 px 라 그래도 걸린다"""
    min_singular_ratio: float = 0.98
    max_rotation_rad: float = math.radians(10)
    stationary_ratio: float = 0.5
    """대응의 이 비율 이상이 제자리(≤ max_residual_px)면 카메라가 그대로다(가구·사람이 바뀐 일부 점은 무시)"""


@dataclass(frozen=True)
class Lens:
    K: object
    model: str
    coeffs: object
    image_size: tuple[int, int]


@dataclass(frozen=True)
class ViewFeatures:
    points: object
    descriptors: object
    count: int


@dataclass(frozen=True)
class Verdict:
    status: str
    reason: str | None = None
    matches: int | None = None
    inlier_ratio: float | None = None
    rotation_rad: float | None = None

    def record(self, camera_id: str) -> dict:
        return {
            "camera_id": camera_id,
            "status": self.status,
            "reason": self.reason,
            "matches": self.matches,
            "inlier_ratio": None if self.inlier_ratio is None else round(float(self.inlier_ratio), 4),
            "rotation_rad": None if self.rotation_rad is None else round(float(self.rotation_rad), 4),
        }


class StreamMismatch(ValueError):
    """영상의 가로세로 비가 기준 렌즈 내부값과 다르다(다른 스트림·해상도)."""


def parse_lens(camera: dict) -> Lens:
    import numpy as np

    K = np.asarray(camera["K"], dtype=np.float64)
    distortion = camera.get("distortion") or {}
    model = distortion.get("model", "NONE")
    coeffs = np.asarray(distortion.get("coeffs") or [], dtype=np.float64)
    size = camera["image_size"]
    if (K.shape != (3, 3) or not np.isfinite(K).all() or K[0, 0] <= 0 or K[1, 1] <= 0
            or not np.allclose(K[2], [0, 0, 1]) or model not in DISTORTION_MODELS
            or coeffs.ndim != 1 or not np.isfinite(coeffs).all()
            or len(size) != 2 or not all(isinstance(v, int) and v > 0 for v in size)):
        raise ValueError("invalid reference lens")
    return Lens(K, model, coeffs, (int(size[0]), int(size[1])))


def analysis_size(lens: Lens, cfg: ViewCheckConfig) -> tuple[int, int]:
    w, h = lens.image_size
    scale = min(1.0, cfg.max_image_side / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def analysis_K(lens: Lens, cfg: ViewCheckConfig):
    import numpy as np

    w, h = analysis_size(lens, cfg)
    return np.diag([w / lens.image_size[0], h / lens.image_size[1], 1.0]) @ lens.K


def extract(image, lens: Lens, cfg: ViewCheckConfig) -> ViewFeatures:
    """같은 분석 크기로 줄여 SIFT, 점은 분석 해상도 K 로 왜곡을 지운다(두 사진이 같은 좌표계)."""
    import cv2
    import numpy as np

    if getattr(image, "ndim", 0) not in (2, 3) or image.dtype != np.uint8:
        raise ValueError("expected a uint8 image")
    h, w = image.shape[:2]
    lw, lh = lens.image_size
    if abs(w / h - lw / lh) > 0.01 * (lw / lh):
        raise StreamMismatch("aspect ratio differs from reference lens")
    size = analysis_size(lens, cfg)
    small = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    sift = cv2.SIFT_create(nfeatures=cfg.max_features)
    keypoints, descriptors = sift.detectAndCompute(gray, None)
    points = np.array([k.pt for k in keypoints], dtype=np.float64).reshape(-1, 2)
    if len(points) and lens.model != "NONE" and lens.coeffs.size:
        K = analysis_K(lens, cfg)
        if lens.model == "OPENCV_FISHEYE":
            points = cv2.fisheye.undistortPoints(points[:, None], K, lens.coeffs, P=K)[:, 0]
        else:
            points = cv2.undistortPoints(points[:, None], K, lens.coeffs, P=K)[:, 0]
    return ViewFeatures(points, descriptors, len(points))


def mutual_matches(a: ViewFeatures, b: ViewFeatures):
    import numpy as np

    if a.descriptors is None or b.descriptors is None or min(a.count, b.count) < 2:
        return np.empty((0, 2)), np.empty((0, 2))
    import cv2

    matcher = cv2.BFMatcher(cv2.NORM_L2)
    forward = matcher.knnMatch(a.descriptors, b.descriptors, k=2)
    reverse = matcher.knnMatch(b.descriptors, a.descriptors, k=2)
    backwards = {m.queryIdx: m.trainIdx for pair in reverse if len(pair) == 2
                 for m, n in [pair] if m.distance < 0.75 * n.distance}
    good = [m for pair in forward if len(pair) == 2 for m, n in [pair]
            if m.distance < 0.75 * n.distance and backwards.get(m.trainIdx) == m.queryIdx]
    return a.points[[m.queryIdx for m in good]], b.points[[m.trainIdx for m in good]]


def classify(p, q, K, cfg: ViewCheckConfig) -> Verdict:
    """기준 점 p → 지금 점 q(분석 해상도, 왜곡 제거). CHANGED 는 한 번의 후보이고 확인은 ViewConfirm 이 한다."""
    import numpy as np

    n = len(p)
    thr = cfg.max_residual_px
    disp = np.linalg.norm(q - p, axis=1) if n else np.empty(0)
    if n < cfg.min_matches:
        if n >= 8 and float(np.median(disp)) <= thr * 1.5:
            return Verdict("UNKNOWN", "OCCLUDED", n)
        return Verdict("CHANGED", "VIEW_DIFFERENT", n)
    if float((disp <= thr).mean()) >= cfg.stationary_ratio:
        return Verdict("SAME", None, n, None, 0.0)
    import cv2

    H, mask = cv2.findHomography(p, q, cv2.RANSAC, thr)
    if H is None or mask is None or not np.isfinite(H).all():
        return Verdict("CHANGED", "VIEW_DIFFERENT", n)
    inliers = mask.ravel() != 0
    ratio = float(inliers.mean())
    if ratio < cfg.min_inlier_ratio:
        anchors = int((disp <= thr).sum())
        if anchors >= 12 and anchors / n >= 0.35:
            return Verdict("UNKNOWN", "OCCLUDED", n, ratio)
        return Verdict("CHANGED", "MOVED", n, ratio)
    K = np.asarray(K, dtype=np.float64)
    M = np.linalg.solve(K, H @ K)
    if np.linalg.det(M) < 0:
        M = -M
    U, singular, Vt = np.linalg.svd(M)
    R = U @ np.diag([1.0, 1.0, np.linalg.det(U @ Vt)]) @ Vt
    angle = float(np.arccos(np.clip((np.trace(R) - 1.0) / 2.0, -1.0, 1.0)))
    projected = np.c_[p[inliers], np.ones(int(inliers.sum()))] @ (K @ R @ np.linalg.inv(K)).T
    if np.any(np.abs(projected[:, 2]) < 1e-9):
        return Verdict("CHANGED", "MOVED", n, ratio)
    rot_residual = np.linalg.norm(projected[:, :2] / projected[:, 2, None] - q[inliers], axis=1)
    if float(singular[-1] / singular[0]) < cfg.min_singular_ratio or float(np.percentile(rot_residual, 95)) > thr:
        # 호모그래피는 맞지만 회전으로 설명되지 않는다: 평면(바닥·벽) 위주 장면에서 카메라가 옮겨졌다
        return Verdict("CHANGED", "MOVED", n, ratio)
    if angle > cfg.max_rotation_rad:
        return Verdict("CHANGED", "ROTATED", n, ratio, angle)
    return Verdict("SAME", None, n, ratio, angle)


@dataclass
class ViewConfirm:
    """카메라마다 서로 다른 키프레임 2회 연속 CHANGED 일 때만 CHANGED(-540 CandidateConfirmation 과 같은 2회)."""

    last: dict[str, tuple[int, Verdict, Verdict]] = field(default_factory=dict)

    def observe(self, camera_id: str, frame_ts_ms: int, verdict: Verdict) -> Verdict:
        prev = self.last.get(camera_id)
        if prev is not None and prev[0] == frame_ts_ms:
            return prev[2]  # 같은 키프레임은 다시 세지 않는다
        if verdict.status == "CHANGED":
            confirmed = prev is not None and prev[1].status == "CHANGED"
            shown = verdict if confirmed else Verdict(
                "UNKNOWN", "CONFIRMING", verdict.matches, verdict.inlier_ratio, verdict.rotation_rad)
        else:
            shown = verdict
        self.last[camera_id] = (frame_ts_ms, verdict, shown)
        return shown

    def reset(self, camera_id: str | None = None) -> None:
        if camera_id is None:
            self.last.clear()
        else:
            self.last.pop(camera_id, None)


def fetch_image(url: str):
    """서명 주소에서 기준 사진 한 장(인증 머리 없음, https 만, 16 MiB 상한)."""
    import cv2
    import numpy as np

    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("reference URL must be https")
    with urllib.request.urlopen(url, timeout=10) as response:  # https 만(위에서 확인)
        raw = response.read(MAX_REFERENCE_BYTES + 1)
    if len(raw) > MAX_REFERENCE_BYTES:
        raise ValueError("reference image too large")
    image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("reference image not decodable")
    return image


def parse_reference(reference) -> tuple[str, str, dict[str, dict]] | None:
    """EC2 수요 응답의 reuse_reference 를 확인한다. 형식이 틀리면 None(비교하지 않음)."""
    if not isinstance(reference, dict):
        return None
    scene, calibration = reference.get("scene_version"), reference.get("calibration_version")
    cameras = reference.get("cameras")
    if not isinstance(scene, str) or not scene or not isinstance(calibration, str) or not calibration \
            or not isinstance(cameras, list):
        return None
    by_id = {}
    for camera in cameras:
        if isinstance(camera, dict) and camera.get("camera_id") in CAMERAS and isinstance(camera.get("url"), str):
            by_id.setdefault(camera["camera_id"], camera)
    return scene, calibration, by_id


class ViewChecker:
    """기준 사진은 Scene 이 바뀔 때만 내려받아 특징을 한 번 뽑아 둔다. 실패한 카메라는 30 s 뒤 다시 받는다."""

    def __init__(self, cfg: ViewCheckConfig | None = None, *, fetch=fetch_image, clock=time.monotonic):
        self.cfg = cfg or ViewCheckConfig()
        self.fetch = fetch
        self.clock = clock
        self.key: tuple[str, str] | None = None
        self.refs: dict[str, tuple[Lens, ViewFeatures]] = {}
        self.failed: dict[str, tuple[float, str]] = {}
        self.confirm = ViewConfirm()

    def _reference(self, cid: str, camera: dict | None) -> tuple[Lens, ViewFeatures] | str:
        if cid in self.refs:
            return self.refs[cid]
        if camera is None:
            return "NO_REFERENCE"
        failed = self.failed.get(cid)
        if failed is not None and self.clock() - failed[0] < REFERENCE_RETRY_S:
            return failed[1]
        try:
            lens = parse_lens(camera)
            features = extract(self.fetch(camera["url"]), lens, self.cfg)
        except StreamMismatch:
            self.failed[cid] = (self.clock(), "STREAM_MISMATCH")
            return "STREAM_MISMATCH"
        except Exception:  # noqa: BLE001 - 서명 주소·응답 본문은 기록하지 않는다
            LOG.warning("reuse reference unavailable camera=%s", cid)
            self.failed[cid] = (self.clock(), "NO_REFERENCE")
            return "NO_REFERENCE"
        self.refs[cid] = (lens, features)
        self.failed.pop(cid, None)
        return self.refs[cid]

    def _one(self, cid: str, camera: dict | None, frame, status: str) -> Verdict:
        if status != "OK" or frame is None:
            return Verdict("UNKNOWN", "NO_VIDEO" if status == "NO_VIDEO" or frame is None else "STALE")
        ref = self._reference(cid, camera)
        if isinstance(ref, str):
            return Verdict("UNKNOWN", ref)
        lens, ref_features = ref
        if ref_features.count < self.cfg.min_reference_features:
            return Verdict("UNKNOWN", "NO_TEXTURE", 0)
        try:
            current = extract(frame, lens, self.cfg)
        except StreamMismatch:
            return Verdict("UNKNOWN", "STREAM_MISMATCH")
        p, q = mutual_matches(ref_features, current)
        return classify(p, q, analysis_K(lens, self.cfg), self.cfg)

    def check(self, reference, frames: dict, statuses: dict[str, str]) -> dict | None:
        """frames: 카메라 → (frame_ts_ms, BGR) | None, statuses: 카메라 → OK|NO_VIDEO|STALE. 기준이 없으면 None."""
        parsed = parse_reference(reference)
        if parsed is None:
            if self.key is not None:
                self._forget()
            return None
        scene, calibration, cameras = parsed
        if self.key != (scene, calibration):
            self._forget()
            self.key = (scene, calibration)
        records = []
        for cid in CAMERAS:
            entry = frames.get(cid)
            ts, image = (entry if entry is not None else (None, None))
            try:
                verdict = self._one(cid, cameras.get(cid), image, statuses.get(cid, "NO_VIDEO"))
            except Exception:  # noqa: BLE001
                LOG.warning("reuse view check failed camera=%s", cid)
                verdict = Verdict("UNKNOWN", "ERROR")
            if ts is not None and verdict.status in ("SAME", "CHANGED"):
                verdict = self.confirm.observe(cid, ts, verdict)
            else:
                self.confirm.reset(cid)  # 근거가 끊기면(가림 포함) 확인을 처음부터 다시
            records.append(verdict.record(cid))
        return {"scene_version": scene, "calibration_version": calibration, "cameras": records}

    def _forget(self) -> None:
        self.key = None
        self.refs.clear()
        self.failed.clear()
        self.confirm.reset()


class MultiViewChecker:
    """후보 맵마다 ViewChecker 하나(S15P31A101-714). 목록 순서대로 결과를 내고, 목록에서 빠진 맵의 기준·확인 상태는 버린다."""

    def __init__(self, factory=ViewChecker):
        self.factory = factory
        self.checkers: dict[tuple[str, str], ViewChecker] = {}

    def check_all(self, references, frames: dict, statuses: dict[str, str]) -> list[dict]:
        keep: dict[tuple[str, str], ViewChecker] = {}
        results = []
        for reference in list(references or [])[:MAX_REFERENCES]:
            parsed = parse_reference(reference)
            if parsed is None:
                continue
            key = (parsed[0], parsed[1])
            if key in keep:
                continue  # 같은 맵은 한 번만(EC2 가 같은 scene_version 을 두 번 싣지 않게 막는다)
            checker = self.checkers.get(key) or self.factory()
            keep[key] = checker
            try:
                view = checker.check(reference, frames, statuses)
            except Exception:  # noqa: BLE001 - 한 맵이 실패해도 다른 맵·각도 안내는 그대로
                LOG.warning("reuse view check failed scene")
                continue
            if view is not None:
                results.append(view)
        self.checkers = keep
        return results


def describe(view: dict | None) -> str:
    """로그 한 줄용 요약(A=S B=C:MOVED …). 서명 주소·영상 정보는 없다."""
    if not view:
        return "-"
    return json.dumps({c["camera_id"][-1]: c["status"][0] + (":" + c["reason"] if c["reason"] else "")
                       for c in view["cameras"]}, separators=(",", ":"))
