# GPU 가 가져가지 않은 세팅 닫기, 막는 세션을 409 에 싣기 (S15P31A101-641)

프론트 점검(GPU 꺼짐 시나리오)의 서버 요청 S1·S2 다. 관련: 명세 3.2 5항(작업 시작은 미리보기 맵 뒤), 4.3 현장 세팅, 12.5 작업 대기열.

## 문제

- 대기 작업에는 마감이 없다(`deadline_at` 은 `vlm_rationale` 에만 붙는다). GPU 인스턴스가 꺼져 있으면 `site_setup` 작업을 가져갈
  워커가 없어 세션이 보정·생성 0% 에서 끝없이 기다렸다. 나갈 길은 관리자 취소뿐이었다.
- 멈춘 세션 하나가 같은 현장의 새 세팅(409 `SETUP_ALREADY_IN_PROGRESS`)과 작업 시작(409 `WORK_SETUP_NOT_READY`)을 모두 막았다.
  응답에 어느 세션인지 없어, 그 세션이 다른 날짜 작업 소속이면 화면에서 찾아 취소할 길이 없었다.

## 만든 것

| 항목 | 위치 |
|---|---|
| 대여되지 않은 세팅 닫기(30초마다) | `SetupQueueTimeout.closeUnclaimed` → `SetupService.unclaimedBefore`·`failUnclaimed` |
| 관리자 취소와 같은 닫기 경로(세션 FAILED + 세팅·구역 초안 작업 취소) | `SetupService.closeAndCancelJobs` |
| 409 응답에 `setup_session_id`·`work_id` | `SetupSessionBlockingException`, `GlobalExceptionHandler`, `WorkUnitRepository`(작업 시작), `SetupService.create` |
| 설정 | `simai.setup.queue-timeout`(10m), `simai.setup.queue-check-interval`(30s) |
| 시험 | `SetupSessionIntegrationTest`(시간 초과 닫기·워커가 보이거나 바쁘면 두기·대여됐던 작업도 워커가 사라지면 닫기·409 본문), `SetupSupplementIntegrationTest`(작업 시작·다시 굽기 409 본문) |

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 닫는 대상 | 미리보기 맵 전(`COLLECTING_BACKGROUND`~`PREVIEW_AND_VLM_DRAFT`) 세션의 원래 `site_setup` 작업이 QUEUED 이고, `site_setup` 을 받는 워커가 기준 시간 동안 한 번도 보이지 않았을 때. "보였다"는 대여 요청 시각(`worker.last_seen_at`) 또는 그 워커가 쥔 진행 중 대여의 하트비트(`job_lease.last_heartbeat_at`) | 작업 시작을 막는 단계만 대상이다. 처음에는 "한 번도 대여되지 않음"으로 정했으나, 워커가 다른 BACKGROUND 작업으로 바빠 10분 넘게 못 가져가는 정상 상황까지 닫고, 대여됐다가 GPU 가 꺼져 다시 대기열로 온 작업은 영영 못 닫아 리뷰에서 바꿨다. 바쁜 워커는 대여 요청을 보내지 않으므로 하트비트도 본다. 맵 보강 다시 굽기(READY 뒤)는 세션을 실패로 바꾸면 안 되므로 뺀다 |
| 기준 시간 | 작업을 넣은 뒤 10분, 그리고 워커가 보이지 않은 지 10분 | GPU 인스턴스를 켜고 워커가 붙는 데 걸리는 시간보다 넉넉히. 세팅은 관제 중에 시작하지 못하고 작업 시작은 세팅이 막으므로, LIVE 때문에 정당하게 기다리는 경우는 없다 |
| 닫은 결과 | 세션 `FAILED`, `failure_code=SETUP_QUEUE_TIMEOUT`, 작업 `CANCELLED` | 관리자 취소(`CANCELLED_BY_ADMIN`)와 같은 경로로 닫고 이유만 다르게 남긴다. 화면은 실패 문구를 그대로 보여 준다 |
| 잠금 순서 | 작업(`FOR UPDATE SKIP LOCKED`) → 세션 | 대여 경로(대여 → 작업 → 세션)와 같은 방향. 지금 대여 중인 작업은 건너뛴다 |
| 409 본문 | `setup_session_id`(UUID 문자열), `work_id`(문자열, `WORK_ALREADY_IN_PROGRESS` 의 `work_id` 와 같은 꼴) | 막는 세션이 여럿이면 하나. 새 세팅의 409 는 취소할 수 있는 진행 중 세션을 다시 굽기 중인 완료 세션보다 먼저, 그다음 최근 것 |

## 시험

- `./gradlew test --tests '*SetupSessionIntegrationTest' --tests '*SetupSupplementIntegrationTest'`: 20개 통과
- 전체 `./gradlew build`(임시 DB `simai_test_641`)

## 남은 일

- 프론트: 409 본문의 `setup_session_id`·`work_id` 로 막는 세션의 날짜·세팅 화면으로 이동하고 취소할 수 있게 한다(F4 세팅 탈출로). `SETUP_QUEUE_TIMEOUT` 실패 문구를 보여 준다.
- 맵 보강 다시 굽기가 GPU 없이 오래 대기하는 경우는 작업 시작은 막지 않지만(READY 뒤 허용) 새 세팅은 막는다. 필요하면 다시 굽기 행만 닫는 같은 규칙을 더한다.
