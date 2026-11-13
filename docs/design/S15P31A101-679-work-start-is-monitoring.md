# S15P31A101-679 작업 시작 = 관제 시작

사용자 결정(2026-11-13): 작업 진행 중(작업 시작 ~ 작업 종료)에만 경보(위험 이벤트·관제 알림·현장 경보기·워치)를 낸다. 작업 전에는
사람 위치·3D·CCTV 화면은 그대로 보이되 경보는 없고, 실시간 관제에 「작업 시작 전 · 경보 꺼짐」을 보인다. 기록(다시 보기·타임라인·
이벤트 집계·업로드 영상)은 모두 작업 시작~종료 구간이다.

배경: Tier 0 는 구성 묶음만 있으면 작업과 상관없이 판정했다. 11-12 에 작업 시작(15:26) 전 14:00~15:23 에 위험 6·경고 5·주의 153건이
실제 경보로 나갔다. -667(다시 보기 범위를 세팅 완료부터·그날 기록 대체)과 -668(녹화 업로드 구간을 세팅 완료부터)은 이 증상을 기록
쪽에서 덮은 것이라 되돌린다.

관련 명세: 3.2(이벤트 소유권·구성 묶음·재시작 복구), 6.4(생명주기·해소), 7.2 R9(요구 해제 해소), 7.3(채널), 8.3(작업 시작·종료),
13장(실시간 관제 작업 세션 바).

## 1. 설계

### 1.1 Tier 0 가 '작업 진행 중'을 아는 방법 — 기존 `ppe_policy.work_id`

- EC2 는 **모든** 구성 묶음에 그때 작업 상태로 만든 `ppe_policy` 를 싣는다(`ConfigBundleService.ppePolicy`, -518·-654).
  `work_id` 는 진행 중(IN_PROGRESS) 작업, 없으면 null 이다.
- 작업 시작·종료는 상태 변경과 새 묶음 발행을 한 트랜잭션으로 한다(`WorkSessionService.start/end` → `publishWith(..., true, ...)`).
  묶음을 만들지 못하면 시작·종료 자체가 실패한다. 그래서 서버 변경 없이 시작 묶음은 work_id 있음, 종료 묶음은 work_id null 이 보장된다.
- Tier 0: `ActiveConfig.work_in_progress` = `ppe_policy is None or ppe_policy.work_id is not None`.
  - `ppe_policy` 가 없는 묶음(-518 이전 EC2·시험 묶음)은 '작업 정보 없음'이라 이전 동작(판정)을 유지한다. 모르는 채로 경보를 끄지 않는다.
    지금 EC2 는 늘 싣으므로 운영에서는 생기지 않는다.
- **계약 변경 없음.** config_bundle(I1)에 명시 필드를 더하는 안은 버렸다(아래 1.6). 같은 사실(작업 진행 중)이 이미 정본 값(work_id)으로
  들어 있고, 필드를 더하면 두 값이 어긋날 수 있으며 Lead 승인이 필요한 I1 변경이다.

### 1.2 막는 위치 — 판정 자체를 끄고 추적·화면은 계속

`JudgmentLoop._monitoring_on(active, world, msg_id)` (ai/services/tier0/judge.py):

| 이 tick 묶음 | 동작 |
|---|---|
| 작업 진행 중 아님 | `active.evaluate` 를 부르지 않는다. `LifecycleTracker.stand_down` 으로 감시를 내린다(활성 이벤트 닫기·대기 버림). `stats.idle_ticks` 증가 |
| 작업 진행 중, 판정을 켠 작업과 같음 | 기존 판정 그대로 |
| 꺼짐 → 켜짐 | 규칙 기억(`RuleState.reset`, R4 머리 높이 이력)을 비우고 판정을 켠다 |
| 작업 A → 작업 B(종료 묶음을 건너뜀) | 같은 tick 에 A 의 이벤트를 `stand_down` 으로 닫고, 기억을 비운 뒤 B 로 판정 |
| 시작 직후(판정을 켠 적 없음) 작업 중 | 닫지 않는다(재시작으로 복원한 이벤트는 그 작업의 것) |

인지(검출·추적·융합)·ID 교정·world_state 송신·체크포인트는 그대로 돈다. 그래서 작업 전에도 사람 위치·3D·CCTV·아바타는 보인다.

판정만 돌리고 이벤트만 막는 안은 버렸다: 13차 혼잡 장면에서 Tier 0 GIL 붕괴가 있었고, 쓰지 않을 판정에 CPU 를 쓰지 않는다. 또
판정을 돌리면 생명주기 대기(지속 조건)가 쌓여 작업 시작 순간 한꺼번에 열리는 문제가 생긴다.

입력 정지 tick(새 프레임 없음)도 같은 문(마지막 World 로 `stand_down`)을 지난다.

### 1.3 작업 종료 때 열린 이벤트 — 기존 관측 해소로 그 tick 에 닫는다

`LifecycleTracker.stand_down(world, config_version, evidence_ref)` (ai/src/simai_ai/risk/lifecycle.py):

- 활성 이벤트를 모두 RESOLVED 로 닫는다. 변경 사유 `CLEARED`·`WORK_ENDED`(+ 검증 상태가 바뀌면 `VERIFICATION`, 버전이 바뀌면 `VERSION`).
- 검증 상태는 VERIFIED, 근거(evidence_ref)는 그 tick 의 world_state msg_id 다. 해소 유지 시간 없이 그 tick 에 닫는다.
  - 근거: R9 의 요구 해제와 같다. '작업 종료'는 관측이 아니라 구성으로 확정된 사실이고, 명세 7.2 R9(v1.3 수정 67)가 이미 이것을
    관측 위치와 관계없는 VERIFIED 해소로 정했다. 사람 대상이든 구역 대상(R7)이든, 관측을 잃어 UNVERIFIABLE 이든 같다.
  - 그래서 I1 `risk_event_transition` 의 관측 해소 규칙(RESOLVED 는 VERIFIED·근거 필수, resolution 없음)을 그대로 지킨다. EC2·화면 변경 없음.
- 현장 경보를 낸 이벤트는 `AlertClear` → `local_alert`(RESOLVED)를 보낸다. 현장 경보기·워치가 꺼지고 active_site_alerts 목록에서 빠진다.
- 지속 대기 중인 키·재개 후보(해소 이벤트)·재시작 연결 힌트를 버린다. 대기 중 관측 상실 발생(`_persist` 의 안전 쪽 발생)도 하지 않는다.
  다음 작업의 같은 위험은 새 이벤트다(재개하지 않는다).
- `WORK_ENDED` 는 저널 기록의 `changes`(I1 계약에 없는 lifecycle 필드, 수신 측은 무시)에만 남는 감사·진단 값이다.

새 resolution 종류(`WORK_ENDED`)를 계약에 더하는 안은 버렸다(1.6).

### 1.4 작업 중 묶음이 바뀔 때·재시작

- 작업 중 구역·규칙·보정 발행은 work_id 가 같으므로 판정 상태에 영향이 없다(기존 묶음 전환 그대로).
- 재시작: Tier 0 는 보관 묶음으로 시작하고, EC2 는 I1 (재)연결 때 최신 묶음을 다시 보낸다. 판정 여부는 그 묶음의 work_id 로만 정하므로
  따로 저장할 상태가 없다. 작업 밖이면 첫 실제 World tick 에 복원 이벤트도 닫는다. 작업 중이면 복원 이벤트를 그대로 둔다
  (`_monitored_work` 가 None 인 첫 켜짐은 닫지 않는다).
- 묶음이 없으면(NO_ACTIVE_CONFIG) 기존처럼 판정하지 않는다.

### 1.5 규칙별 영향

모든 규칙(R1~R7·R9·R11)이 같은 문을 지난다. 규칙 코드 변경 없음.

| 규칙 | 작업 전 | 작업 종료 때 |
|---|---|---|
| R1·R2·R3·R5·R6(사람 대상) | 판정 없음 | 열린 이벤트 VERIFIED 해소, 대기 버림 |
| R4(낙상 의심) | 판정 없음. 켜질 때 기억(이력) 비움 | 위와 같음 |
| R7(감시 저하, 구역 대상) | 판정 없음(작업 전 카메라 저하는 경보 아님. 화면의 카메라 상태·health 는 그대로) | 근거 없이도 닫는다(구성 확정 해소). 작업 시작 즉시 저하면 바로 다시 난다(R7 은 즉시 발생) |
| R9·R11(안전모·조끼) | 이미 요구 없음(work_id null → NOT_REQUIRED) | 이전처럼 해소되던 것을 이 tick 에 닫는다 |

### 1.6 버린 대안

| 안 | 버린 이유 |
|---|---|
| config_bundle 에 `monitoring` 같은 명시 필드 추가 | work_id 와 중복이라 어긋날 수 있고, I1 계약 변경(Lead 승인)이다. 지금 EC2 가 이미 정확한 값을 싣는다 |
| risk_event_transition resolution 에 `WORK_ENDED` 종류 추가(마이너 2.4.0) | EC2·front 가 새 종류를 받아야 하고 배포 순서가 생긴다. R9 요구 해제라는 같은 의미의 선례가 관측 해소로 이미 있다 |
| 판정은 돌리고 이벤트·경보만 막기 | CPU 낭비(13차 GIL 붕괴), 대기 누적, '판정은 있는데 이벤트가 없음'의 이중 상태 |
| 작업 종료 때 이벤트를 열린 채 두고 UNVERIFIABLE 로 남기기 | 현장 경보기·워치가 계속 울리고 운영자가 하나씩 닫아야 한다(HIGH·CRITICAL·사람 대상은 운영자 종료도 거부) |
| EC2 가 작업 밖 전이를 거르기 | 현장 경보(노트북 I0)·워치는 Tier 0 가 직접 보내므로 막지 못한다. 이벤트 소유권은 Tier 0 다(3.2) |
| 실시간 관제 '작업 진행 중' 값을 Tier 0 health 에 싣기 | health 는 I1 계약이다. 화면은 이미 작업 목록(GET /api/work-units)의 IN_PROGRESS 로 세션 바를 그린다 |

## 2. 화면(front)

- `WorkSessionBar`(실시간 관제 알림 칸, 경보 배너 아래): 진행 중 작업이 있으면 기존 세션 바, 없고 관제 화면이 열려 있으면(세팅을 마친 오늘 작업,
  대기 화면 아님) 작은 표시 「작업 시작 전 · 경보 꺼짐」(`aria-label="관제 상태"`). 대기 화면(세팅 전·오늘 작업 끝)에서는 보이지 않는다.
- 작업 진행 여부는 서버 작업 목록(IN_PROGRESS)으로 안다. 서버 변경 없음.

## 3. 되돌린 범위

| 티켓 | 되돌린 것 | 위치 |
|---|---|---|
| -667 | 다시 보기 구간 `replayRange` = min(세팅 완료, 실제 시작) → 실제 시작 ~ 실제 종료(진행 중이면 지금). 그날(00:00~24:00) 기록 대체(`dayReplayRange`·`DAY_FALLBACK_NOTICE`·`shouldFallbackToDay`)·안내 문구(`notice`) 제거. `setup_completed_at` 스키마 필드(history·work·tbm types)와 목 값 제거. 시험 원복 | front `features/history/{works,useWorkReplay,types}.ts`, `features/work/{types.ts,past/PastDayPage.tsx}`, `features/tbm/types.ts`, `mocks/{history,work}.ts`, 해당 시험 |
| -668 | 녹화 업로드 구간 시작 `LEAST(setup_completed_at, actual_started_at)` → `actual_started_at`(구간 조회·조각 등록 검사·open_work 모두). 계약 `recording_windows` 1.0.0 설명, 게이트웨이 README·-668 설계 문서 문구 | server `RecordingService`, `RecordingSegmentIntegrationTest`, `contracts/messages/recording_windows.schema.json`, `contracts/README.md`(v7.46.1), `gateway/{recorder,ops}/README.md` |

-667 커밋 안의 `setupSupplement.test.ts`(-670 높이 맞춤) 변경은 되돌리지 않았다(이 문제와 무관). 캘린더 이벤트 집계(`WorkUnitRepository`)는
이미 작업 구간이라 그대로다. 게이트웨이 코드는 EC2 가 준 구간만 쓰므로 바뀌지 않는다.

`recording_windows` 는 구조·schema_version(1.0.0 const)을 그대로 두고 설명만 고쳤다. 구간이 좁아질 뿐 소비자(게이트웨이) 동작이 같고,
const 를 올리면 게이트웨이를 함께 바꿔야 한다. 같은 날 신설한 기기 REST 라 계약 묶음은 패치(v7.46.1)로 남겼다.

## 4. 시연 진행 순서

1. 세팅 완료(관제 화면이 열린다). 화면 상단에 「작업 시작 전 · 경보 꺼짐」, 사람 위치·3D·CCTV 는 보인다. 위험 구역에 들어가도 경보 없음.
2. TBM 서명.
3. 작업 시작(작업 관리 → 작업 시작). EC2 가 새 묶음(work_id 있음)을 내고 Tier 0 가 다음 tick 에 적용(로그
   「작업 진행 중(work_id=…): 위험 판정·경보를 켠다」). 화면 표시가 세션 바로 바뀐다. 묶음 적용까지 수 초 걸릴 수 있으니 장면은 세션 바가
   뜨고 2~3초 뒤 시작한다.
4. 장면(구역 진입 등). 경보·워치·관제 알림이 난다.
5. 작업 종료. 열린 이벤트는 그 tick 에 해소되고 현장 경보기·워치가 꺼진다(로그 「작업 진행 중이 아니다…열린 이벤트를 닫는다」).
6. 지난 작업 화면의 다시 보기·타임라인·업로드 영상은 3~5 구간(작업 시작~종료)만이다.

## 5. 시험

- ai: `tests/risk/test_stand_down.py`(구역·사람 대상 VERIFIED 해소·근거, AlertClear, 대기 버림·관측 상실 발생 없음, 재개 없음, 입력 검사, 복원 이벤트),
  `tests/services/tier0/test_tier0_judge.py` 의 -679 절(작업 전 구역 안이어도 이벤트·현장 경보·저널 0이고 world_state 는 계속, 작업 시작 뒤
  발생 → 종료 tick 에 RESOLVED·local_alert RESOLVED·계약 검증, UNVERIFIABLE·대기 키, 입력 정지 중 종료, 작업 A→B, 작업 밖 재시작 복원 닫기,
  작업 중 재시작 복원 유지(반례), ppe_policy 없는 묶음은 판정 유지).
- server: `RecordingSegmentIntegrationTest`(구간 시작 = 작업 시작, 세팅 완료~작업 시작 사이 조각은 OUTSIDE_WORK_WINDOW).
- front: `MonitorWorkSession.test.tsx`(작업 시작 전 표시·시작하면 세션 바·대기 화면에서는 없음), history·past 시험(-667 원복).

## 6. 남은 위험

- 작업 시작 클릭과 Tier 0 묶음 적용 사이(내려받기·검증, 보통 수 초)는 화면은 세션 바인데 경보는 아직 꺼져 있다. 반대로 종료 직후 수 초는
  화면은 꺼짐인데 이벤트가 남아 있을 수 있다. 묶음 적용 ACK(config_ack)를 화면에 잇는 일은 범위 밖이다.
- 입력이 완전히 멈춘 채(카메라 모두 끊김) 처음부터 World 가 없으면 `stand_down` 할 world 가 없어 복원 이벤트를 닫지 못한다. 첫 프레임이 오면 닫힌다.
- 재시작 중에 작업이 A 에서 B 로 바뀌었으면(종료·시작이 모두 Tier 0 가 꺼져 있는 동안) 복원한 A 의 이벤트가 B 에서 이어진다(판정을 켠 작업을
  저장하지 않는다). 시연에서는 생기지 않는다.
- `ppe_policy` 가 없는 묶음은 판정을 켠다(경보를 모르는 채로 끄지 않음). 지금 EC2 는 늘 싣는다.
- `WORK_ENDED` 를 모르는 이전 Tier 0 로 되돌리면 이 값이 든 저널 기록을 읽지 못한다(Change 열거). 되돌릴 때는 저널을 먼저 비운다.
- REPLAY 출처(file 입력)도 같은 문을 지난다. 작업 밖 묶음으로 녹화를 다시 돌리면 이벤트가 없다(의도: 작업 시작 = 관제 시작).

## 7. 명세 정리 대상

- 3.2(구성 묶음·이벤트 소유권): "Tier 0 는 활성 묶음의 `ppe_policy.work_id` 가 null 이면 위험 판정을 하지 않는다(추적·world_state 는 계속).
  작업 종료 묶음 적용 tick 에 활성 이벤트를 구성 확정 해소로 닫는다"를 적는다.
- 6.4·7.2: R9 요구 해제와 같은 '구성 확정 해소'를 모든 규칙의 작업 종료 해소로 넓힌 것.
- 7.3·8.3: 작업 전에는 경보 채널(관제 알림·현장 경보기·워치)이 없다.
- 13장: 실시간 관제 작업 세션 바 자리에 「작업 시작 전 · 경보 꺼짐」.
- 11.5·12.6: 녹화 업로드 구간 = 작업 시작~종료(-668 설명 정정).
