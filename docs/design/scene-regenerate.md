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
