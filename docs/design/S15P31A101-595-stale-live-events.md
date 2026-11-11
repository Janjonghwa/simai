# S15P31A101-595 꺼진 관제에서 열린 채 남은 사건을 '진행 중'에서 빼기

## 문제

- 작업도 GPU(Tier 0)도 꺼져 있는데 관제 화면 이벤트 패널에 「진행 중」 309건이 보였다.
  - 40시간쯤 전 11-09 세션의 이벤트다. 운영 DB 에는 해소 전(lifecycle RAISED·UPDATED)인 `risk_event` 가 10-30~11-09 사이 3,891건 있다.
  - 같은 목록을 쓰는 상단 경보 배너·경보음·종(소리 버튼)·2D 평면도 그 이벤트로 울리고 표시됐다.
- 원인: 이벤트는 Tier 0 의 RESOLVED 전이로만 닫힌다. EC2 는 전이를 만들지 않는다(AGENTS 규칙 4, `EventCloseService` 설명).
  GPU Pod 를 멈추면 그 Pod 가 열어 둔 이벤트는 영원히 활성으로 남는다.
- 화면만 고쳐서는 안 된다. 저널이 빈 새 Tier 0 Pod 가 붙으면 화면은 옛 이벤트와 지금 이벤트를 구분할 근거가 없다.

## 관련 명세

- 3.2 이벤트 소유권과 outbox, 재동기화 알림 규칙, 스냅샷 만들기, 재시작 복구: "복원한 활성 이벤트는 … 자동 해소하지 않으며 운영자 확인을 요청한다", "EC2 는 전이를 새로 만들지 않는다". 이 규칙은 그대로 둔다.
- 3.2 이중 감시, 3.10 노트북 장애 판단: 하트비트 1초 주기, 3회 누락(3초)을 끊김으로 본다.
- 6.4 RiskEvent 생명주기, 운영자 종료: 상세 화면과 운영자 종료 동작은 바꾸지 않는다.
- 12.4 `/topic/events`·`/topic/health` 화면 규칙, 12.6 `GET /api/events`·`GET /api/events/{id}`.
- 13.2 상태 표시 규칙, 13.4 실시간 관제 화면(이벤트 패널).

## 정한 규칙(사용자 승인 안 A)

데이터는 바꾸지 않는다. 자동 해소도, `risk_event` 쓰기도 없다. 화면에 보이는 분류만 바꾼다.

- **「진행 중」:** 해소 전 LIVE 이벤트 중 지금 연결된 Tier 0 가 들고 있는 것.
- **「지난 관제 · 확인 필요 N건」:** 그 밖의 해소 전 이벤트. 이벤트 패널 끝에 처음에는 접힌 칸으로 둔다. 펼쳐 볼 수 있고, 상세 화면과 운영자 종료는 지금과 같다. N 은 사건(사람 묶음) 수가 아니라 이벤트 수다.
- **경보 배너·경보음·종·2D 평면·패널 머리줄 사건 수:** 「진행 중」만 쓴다.

### 들고 있음(held) 판정 — EC2 `Tier0HeldEvents`

- 집합: Tier 0(gateway_id)마다 event_id 집합을 메모리에 둔다.
  - 스냅샷(`event_state_snapshot`)을 분류할 때 `active_events` 로 통째로 바꾼다. 빈 구간 재요청이나 새 저널 재연결로 분류 전에 돌아가는 스냅샷은 쓰지 않는다.
  - 스냅샷 뒤(동기화 모드가 끝난 뒤) 현재 상태를 바꾼(APPLIED) 실시간 전이로 고친다. RESOLVED 는 빼고, RAISED·UPDATED 는 넣는다. ACKNOWLEDGED 는 그대로 둔다.
  - 동기화 중 재전송 전이와 늦게 온 낮은 sequence(HISTORY_ONLY)는 보지 않는다. 이어지는 스냅샷이 맞춘다.
- 들고 있음 = 집합에 있음 **그리고** 그 Tier 0 의 I1 메시지(종류 무관)를 마지막으로 받은 지 유예 시간 안. 수신 시각은 받는 쪽 단조 시계로 잰다(`I1ConnectionRegistry.received`). 저장 성공 여부와 상관없이 받은 것만으로 센다.
- 끊김 처리:
  - (a) 실제 세션 중 짧은 I1 끊김: 집합을 그대로 두고 유예 시간 동안 들고 있음을 유지한다. 경보·배너가 꺼지지 않는다. 같은 Tier 0 가 다시 붙어 스냅샷을 보내면 그것으로 바꾼다(그사이 해소된 것은 빠진다).
  - (b) GPU 꺼짐: 유예 시간이 지나면 1초 주기 작업이 그 Tier 0 의 집합을 비운다. 조회는 주기 작업 전에도 이미 내려간 값을 준다. 같은 Tier 0 가 늦게 다시 붙어도 스냅샷 전에는 비어 있다(옛 집합을 되살리지 않는다).
- EC2 재시작: 집합은 비어 있다. Tier 0 가 다시 붙어 스냅샷을 보낼 때까지 모든 해소 전 이벤트는 「지난 관제」다(받아들인 동작, 아래 남은 위험).
- 파생값이 실리는 곳: `held_by_tier0 = lifecycle ≠ RESOLVED && isHeld(gateway_id, event_id)`. `EventQueryRepository.toPayload` 한 곳에서 만든다. 그래서 목록(`GET /api/events`)·상세·확인 응답·`/topic/events` 가 같은 값을 쓴다.

### 유예 시간 60초의 근거

- 끊김 판정은 3초(1초 주기 × 3회 누락, 명세 3.2·3.10)다. 하지만 3초는 "끊겼다"이지 "다시 붙을 수 없다"가 아니다.
- Tier 0 I1 클라이언트(`ai/services/tier0/i1_client.py`) 값:
  - 반쯤 끊긴 연결 감지: websockets ping 10초 + pong 기다림 10초 = 최대 20초.
  - 재접속 대기(`Backoff` 1·2·4…최대 30초)와 접속 시간 제한 10초.
- 반쯤 끊김 감지(20초) + 첫 재접속(1초 + 10초) + 두 번째 재접속(2초 + 10초) = 43초. 여기에 재전송·스냅샷 여유를 둬 **60초**로 정했다.
- GPU 를 끄면 늦어도 60초(+ 주기 1초)에 「진행 중」이 0이 된다. 그동안도 viewer_health 는 3초 만에 Tier 0 DISCONNECTED·UNSAFE_UNKNOWN 을 알려 장애 띠가 뜬다.
- 설정: `simai.events.held-grace-ms`(기본 60000, 0 이하는 기동 실패).

### 화면에 바뀐 것을 알리는 방법

- 전이 하나로 바뀐 것은 그 전이의 `/topic/events`(이미 있던 알림)가 바뀐 `held_by_tier0` 를 싣는다. 알림보다 먼저 집합을 고친다.
- 전이 없이 한꺼번에 바뀌는 것(스냅샷으로 집합이 달라짐, 유예 만료, EC2 재시작)은 이벤트마다 보내지 않는다. 대신 `viewer_health.held_events_revision`(판, `<EC2 기동 ms>-<번호>`)이 바뀌고, 관제 앱은 판이 바뀌면 목록을 다시 받는다. 같은 집합의 스냅샷은 판을 바꾸지 않는다.
- 이렇게 한 이유:
  - `AUDIT_ONLY` 이벤트는 `/topic/events` 로 보내지 않는다(명세 3.2, G3 D-G3-05). 그래서 이벤트마다 보내는 방식으로는 그런 이벤트의 표시를 고칠 수 없다.
  - 목록 다시 받기는 지금도 연결이 다시 열릴 때 쓰는 경로라 새 경로가 없다.
  - 판은 1초마다 오는 health 에 실려 따로 토픽을 만들지 않는다.
- 관제 앱 합치기 규칙(`entries.mergeEvent`): 같은 event_sequence 를 다시 받으면 `held_by_tier0` 만 받은 값으로 바꾼다. 늦게 온 낮은 sequence 로는 되돌리지 않는다. 칸이 없는 항목(이전 EC2)은 바꾸지 않는다.
- 관제 앱은 칸이 없으면 들고 있다고 본다(`isHeld`). 경보를 놓치지 않는 쪽이다.

## 계약

- 묶음 **v7.27.0**(마이너, v7.26.0 -593 다음). origin/dev 와 최근 원격 브랜치에서 v7.27.0 이 비어 있음을 확인했다.
- `messages/risk_event` 2.3.0: 선택 `held_by_tier0`(boolean, LIVE 만).
  - 불변 규칙 둘: LIVE 가 아니면 싣지 않는다. true 는 RAISED·UPDATED 에만.
  - 예시 `risk_event.held_by_tier0`·`risk_event.stale_unheld`, 반례 `invalid/risk_event.held_resolved`·`invalid/risk_event.held_on_replay`.
  - EC2 `Tier0Relay` 의 `/topic/events` schema_version 을 2.1.0 → 2.3.0 으로 올렸다. 2.2.0(PPE_UNKNOWN 값)도 이미 실릴 수 있었다.
- `messages/viewer_health` 1.4.0: 선택 `held_events_revision`(문자열 1~64). 예시 `viewer_health.held_events`.
- I1 LIVE 계약(`risk_event_transition`·`event_state_snapshot` 등)은 바꾸지 않았다.
- 다시 만든 것: Python 생성 모델, front `@simai/contracts` 생성 타입·검증기.

## 코드·시험 위치

- server
  - `domain/tier0/event/Tier0HeldEvents.java`(새 파일): 집합·유예·판. 시험 `Tier0HeldEventsTest`(단위).
  - `domain/tier0/i1/I1ConnectionRegistry.java`·`Tier0I1WebSocketHandler.java`: gateway 별 마지막 I1 수신 시각.
  - `domain/tier0/event/EventStateSnapshotIngestor.java`: 분류 전에 `replace`.
  - `domain/tier0/event/RiskEventTransitionIngestor.java`: 실시간 APPLIED 전이를 알리기 전에 `apply`.
  - `domain/event/EventQueryRepository.java`: `held_by_tier0` 투영.
  - `domain/health/ViewerHealthService.java`(1.4.0)·`HealthPublisher.java`(shape 에 판 포함).
  - `domain/realtime/Tier0Relay.java`: risk_event 2.3.0.
  - 통합 시험 `StompRelayIntegrationTest`(DELAYED_ACTIVE·실시간 전이의 held true, 저널이 빈 새 Tier 0 가 붙으면 해소 없이 held false·판 변경·이벤트별 알림 없음), `HealthIntegrationTest`(1.4.0·판).
- front(`apps/admin`)
  - `features/events/entries.ts`: `isHeld`·`isCurrent`·`isStale`, `isAlarming` 은 진행 중만, 같은 sequence 의 held 갱신.
  - `features/events/incidents.ts`: `eventSections` 에 `stale`·`staleEvents`. 지금 쪽과 지난 관제 쪽을 따로 묶는다(World ID 가 Tier 0 마다 다시 시작해 섞이지 않게). `incidentOf`·`withoutStale` 도 같다.
  - `features/events/EventPanel.tsx`: 접힌 「지난 관제 · 확인 필요 N건」 칸. 머리줄 수는 진행 중만 센다. 지난 관제만 남으면 "진행 중 이벤트 없음"을 보이고 패널을 접는다. 「모두 지우기」는 지난 관제도 지운다.
  - `IncidentCard.tsx`·`incidentView.ts`: 지난 관제 카드는 등급 테두리·점멸·카메라 버튼이 없고, 조용한 칩 「지난 관제」를 단다.
  - `alarmPolicy.ts`(소리·종)·`features/plan/plan.ts`(2D 평면)·`features/cctv/CamSplitPanel.tsx`: 지난 관제를 뺀다. 배너는 `isAlarming` 으로 따라온다.
  - `eventsCache.ts`: 판이 바뀌면 목록 다시 받기.
  - mock: 시나리오 이벤트에 held, 개발 서버 시드(진행 중 2·지난 관제 3, `seededEventsAt`), health 판, 히스토리 기록에서 held 제거.
  - 시험: `events.test.ts`·`EventPanel.test.tsx`·`AlarmBanner.test.tsx`·`useAlarmSound.test.ts`·`eventsCache.test.ts`·`plan.test.ts`·`mocks/events.test.ts`.

## 버린 대안

- **화면만, 시간으로 가르기(예: 마지막 갱신이 N분 넘으면 지난 관제):** 저널이 빈 새 Pod 가 붙은 직후나, 오래 이어지는 실제 경보(사람이 위험 구역에 계속 서 있음, 갱신 전이가 없음)를 가르지 못한다. 명세 3.2 "경과 시간만으로 알림을 억제하지 않는다"와도 맞지 않는다.
- **작업 종료·GPU 끄기 때 자동 해소:** EC2 가 전이를 만드는 것이라 AGENTS 규칙 4·명세 3.2(복원 이벤트 자동 해소 금지, EC2 는 전이를 만들지 않음)를 어긴다. 기록도 바뀐다(덮어쓰지 않는다 규칙 6).
- **viewer_health 에 들고 있는 event_id 목록을 싣기:** 1초 주기라 새 경보가 최대 1초 늦게 「진행 중」이 된다. 이벤트별 칸 + 판 방식은 새 경보를 `/topic/events` 로 바로 보인다.
- **판 대신 바뀐 이벤트를 하나씩 `/topic/events` 로 보내기:** AUDIT_ONLY 이벤트는 보낼 수 없어(3.2) 그 표시가 고쳐지지 않는다. 판이 바뀌면 목록을 다시 받는 쪽이 그런 예외가 없다.
- **I1 연결이 닫히는 순간만 보기:** GPU Pod 를 갑자기 멈추면 TCP 가 반쯤 열린 채 남아 EC2 가 닫힘을 오래 모를 수 있다. 마지막 수신 시각으로 잰다.
- **EC2 재시작 때 DB 의 활성 이벤트로 집합을 미리 채우기:** 꺼진 관제의 3,891건이 재시작마다 다시 울린다. 버렸다.

## 남은 위험

- **EC2 재시작 직후 공백:** 집합이 비어 있어, Tier 0 가 다시 붙어 스냅샷을 보낼 때까지(재접속 대기 최대 30초 + 재전송) 실제 경보도 「지난 관제」로 보이고 관제 화면 소리·배너가 멈춘다. 현장 노트북 경보(3.10)는 Tier 0 가 직접 내므로 영향이 없다. 스냅샷이 오면 판이 바뀌어 화면이 목록을 다시 받는다.
- **스냅샷이 오래 미뤄지는 새 Pod:** 옛 Pod 가 꺼진 지 60초 안에 새 Pod 가 붙어 health 를 보내면서 스냅샷을 미루면(명세 3.2 "근거를 모르는 값이 있으면 스냅샷을 미룬다"), 그동안 옛 집합이 들고 있음으로 남는다. 스냅샷이 오면 바로 맞춰진다.
- **지난 관제는 계속 쌓인다:** 운영자 종료는 Tier 0 결과가 있어야 끝나고 사람 대상·HIGH·CRITICAL 은 거부된다(6.4). 그래서 GPU 를 끈 뒤의 지난 관제는 화면에서 닫을 길이 없다. 「모두 지우기」로 사용자별로 숨길 수는 있다. 목록은 지금처럼 최대 5쪽(500건)만 받는다.
- **3D 뷰어 위험 표시:** `viewer3d` 는 `/topic/events` 로 받은 risk_event 를 Source 로 따로 쓰고 `held_by_tier0` 를 보지 않는다(3D 담당 영역이라 이번에 고치지 않음). 사람 표시는 world_state 가 끊기면 사라지지만, 구역 표시가 남는지는 확인하지 않았다.
- **여러 Tier 0:** 집합은 gateway_id 별이라 동작하지만, 관제 앱의 판은 하나다(어느 Tier 0 의 변경이든 목록을 다시 받는다). MVP 는 Tier 0 하나다.
- **목록 다시 받기와 실시간 전이의 경합:** 같은 sequence 끼리는 나중에 받은 것이 이긴다. 목록 응답이 그 뒤 실시간 전이보다 늦게 와도 sequence 가 낮아 되돌리지 않는다. 같은 sequence 의 목록 응답 둘이 엇갈리는 경우는 React Query 가 앞 요청을 취소해 막는다.

## 명세 문구 후속(명세 정리 MR 용, 이 MR 은 `docs/spec/` 을 고치지 않음)

- 3.2 재동기화 알림 규칙 뒤에 덧붙일 문구 제안: "EC2 는 Tier 0 별로 마지막 스냅샷과 그 뒤 실시간 전이로 접은 '들고 있는 이벤트' 집합을 메모리에 둔다. 그 Tier 0 의 I1 수신이 유예 시간(초기값 60초) 넘게 없으면 비운다. 관제 화면의 진행 중·경보는 이 집합에 있는 해소 전 LIVE 이벤트만이다. 나머지 해소 전 이벤트는 '지난 관제 · 확인 필요'로 보이며 자동 해소하지 않는다."
- 13.4 실시간 관제의 이벤트 패널 설명에 「지난 관제 · 확인 필요」 접힌 칸과 머리줄 수 규칙을 추가한다.
- 12.4 `/topic/events` 화면 규칙의 경보 대상에 "held_by_tier0 가 false 가 아님"을 추가하고, `/topic/health` 에 `held_events_revision` 을 추가한다.
- 6.4 운영자 종료: Tier 0 가 없는 지난 관제 이벤트를 정리하는 방법(예: 관리자 '확인 완료로 숨김' 기록)은 미결로 남긴다.
