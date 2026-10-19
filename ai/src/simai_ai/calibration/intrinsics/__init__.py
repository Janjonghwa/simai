"""무마커 내부 파라미터 추정 (명세 4.3 1단계, 요구사항 FR-23).

교실의 직선(벽 모서리·문틀·책상 가장자리·천장선)으로 카메라 한 대의 초점·렌즈 왜곡 초기값을 단일 영상에서
추정하고, 같은 렌즈 4대의 추정을 합쳐 공통 초기값을 만든다. 마커·체커보드(ChArUco)는 쓰지 않는다.
최종 카메라별 값은 번들 조정(-104)이 걷기 keypoint 와 실측 정물 제약으로 다듬는다.

- `estimate`: 추정기 인터페이스(`IntrinsicsEstimator`), 결과(`IntrinsicsEstimate`), 가중치 참조(`WeightsRef`),
  카메라 모델 계산과 같은 광선 재투영 차이(`model_discrepancy_px`)
- `lines`: 에지 사슬(휘어 있는 직선 후보) 검출, numpy 만 사용
- `plumbline`: 고전적 기준 백엔드(plumb-line 왜곡 + 직교 소실점 초점). 모델 없이 시험할 수 있다
- `learned`: GeoCalib·AnyCalib 백엔드(선택 의존성, 없으면 쓸 때 `LearnedBackendUnavailable`)
- `shared`: 같은 렌즈 4대 공유(강건 합치기, 카메라별 편차, 이상치 표시)
- `record`: 추정 기록(input_provenance 포함)과 번들 조정 초기값 넘김
- `chain`: 백엔드 순서. 기본 AnyCalib → 예비 GeoCalib → 가중치 없이 도는 고전 기준, 학습 결과의 직선성 확인
- `capture`: 카메라 RTSP·영상 파일에서 프레임을 메모리로만 받아 시간 중앙값 배경(디스크에 쓰지 않음)
- `runner`·`cli`: 여러 카메라 수집 → 추정 → 같은 렌즈 공유 → 기록 JSON (`python -m simai_ai.calibration.intrinsics.cli`)

이 단계 결과는 보정 버전이 아니며 위험 판정에 직접 쓰이지 않는다. 결과는 SIM 영상이면 상한(is_upper_bound)이다.
"""

from simai_ai.calibration.intrinsics.capture import CameraInput, CaptureConfig, RtspCredentials, capture_background
from simai_ai.calibration.intrinsics.chain import (
    DEFAULT_BACKEND_ORDER,
    METHOD_NONE,
    ChainResult,
    IntrinsicsBackendChain,
    StraightnessCheckConfig,
    build_default_chain,
)
from simai_ai.calibration.intrinsics.estimate import (
    METHOD_ANYCALIB,
    METHOD_GEOCALIB,
    METHOD_PLUMB_LINE_VP,
    METHOD_SHARED_LENS,
    STATUS_FAILED,
    STATUS_NO_CONSENSUS,
    STATUS_NO_FOCAL,
    STATUS_OK,
    IntrinsicsEstimate,
    IntrinsicsEstimationError,
    IntrinsicsEstimator,
    IntrinsicsUncertainty,
    ModelDiscrepancy,
    ModelRef,
    WeightsMismatchError,
    WeightsRef,
    model_discrepancy_px,
)
from simai_ai.calibration.intrinsics.learned import AnyCalibEstimator, GeoCalibEstimator, LearnedBackendUnavailable
from simai_ai.calibration.intrinsics.lines import EdgeChainConfig, detect_edge_chains
from simai_ai.calibration.intrinsics.plumbline import PlumbLineConfig, PlumbLineVPEstimator
from simai_ai.calibration.intrinsics.record import (
    BundleAdjustmentInit,
    CameraIntrinsicsInit,
    build_intrinsics_record,
    to_bundle_adjustment_init,
)
from simai_ai.calibration.intrinsics.runner import IntrinsicsRun, run_intrinsics
from simai_ai.calibration.intrinsics.shared import (
    CameraDeviation,
    SharedLensConfig,
    SharedLensResult,
    combine_shared_lens,
)

__all__ = [
    "DEFAULT_BACKEND_ORDER",
    "METHOD_ANYCALIB",
    "METHOD_GEOCALIB",
    "METHOD_NONE",
    "METHOD_PLUMB_LINE_VP",
    "METHOD_SHARED_LENS",
    "STATUS_FAILED",
    "STATUS_NO_CONSENSUS",
    "STATUS_NO_FOCAL",
    "STATUS_OK",
    "AnyCalibEstimator",
    "BundleAdjustmentInit",
    "CameraDeviation",
    "CameraInput",
    "CameraIntrinsicsInit",
    "CaptureConfig",
    "ChainResult",
    "EdgeChainConfig",
    "GeoCalibEstimator",
    "IntrinsicsBackendChain",
    "IntrinsicsEstimate",
    "IntrinsicsEstimationError",
    "IntrinsicsEstimator",
    "IntrinsicsRun",
    "IntrinsicsUncertainty",
    "LearnedBackendUnavailable",
    "ModelDiscrepancy",
    "ModelRef",
    "PlumbLineConfig",
    "PlumbLineVPEstimator",
    "RtspCredentials",
    "SharedLensConfig",
    "SharedLensResult",
    "StraightnessCheckConfig",
    "WeightsMismatchError",
    "WeightsRef",
    "build_default_chain",
    "build_intrinsics_record",
    "capture_background",
    "combine_shared_lens",
    "detect_edge_chains",
    "model_discrepancy_px",
    "run_intrinsics",
    "to_bundle_adjustment_init",
]
