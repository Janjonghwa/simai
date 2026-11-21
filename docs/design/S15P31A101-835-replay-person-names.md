# S15P31A101-835 지난 기록의 사람 표기를 그 시각 이름으로

## 배경
- 실시간 관제 화면은 `viewer_health.person_names`(S15P31A101-658)를 통해 작업자의 표시 이름(관리자 지정 이름·워치 기기 이름)을 보인다.
- 반면 지난 기록 다시 보기(이벤트 로그·작업자 목록)는 이름 조회 경로가 없어 모든 작업자를 일괄 `작업자 #NNNN`(World ID 끝번호)으로만 표기했다(S15P31A101-751).
- 워치 기기 이름이나 배정이 나중에 바뀌더라도, 지난 기록을 볼 때는 **그 시각** 그 World ID 에 해당하던 표시 이름을 보여 주어야 한다.

## 관련 명세·원칙
- `docs/spec/05-processing.md` §5.2: 병합(`aliases`) 승계, 분할(`split_from`) 미승계.
- `docs/spec/11-data.md` §11.1·§11.4·§11.5: 추가 전용 이력(`V65`, `V35`/`V85`)을 읽기 전용으로만 조회하며 `world_track_state`·`risk_event`·`work_unit` 원본 테이블은 변경하지 않는다.
- `docs/spec/14-security.md`: 로그인한 모든 역할(`VIEWER`·`FOREMAN`·`ADMIN`)이 열람할 수 있다.

## 설계

### 1. 서버 (`GET /api/people/display-names?date=YYYY-MM-DD`)
- **경로·권한**: `PersonDisplayNameController.listForDate` (`GET /api/people/display-names?date=YYYY-MM-DD`). 로그인한 모든 역할 열람 가능(읽기 전용, `REPEATABLE_READ` 트랜잭션).
- **입력 검증**: `date` 가 없거나 `YYYY-MM-DD` 달력 날짜가 아니면 `400 INVALID_DISPLAY_NAME_DATE` (`ErrorCode.Watch.INVALID_DISPLAY_NAME_DATE`).
- **응답 형식**:
  ```json
  {
    "site_id": "classroom-1",
    "date": "2026-11-21",
    "items": [
      {
        "world_id": "w-0012",
        "display_name": "추창만",
        "name_source": "WATCH_DEVICE",
        "from_ms": 1795119600000,
        "to_ms": 1795206000000
      }
    ]
  }
  ```
  - `items` 는 `Asia/Seoul` 기준 `[00:00, 다음날 00:00)` 안의 반열린 구간 `[from_ms, to_ms)` 목록이다. 인접한 같은 `(world_id, display_name, name_source)` 구간은 하나로 합친다.
  - 이름이 없는 구간(`UNIDENTIFIED` 포함)은 싣지 않는다(호출부가 `작업자 #NNNN` 폴백으로 표기한다).
- **시각별 해석 규칙 (`PersonDisplayNames.historicalSnapshot`)**:
  1. `WORLD_MANUAL`: `person_world_name_history` (`site_id`, `site_date = date`) 에서 시각 `t` 이하 가장 최근(`created_at`, `history_id`) 행이 `SET`/`MERGE` 이면 그 이름. `CLEAR` 이면 다음 우선순위로 내려간다.
  2. `WATCH_DEVICE`: 시각 `t` 에 유효한 `watch_binding`(`bound_at <= t < min(unbound_at, device.revoked_at)`) 의 `device_id` 에 대해 `watch_display_name_history` (`site_id`, `created_at <= t`) 가장 최근 행이 `SET` 이면 그 이름(이전 날에 지정된 워치 이름 포함, 다음 날 변경분은 미반영).
  3. 없음: 생략.
- **병합(`aliases`)·이어받기(`handoff_from`) 승계**:
  - `person_world_merge_log` (`site_id`, `site_date = date`) 와 당일 발생한 `watch_binding.handoff_from` (미확정 반증 해제 `MATCH_LOST` 제외) 을 같은 사람의 계보로 묶는다.
  - 분할(`split_from`)은 계보에 넣지 않아 자식이 부모 이름을 물려받지 않는다.
  - 병합·이어받기 이전 시각에 이름이 비어 있던 ID 도 같은 사람의 당일 지정 이름·배정 워치 이름을 이어받아, 병합 전 발생한 이벤트 로그에서도 같은 사람 이름으로 보인다.
- **성능**: `world_track_state` 하이퍼테이블을 훑지 않고 이력·배정 메타 테이블 4개만 읽어 변화 시각 기준 sweep-line 으로 구간을 계산한다.

### 2. 프론트엔드
- `features/people/api.ts`: `getHistoricalPersonNames` (`GET /api/people/display-names?date=...`, zod 스키마 검증).
- `features/people/personDisplay.ts`: `HistoricalPersonNameSpan`, `resolveHistoricalPersonName`, `resolveHistoricalWorkerName`, `workerShortLabel(worldId, atMs?, spans?)` 확장.
- `features/people/usePersonDisplay.ts`: `useHistoricalPersonNames(date)` 훅 (`historicalPersonNamesKey(date)`). 이전 EC2 나 오류 시에도 `undefined` 를 반환해 기존 `작업자 #NNNN` 폴백이 깨지지 않는다.
- `features/history/WorkLists.tsx`·`ReplayPanels.tsx`·`features/work/past/PastDayPage.tsx`:
  - 다시 보기 이벤트 로그(`EventLog`)가 각 사건 줄의 발생 시각(`first.raisedAt`)으로 표시 이름을 찾는다. 이름이 있으면 그 이름, 없으면 `작업자 #NNNN`.
  - `외 n명` 묶음과 hover(`title`)의 원래 World ID 목록은 그대로 유지한다.
  - 작업자 목록(`WorkerList`)도 활동 구간 기준 이름 표시 + hover World ID 를 지원한다.
  - 종료 미팅·TBM 보고서는 사람별 World ID 표기가 없어 변경하지 않는다.

### 3. 목업
- `mocks/peopleNames.ts`: `bindingHistory` 를 추가 전용으로 남기고 `GET /api/people/display-names?date=YYYY-MM-DD` 핸들러를 추가한다.
  - 오늘 날짜(`Asia/Seoul`): `w-0012` → `추창만`, `w-0015` → `이영희`, `w-0021` → 이름 없음(생략). 당일 워치·World 이름 변경 및 재배정도 시각별 구간으로 반영한다.
  - 지난 날(11-18·11-19 등 가짜 기록): `items: []` 를 돌려주어 `작업자 #NNNN` 표기를 유지한다.

## 버린 대안
- **World ID 당 단일 이름만 돌려주는 평면 맵**: 같은 날 워치 배정이 바뀌거나 워치·수동 이름이 바뀌었을 때 시각별 이름을 구분할 수 없다. 반열린 구간 `[from_ms, to_ms)` 배열이 단순하면서도 시각별 배정·이름 변경을 정확히 표현한다.
- **`GET /api/replay` 응답에 이름을 섞는 안**: `replay` 는 창 분할·병합(`mergeReplay`)이 있고 계약 `world_state`/`risk_event` 봉투만 다루며, 위치·이벤트 기록에 사람 이름을 두지 않는 원칙(NFR-14)과도 분리하는 편이 깔끔하다.

## 남은 위험
- 지난 기록에서 같은 이름의 작업자가 둘 이상(동명이인)인 경우 짧은 표시는 이름만 보이고 hover 의 World ID 로 구분한다(실시간 화면처럼 `(0012)` 괄호 구분이 필요하면 후속으로 `isDuplicateName` 판정을 덧붙일 수 있다).
- `watch_binding` 테이블에 `site_id` 열이 없어 현재 MVP 단일 현장(`SiteLookup`) 전제를 따른다. 다중 현장 확장 시 `device`·`watch_binding` 의 현장 필터링이 함께 필요하다.
