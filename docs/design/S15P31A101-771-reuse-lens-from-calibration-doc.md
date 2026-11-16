# S15P31A101-771 재사용·기준 시점 기준의 렌즈를 보정 원문에서 읽기

## 문제

「이전 맵 재사용」 시점 확인 기준(-712·-714)과 기준 시점 맞추기(-765)는 `CoverageReuseReference.forMap` 이 만든다. 이 함수는 맵의
카메라별 사진 서명 주소와 그 보정의 렌즈(K·왜곡·image_size)를 묶는데, 렌즈를 `calibration_camera` 표에서만 읽었다.
서버에는 이 표에 값을 넣는 경로가 없다(보정 등록 `CalibrationService`·`CalibrationUpdateIngestor` 는 `calibration_version`·
`calibration_document` 만 쓴다). 운영 DB 최근 보정 10개 모두 0행이라 사진이 정상인데도 기준이 null 이 되고, 완료 맵이 전부
NO_PHOTOS 로 막혔으며 헬퍼는 비교 없이 coverage_guide 1.0.0 만 보냈다.

## 바꾼 것

- `CoverageReuseReference.documentLenses`: 그 보정의 원문 `calibration_document.content` 의 `cameras[]` 에서
  `image_size`·`K`·`distortion{model, coeffs}` 를 읽는다. 보정 계약(`contracts/providers/calibration.schema.json`)의 카메라 필수 칸이다.
  같은 원문에서 뷰어 `map_cameras` 도 만든다(`SceneService.setupCalibrationMapCameras`, -593).
- 원문 값이 표 값보다 앞선다. 원문에 없는 카메라만 표 값을 쓴다(데이터셋·시험처럼 표만 채운 경우).
- 기기 수요 응답 `reuse_references`·`reuse_reference`·`aim_reference` 모양은 그대로다. 헬퍼·화면·계약 변경 없음.
- 저장된 원문을 읽으므로 데이터 이전 없이 기존 완료 맵에도 바로 적용된다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 렌즈 출처 | 보정 원문 우선, 표는 보조 | 원문이 등록 정본이고 표는 비어 있다. 표만 있는 경우를 깨지 않는다 |
| 형식이 틀린 카메라 | 그 카메라만 뺀다(로그 경고) | 지어내지 않는다(규칙 2). 헬퍼는 렌즈가 있는 카메라만 비교한다(-712 와 같음) |
| 검사 | image_size 양의 정수 2개, K 3×3 유한·fx·fy>0·마지막 행 [0,0,1], 왜곡 모델 enum, coeffs 유한 수 | 계약 칸, `SceneService` map_cameras 검사, 헬퍼 `view_check.parse_lens` 와 맞춘다 |
| 표 읽기 실패 | 경고만 남기고 원문 렌즈로 계속 | 원문이 정본이다. 표 불량 행이 원문 기준까지 막지 않게 한다 |

## 버린 대안

- 보정 등록 때 `calibration_camera` 도 채우고 기존 보정은 마이그레이션으로 채우기: 쓰는 경로 세 곳과 데이터 이전이 필요하고,
  원문과 표가 어긋날 수 있다. 시연 전 위험이 더 크다.

## 시험

`CoverageReuseReferenceIntegrationTest.렌즈_표가_비어도_보정_원문의_렌즈로_기준을_만들고_형식이_틀린_카메라는_뺀다`
(표 비움 + 원문 없음 → null, 원문 cam_A·cam_B 정상·cam_C fx=0 → 두 대만, R·t 는 싣지 않음). 기존 표 기반 시험은 그대로 통과한다.

## 남은 일

- `calibration_camera` 표는 여전히 아무도 채우지 않는다. 표를 쓰는 다른 곳이 생기면 원문에서 읽거나 등록 때 채우는 일을 따로 정한다.
