# S15P31A101-624 세팅 LIVE 가드 정리와 날짜 작업 TBM 초안 시작

이 이슈는 2026-11-11 개인 감사(dev c73f1cce)에서 서버 통합 시험으로 재현한 결함 셋을 묶는다. 이 문서는 고친 순서대로 절을 더한다.

| 절 | 발견 | 상태 |
|---|---|---|
| 1 | G2b: 세팅 작업 대여 LIVE 가드가 SQL NULL 비교에 기댐 | 이 커밋 |
| 2 | M5: 작업 시작이 세팅 단계(READY_FOR_ZONES 전)를 보지 않음 | 이 커밋 |
| 3 | N1: 날짜 작업 TBM AI 양식 초안을 시작하는 곳이 없음 | 이 커밋(프런트, -615 비침범) |

## 1. 세팅 작업 대여 LIVE 가드 (G2b)

### 관련 명세
- 명세 3.2 5항: 작업 시작(LIVE) 뒤에는 백그라운드 후처리를 회수한다.
- AGENTS 작업 방식: LIVE 관제 시간에 배치 Job 을 돌리지 않는다.
- 현장 세팅은 작업 시작 전 절차다(`SetupService` 가 진행 중 작업이 있으면 세팅 시작을 409 `SETUP_BLOCKED_BY_LIVE_WORK` 로 막는다).

### 결함
- -621 이 `JobLeaseService.tryLease` 에 넣은 조건은 `NOT (job_type = 'site_setup' AND input ->> 'mode' = 'SUPPLEMENT_REBAKE' AND EXISTS(IN_PROGRESS))` 이었다.
- 새 모델 세팅은 서버가 입력에 `mode` 를 싣지 않는다(`SetupService`: REUSE 일 때만 `mode=REUSE_SCENE`). 그래서 비교가 NULL 이 되고, `NOT(NULL)` 도 NULL 이라 WHERE 에서 행이 빠졌다.
- 결과적으로 동작이 의도와 달랐다.

  | 입력 mode | 작업 진행 중일 때 | 비고 |
  |---|---|---|
  | 없음 | 대여 안 됨 | 우연 |
  | `REUSE_SCENE` | **대여됨** | 결함 |
  | `SUPPLEMENT_REBAKE` | 대여 안 됨 | 의도 |

- 대여가 끝나 다시 대기 중인 세팅(재시도 가능한 실패·대여 만료)은 이 경로로 LIVE 중에 다시 GPU 단계를 돈다.
- 재현: 감사 기록 `2026-11-11-open2-c73f1cce` 10절(재시도 대기 세팅이 LIVE 중 새 모델 204, REUSE 200).

### 바꾼 것
| 위치 | 내용 |
|---|---|
| `JobLeaseService.tryLease` | 조건을 `NOT (job_type = 'site_setup' AND EXISTS(같은 현장 IN_PROGRESS))` 로 바꿨다. mode 를 보지 않으므로 NULL 에 기대지 않는다 |
| `SetupSupplementIntegrationTest` | 재시도 대기 세팅이 작업 진행 중이면 mode 없음·`FULL_SETUP`·`REUSE_SCENE` 모두 대여되지 않고(204), 작업이 끝나면 같은 작업이 다음 시도로 대여됨. 기존 다시 굽기 시험은 그대로 통과 |

### 정한 것과 근거
- **모드를 나누지 않고 세팅 전체를 막는다.**
  - 명세상 세팅은 모두 작업 시작 전 절차다. 세팅 시작 자체가 LIVE 중 409 이므로, LIVE 중 대기 세팅이 생기는 경우는 대기 중에 작업이 시작된 경우뿐이다(재시도 대기, 다시 굽기 대기).
  - 모드 목록을 적으면 새 모드가 생길 때마다 빠질 수 있다.
- **이미 대여된 세팅은 이 조건과 무관하다.** 하트비트의 `live_work_active` 신호로 회수한다(-597·-621 회수 범위). READY 전 단계는 회수 대상이 아니다(M5 에서 다룬다).
- **대기 중인 세팅은 작업이 끝나면 대여된다.** 작업 대기 마감(deadline)은 원래 없다.

### 버린 대안
- **`coalesce(input ->> 'mode', 'FULL_SETUP') IN (…)` 로 모드를 명시**: 같은 효과다. 그러나 새 모드가 생기면 빠질 수 있고, 모든 모드를 막는 것이 명세와 같다.
- **재시도 대기 세팅을 작업 시작 때 실패로 끝내기**: 세션을 다시 만들어야 하고, 작업 종료 뒤 이어서 할 기회를 잃는다.

### 잔여 위험
- **세팅 화면의 대기 사유.** 작업 중 대기하는 세팅은 세팅 화면에 "워커 대기"로 보일 수 있다(`wait_reason`). 사유를 "진행 중 작업"으로 따로 보이는 것은 M5 와 함께 본다.
- **M5 가 남아 있다.** READY 전에 작업을 시작할 수 있는 한, 이미 대여된 세팅의 READY 전 GPU 단계는 LIVE 중에도 돈다. M5 서버 가드로 원인을 막는다.

### 배포 순서
제약이 없다. 서버 쿼리만 바뀌고 계약·마이그레이션은 없다.

## 2. 세팅이 미리보기 맵 전이면 작업 시작을 막는다 (M5)

### 관련 명세
명세 3.2 5항은 작업 시작(LIVE)을 누르면 백그라운드 후처리를 회수하고, **빠른 미리보기 맵(`preview_scene`)으로 먼저 관제를 시작**한다고 정한다. 미리보기 맵은 세팅이 READY_FOR_ZONES 에 닿아야 생긴다.

### 결함
- `WorkUnitRepository.startSession` 은 PLANNED 와 다른 IN_PROGRESS 작업만 봤다. 그래서 세팅이 `COLLECTING_BACKGROUND` 인데도 작업이 시작됐다(감사 재현).
- READY 전 단계(배경 수집·보정·MapAnything·미리보기)는 회수 대상이 아니라(-597·-621 은 굽기 쪽만 회수) LIVE 중에도 GPU 를 쓴다.
- 그 세팅의 새 보정은 작업이 끝날 때까지 Tier 0 에 발행되지 않는다. `ConfigBundleService` 가 LIVE 중 세팅 보정 묶음을 건너뛰고, 작업 종료 묶음이 싣는다.
- 관제 화면 Scene 도 미리보기가 없어 이전 맵이다.

### 바꾼 것
| 위치 | 내용 |
|---|---|
| `ErrorCode.Work.WORK_SETUP_NOT_READY` | 409. "현장 세팅이 미리보기 맵(구역 단계) 전이라 작업을 시작할 수 없습니다" |
| `WorkUnitRepository.startSession` | 같은 현장의 `setup_session.status` 가 `COLLECTING_BACKGROUND`·`INITIAL_POSE_AND_WORLD`·`JOINT_BA_AND_VERIFY`·`PREVIEW_AND_VLM_DRAFT` 중 하나면 409 다. 날짜·서명 검사 뒤, 진행 중 작업 검사 앞에 둔다 |
| 프런트 `features/work/api.ts`, `preflight/preflightErrors.ts` | `WORK_ERROR.SETUP_NOT_READY` 와 안내 "현장 세팅이 아직 진행 중이에요. 미리보기 맵이 나온 뒤 시작해 주세요" |
| `SetupSupplementIntegrationTest` | 세팅이 COLLECTING_BACKGROUND 면 409·작업 PLANNED 유지, READY_FOR_ZONES 뒤에는 200 |
| `preflight.test.ts` | 새 코드의 문구 |

### 정한 것과 근거
- **기준을 READY_FOR_ZONES 로 둔다.** "세팅 COMPLETED 전 전부"로 넓히면 명세 3.2 5항의 "굽는 중 미리보기 맵으로 관제 시작"(부록 E 장면 1, -597)을 막는다.
- **현장 단위로 본다.** 세팅은 현장당 하나만 진행된다(`setup_one_running_per_site`). 작업과 직접 연결되지 않은 세팅도 같은 카메라·GPU 를 쓴다.
- **FAILED 세션은 막지 않는다.** 실패한 세팅은 더 진행되지 않는다.
- **REUSE 도 같은 단계 이름을 쓴다.** 보정을 다시 맞추는 동안(READY 전)은 막고, READY 뒤(곧바로 COMPLETED)는 허용한다.
- 오류 코드 이름은 기존 `WORK_*` 409 관례(`WORK_NOT_TODAY`·`WORK_NO_SIGNATURE`)를 따른다.

### 버린 대안
- **작업 시작 때 READY 전 세팅을 취소**: 관리자가 의도하지 않은 세팅 실패가 생기고, 다시 세팅해야 한다.
- **READY 전 단계도 회수 대상에 넣기**: 회수되면 미리보기 맵·보정이 없어 관제할 맵이 없다.

### 잔여 위험
- **화면 쪽 선제 차단.** 프런트 작업 시작 화면이 세팅 단계를 보고 버튼을 미리 막는지는 이 변경 범위 밖이다(지금은 서버 409 안내로 알린다).
- **N1 은 별도다.** 세팅이 READY 를 지나도 날짜 작업 TBM AI 초안은 시작되지 않는다(3절).

### 배포 순서
제약이 없다. 이전 판 프런트는 새 코드를 몰라 일반 문구("시작하지 못했어요")로 보인다. 계약·마이그레이션은 없다.

## 3. 날짜 작업 TBM AI 초안을 사람이 요청한다 (N1)

### 결함
- AI 양식 초안 API(`POST /api/reports/tbm/{id}/form-draft`, `TbmFormAiService.startDraft`)는 있다. 그러나 부르는 곳이 없었다.
  - 서버의 `startDraftForWork`·`tryStartDraftForWork` 는 세팅 완료(C17, -615)가 부르도록 만들어 뒀고, -615 는 아직 없다.
  - 프런트는 이 API 를 부르지 않았다.
- 그래서 날짜 작업 보고서는 `draft_status=null` 로 남는다. 위험·대책·점검 칸은 줄무늬(초안 전)이고 편집 창은 작업내용만 고칠 수 있다.
- 서명·작업 시작·제출은 초안 없이 된다(감사 재현: 세팅을 COMPLETED 까지 진행해도 초안 시작 0회).

### 정한 것: -615 를 건드리지 않는 범위
- **서버 쪽 호출 지점(세팅 결과 COMPLETED·READY 때 `tryStartDraftForWork`)은 만들지 않는다.** -615 의 C17 이 "완료 커밋 뒤 초안 시작"을 맡는다(-615 댓글). 여기서 따로 부르면 호출 지점이 둘이 되고, -615 가 세팅 완료 시점을 옮길 때 서로 엇갈린다.
- **프런트에서 사람이 요청하게 한다.** 작업 전 확인(세팅 뒤 단계) 보고서 종이 위에 'AI 초안 만들기' 단추를 둔다.
  - 보이는 때: 초안이 아직 없거나(`draft_status` null) 실패(FAILED)했을 때만. 실패면 'AI 초안 다시 만들기'다.
  - 작업내용이 없으면 끈다(서버 409 `REPORT_FORM_NO_TASKS` 와 같은 조건).
  - 요청하면 보고서를 다시 받아 GENERATING 을 보이고, 이후는 기존 `useWorkReport` 의 2초 다시 받기가 채워지는 때를 잡는다.
  - -615 가 들어와 서버가 자동으로 시작하면 GENERATING·SUCCEEDED 라 단추는 보이지 않는다. 그 뒤에도 실패 재시도용으로 남는다.
- **보고서 단계(세팅 전)에는 두지 않는다.** 초안은 세팅이 만든 구역·규칙을 근거로 쓴다(`buildDraftRequest`).

### 바꾼 것
| 위치 | 내용 |
|---|---|
| `features/work/api.ts` | `startReportDraft()`, `WORK_ERROR.REPORT_IN_PROGRESS`·`REPORT_FORM_NO_TASKS` |
| `features/work/useWorkReport.ts` | `useStartReportDraft()`: 요청 뒤(409 포함) 그 작업의 보고서를 다시 받는다 |
| `features/work/report/ReportPanel.tsx`, `stageProps.ts` | `draftable` 이 켜지면 단추와 오류 안내 |
| `features/work/preflight/PreflightStage.tsx` | 작업 전 확인 보고서에 `draftable` |
| `features/work/preflight/preflightErrors.ts` | `draftErrorMessage()` |
| `mocks/work.ts` | `POST …/form-draft` mock(생성 중 409, 작업내용 없음 409, 잠김 409). 요청 시각부터 기존 draftDelayMs 진행 |
| 시험 | `ReportPanel.test.tsx` 4건(요청 → GENERATING → 채움, 작업내용 없음·비대상 화면, 실패 뒤 다시, 409 안내), `preflight.test.ts` |

### 버린 대안
- **서버가 세팅 결과 때 `tryStartDraftForWork` 호출**: 화면 변경 없이 되지만 -615 와 호출 지점이 겹친다(위).
- **제출 때 초안 미완료 경고만**: 초안을 만들 길이 없으면 경고만으로는 빈 TBM 을 막지 못한다.

### 잔여 위험
- **-615 머지 뒤 겹침 확인.** 서버 자동 시작과 단추가 같은 순간에 겹치면 둘째 요청은 409 `REPORT_IN_PROGRESS` 다. 화면은 "이미 만들고 있어요"를 보이고 다시 받는다(무해).
- **화면 시험만 있다.** 서버 쪽 `startDraft` 는 -616 의 기존 시험(`TbmFormAiIntegrationTest`)이 다룬다. 실제 LLM 은 쓰지 않았다.
