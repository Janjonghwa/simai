# S15P31A101-692 관제 시작·종료 상태 표시

-679(작업 시작 = 관제 시작) 뒤 2차 검토에서 나온 화면 문제를 고친다. 작업 시작 API 성공은 DB 상태(IN_PROGRESS)와 묶음 발행일 뿐이고,
Tier 0 가 이번 작업 묶음(`ppe_policy.work_id`)을 판정 경계에서 적용해야 경보가 켜진다. 화면은 그 차이를 보이지 않았다.

관련 명세: 3.2(구성 묶음·config_ack·원자적 전환), 6.2(Config Sync), 8.3(작업 시작·종료), 13장(실시간 관제), 부록 C.6(워치)·E(시연, A·B 워치 BOUND·연결).

## 1. "관제 중" 판정 근거

**쓰는 신호(모두 서버에 이미 있음, 계약·서버·DB 변경 없음)**

| 신호 | 출처 | 쓰임 |
|---|---|---|
| 묶음별 마지막 config_ack 상태(PENDING·APPLIED·REJECTED·MISMATCH)와 `ppe_policy.work_id` | `GET /api/config-bundles`(최근 20개, `ConfigBundleViews`) | 현장이 쓰는 묶음 = 가장 새로 APPLIED 된 묶음(`appliedBundle`). 그 `work_id` 가 진행 중 작업이면 관제 중 |
| `tier0_link`·`remote_state` | `/topic/health`(viewer_health) | Tier 0 끊김·관제 정지(CONTROL_HALTED)를 지연과 구분 |
| 작업 `actual_started_at`·`actual_ended_at` | `GET /api/work-units` | 묶음 목록에 이번 작업 묶음이 아직 없을 때 지연 기준 |

**고른 이유**: Tier 0 는 검증한 묶음을 판정 tick 경계(`ConfigSync.begin_tick`)에서만 활성으로 바꾸고 바로 그때 config_ack APPLIED 를 낸다.
-679 의 `JudgmentLoop._monitoring_on` 도 같은 tick 의 활성 묶음 `work_id` 로 판정을 켠다. 그래서 '이번 작업 묶음 APPLIED' 는
'판정이 그 묶음으로 돌기 시작했다' 와 같은 사건이다. 서버는 해시가 발행한 것과 같을 때만 APPLIED 로 보고(다르면 MISMATCH) 화면은
그 값만 쓴다. 갱신은 health 의 `config.applied_version` 변화 → `bindConfigCache` 가 묶음 목록을 다시 받고, 발행 뒤 60 s 는 3 s 주기로도 받는다.

버린 대안

| 안 | 버린 이유 |
|---|---|
| 작업 시작 응답에 묶음 버전을 싣고 그 버전만 본다 | 서버 변경이 필요하고, 시작 묶음이 같은 작업의 더 새 묶음(구역 발행)에 대체되면 시작 묶음은 APPLIED 가 오지 않는다. '현장이 쓰는 묶음의 work_id' 가 정확하다 |
| `tier0_health.config_version`(Tier 0 health 원문)으로 본다 | 계약상 객체 검사만 하는 원문이라 형식 보장이 약하고, ack 와 같은 사실이다 |
| world_state 수신·health 수신만으로 관제 중 | 추적은 작업과 상관없이 돈다(-679). 경보가 켜졌다는 근거가 아니다 |
| viewer_health 에 '관제 중' 필드 추가 | I1·뷰어 계약 변경이며 같은 사실이 이미 있다 |

## 2. 단계와 표시 (`features/monitor/monitoringPhase.ts`)

| 단계 | 조건 | 배지 |
|---|---|---|
| unknown | 작업·묶음 목록 중 하나라도 모름 | 「관제 상태 확인 중」(작업 전에는 아무것도 그리지 않음) |
| idle | 진행 중 작업 없음, 현장 묶음의 `work_id` 없음 | 「작업 시작 전 · 경보 꺼짐」 |
| starting | 진행 중인데 현장 묶음이 이번 작업 것이 아님 | 「관제 준비 중」 |
| on | 현장 묶음 `work_id` = 진행 중 작업(또는 `ppe_policy` 없는 이전 묶음: Tier 0 가 판정한다, -679 1.1) | 「관제 중」 |
| ending | 진행 중 작업 없음, 현장 묶음이 아직 끝난 작업의 것 | 「종료 요청됨 · 현장 경보 끄는 중」 |

문제(starting·ending, on 은 끊김만)는 우선순위대로: Tier 0 끊김(`link_lost`) → 거부(`rejected`: 이번 작업의 가장 새 묶음, 종료면 가장 새
작업 없음 묶음이 REJECTED·MISMATCH) → 지연(`delayed`). 문제마다 무엇이 / 어떻게 두 줄이고 거부만 주 단추 「현장에 다시 보내기」
(`POST /api/config-bundles`, 서버가 그때 작업 상태로 ppe_policy 를 싣는다)를 둔다. 작업 시작·종료는 다시 보내지 않는다.

**명세에 없어 정한 수치**: 지연 = 기준 시각 + 10 s(`MONITORING_APPLY_DELAY_MS`, EC2 시계). 기준은 이번 작업(종료면 작업 없음)의 가장 새 묶음
발행 시각이고 목록에 없으면 작업 시작·종료 시각이다. 다시 보내면 그때부터 다시 잰다. 근거: 내려받기·검증·다음 tick 으로 보통 수 초(-679 6절),
health 1 Hz·묶음 3 s 조회 지연을 더해도 10 s 안. 판정은 지연 경계에 한 번만 다시 깨운다(매 초 다시 그리지 않음).

Tier 0 연결을 모르면(EC2 와 끊겨 health 가 낡음) 끊김으로 보지 않는다. 그때 상단 바가 EC2 끊김을 따로 알린다.

## 3. 화면 변경

| 위치 | 변경 |
|---|---|
| 작업 전 확인(`PreflightStage`) | 시작 성공 → `/live` 로 이동. 확인창 '작업 시작' 은 요청 중 잠겨 중복 시작 없음. 오른쪽에 워치 카드(`WatchSummaryCard`): 표시 이름(health `watch_names`)·기기·연결(health `watches`)·사람 짝지음(`GET /api/watch-bindings`), `연결 a/b`, 끊김이면 어떻게 한 줄. 시작을 막지 않는다 |
| 관제 상단(`WorkSessionBar`) | 세션 바 맨 앞 점을 관제 단계 배지로. 문제 두 줄·거부 단추를 바 안에. 종료 대기는 「작업 종료」 칸(대기 화면 위에도 보임). 작업 전 「작업 시작 전 · 경보 꺼짐」 에 사람이 보이면 「작업을 시작해야 경보가 켜집니다」·「작업 관리로」(기존 `WorkPpeStatus` 의 작업 전 안내를 합쳐 중복 배지 제거) |
| 보호구 줄(`WorkPpeStatus`) | 진행 중일 때만. 적용 전 바로 「보호구 감시 준비 중」, 적용 뒤 「안전모·조끼 감시 중」, 끈 장비는 「○○ 감시 대상 아님」 |
| PPE 안내(`PpeWorkNotice`) | 15 s 지연 뒤 개발 용어(R9·R11·구성 묶음)로 보이던 '요구 미적용·해제 대기' 를 뺐다(관제 단계·보호구 줄이 바로 보인다). 남은 '보호구 감시 멈춤'(관제 중인데 PPE 상태 10 s 넘게 없음, 15 s 지속)은 무엇이 / 어떻게로 바꿨다. `ppeRequirementMismatch` 삭제 |
| 관제 대기 | 「세팅을 마친 뒤 작업을 시작하면 경보가 켜집니다.」 |
| 관제 적용 상태 카드(`AppliedStatusCard`) | 맨 위에 이번 작업 설정 현장 적용(대기·지연·거부·끊김·경보 켜짐). 구역 수는 이번 작업 묶음이 적용됐을 때 그 묶음의 구역 수. 카메라는 모두 이어졌을 때만 체크, 아니면 경고(0/4 에 초록 없음). 아이콘: 체크·시계·경고 |
| 작업 종료 뒤(`RunStage` 종료 화면) | `EndAlertStatus`: 「종료 요청됨 · 현장 경보 끄는 중」(지연·거부·끊김 안내) → 「현장 경보 종료 확인」 |

`PpePolicyNotice`(「PPE 요구 미수신」)는 명세 7.2 의 표시 이름이라 그대로 뒀다. 세팅 화면(`features/setup/`)은 건드리지 않았다.

## 4. 코드·시험 위치

- 판정: `front/apps/admin/src/features/monitor/monitoringPhase.ts`(순수), `useMonitoringPhase.ts`(훅: Tier 0 연결 글자 구독, 지연 경계 타이머)
- 표시: `MonitoringStatus.tsx`, `WorkSessionBar.tsx`, `WorkPpeStatus.tsx`, `workPpeText.ts`, `PpeWorkNotice.tsx`, `MonitorStandby.tsx`,
  `features/work/run/{AppliedStatusCard,EndAlertStatus,run,RunStage}`, `features/work/preflight/{PreflightStage,WatchSummaryCard}`, `features/watch/watchStatus.ts`(`watchSummaryRows`)
- 시험: `__tests__/features/monitor/monitoringPhase.test.ts`(단계·문제·지연 기준·문구에 개발 용어 없음), `MonitorWorkSession.test.tsx`
  (준비 중 → 관제 중, 지연, 거부 → 다시 보내기는 묶음 발행만, 종료 대기 → 확인), `WorkPpeStatus`·`PpeWorkNotice`·`run`·`RunCards`·`RunStage`·`PreflightStage`(시작 성공 → `/live` 1회, 워치 카드)

## 5. 남은 위험

- 시작 직후 묶음 목록이 아직 옛 것이면(시작 응답 뒤 다시 받기 전) 잠깐 「관제 준비 중」이다. 안전 쪽(경보 켜짐을 늦게 말함)이다.
- 묶음 목록은 최근 20개다. 그 사이 발행이 20개를 넘으면 현장이 쓰는 묶음을 못 찾아 「관제 준비 중」/idle 로 잘못 볼 수 있다(작업 하나 동안은 생기지 않음).
- `ppe_policy` 가 없는 이전 묶음을 쓰는 현장은 진행 중이면 '관제 중' 으로 본다(Tier 0 도 판정한다). 작업이 없을 때 그 묶음이면 idle 로 보지만 Tier 0 는 판정한다(이전 EC2 만 해당, 기존 「PPE 요구 미수신」 안내가 남는다).
- 「현장에 다시 보내기」 는 지금 유효한 구역·규칙·보정으로 새 묶음을 만든다. 거부 원인(보정 문서 문제 등)이 그대로면 다시 거부된다(안내 문구가 현장 세팅 확인을 말한다).
- Tier 0 재시작 중 작업이 바뀌는 경우 등 -679 6절의 위험은 그대로다. 화면은 현장이 보고한 묶음만 믿는다.

## 6. 명세 정리 대상

- 13장: 실시간 관제 세션 바의 관제 단계 배지(관제 준비 중·관제 중·지연·실패·현장 연결 끊김, 종료 대기), 판정 근거 = 현장 묶음(config_ack APPLIED)의 work_id, 지연 10 s.
- 8.3: 작업 시작 성공 뒤 실시간 관제로 이동, 작업 종료 뒤 「현장 경보 종료 확인」.
- 부록 E: 장면 1 의 "세션 바가 뜨고 2~3초 뒤" 를 "「관제 중」 이 뜬 뒤" 로. 작업 전 확인 화면에서 A·B 워치 연결을 본다.
