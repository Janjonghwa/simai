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
- `services/tier0/` 런타임 연결은 아래 S15P31A101-363 절에서 구현했다.
- 완성 묶음의 static_reconstruct 자동 제출(관제가 빈 뒤 업로드·작업 등록)은 S15P31A101-403 이 분석 워커에서 한다(`docs/design/worker-static-reconstruct.md` 의 "S15P31A101-403" 절).

## S15P31A101-363: Tier 0 비동기 수집과 Pod 내보내기

관련 명세는 §3.2(Tier 0 우선·MapAnything 배치 실행 보류), §4.5(DRIFTED 재수집), AGENTS 규칙 1·5·6이다. 계약·health·`ai/uv.lock`은 변경하지 않는다. 배경 사진은 보조 분석 입력이며 World 위치·ID·위험 판정에 주입하지 않는다. MapAnything 실행은 후속 -364의 `static_reconstruct` 작업이다.

### 연결 지점과 실행 경계

`services/tier0/perception.py`의 `DetectorFeed.poll`에서 프레임 식별자와 시각 최신성을 통과한 원본 `Frame`과 **동일 프레임의 `CameraDetections`**를 `observe_decoded`로 넘긴다. 추적기가 확정하지 못한 사람 박스도 포함하며, 사람이 없는 정상 검출 결과(빈 박스)도 수집한다. 검출 실패·프레임 불일치·중복·역순 결과는 수집하지 않는다. 검출·디코딩을 다시 실행하지 않는다.

`services/tier0/service.py`는 이미지가 있는 `DetectorFeed`에만 `BackgroundCleanBackground`를 조립한다. 영상 없는 SIM 관측 공급기는 수집하지 않는다. LIVE와 FILE(REPLAY) 모두 기존 `build_input_provenance` 결과를 메타·상태 파일에 보존한다. 알림 채널은 추가하지 않는다.

tick의 `offer`는 표본 간격 확인과 참조 enqueue만 한다. 큐 lock을 즉시 잡지 못하거나 깊이·바이트 상한을 넘으면 버린다. 프레임 배열은 현재 디코더가 새로 생성하고 이후 수정하지 않는 BGR 배열을 참조한다. `np.array(copy=True)`·사람 마스크·준비 비율·중앙값·JPEG·JSON·디렉터리 공개는 `tier0-clean-background` 스레드에서만 실행한다. 큐 lock을 잡은 채 이 작업들을 하지 않는다. 서비스의 기존 stop 이벤트·스레드 종료 제한을 함께 쓰며, 파일 쓰기 실패는 로그·상태로 알리고 1초 뒤 보유 표본으로 재시도한다. 실패나 수집 지연 때문에 판정을 기다리게 하지 않는다.

### 설정과 값의 근거

Tier 0 JSON 설정의 `clean_background` 객체를 쓴다. `output_dir` 상대 경로는 해당 설정 파일의 부모 기준이다.

```json
{
  "clean_background": {
    "enabled": true,
    "sample_interval_ms": 5000,
    "min_samples_per_camera": 20,
    "max_samples_per_camera": 60,
    "max_bytes": 2306867200,
    "queue_depth": 4,
    "max_pending_bytes": 67108864
  }
}
```

- 서비스 기본 간격은 5초다. 독립 누적기의 3초 기본값은 유지한다. 관제 중 CPU·복사 빈도를 낮추며 약 100초에 최소 20장을 모으는 초기 운영값이다.
- 최소 20장·최대 60장·누적 표본+마스크 상한 2,200 MiB, 박스 확장·충분 기준(`< 2%`)·64행 중앙값은 -358 값을 재사용한다.
- 큐 깊이 4는 카메라 한 묶음을 수용한다. 보류 프레임 상한 64 MiB는 2304×1296 BGR 4장(약 34 MiB)을 수용하되 큰 영상이 무제한 쌓이지 않게 한다. 이 값은 **큐에 남은 참조** 상한이다. 처리 중 프레임 1장, 누적기의 복사·마스크·중앙값 임시 배열, 원본 디코더 메모리는 별도다. 프로세스 RSS 상한이라는 뜻이 아니다.
- 카메라가 충분해지면 사진을 한 번 공개하고 그 카메라의 추가 수집은 중단한다. 완성 뒤 계속 중앙값을 계산하지 않는다. 버전 교체·reset 때만 다시 수집/공개한다.
- `enabled: false`이면 수집 스레드·콜백·배경 출력 경로를 만들지 않는다. 양수·bool·최소/최대 표본 관계를 기동 전에 검증한다.

### DRIFTED·보정 교체

`Tier0Service._apply_drift_report`에서 기존 감시가 귀속한 DRIFTED 카메라만 `reset_camera` 요청한다. 스트림 해상도 등 변경이 보고된 카메라도 비운다. 귀속 불확실한 전체 DRIFTED를 임의로 모든 카메라의 틀어짐으로 해석하지 않는다.

`_begin_calibration_tick`에서 실제 활성화 성공 뒤 새 보정 문서와 출처를 넘긴다. 카메라별 `R`·`t`를 비교해 자세가 바뀐 카메라만 비운다. `K`·`distortion`·`image_size` 변화도 영상 해석이 달라지므로 같은 방식으로 무효화한다. 다른 카메라의 표본은 유지하고 새 보정 버전 메타로 다시 내보낸다. 버전 ID만 바뀌면 표본을 모두 보존한다.

tick은 작은 불변 문맥과 카메라 세대 번호만 교체한다. 실제 누적기 reset은 수집 스레드가 적용한다. 세대가 다른 큐 표본은 버리고, 중앙값 계산 중 문맥이 바뀌었으면 그 임시 산출물을 공개하지 않는다. 기존 완성 산출물은 이력으로 남는다.

### 경로·형식과 -364 인계

기본 루트는 `state_dir/clean-background/`다(기본 `state_dir`이면 `$SIMAI_ROOT/state/clean-background/`). FILE 입력은 기존 설정의 `state_dir/replay/<run>/` 격리도 따른다. 별도 `output_dir`를 주면 그 경로를 쓴다.

```text
clean-background/
  status.json
  <calibration_version>/
    camera-<uuid>/
      <camera_id>.jpg
      clean_backgrounds.json
    bundle-<uuid>/
      <camera_1>.jpg ... <camera_4>.jpg
      clean_backgrounds.json
      COMPLETE
```

카메라마다 충분해지면 `camera-<uuid>`에 사진·메타를 먼저 공개한다. 설정된 카메라가 모두(현장 기본 4대) 준비되면 이미 공개한 사진을 묶어 `bundle-<uuid>`와 `COMPLETE`를 공개한다. 기존 `simai.clean_backgrounds` 0.1.0의 `cameras[camera_id].image`·해상도·SHA256·표본 수·촬영 범위·못 채운 비율·보정 버전을 그대로 쓰고, 서비스 메타에 `input_provenance`·`camera_generations`·`complete`를 덧붙인다. 이 파일은 기존 내부 준비 형식이며 구간 계약 스키마를 바꾸지 않는다. `COMPLETE`는 JSON으로 `calibration_version`, `metadata: "clean_backgrounds.json"`, `camera_ids`를 담는다.

파일은 같은 부모의 숨김 `.pending-<uuid>`에 먼저 완성한 뒤 **새 UUID 디렉터리로 rename**한다. 따라서 부분 작성된 사진·메타·완료 표시는 소비자에게 완성 묶음으로 보이지 않는다. 실패 임시 폴더는 정리하며 기존 산출물은 덮어쓰지 않는다. 완료 표시 없는 카메라별 폴더는 전체 입력으로 쓰지 않는다.

`status.json`은 최신 상태 포인터로 임시 파일 + replace한다. 카메라별 현재 `sample_count`·`never_revealed_ratio`·`ready`, 공개 여부 `exported`·경로 `export_path`, 전체 `complete`·`bundle`, 버전·세대·출처·큐 버림 수·마지막 오류를 담는다. 메모리 압박으로 누적 표본이 빠져도 이미 검증해 공개한 카메라 사진은 유효하므로 `ready`와 `exported`가 다를 수 있다.

소비자는 **`status.json`의 활성 `calibration_version`·`complete: true`·`bundle`과 그 폴더의 `COMPLETE`**를 함께 확인하고, 메타의 버전·세대를 대조한다. 이전 `bundle-*`는 이력이므로 단순 디렉터리 검색으로 최신 입력을 고르지 않는다. `cameras`를 `{cid: bundle / camera["image"]}`로 바꾸면 `prepare_inputs`가 받는 이미지 매핑이다. 보정·DRIFTED 요청은 비동기로 반영되므로 상태 파일은 수집 스레드가 마지막으로 처리한 문맥의 스냅샷이다. 소비 작업 시작 시 현재 보정과의 적합성 확인은 -364가 맡는다.

### 시험·9차 세션 확인 로그와 잔여 위험

새 시험 `ai/tests/services/tier0/test_clean_background.py`는 합성 BGR 영상과 움직이는 사람 박스만 쓴다. 간격 경계·큐 초과/lock 경합·지속 가림 보류·검출 식별자 게이트·수집 지연 중 실제 judge tick·DRIFTED 및 보정 전환 연결·기존 산출물 보존·계산 중 reset·같은 버전 재수집·파일 실패 재시도·끄기를 확인한다. -358 누적기 및 기존 drift/live-calibration 시험을 함께 실행한다. 합성 지연은 CPU/GPU 현장 성능을 뜻하지 않는다.

9차 GPU 세션에서는 다음 로그와 `status.json`을 확인한다.

```text
clean_background context version=... generations=...
clean_background status {... "sample_count": ..., "never_revealed_ratio": ..., "exported": ...}
clean_background camera_ready camera=... version=... path=...
clean_background complete version=... path=...
```

`clean_background failed`·`status_write_failed`는 CPU/저장 경로 장애다. 별도 I1 health 필드는 추가하지 않는다. 큐 버림이 지속되면 표본 수집은 늦어져도 tick은 진행한다. 원본 해상도 중앙값의 CPU/GIL·메모리 대역폭 경쟁과 실제 Pod RSS·Tier 0 지연은 9차 세션에서 확인해야 한다. 계속 가려진 픽셀 비율이 2% 이상이면 의도적으로 완료하지 않는다. 프로세스 종료 뒤 표본 자동 복구·묶음 보존 기간 정리·강제 내보내기는 범위 밖이며, 불변 묶음이 쌓이므로 운영자가 저장 공간을 관리해야 한다. 원자 rename은 부분 노출 방지이며 전원 손실에 대한 fsync 영속성 보장은 아니다.
