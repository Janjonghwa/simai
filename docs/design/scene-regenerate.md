# 3D 배경(Scene) 원클릭 재생성 설계 (S15P31A101-428)

## 1. 목적
자재 반입·구조물 변경 등으로 현장 정적 환경이 달라졌을 때, 관제 화면(Admin 앱)에서 관리자가 **"맵 재생성"** 버튼 하나로
배경 캡처 트리거(`scene_regenerate`) → 정적 3D 재구성(`static_reconstruct`) → 후보 Scene 생성(`CANDIDATE_READY`) 및 활성화(`COMMITTED`)까지 이어지는 흐름을 제공한다.

## 2. 데이터 모델 (`V32__scene_regenerate_request.sql`)
- 표: `scene_regenerate_request`
  - `id` (`bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY`): 요청 식별자(`request_id`)
  - `status` (`text`): `REQUESTED` | `CAPTURING` | `RECONSTRUCTING` | `CANDIDATE_READY` | `FAILED`
  - `failure_reason` (`text`, nullable): `NO_WORKER` | `TIER0_NOT_RUNNING` | `BACKGROUND_NOT_READY` | `UPLOAD_FAILED` | `RECONSTRUCT_FAILED` | `TIMEOUT`
  - `reason` (`text`, nullable, `<= 120자`): 관리자가 입력한 재생성 사유(사람 이름은 포함하지 않음)
  - `requested_by` (`bigint REFERENCES app_user`): 요청 관리자 ID
  - `job_id` (`text REFERENCES analysis_job`): `scene_regenerate` 작업 ID
  - `static_reconstruct_job_id` (`text REFERENCES analysis_job`): 연결된 `static_reconstruct` 작업 ID
  - `scene_version` (`text REFERENCES scene_version`): 생성된 후보 Scene 버전
  - `created_at`, `updated_at` (`timestamptz`)
- 제약: 부분 유니크 인덱스(`uq_scene_regenerate_request_in_progress`)로 진행 중(`REQUESTED`, `CAPTURING`, `RECONSTRUCTING`)인 요청은 동시에 최대 1개만 허용한다.

## 3. REST API 및 상태 전이
1. `POST /api/scenes/regenerate` (`ADMIN` 전용)
   - 요청 본문: `{"reason": "자재 반입"}` (선택, 최대 120자)
   - 진행 중인 요청이 있으면 `409 Conflict` (`{"code": "REGENERATE_IN_PROGRESS", "request_id": ...}`) 반환
   - 없으면 `scene_regenerate_request(REQUESTED)` 생성 후 `analysis_job(job_type = "scene_regenerate", priority = "BACKGROUND")` 큐잉 → `202 Accepted` (`{"request_id", "status": "REQUESTED"}`)
2. 상태 전이
   - `REQUESTED` → `CAPTURING`: 워커가 `scene_regenerate` 잡을 lease 했을 때(`JobStateChanged(LEASED)`)
   - `CAPTURING` → `RECONSTRUCTING`: 워커가 `POST /api/worker/scene-jobs/static-reconstruct` 에 선택 `request_id` 를 실어 제출하거나 `scene_regenerate` 콜백이 `SUCCEEDED` 로 완료됐을 때
   - `RECONSTRUCTING` → `CANDIDATE_READY`: `SceneJobListener` 가 `static_reconstruct` 완료(`SUCCEEDED`)를 받아 후보 `scene_version`(`CANDIDATE`)을 생성했을 때
   - `*` → `FAILED`: 워커 실패 콜백 또는 `REQUESTED` 상태에서 10분 이내 미임대(`TIMEOUT`) 시 `failure_reason` 기록
3. `GET /api/scenes/regenerate/{request_id}` 및 `GET /api/scenes/regenerate?latest=1` (`Viewer` 이상)
   - `{"request_id", "status", "failure_reason", "scene_version", "reason", "requested_at_ms", "updated_at_ms"}` 반환 (`latest=1` 조회 시 이력이 없으면 `204 No Content`)

## 4. 관제 앱 (`front/apps/admin`)
- 실시간 모니터링 화면(`SpatialViewportHost`) 상단 도구 영역에 관리자(`ADMIN`) 전용 `SceneRegeneratePanel` 배치(모바일 폭에서는 숨김).
- 진행 중(`REQUESTED`·`CAPTURING`·`RECONSTRUCTING`)일 때 2초 주기로 폴링하며 한국어 단계명과 실패 사유를 표시한다.
- `CANDIDATE_READY` 도달 시 **"새 3D 배경 후보 — 활성화"** 버튼을 노출하여 `POST /api/scenes/{sceneVersion}/commit` 호출 및 현재 Scene(`['scenes', 'current']`)을 즉시 갱신한다.

## 5. I5 계약과 검증 (2차)

- `scene_regenerate`는 계약 v7.2.0의 `job_request` 3.6.0과 `job_callback` 2.5.0으로 직접 교환한다. 대여 요청·작업 입력·콜백은 원본 스키마로 검사하며 `static_reconstruct`로 치환하지 않는다. 입력은 `request_id`, `requested_at_ms`, 선택 `reason`이고 사람 식별 정보는 넣지 않는다.
- 워커는 배경 묶음 등록에 `request_id`를 전달하고 성공 결과로 `bundle_id`와 `static_reconstruct_job_id`를 보낸다. 서버는 결과가 실제 등록된 `static_reconstruct` 작업과 묶음에 일치하는지 확인한다. 후보 생성은 해당 작업의 `scene_output` 검증 이후 기존 Scene 경로가 처리한다.
- LIVE 중 재생성 및 복원은 기본적으로 차단된다. 워커의 명시적 예외 설정과 측정·명세 결정에 관한 잔여 위험은 `docs/design/worker-static-reconstruct.md`를 따른다.
- 통합 시험은 계약 예시와 잘못된 결과를 사용한다. 서버 시험은 로컬 PostgreSQL에서 실행해야 한다. 프런트 API 형태는 바뀌지 않았다.


## 6. 맵 재생성 배경 품질 개선 — 최근 구간 중앙값·이전 배경 채움·이음새 블렌딩 (S15P31A101-461)

11차 로컬 세션(`RESULTS.md` §6)에서 발견된 한계(전체 누적 중앙값으로 인해 사람이 계속 앉아 있던 자리의 픽셀이 배경 사진에 남거나 검은 구멍으로 남고, 바뀐 물건이 옛 프레임에 묻히는 문제)를 해결하기 위해 `ai/src/simai_ai/scene/clean_bg_job.py` 및 `ai/services/worker/scene_regenerate.py` 에 다음 4단계를 적용한다.

1. **최근 구간 배경 중앙값 (`compute_recent_window_median`)**
   - 전체 누적 대신 설정 가능한 최근 창(`window_frames`, 기본 `30`장) 안에서만 프레임을 취한다.
   - 사람이 검출된 마스크(2D 가림 마스크 또는 `bbox_pad_px` 여백을 둔 검출 박스) 픽셀은 해당 프레임에서 `NaN` 으로 제외하고, 관측된 배경 픽셀들의 채널별 중앙값(`np.nanmedian`)으로 최근 배경을 합성한다.
   - 최근 창 내내 사람이 앉아 있어 한 번도 드러나지 않은 픽셀(`~recent_revealed`)은 절대 가려진 프레임의 중앙값으로 대체하지 않는다.

2. **못 본 곳 채우기 (`compose_with_previous_background` / `refine_bundle_backgrounds`)**
   - 최근 창에서 한 번도 배경이 드러나지 않은 영역(`~recent_revealed`, 또는 Tier 0 묶음의 `<cid>.valid.png == 0`)은 같은 보정 버전·카메라 세대(`camera_generations`)의 **이전 배경 사진**(초기 세팅 `REFERENCE` 카메라 폴더 또는 직전 완료 `bundle-*` 묶음)에서 유효한 픽셀을 가져와 채운다.
   - 이전 배경/맵도 없거나 이전 배경에서도 관측되지 않은 픽셀은 **만들어내지 않고 비운 뒤(`0`, `valid_mask = 0`)** 미관측 상태로 기록한다.

3. **경계 이음새 섞기 (`blend_seam_feather`)**
   - 최근 중앙값 영역(`recent_revealed`)과 이전 배경으로 채운 영역(`previous_filled_mask`)의 경계 반경(`blend_radius_px`, 기본 `8` px) 안에서 유클리드 거리 변환(`cv2.distanceTransform`)과 정규화 가우시안 경계 확장을 결합한 양방향 거리 가중 페더링($\alpha = \text{clip}(0.5 + 0.5 \cdot s / r, 0, 1)$)을 수행해 채운 자리의 급격한 밝기·색 단차를 완화한다.
   - 가려진 사람 픽셀이나 미관측(`unrevealed`) 영역은 블렌딩 가중치에서 완전히 배제된다.

4. **품질 비율 기록 및 프런트 표시 연동 (`RegenerateQuality`)**
   - 카메라별 및 전체 평균으로 다음 두 지표를 0~1 비율로 계산해 남긴다:
     - `previous_fill_ratio`: 최근 창에서 가려져 **이전 모습으로 채운 비율**
     - `unrevealed_ratio`: 최근 창과 이전 배경 모두에서 관측되지 않아 **비운 비율**
   - 기록 위치(계약 v7.4.0 마이너 확장):
     - 묶음 메타데이터 `clean_backgrounds.json` 의 `cameras[cid].previous_fill_ratio`, `cameras[cid].unrevealed_ratio` 및 최상위 `regenerate_quality`
     - 워커 → EC2 `static_reconstruct_submit` (`payload.input.regenerate_quality`)
     - 워커 → EC2 `scene_regenerate` 작업 콜백 (`job_callback.payload.result.quality`: `{previous_fill_ratio, unrevealed_ratio, cameras}`)
     - 정적 장면 결과 `scene_output` 2.2.0 (`quality.previous_fill_ratio`, `quality.unrevealed_ratio`, `quality.camera_background_quality`)
   - **프런트(`front/apps/admin`) 추후 표시 안내**:
     - 관제 화면의 맵 재생성 패널(`SceneRegeneratePanel`) 또는 후보 Scene 검토 카드에서 `quality.previous_fill_ratio`("이전 모습으로 채운 비율: X%")와 `quality.unrevealed_ratio`("미관측(비운) 비율: Y%"), 필요 시 `cameras`/`camera_background_quality` 의 카메라별 수치를 배지·툴팁으로 바로 표시할 수 있다.
