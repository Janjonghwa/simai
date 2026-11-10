# Tier 0 배경 누적 프로세스 분리 (S15P31A101-563)

## 관련 명세·코드·시험

- 명세: 3.2(Tier 0 GPU·자원 우선), 4.3 1단계(관제 중 빈 배경 누적·정적 복원 입력), 4.4(회전 복구 배경 비교)
- 코드:
  - `ai/services/tier0/clean_background.py` (`BackgroundCleanBackground`, `_OffloadAccumulatorProxy`, `_clean_background_worker_main`)
  - `ai/services/tier0/settings.py` (`CleanBackgroundSettings.offload`)
  - `ai/services/tier0/service.py` (`Tier0Service._shutdown` 정리 연동)
  - `ai/src/simai_ai/calibration/world_frame/clean_background.py` (`CleanBackgroundAccumulator`)
- 시험:
  - `ai/tests/services/tier0/test_clean_background.py`
  - `ai/tests/services/tier0/test_rotation_recovery.py`
  - `ai/tests/services/tier0/test_tier0_service.py`

## 무엇을 옮겼고 왜

13차 세션(`lobby-s13.log.gz`)에서 Tier 0 프로세스 내부의 `tier0-clean-background` 스레드가 2304×1296 원본 표본(카메라당 최대 60장, 4대 약 2.3 GB)을 보관하고 `nanmedian` 합성·시간 분산 마스크(`_mask_unknown`)·회전 복구용 축소 표본 생성을 수행하면서, 판정(`tier0-judge`) 및 검출(`tier0-detect`) 스레드와 GIL·메모리 대역폭을 다투어 `queue_dropped=3263` 및 회전 복구 CPU 예산(500 ms) 초과가 반복되었다.

이에 따라 원본 해상도(2304×1296)는 그대로 유지하면서 다음 작업을 `multiprocessing.get_context("spawn")` 자식 프로세스 1개(`tier0-clean-bg-worker`)로 분리했다:

1. **카메라별 표본 링버퍼(`CleanBackgroundAccumulator`) 보관**: 약 2.2 GiB 규모의 원본 BGR 표본과 `reveal_mask`를 자식 프로세스 주소 공간에만 둔다.
2. **사람 마스크 생성(`build_reveal_mask`) 및 표본 만료**: 부모는 검출 결과에서 사람 bbox 좌표 튜플만 추출해 넘기고, 픽셀 마스크 생성은 자식이 수행한다.
3. **회전 복구용 축소 표본(`_small_samples_for_camera`) 생성**: 최근 창(최대 10장)의 `cv2.resize` 및 드러난 영역 검사를 자식에서 수행하고 축소본만 부모의 `_rotation_views` 스냅샷에 반영한다.
4. **배경 합성(`compute_masked_median_background`, `nanmedian`) 및 시간 분산 마스크(`_mask_unknown_with_samples`)**: 가장 무거운 float32 행 묶음 `nanmedian`·분산 계산·JPEG/PNG 인코딩·SHA-256 해시를 자식 프로세스에서 `.pending-*` 임시 폴더에 기록하고, 부모는 generation 일치 여부를 확인한 뒤 원자적 `rename`(`_publish`)만 수행한다.

## 프로세스 경계·전송·장애 처리 설계

- **설정(`clean_background.offload`)**: 기본값 `true`. `false`로 두면 기존 인프로세스 경로로 동작한다.
- **비블로킹 프레임 전달(`offer` + `multiprocessing.shared_memory`)**:
  - 판정/검출 스레드가 호출하는 `offer()`는 `blocking=False` 락과 유계 큐(`queue_depth=4`, `max_pending_bytes=64 MiB`)로 참조만 넘기며, 큐가 차 있거나 락이 바쁘면 기다리지 않고 즉시 버리고 `dropped`(`status.json`의 `queue_dropped`)를 올린다.
  - 프레임 픽셀 전송은 `queue_depth`개의 고정 `SharedMemory` 슬롯과 작은 메타데이터 큐(`put_nowait`)를 사용한다. 슬롯이 모두 사용 중이거나 메타 큐가 가득 차면 기다리지 않고 버리며 `dropped`를 올린다.
- **장애 복구(1회 재시작 후 비활성화) 및 종료 정리**:
  - 자식 프로세스가 비정상 종료되면 경고를 남기고 기존 공유 메모리를 정리한 뒤 **1회 재시작**한다(`worker_restarts == 1`).
  - 재시작한 자식까지 다시 종료되면 에러 로그를 남기고 배경 누적을 끈다(`disabled = True`). 이후 `offer()`와 `process_one()`은 즉시 `False`를 반환하며 Tier 0 위험 판정은 영향을 받지 않고 계속된다.
  - 서비스 종료(`close()`, `Tier0Service._shutdown`) 및 프로세스 종료 시 자식 프로세스를 종료하고 생성한 모든 `SharedMemory` 세그먼트를 `close()` 및 `unlink()`한다.

## 버린 대안

1. **표본 저장 해상도 축소(긴 변 1152 px 등)**:
   - `world_frame.prepare.prepare_inputs`, `reconstruct_tasks.prepare`, `rotation_worker.reference_features`가 모두 배경 이미지 해상도와 보정 문서의 `camera["image_size"]`(및 내부 파라미터 `K`) 일치를 강제한다(`STREAM_CHANGED` 등). 보정 문서와 특징점 좌표계를 함께 바꾸지 않으면 호환성이 깨지므로 원본 해상도를 유지했다.
2. **인프로세스 스레드 유지**:
   - 이미 `tier0-clean-background` 전용 스레드로 분리되어 있었으나, 같은 프로세스 내 2.3 GB 링버퍼 상주와 NumPy/OpenCV 구간의 메모리 대역·GIL 경합을 막지 못해 기각했다.
3. **`multiprocessing.Queue`에 NumPy 배열을 직접 pickle 전송**:
   - 4대×2304×1296×3(장당 약 8.96 MB) 배열을 매 표본마다 직렬화·역직렬화하면 부모 프로세스에서 GIL 점유와 메모리 복사가 다시 커지므로 버리고, `multiprocessing.shared_memory` 재사용 슬롯 + 경량 메타 큐 방식을 채택했다.

## 남은 위험

- 자식 프로세스가 죽어 재시작될 때 자식 메모리에만 있던 미내보내기 표본은 비워지므로 그 카메라의 표본을 다시 모아야 한다(이미 디스크에 내보낸 기준 배경 `_reference` 및 완성 묶음은 그대로 유지된다).
- 원본 해상도를 유지하므로 호스트 전체 RAM 사용량(자식 프로세스 상한 기본 2.2 GiB)은 동일하다. 해상도 축소는 보정 `image_size`·`K` 스케일링과 MapAnything·회전 복구 정밀도 영향을 함께 검증하는 별도 후속 과제로 둔다.
