"""내부 파라미터 추정 백엔드 순서: AnyCalib(기본) → GeoCalib(예비) → 고전 기준(가중치 없이 도는 예비) (명세 4.3 1단계).

- **순서와 대체.** 앞 백엔드가 *사용 불가*(`LearnedBackendUnavailable`)일 때만 다음으로 넘어간다. 사용 불가는 선택
  의존성이 없거나, 가중치 인자가 없거나, 로딩 단계에서 가중치 파일이 없거나 해시가 manifest 값과 다른 것이다
  (`learned` 가 로딩 경계에서 이 예외로 바꾼다). 추론 중에 올라온 FileNotFoundError 등 그 밖의 예외는 추론 실패다.
  한 번 사용 불가로 판정된 백엔드는 같은 실행의 다른 카메라에서 다시 시도하지 않는다(가중치 해시를 카메라마다
  다시 계산하지 않는다).
- **실패는 대체하지 않는다.** 백엔드가 돌았는데 결과가 FAILED(모델이 실패를 알림, 값이 범위 밖, 직선성 확인 실패)이거나
  추론 중 예외가 나면 그 카메라는 FAILED 로 비운다. 다른 방법의 값으로 바꿔 채우지 않는다(모르면 비움). 같은 렌즈
  공유 단계(`shared`)가 실패한 카메라를 빼고 합친다.
- **어떤 경로로 나왔는지 남긴다.** 결과의 `method`·`model` 은 실제로 값을 낸 백엔드이고, `ChainResult.attempts` 에
  시도한 백엔드마다 결과(OK·UNAVAILABLE·ERROR)와 사유가 남는다. 학습 백엔드가 모두 사용 불가여서 고전 기준으로
  내려가면 method 는 `PLUMB_LINE_VP` 이고 `fallback` 이 True 다(AnyCalib 결과처럼 보이지 않는다). 순서에 고전 기준이
  없고 모두 사용 불가면 method `NONE`·status FAILED 로 비운다.
- **학습 결과의 직선성 확인.** 고전 기준(plumb-line)이 같은 영상에서 곧게 편 교실 직선(이상치 사슬을 뺀 것)을 학습
  모델의 K·왜곡으로 펴서 직선성 RMS(원본 픽셀)를 잰다. 한도 max(`max_rms_px`, `reference_factor` × 고전 기준 자신의
  RMS) 를 넘거나, 펴기 전에 뚜렷이 휘어 있었는데 절반 넘게 남기거나(고전 기준 자신의 실패 기준과 같다), 학습 모델이
  그 직선 위 점에서 접히면(왜곡 제거 불가) 학습 결과를 FAILED 로 비운다. 이 확인은 왜곡을 본다. 초점 오차는 직선성에
  거의 드러나지 않는다(초점은 같은 렌즈 공유의 편차와 번들 조정이 다룬다). 고전 기준이
  직선을 충분히 찾지 못해(FAILED) 확인할 수 없으면 학습 결과를 그대로 두고 `UNVERIFIED` 로 표시한다(확인 실패가
  아니다). 확인은 학습 결과가 OK 일 때만 한다.

이 모듈은 결과를 보정 버전으로 만들지 않는다. 결과는 번들 조정(-104, 4.3 3단계)의 초기값이다.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from simai_ai.calibration.intrinsics.estimate import (
    METHOD_ANYCALIB,
    METHOD_GEOCALIB,
    METHOD_PLUMB_LINE_VP,
    STATUS_FAILED,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsEstimator,
    IntrinsicsUncertainty,
    WeightsRef,
    undistort_pixels,
)
from simai_ai.calibration.intrinsics.learned import (
    ANYCALIB_INSTALL_HINT,
    GEOCALIB_INSTALL_HINT,
    AnyCalibEstimator,
    GeoCalibEstimator,
    LearnedBackendUnavailable,
)
from simai_ai.calibration.intrinsics.plumbline import PlumbLineConfig, PlumbLineVPEstimator

CHAIN_VERSION = "0.1.0"
METHOD_NONE = "NONE"  # 쓸 수 있는 백엔드가 없어 아무 방법도 값을 내지 않았다

# 백엔드 이름(CLI·기록용)과 방법 값
BACKEND_ANYCALIB = "anycalib"
BACKEND_GEOCALIB = "geocalib"
BACKEND_PLUMBLINE = "plumbline"
BACKEND_METHODS = {
    BACKEND_ANYCALIB: METHOD_ANYCALIB,
    BACKEND_GEOCALIB: METHOD_GEOCALIB,
    BACKEND_PLUMBLINE: METHOD_PLUMB_LINE_VP,
}
DEFAULT_BACKEND_ORDER = (BACKEND_ANYCALIB, BACKEND_GEOCALIB, BACKEND_PLUMBLINE)

ATTEMPT_OK = "OK"  # 백엔드가 돌아 결과를 냈다(결과 status 는 OK·NO_FOCAL·FAILED 중 하나)
ATTEMPT_UNAVAILABLE = "UNAVAILABLE"  # 의존성·가중치가 없거나 해시가 달라 쓸 수 없다 → 다음 백엔드
ATTEMPT_ERROR = "ERROR"  # 추론 중 예외 → 그 카메라는 FAILED (대체하지 않는다)

CHECK_PASSED = "PASSED"
CHECK_FAILED = "FAILED"
CHECK_UNVERIFIED = "UNVERIFIED"

# 사용 불가로 보는 예외(그 밖의 예외는 추론 실패)
# 사용 불가로 보는 예외. 가중치 없음·해시 불일치는 learned 의 로딩 경계가 LearnedBackendUnavailable 로 바꿔 올린다
UNAVAILABLE_ERRORS: tuple[type[BaseException], ...] = (LearnedBackendUnavailable,)


@dataclass(frozen=True)
class StraightnessCheckConfig:
    # 학습 결과로 편 교실 직선의 직선성 RMS(원본 픽셀) 절대 한도. 고전 기준의 `max_straightness_px` 와 같은 초기값
    max_rms_px: float = PlumbLineConfig().max_straightness_px
    # 고전 기준 자신이 편 RMS 의 이 배수까지는 허용한다(압축 잡음 등으로 고전 기준 RMS 자체가 클 때)
    reference_factor: float = 3.0
    # 펴기 전 RMS 가 improvement_check_min_px 보다 컸는데 학습 결과로 편 RMS 가 그 비율보다 크면 실패
    # (왜곡을 덜 편 결과. 고전 기준 자신의 실패 기준과 같은 값)
    improvement_check_min_px: float = PlumbLineConfig().improvement_check_min_px
    max_improvement_ratio: float = PlumbLineConfig().max_improvement_ratio


DEFAULT_STRAIGHTNESS_CHECK = StraightnessCheckConfig()


@dataclass(frozen=True)
class BackendAttempt:
    backend: str
    method: str
    outcome: str
    reason: str | None = None
    status: str | None = None  # outcome OK 일 때 결과 status

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"backend": self.backend, "method": self.method, "outcome": self.outcome}
        if self.status is not None:
            rec["status"] = self.status
        if self.reason:
            rec["reason"] = self.reason
        return rec


@dataclass(frozen=True)
class StraightnessCheck:
    """학습 결과의 직선성 확인 결과."""

    status: str
    learned_rms_px: float | None = None
    reference_rms_px: float | None = None
    uncorrected_rms_px: float | None = None
    limit_px: float | None = None
    chain_count: int | None = None
    reason: str | None = None

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {"status": self.status}
        for key in ("learned_rms_px", "reference_rms_px", "uncorrected_rms_px", "limit_px", "chain_count"):
            value = getattr(self, key)
            if value is not None and (not isinstance(value, float) or math.isfinite(value)):
                rec[key] = value
        if self.reason:
            rec["reason"] = self.reason
        return rec


@dataclass(frozen=True)
class ChainResult:
    """카메라 한 대의 추정과 그 경로."""

    estimate: IntrinsicsEstimate
    backend: str | None  # 값을 낸(또는 실패한) 백엔드. 모두 사용 불가면 None
    attempts: tuple[BackendAttempt, ...]
    verification: StraightnessCheck | None
    order: tuple[str, ...]

    @property
    def fallback(self) -> bool:
        """기본(순서의 첫) 백엔드가 아닌 백엔드가 결과를 냈거나, 아무 백엔드도 쓰지 못했다."""
        return self.backend != (self.order[0] if self.order else None)

    def to_record(self) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "chain_version": CHAIN_VERSION,
            "order": list(self.order),
            "selected": self.backend,
            "method": self.estimate.method,
            "fallback": self.fallback,
            "attempts": [a.to_record() for a in self.attempts],
        }
        if self.verification is not None:
            rec["straightness_check"] = self.verification.to_record()
        return rec


class IntrinsicsBackendChain:
    """백엔드 순서대로 시도하는 추정기. `estimate_with_trace` 가 경로까지 돌려준다.

    backends 는 (이름, 추정기) 순서다. 이름은 `BACKEND_*` 중 하나를 권장하지만 시험용 가짜도 받는다.
    고전 기준 추정기(method PLUMB_LINE_VP)는 순서에 있으면 예비 경로이자 직선성 확인 기준이고, 없으면 확인용으로만
    따로 만든다(verify 가 None 이면 만들지 않는다).
    """

    def __init__(
        self,
        backends: Sequence[tuple[str, IntrinsicsEstimator]],
        *,
        verify: StraightnessCheckConfig | None = DEFAULT_STRAIGHTNESS_CHECK,
        reference: PlumbLineVPEstimator | None = None,
    ) -> None:
        if not backends:
            raise ValueError("백엔드가 하나도 없다")
        names = [n for n, _ in backends]
        if len(set(names)) != len(names):
            raise ValueError(f"같은 백엔드가 두 번 있다: {names}")
        self.backends = tuple(backends)
        self.verify = verify
        plumb = [e for _, e in backends if isinstance(e, PlumbLineVPEstimator)]
        self.reference = reference or (plumb[0] if plumb else (PlumbLineVPEstimator() if verify else None))
        self._unavailable: dict[str, str] = {}

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(n for n, _ in self.backends)

    @property
    def unavailable(self) -> dict[str, str]:
        """사용 불가로 판정된 백엔드와 사유(이 실행 동안 유지)."""
        return dict(self._unavailable)

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        return self.estimate_with_trace(image, camera_id=camera_id).estimate

    def estimate_with_trace(self, image: np.ndarray, *, camera_id: str) -> ChainResult:
        arr = np.asarray(image)
        h, w = arr.shape[:2]
        reference_cache: list[IntrinsicsEstimate] = []

        def reference_estimate() -> IntrinsicsEstimate:
            if not reference_cache:
                assert self.reference is not None
                reference_cache.append(self.reference.estimate(arr, camera_id=camera_id))
            return reference_cache[0]

        attempts: list[BackendAttempt] = []
        for name, estimator in self.backends:
            method = getattr(estimator, "method", BACKEND_METHODS.get(name, name))
            if name in self._unavailable:
                attempts.append(BackendAttempt(name, method, ATTEMPT_UNAVAILABLE, self._unavailable[name]))
                continue
            if estimator is self.reference:
                result = reference_estimate()
                attempts.append(BackendAttempt(name, method, ATTEMPT_OK, status=result.status))
                return ChainResult(result, name, tuple(attempts), None, self.order)
            try:
                result = estimator.estimate(arr, camera_id=camera_id)
            except UNAVAILABLE_ERRORS as e:
                reason = f"{type(e).__name__}: {e}"
                self._unavailable[name] = reason
                attempts.append(BackendAttempt(name, method, ATTEMPT_UNAVAILABLE, reason))
                continue
            except Exception as e:  # noqa: BLE001 - 추론 실패는 그 카메라만 비우고 다른 카메라는 계속한다
                reason = f"추론 중 예외 {type(e).__name__}: {e}"
                attempts.append(BackendAttempt(name, method, ATTEMPT_ERROR, reason))
                failed = _failed_estimate(camera_id, (w, h), method, reason, getattr(estimator, "model_ref", None))
                return ChainResult(failed, name, tuple(attempts), None, self.order)
            attempts.append(BackendAttempt(name, method, ATTEMPT_OK, status=result.status))
            check = None
            if self.verify is not None and result.status == STATUS_OK and self.reference is not None:
                check = check_straightness(result, reference_estimate(), self.reference, self.verify)
                if check.status == CHECK_FAILED:
                    result = _blank(result, f"직선성 확인 실패: {check.reason}", check)
            return ChainResult(result, name, tuple(attempts), check, self.order)
        why = "사용할 수 있는 백엔드가 없다: " + "; ".join(f"{a.backend}({a.reason})" for a in attempts)
        return ChainResult(_failed_estimate(camera_id, (w, h), METHOD_NONE, why, None), None, tuple(attempts), None,
                           self.order)


def check_straightness(
    learned: IntrinsicsEstimate,
    reference: IntrinsicsEstimate,
    reference_estimator: PlumbLineVPEstimator,
    cfg: StraightnessCheckConfig,
) -> StraightnessCheck:
    """고전 기준이 곧게 편 직선을 학습 결과로 펴서 잰 직선성으로 학습 결과를 확인한다(모듈 설명).

    reference 는 reference_estimator 가 같은 영상에서 방금 낸 결과여야 한다(`last_diagnostics` 를 쓴다).
    """
    diag = reference_estimator.last_diagnostics
    if reference.radial_pixel is None or diag is None:
        why = reference.uncertainty.notes[-1] if reference.uncertainty.notes else reference.status
        return StraightnessCheck(CHECK_UNVERIFIED, reason=f"고전 기준이 곧은 직선을 찾지 못해 확인할 수 없다({why})")
    chains = [np.asarray(c, dtype=float) for c, keep in zip(diag.chains, diag.kept_chain_mask) if keep]
    ref_rms = reference.uncertainty.straightness_rms_px
    before = reference.uncertainty.straightness_rms_uncorrected_px
    limit = max(cfg.max_rms_px, cfg.reference_factor * (ref_rms or 0.0))
    if not chains:
        return StraightnessCheck(CHECK_UNVERIFIED, reason="고전 기준이 남긴 직선이 없다")
    assert learned.focal_px is not None and learned.distortion_coeffs is not None
    rms = straightness_rms_under(chains, learned.focal_px, learned.distortion_coeffs, learned.principal_point)
    common = {
        "reference_rms_px": ref_rms,
        "uncorrected_rms_px": before,
        "limit_px": limit,
        "chain_count": len(chains),
    }
    if rms is None:
        return StraightnessCheck(
            CHECK_FAILED, reason="학습 모델의 왜곡이 교실 직선 위 점에서 접혀 펼 수 없다", **common
        )
    if rms > limit:
        why = f"학습 결과로 편 직선성 RMS {rms:.2f}px 가 한도 {limit:.2f}px 를 넘는다"
        if ref_rms is not None and before is not None:
            why += f"(고전 기준 {ref_rms:.2f}px, 펴기 전 {before:.2f}px)"
        return StraightnessCheck(CHECK_FAILED, learned_rms_px=rms, reason=why, **common)
    if before is not None and before > cfg.improvement_check_min_px and rms > cfg.max_improvement_ratio * before:
        why = (f"학습 결과가 휜 직선을 덜 폈다(펴기 전 {before:.2f}px → {rms:.2f}px, 비율 {rms / before:.2f} > "
               f"{cfg.max_improvement_ratio})")
        return StraightnessCheck(CHECK_FAILED, learned_rms_px=rms, reason=why, **common)
    return StraightnessCheck(CHECK_PASSED, learned_rms_px=rms, **common)


def straightness_rms_under(
    chains: Sequence[np.ndarray], focal_px: float, coeffs: Sequence[float], principal_point: Sequence[float] | None
) -> float | None:
    """원본 픽셀 사슬들을 (초점, 방사 왜곡, 주점) 모델로 펴서 잰 직선 맞춤 수직 거리 RMS(원본 픽셀).

    고전 기준과 같은 단위로 맞추려고 사슬마다 (원본 길이 / 펴진 길이) 를 곱한다. 펼 수 없는 점이 있으면 None.
    """
    sq: list[np.ndarray] = []
    pp = principal_point if principal_point is not None else (0.0, 0.0)
    for uv in chains:
        xn = undistort_pixels(uv, focal_px, coeffs, pp)
        if not np.all(np.isfinite(xn)):
            return None
        pts = xn * focal_px
        dist, ext_u = _line_fit(pts)
        _, ext_d = _line_fit(np.asarray(uv, dtype=float))
        sq.append((dist * (ext_d / max(ext_u, 1e-12))) ** 2)
    return float(np.sqrt(np.mean(np.concatenate(sq))))


def _line_fit(pts: np.ndarray) -> tuple[np.ndarray, float]:
    """주성분 직선까지의 수직 거리와 직선 방향 길이."""
    c = pts - pts.mean(axis=0)
    _, vecs = np.linalg.eigh(c.T @ c)
    along = c @ vecs[:, 1]
    return c @ vecs[:, 0], float(along.max() - along.min())


def _blank(result: IntrinsicsEstimate, why: str, check: StraightnessCheck) -> IntrinsicsEstimate:
    """학습 결과의 값을 비운 FAILED (방법·모델은 그대로 둔다: 어느 모델이 실패했는지 남긴다)."""
    unc = result.uncertainty
    return replace(
        result,
        focal_px=None,
        distortion_coeffs=None,
        radial_pixel=None,
        status=STATUS_FAILED,
        uncertainty=IntrinsicsUncertainty(
            focal_std_px=None,
            straightness_rms_px=check.learned_rms_px,
            straightness_rms_uncorrected_px=check.uncorrected_rms_px,
            line_count=check.chain_count,
            notes=(*unc.notes, why),
        ),
    )


def _failed_estimate(
    camera_id: str, image_size: tuple[int, int], method: str, why: str, model: Any
) -> IntrinsicsEstimate:
    return IntrinsicsEstimate(
        camera_id=camera_id,
        image_size=(int(image_size[0]), int(image_size[1])),
        focal_px=None,
        distortion_coeffs=None,
        radial_pixel=None,
        uncertainty=IntrinsicsUncertainty(notes=(why,)),
        method=method,
        method_version=CHAIN_VERSION,
        status=STATUS_FAILED,
        model=model,
    )


class MissingWeightsEstimator:
    """가중치(경로·sha256)를 받지 못한 학습 백엔드 자리. 쓰면 사용 불가(`LearnedBackendUnavailable`)다."""

    def __init__(self, method: str, hint: str) -> None:
        self.method = method
        self.hint = hint

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        raise LearnedBackendUnavailable(f"{self.method} 가중치(경로·sha256)가 주어지지 않았다. {self.hint}")


def build_default_chain(
    order: Sequence[str] = DEFAULT_BACKEND_ORDER,
    *,
    anycalib_weights: WeightsRef | None = None,
    geocalib_weights: WeightsRef | None = None,
    device: str = "cpu",
    plumbline_config: PlumbLineConfig | None = None,
    verify: StraightnessCheckConfig | None = DEFAULT_STRAIGHTNESS_CHECK,
) -> IntrinsicsBackendChain:
    """이름 순서(`BACKEND_*`) → 백엔드 순서. 기본은 AnyCalib(anycalib_dist, radial:2) → GeoCalib(distorted, radial) → 고전 기준.

    가중치를 주지 않은 학습 백엔드는 사용 불가로 기록되고 다음 백엔드로 넘어간다(조용히 건너뛰지 않는다).
    """
    backends: list[tuple[str, IntrinsicsEstimator]] = []
    for name in order:
        if name == BACKEND_ANYCALIB:
            est: IntrinsicsEstimator = (
                AnyCalibEstimator(anycalib_weights, model_id="anycalib_dist", cam_id="radial:2", device=device)
                if anycalib_weights is not None
                else MissingWeightsEstimator(METHOD_ANYCALIB, ANYCALIB_INSTALL_HINT)
            )
        elif name == BACKEND_GEOCALIB:
            est = (
                GeoCalibEstimator(geocalib_weights, variant="distorted", camera_model="radial", device=device)
                if geocalib_weights is not None
                else MissingWeightsEstimator(METHOD_GEOCALIB, GEOCALIB_INSTALL_HINT)
            )
        elif name == BACKEND_PLUMBLINE:
            est = PlumbLineVPEstimator(plumbline_config)
        else:
            raise ValueError(f"백엔드 이름은 {sorted(BACKEND_METHODS)} 중 하나다: {name!r}")
        backends.append((name, est))
    return IntrinsicsBackendChain(backends, verify=verify)
