"""운영 중 재투영 오차 감시와 틀어짐(DRIFTED) 판정 (명세 4.4 DRIFTED, 4.5 운영 중 재투영 오차, 6.5 캘리브레이션 틀어짐,
15.1 틀어짐 재현·부록 D-G1-03: 사람이 있을 때 약 1° 회전을 2분 안에 감지).

지나가는 사람의 keypoint 로 카메라마다 "한 카메라 제외 재투영 오차"를 계속 잰다. 입력은 Cross-view 연결로 같은 사람에
묶이고 공통 시각으로 보간된 카메라별 2D keypoint 다(번들 조정 입력 `PersonObservation.views` 와 같은 모양). 이 모듈은
사람 대응을 다시 하지 않는다.

표본 하나 = 사람 한 명 × 카메라 c 하나:
- 관절마다 c 를 뺀 다른 카메라 두 대(쌍)로 삼각측량해 c 에 투영하고, c 의 관측(왜곡 제거 이상적 핀홀 픽셀)과의 거리를
  잰다. 쌍이 여럿이면 그중 최솟값을 그 관절 값으로 쓴다. 관절 값들(min_joints_per_sample 개 이상)의 중앙값이 표본이다.
- c 를 빼는 이유: c 가 틀어지면 c 를 넣은 삼각측량은 오차를 다른 카메라에도 나눠 준다. 빼고 재야 틀어진 카메라만 커진다
  (15.1 G5 의 한 카메라 제외 재투영과 같은 원리).
- 쌍별 최솟값인 이유: 다른 틀어진 카메라가 기준 쌍에 끼면 기준점이 치우친다. c 가 정상이면 정상 쌍 하나와는 맞아서
  최솟값이 작게 남고, c 가 틀어졌으면 어떤 정상 쌍과도 맞지 않아 커진다.
- 귀속: 이 보호는 기준 쌍 중 하나가 틀어진 카메라를 빼고 있을 때만 성립한다. 관절마다 쓸 수 있었던 쌍(시선각 하한
  이상)에 모두 들어 있는 공통 카메라가 없을 때만(예: 다른 카메라 3대가 본 관절) "귀속 가능"으로 센다. 귀속 가능한
  관절로 만든 표본만 특정 카메라를 DRIFTED 로 확정한다. 같은 관절을 3대만 봐서 기준 쌍이 하나뿐인 표본은 "귀속 불확실"
  표본으로 따로 모은다. 그 불일치가 지속되면 보정이 틀어졌다는 근거는 내되(AMBIGUOUS) 어느 카메라인지는 정하지 않는다.
- 시선각이 작은 쌍(min_ray_angle_rad 미만)은 깊이가 불안정해 쓰지 않는다. 신뢰도가 낮거나 왜곡을 제거할 수 없는
  keypoint 는 관측으로 세지 않는다(삼각측량 모듈과 같은 기준).

판정(카메라별·표본 종류별, 강건 통계):
- 창: 최근 window_ms 안의 표본. 창 중앙값으로 판정하고 p95 는 기록·보고에만 쓴다.
- 신선함: 마지막 표본이 max_sample_age_ms 보다 오래됐으면 창에 표본이 남아 있어도 감시 근거 없음이다. 지속 시간은
  새 표본이 들어온 평가에서만 쌓이고 완료된다(직전 진전 뒤 경과, 최대 max_sample_age_ms). 새 표본이 없는 호출은 창
  통계가 아직 신선해도 지속을 진전·완료하지 않는다. 사람이 사라지면 그 자리에서 지속 시간이 멈춘다.
- 기준선: 같은 방식으로 잰 값과 비교해야 하므로 보정 문서의 재투영 오차(번들 조정 잔차)를 쓰지 않는다.
  - 검증된 기준선은 `baseline_from_observations` 로 그 보정을 만든 입력 관측(보정 걷기·재보정 관측)에서 잰 것뿐이다
    (`BaselineRecord.verified_camera_ids`). 보정이 그 관측에 맞춰졌으므로 그때 카메라가 그 자세였다는 근거가 있다.
  - 그 밖에는 감시를 시작한 뒤 처음 모은 표본(baseline_min_samples 개 이상, baseline_min_span_ms 이상 걸쳐)의 중앙값을
    쓰되 끝까지 "기준선 미확정"이다. 저장했다 다시 줘도(`baseline_record`) 검증 여부가 그대로 따라가며, 시간이 지나거나
    재시작해도 검증된 것으로 바뀌지 않는다. 감시 시작 전에 이미 틀어졌으면 학습 기준선이 그 상태를 정상으로 삼을 수
    있기 때문이다(evidence UNVERIFIED 로 보고).
- 절대 상한(보조 보호, 검증을 대신하지 않는다): absolute_max_median_px 를 넘는 창 중앙값은 기준선과 무관하게 초과다.
  기준선을 배우는 중에도 적용하고, 이 상한을 넘는 학습 기준선은 쓰지 않는다. 그래서 감시 시작 전부터 크게 틀어진
  카메라는 SUSPECT → DRIFTED 가 된다. 값 8px 는 15.1 G5 의 "한 카메라 제외 재투영 p95 8px" 목표에서 빌렸다. G5 는
  관절 단위 p95 이고 여기는 사람별 관절 중앙값의 창 중앙값이라 같은 통계가 아니며, 같은 값이면 여기가 더 너그럽다.
  초점거리가 짧으면 같은 회전이 더 작은 픽셀이 되어 상한 아래에 머물 수 있다(그때는 미확정 기준선으로만 보고한다).
  현장 정상 영상의 오경보는 D-G1-03 측정으로 따로 확인한다.
- 초과: 창 중앙값 > min(max(기준선 × increase_ratio, 기준선 + increase_margin_px), 절대 상한).
- 지속: 신선한 평가에서 초과가 sustain_ms 이상 쌓이면 확정한다. 기준 안의 신선한 평가가 나오면 쌓인 시간을 지운다.
  평가(호출)나 관측이 evidence_gap_ms 넘게 끊기면 지운다(근거 끊김). 끊김 검사는 표본 수 검사보다 먼저 하고, 관측이
  돌아온 첫 표본에서도 직전 관측과의 공백을 본다(공백이 한도를 넘으면 그 표본부터 다시 쌓는다. 한도와 같으면 잇는다).
- 해상도·화각 변경(`drift.detect_drift` 의 스트림 근거)은 지속 조건 없이, 카메라 상태(DRIFTED 포함)와 상관없이 처음
  알게 된 때 즉시 근거로 낸다. 그 카메라의 keypoint 는 보정 해상도와 맞지 않으므로 이후 표본에서 뺀다(기준 카메라로도
  쓰지 않는다).
- 귀속 불확실 근거를 낸 뒤에도 지금 신선한 표본이 없으면 그 카메라는 감시 근거 없음이다(과거 이력은
  ambiguous_reported 로 따로 보고한다).
- 모르면 비운다: 창의 신선한 표본이 min_window_samples 개 미만이면 감시 근거 없음(NO_EVIDENCE)이다. 그동안은 틀어졌다고도,
  괜찮다고도 판단하지 않는다(보정 상태를 바꾸지 않고 보고에 감시 근거 없음을 남긴다).

특정 카메라로 확정한 틀어짐(DRIFTED)은 이 감시기(보정 버전 하나)에서 고정하고 그 카메라를 기준에서 뺀다. 귀속 불확실
근거는 카메라마다 한 번만 내고, 귀속된 카메라가 생기면 더 판단하지 않는다(이미 DRIFTED 이고 원인이 정해졌다).
새 보정 버전이 활성화되면 호출자가 새 감시기를 만든다(`calibration.watch`). 시각은 UTC int64 ms 이고 주입받는다.
"""

from __future__ import annotations

import json
import math
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from itertools import combinations
from typing import Any

import numpy as np

from simai_ai.calibration.drift import CameraObservation, DriftFinding, DriftReason, detect_drift
from simai_ai.geometry.rig import RigCamera, UndistortedKeypoints
from simai_ai.geometry.triangulation import triangulate_normalized_many
from simai_ai.providers.contract import is_finite_number
from simai_ai.tracking.time_model import require_ms

# [u, v, confidence] 또는 None (원본 해상도 픽셀)
Keypoint = Sequence[float] | None
# 사람 한 명의 카메라별 keypoint 목록
PersonViews = Mapping[str, Sequence[Keypoint]]

STREAM_REASONS = frozenset({DriftReason.RESOLUTION_CHANGED, DriftReason.FIELD_OF_VIEW_CHANGED})
AMBIGUOUS_PREFIX = "귀속 불확실"


class MonitorStatus(StrEnum):
    """카메라 한 대의 감시 상태."""

    NO_EVIDENCE = "NO_EVIDENCE"
    """신선한 표본이 모자라 판단하지 않는다(사람 없음·3대 미만 관측). 감시 근거 없음"""
    BASELINE_LEARNING = "BASELINE_LEARNING"
    """표본은 있지만 기준선을 아직 정하지 못했다(절대 상한은 본다)"""
    MONITORING = "MONITORING"
    """기준 안"""
    SUSPECT = "SUSPECT"
    """기준을 넘었지만 지속 시간을 채우지 못했다"""
    AMBIGUOUS = "AMBIGUOUS"
    """이 카메라가 낀 불일치가 지속됐지만 원인 카메라를 가릴 수 없다(기준 쌍 부족). 보정 틀어짐 근거는 냈다"""
    DRIFTED = "DRIFTED"
    """이 카메라의 재투영 오차 증가가 지속돼 틀어짐으로 확정했다"""
    STREAM_CHANGED = "STREAM_CHANGED"
    """해상도·화각이 바뀌어 즉시 틀어짐으로 확정했다"""


class MonitorEvidence(StrEnum):
    """보고 하나의 감시 근거. 판정을 끝낸 카메라(DRIFTED·STREAM_CHANGED)는 세지 않는다."""

    FULL = "FULL"
    """남은 카메라 모두 검증된 기준선과 신선한 표본이 있다"""
    UNVERIFIED = "UNVERIFIED"
    """남은 카메라 모두 감시 중이지만 일부 기준선이 미확정이다(감시 시작 전 틀어짐은 절대 상한으로만 본다)"""
    PARTIAL = "PARTIAL"
    """일부 카메라만 감시 근거가 있다"""
    NONE = "NONE"
    """어느 카메라도 감시 근거가 없다(사람 없음). 보정 상태를 근거 없이 유지·전환하지 않는다"""


@dataclass(frozen=True)
class MonitorConfig:
    """감시 설정 초기값. SIM 시험(tests/calibration/drift)의 감지 시간·오경보 수는 상한이다.

    창 중앙값은 틀어진 뒤 표본이 창의 절반을 넘어야 기준을 넘는다. 사람이 계속 지나가면 감지 시간은 약
    window_ms / 2 + sustain_ms(기본 약 35초)이고, window_ms / 2 보다 짧은 이상(순간 오검출·가림)은 기준을 넘지 못한다.
    """

    min_keypoint_confidence: float = 0.3
    """삼각측량 기본값과 같다"""
    min_ray_angle_rad: float = math.radians(5.0)
    """기준 쌍의 시선 직선 사이 각 하한 (삼각측량 VALID 하한과 같다)"""
    min_joints_per_sample: int = 4
    """사람 한 명·카메라 하나의 표본에 필요한 관절 수"""
    refine_iterations: int = 5
    window_ms: int = 30_000
    min_window_samples: int = 20
    max_sample_age_ms: int = 5_000
    """마지막 표본이 이보다 오래되면 신선한 근거가 없다고 본다"""
    baseline_min_samples: int = 60
    baseline_min_span_ms: int = 10_000
    increase_ratio: float = 2.0
    increase_margin_px: float = 3.0
    """잡음으로 기준선이 작을 때 작은 흔들림을 틀어짐으로 보지 않게 하는 절대 여유"""
    absolute_max_median_px: float | None = 8.0
    """기준선과 무관한 창 중앙값 상한 (15.1 G5 한 카메라 제외 재투영 p95 8px). None 이면 쓰지 않는다"""
    sustain_ms: int = 20_000
    evidence_gap_ms: int = 30_000

    def __post_init__(self) -> None:
        for name in ("min_keypoint_confidence", "min_ray_angle_rad", "increase_margin_px"):
            value = getattr(self, name)
            if not is_finite_number(value) or value < 0:
                raise ValueError(f"MonitorConfig.{name} 는 유한한 0 이상의 수여야 한다: {value!r}")
        if not is_finite_number(self.increase_ratio) or self.increase_ratio < 1:
            raise ValueError(f"MonitorConfig.increase_ratio 는 1 이상이어야 한다: {self.increase_ratio!r}")
        if self.absolute_max_median_px is not None and (
            not is_finite_number(self.absolute_max_median_px) or self.absolute_max_median_px <= 0
        ):
            raise ValueError(f"absolute_max_median_px 는 양수 또는 None 이어야 한다: {self.absolute_max_median_px!r}")
        for name in ("min_joints_per_sample", "window_ms", "min_window_samples", "baseline_min_samples"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"MonitorConfig.{name} 는 1 이상의 정수여야 한다: {value!r}")
        for name in ("refine_iterations", "baseline_min_span_ms", "sustain_ms", "evidence_gap_ms", "max_sample_age_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"MonitorConfig.{name} 는 0 이상의 정수여야 한다: {value!r}")


BASELINE_SOURCE_CALIBRATION_INPUT = "CALIBRATION_INPUT"
BASELINE_SOURCE_LEARNED = "LEARNED_IN_OPERATION"


@dataclass(frozen=True)
class BaselineRecord:
    """보정 버전 하나의 카메라별 기준선(px, 귀속 가능 표본의 중앙값)과 검증 근거.

    verified_camera_ids: 그 보정을 만든 입력 관측에서 잰 카메라(`baseline_from_observations`). 나머지는 운영 중에 배운
    미확정 기준선이다. 저장·복원해도 이 구분이 그대로 따라간다.
    """

    calibration_version: str
    values_px: Mapping[str, float]
    verified_camera_ids: tuple[str, ...] = ()
    sources: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CameraMonitorReport:
    """카메라 한 대의 감시 결과. window_*·baseline_* 는 귀속 가능한 표본 기준이다. 모르는 값은 None."""

    camera_id: str
    status: MonitorStatus
    window_samples: int
    window_median_px: float | None
    window_p95_px: float | None
    baseline_median_px: float | None
    baseline_verified: bool
    """기준선이 그 보정의 입력 관측에서 잰 것이고 절대 상한 안인지. 아니면 기준선 미확정"""
    threshold_px: float | None
    exceed_ms: int
    """신선한 평가에서 쌓인 초과 지속 시간"""
    last_sample_ms: int | None
    ambiguous_window_samples: int = 0
    ambiguous_window_median_px: float | None = None
    window_last_sample_ms: int | None = None
    """귀속 가능 창의 마지막 표본 시각"""
    ambiguous_reported: bool = False
    """이 감시기에서 귀속 불확실 근거를 낸 적이 있는지(과거 이력. 현재 근거와 별개)"""
    change_point_ms: int | None = None
    """귀속 가능 오차가 연속 상승하기 시작한 시각. 모르면 None."""


@dataclass(frozen=True)
class MonitorReport:
    """감시 갱신 한 번의 결과. findings 는 이번 갱신에서 새로 확정한 틀어짐 근거다(없으면 빈 tuple)."""

    t_ms: int
    calibration_version: str
    cameras: tuple[CameraMonitorReport, ...]
    findings: tuple[DriftFinding, ...]
    min_window_samples: int = 1
    max_sample_age_ms: int | None = None
    fusion_3plus_ratio: float | None = None

    @property
    def evidence(self) -> MonitorEvidence:
        open_ = [c for c in self.cameras if c.status not in (MonitorStatus.DRIFTED, MonitorStatus.STREAM_CHANGED)]
        evidenced = [
            c for c in open_ if c.status in (MonitorStatus.MONITORING, MonitorStatus.SUSPECT, MonitorStatus.AMBIGUOUS)
        ]
        if open_ and not evidenced:
            return MonitorEvidence.NONE
        if len(evidenced) != len(open_):
            return MonitorEvidence.PARTIAL
        return MonitorEvidence.FULL if all(c.baseline_verified for c in evidenced) else MonitorEvidence.UNVERIFIED

    @property
    def unmonitored_camera_ids(self) -> tuple[str, ...]:
        """감시 근거가 없는 카메라(표본 부족·기준선 학습 중)."""
        return tuple(
            c.camera_id
            for c in self.cameras
            if c.status in (MonitorStatus.NO_EVIDENCE, MonitorStatus.BASELINE_LEARNING)
        )

    def camera(self, camera_id: str) -> CameraMonitorReport:
        for c in self.cameras:
            if c.camera_id == camera_id:
                return c
        raise KeyError(camera_id)

    def reprojection_errors(self) -> list[dict[str, Any]]:
        """calibration_update 의 cameras 항목: 창 표본이 min_window_samples 개 이상이고 신선한 카메라만(모르면 비움)."""
        out = []
        for c in self.cameras:
            stale = (
                self.max_sample_age_ms is not None
                and c.window_last_sample_ms is not None
                and self.t_ms - c.window_last_sample_ms > self.max_sample_age_ms
            )
            if c.window_samples >= self.min_window_samples and c.window_median_px is not None and not stale:
                assert c.window_p95_px is not None
                out.append(
                    {
                        "camera_id": c.camera_id,
                        "reprojection_error_px": {
                            "median": round(c.window_median_px, 3),
                            "p95": round(c.window_p95_px, 3),
                        },
                    }
                )
        return out


@dataclass
class _Judge:
    """표본 한 종류(귀속 가능·귀속 불확실)의 창·기준선·지속 판정."""

    cfg: MonitorConfig
    baseline: float | None = None
    baseline_trusted: bool = False
    window: deque[tuple[int, float]] = field(default_factory=deque)
    baseline_samples: list[float] = field(default_factory=list)
    baseline_first_ms: int | None = None
    baseline_last_ms: int | None = None
    exceed_ms: int = 0
    exceeding: bool = False
    last_sample_ms: int | None = None
    last_eval_ms: int | None = None
    last_progress_ms: int | None = None
    pending_new: int = 0

    def add(self, t_ms: int, error: float) -> None:
        # 관측이 evidence_gap_ms 넘게 끊겼다가 돌아오면 끊기기 전 지속 시간을 버린다. 마지막 관측 시각을 새 시각으로
        # 덮기 전에 본다(빈 호출 빈도·복귀 표본 수와 무관)
        if self.last_sample_ms is not None and t_ms - self.last_sample_ms > self.cfg.evidence_gap_ms:
            self._reset()
        self.window.append((t_ms, error))
        self.last_sample_ms = t_ms
        self.pending_new += 1
        if self.baseline is None:
            self.baseline_samples.append(error)
            self.baseline_first_ms = t_ms if self.baseline_first_ms is None else self.baseline_first_ms
            self.baseline_last_ms = t_ms

    @property
    def baseline_usable(self) -> bool:
        ceiling = self.cfg.absolute_max_median_px
        return self.baseline is not None and (ceiling is None or self.baseline <= ceiling)

    def threshold(self) -> float | None:
        cfg = self.cfg
        relative = None
        if self.baseline_usable:
            assert self.baseline is not None
            relative = max(self.baseline * cfg.increase_ratio, self.baseline + cfg.increase_margin_px)
        ceiling = cfg.absolute_max_median_px
        if relative is None:
            return ceiling
        return relative if ceiling is None else min(relative, ceiling)

    def _reset(self) -> None:
        self.exceed_ms = 0
        self.exceeding = False
        self.last_progress_ms = None

    def evaluate(self, t_ms: int) -> tuple[MonitorStatus, bool]:
        """(상태, 이번 평가로 지속 조건을 채웠는지)."""
        cfg = self.cfg
        while self.window and self.window[0][0] <= t_ms - cfg.window_ms:
            self.window.popleft()
        # 호출 자체가 오래 끊겼으면 쌓인 지속 시간을 버린다(표본 수 검사보다 먼저)
        if self.last_eval_ms is not None and t_ms - self.last_eval_ms > cfg.evidence_gap_ms:
            self._reset()
        self.last_eval_ms = t_ms
        if (
            self.baseline is None
            and len(self.baseline_samples) >= cfg.baseline_min_samples
            and self.baseline_last_ms is not None
            and self.baseline_first_ms is not None
            and self.baseline_last_ms - self.baseline_first_ms >= cfg.baseline_min_span_ms
        ):
            self.baseline = float(np.median(self.baseline_samples))
            self.baseline_samples = []
        new, self.pending_new = self.pending_new, 0
        fresh = self.last_sample_ms is not None and t_ms - self.last_sample_ms <= cfg.max_sample_age_ms
        if len(self.window) < cfg.min_window_samples or not fresh:
            if self.last_sample_ms is None or t_ms - self.last_sample_ms > cfg.evidence_gap_ms:
                self._reset()
            return MonitorStatus.NO_EVIDENCE, False
        median = float(np.median([e for _, e in self.window]))
        threshold = self.threshold()
        if threshold is not None and median > threshold:
            if not new:
                # 새 관측 없는 호출: 창 통계가 신선해도 지속을 진전·완료하지 않는다
                return MonitorStatus.SUSPECT, False
            if self.exceeding and self.last_progress_ms is not None:
                self.exceed_ms += min(t_ms - self.last_progress_ms, cfg.max_sample_age_ms)
            self.exceeding = True
            self.last_progress_ms = t_ms
            return MonitorStatus.SUSPECT, self.exceed_ms >= cfg.sustain_ms
        self._reset()
        return (MonitorStatus.BASELINE_LEARNING if self.baseline is None else MonitorStatus.MONITORING), False

    def stats(self) -> tuple[int, float | None, float | None]:
        values = [e for _, e in self.window]
        if not values:
            return 0, None, None
        return len(values), float(np.median(values)), float(np.percentile(values, 95))


@dataclass
class _CameraTrack:
    attributed: _Judge
    ambiguous: _Judge
    final_status: MonitorStatus | None = None
    ambiguous_reported: bool = False
    change_point_ms: int | None = None
    change_level: float | None = None
    change_run: list[tuple[int, float]] = field(default_factory=list)

    def observe_change(self, t_ms: int, errors: list[float]) -> None:
        value = float(np.median(errors))
        threshold = self.attributed.threshold()
        if threshold is None:
            return
        # 최초 상승은 정상 문턱, 복구 도중 재이동은 직전 상승 수준 대비 변화로 추정한다.
        changed = value > threshold if self.change_level is None else abs(value - self.change_level) > max(3., self.change_level * .5)
        if not changed:
            self.change_run.clear()
            return
        if self.change_run and t_ms - self.change_run[-1][0] > self.attributed.cfg.max_sample_age_ms:
            self.change_run.clear()
        if not self.change_run or self.change_run[-1][0] != t_ms:
            self.change_run.append((t_ms, value))
        if len(self.change_run) >= 3:
            self.change_point_ms = self.change_run[0][0]
            self.change_level = float(np.median([v for _, v in self.change_run]))
            self.change_run.clear()


class ReprojectionMonitor:
    """보정 버전 하나의 운영 중 재투영 오차 감시기. 보정 문서(계약 calibration)의 카메라를 감시한다.

    baseline: 이 보정 버전의 기준선 기록(`BaselineRecord`). 다른 버전의 기록은 쓰지 않는다. verified_camera_ids 에 든
      카메라만 검증된 기준선이고, 나머지 값은 미확정 기준선으로 쓴다. 없는 카메라는 운영 중에 배운다(미확정).
    """

    def __init__(
        self,
        calibration: Mapping[str, Any],
        config: MonitorConfig = MonitorConfig(),
        *,
        baseline: BaselineRecord | None = None,
    ) -> None:
        self.config = config
        self._doc = json.loads(json.dumps(calibration))
        self.calibration_version: str = self._doc["calibration_version"]
        self._cameras = {cam["camera_id"]: RigCamera.from_calibration(cam) for cam in self._doc["cameras"]}
        record = baseline if baseline is not None and baseline.calibration_version == self.calibration_version else None
        given = dict(record.values_px) if record is not None else {}
        verified = set(record.verified_camera_ids) if record is not None else set()
        self._sources = dict(record.sources) if record is not None else {}
        self._tracks = {}
        for cid in self._cameras:
            base = given.get(cid)
            base = float(base) if is_finite_number(base) and base >= 0 else None
            self._tracks[cid] = _CameraTrack(
                attributed=_Judge(config, base, baseline_trusted=base is not None and cid in verified),
                ambiguous=_Judge(config, None, baseline_trusted=False),
            )
        self._stream_changed: set[str] = set()
        self._last_t: int | None = None

    @property
    def camera_ids(self) -> tuple[str, ...]:
        return tuple(self._cameras)

    @property
    def stream_changed_camera_ids(self) -> tuple[str, ...]:
        """해상도·화각이 바뀐 카메라(이 감시기가 알게 된 전부)."""
        return tuple(sorted(self._stream_changed))

    def baseline_record(self) -> BaselineRecord:
        """정해진 카메라별 기준선과 검증 여부. 저장해 두었다가 다시 줘도 검증 여부가 그대로 따라간다."""
        values = {cid: tr.attributed.baseline for cid, tr in self._tracks.items() if tr.attributed.baseline is not None}
        verified = tuple(sorted(cid for cid in values if self._tracks[cid].attributed.baseline_trusted))
        sources = {
            cid: self._sources.get(cid, BASELINE_SOURCE_LEARNED) if cid in verified else BASELINE_SOURCE_LEARNED
            for cid in values
        }
        return BaselineRecord(self.calibration_version, values, verified, sources)

    def observe(
        self,
        t_ms: int,
        people: Iterable[PersonViews],
        streams: Iterable[CameraObservation] = (),
    ) -> MonitorReport:
        """공통 시각 t_ms 의 사람별 카메라 keypoint 로 감시를 갱신한다.

        streams: 카메라별 현재 스트림 정보(해상도·화각 변경 통지). 재투영 오차 값은 보지 않는다(이 감시기가 잰다).
        """
        require_ms("t_ms", t_ms)
        self._check_monotonic(t_ms)
        stream_only = [
            CameraObservation(o.camera_id, o.image_size, None, None, o.field_of_view_changed) for o in streams
        ]
        new_findings: list[DriftFinding] = []
        for finding in detect_drift(self._doc, stream_only):
            if finding.reason in STREAM_REASONS and finding.camera_id not in self._stream_changed:
                self._stream_changed.add(finding.camera_id)
                track = self._tracks[finding.camera_id]
                track.final_status = MonitorStatus.STREAM_CHANGED  # DRIFTED 였어도 스트림 변경을 앞세운다
                track.attributed.window.clear()  # 바뀐 스트림에서는 이 보정의 재투영 오차를 모른다
                track.ambiguous.window.clear()
                new_findings.append(finding)
        usable = {cid: cam for cid, cam in self._cameras.items() if cid not in self._stream_changed}
        samples = leave_one_out_samples(usable, people, self.config,
            excluded_reference_camera_ids=tuple(cid for cid, track in self._tracks.items()
                                                if track.final_status is MonitorStatus.DRIFTED))
        return self._ingest(t_ms, samples, new_findings)

    def ingest(self, t_ms: int, samples: Iterable[tuple[str, float] | tuple[str, float, bool]]) -> MonitorReport:
        """이미 잰 표본으로 판정만 갱신한다. 표본은 (camera_id, 오차 px[, 귀속 가능]) 이고 기본은 귀속 가능이다."""
        require_ms("t_ms", t_ms)
        self._check_monotonic(t_ms)
        return self._ingest(t_ms, samples, [])

    # ------------------------------------------------------------------ 내부

    def _check_monotonic(self, t_ms: int) -> None:
        if self._last_t is not None and t_ms < self._last_t:
            raise ValueError(f"t_ms {t_ms} 가 직전 갱신 {self._last_t} 보다 이르다")
        self._last_t = t_ms

    def _ingest(
        self, t_ms: int, samples: Iterable[tuple[str, float] | tuple[str, float, bool]], findings: list[DriftFinding]
    ) -> MonitorReport:
        change_samples: dict[str, list[float]] = {}
        for sample in samples:
            camera_id, error = sample[0], sample[1]
            attributable = True if len(sample) < 3 else bool(sample[2])  # type: ignore[misc]
            track = self._tracks.get(camera_id)
            if track is None or camera_id in self._stream_changed or not is_finite_number(error) or error < 0:
                continue
            (track.attributed if attributable else track.ambiguous).add(t_ms, float(error))
            if attributable:
                change_samples.setdefault(camera_id, []).append(float(error))

        for cid, errors in change_samples.items():
            self._tracks[cid].observe_change(t_ms, errors)

        evaluated = {
            cid: (tr.attributed.evaluate(t_ms), tr.ambiguous.evaluate(t_ms)) for cid, tr in self._tracks.items()
        }
        for cid, ((_, done), _) in evaluated.items():
            track = self._tracks[cid]
            if done and track.final_status is None:
                track.final_status = MonitorStatus.DRIFTED
                n, median, _ = track.attributed.stats()
                findings.append(
                    DriftFinding(
                        cid,
                        DriftReason.REPROJECTION_INCREASED,
                        f"한 카메라 제외 재투영 오차 창 중앙값 {median:.2f}px > 기준 {track.attributed.threshold():.2f}px "
                        f"({_baseline_text(track.attributed)}, {track.attributed.exceed_ms / 1000:.0f}s 지속, 표본 {n})",
                    )
                )
        attributed_any = any(tr.final_status is MonitorStatus.DRIFTED for tr in self._tracks.values())
        if not attributed_any:
            for cid, (_, (_, done)) in evaluated.items():
                track = self._tracks[cid]
                if done and track.final_status is None and not track.ambiguous_reported:
                    track.ambiguous_reported = True
                    n, median, _ = track.ambiguous.stats()
                    findings.append(
                        DriftFinding(
                            cid,
                            DriftReason.REPROJECTION_INCREASED,
                            f"{AMBIGUOUS_PREFIX}: 이 카메라가 낀 기준 쌍 하나뿐인 재투영 오차 창 중앙값 {median:.2f}px > "
                            f"기준 {track.ambiguous.threshold():.2f}px ({track.ambiguous.exceed_ms / 1000:.0f}s 지속, "
                            f"표본 {n}). 원인 카메라는 정하지 않는다",
                        )
                    )

        reports = []
        for cid, track in self._tracks.items():
            (attr_status, _), (amb_status, _) = evaluated[cid]
            if track.final_status is not None:
                status = track.final_status
            elif (
                track.ambiguous_reported
                and not attributed_any
                and (attr_status is not MonitorStatus.NO_EVIDENCE or amb_status is not MonitorStatus.NO_EVIDENCE)
            ):
                status = MonitorStatus.AMBIGUOUS  # 지금도 신선한 표본이 있을 때만(과거 이력은 ambiguous_reported)
            elif attr_status is MonitorStatus.NO_EVIDENCE and amb_status is not MonitorStatus.NO_EVIDENCE:
                status = amb_status
            else:
                status = attr_status
            n, median, p95 = track.attributed.stats()
            an, amedian, _ = track.ambiguous.stats()
            judge = track.attributed if attr_status is not MonitorStatus.NO_EVIDENCE else track.ambiguous
            reports.append(
                CameraMonitorReport(
                    camera_id=cid,
                    status=status,
                    window_samples=n,
                    window_median_px=median,
                    window_p95_px=p95,
                    baseline_median_px=track.attributed.baseline,
                    baseline_verified=bool(judge.baseline_trusted and judge.baseline_usable),
                    threshold_px=track.attributed.threshold(),
                    exceed_ms=max(track.attributed.exceed_ms, track.ambiguous.exceed_ms),
                    last_sample_ms=_latest(track.attributed.last_sample_ms, track.ambiguous.last_sample_ms),
                    ambiguous_window_samples=an,
                    ambiguous_window_median_px=amedian,
                    window_last_sample_ms=track.attributed.window[-1][0] if track.attributed.window else None,
                    ambiguous_reported=track.ambiguous_reported,
                    change_point_ms=track.change_point_ms,
                )
            )
        return MonitorReport(
            t_ms,
            self.calibration_version,
            tuple(reports),
            tuple(findings),
            self.config.min_window_samples,
            self.config.max_sample_age_ms,
        )


def _baseline_text(judge: _Judge) -> str:
    if judge.baseline is None:
        return "기준선 미확정, 절대 상한"
    if not judge.baseline_usable:
        return f"학습 기준선 {judge.baseline:.2f}px 이 절대 상한 초과라 쓰지 않음"
    return f"기준선 {judge.baseline:.2f}px{'' if judge.baseline_trusted else ' (미확정)'}"


def _latest(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def baseline_from_observations(
    calibration: Mapping[str, Any], people: Iterable[PersonViews], config: MonitorConfig = MonitorConfig()
) -> BaselineRecord:
    """그 보정을 만든 입력 관측(보정 걷기·재보정 관측)으로 잰 검증된 기준선.

    운영 감시와 같은 한 카메라 제외 통계(귀속 가능 표본)의 카메라별 중앙값이다. 표본이 min_window_samples 개 미만인
    카메라는 넣지 않는다(모르면 비움). 절대 상한을 넘는 값은 넣되 감시기가 쓰지 않는다.
    """
    cams = {cam["camera_id"]: RigCamera.from_calibration(cam) for cam in calibration["cameras"]}
    by_camera: dict[str, list[float]] = {}
    for cid, error, attributable in leave_one_out_samples(cams, list(people), config):
        if attributable:
            by_camera.setdefault(cid, []).append(error)
    values = {cid: float(np.median(v)) for cid, v in by_camera.items() if len(v) >= config.min_window_samples}
    return BaselineRecord(
        calibration["calibration_version"],
        values,
        tuple(sorted(values)),
        {cid: BASELINE_SOURCE_CALIBRATION_INPUT for cid in values},
    )


# --------------------------------------------------------------------------- 표본


def leave_one_out_samples(
    cameras: Mapping[str, RigCamera], people: Iterable[PersonViews], config: MonitorConfig = MonitorConfig(),
    *, excluded_reference_camera_ids: Sequence[str] = (),
) -> list[tuple[str, float, bool]]:
    """사람별 카메라 keypoint → 표본 (camera_id, 한 카메라 제외 재투영 오차 중앙값 px, 귀속 가능) 목록.

    귀속 가능한 관절(쓸 수 있었던 기준 쌍 모두에 들어 있는 공통 카메라가 없다)이 min_joints_per_sample 개 이상이면 그
    관절들로 귀속 가능 표본을 하나 만든다. 아니면 쓸 수 있었던 관절 전부로 귀속 불확실 표본을 만든다.
    cameras 에 없는 카메라의 view 는 쓰지 않는다. 카메라마다 keypoint 수가 다른 사람은 건너뛴다(관절 배치가 다르다).
    왜곡 제거는 카메라마다 그 시각 모든 사람의 view 를 한 번에 한다(`RigCamera.undistort_many`).
    """
    persons: list[dict[str, Sequence[Keypoint]]] = []
    for views in people:
        own = {cid: kps for cid, kps in views.items() if cid in cameras}
        if len(own) < 3 or len({len(kps) for kps in own.values()}) != 1:
            continue
        persons.append(own)
    if not persons:
        return []

    undistorted: dict[tuple[int, str], UndistortedKeypoints] = {}
    for cid, cam in cameras.items():
        idx = [p for p, views in enumerate(persons) if cid in views]
        if idx:
            for p, und in zip(idx, cam.undistort_many([persons[p][cid] for p in idx])):
                undistorted[(p, cid)] = und
    usable = {key: und.usable(config.min_keypoint_confidence) for key, und in undistorted.items()}
    best = {key: np.full(len(mask), np.inf) for key, mask in usable.items()}
    camera_ids = sorted(cameras)
    bit = {cid: 1 << i for i, cid in enumerate(camera_ids)}
    all_bits = (1 << len(camera_ids)) - 1
    # 관절마다 쓸 수 있었던 기준 쌍의 공통 카메라(비트 교집합)와 쌍 수
    common = {key: np.full(len(mask), all_bits, dtype=np.int64) for key, mask in usable.items()}
    pairs = {key: np.zeros(len(mask), dtype=np.int64) for key, mask in usable.items()}

    for c in camera_ids:
        for a, b in combinations([cid for cid in camera_ids if cid != c and cid not in excluded_reference_camera_ids], 2):
            rows = []
            for p in range(len(persons)):
                if (p, c) in usable and (p, a) in usable and (p, b) in usable:
                    joints = np.flatnonzero(usable[(p, c)] & usable[(p, a)] & usable[(p, b)])
                    if len(joints):
                        rows.append((p, joints))
            if not rows:
                continue
            batch = [_Rows(p, joints, undistorted, (c, a, b)) for p, joints in rows]
            errors = _pair_errors(cameras[c], cameras[a], cameras[b], batch, config)
            start = 0
            for p, joints in rows:
                part = errors[start : start + len(joints)]
                start += len(joints)
                valid = np.isfinite(part)
                key = (p, c)
                best[key][joints] = np.minimum(best[key][joints], part)
                common[key][joints[valid]] &= bit[a] | bit[b]
                pairs[key][joints[valid]] += 1

    out = []
    for key, values in best.items():
        finite = np.isfinite(values)
        protected = finite & (pairs[key] >= 2) & (common[key] == 0)
        if protected.sum() >= config.min_joints_per_sample:
            out.append((key[1], float(np.median(values[protected])), True))
        elif finite.sum() >= config.min_joints_per_sample:
            out.append((key[1], float(np.median(values[finite])), False))
    return out


@dataclass(frozen=True)
class _Rows:
    """한 사람의 관절 묶음: 카메라 c·a·b 모두에서 쓸 수 있는 관절의 왜곡 제거 결과."""

    person: int
    joints: np.ndarray
    undistorted: Mapping[tuple[int, str], UndistortedKeypoints]
    ids: tuple[str, str, str]

    def take(self, k: int, attr: str) -> np.ndarray:
        return getattr(self.undistorted[(self.person, self.ids[k])], attr)[self.joints]


def _pair_errors(
    cam_c: RigCamera, cam_a: RigCamera, cam_b: RigCamera, rows: Sequence[_Rows], config: MonitorConfig
) -> np.ndarray:
    """a·b 로 삼각측량한 관절을 c 에 투영한 오차 (M,). 해가 없거나 시선각이 작거나 c 뒤면 inf."""

    def gather(k: int, attr: str) -> np.ndarray:
        return np.concatenate([r.take(k, attr) for r in rows])

    normalized = np.stack([gather(1, "normalized"), gather(2, "normalized")], axis=1)
    targets = np.stack([gather(1, "ideal_pixels"), gather(2, "ideal_pixels")], axis=1)
    X = triangulate_normalized_many([cam_a, cam_b], normalized, None, config.refine_iterations, targets)
    errors = np.full(len(X), np.inf)
    ok = np.isfinite(X).all(axis=1)
    if not ok.any():
        return errors
    d1, d2 = X[ok] - cam_a.center, X[ok] - cam_b.center
    cos = np.abs(np.sum(d1 * d2, axis=1)) / (np.linalg.norm(d1, axis=1) * np.linalg.norm(d2, axis=1))
    angle_ok = np.arccos(np.clip(cos, 0.0, 1.0)) >= config.min_ray_angle_rad
    Xc = cam_c.to_camera(X[ok])
    good = angle_ok & (Xc[:, 2] > 1e-6)
    idx = np.flatnonzero(ok)[good]
    if len(idx):
        projected = cam_c.ideal_pixels(Xc[good, :2] / Xc[good, 2:3])
        errors[idx] = np.linalg.norm(projected - gather(0, "ideal_pixels")[idx], axis=1)
    return errors
