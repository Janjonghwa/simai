"""자동 보정, PnP, CalibrationProvider.

- `state`: 보정 상태(UNCALIBRATED/PROVISIONAL/CALIBRATED/DRIFTED)의 운영 의미와 허용 전이 (명세 4.4)
- `criteria`: CALIBRATED 판정과 스케일 근거 판정 (명세 4.4, 4.3, 20.4 규칙 4. 수정 6 기준)
- `document`: 판정 결과 → CalibrationProvider 계약 문서 (G1_AUTO·CHARUCO 공통)
- `drift`: 운영 중 틀어짐(DRIFTED) 감지
- `structure`: 보정 문서의 계약 형식 검사(계약 스키마 직접 검증). 버전 저장소가 저장 전에 쓴다
- `versions`: 보정 버전 저장소(추가만, 원자적 전환)와 calibration_version 태깅, 판정 근거 감사 기록,
  VersionedCalibrationProvider
- `intrinsics`(하위 패키지): 교실 직선으로 무마커 내부 파라미터 초기값 추정, 같은 렌즈 4대 공유, 번들 조정 초기값
  넘김 (명세 4.3 1단계). 여기서 다시 내보내지 않는다(`simai_ai.calibration.intrinsics` 로 import)

자동 보정 알고리즘(HumanCalib·번들 조정)은 아직 없다. 그 결과(카메라별 K·왜곡·R·t·재투영 오차와 검산 근거)를
`CameraCalibration`·`CalibrationEvidence` 로 넣으면 판정·버전 전환·계약 출력이 된다.

jsonschema 가 필요하다(extras calibration). 없으면 이 패키지를 import 할 때 설치 방법을 적은 ImportError 가 난다.
"""

from simai_ai.calibration.criteria import (
    CalibrationEvidence,
    CalibrationJudgement,
    Criterion,
    HeightCheckpoint,
    ScaleBasis,
    ScaleEstimate,
    judge_calibration,
)
from simai_ai.calibration.document import CameraCalibration, build_calibration_doc
from simai_ai.calibration.drift import CameraObservation, DriftConfig, DriftReason, detect_drift
from simai_ai.calibration.state import CalibrationOperation, CalibrationState, check_transition, operation_for
from simai_ai.calibration.structure import check_calibration_structure
from simai_ai.calibration.versions import (
    ActiveCalibration,
    CalibrationVersionStore,
    ProposalResult,
    RejectReason,
    VersionedCalibrationProvider,
    VersionSwitchConfig,
)

__all__ = [
    "ActiveCalibration",
    "CalibrationEvidence",
    "CalibrationJudgement",
    "CalibrationOperation",
    "CalibrationState",
    "CalibrationVersionStore",
    "CameraCalibration",
    "CameraObservation",
    "Criterion",
    "DriftConfig",
    "DriftReason",
    "HeightCheckpoint",
    "ProposalResult",
    "RejectReason",
    "ScaleBasis",
    "ScaleEstimate",
    "VersionSwitchConfig",
    "VersionedCalibrationProvider",
    "build_calibration_doc",
    "check_calibration_structure",
    "check_transition",
    "detect_drift",
    "judge_calibration",
    "operation_for",
]
