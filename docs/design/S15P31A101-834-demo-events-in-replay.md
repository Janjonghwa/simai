# S15P31A101-834 목업 시연 경보를 오늘 다시 보기에 남기기

## 원인
- 목업 다시 보기(`mocks/history.ts` `historyReplay`)는 30분 간격의 합성 경보(`sampledEvents`)와 인공 끊김(`OUTAGE_MS`)·빈 방(`EMPTY_ROOM_MS`)만으로 응답을 만들었다.
- 시연 시나리오(`mocks/demoAlarmScenario.ts`)가 작업 시작 뒤 5·10·17·25·35초에 띄우는 4건의 경보 전이는 어디에도 기록되지 않아, 오늘 작업을 종료한 뒤 다시 보기를 열면 타임라인 핀과 이벤트 로그가 비었다.

## 변경 (목업 전용, 실제 서비스 경로 변경 없음)
1. **세션 메모리 기록 저장소 (`src/mocks/demoReplayStore.ts`)**
   - 시연 실행 구간(`startRun`/`endRun`)과 경보 전이(`recordEvent`)를 실행 단위로 저장한다.
   - `/api/replay` 형식에 맞게 원래 `msg_id`를 유지하고(17초 동시 전이 충돌 방지), `held_by_tier0` 제거·`delivery: 'REALTIME'` 설정만 적용한다.
2. **시연 송출 연동 (`src/mocks/startBackend.ts`, `src/mocks/handlers.ts`)**
   - 작업 시작·종료 시 `workMock` 실행의 실제 `started_at`/`ended_at`으로 저장소 실행을 열고 닫으며, `단상 개구부` 구역 ID를 맞춘 시연 경보 전이를 저장소에 남긴다.
3. **오늘 다시 보기 `/api/replay` (`src/mocks/history.ts`)**
   - 요청 구간이 저장소의 시연 실행에 속하면 합성 경보·인공 끊김을 쓰지 않고, 그 실행의 시연 경보(`initial_events`·`events`)와 시연 이동 경로(`demoElapsedMs`) `world_states`만 돌려준다.
   - 지난 날(11-18·19) 등 시연 실행 기록이 없는 구간은 기존 동작을 그대로 유지한다.
4. **사건 수 일치 (`src/mocks/work.ts`, `src/mocks/history.ts`)**
   - 오늘 작업의 `event_counts`·`incident_counts`·종료 미팅 사건 수가 같은 저장소의 발생(`RAISED`) 집계(위험 2·주의 2)를 읽도록 묶어 달력·월 요약·작업 목록·보고서 수를 일치시킨다.
5. **작업 시작 전 단일 시연 작업자 (`src/mocks/startBackend.ts`, `src/mocks/scenario/ppe.ts`)**
   - 목업 빌드에서 작업 시작 전에도 `demoElapsed` 를 `0` 으로 내려 `world_state` 와 `ppe_view` 가 시연 작업자(`w-0012` 추창만) 1명만 송출하도록 한다. 다른 작업자(`이영희` 미착용 이름표·`작업자 #1`)는 나타나지 않는다.

## 검증
- `tsc --noEmit` 및 `mocks`/`features/history`/`features/work` 단위 테스트 통과.
- 목업 빌드(`vite build --mode mock`) 후 preview 4347에서 `demo/demo` 전체 흐름(작업 준비→TBM→세팅 재사용→구역 수락→서명→작업 시작 45초→종료→제출→달력·오늘 다시 보기)을 Playwright로 확인하고 스크린샷 촬영.
