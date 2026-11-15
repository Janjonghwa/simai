# S15P31A101-751 지난 날 이벤트 로그 묶음·사건 수

UX 재점검(2026-11-16 N3·N4): 지난 날 이벤트 로그에 내부 트랙 ID(`w-mwhubgp0-0010`)가 보이고 같은 초의 「감시 저하 · 구역」이 4행으로 겹쳤으며,
달력 칸(「위험 190·경고 250」)과 종료 미팅(「총 705건 · 미확인 위험 경보 190건」)의 수는 관제 카드의 「n회」·사건 수(-731)와 기준이 달랐다.
기록·이벤트·판정은 바꾸지 않는다(AGENTS 규칙 1·6). 화면 표시와 서버의 **읽기 집계**만 더한다.

관련 명세: 7.4(이벤트 병합), 13.4(이벤트 패널). 관련 설계: -731(카드 묶음·수 정의), -723. 계약 변경 없음(작업 응답에 칸만 더함, 아래 5절).

## 1. 결정

- 로그 줄 = 관제 카드와 같은 묶음 함수. `incidents.ts` 의 `chainItems`(같은 위험=규칙·구역별로 시각 순, 같은 사람이거나 앞 줄 끝에서 60초 안이면 한 줄)를
  내보내 `features/history/pastEventRows.ts` 가 그대로 쓴다. 지난 기록이라 살아 있는 사람이 없어(alive 빈 집합) 트랙 번호만 바뀐 같은 초 경보가 「×n」 한 줄이 된다.
  줄 머리 수는 줄 수(= 사건 수), 경보 건수는 괄호(`사건 12 (경보 190)`). 같은 줄의 발생 수는 카드와 같이 `1 + resume_count` 합이다(`resumeCount` 를 EventRow 에 더함).
- 사람 표시: `작업자 #0010`(`workerShortLabel`, 카드의 이름 목록 없을 때 표시와 같음), 여럿이면 `작업자 #0010 외 3명`, 원래 World ID 는 hover(`title`).
- 달력·종료 미팅: 「경보 n건(사건 m건)」 둘 다. 서버가 같은 규칙을 Java(`IncidentCounter`)로 세어 작업 응답에 `incident_counts{CRITICAL,HIGH}` 를 더한다.
  달력 칸의 보이는 숫자는 사건 수, hover·접근성 이름에 경보 기록 수를 곁들인다(칸이 없는 이전 서버는 경보 수 그대로). 월간 요약도 같다.
  종료 미팅은 AI·결정 문장을 그대로 두고 끝에 서버가 결정론 한 줄을 덧붙인다: 「관제 카드 기준 사건: 위험 a건·경고 b건(위 경보 수는 기록 한 건 한 건의 수).」
- 사건의 등급 = 든 경보 중 가장 높은 등급(위험이 든 사건은 경고에 다시 세지 않는다). 사건 수 범위는 `event_counts` 와 같다(작업 구간·작업 구역, 종료 미팅은 끝난 실행 구간).

## 2. 코드·시험

| 영역 | 위치 |
|---|---|
| 로그 줄 묶음 | `front/apps/admin/src/features/history/{pastEventRows.ts,WorkLists.tsx}`, `features/events/incidents.ts`(`chainItems`·`riskKeyOf`·`occurrencesOf` 내보냄), `features/people/personDisplay.ts`(`workerShortLabel`) |
| 달력 | `features/work/calendar/{calendarMath.ts,CalendarCell.tsx,WorkCalendar.tsx}`, `features/work/types.ts`(`incident_counts`) |
| 서버 | `server/.../domain/work/{IncidentCounter.java,WorkUnitRepository.java}`, `domain/report/WorkWrapUpRepository.java`(`incidentNote`) |
| 시험 | front `pastEventRows.test.ts`·`WorkLists.test.tsx`·`calendarMath.test.ts`·`WorkCalendar.test.tsx`; server `IncidentCounterTest`·`WorkUnitIntegrationTest`·`WorkSessionIntegrationTest` |

## 3. 명세에 없어 정한 값

| 값 | 근거 |
|---|---|
| 지난 날 묶음은 alive 를 빈 집합으로 | 지금 살아 있는 사람이 없다. 서로 다른 두 사람이 같은 위험·구역에서 60초 안에 난 경우는 한 줄로 묶이나, 줄이 「외 n명」과 hover 로 사람 수를 알린다 |
| 서버는 CRITICAL·HIGH 만 센다 | `event_counts` 와 같은 범위(달력·종료 미팅의 위험·경고) |

## 4. 버린 대안

- **서버에서 이벤트를 병합·수정**: 기록을 바꾼다(규칙 1·6). 표시·집계만 한다.
- **종료 미팅 facts(`tbm_form_ending_request`) 에 사건 수 칸 추가**: 계약·Python·Java 세 곳과 AI 문장 변경이 필요하다. 결정론 한 줄을 서버가 덧붙이는 쪽으로 했다.
- **「같은 초」만 묶기**: 카드 묶음과 기준이 갈라져 줄 수와 사건 수가 또 다르게 된다. 카드와 같은 함수를 쓴다.

## 5. 잔여 위험·넘긴 일

1. 구역 이름: 로그는 지금 구역 목록으로 이름을 찾는다. 이후 보관·삭제된 구역의 이벤트는 이름을 몰라 `구역` 으로 보인다(서버에는 시점별 이름이 있으나 이벤트 응답에 싣지 않는다). 시점별 구역 이름을 이벤트 응답·재생 기록에 싣는 일은 넘긴다.
2. 달력 사건 수 계산은 작업마다 이벤트를 한 번 더 읽는다(N+1, 달 30건 안팎). 느려지면 한 번에 묶어 읽는다.
3. 종료 미팅의 「미확인 위험 경보 n건」은 그대로 기록 건수다. 사건 기준 미확인 수는 확인 기록 묶음이 필요해 넘긴다.
4. `incident_counts` 는 응답 칸 추가(마이너)이며 이전 화면은 무시한다. 새 화면은 칸이 없으면 경보 수로 돌아간다.
