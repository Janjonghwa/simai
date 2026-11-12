# S15P31A101-625 대기 중 세팅 굽기 회수, 초안 경고, 세팅 대기 안내

관련 명세: 3.2 5항(작업 시작은 미리보기 맵으로 관제하고 정식 굽기만 회수), 8.3(TBM). 선행: -597(워커 굽기 회수), -607(맵 다시 굽기), -621(다시 굽기 회수), -624(READY 전 작업 시작 409, LIVE 중 세팅 대여 보류). Jira: <https://ssafy.atlassian.net/browse/S15P31A101-625>.

2026-11-12 개인 감사(dev 996c6088)에서 -624 머지 뒤 남은 셋을 고친다.

## 1. 대기 중 세팅 굽기 회수 (S8)

### 결함
- 굽기(BAKING_TEXTURE) 중 재시도 가능한 실패(Pod 정지·대여 만료)가 나면 작업은 QUEUED·attempt 2 가 되고, 세션은 BAKING_TEXTURE 로 남는다.
- 이때 작업을 시작할 수 있다(-624 가드는 READY 전 4단계만 본다). LIVE 중에는 대여가 보류된다(-624).
- 작업이 끝나면 attempt 2 가 대여된다. 워커에는 재개가 없어 처음부터 돈다. 새 대여는 단계 역행이 허용돼(`SetupService` 의 같은 대여 안 역행 검사) 세션이 COLLECTING_BACKGROUND 로 돌아간다.
- 결과: 같은 현장 다음 작업 시작이 409 `WORK_SETUP_NOT_READY` 이고, 요청하지 않은 재세팅이 GPU 를 쓴다(감사에서 서버 통합 시험으로 재현).
- 원인: -597 회수는 대여 중인 작업의 하트비트 응답 신호로만 된다. 대기열 작업에는 신호를 받을 대여가 없다.

### 바꾼 것
| 위치 | 내용 |
|---|---|
| `work/WorkStarted` (새 이벤트) | 작업 시작 트랜잭션 안에서 `WorkSessionService.start` 가 보낸다(`startSession` 직후, 구성 묶음 발행 전) |
| `SetupService.onWorkStarted` | 같은 현장 세션 중 READY_FOR_ZONES·BAKING_TEXTURE 이고 세팅 작업이 QUEUED 인 것을 회수한다 |
| `SetupService.onJobStateChanged` | site_setup 작업이 다시 QUEUED 가 됐는데(재시도 가능한 실패·대여 만료·반납, 모두 `JobStateChanged` 를 같은 트랜잭션에서 보냄) 현장에 IN_PROGRESS 작업이 있으면 회수한다. 다시 굽기 작업은 제외 |
| `SetupService.reclaimQueuedBake` | 작업 행 → 세션 행 순서로 잠근다(하트비트·제출과 같은 순서). 작업이 QUEUED 이고 세션이 READY_FOR_ZONES·BAKING_TEXTURE·정식 Scene 없음일 때만, 세션을 `COMPLETED` + `bake_skipped=LIVE_WORK_STARTED`(재사용 모드는 null)로 먼저 닫고 작업을 취소한다 |
| `SetupSupplementIntegrationTest` | 3건: 작업 시작 때 회수 → 작업 뒤 대여 0·다음 작업 시작 200, LIVE 중 재시도 실패로 회수, LIVE 중 대여 만료로 회수. 수정 전 코드에서는 셋 다 실패함을 확인 |

### 정한 것과 근거
- **끝 상태를 워커 회수(-597)와 같게 한다.** V50·V58 의 `bake_skipped` 제약, 세팅 화면의 "정식 맵 없음(회수)" 표시, 다시 굽기(-607)의 회수 세션 처리를 그대로 쓴다. 계약·마이그레이션 변경이 없다.
- **세션을 먼저 닫고 작업을 취소한다.** 취소 알림(`JobFinished CANCELLED`)이 `onFinished` 에서 세션을 FAILED 로 바꾸지 않게 한다(관리자 취소와 같은 순서).
- **대여 중(LEASED) 작업은 건드리지 않는다.** 하트비트 신호로 워커가 회수한다(-597).
- **작업 뒤 자동으로 다시 굽지 않는다.** 감사 S8 이 바로 그 자동 재실행의 문제였다. 정식 맵이 필요하면 관리자가 다시 굽기(-607)를 요청한다.
- **보정 묶음을 여기서 따로 내지 않는다.** 작업 시작 묶음이 현재 보정을 싣는다.
- **work → setup 을 직접 부르지 않고 이벤트로 잇는다.** setup 이 이미 `WorkUnitRepository` 를 써서 직접 호출하면 두 패키지가 서로 부른다.

### 버린 대안
- **세션을 FAILED 로 닫기(관리자 취소 재사용)**: 관제에 쓰는 미리보기 맵·보정·구역의 세션이 실패로 보이고, -615·TBM 초안이 그 세션 구역을 쓰는 경로가 헷갈린다.
- **워커가 READY 뒤 굽기만 이어 하기**: 계약(재개 단계)·워커·산출물 재사용까지 바뀌어 시연 전 범위를 넘는다. 시연 뒤 과제로 남긴다.
- **READY 뒤라도 대기 굽기가 있으면 작업 시작을 막기**: 명세 3.2 5항과 충돌하고, Pod 가 멈추면 작업을 시작할 수 없다.

### 잔여 위험
- 작업 시작 트랜잭션이 커밋되기 전에 워커가 같은 작업을 대여하면(대여 가드는 커밋 전 IN_PROGRESS 를 못 본다) 회수가 그 작업을 건너뛴다(QUEUED 가 아님). 그 대여는 다음 하트비트 응답의 회수 신호로 워커가 회수한다(-597).
- 굽기 입력 사진 등록 전에 회수된 세션은 다시 굽기가 409 `SUPPLEMENT_REBAKE_BASE_MISSING` 이라 새 세팅이 필요하다.
- 같은 세션의 미완료 VLM 구역 초안 작업은 그대로 둔다.

## 2. 초안 없는 TBM 경고 (N1r)

- `workDay.draftWarning(report)`: 날짜 작업 양식(form)이 있고 잠기지 않았는데 `draft_status` 가 null·FAILED·GENERATING 이면 경고 문구를 돌려준다.
- 작업 전 확인 창(`PreflightConfirmDialog`)과 보고서 보내기 창(`RunStage` 의 `EndedView`)에 그 문구를 보인다. **막지 않는다.** 사람이 칸을 직접 채웠을 수 있고, 막으려면 서버 409 를 새로 둬야 한다.
- 근본 해결(세팅 완료 때 초안 자동 시작)은 -615 다.
- 시험: `workDay.test.ts`(상태별 문구), `PreflightConfirmDialog.test.tsx`(경고 표시·시작 가능), `RunStage.test.tsx`(null·FAILED·SUCCEEDED).

## 3. 세팅 대기 안내 (S9)

- `WORK_SETUP_NOT_READY` 는 현장 단위라 멈춘 세팅 하나가 모든 작업 시작을 막는다(site_setup 은 마감이 없어 워커가 없으면 QUEUED 로 남는다).
- 안내 문구에 "세팅이 멈췄다면 현장 세팅 단계에서 세팅을 취소한 뒤 다시 하세요"를 더했다. 세팅 단계로 가는 단추는 두지 않았다(범위 최소).
- 시험: `preflight.test.ts`.

## 배포 순서

제약이 없다. 서버만 먼저 나가도 회수는 동작하고, 이전 프런트는 경고·새 문구 없이 지금처럼 보인다. 계약·마이그레이션이 없다.
