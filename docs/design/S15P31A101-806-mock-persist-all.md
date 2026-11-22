# 연습 목업 상태 전체 저장 (S15P31A101-806 후속)

연습용 목업(`vite build --mode mock`, VITE_MOCK_BUILD=true)에서 새로고침·새 탭·주소 직접 입력 뒤에도 연습이 이어지게 한다. 예전에는 msw 목업 상태가 메모리에만 있어서 /live 를 새로고침하면 「오늘 세팅이 끝나지 않았습니다」가 뜨고 작업 관리는 빈 TBM 보고서부터 다시 시작했다. -806(!760·!762)은 저장 도구(`mocks/mockPersist.ts`)만 만들었고, 실제로 쓰는 곳은 GPU mock 하나였다.

## 코드 위치

- `front/apps/admin/src/mocks/mockSession.ts`: 저장·복원을 맡는다. 부분마다 검사한 뒤 적용하고, 짧게 기다렸다 저장한다. 모양 검사 도우미와 base64 변환도 여기 있다.
- 각 mock 의 `persist`(`exportState`·`loadState`):
  - `work.ts`: 날짜 작업(상태·실행 runs·actual_started/ended·setupDoneAt·초안·보고서 양식), 보고서별 서명, 번호
  - `tbm.ts`: 오늘 작업 셋, 레이아웃·리비전, 올린 서명 이미지(base64)
  - `setup.ts`·`setupSupplement.ts`: 세팅 세션(단계·맵 선택·카메라 확인 걷기), 구역 목록·초안, 맵 보강 파일·다시 굽기
  - `zones.ts`: 구역 버전, 구성 묶음, 함께 바뀌는 작업면 저장소(`staticObjectStore` 의 snapshot/restore)
  - `events.ts`: 이벤트 저장분, 상향 결정, 구간 요약
  - `demoReplayStore.ts`: 시연 실행 구간과 경보 전이(다시 보기·사건 수)
  - `peopleNames.ts`: 워치 등록·배정·이름 이력, 날짜별 미확인 번호
- `demoAlarmScenario.ts`: 스케줄러를 작업 시작 시각 기준으로 바꾸고 `resume`·`exportState` 를 더했다.
- `scenario/shared.ts`: msg_id 번호를 내보내고 이어 쓰는 함수(`msgIdSerial`·`resumeMsgIdSerial`)를 더했다.
- `startBackend.ts`: 부분을 모아 부팅 때 복원하고, 저장 시점을 건다.
- `mockPersist.ts`: `clearMockState` 뒤에는 그 페이지에서 더 쓰지 않는다.
- 시험: `src/__tests__/mocks/mockSession.test.ts`

## 저장 키·형식

- 키는 `simai-mock:session` 하나다. 값은 `{ version: 1, day: 'YYYY-MM-DD'(Asia/Seoul), saved_at, parts: { zones, setup, work, tbm, events, demoReplay, peopleNames, demo } }` 이다. GPU 는 예전 키(`simai-mock:gpu`)를 그대로 쓴다. 연습 빌드는 `alwaysOn` 이라 그 키도 읽지 않는다.
- Map·Set 은 `[키, 값]` 배열로, 서명 이미지(Uint8Array)는 base64 로 바꿔 남긴다. Date 객체는 쓰지 않는다. 시각은 모두 ms 숫자다.
- 「연습 처음부터 다시」는 지금처럼 `simai-mock:` 접두어 키를 모두 지우므로 새 키도 함께 지워진다.

## 복원 순서

1. 판(`version`)이 다르거나, 날짜가 오늘(서울)이 아니거나, JSON 이 깨졌거나, `parts` 가 객체가 아니면 키를 지우고 처음 상태로 시작한다.
2. 모든 부분의 `loadState` 를 먼저 부른다. `loadState` 는 상태를 바꾸지 않고 검사만 한 뒤 적용 함수를 돌려준다. 하나라도 빠졌거나 모양이 틀리면 키를 지우고 아무것도 적용하지 않는다. 부분끼리 어긋나지 않게 전부 적용하거나 하나도 적용하지 않는다.
3. 적용 순서는 작업면·구역 → 세팅 → 작업·TBM → 이벤트·다시 보기 → 사람 이름 → 시연이다. 시연을 마지막에 둔 것은 작업이 진행 중인지와 다시 보기 기록을 읽기 때문이다.
4. 화면과 다른 mock 이 참조를 나눠 쓰는 배열·Map(`tbmMock.works`, `setupMock.sessions` 등)은 새로 만들지 않고 안을 바꾼다.
5. 복원은 `network.enable()` 전에 끝난다. 처음 요청부터 되살린 상태로 답한다.

## 저장 시점

- 응답을 흉내 낼 때마다(`response:mocked`) 300 ms 를 기다렸다 한 번 저장한다. GET 도 넣는다. 세팅 단계와 초안 진행은 조회할 때의 시각으로 앞으로 가기 때문이다.
- 작업 시작·종료는 바로 저장한다.
- 시연 경보를 보낼 때마다 300 ms 를 기다렸다 저장한다.
- `pagehide`, 그리고 `visibilitychange` 가 hidden 이 될 때 기다리던 저장을 바로 한다.
- 「연습 처음부터 다시」 뒤에는 `clearMockState` 가 그 저장소를 표시해 두고 `writeMockState` 가 더 쓰지 않는다. 표시가 없으면 새로고침 직전의 `pagehide` 저장이 지운 상태를 되살린다. 화면 확인에서 실제로 이 문제를 찾아 고쳤다.

## 시연 경보 시나리오 이어 가기

- 스케줄러는 실행마다 `{ startedAt, base }` 를 갖는다. base 는 event_id 끝 번호의 시작점이다. 단계 i 의 event_id 는 `base + i + 1` 이다. 첫 실행은 예전처럼 1~4 다.
- `start(startedAt)` 은 작업 시작 응답의 `runs.at(-1).started_at` 을 받아, 각 단계를 `startedAt + delayMs`·`startedAt + resolveAtMs` 시각에 예약한다.
- 새로고침 뒤 `resume(state, lookup)` 은 같은 시작 시각으로 다시 계획한다.
  - 아직 오지 않은 경보와 해소는 남은 시간 뒤에 예약한다.
  - 이미 지난 경보는 다시 보내지 않는다. 화면에는 저장해 둔 이벤트가 나온다.
  - 지난 경보가 아직 해소 전이면 해소만 원래 시각에 예약한다.
  - 저장되기 전에 새로고침해서 저장분에 없는 지난 전이만, 원래 시각으로 한 번 채운다.
  - `lookup` 은 이벤트 저장소가 아니라 다시 보기 저장소(`demoReplayStore.latestEvent`)를 읽는다. 이벤트 저장소는 해소된 이벤트를 2분 뒤 지운다(KEEP_RESOLVED_MS). 그래서 이벤트 저장소를 보면 지난 경보를 다시 띄울 수 있다.
- 작업이 IN_PROGRESS 가 아니면(종료) `run` 을 비워 이어 가지 않는다. 다음 실행은 저장해 둔 `sequence` 다음 번호를 쓴다.
- 3D 사람 움직임은 `startRealtimeMock` 의 elapsed(`Date.now() - demoStartedAt`)를 쓴다. `demoStartedAt` 을 되살리면 같은 시작 시각 기준으로 이어진다.
- msg_id 번호도 남긴다. 새로고침 뒤 번호가 1부터 다시 시작하면 다시 보기 기록의 msg_id 와 겹칠 수 있기 때문이다.

## 운영·개발·시험 영향

- `mockStateStorage()` 가 null 이면(VITE_MOCK_BUILD 가 아님) `startBackend` 가 세션을 만들지 않는다. 개발 서버와 시험은 지금처럼 메모리만 쓴다. 단위 시험은 메모리 저장소를 직접 넘긴다.
- 스케줄러는 이제 개발 서버에서도 만들어진다. 하지만 `start` 는 연습 빌드의 작업 시작에서만 부르므로 하는 일은 없다.
- 운영 빌드에는 mock 코드가 들어가지 않는다. `main.tsx` 의 조건이 상수라 `startBackend` 동적 import 가 빠진다. `check-mock-build.mjs` 를 통과했고, `dist` 에서 `simai-mock:session` 문자열을 찾지 못했다.

## 버린 대안

- **mock 마다 키를 따로 쓰기**: 한 부분만 버려지면 작업은 IN_PROGRESS 인데 이벤트나 다시 보기는 비는 식으로 어긋난다. 키 하나에 모두 넣거나 모두 버리는 쪽을 골랐다.
- **변경 요청을 기록해 두었다 다시 재생하기**: 세팅·초안이 시각으로 진행해서 재생할 때 시계를 흉내 내야 한다. 서명 업로드처럼 본문이 있는 요청도 남겨야 한다. 스냅숏보다 깨지기 쉽다.
- **이벤트 저장소로 지난 경보 판정하기**: 해소된 이벤트를 2분 뒤 지우므로 오래 지난 뒤 새로고침하면 경보를 다시 띄운다.
- **IndexedDB**: 저장량이 작고(이벤트 4건, 세션 1~2개, 서명 이미지 몇 장) localStorage 로 충분하다. 동기 API 라 `pagehide` 에서도 확실히 쓴다.

## 남은 위험

- **localStorage 용량(보통 5 MB)**: 서명 이미지를 base64 로 남긴다. 서명을 수십 장 넣으면 차서 저장이 조용히 실패할 수 있다. 실패하면 앞서 남긴 값이 그대로 남는다. 보통 연습(서명 1~4장)에서는 수십 KB 다.
- **같은 연습을 탭 둘에서 동시에 열기**: 탭마다 따로 메모리를 갖고, 마지막에 저장한 탭이 이긴다. 맞추지 않는다.
- **새로고침 직전 300 ms 안의 변경**: 보통은 `pagehide` 저장이 잡는다. 브라우저가 강제로 끝나면 잃을 수 있다. 시연 경보는 이어 가기가 원래 시각으로 채운다.
- **구간 요약 진행**: 새로고침 전에 걸어 둔 끝 타이머는 사라진다. 그래서 복원할 때 QUEUED·RUNNING 요약의 끝을 다시 예약한다. 남은 시간이 아니라 처음부터 1.5 초를 다시 기다린다.
- **클립 mock(`clips.ts`)**: 클립 id 색인은 남기지 않는다. 새로고침 뒤 처음 요청할 때 새로 만든다. 화면도 새로 읽으므로 문제는 보지 못했다.
- **개발 콘솔 스위치**: `simaiMock.*` 의 거절·실패 흉내 같은 스위치는 남기지 않는다.
