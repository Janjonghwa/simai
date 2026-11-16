"""기준 시점 맞추기(S15P31A101-765): 카메라마다 자기 기준 사진에 맞추는 각도 안내.

카메라끼리 겹침(옆 쌍 판정)으로 맞추면 한 대를 움직일 때 다른 쌍 판정도 바뀌어 맞추기 어렵다. 그래서 카메라마다 「보정이 잘 됐던 세팅」
(그 현장의 가장 최근 CALIBRATED 세팅: 걷기 카메라 확인 PASSED·검증 기준선 있음, EC2 수요 응답 `aim_reference`)의 같은 카메라 사람 지운
배경 사진에 맞춘다. 서로 영향이 없다.

- 매칭은 재사용 시점 확인(-712 `view_check`)과 같다: 같은 분석 크기 SIFT, 왜곡 제거, 상호 최근접 Lowe 0.75, 호모그래피 RANSAC 3 px,
  인라이어 0.6, K⁻¹HK 특이값 비 0.98, 회전 재투영 p95 3 px. 지금 키프레임 SIFT 는 `RoundFeatures` 로 재사용 시점 확인과 나눠 쓴다.
- 같은 위치에서 회전만 바뀌었다고 보고 H ≈ K R K⁻¹ 의 R 을 인라이어 광선의 직교 Procrustes(Kabsch)로 다시 맞춘다(호모그래피 8자유도보다
  회전 3자유도 최소제곱이 작은 각도에서 덜 흔들린다). 순수 회전으로 설명되지 않으면(위치 이동·다른 장면) 돌려서 맞출 수 없으므로 UNKNOWN.
- 각도는 「기준에 맞추려면 카메라를 이만큼 돌린다」는 교정량이다(카메라 뒤에서 렌즈 방향으로 볼 때): pan + 오른쪽, tilt + 위, roll + 시계 방향.
  C(기준 광선 → 지금 광선, 지금 카메라 좌표계, OpenCV 축 x 오른쪽·y 아래·z 앞) = R_y(pan)·R_x(tilt)·R_z(roll).
  지금 카메라를 C 만큼(자기 축 기준) 돌리면 기준 카메라 방향이 된다: 기준 광축 = C·e_z 이므로 C·e_z 가 오른쪽(+x)이면 오른쪽으로 돌린다.
- 두 단계 허용 오차(목적별):
  ALIGNED(기준과 같음, 재사용 가능): 광축 방향 차 sqrt(pan² + tilt²) ≤ 0.25°, |roll| ≤ 1.0°. 근거는 -694 섭동표(pan 0.25° 는 LOO 6.7~9.4 px 로
  잡음 바닥 5.9~8.7 px 와 겹치고, 0.5° 는 LOO 9.0~12.0 px 로 세팅 상한 12 px 에 닿는다. roll 2° 가 pan 0.5° 와 비슷하다).
  NEAR(새 맵 세팅엔 충분): 광축 ≤ 1.5°, |roll| ≤ 3.0°. 옆 쌍 겹침 띠(-761 격자 한 열 = 화면 1/4 폭, 로비 광각에서 약 19°)가 1/10 열 안에서만
  움직이는 범위라 Stage A·-761 판정의 칸 수가 기준 때와 거의 같다.
- 깜박임 방지: 단계는 그 카메라의 서로 다른 키프레임 2라운드 연속 충족일 때만 오르고, 2라운드 연속 미충족일 때만 내린다(`AimHysteresis`).
  docs/design/S15P31A101-765-aim-reference-overlay.md.

이 모듈은 위치·자세·보정을 만들지 않는다(사람이 카메라를 돌릴 안내만). 영상·특징점은 메시지에 싣지 않는다.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, replace

from .judge import CAMERAS
from .view_check import (
    Lens,
    RoundFeatures,
    StreamMismatch,
    ViewCheckConfig,
    ViewChecker,
    analysis_K,
    fetch_image,
    fit_homography_rotation,
    mutual_matches,
    parse_reference,
    rotation_residuals,
)

LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class AimConfig:
    max_aim_rad: float = math.radians(0.25)
    """광축 방향 차 sqrt(pan² + tilt²) 상한. -694 섭동표 pan 0.25° = 걷기 확인 LOO 잡음 바닥 안, 0.5° = 세팅 LOO 상한 12 px 경계"""
    max_roll_rad: float = math.radians(1.0)
    """기울기 상한. -694 섭동표에서 roll 2° 의 피해가 pan 0.5° 와 비슷하므로 같은 비(×4)로 0.25° → 1.0°"""
    max_setup_aim_rad: float = math.radians(1.5)
    """NEAR(새 맵 세팅엔 충분) 광축 상한. 옆 쌍 겹침 띠(4×4 격자 한 열 ≈ 19°, 로비 f ≈ 0.42 W)의 이동이 1/10 열(약 43 px / 576 px) 안"""
    max_setup_roll_rad: float = math.radians(3.0)
    """NEAR 기울기 상한. 3° 기울기로 겹침 띠 위아래 끝이 옆으로 밀리는 양((H/2)·sin 3° ≈ 34 px)이 1.5° 광축 이동(43 px)보다 작다"""
    hysteresis_rounds: int = 2
    """단계가 오르내리려면 연속으로 충족·미충족해야 하는 라운드 수(서로 다른 키프레임)"""


LEVELS = ("MISALIGNED", "NEAR", "ALIGNED")


def level_of(pan: float, tilt: float, roll: float, aim: AimConfig) -> str:
    """이번 라운드 측정만으로 본 단계(히스테리시스 전)."""
    off_axis = math.hypot(pan, tilt)
    if off_axis <= aim.max_aim_rad and abs(roll) <= aim.max_roll_rad:
        return "ALIGNED"
    if off_axis <= aim.max_setup_aim_rad and abs(roll) <= aim.max_setup_roll_rad:
        return "NEAR"
    return "MISALIGNED"


class AimHysteresis:
    """카메라마다 단계(MISALIGNED < NEAR < ALIGNED)를 연속 n 라운드 충족해야 올리고, 연속 n 라운드 미충족해야 내린다.

    처음(또는 근거가 끊긴 뒤)은 가장 낮은 단계에서 시작한다. 같은 키프레임은 다시 세지 않는다. UNKNOWN 이면 처음부터 다시 센다."""

    def __init__(self, rounds: int = 2):
        self.rounds = max(1, rounds)
        self.state: dict[str, tuple[object, list[int], int]] = {}

    def observe(self, camera_id: str, frame_ts_ms, level: str) -> str:
        last_ts, history, shown = self.state.get(camera_id, (None, [], 0))
        if frame_ts_ms is None or frame_ts_ms != last_ts:
            history = (history + [LEVELS.index(level)])[-self.rounds:]
        if len(history) == self.rounds:
            up, down = min(history), max(history)
            if up > shown:
                shown = up
            elif down < shown:
                shown = down
        self.state[camera_id] = (frame_ts_ms, history, shown)
        return LEVELS[shown]

    def reset(self, camera_id: str | None = None) -> None:
        if camera_id is None:
            self.state.clear()
        else:
            self.state.pop(camera_id, None)


@dataclass(frozen=True)
class AimVerdict:
    status: str
    reason: str | None = None
    matches: int | None = None
    inlier_ratio: float | None = None
    pan_rad: float | None = None
    tilt_rad: float | None = None
    roll_rad: float | None = None
    target_px: tuple[float, float] | None = None
    target_point: tuple[float, float] | None = None
    arrow_rad: float | None = None

    def record(self, camera_id: str) -> dict:
        def r(value, digits):
            return None if value is None else round(float(value), digits)

        return {
            "camera_id": camera_id,
            "status": self.status,
            "reason": self.reason,
            "matches": self.matches,
            "inlier_ratio": r(self.inlier_ratio, 4),
            "pan_rad": r(self.pan_rad, 6),
            "tilt_rad": r(self.tilt_rad, 6),
            "roll_rad": r(self.roll_rad, 6),
            "target_point": None if self.target_point is None else [r(v, 4) for v in self.target_point],
            "target_px": None if self.target_px is None else [r(v, 1) for v in self.target_px],
            "arrow_rad": r(self.arrow_rad, 4),
        }


def kabsch_rotation(p, q, K):
    """p·q(분석 해상도 픽셀, 왜곡 제거)의 광선으로 q ≈ R p 인 회전의 최소제곱 해(직교 Procrustes)."""
    import numpy as np

    Kinv = np.linalg.inv(np.asarray(K, dtype=np.float64))
    a = np.c_[p, np.ones(len(p))] @ Kinv.T
    b = np.c_[q, np.ones(len(q))] @ Kinv.T
    a /= np.linalg.norm(a, axis=1, keepdims=True)
    b /= np.linalg.norm(b, axis=1, keepdims=True)
    U, _, Vt = np.linalg.svd(b.T @ a)
    return U @ np.diag([1.0, 1.0, np.linalg.det(U @ Vt)]) @ Vt


def decompose(R) -> tuple[float, float, float]:
    """C = R_y(pan)·R_x(tilt)·R_z(roll) 분해(표준 오른손 축 회전, OpenCV 카메라 축). 교정량: pan + 오른쪽, tilt + 위, roll + 시계 방향."""
    import numpy as np

    pan = math.atan2(R[0, 2], R[2, 2])
    tilt = math.asin(float(np.clip(-R[1, 2], -1.0, 1.0)))
    roll = math.atan2(R[1, 0], R[1, 1])
    return pan, tilt, roll


def compose(pan: float, tilt: float, roll: float):
    """decompose 의 역(시험·합성용)."""
    import numpy as np

    c, s = math.cos(pan), math.sin(pan)
    Ry = np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])
    c, s = math.cos(tilt), math.sin(tilt)
    Rx = np.array([[1, 0, 0], [0, c, -s], [0, s, c]])
    c, s = math.cos(roll), math.sin(roll)
    Rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    return Ry @ Rx @ Rz


def _normalized(points, lens: Lens):
    """원본 픽셀(왜곡 그대로) → 정규화 카메라 좌표(왜곡 제거)."""
    import cv2
    import numpy as np

    pts = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    if lens.model == "NONE" or not lens.coeffs.size:
        K = lens.K
        return np.c_[(pts[:, 0, 0] - K[0, 2]) / K[0, 0], (pts[:, 0, 1] - K[1, 2]) / K[1, 1]]
    if lens.model == "OPENCV_FISHEYE":
        return cv2.fisheye.undistortPoints(pts, lens.K, lens.coeffs)[:, 0]
    return cv2.undistortPoints(pts, lens.K, lens.coeffs)[:, 0]


def _project(xn: float, yn: float, lens: Lens) -> tuple[float, float]:
    """정규화 카메라 좌표 → 원본 픽셀(렌즈 왜곡 적용)."""
    import cv2
    import numpy as np

    if lens.model == "NONE" or not lens.coeffs.size:
        return float(lens.K[0, 0] * xn + lens.K[0, 2]), float(lens.K[1, 1] * yn + lens.K[1, 2])
    zero = np.zeros(3)
    if lens.model == "OPENCV_FISHEYE":
        out, _ = cv2.fisheye.projectPoints(np.array([[[xn, yn, 1.0]]]), zero, zero, lens.K, lens.coeffs)
    else:
        out, _ = cv2.projectPoints(np.array([[xn, yn, 1.0]]), zero, zero, lens.K, lens.coeffs)
    u, v = out.reshape(2)
    return float(u), float(v)


def target_of(R, lens: Lens) -> tuple[float, float] | None:
    """기준 영상 중심이 지금 영상(원본 픽셀, 왜곡 그대로)에 오는 곳. 카메라 뒤·영상 밖이면 None.

    왜곡 다항식은 영상 밖에서 다시 안으로 접힐 수 있으므로, 먼저 정규화 좌표가 영상 테두리의 왜곡 제거 범위(10% 여유) 안인지 본다."""
    import numpy as np

    w, h = lens.image_size
    center = _normalized([[(w - 1) / 2, (h - 1) / 2]], lens)[0]
    ray = np.asarray(R, dtype=np.float64) @ np.array([center[0], center[1], 1.0])
    if ray[2] <= 1e-6:
        return None
    xn, yn = ray[0] / ray[2], ray[1] / ray[2]
    border = _normalized([[0, 0], [w - 1, 0], [0, h - 1], [w - 1, h - 1],
                          [(w - 1) / 2, 0], [(w - 1) / 2, h - 1], [0, (h - 1) / 2], [w - 1, (h - 1) / 2]], lens)
    if abs(xn) > 1.1 * float(np.abs(border[:, 0]).max()) or abs(yn) > 1.1 * float(np.abs(border[:, 1]).max()):
        return None
    u, v = _project(xn, yn, lens)
    if not (math.isfinite(u) and math.isfinite(v)) or not (-0.5 <= u <= w - 0.5 and -0.5 <= v <= h - 0.5):
        return None
    return u, v


def measure(p, q, lens: Lens, cfg: ViewCheckConfig, aim: AimConfig) -> AimVerdict:
    """기준 점 p → 지금 점 q(분석 해상도, 왜곡 제거)에서 기준에 맞출 교정 각도."""
    import numpy as np

    n = len(p)
    if n < cfg.min_matches:
        return AimVerdict("UNKNOWN", "FEW_MATCHES", n)
    K = analysis_K(lens, cfg)
    fit = fit_homography_rotation(p, q, K, cfg.max_residual_px, cfg.min_inlier_ratio)
    if fit is None:
        return AimVerdict("UNKNOWN", "NOT_ROTATION", n)
    if fit.R is None:
        return AimVerdict("UNKNOWN", "NOT_ROTATION", n, fit.ratio)
    inliers = fit.inliers
    R = kabsch_rotation(p[inliers], q[inliers], K)
    residual = rotation_residuals(p[inliers], q[inliers], K, R)
    if (residual is None or fit.singular_ratio < cfg.min_singular_ratio
            or float(np.percentile(residual, 95)) > cfg.max_residual_px):
        # 호모그래피는 맞지만 순수 회전으로 설명되지 않는다(위치 이동): 돌려서는 맞출 수 없다
        return AimVerdict("UNKNOWN", "NOT_ROTATION", n, fit.ratio)
    pan, tilt, roll = decompose(R)
    off_axis = math.hypot(pan, tilt)
    target = target_of(R, lens)
    point = None if target is None else (
        (target[0] + 0.5) / lens.image_size[0], (target[1] + 0.5) / lens.image_size[1])
    arrow = None
    if off_axis > aim.max_aim_rad:
        w, h = lens.image_size
        if target is not None:
            arrow = math.atan2(target[1] - (h - 1) / 2, target[0] - (w - 1) / 2)
        else:
            arrow = math.atan2(-math.tan(tilt) * lens.K[1, 1], math.tan(pan) * lens.K[0, 0])
    return AimVerdict(level_of(pan, tilt, roll, aim), None, n, fit.ratio, pan, tilt, roll, target, point, arrow)


class AimChecker(ViewChecker):
    """기준 사진 받기·30 s 재시도·맵 바뀜 처리는 ViewChecker 그대로. 같은 맵(scene·보정)의 재사용 시점 확인이 이미 받은 기준 특징은 빌려 쓴다."""

    def __init__(self, cfg: ViewCheckConfig | None = None, aim: AimConfig | None = None, *, fetch=fetch_image,
                 clock=time.monotonic):
        super().__init__(cfg, fetch=fetch, clock=clock)
        self.aim = aim or AimConfig()
        self.hysteresis = AimHysteresis(self.aim.hysteresis_rounds)

    def _forget(self) -> None:
        super()._forget()
        self.hysteresis.reset()

    def _borrow(self, cid: str, donors) -> None:
        if cid in self.refs:
            return
        for donor in donors:
            if donor is not self and donor.key == self.key and cid in donor.refs:
                self.refs[cid] = donor.refs[cid]
                self.failed.pop(cid, None)
                return

    def _aim_one(self, cid: str, camera: dict | None, frame, status: str, ts,
                 features: RoundFeatures | None) -> AimVerdict:
        if status != "OK" or frame is None:
            return AimVerdict("UNKNOWN", "NO_VIDEO" if status == "NO_VIDEO" or frame is None else "STALE")
        ref = self._reference(cid, camera)
        if isinstance(ref, str):
            return AimVerdict("UNKNOWN", ref)
        lens, ref_features = ref
        if ref_features.count < self.cfg.min_reference_features:
            return AimVerdict("UNKNOWN", "NO_TEXTURE")
        try:
            current = self._current(cid, ts, frame, lens, features)
        except StreamMismatch:
            return AimVerdict("UNKNOWN", "STREAM_MISMATCH")
        p, q = mutual_matches(ref_features, current)
        return measure(p, q, lens, self.cfg, self.aim)

    def check(self, reference, frames: dict, statuses: dict[str, str], features: RoundFeatures | None = None,
              donors=()) -> dict | None:
        """coverage_guide 1.4.0 aim_check. 기준이 없거나 형식이 틀리면 None(싣지 않음)."""
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
            self._borrow(cid, donors)
            try:
                verdict = self._aim_one(cid, cameras.get(cid), image, statuses.get(cid, "NO_VIDEO"), ts, features)
            except Exception:  # noqa: BLE001 - 서명 주소·응답 본문은 기록하지 않는다
                LOG.warning("aim check failed camera=%s", cid)
                verdict = AimVerdict("UNKNOWN", "ERROR")
            if verdict.status == "UNKNOWN":
                self.hysteresis.reset(cid)  # 근거가 끊기면 단계를 처음부터
            else:
                shown = self.hysteresis.observe(cid, ts, verdict.status)
                verdict = replace(verdict, status=shown, arrow_rad=None if shown == "ALIGNED" else verdict.arrow_rad)
            records.append(verdict.record(cid))
        return {"scene_version": scene, "calibration_version": calibration,
                "max_aim_rad": round(self.aim.max_aim_rad, 6), "max_roll_rad": round(self.aim.max_roll_rad, 6),
                "max_setup_aim_rad": round(self.aim.max_setup_aim_rad, 6),
                "max_setup_roll_rad": round(self.aim.max_setup_roll_rad, 6),
                "hysteresis_rounds": self.hysteresis.rounds, "cameras": records}


def describe_aim(aim: dict | None) -> str:
    """로그 한 줄용 요약(A=OK B=+1.20/-0.30/+0.10 도 …). 서명 주소·영상 정보는 없다."""
    if not aim:
        return "-"

    def one(c: dict) -> str:
        if c["status"] == "UNKNOWN":
            return "?:" + str(c["reason"])
        angles = "/".join(f"{math.degrees(c[k]):+.2f}" for k in ("pan_rad", "tilt_rad", "roll_rad"))
        return {"ALIGNED": "OK ", "NEAR": "NEAR "}.get(c["status"], "") + angles

    return json.dumps({c["camera_id"][-1]: one(c) for c in aim["cameras"]}, separators=(",", ":"), ensure_ascii=False)
