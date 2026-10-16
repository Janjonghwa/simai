"""사람 궤적과 관절 3D (정답).

궤적은 시각이 붙은 경유점 사이를 등속 직선으로 잇는다. 골격은 키 1.70 m 기준 비율을 키에 맞춰 늘리고,
걸을 때는 다리·팔을 진자처럼 흔든다. 발목은 늘 지지 표면 위 같은 높이에 있도록 골반을 내린다.
관절은 COCO17 순서에 발 끝점(l_toe, r_toe)을 따로 둔다.
"""

import math
from dataclasses import dataclass

from sim.scenario import PersonSpec, Surface
from sim.vec import Vec3

COCO17 = (
    "nose", "l_eye", "r_eye", "l_ear", "r_ear",
    "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_wrist", "r_wrist",
    "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle",
)  # fmt: skip
FOOT_TIPS = ("l_toe", "r_toe")
HEAD_JOINTS = range(0, 5)
FACE_JOINTS = range(0, 3)
"""코·두 눈. 머리 뒤에서는 보이지 않는다."""
L_HIP, R_HIP, L_ANKLE, R_ANKLE = 11, 12, 15, 16

_REF_HEIGHT = 1.70
_HIP_U = 0.92
_THIGH, _SHIN = 0.42, 0.42
_ANKLE_U = _HIP_U - _THIGH - _SHIN  # 0.08
_UPPER_ARM, _FOREARM = 0.30, 0.27
_STRIDE = 1.4
"""걸음 주기 한 번(두 걸음)의 이동 거리, 키 1.70 m 기준"""
_WALK_SPEED = 1.2
_MAX_SWING = 0.35


@dataclass(frozen=True)
class BodyState:
    """사람 한 명의 한 시각 정답. 모든 좌표는 World 미터."""

    position_xy: tuple[float, float]
    velocity: Vec3
    heading_rad: float
    support_surface_id: str
    support_z: float
    joints: tuple[Vec3, ...]
    """COCO17 순서"""
    foot_tips: tuple[Vec3, Vec3]
    head_top: Vec3
    pelvis: Vec3
    ground_anchor: Vec3
    """두 발목 XY 평균을 지지 표면 높이에 둔 점"""


class Person:
    def __init__(self, spec: PersonSpec, surfaces: tuple[Surface, ...]):
        self.spec = spec
        self._surfaces = surfaces
        wps = spec.waypoints
        # 구간별 (t0, t1, p0, p1, 시작 누적거리, 진행 방향)
        self._segments: list[tuple[int, int, tuple[float, float], tuple[float, float], float, float | None]] = []
        dist = 0.0
        for a, b in zip(wps, wps[1:]):
            dx, dy = b.xy[0] - a.xy[0], b.xy[1] - a.xy[1]
            length = math.hypot(dx, dy)
            heading = math.atan2(dy, dx) if length > 1e-9 else None
            self._segments.append((a.t_ms, b.t_ms, a.xy, b.xy, dist, heading))
            dist += length
        self._total_dist = dist
        # 멈춘 구간은 직전에 걷던 방향을 유지한다. 걸은 적이 없으면 initial_heading_rad.
        self._headings: list[float] = []
        last = spec.initial_heading_rad
        for seg in self._segments:
            if seg[5] is not None:
                last = seg[5]
            self._headings.append(last)
        self._final_heading = last

    def state(self, t_rel_ms: int) -> BodyState:
        """시나리오 시작 기준 t_rel_ms 의 정답 상태."""
        xy, vel, dist, heading, speed = self._sample(t_rel_ms)
        support_id, support_z = self._support(xy)
        s = self.spec.height_m / _REF_HEIGHT
        swing = _MAX_SWING * min(1.0, speed / _WALK_SPEED)
        phase = 2 * math.pi * dist / (_STRIDE * s)
        return _skeleton(xy, heading, support_id, support_z, s, swing, phase, vel)

    def _sample(self, t: int) -> tuple[tuple[float, float], Vec3, float, float, float]:
        wps = self.spec.waypoints
        if not self._segments or t <= wps[0].t_ms:
            heading = self._headings[0] if self._headings else self._final_heading
            return wps[0].xy, (0.0, 0.0, 0.0), 0.0, heading, 0.0
        if t >= wps[-1].t_ms:
            return wps[-1].xy, (0.0, 0.0, 0.0), self._total_dist, self._final_heading, 0.0
        for i, (t0, t1, p0, p1, d0, _) in enumerate(self._segments):
            if t0 <= t < t1:
                f = (t - t0) / (t1 - t0)
                dt_s = (t1 - t0) / 1000.0
                vx, vy = (p1[0] - p0[0]) / dt_s, (p1[1] - p0[1]) / dt_s
                speed = math.hypot(vx, vy)
                xy = (p0[0] + f * (p1[0] - p0[0]), p0[1] + f * (p1[1] - p0[1]))
                return xy, (vx, vy, 0.0), d0 + speed * (t - t0) / 1000.0, self._headings[i], speed
        raise AssertionError("구간 탐색 실패")

    def _support(self, xy: tuple[float, float]) -> tuple[str, float]:
        """발 아래 가장 높은 지지 표면. 상자 위가 아니면 바닥(Z=0)."""
        best_id, best_z = "floor", 0.0
        for sf in self._surfaces:
            if (
                sf.box_min[0] <= xy[0] <= sf.box_max[0]
                and sf.box_min[1] <= xy[1] <= sf.box_max[1]
                and sf.box_max[2] > best_z
            ):
                best_id, best_z = sf.surface_id, sf.box_max[2]
        return best_id, best_z


def _skeleton(
    xy: tuple[float, float], heading: float, support_id: str, z0: float, s: float, swing: float, phase: float, vel: Vec3
) -> BodyState:
    fx, fy = math.cos(heading), math.sin(heading)
    lx, ly = -fy, fx
    a_left = swing * math.sin(phase)
    a_right = -a_left
    # 다리를 펴고 흔들면 발목이 뜨므로 골반을 내려 발목 높이를 지지 표면 + _ANKLE_U 로 둔다.
    drop = (_THIGH + _SHIN) * s * (1 - math.cos(a_left))

    def pt(f: float, side: float, u: float) -> Vec3:
        """몸 기준 (앞 f, 왼쪽 side, 위 u, 미터)를 World 로 바꾼다. u 는 지지 표면 기준."""
        return (xy[0] + f * fx + side * lx, xy[1] + f * fy + side * ly, z0 + u)

    def leg(side: float, a: float) -> tuple[Vec3, Vec3, Vec3, Vec3]:
        hip_u = _HIP_U * s - drop
        hip = pt(0.0, side * 0.10 * s, hip_u)
        knee = pt(_THIGH * s * math.sin(a) + 0.02 * s, side * 0.10 * s, hip_u - _THIGH * s * math.cos(a))
        ank_f = (_THIGH + _SHIN) * s * math.sin(a)
        ankle = pt(ank_f, side * 0.10 * s, _ANKLE_U * s)
        toe = pt(ank_f + 0.15 * s, side * 0.10 * s, 0.02 * s)
        return hip, knee, ankle, toe

    def arm(side: float, b: float) -> tuple[Vec3, Vec3, Vec3]:
        sh_u = 1.42 * s - drop
        shoulder = pt(0.0, side * 0.19 * s, sh_u)
        el_f, el_u = _UPPER_ARM * s * math.sin(b), sh_u - _UPPER_ARM * s * math.cos(b)
        elbow = pt(el_f, side * 0.21 * s, el_u)
        wrist = pt(el_f + _FOREARM * s * math.sin(b + 0.15), side * 0.22 * s, el_u - _FOREARM * s * math.cos(b + 0.15))
        return shoulder, elbow, wrist

    def head(f: float, side: float, u: float) -> Vec3:
        return pt(f * s, side * s, u * s - drop)

    l_hip, l_knee, l_ankle, l_toe = leg(+1.0, a_left)
    r_hip, r_knee, r_ankle, r_toe = leg(-1.0, a_right)
    l_sh, l_el, l_wr = arm(+1.0, -0.8 * a_left)
    r_sh, r_el, r_wr = arm(-1.0, -0.8 * a_right)
    joints = (
        head(0.10, 0.0, 1.60),
        head(0.08, 0.035, 1.645), head(0.08, -0.035, 1.645),
        head(0.0, 0.075, 1.62), head(0.0, -0.075, 1.62),
        l_sh, r_sh, l_el, r_el, l_wr, r_wr,
        l_hip, r_hip, l_knee, r_knee, l_ankle, r_ankle,
    )  # fmt: skip
    pelvis = ((l_hip[0] + r_hip[0]) / 2, (l_hip[1] + r_hip[1]) / 2, (l_hip[2] + r_hip[2]) / 2)
    anchor = ((l_ankle[0] + r_ankle[0]) / 2, (l_ankle[1] + r_ankle[1]) / 2, z0)
    return BodyState(
        position_xy=xy,
        velocity=vel,
        heading_rad=heading,
        support_surface_id=support_id,
        support_z=z0,
        joints=joints,
        foot_tips=(l_toe, r_toe),
        head_top=head(0.0, 0.0, _REF_HEIGHT),
        pelvis=pelvis,
        ground_anchor=anchor,
    )
