# S15P31A101-808 아바타 GPU 부하 줄이기 — 외형 분할 FP16·동기화 제거, 굽기 직전 게이트·감속 재확인, Re-ID 휴면 복원 재사용

11-19 과부하 구간(13:43~13:46)에서 상시 아바타가 GPU를 과다 점유한 세 가지 경로를 줄인다.
**표시 전용이며 Tier 0 위치·관절·위험 판정(AGENTS 규칙 1), I1·I6 계약, 얼굴 규칙(규칙 8, 생성형 얼굴 금지)은 바꾸지 않는다.**

## 관련 명세 절

| 절 | 내용 | 이 문서에서 |
|---|---|---|
| 3.2 | 한 시점에 GPU 인스턴스는 하나이며 Tier 0가 자원을 먼저 쓴다. 상시 아바타 스트림은 Tier 0 지연 초과·모름이면 멈춘다 | `AppearanceWorker`가 분할 실행 직전 게이트·감속 상태를 다시 확인해 닫혀 있으면 다음 주기로 미룸. 측정용이 아닌 `torch.cuda.synchronize` 제거 |
| 5.2 | World Track 재연결·병합·분할 및 Re-ID `DORMANT` 30분 재입장(`-802`) | Tier 0가 30분 안의 같은 World ID를 복원할 때 아바타 체형·외형을 다시 굽지 않고 재사용 |
| 20.3 · 20.4 | EXP-02 아바타 외형 누적·교정 직후 누적 멈춤, 다른 사람의 외형이 섞이지 않도록 ID 변경 규칙 준수 | `identity_revision` 초기화(순수 병합 제외)·`split_from` 변경 시 기존처럼 완전 초기화 후 새로 굽기. 휴면 보관은 같은 `world_id` 복원 전용(새 ID 이어받기 미사용) |

## 코드·시험 위치

| 구분 | 경로 | 바꾼 것 |
|---|---|---|
| 외형 분할 | `ai/src/simai_ai/hmr/segmentation.py` | CUDA에서 `YoloPersonSegmenter` 기본 `half=True`(FP16, CPU는 FP32 고정, `SIMAI_AVATAR_SEG_HALF=auto\|0\|1`), `retina_masks=True` 및 해상도 검증 유지, 기본 `sync_timing=False`로 `torch.cuda.synchronize` 제거(`SIMAI_AVATAR_SEG_SYNC_TIMING=1`로만 측정용 활성화) |
| 외형 누적 | `ai/src/simai_ai/hmr/appearance.py`, `ai/src/simai_ai/hmr/appearance_stream.py` | `AppearanceConfig.sync_timing=False`(`SIMAI_AVATAR_TEXTURE_SYNC_TIMING=0\|1`)로 기본 경로의 CUDA 동기화·이벤트 대기 제거. `TexelSnapshot`에 발행 상태(`published_coverage`·`last_publish`·`dirty`) 보관 및 `absorb(..., publish=False)`로 이미 텍스처가 있는 같은 ID 복원 시 불필요한 재발행·재업로드 방지 |
| 외형 작업자 | `ai/src/simai_ai/hmr/appearance_worker.py` | `_run()` 시작과 `seg.prepare` 직전에 게이트(`gate.check().admit`)·감속 상태(`recommended_interval_ms() <= 200`) 재확인. 닫혀 있으면 작업을 버리지 않고 `_pending`에 되돌려 다음 주기(`defer_interval_s=0.2s`)로 미룸(`deferred`·`deferred_reasons` 계측) |
| 휴면 보관·루프 | `ai/src/simai_ai/hmr/appearance_carry.py`, `ai/src/simai_ai/hmr/stream.py` | `CarryConfig`에 같은 ID Re-ID 휴면 보관(`dormant_ttl_ms=1_800_000`, `max_dormant=64`, `max_dormant_bytes=512 MiB`) 추가. `departed`(20초·16명, 새 ID 이어받기·혼잡 판정) 만료·축출 시 자산 보유 ID를 `dormant`로 승격. `CarryDonor.size_bytes()` 메모리 계측, `revision`·`split_from` 변경 시 초기화(`state.baked=None` 포함), 60초 요약 로그에 `dormant`·`worker_deferred` 추가 |
| 단위·통합 시험 | `ai/tests/hmr/test_segmentation_fp16.py`, `ai/tests/hmr/test_segmentation.py`, `ai/tests/hmr/test_appearance_worker.py`, `ai/tests/hmr/test_appearance_carry.py`, `ai/tests/hmr/test_avatar_loop.py` | CUDA FP16/CPU FP32 전환·retina 해상도 오류 폴백·동기화 기본 비활성/환경변수 토글, 게이트 닫힘·감속 시 분할 미루기 및 재개 실행·신원 변경 폐기, 30분 휴면 복원(재굽기·재업로드 0회)·개수/메모리 상한 축출·`identity_revision`/`split_from` 변경 시 초기화 |

## 실제 상태 보존 위치 조사 결과

분석 메모에서 언급한 `stream.py stale_s=60`은 현재 코드에 존재하지 않는다. 실제 상태 보존과 삭제 위치는 다음과 같다.

1. `ai/src/simai_ai/hmr/stream.py` (`AvatarLoop.people`): 매 회차 `world_state.tracks`에서 `track_lifecycle != "LOST"`인 활성 ID만 남기고, 빠진 ID는 즉시 `self.people`에서 제거해 `self.carry.depart(...)`로 넘긴다. `_high_versions`·`_last_versions`만 세션 내 단조 버전 유지를 위해 정수로 남긴다.
2. `ai/src/simai_ai/hmr/appearance_carry.py` (`CarryConfig`·`AppearanceCarry.departed`): 기존에는 빠진 ID를 `keep_ms=20_000`(20초, `WorldTrackConfig.reconnect_window_ms`와 동일), `max_departed=16`까지만 들고 있다가 삭제했다.
3. `ai/src/simai_ai/tracking/world_track.py` (`WorldTrackConfig`): `-802` 이후 `reconnect_window_ms=20_000`을 넘긴 `CONFIRMED` 트랙을 `reid_dormant_ttl_ms=1_800_000`(30분) 동안 `_reid_dormant`에 보관해 같은 `world_id`로 복원한다. 20초~30분 사이 복원 때 아바타 쪽 `departed`에는 이미 상태가 없어 체형과 외형을 처음부터 다시 굽는 원인이었다.
4. `ai/src/simai_ai/hmr/appearance_stream.py` / `appearance_worker.py` / `services/avatar/transport.py`: `pipeline.retain(active)`와 `sender.retain(active)`가 비활성 ID를 즉시 정리하며, `AvatarSender.tick()`은 30초 넘은 `_assets`를 정리한다.

## 명세에 없어 정한 수치와 근거

| 항목 | 기본값 | 근거 |
|---|---|---|
| 외형 분할 정밀도 (`SIMAI_AVATAR_SEG_HALF`) | `auto` (CUDA `half=True`, CPU `False`) | `yolo26x-seg` imgsz 1280 4카메라 배치의 연산·대역폭을 FP16으로 줄이되, `retina_masks=True`와 원본 해상도 일치 검증·0.5 이진화 규칙은 그대로 유지한다. GPU A/B 비교나 회귀 확인 시 `SIMAI_AVATAR_SEG_HALF=0`으로 즉시 FP32 복귀 가능 |
| 측정용 CUDA 동기화 (`SIMAI_AVATAR_SEG_SYNC_TIMING`, `SIMAI_AVATAR_TEXTURE_SYNC_TIMING`) | `0` (비활성) | 기본 경로에서 `torch.cuda.synchronize`와 CUDA Event `.synchronize()`가 호출되면 같은 GPU를 쓰는 Tier 0 스트림과 불필요하게 전역 동기화된다. 정밀 타이밍이 필요한 프로파일링 세션에서만 `1`로 켠다 |
| 분할 허용 최대 권장 주기 (`AppearanceWorker.seg_max_interval_ms`) | `None`(감속으로 미루지 않음, Lead 결정 2026-11-19) | 17차처럼 판정 p95 가 오래 100~200 ms 면 `recommended_interval_ms` 가 계속 800 ms 라, 감속으로 미루면 새 사람의 외형이 끝내 생기지 않는다(아바타 시연 필수). 외형 분할은 아바타 게이트가 닫혔을 때(예산 초과 10 s 유지·재개 직후 재차단, -807)만 미룬다. 값을 주면(예: 200) 그 권장 주기를 넘을 때도 미룬다 |
| 미룬 외형 작업 재확인 주기 (`AppearanceWorker.defer_interval_s`) | `0.2 s` | 아바타 포즈 루프 기본 주기(5 Hz = 200 ms)와 맞춘다. 새 `offer()`나 `close()`가 오면 조건 변수로 즉시 깨어나며, 포즈 루프가 게이트·감속으로 쉬는 동안에도 마지막 유효 작업을 버리지 않고 0.2초마다 재확인한다 |
| 같은 ID 휴면 보존 시간 (`CarryConfig.dormant_ttl_ms`) | `1_800_000 ms` (30분) | Tier 0 Re-ID 휴면 보관(`WorldTrackConfig.reid_dormant_ttl_ms = 1_800_000`)과 일치시켜 30분 안 재입장 때 재굽기를 없앤다. 단기 `departed`(`keep_ms=20_000`, `max_departed=16`)와 분리해 새 ID 이어받기(3초 창)·혼잡 판정에는 절대 쓰지 않는다 |
| 휴면 보관 개수 상한 (`CarryConfig.max_dormant`) | `64` | 교실·현장 30분 동안 출입하는 고유 인원 상한(동시 2~10명의 6배 이상 여유). 초과 시 가장 오래된(`departed_t` 최소) ID부터 축출한다 |
| 휴면 보관 메모리 상한 (`CarryConfig.max_dormant_bytes`) | `512 MiB` (`536,870,912 B`) | 1024² `TexelSnapshot`(색 12 + 가중치 4 + 최고 세기 4 + 표시 1 + PNG ~1 MiB) + `BakedPersonRig`(~0.5 MiB) ≈ 22.5 MiB/인 기준 약 22명 풀해상도 누적 사본을 보유하면서도 호스트 RAM 증가를 0.5 GiB 이내로 제한한다(기존 `-743` 단기 보관 16명 ≈ 280 MB 대비 보수적 상한). 텍스처가 없거나 저해상도(512², ~6 MiB/인)일 때는 개수 상한 64명까지 수용한다 |

## 버린 대안

1. **`CarryConfig.keep_ms` 자체를 20초에서 30분으로 늘리는 안**: `departed`는 새 World ID 기하 이어받기와 혼잡 판정(`crowd_lost`)에 쓰이며, `keep_ms == WorldTrackConfig.reconnect_window_ms`(20초) 계약 테스트가 묶여 있다. 30분 휴면 보관을 `dormant`로 분리해 같은 `world_id` 복원(`take`)에만 쓰도록 격리했다.
2. **게이트 닫힘·감속 시 외형 작업을 버리고 다음 `offer()`만 기다리는 안**: 포즈 루프도 같은 게이트·감속을 따르므로 포즈 루프가 쉴 때 직전 유효 외형 요청이 유실되거나, 감속이 풀린 뒤에도 다음 샘플링 시점까지 굽기가 늦어진다. 대기 슬롯(`_pending`)에 되돌려 최신 1건을 유지하면서 다음 주기로 미루는 편이 안전하고 단순하다.
3. **휴면 텍스처 사본을 PNG로 재압축해 보관하는 안**: 텍셀별 가중치(`weights`)·최고 세기(`peaks`)가 사라져 복원 뒤 `best` 품질 합치기 연속성이 깨진다. 원본 numpy 배열 참조를 유지하되 `max_dormant_bytes=512 MiB` 상한으로 메모리를 제한한다.
4. **감속 단계(권장 주기 200 ms 초과)부터 분할을 미루는 안(Owner 초안)**: Tier 0 를 더 보호하지만, 판정 p95 가 기준선(100 ms)을 넘는 일이 흔해 새 사람의 외형이 계속 미뤄진다. 아바타 시연 필수라 버리고, 차단은 게이트에 맡겼다(옵션으로는 남김).

## 잔여 위험

- **17차처럼 판정 p95가 장시간 100~200 ms 인 구간**: 기본값에서는 감속만으로 외형 분할을 미루지 않으므로 Tier 0 부하가 조금 더 남는다. 예산(판정 200·검출 150 ms)을 10 s 넘으면 게이트가 닫혀 분할도 멈춘다.
- **512 MiB 상한 초과 시 1024² 기준 약 23명 이상 동시 휴면**: 30분 안에 23명 넘는 고유 ID가 모두 풀해상도 텍스처를 굽고 빠져나가면 가장 오래된 ID부터 휴면 보관에서 축출되어 재입장 시 다시 굽게 된다. 필요 시 추후 환경변수로 상한을 조정할 수 있다.
- **Windows 개발 환경 제약**: 현재 노트북 가상환경에는 `cv2`·`torch`·`ultralytics`가 없어 실제 GPU FP16 커널 실행과 `test_segmentation.py` 전체는 GPU Pod/Linux CI에서 확인해야 한다(`test_segmentation_fp16.py`에서 가짜 텐서·모듈로 FP16/retina/동기화 규칙을 검증함).

## GPU에서 잴 것

1. **외형 분할 FP16 vs FP32 (`SIMAI_AVATAR_SEG_HALF=1` vs `0`)**:
   - `SIMAI_AVATAR_SEG_SYNC_TIMING=1`로 4카메라 `yolo26x-seg`(imgsz 1280, `retina_masks=True`) 배치 `elapsed_ms`, GPU utilization, VRAM 변화.
   - FP16 마스크와 FP32 마스크의 픽셀 IoU 및 외형 텍스처 `observed` 비율 차이(마스크 해상도·품질 규칙 유지 확인).
2. **동기화 제거 효과 (`SIMAI_AVATAR_SEG_SYNC_TIMING=0`, `SIMAI_AVATAR_TEXTURE_SYNC_TIMING=0`)**:
   - 과부하 구간(3~5명 동시)에서 Tier 0 `detect_trt`·`detect_once` p95와 `judge` p95 변화, `AvatarAdmissionGate` `resets.over_budget` 횟수.
3. **`AppearanceWorker` 게이트·감속 미루기**:
   - 60초 요약 로그의 `worker_deferred` (`gate_closed`·`throttled`) 건수와, 감속 해제 직후 미뤄 둔 외형 분할이 누락 없이 완료되는지 확인.
4. **30분 Re-ID 휴면 복원**:
   - 20초~30분 사이 퇴장·재입장 시 `carry.dormant_restored`·`identity_events.lost_restored` 증가와 함께 해당 ID의 `model.bake`·GLB/PNG 재업로드가 0회인지, `dormant` 메모리(`MB`)가 `512 MB` 상한 안에서 유지되는지 확인.

## 넘긴 일

- GPU Pod에서 위 4개 항목 실측 후 `contracts/experiments/run_record.schema.json` 기록 및 필요 시 `max_dormant_bytes`·`seg_max_interval_ms` 미세 조정.
- 감속 구간(`400 ms` vs `600 ms`)이 길어 새 사람 외형 첫 발행이 지나치게 지연되면 `SIMAI_AVATAR_SEG_MAX_INTERVAL_MS` 환경변수 노출 검토.
