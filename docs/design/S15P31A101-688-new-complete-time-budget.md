# S15P31A101-688 새 맵 세팅은 정밀 맵까지 기다려 완료하고, 시간 제한은 사람 대기·보정·굽기 몫으로 나눈다

관련 명세: 3.2(작업 시작 전 세팅, LIVE 굽기 회수), 4.3(현장 세팅 파이프라인), 4.5(Scene 계보), 8.1~8.2(세팅·구역), 12.5(I5 대여·
하트비트·실행 상한), 12.6(세팅 REST), 13.4(세팅 화면). 앞 문서 `S15P31A101-615-setup-complete.md`(세팅 완료 C17),
`-642`(작업 시작은 세팅 완료 뒤), `-686`(새 배경), `-690`(높이 확인), `-687`(카메라 확인을 맵 앞으로, 시간 배분 표), `-597`·`-625`(LIVE 굽기 회수).
계약 변경 없음(묶음 v7.52.0 그대로). DB 마이그레이션 없음.

## 문제

- 화면은 새 맵(NEW) 세팅에서 정밀 맵(FINAL)을 기다렸지만(`isSceneMapComplete`), 서버 완료(`SetupCompletionService`)는 미리보기 단계
  (READY_FOR_ZONES, 85%)부터 받았다. 85% 에서 완료·작업 시작하면 남은 굽기가 LIVE_WORK_STARTED 로 회수되고(대여 중 -597, 대기열 -625),
  진행 중 작업이 있으면 정밀 Scene 승격도 하지 않아 시연 맵이 미리보기 품질로 남는다. Codex·Argon 리뷰가 "85% 완료 허용 금지"로 같았다.
- 세팅 작업 실행 상한 900 s(`SetupService.SETUP_TIMEOUT_MS`) 하나에 사람 대기(배경 촬영 -686, 높이 확인 최대 180 s -690, 카메라 확인
  최대 600 s -687)와 보정·굽기가 함께 들어 있었다. 15차 NEW 세팅 1 은 880 s 에 끝났다(걷지 않아 INCOMPLETE). -687 은 맵 몫 420 s 를
  관측 한 번·추정으로 잡아, 굽기가 길면 카메라 확인을 통과한 뒤 TIMEOUT 이 나 통과한 확인이 버려질 수 있었다.

사용자 원칙: NEW 는 서버도 정밀 맵을 기다린다(미리보기로 완료 금지). 시간 제한은 사람 대기·보정·굽기로 나눠 늘린다. 앞에서 통과한
확인을 시간 때문에 뒤에서 무효로 만들지 않는다.

## 1. 완료 정본 판정(`SetupCompletionGate`)

완료 POST 와 세션 조회가 같은 함수를 쓴다. 세션 쪽 판정 순서와 거부 코드(모두 409):

| 순서 | 조건 | 코드 |
|---|---|---|
| 1 | 단계가 READY_FOR_ZONES·BAKING_TEXTURE·COMPLETED 가 아님(실패 포함) | `SETUP_NOT_READY_TO_COMPLETE` |
| 2 | REUSE 인데 World 다시 맞추기(world_anchor) 채택 아님 | `SETUP_NOT_READY_TO_COMPLETE` |
| 3 | 카메라 확인 PASSED 이고 그 보정 = 세션 보정 아님(-646) | `SETUP_CAMERA_CHECK_NOT_PASSED` |
| 4 | NEW 인데 정밀 맵(`scene_version`) 없음, 굽는 중 | `SETUP_FINAL_MAP_NOT_READY`(새) |
| 4 | NEW 인데 정밀 맵 없음, 작업 시작으로 굽기 회수(bake_skipped) | `SETUP_FINAL_MAP_SKIPPED`(새) |

- 정밀 맵은 정식 굽기 결과 또는 다시 굽기 APPLIED 가 옮긴 맵(`setup_session.scene_version`)이다. 완료는 그 맵이 이 세션 등록일 때만 켜고,
  미리보기로 대신하지 않는다(`finalScene`). 모드를 모르는 행은 NEW 로 본다.
- 작업 쪽 조건(걸린 세션·오늘·PLANNED·관제 중)과 구역 적용 조건은 그대로 `SetupCompletionService` 가 판정 뒤에 본다(순서 유지).
- 세션 조회에 `complete_blocked_reason`(위 코드 또는 null)을 싣는다(계약 스키마가 없는 REST 응답이라 계약 변경 없음).
- 작업 시작(`WorkUnitRepository.startSession`)은 지금처럼 그 작업의 `setup_completed_at` 을 본다. 완료가 정밀 맵을 요구하므로 "미리보기로
  관제 시작"이 생기지 않는다. 그 작업의 굽기가 남은 채 시작하는 일이 없어 시작 때 회수할 것도 없다. 같은 현장의 다른 세팅이 굽는 중에
  세팅을 마친 다른 작업이 시작되면 그 굽기는 지금처럼 회수되고(LIVE 우선, -597·-625), 그 세션은 `SETUP_FINAL_MAP_SKIPPED` 로 완료할 수
  없다(작업 뒤 다시 세팅).
- 화면(`setup.ts`): `completeBlockedReason` 은 서버 값이 있으면 그대로, 없으면(이전 서버) 같은 순서로 계산한다. `isSceneMapComplete`
  (2단계 '다음')는 맵 조건(`completeMapBlockedReason`)과 같아졌다: NEW 는 정밀 맵, REUSE 는 READY 이후 + 채택. 회수된 세팅은 더 이상
  맵 완성으로 보지 않는다. `canCompleteSetup`(구역 단계 마지막 단추)은 READY 단계만 보던 것을 `completeBlockedReason === null` 로 바꿨다.
  오류 문구(`SETUP_COMPLETE_ERROR_TEXT`)는 서버 ErrorCode 문구와 같다. 개발 서버 목업도 같은 판정을 쓴다.

## 2. 시간 예산: 사람 대기 몫과 기계 몫을 따로 잡아 더한다

실행 상한 = 몫의 합 **1,800 s**(`SETUP_TIMEOUT_MS`, 워커 `SETUP_BUDGET_*`):

| 몫 | 값 | 종류 | 근거 |
|---|---|---|---|
| (a) 배경 촬영 | 240 s | 사람(+촬영 ~12~20 s) | 기존 `SIMAI_SETUP_BACKGROUND_WAIT_S`(-686). 사람이 화면 밖으로 나가 그 카메라만 다시 찍을 여유 |
| (b~c) 초기 복원·World | 120 s | 기계 | 15차 실측: 배경 뒤 초기 복원~보정 등록 약 60 s(초기 복원 약 40 s). 약 3배 |
| (d) 높이 확인 대기 | 180 s | 사람 | -690 그대로. 줄자로 한 대를 다시 재고 입력하는 1~2분 |
| (e~f) Stage A·게이트·보정 등록 | 60 s | 기계 | 15차 약 20 s 의 3배 |
| (f') 카메라 확인 | 600 s | 사람(걷기 포함) | -687 그대로. 걷기 시작 대기 + 시도(최대 150 s) 몇 번 |
| (g~h) 맵 | 600 s | 기계 | 미리보기 약 18 s(15차 07:48:20→07:48:38) + 정밀 굽기 약 300 s(15차 세팅 2 는 208 s 째 취소될 때까지 돌았다, Pod 추정 100~155 s·노트북 245 s, -646 추정 5분) + 굽기 실패 시 폴백 상한 120 s + GLB 업로드·등록·결과 제출 여유 |

- 워커는 사람 대기를 각자 상한과 "남은 시간 − 뒤 단계 몫" 중 짧은 쪽까지만 기다린다(기존 식 그대로, 몫만 바꿈):
  - 높이 확인 대기 = min(180, 남은 시간 − 1,260), 1,260 = Stage A 60 + 카메라 확인 600 + 맵 600(옛 540 s). 높이 대기가 카메라 확인 시간을
    먹지 않는다.
  - 카메라 확인 단계 = min(600, 남은 시간 − 600)(NEW 맵 몫, 옛 420 s). REUSE 는 결과 제출 몫 30 s 그대로.
- 그래서 사람이 높이 확인을 끝까지 기다리고 카메라 확인 걷기 시작을 마감 직전에 눌러도, 통과한 뒤 맵에 600 s 이상이 남는다(시험
  `test_human_waits_never_eat_the_map_share_of_the_execution_limit`). 기계 몫이 넘쳐도(예: MapAnything 이 오래 걸림) 먼저 줄어드는 것은
  사람 대기 몫이다: 높이 확인은 묻지 않고(TIMED_OUT, 게이트 높이 상한 그대로), 카메라 확인 대기가 짧아지며(45 s 미만이면 맵 없이 실패),
  맵 몫은 지킨다.
- 보통 타임라인(15차, 높이 확인 없음): 배경 ~20 s + 초기 복원~등록 ~60 s → 카메라 확인 대기 600 s 창(걷기 30~45 s) → 미리보기·굽기
  (~5분). 전체 약 8~10분이고, 1,800 s 는 사람이 모든 대기를 끝까지 쓴 최악에서만 닿는다.
- heartbeat·취소·LIVE 회수는 그대로다. 죽은 워커는 대여 유지 시간(하트비트)이 따로 잡으므로, 실행 상한은 "살아 있지만 멈춘" 작업의
  마지막 울타리다. 관리자는 언제든 「현재 시도 중단」(세션 취소)으로 작업을 끊는다.
- 워커는 실행 상한이 예산 합보다 30 s 넘게 짧으면(옛 EC2 900 s) 경고 로그를 남긴다. 그때도 맵 몫은 지키고 사람 대기 몫이 줄어든다.
- 계약 예시(`job_request.site_setup*.json`)의 `timeout_ms` 는 이미 1,800,000 이라 고치지 않았다.

## 3. 굽기 중 시간 초과 뒤 "맵만 다시"(검토, 넘긴 일)

- 지금 굽기 중 TIMED_OUT 이면 세션은 FAILED 이고, 다시 하려면 세팅 재시도(새 세션: 배경·MapAnything·Stage A·카메라 확인부터)다.
- 기존 다시 굽기(-607 SUPPLEMENT_REBAKE)를 쓰려면 (1) FAILED 세션도 보정·미리보기·통과한 카메라 확인이 있으면 다시 굽기를 받도록 세션
  상태 규칙을 바꾸고, (2) 보강 촬영 파일 없이도 정식 굽기만 하도록 워커 다시 굽기 모드를 넓히고(지금은 보강이 적용되지 않으면 새 Scene 을
  올리지 않는다, `supplement_media` 최소 1개), (3) 화면에 「맵만 다시」 단추를 둬야 한다. 계약(site_setup 입력·결과)·워커·서버·화면을 함께
  바꾸는 일이라 이 티켓에서는 하지 않았다. 대신 맵 몫을 600 s 로 따로 잡아 굽기 시간 초과 자체를 거의 없앴다(굽기 추정의 약 2배).

## 4. -687·-690 시간 배분 값 정리

| 값 | 전 | 후 | 위치 |
|---|---|---|---|
| 실행 상한 | 900 s | 1,800 s | `SetupService.SETUP_TIMEOUT_MS` |
| NEW 맵 몫 | 420 s | 600 s | `camera_check_reserve_s` = `SETUP_BUDGET_MAP_S` |
| 높이 확인 뒤 단계 몫 | 540 s | 1,260 s | `height_review_reserve_s` = Stage A + 카메라 확인 + 맵 |
| 카메라 확인 대기 상한 | 600 s | 600 s | `camera_check_wait_s` = `SETUP_BUDGET_CAMERA_CHECK_S` |
| 높이 확인 대기 상한 | 180 s | 180 s | `height_review_wait_s` = `SETUP_BUDGET_HEIGHT_REVIEW_S` |
| REUSE 결과 몫·시도 상한·최소·채널 | 30·150·45·15 s | 그대로 | -687 표 |

`S15P31A101-687-camera-check-before-map.md` 의 "맵 몫 420 s"·"높이 확인 뒤 단계 몫 540 s"와 `-690` 의 "뒤 단계 몫 360 s(→540)"는 이 표가
대신한다.

## 버린 대안

- **하트비트 기반 deadline 연장(사람 대기 시간만큼 서버 실행 상한을 늘림)**: 워커가 대기 시간을 알리는 계약 필드와 대여별 누적 칼럼(DB)이
  필요하고, 서버와 워커가 따로 잰 대기 시간이 어긋나면 서버가 먼저 TIMED_OUT 을 내 통과한 굽기를 끊는다(사용자가 가장 싫어하는 경우).
  사람 대기 몫마다 상한이 이미 있으므로 합을 실행 상한으로 두는 편이 두 쪽이 늘 같은 값을 본다.
- **대여 연장 요청(워커가 남은 시간을 더 달라고 함)**: 같은 이유로 새 계약·서버 판단이 늘고, 언제 얼마를 줄지 다시 정해야 한다.
- **실행 상한만 크게(예: 3,600 s) 늘리기**: 사람 대기 몫이 뒤 몫을 깎는 식이 그대로라 원칙("나눠 늘린다")과 맞지 않고, 멈춘 작업을 오래 둔다.
- **완료는 미리보기로 허용하고 작업 시작만 정밀 맵을 기다림**: 완료가 미리보기를 관제 맵으로 켜고 구성 묶음을 발행한 뒤라 정밀 맵 승격
  경로(완료 뒤 도착)가 다시 필요하고, 화면·서버 판정이 둘로 갈린다.
- **새 오류 코드 없이 SETUP_NOT_READY_TO_COMPLETE 하나로**: 굽는 중(기다리면 됨)과 회수(다시 세팅해야 함)를 화면이 구분하지 못한다.
- **회수된 세팅도 미리보기로 완료 허용**: 사용자 원칙(NEW 는 정밀 맵) 위반. 회수는 다른 작업이 LIVE 로 GPU 를 쓰는 드문 경우다.

## 잔여 위험

- 맵 몫 600 s 는 15차 굽기 관측(208 s 째 취소)과 추정에 기댄다. 16차에 실제 굽기 시간을 재서 맞는지 본다.
- 실행 상한이 1,800 s 라 멈춘 채 살아 있는 세팅 작업은 최대 30분 GPU 워커를 쥔다(같은 현장 새 세팅·다시 굽기도 그동안 막힘). 관리자 취소로
  끊을 수 있다.
- 배포 순서: **EC2·화면 먼저, 워커 나중**. 새 워커 + 옛 EC2(900 s)면 높이 확인을 묻지 않고(TIMED_OUT) 카메라 확인 대기가 약 300 s 로 준다
  (맵 몫은 지킨다). 옛 워커 + 새 EC2 는 옛 몫(420·540 s)으로 돌지만 상한이 넉넉해 대기 상한이 그대로 쓰인다.
- 배포 전에 미리보기로 완료한 세션(굽기가 남은)은 지금처럼 완료 뒤 도착한 정밀 맵을 올린다(`promoteSetupSceneAfterCommit` 유지).
- 화면은 2단계 '다음'이 꺼진 이유로 회수된 세팅에도 "3D 맵을 다 만들면 넘어갈 수 있어요"를 보인다(단추 문구는 -691 범위). 맵 단계 표시
  (`deriveSceneTierStatus`)가 "이 세팅으로는 완료할 수 없습니다"를 알린다.

## 넘긴 일

- 16차 GPU 세션: 새 맵 세팅을 정밀 맵까지 기다려 완료되는지(미리보기에서 완료 단추가 꺼져 있는지), 실제 굽기 시간(맵 몫 600 s 확인),
  높이 확인·카메라 확인을 오래 기다린 뒤에도 굽기가 끝나는지.
- 굽기 중 시간 초과 뒤 "맵만 다시"(3절): 별도 티켓.
- 명세 3.2·4.3(완료는 정밀 맵 뒤, 미리보기로 관제 시작 없음, 실행 상한 1,800 s 의 몫)·12.6(`complete_blocked_reason`, 새 거부 코드) 반영은
  명세 정리 MR 에서. 현재 명세 3.2 6항은 "관리자가 구역을 편집·발행하는 동안 정밀 텍스처를 마쳐 Scene 을 교체"로 미리보기 단계 완료를 막지
  않으며, `-615` 문서의 "굽기 도중 완료 허용"과 이 티켓이 다르다(사용자 결정 우선).

## 코드·시험 위치

- 서버: `domain/setup/SetupCompletionGate.java`(새), `SetupCompletionService`(판정·정밀 맵만 켬), `SetupService`(`SETUP_TIMEOUT_MS`
  1,800,000, 세션 조회 `complete_blocked_reason`), `ErrorCode.Setup.SETUP_FINAL_MAP_NOT_READY`·`SETUP_FINAL_MAP_SKIPPED`,
  `WorkUnitRepository`(주석).
- 워커: `ai/services/worker/site_setup.py`(`SETUP_BUDGET_*`, `SETUP_TIME_BUDGET_S`, 기본값·경고 로그), `site_setup_stages.py`(주석).
- 화면: `features/setup/setup.ts`(`completeMapBlockedReason`·`completeBlockedReason`·`isSceneMapComplete`), `SetupCompleteCard.tsx`
  (`canCompleteSetup`), `setupErrorMessage.ts`, `types.ts`(`complete_blocked_reason`), 목업 `mocks/setup.ts`.
- 시험: 서버 `SetupCompletionGateTest`(새), `SetupCompleteIntegrationTest`(정밀 맵 전 거부·조회 이유·회수 세팅 거부, 기존 시험은 굽기 뒤
  완료), `SetupResultRegisterIntegrationTest`·`SetupReuseModeIntegrationTest`·`SetupSupplementIntegrationTest`(정밀 맵 뒤 완료, 회수는 다른
  작업 시작으로), 워커 `test_site_setup.py`(예산 합·사람 대기 뒤 맵 몫·옛 상한 축소), 화면 `setup.test.ts`·`SetupCompleteCard.test.tsx`·
  `mocks/setupWorkFlow.test.ts`.
