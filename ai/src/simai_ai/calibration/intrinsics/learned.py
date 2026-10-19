"""학습 모델 백엔드: GeoCalib(ECCV 2024), AnyCalib(ICCV 2025) 단일 영상 내부 파라미터 추정.

AnyCalib(`anycalib_dist`, `radial:2`)이 기본 경로, GeoCalib(`distorted`, `radial`)이 예비다. 순서와 대체 규칙은
`chain.IntrinsicsBackendChain` 이 정한다(사용 불가 → 다음 백엔드, 결과가 FAILED 면 대체하지 않고 비움).

선택 의존성이다. torch 와 각 패키지(geocalib, anycalib)가 없어도 이 모듈은 import 되고, 추정기를 처음 쓸 때
설치 방법을 적은 `LearnedBackendUnavailable`(ImportError) 를 낸다. 두 패키지는 PyPI 배포가 없어 git 으로 설치한다
(pyproject 에 넣지 않았다. 후보 비교는 docs/design/markerless-intrinsics.md).

    uv pip install torch "geocalib @ git+https://github.com/cvg/GeoCalib"
    uv pip install torch "anycalib @ git+https://github.com/javrtg/AnyCalib"

- **가중치는 커밋하지 않는다.** `WeightsRef`(로컬 경로 + sha256)로만 받고, 쓰기 전에 해시를 확인한다. 코드가
  가중치를 내려받지 않는다(패키지 기본 동작인 torch.hub 내려받기를 쓰지 않는다). 원본 위치는 설계 문서의 표.
- 라이선스: GeoCalib 코드 Apache-2.0·가중치 CC-BY-4.0(출처 표시), AnyCalib 코드·가중치 Apache-2.0.
- 출력은 OpenCV 방사 모델(k1, k2)로 바꾼다. GeoCalib `radial`, AnyCalib `radial:2`·`simple_radial:2` 는
  x_d = x_u (1 + k1 r² + k2 r⁴) 로 OpenCV 와 같은 식이다. 어안·분할(division) 모델은 OPENCV_PINHOLE 로 정확히
  바뀌지 않으므로 받지 않는다.
- fx ≠ fy 이면 평균을 초점으로 쓰고 차이를 메모에 남긴다(이 패키지의 결과는 정사각 화소 하나의 초점).
- GeoCalib 은 주점을 영상 중심으로 고정한다(principal_point_assumed). AnyCalib 은 주점도 추정한다.
- CPU 로 돈다(device 기본 "cpu"). GPU 는 쓰지 않는다.
- AnyCalib 의 `success` 가 False 이거나 값이 범위 밖이면 FAILED(값 비움)로 낸다.
- 런타임 내려받기: GeoCalib 은 파일 경로를 주면 `torch.load` 로만 읽는다. AnyCalib 은 `model_id=None` 으로 만들고
  `load_state_dict` 로 읽으며, 내장 DINOv2 백본 코드(`anycalib/model/dinov2.py`)에 hub 내려받기 호출이 없음을 코드로
  확인했다. 오프라인(네트워크 차단, 빈 TORCH_HOME) 실행 확인은 C110 검증 때 한다.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Sequence
from typing import Any

import numpy as np

from simai_ai.calibration.intrinsics.estimate import (
    METHOD_ANYCALIB,
    METHOD_GEOCALIB,
    STATUS_FAILED,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsEstimationError,
    IntrinsicsUncertainty,
    ModelRef,
    WeightsMismatchError,
    WeightsRef,
    radial_pixel_from_opencv,
)

LEARNED_BACKEND_VERSION = "0.1.0"
# 초점 허용 범위: 영상 너비의 배수 (고전 기준 백엔드 `PlumbLineConfig.focal_range_width` 와 같은 값)
LEARNED_FOCAL_RANGE_WIDTH = (0.15, 6.0)

GEOCALIB_INSTALL_HINT = (
    "GeoCalib 백엔드에는 torch 와 geocalib 패키지가 필요하다(선택 의존성, PyPI 없음): "
    'uv pip install torch "geocalib @ git+https://github.com/cvg/GeoCalib". '
    "가중치는 https://github.com/cvg/GeoCalib/releases/download/v1.0/geocalib-distorted.tar 를 Network Volume 에 두고 "
    "WeightsRef(경로, sha256) 로 넘긴다"
)
ANYCALIB_INSTALL_HINT = (
    "AnyCalib 백엔드에는 torch 와 anycalib 패키지가 필요하다(선택 의존성, PyPI 없음): "
    'uv pip install torch "anycalib @ git+https://github.com/javrtg/AnyCalib". '
    "가중치는 https://github.com/javrtg/AnyCalib/releases/download/v1.0.0/anycalib_dist.pt 를 Network Volume 에 두고 "
    "WeightsRef(경로, sha256) 로 넘긴다"
)

GEOCALIB_CAMERA_MODELS = ("pinhole", "simple_radial", "radial")
ANYCALIB_CAMERA_IDS = ("pinhole", "simple_pinhole", "radial:1", "radial:2", "simple_radial:1", "simple_radial:2")


class LearnedBackendUnavailable(ImportError):
    """학습 모델 백엔드의 선택 의존성이 설치돼 있지 않다."""


def _verified_weights(weights: WeightsRef) -> Any:
    """가중치 파일 확인. 파일 없음·해시 불일치는 사용 불가(`LearnedBackendUnavailable`)로 바꾼다.

    사용 불가로 보는 경계는 이 로딩 단계뿐이다. 모델을 읽은 뒤 추론에서 올라오는 FileNotFoundError 등은 추론 실패다
    (`chain` 이 그 카메라를 FAILED 로 비우고 다른 방법으로 대체하지 않는다).
    """
    try:
        return weights.verify()
    except (FileNotFoundError, WeightsMismatchError) as e:
        raise LearnedBackendUnavailable(f"가중치를 쓸 수 없다({type(e).__name__}: {e})") from e


def _require(module: str, hint: str) -> Any:
    try:
        return importlib.import_module(module)
    except ImportError as e:
        raise LearnedBackendUnavailable(f"{module} 를 import 할 수 없다. {hint}") from e


def learned_estimate(
    *,
    camera_id: str,
    image_size: tuple[int, int],
    fx: float,
    fy: float,
    cx: float,
    cy: float,
    radial: Sequence[float],
    focal_std_px: float | None,
    method: str,
    model: ModelRef,
    principal_point_assumed: bool,
    notes: Sequence[str] = (),
    success: bool = True,
    focal_range_width: tuple[float, float] = LEARNED_FOCAL_RANGE_WIDTH,
) -> IntrinsicsEstimate:
    """학습 모델 출력(원본 해상도 픽셀) → 추정 결과. torch 없이 시험할 수 있게 분리했다.

    모델이 실패를 알렸거나(success False), 값이 NaN·무한대·음수이거나, 초점이 허용 범위(영상 너비의 배수) 밖이면
    값을 비운 FAILED 로 낸다(지어내지 않는다).
    """
    values = [fx, fy, cx, cy, *radial]
    why = None
    if not success:
        why = "모델이 보정 실패를 알렸다(success False)"
    elif not all(math.isfinite(float(v)) for v in values):
        why = f"모델 출력에 NaN·무한대가 있다: {values}"
    elif fx <= 0 or fy <= 0:
        why = f"초점이 양수가 아니다: fx={fx}, fy={fy}"
    else:
        f_mean = 0.5 * (float(fx) + float(fy))
        lo, hi = (focal_range_width[0] * image_size[0], focal_range_width[1] * image_size[0])
        if not (lo <= f_mean <= hi):
            why = f"초점 {f_mean:.1f}px 가 허용 범위 [{lo:.0f}, {hi:.0f}]px 밖이다"
    if why is not None:
        return IntrinsicsEstimate(
            camera_id=camera_id,
            image_size=(int(image_size[0]), int(image_size[1])),
            focal_px=None,
            distortion_coeffs=None,
            radial_pixel=None,
            uncertainty=IntrinsicsUncertainty(notes=(*notes, why)),
            method=method,
            method_version=LEARNED_BACKEND_VERSION,
            status=STATUS_FAILED,
            model=model,
            principal_point_assumed=principal_point_assumed,
        )
    k = [float(v) for v in radial][:2] + [0.0] * max(0, 2 - len(radial))
    focal = 0.5 * (float(fx) + float(fy))
    all_notes = list(notes)
    if abs(fx - fy) / focal > 0.01:
        all_notes.append(f"fx={fx:.1f}, fy={fy:.1f} 차이가 1% 를 넘어 평균을 썼다")
    coeffs = (k[0], k[1], 0.0, 0.0, 0.0)
    return IntrinsicsEstimate(
        camera_id=camera_id,
        image_size=(int(image_size[0]), int(image_size[1])),
        focal_px=focal,
        distortion_coeffs=coeffs,
        radial_pixel=radial_pixel_from_opencv(coeffs, focal, image_size),
        uncertainty=IntrinsicsUncertainty(focal_std_px=focal_std_px, notes=tuple(all_notes)),
        method=method,
        method_version=LEARNED_BACKEND_VERSION,
        status=STATUS_OK,
        model=model,
        principal_point=(float(cx), float(cy)),
        principal_point_assumed=principal_point_assumed,
    )


def _image_tensor(torch: Any, image: np.ndarray, channel_order: str, device: str) -> Any:
    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.repeat(arr[:, :, None], 3, axis=2)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise IntrinsicsEstimationError(f"영상은 (H, W) 또는 (H, W, 3) 이어야 한다: {arr.shape}")
    arr = arr[:, :, :3]
    if channel_order.upper() == "BGR":
        arr = arr[:, :, ::-1]
    arr = arr.astype(np.float32) / 255.0 if arr.dtype == np.uint8 else arr.astype(np.float32)
    return torch.from_numpy(np.ascontiguousarray(arr.transpose(2, 0, 1))).to(device)


class GeoCalibEstimator:
    """GeoCalib 단일 영상 추정기 (`IntrinsicsEstimator`). 가중치 variant 기본은 distorted."""

    method = METHOD_GEOCALIB

    def __init__(
        self,
        weights: WeightsRef,
        *,
        variant: str = "distorted",
        camera_model: str = "radial",
        device: str = "cpu",
        channel_order: str = "BGR",
        package_version: str = "1.0",
    ) -> None:
        if camera_model not in GEOCALIB_CAMERA_MODELS:
            raise ValueError(
                f"GeoCalib camera_model 은 {GEOCALIB_CAMERA_MODELS} 중 하나(OpenCV 로 바뀌는 것만): {camera_model}"
            )
        self.weights = weights
        self.variant = variant
        self.camera_model = camera_model
        self.device = device
        self.channel_order = channel_order
        self.model_ref = ModelRef("GeoCalib", package_version, variant, weights, "Apache-2.0", "CC-BY-4.0")
        self._model: Any = None
        self._torch: Any = None

    def _load(self) -> None:
        if self._model is not None:
            return
        torch = _require("torch", GEOCALIB_INSTALL_HINT)
        geocalib = _require("geocalib", GEOCALIB_INSTALL_HINT)
        path = _verified_weights(self.weights)
        self._model = geocalib.GeoCalib(weights=str(path)).to(self.device)
        self._torch = torch

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        self._load()
        h, w = np.asarray(image).shape[:2]
        img = _image_tensor(self._torch, image, self.channel_order, self.device)
        with self._torch.no_grad():
            result = self._model.calibrate(img, camera_model=self.camera_model)
        cam = result["camera"]
        fx, fy = (float(v) for v in cam.f.reshape(-1)[:2])
        cx, cy = (float(v) for v in cam.c.reshape(-1)[:2])
        radial: list[float] = []
        if self.camera_model != "pinhole":
            radial = [float(v) for v in cam.dist.reshape(-1)]
        size = [float(v) for v in cam.size.reshape(-1)[:2]]
        if abs(size[0] - w) > 0.5 or abs(size[1] - h) > 0.5:
            raise IntrinsicsEstimationError(f"GeoCalib 출력 카메라 크기 {size} 가 영상 크기 {(w, h)} 와 다르다")
        unc = result.get("focal_uncertainty")
        focal_std = float(unc.reshape(-1)[0]) if unc is not None else None
        return learned_estimate(
            camera_id=camera_id,
            image_size=(w, h),
            fx=fx,
            fy=fy,
            cx=cx,
            cy=cy,
            radial=radial,
            focal_std_px=focal_std,
            method=self.method,
            model=self.model_ref,
            principal_point_assumed=True,
            notes=(f"GeoCalib {self.variant}/{self.camera_model}",),
        )


class AnyCalibEstimator:
    """AnyCalib 단일 영상 추정기 (`IntrinsicsEstimator`). 내부 파라미터 추정의 기본 백엔드다(명세 4.3, `chain`).

    model_id 기본은 anycalib_dist(왜곡 영상 학습), cam_id 기본은 radial:2(fx, fy, cx, cy, k1, k2: OpenCV [k1, k2] 와
    같은 식. 주점도 추정한다).
    """

    method = METHOD_ANYCALIB

    def __init__(
        self,
        weights: WeightsRef,
        *,
        model_id: str = "anycalib_dist",
        cam_id: str = "radial:2",
        device: str = "cpu",
        channel_order: str = "BGR",
        package_version: str = "1.0",
    ) -> None:
        if cam_id not in ANYCALIB_CAMERA_IDS:
            raise ValueError(f"AnyCalib cam_id 는 {ANYCALIB_CAMERA_IDS} 중 하나(OpenCV 로 바뀌는 것만): {cam_id}")
        self.weights = weights
        self.model_id = model_id
        self.cam_id = cam_id
        self.device = device
        self.channel_order = channel_order
        self.model_ref = ModelRef("AnyCalib", package_version, model_id, weights, "Apache-2.0", "Apache-2.0")
        self._model: Any = None
        self._torch: Any = None

    def _load(self) -> None:
        if self._model is not None:
            return
        torch = _require("torch", ANYCALIB_INSTALL_HINT)
        anycalib = _require("anycalib", ANYCALIB_INSTALL_HINT)
        path = _verified_weights(self.weights)
        model = anycalib.AnyCalib(model_id=None)
        model.load_state_dict(torch.load(str(path), map_location="cpu"), strict=True)
        self._model = model.eval().to(self.device)
        self._torch = torch

    def estimate(self, image: np.ndarray, *, camera_id: str) -> IntrinsicsEstimate:
        self._load()
        h, w = np.asarray(image).shape[:2]
        img = _image_tensor(self._torch, image, self.channel_order, self.device)
        with self._torch.no_grad():
            pred = self._model.predict(img, cam_id=self.cam_id)
        params = [float(v) for v in pred["intrinsics"].reshape(-1)]
        fx, fy, cx, cy, radial = parse_anycalib_params(self.cam_id, params)
        success = pred.get("success")
        ok = True if success is None else bool(np.asarray(success.cpu() if hasattr(success, "cpu") else success).all())
        return learned_estimate(
            camera_id=camera_id,
            image_size=(w, h),
            fx=fx,
            fy=fy,
            cx=cx,
            cy=cy,
            radial=radial,
            focal_std_px=None,
            method=self.method,
            model=self.model_ref,
            principal_point_assumed=False,
            notes=(f"AnyCalib {self.model_id}/{self.cam_id}. 초점 불확실성은 아직 옮기지 않았다(intrinsics_icovs)",),
            success=ok,
        )


def parse_anycalib_params(cam_id: str, params: Sequence[float]) -> tuple[float, float, float, float, list[float]]:
    """AnyCalib intrinsics 벡터 → (fx, fy, cx, cy, [k...]). 순서는 AnyCalib cameras/*.py (f 또는 fx, fy, cx, cy, k1, ...)."""
    simple = cam_id.startswith("simple_")
    n_f = 1 if simple else 2
    num_k = int(cam_id.split(":")[1]) if ":" in cam_id else 0
    expected = n_f + 2 + num_k
    if len(params) != expected:
        raise IntrinsicsEstimationError(f"AnyCalib {cam_id} 출력은 {expected}개여야 한다: {len(params)}개")
    fx = params[0]
    fy = params[0] if simple else params[1]
    cx, cy = params[n_f], params[n_f + 1]
    return fx, fy, cx, cy, list(params[n_f + 2 :])
