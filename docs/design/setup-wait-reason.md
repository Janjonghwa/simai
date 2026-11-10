# 세팅 대기 이유 표시 (S15P31A101-583)

- **티켓**: `S15P31A101-583` (13차 현장 세션 P1 #8)
- **명세 절**: 3.2 (GPU 공유·Tier 0 우선 접수 문), 4 (세팅 실패 원인·조치 안내), 8.1 (세팅 대기 화면), 12.5 (I5 작업 대여)
- **코드 경로**:
  - `server/src/main/java/com/ssafy/simai/domain/setup/SetupWaitReason.java` (사유 고르기, 순수 함수)
  - `server/src/main/java/com/ssafy/simai/domain/setup/SetupWaitReasonReader.java` (판단 재료 DB 읽기)
  - `server/src/main/java/com/ssafy/simai/domain/setup/SetupService.java` (`getInSite` 가 `wait_reason` 을 싣는다)
  - `front/apps/admin/src/features/setup/types.ts` (`setupWaitReasonSchema`), `setup.ts` (`deriveWaitReasonView`), `SetupPanels.tsx` (`WaitReasonNotice`)
- **테스트 경로**:
  - `server/src/test/java/com/ssafy/simai/domain/setup/SetupWaitReasonTest.java` (단위)
  - `server/src/test/java/com/ssafy/simai/domain/setup/SetupSessionIntegrationTest.java` (`세팅_조회는_기다리는_이유를_있는_상태에서만_고른다`)
  - `front/apps/admin/src/__tests__/features/setup/setup.test.ts`, `SetupPanels.test.tsx`

## 1. 배경

13차 세션에서 세팅 화면은 10분 넘게 "0%"만 보였다. 실제로는 워커 접수 문이 Tier 0 지연(예산 600 ms 초과)으로 `site_setup` 대여를
멈췄는데 화면에는 이유가 없었다. 혼잡할 때 배경 수집이 덜 돼(cam_C) `BACKGROUND_INCOMPLETE` 로 끝난 것도 화면에서는 알 수 없었다.
지연 차단 자체는 S15P31A101-566 이 고쳤다(`site_setup` 은 기본으로 지연 예산 초과만으로는 막히지 않음). 이 티켓은 "왜 기다리는지"를 보이는 일이다.

## 2. 응답 형식

`GET /api/setup-sessions/{id}`(생성·취소·재시도 응답도 같은 조회)에 `wait_reason` 을 더했다. 정상 진행·끝남·구역 단계 이후면 `null`.
REST 응답이라 `contracts/` 스키마는 없다(기존 세팅 세션 응답도 계약 밖). 필드 추가뿐이다.

| 필드 | 뜻 |
|---|---|
| `code` | 아래 코드 |
| `since_ms` | 기다리기 시작한 시각(UTC ms) |
| `waited_s` | 서버가 계산한 대기 초(화면은 `since_ms` 로 매초 다시 센다) |
| `basis` | `WORKER_PAUSED_TIER0` 근거: `WORKER_RELEASE`(워커가 직접 알림) · `TIER0_HEALTH`(서버 추정), 그 밖에는 null |
| `detail` | 워커 반납 메시지 또는 앞 시도 오류 메시지(원문), 없으면 null |
| `tier0_lag_ms` | 최근 15 s Tier 0 health `pipeline_lag_ms` 최댓값. 관측 참고값이며 판단 근거가 아니다 |
| `worker_last_seen_ms` | `site_setup` 워커의 마지막 대여 요청 시각 |
| `other_job_type` | 워커가 지금 처리 중인 다른 작업 유형 |
| `previous_error_code` | 재시도 대기일 때 앞 시도 오류 코드(예: `BACKGROUND_INCOMPLETE`) |
| `cameras` | `BACKGROUND_INCOMPLETE` 일 때 덜 모인 카메라 `[{camera_id, collection_pct}]` |

## 3. 코드와 고르는 순서

재료는 모두 이미 서버에 있는 상태다: `analysis_job`(상태·만든 시각·`error`), `job_lease`(유효 대여·마지막 움직임),
`worker`(`job_types` 에 `site_setup`, `revoked_at`, `last_seen_at`), `system_health`(최근 15 s, `received_at` 기준),
`setup_session`(`status`, `progress_lease_id`, `background_collection_pct`).

작업이 대기열(`QUEUED`)에 있을 때, 앞에서 걸리는 것으로 끝:

1. `NO_WORKER`: `site_setup` 을 처리하는 폐기되지 않은 워커가 없다.
2. `WORKER_BUSY_OTHER_JOB`: 그 워커가 다른 작업을 유효하게 대여 중이다(워커는 한 번에 하나).
3. `WAITING_FOR_WORKER`: 워커가 45 s 안에 대여를 물었다(long-poll 상한 30 s + 여유). 곧 가져간다.
4. `WORKER_PAUSED_TIER0`, `basis=WORKER_RELEASE`: 작업 `error.code` 가 `TIER0_PRIORITY`(워커가 받고 시작 전에 Tier 0 우선으로 반납, `runner._defer`). `detail` 에 접수 문 이유 원문.
5. `WORKER_PAUSED_TIER0`, `basis=TIER0_HEALTH`: 워커가 묻지 않고, 최근 Tier 0 health 에 지연 모름(`pipeline_lag_ms` 없음)이 있다. 접수 문은 지연을 모르면 모든 유형을 막는다(`LAG_UNKNOWN`).
6. `WORKER_NOT_POLLING`: 워커가 묻지 않는데 근거가 없다(워커 꺼짐·연결 끊김·표시 파일 이상 등). 마지막 연결 시각을 함께 보인다.

`since` 는 작업을 넣은 시각과 이 작업 대여의 마지막 움직임(대여·하트비트·제출) 중 늦은 것. 반납이 아닌 오류가 남아 있으면
(재시도 대기) `previous_error_code` 를 싣는다.

작업이 대여(`LEASED`)돼 유효 대여가 있을 때:

- `WORKER_STARTING`: 이 대여가 아직 세팅 하트비트를 보내지 않았다(`progress_lease_id` ≠ 유효 대여). 앞 대여의 수집률을 이 대여 것으로 보이지 않는다. 접수 문 재확인(`_hold`) 중이면 이 상태로 보이다가, 반납되면 4 로 바뀐다.
- `BACKGROUND_INCOMPLETE`: 이 대여의 하트비트가 `COLLECTING_BACKGROUND` 이고 100% 미만 카메라가 있다. `since` 는 대여 시각.
- 그 밖에는 null(정상 진행).

세션이 `READY_FOR_ZONES`·`BAKING_TEXTURE`·`COMPLETED`·`FAILED` 이면 재료를 읽지 않고 null. 실패 원인은 기존 `failure_code`·`failure_message` 로 보인다.

## 4. 명세에 없어 정한 수치와 근거

| 값 | 근거 |
|---|---|
| 워커 무응답 45 s | 계약 `job_lease_request.wait_ms` 상한 30 s(워커 기본 25 s, LIVE 5 s) + 응답·재요청 여유 15 s |
| Tier 0 health 창 15 s | health 1 Hz, 접수 문 표시 파일 5 s 창 + 안정 대기 10 s(`resume_after_s`) |
| health 조회 `reported_at` 하한 10분 | `system_health` 는 `reported_at`(Tier 0 시계) 기준 hypertable. 시계 차이를 넉넉히 두고 오래된 조각을 거른다 |

## 5. 버린 대안

- **지연 예산 초과(> 600 ms)로 `WORKER_PAUSED_TIER0` 추정**: S15P31A101-566 뒤로 `site_setup` 은 기본으로 지연 예산 초과만으로는 막히지 않는다
  (`SIMAI_WORKER_SETUP_LAG_BUDGET_MS` 를 정했을 때만 막힘, 그 값은 서버가 모른다). 그래서 높은 지연은 `tier0_lag_ms` 참고값으로만 싣고, 지연 모름만 근거로 쓴다.
- **워커 접수 상태를 새 메시지로 보내기**: 아래 6 참고. 이번 범위(이미 흐르는 정보만)에서 뺐다.

## 6. 아직 없는 정보(워커가 보내야 하는 것)

접수 문이 닫히면 워커는 대여 요청 자체를 보내지 않는다(`runner._lease_gated` 가 `pause_s` 쉬고 끝). 그래서 서버는 "워커가 묻지 않는다"까지만 알고,
그 까닭(표시 파일 오래됨·Tier 0 기동 중·GPU 선확보 실패·지연 모름·세팅 전용 예산 초과·LIVE 직후 linger)은 반납(4)이나 health 지연 모름(5)일 때만 안다.
기존 워커→서버 메시지(`job_lease_request`·`job_heartbeat`·`job_callback`)는 닫힌 동안 하나도 오가지 않아 작은 필드 추가로는 해결되지 않는다.

정확히 보이려면 워커가 접수 중지 동안에도 주기적으로(예: 30 s) 다음을 보내야 한다(새 I5 메시지 또는 엔드포인트, 계약 마이너 추가):

- `worker_id`, `sent_at`
- `admission`: `admit`(bool), `code`(`simai_ai.gpu.admission` 사유 코드: `marker_stale`·`not_running`·`reserve_not_ok`·`lag_unknown`·`over_budget`·`live_linger` 등), `reason`(원문), `closed_since`(ms), `lag_ms_max`, `setup_admitted`(bool, `admit_types` 에 `site_setup`)

서버는 이를 `worker` 행에 마지막 값으로 두고 3~6 대신 그 코드를 그대로 보이면 된다. 별도 티켓으로 넘긴다.

## 7. 잔여 위험

- `WORKER_NOT_POLLING` 은 워커 꺼짐과 근거 없는 접수 중지(표시 파일 이상 등)를 구별하지 못한다. 화면은 "GPU 서버 상태 확인"으로 안내한다.
- `TIER0_HEALTH` 추정은 Tier 0 health 가 EC2 로 오지 않으면(게이트웨이·Tier 0 끊김) 쓰지 못하고 `WORKER_NOT_POLLING` 으로 떨어진다.
- `BACKGROUND_INCOMPLETE` 의 카메라별 값은 하트비트의 수집률(표본 수/20, 최대 99)이다. 누적기의 "아직 안 드러난 픽셀 비율"(13차의 cam_C 15%)은 하트비트에 없어 보이지 않는다. 필요하면 `site_setup` progress 에 카메라별 `unrevealed_ratio` 를 더해야 한다(계약 마이너).
- 같은 현장 워커가 여럿이면 `worker_last_seen_ms` 는 그중 가장 늦은 값이다.
- 조회마다 쿼리 3개(작업·워커·health)를 더 돈다. 세팅 화면은 2 s 마다 조회하며, 끝난 세션은 읽지 않는다.
