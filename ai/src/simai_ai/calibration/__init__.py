"""자동 보정, PnP, CalibrationProvider.

- `state`: 보정 상태(UNCALIBRATED/PROVISIONAL/CALIBRATED/DRIFTED)의 운영 의미와 허용 전이 (명세 4.4)
- `criteria`: CALIBRATED 판정과 스케일 근거 판정 (명세 4.4, 4.3, 20.4 규칙 4. 수정 6 기준)
- `document`: 판정 결과 → CalibrationProvider 계약 문서 (G1_AUTO·대체 구현 공통)
- `drift`: 운영 중 틀어짐(DRIFTED) 근거 판정(스트림 해상도·화각 변경, 재투영 오차 값 비교)
- `monitor`: 지나가는 사람 keypoint 로 카메라별 한 카메라 제외 재투영 오차를 계속 재고 기준선 대비 증가가 지속되면
  틀어짐으로 확정(명세 4.5, 6.5, D-G1-03). 사람이 없으면 감시 근거 없음으로 보고한다
- `watch`: 감시 → 버전 저장소 DRIFTED 표시 → 자동 재보정 요청, calibration_update payload
- `recalibration`: DRIFTED 뒤 관측을 모아 -104 번들 조정을 부르는 트리거(판정은 저장소 criteria)
- `live_candidate`: 운영 중 모은 Tier 0 2D 관절·로컬 트랙 고리 버퍼(`LiveJointBuffer`)와 백그라운드 후보 보정 실행기
  (`LiveCandidateCalibrationRunner`, 명세 3.2·3.12·4.5, -345)
- `coverage`: 커버리지 기반 설치 안내("카메라가 너무 아래를 봄", 사각·약한 바닥)와 걷기 경로 안내
- `structure`: 보정 문서의 계약 형식 검사(계약 스키마 직접 검증). 버전 저장소가 저장 전에 쓴다
- `versions`: 보정 버전 저장소(추가만, 원자적 전환)와 calibration_version 태깅, 판정 근거 감사 기록,
  VersionedCalibrationProvider
- `intrinsics`(하위 패키지): 교실 직선으로 무마커 내부 파라미터 초기값 추정, 같은 렌즈 4대 공유, 번들 조정 초기값
  넘김 (명세 4.3 1단계). 여기서 다시 내보내지 않는다(`simai_ai.calibration.intrinsics` 로 import)
- `bundle`(하위 패키지): 걷는 사람 keypoint 번들 조정(명세 4.3 3단계, HumanCalib 원리의 자체 구현)과 CCVS 방식
  카메라 높이 검산. 결과(카메라별 K·왜곡·R·t·재투영 오차와 바닥·스케일 근거)를 `CameraCalibration`·
  `CalibrationEvidence` 로 넘기면 판정·버전 전환·계약 출력이 된다. 여기서 다시 내보내지 않는다
  (`simai_ai.calibration.bundle` 로 import, scipy 필요)

`monitor`·`watch`·`recalibration`·`live_candidate`·`coverage` 는 여기서 다시 내보내지 않는다(numpy·scipy 가 필요해 서브모듈로 import).

- `world_frame`(하위 패키지): MapAnything 입력 준비(왜곡 펴기·펴진 K·사람 마스크)와 결과의 바닥·벽으로 World 좌표계 고정,
  PROVISIONAL 보정 문서와 외부 파라미터 도구의 World 정렬 입력 (명세 4.3 1·2단계, -102). 여기서 다시 내보내지 않는다

실측 정물 끝점 검출(-100)은 아직 없다.

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
