"""걷는 사람 keypoint 번들 조정 (명세 4.3 3단계 정밀화, 4단계 검산 일부, 요구사항 FR-23).

- `inputs`: 입력(공통 시각으로 맞춘 사람별 카메라 keypoint, 실측 정물 자리)과 설정
- `skeleton`: 관절 배치(COCO17 + 선택 발 끝점)와 뼈 길이 변수
- `solver`: Levenberg–Marquardt 풀이(Schur 보수, 해석적 희소 야코비안, 강건 손실, 게이지 고정)
- `checks`: 카메라별 바닥 평면 차이, 바닥 커버리지, CCVS 방식 카메라 높이 검산, 퇴화 판단 재료
- `adjust`: 진입점 `adjust_human_bundle` 과 결과 `HumanBundleResult`(계약 camera_calibration·판정 근거·기록)

판정(CALIBRATED/PROVISIONAL)은 하지 않는다. 결과를 `CalibrationVersionStore.propose` 에 넣으면 criteria 가 정한다.
SIM·정답 입력의 지표는 상한이다(input_provenance.is_upper_bound). scipy 가 필요하다(extras calibration).
"""

from simai_ai.calibration.bundle.adjust import (
    PROVIDER_VERSION,
    STATUS_CONVERGED,
    STATUS_FAILED,
    STATUS_UNCERTAIN,
    BundleNotUsable,
    HumanBundleResult,
    adjust_human_bundle,
)
from simai_ai.calibration.bundle.checks import CameraHeightCheck
from simai_ai.calibration.bundle.inputs import BundleConfig, MeasuredObjectObservation, PersonObservation

__all__ = [
    "PROVIDER_VERSION",
    "STATUS_CONVERGED",
    "STATUS_FAILED",
    "STATUS_UNCERTAIN",
    "BundleConfig",
    "BundleNotUsable",
    "CameraHeightCheck",
    "HumanBundleResult",
    "MeasuredObjectObservation",
    "PersonObservation",
    "adjust_human_bundle",
]
