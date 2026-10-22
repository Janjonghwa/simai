"""DRIFTED 뒤 자동 재보정 시작 (명세 4.4 DRIFTED "자동 재보정 시작", 6.5 캘리브레이션 틀어짐, 18장 DRIFTED 운영).

- 트리거(`RecalibrationTrigger`)는 언제 시작할지만 정한다. DRIFTED 로 표시한 뒤에 들어온 사람 관측만 모은다. 틀어지기
  전 관측을 섞으면 한 카메라의 두 자세가 섞인다. 충분히 모이면(min_collect_ms 이상, min_observations 개 이상)
  `RecalibrationRequest` 를 한 번 낸다. 번들 조정은 수 초 걸리므로 호출자가 LIVE 처리 밖(백그라운드)에서 돌린다.
- 실행(`run_recalibration`)은 -104 번들 조정 진입점 `adjust_human_bundle` 을 그대로 부른다. 초기값은 틀어진 보정(활성
  버전)의 K·왜곡·R·t 다. 내부 파라미터 사전 분포는 좁게 둔다(거치대가 돌아가도 렌즈는 그대로다).
- 틀어진 카메라의 회전 초기값만 먼저 맞춘다(`realign_rotation`): 다른 카메라 두 대로 삼각측량한 관절과 틀어진 카메라의
  관측 시선을 맞추는 회전(카메라 중심은 그대로, 거치대 회전 가정)을 Kabsch 로 구한다. 번들 조정의 축 게이지는 초기
  외부 파라미터 전체에서 정해지므로, 한 카메라의 틀린 초기 회전이 그대로 들어가면 재보정 결과의 World 축이 그만큼
  (4대면 약 1/4) 돌아간다(SIM: 1° 회전 → 약 0.25°). 대응이 모자라면 맞추지 않고 원래 값을 쓴다.
- 판정은 하지 않는다. 조정이 수렴하면 `HumanBundleResult.propose_to` 로 버전 저장소에 넣고, CALIBRATED/PROVISIONAL 은
  저장소의 criteria 가 정한다. 운영 중에는 독립 검산점(4.4, 키를 아는 사람이 선 3~5곳)이 없으므로 기준상 PROVISIONAL
  이 된다. CALIBRATED 로 되돌리려면 4.4 검산을 다시 한다(18장: 위치 복구 후 한 명 걷기로 재보정).
- 틀어진 카메라의 회전 초기값을 맞추지 못하면(대응 부족) 요청하지 않고 더 모은다. 원인 카메라를 모르면(귀속 불확실)
  호출자(`watch`)가 트리거를 시작하지 않는다. 둘 다 World 축 게이지를 보장하지 못하는 결과를 자동 채택하지 않으려는 것이다.
- 조정이 실패·불확실하면 제안하지 않는다. 트리거는 retry_after_ms 뒤에 관측을 새로 모아 다시 시도한다.
- 요청을 낸 뒤 활성 버전이 바뀌었거나 더 이상 DRIFTED 가 아니면 결과를 넣지 않는다(낡은 요청).
- 입력 출처(input_provenance)를 그대로 넘긴다. SIM·정답 입력이면 결과 출처가 SIM·DATASET_GT 이고 지표는 상한이다.

scipy 가 필요하다(extras calibration, 번들 조정과 같다).
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import combinations
from typing import Any

import numpy as np

from simai_ai.calibration.bundle import (
    STATUS_CONVERGED,
    BundleConfig,
    HumanBundleResult,
    PersonObservation,
    adjust_human_bundle,
)
from simai_ai.calibration.intrinsics.record import BundleAdjustmentInit, CameraIntrinsicsInit
from simai_ai.calibration.monitor import BaselineRecord, MonitorConfig, baseline_from_observations
from simai_ai.calibration.versions import CalibrationVersionStore, ProposalResult
from simai_ai.geometry.rig import RigCamera
from simai_ai.geometry.triangulation import triangulate_normalized_many
from simai_ai.providers.contract import check_input_provenance, is_finite_number
from simai_ai.tracking.system_health import CalibrationState
from simai_ai.tracking.time_model import require_ms


@dataclass(frozen=True)
class RecalibrationConfig:
    min_collect_ms: int = 30_000
    """DRIFTED 표시 뒤 최소 수집 시간"""
    min_observations: int = 60
    """요청에 필요한 사람 관측(사람 한 명·공통 시각 하나) 수"""
    max_observations: int = 600
    """모아 두는 관측 수 상한(번들 조정 시간 상한). 넘으면 가장 오래된 관측부터 내보낸다"""
    sample_period_ms: int = 300
    """같은 사람의 관측은 이 간격보다 촘촘하게 모으지 않는다(번들 조정 시험의 공통 시각 간격과 같다)"""
    retry_after_ms: int = 60_000
    """조정이 실패·불확실해 제안하지 못했을 때 다시 모으기 시작할 때까지 기다리는 시간"""
    realign_retry_ms: int = 10_000
    """틀어진 카메라 회전 초기값을 맞추지 못했을 때 다시 시도할 때까지 기다리는 시간"""
    focal_prior_rel: float = 0.01
    k_prior_std: tuple[float, float] = (0.01, 0.006)

    def __post_init__(self) -> None:
        for name in ("min_collect_ms", "sample_period_ms", "retry_after_ms", "realign_retry_ms"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"RecalibrationConfig.{name} 는 0 이상의 정수여야 한다: {value!r}")
        if not 1 <= self.min_observations <= self.max_observations:
            raise ValueError("1 <= min_observations <= max_observations 여야 한다")
        values = (self.focal_prior_rel, *self.k_prior_std)
        if len(self.k_prior_std) != 2 or not all(is_finite_number(v) and v > 0 for v in values):
            raise ValueError("focal_prior_rel·k_prior_std 는 유한한 양수여야 한다")


class TriggerPhase(StrEnum):
    IDLE = "IDLE"
    """DRIFTED 가 아니라 모으지 않는다"""
    COLLECTING = "COLLECTING"
    REQUESTED = "REQUESTED"
    """요청을 냈고 결과를 기다린다"""
    WAITING_RETRY = "WAITING_RETRY"
    CANCELLED = "CANCELLED"
    """해상도·화각이 바뀌어 이 보정 버전으로는 자동 재보정을 하지 않는다"""
    """조정이 실패·불확실해 다시 모을 때까지 기다린다"""


@dataclass(frozen=True)
class RecalibrationRequest:
    """번들 조정 한 번의 입력. calibration_version 은 틀어진(재보정 대상) 보정 버전이다."""

    calibration_version: str
    created_at: int
    observations: tuple[PersonObservation, ...]
    intrinsics: BundleAdjustmentInit
    initial_extrinsics: Mapping[str, tuple[Sequence[Sequence[float]], Sequence[float]]]
    input_provenance: Mapping[str, Any]
    realigned_camera_ids: tuple[str, ...] = ()
    """회전 초기값을 먼저 맞춘 카메라(`realign_rotation`)"""


@dataclass(frozen=True)
class RecalibrationOutcome:
    """재보정 실행 결과. proposal 이 None 이면 저장소에 넣지 않았다(skipped_reason 에 이유)."""

    request: RecalibrationRequest
    result: HumanBundleResult | None
    proposal: ProposalResult | None
    skipped_reason: str | None = None
    baseline: BaselineRecord | None = None
    """채택된 새 버전의 검증된 기준선: 재보정 관측으로 잰 한 카메라 제외 통계(`baseline_from_observations`)"""

    @property
    def adopted(self) -> bool:
        return self.proposal is not None and self.proposal.adopted


class RecalibrationTrigger:
    """DRIFTED 동안 사람 관측을 모아 재보정 요청을 낸다. 보정 버전 하나에 하나를 쓴다."""

    def __init__(
        self,
        calibration: Mapping[str, Any],
        input_provenance: Mapping[str, Any],
        config: RecalibrationConfig = RecalibrationConfig(),
    ) -> None:
        problems = check_input_provenance(input_provenance)
        if problems:
            raise ValueError(f"input_provenance 가 계약에 맞지 않는다: {problems}")
        self.config = config
        self.calibration = calibration
        self.calibration_version: str = calibration["calibration_version"]
        self.input_provenance = dict(input_provenance)
        self.phase = TriggerPhase.IDLE
        self._since: int | None = None
        self._observations: deque[PersonObservation] = deque(maxlen=config.max_observations)
        self._last_by_person: dict[str, int] = {}
        self._camera_ids = {cam["camera_id"] for cam in calibration["cameras"]}
        self._drifted: set[str] = set()
        self._next_attempt_ms: int | None = None
        self.realign_failed = False
        """마지막 요청 시도에서 틀어진 카메라 회전 초기값을 맞추지 못했는지(맞출 때까지 요청하지 않는다)"""
        self.issued: RecalibrationRequest | None = None
        """마지막으로 낸 요청"""

    @property
    def collected(self) -> int:
        return len(self._observations)

    def start(self, at_ms: int, drifted_camera_ids: Sequence[str] = ()) -> None:
        """DRIFTED 표시 시각과 틀어진 카메라. 이 시각 이후 관측만 모은다. 이미 시작했으면 카메라만 더한다."""
        require_ms("at_ms", at_ms)
        self._drifted |= {cid for cid in drifted_camera_ids if cid in self._camera_ids}
        if self.phase is TriggerPhase.IDLE:
            self.phase = TriggerPhase.COLLECTING
            self._since = at_ms

    def cancel(self) -> None:
        """이 버전의 자동 재보정을 멈춘다(스트림 변경). 모은 관측을 버리고, 낸 요청은 채택하지 않게 한다."""
        self.phase = TriggerPhase.CANCELLED
        self._observations.clear()
        self._last_by_person = {}

    def add(self, observations: Sequence[PersonObservation]) -> None:
        if self.phase is not TriggerPhase.COLLECTING or self._since is None:
            return
        for obs in observations:
            # 상한에 닿으면 가장 오래된 관측을 내보낸다(순환 버퍼). 회전 초기값을 못 맞춘 관측이 버퍼를 막지 않게 한다
            if obs.t < self._since:
                continue
            last = self._last_by_person.get(obs.person_id)
            if last is not None and obs.t - last < self.config.sample_period_ms:
                continue
            views = {cid: kps for cid, kps in obs.views.items() if cid in self._camera_ids}
            if len(views) < 2:
                continue
            self._observations.append(PersonObservation(obs.person_id, obs.t, views))
            self._last_by_person[obs.person_id] = obs.t

    def poll(self, t_ms: int) -> RecalibrationRequest | None:
        """요청을 낼 때가 되면 한 번 낸다. 아니면 None."""
        require_ms("t_ms", t_ms)
        if self.phase is TriggerPhase.WAITING_RETRY and self._since is not None and t_ms >= self._since:
            self.phase = TriggerPhase.COLLECTING
        if self.phase is not TriggerPhase.COLLECTING or self._since is None:
            return None
        if t_ms - self._since < self.config.min_collect_ms or len(self._observations) < self.config.min_observations:
            return None
        if not self._drifted or (self._next_attempt_ms is not None and t_ms < self._next_attempt_ms):
            return None
        extrinsics = {cam["camera_id"]: (cam["R"], cam["t"]) for cam in self.calibration["cameras"]}
        realigned = []
        for cid in sorted(self._drifted):
            pose = realign_rotation(self.calibration, cid, self._observations, self._drifted)
            if pose is None:
                # 틀린 초기 회전으로 번들 조정하면 World 축이 돈다. 맞출 때까지 더 모은다
                self.realign_failed = True
                self._next_attempt_ms = t_ms + self.config.realign_retry_ms
                return None
            extrinsics[cid] = pose
            realigned.append(cid)
        self.realign_failed = False
        self.phase = TriggerPhase.REQUESTED
        self.issued = RecalibrationRequest(
            calibration_version=self.calibration_version,
            created_at=t_ms,
            observations=tuple(self._observations),
            intrinsics=intrinsics_init(self.calibration, self.config),
            initial_extrinsics=extrinsics,
            input_provenance=self.input_provenance,
            realigned_camera_ids=tuple(realigned),
        )
        return self.issued

    def finished(self, outcome: RecalibrationOutcome, t_ms: int) -> None:
        """요청 결과를 알린다. 채택되지 않았으면 retry_after_ms 뒤에 관측을 새로 모은다."""
        require_ms("t_ms", t_ms)
        if self.phase is not TriggerPhase.REQUESTED or outcome.request.calibration_version != self.calibration_version:
            return
        if outcome.adopted:
            self.phase = TriggerPhase.IDLE
            return
        self.phase = TriggerPhase.WAITING_RETRY
        self._since = t_ms + self.config.retry_after_ms
        self._observations.clear()
        self._last_by_person = {}


def intrinsics_init(
    calibration: Mapping[str, Any], config: RecalibrationConfig = RecalibrationConfig()
) -> BundleAdjustmentInit:
    """활성 보정의 내부 파라미터를 번들 조정 초기값으로. 주점은 보정 값으로 고정하고 사전 분포는 좁게 둔다."""
    cams = []
    for cam in calibration["cameras"]:
        K = cam["K"]
        f = (float(K[0][0]) + float(K[1][1])) / 2
        cams.append(
            CameraIntrinsicsInit(
                camera_id=cam["camera_id"],
                camera={k: cam[k] for k in ("camera_id", "image_size", "K", "distortion")},
                focal_prior_std_px=config.focal_prior_rel * f,
                k_prior_std=config.k_prior_std,
                principal_point_fixed=True,
                flags=("FROM_ACTIVE_CALIBRATION",),
            )
        )
    return BundleAdjustmentInit(
        tuple(cams),
        {"format": "simai.recalibration_init", "calibration_version": calibration["calibration_version"]},
    )


def realign_rotation(
    calibration: Mapping[str, Any],
    camera_id: str,
    observations: Sequence[PersonObservation],
    exclude: Collection[str] = (),
    *,
    min_confidence: float = 0.3,
    min_ray_angle_rad: float = math.radians(5.0),
    min_correspondences: int = 30,
) -> tuple[list[list[float]], list[float]] | None:
    """틀어진 카메라 camera_id 의 회전을 다시 맞춘 (R, t). 카메라 중심은 보정 값 그대로 둔다. 대응이 모자라면 None.

    기준점은 camera_id 와 exclude(다른 틀어진 카메라)를 뺀 카메라 두 대로 삼각측량한 관절이다(시선각 하한 이상).
    World 시선 (X - C)/|X - C| 와 카메라 관측 시선 (x, y, 1)/|.| (왜곡 제거 정규화 좌표)을 맞추는 회전을 Kabsch 로
    풀고, 각도 잔차가 중앙값의 3배(최소 0.2°)를 넘는 대응을 빼고 한 번 더 푼다.
    """
    cams = {cam["camera_id"]: RigCamera.from_calibration(cam) for cam in calibration["cameras"]}
    if camera_id not in cams:
        return None
    target = cams[camera_id]
    refs = sorted(cid for cid in cams if cid != camera_id and cid not in exclude)
    world_rays, cam_rays = [], []
    for obs in observations:
        if camera_id not in obs.views:
            continue
        own = target.undistort_keypoints(obs.views[camera_id])
        own_ok = own.usable(min_confidence)
        views = {cid: cams[cid].undistort_keypoints(obs.views[cid]) for cid in refs if cid in obs.views}
        for a, b in combinations(sorted(views), 2):
            ua, ub = views[a], views[b]
            joints = np.flatnonzero(own_ok & ua.usable(min_confidence) & ub.usable(min_confidence))
            if not len(joints):
                continue
            normalized = np.stack([ua.normalized[joints], ub.normalized[joints]], axis=1)
            targets = np.stack([ua.ideal_pixels[joints], ub.ideal_pixels[joints]], axis=1)
            X = triangulate_normalized_many([cams[a], cams[b]], normalized, None, 5, targets)
            ok = np.isfinite(X).all(axis=1)
            if not ok.any():
                continue
            X, joints = X[ok], joints[ok]
            d1, d2 = X - cams[a].center, X - cams[b].center
            cos = np.abs(np.sum(d1 * d2, axis=1)) / (np.linalg.norm(d1, axis=1) * np.linalg.norm(d2, axis=1))
            wide = np.arccos(np.clip(cos, 0.0, 1.0)) >= min_ray_angle_rad
            w = X - target.center
            n = np.linalg.norm(w, axis=1)
            # 방향을 모르는 대응은 뺀다: 대상 카메라 중심과 같은 점(0/0 = NaN), 넘친 거리(inf 로 나눠 0 벡터·NaN).
            # Kabsch SVD 에 유한하지 않은 값을 넣지 않는다(S15P31A101-245)
            wide &= np.isfinite(n) & (n > 0)
            if not wide.any():
                continue
            world_rays.append(w[wide] / n[wide, None])
            c = np.column_stack([own.normalized[joints[wide]], np.ones(int(wide.sum()))])
            cam_rays.append(c / np.linalg.norm(c, axis=1, keepdims=True))
    if not world_rays:
        return None
    A, B = np.concatenate(world_rays), np.concatenate(cam_rays)
    if len(A) < min_correspondences:
        return None
    R = _kabsch(A, B)
    residual = np.arccos(np.clip(np.sum((A @ R.T) * B, axis=1), -1.0, 1.0))
    keep = residual <= max(3.0 * float(np.median(residual)), math.radians(0.2))
    if keep.sum() < min_correspondences:
        return None
    R = _kabsch(A[keep], B[keep])
    return R.tolist(), (-R @ target.center).tolist()


def _kabsch(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """sum |R a_i - b_i|^2 를 최소로 하는 회전 R (det = +1)."""
    U, _, Vt = np.linalg.svd(B.T @ A)
    D = np.diag([1.0, 1.0, 1.0 if np.linalg.det(U @ Vt) >= 0 else -1.0])
    return U @ D @ Vt


def run_recalibration(
    request: RecalibrationRequest,
    store: CalibrationVersionStore,
    *,
    at_ms: int,
    is_valid: Callable[[RecalibrationRequest], bool],
    bundle_config: BundleConfig = BundleConfig(),
    monitor_config: MonitorConfig = MonitorConfig(),
) -> RecalibrationOutcome:
    """번들 조정을 돌리고, 수렴하면 버전 저장소에 후보로 넣는다(판정은 저장소 criteria). at_ms 는 제안 시각이다.

    is_valid: 반드시 준다. 조정 전과 제안 직전에 요청이 아직 유효한지 묻는다(`CalibrationDriftWatch.request_still_valid`).
    조정하는 동안 스트림이 바뀌었으면 결과를 넣지 않는다. 채택되면 재보정 관측으로 새 버전의 검증된 기준선을 잰다.
    """
    require_ms("at_ms", at_ms)
    if not callable(is_valid):
        raise TypeError("is_valid 콜백이 필요하다(CalibrationDriftWatch.request_still_valid)")
    if _stale(request, store):
        return RecalibrationOutcome(request, None, None, "활성 버전이 바뀌었거나 DRIFTED 가 아니다")
    if not is_valid(request):
        return RecalibrationOutcome(request, None, None, "요청이 무효가 됐다(스트림 변경 등)")
    try:
        result = adjust_human_bundle(
            request.observations,
            request.intrinsics,
            request.initial_extrinsics,
            input_provenance=request.input_provenance,
            created_at=request.created_at,
            config=bundle_config,
        )
    except ValueError as exc:
        return RecalibrationOutcome(request, None, None, f"번들 조정 입력 오류: {exc}")
    if result.status != STATUS_CONVERGED:
        return RecalibrationOutcome(request, result, None, f"번들 조정 {result.status}: {', '.join(result.flags)}")
    if _stale(request, store):
        return RecalibrationOutcome(request, result, None, "조정 중 활성 버전이 바뀌었거나 DRIFTED 가 아니다")
    if not is_valid(request):
        return RecalibrationOutcome(request, result, None, "요청이 무효가 됐다(스트림 변경 등)")
    proposal = result.propose_to(store, at_ms=at_ms)
    baseline = None
    if proposal.adopted:
        baseline = baseline_from_observations(proposal.doc, [o.views for o in request.observations], monitor_config)
    return RecalibrationOutcome(request, result, proposal, baseline=baseline)


def _stale(request: RecalibrationRequest, store: CalibrationVersionStore) -> bool:
    snap = store.snapshot()
    return (
        snap is None
        or snap.calibration_version != request.calibration_version
        or snap.state is not CalibrationState.DRIFTED
    )
