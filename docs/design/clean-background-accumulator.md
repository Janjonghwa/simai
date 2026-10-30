# 관제 중 카메라별 사람 지운 배경 누적기 설계 (S15P31A101-358)

관련 명세: 기획서 §3.2(GPU 우선순위·관제 중 배치 분석 보류), §4.3 1단계(MapAnything 초기 추정·입력 준비), §4.5(DRIFTED 재보정), §11.5(사람이 찍힌 영상 비커밋).
코드: `ai/src/simai_ai/calibration/world_frame/clean_background.py`.
시험: `ai/tests/calibration/world_frame/test_clean_background.py`.

## 배경과 의도

MapAnything 으로 사람 없는 정적 3D 배경과 초기 카메라 자세를 얻으려면 카메라마다 사람이 지워진 정적 배경 사진이 필요하다. 그러나 관제 시간에는 사람이 계속 돌아다니고, 명세 §3.2 에 따라 관제 중에는 MapAnything 같은 무거운 장시간 GPU 작업을 돌리지 않는다.

따라서 관제 중에는 Tier 0 가 이미 매 프레임마다 내는 **원본 해상도 프레임(BGR)** 과 **사람 검출 박스(`Observation2DFrame` / `CameraDetections` / `PoseDetection` / `bbox`)** 만 받아 CPU 메모리에서 카메라별로 표본을 모으고(검출기를 다시 부르지 않음), 모든 카메라가 충분히 채워졌을 때 관제가 빌 때 한 번 내보내어 `calibration.world_frame.prepare.prepare_inputs` 로 넘긴다.

## 동작과 수치 근거

| 항목 | 기본값 | 근거 |
|---|---|---|
| 표본 간격 `sample_interval_ms` | 3,000 ms (3초) | 내부 파라미터 시간 중앙값 수집(`calibration.intrinsics.capture`, 3초)과 같은 간격. 걷는 사람이 한 자리를 지나가는 시간(1~2초)보다 길어 연속 표본에서 같은 자리가 반복 가려지지 않게 한다 |
| 최소 표본 수 `min_samples_per_camera` | 20장 | 3초 간격 기준 최소 1분 분량. 10/29 로비 녹화 검증에서 카메라당 20장만으로도 4대 모두 `never_revealed_ratio = 0.0` 및 기준본(60장)과의 픽셀 절대 차이 중앙값 1.0(평균 4.74~5.06 / 255)을 달성함 |
| 최대 표본 수 `max_samples_per_camera` | 60장 | Lead 오프라인 실험(`.agy/clean_bg.py`)의 기준 표본 수(60장, 3초 간격 시 3분 창). 오래된 조명 변화를 자연스럽게 밀어낸다 |
| 전체 메모리 상한 `max_bytes` | 2,200 MiB | 원본 2304×1296 uint8 BGR(8.96 MB) + bool 마스크(2.99 MB) = 표본당 약 11.94 MB. 4대 × 45~48장(또는 20장 × 4대 = 955 MB)을 원본 해상도 그대로 수용하며, 상한을 넘으면 표본이 가장 많은 카메라의 가장 오래된 표본부터 버린다 |
| 사람 박스 확장 `pad_x_ratio`, `pad_y_ratio` | 좌우 15%, 상하 10% | `.agy/clean_bg.py` 실측 값(`0.15 * w`, `0.10 * h`). 검출 박스에서 삐져나오는 팔·발끝·그림자·압축 번짐을 함께 가린다 |
| "충분히 찼다" 못 채운 비율 상한 `max_unrevealed_ratio` | `< 0.02` (2%) | 모든 표본에서 내내 가려진 픽셀 비율(`never_revealed_ratio`)이 2% 미만이고 `sample_count >= min_samples_per_camera` 일 때 `ready = True` 로 판정한다. 한 사람이 계속 서 있는 면적(10/29 녹화 평균 사람 가림 4~9%)이 남아 있으면 미달(`ready = False`)로 막고, 작은 경계 잔여만 허용한다 |
| 중앙값 합성 `rows_per_chunk` | 64행 묶음 | 전체 영상을 한 번에 float32(`60 × 1296 × 2304 × 3 × 4B ≈ 2.15 GB`)로 펼치지 않고 64행씩 잘라 `np.nanmedian` 을 계산해 합성 시 추가 메모리 피크를 약 110 MB 이하로 억제한다 |

## 주요 인터페이스 (`clean_background.py`)

- `CleanBackgroundAccumulator.add_frame(camera_id, capture_time_ms, frame_bgr, person_boxes)`:
  - 프레임 해상도·dtype(`uint8` BGR)과 촬영 시각 단조 증가·표본 간격(`sample_interval_ms`)을 확인한다.
  - 사람 박스를 `pad_x_ratio`·`pad_y_ratio` 로 넓혀 드러난 배경 마스크(`reveal_mask`)를 만든다.
  - 카메라별 `max_samples_per_camera` 와 전체 `max_bytes` 를 넘으면 오래된 표본부터 비운다.
- `CleanBackgroundAccumulator.reset_camera(camera_id)`:
  - 특정 카메라가 `DRIFTED` 판정을 받았을 때 그 카메라의 누적 표본과 해상도 기록만 비운다. 다른 카메라 누적은 유지한다.
- `CleanBackgroundAccumulator.camera_status(camera_id)` / `status()` / `is_ready()`:
  - 카메라별 표본 수(`sample_count`), 시각 범위(`time_range_ms`), 못 채운 비율(`never_revealed_ratio`), 사람 가림 평균 비율(`mean_person_cover_ratio`), 준비 여부(`ready`)를 보고한다.
- `CleanBackgroundAccumulator.export(out_dir, *, calibration_version, ...)`:
  - 새 폴더(덮어쓰지 않음)에 `<camera_id>.jpg` 와 `clean_backgrounds.json`(형식 `simai.clean_backgrounds` 0.1.0: 해상도, 표본 수, 시각 범위, 못 채운 비율, 사람 가림 평균 비율, 만들 때 쓴 `calibration_version`)을 쓴다.
  - 한 번도 드러나지 않은 픽셀(`never_revealed`)은 전체 표본의 단순 중앙값으로 채워 빈 구멍(NaN)을 남기지 않고 비율만 메타에 기록한다.
- `CleanBackgroundAccumulator.export_prepared(backgrounds_dir, prepared_dir, intrinsics_record, ...)`:
  - `export` 결과의 `images: dict[str, Path]` 를 그대로 `calibration.world_frame.prepare.prepare_inputs` 에 넘겨 왜곡을 편 영상·펴진 K·사람 마스크·`prepared.json`(`simai.mapanything_inputs`)을 한 번에 만든다.

## 10/29 녹화 확인 결과

`rec-20261029-015512z-lobby-railing-pose` 녹화(4대, 2304×1296)에서 CPU 실행 시간을 고려해 카메라당 고르게 20장씩 뽑아 YOLO 사람 검출 박스와 함께 `CleanBackgroundAccumulator` 에 흘리고, `.agy/clean_bg.py`(60장, 절반 해상도 중앙값 후 2배 보간) 결과물(`C:/Users/SSAFY/simai-local/clean-bg-1029/`)과 비교했다(결과 사본은 `.agy/verify-1029/`, 커밋 안 함).

| 카메라 | 표본 수 | `never_revealed_ratio` (기준 60장) | `mean_person_cover_ratio` (기준 60장) | 기준 배경과 픽셀 차이 중앙값 / 평균 (0~255) | `ready` |
|---|---:|---:|---:|---:|---|
| `cam_A` | 20 | 0.0000 (0.0) | 0.0416 (0.0472) | 1.0 / 4.97 | True |
| `cam_B` | 20 | 0.0000 (0.0) | 0.0692 (0.0718) | 1.0 / 5.06 | True |
| `cam_C` | 20 | 0.0000 (0.0) | 0.0926 (0.0841) | 1.0 / 5.04 | True |
| `cam_D` | 20 | 0.0000 (0.0) | 0.0719 (0.0649) | 1.0 / 4.74 | True |

4대 모두 사람이 깨끗이 지워졌고, 원본 해상도에서 바로 중앙값을 내므로 `.agy/clean_bg.py` 의 절반 축소·확대 보간 차이(평균 약 5 / 255, 중앙값 1 / 255) 외에는 동일한 정적 배경을 얻었다.

## 버린 대안

- **절반 해상도로 줄여 저장한 뒤 내보낼 때 2배 확대(`.agy/clean_bg.py` 방식)**: 메모리는 1/4 로 줄지만 MapAnything 입력 준비(`prepare_inputs`)가 왜곡을 펴고 바닥·벽·실측 정물 경계를 볼 때 원본 고주파 성분이 뭉개진다. 행 묶음(`rows_per_chunk=64`) 계산과 `max_bytes` 상한을 두면 원본 해상도(uint8) 그대로 들고 있어도 메모리가 통제된다.
- **온라인 이동 평균(EMA) 배경**: 메모리 한 장만 쓰지만 사람 박스 바깥의 그림자·검출 누락 한두 프레임이 배경에 잔상으로 섞인다. 시간 중앙값은 절반 미만의 검출 누락·그림자에 강건하다.

## 잔여 위험과 넘긴 일

- 내내 한자리에 앉아 있거나 서 있는 사람이 카메라 화각의 2% 이상을 차지하면 `never_revealed_ratio >= 0.02` 가 되어 `is_ready()` 가 계속 `False` 로 남는다(의도된 보류). 사람이 자리를 비우거나 `require_ready=False` 강제 내보내기 + `prepare_inputs` 의 세그멘테이션 마스크로 남아 있는 사람 점을 빼는 운영 연결은 호출자(서비스 조립 단계) 몫이다.
- `services/tier0/` 런타임 루프에 누적기를 직접 연결하는 작업은 현행 병렬 티켓(-352·-356)과의 충돌을 피하기 위해 이번 범위에서 제외했다.
