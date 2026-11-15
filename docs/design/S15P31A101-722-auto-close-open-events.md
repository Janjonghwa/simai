# S15P31A101-722 Tier 0 가 사라진 뒤 열린 이벤트 마감(EC2 마감)

10-30~11-14 시험에서 `risk_event` 에 `lifecycle <> 'RESOLVED'` 가 3,937건 쌓였다. 작업 종료·GPU 세션 종료로 Tier 0 가 사라지면
그 이벤트의 RESOLVED 전이는 다시 오지 않는다(새 Pod 는 새 저널이다). 17차에 `OPERATOR_CLOSED`(actor `lead-cleanup`)로 손으로 닫았다.
이 티켓은 그 마감을 정식 경로로 만든다.

관련 명세: 3.2(이벤트 소유권·재동기화·복원 이벤트 자동 해소 금지), 6.4(생명주기·운영자 종료·병합 흡수 마감), 12.3·12.4(I1·I2 메시지),
13.4(이벤트 패널). 계약 묶음 v7.57.0, DB V79.

## 1. 결정

### 1.1 원칙

- Tier 0 가 살아 있으면(그 `gateway_id` 의 I1 연결이 열려 있으면) EC2 는 닫지 않는다. Tier 0 가 정본이다(AGENTS 규칙 1).
  - 작업 종료: Tier 0 가 종료 묶음(`ppe_policy.work_id` null)을 받아 그 tick 에 활성 이벤트를 관측 해소로 닫는다(-679 `stand_down`).
    `event_close` 를 따로 보내지 않는다. `event_close` 는 사람 대상·HIGH·CRITICAL·지금 판정 있는 이벤트를 거부하므로(6.4) 마감 경로로
    쓸 수 없고, -679 가 같은 일을 Tier 0 안에서 이미 한다.
  - Tier 0 가 들고 있지 않은 이벤트(이전 Pod·이전 저널): 그 Tier 0 의 재동기화 스냅샷을 받을 때 닫는다(1.2 NOT_IN_SNAPSHOT).
- Tier 0 가 없으면 EC2 가 닫되, 관측 해소가 아님을 `resolution` 으로 분명히 한다(EC2 마감). Tier 0 전이를 지어내지 않는다.

### 1.2 마감 종류(`server_close_resolution.kind`)

| 종류 | 언제 | trigger_ref |
|---|---|---|
| `SESSION_ENDED` | 그 `gateway_id` 로 등록한 GPU 세션이 하나 이상 있고 모두 끝났으며 살아 있는 연결이 없다. 세션 종료 커밋 뒤 + 주기 60 s | 마지막 GPU 세션 ID |
| `WORK_ENDED` | 작업 종료 커밋 뒤, 연결도 열린 GPU 세션도 없는 `gateway_id`(환경 변수 토큰으로 붙던 Tier 0 등) | work_unit ID |
| `NOT_IN_SNAPSHOT` | 살아 있는 Tier 0 의 `event_state_snapshot` 분류 때, EC2 활성인데 `active_events` 에 없음(같은 트랜잭션) | gateway_id |

- 끝난 GPU 세션 토큰으로 연 연결은 곧 끊기므로(GpuSessionSweeper) 살아 있는 연결로 치지 않는다(두 커밋 뒤 리스너의 순서와 무관).
- 열린 GPU 세션이 있으면(새 등록이 이전 세션을 밀어낸 SUPERSEDED) 닫지 않는다. 새 Tier 0 가 붙어 스냅샷을 보내면 NOT_IN_SNAPSHOT,
  붙지 못하고 세션이 끝나면 SESSION_ENDED 로 닫힌다. `gpu-session.gateway-id` 는 세션마다 같은 값이라 세션 하나만 보고 닫으면 안 된다.
- NOT_IN_SNAPSHOT 은 마지막 Tier 0 시각(`updated_at`, 없으면 `raised_at`)이 `snapshot_at` 보다 늦은 이벤트를 닫지 않는다
  (I1 은 연결마다 순서대로 처리하므로 일어나지 않아야 하지만 시계·순서 어긋남에 대한 방어). 시각을 모르면 닫는다.
- 스냅샷에 빈 구간이 있으면(다시 요청하고 돌아감) 분류 자체를 하지 않으므로 닫지 않는다(기존 흐름).

### 1.3 기록(추가만)

- 새 표 `risk_event_server_close`(V79): close_id, event_id, kind, gateway_id, trigger_ref, reason, lifecycle_before, last_event_sequence,
  closed_at. 행을 고치지 않는다. 다시 닫히면 새 행.
- `risk_event`(현재 상태 투영)에는 `lifecycle = RESOLVED`, `resolved_at = 닫은 시각(EC2 시계)`, `resolution = {kind, close_id, closed_at,
  reason, trigger_ref, last_event_sequence}` 만 쓴다. 검증 상태·등급·`event_sequence`·`updated_at` 은 그대로(관측 해소 아님).
- `risk_event_update`(Tier 0 전이 기록)에는 아무것도 넣지 않는다. 여기에 넣으려면 `journal_offset`·`msg_id` 를 지어내야 하고,
  "EC2 는 전이를 만들지 않는다"(3.2)를 깬다.
- 이미 RESOLVED 인 이벤트는 건드리지 않는다. 마감 SQL 은 대상 행을 `FOR UPDATE` 로 잡은 뒤 `lifecycle <> 'RESOLVED'` 를 다시 본다
  (Tier 0 RESOLVED 반영과 겹쳐도 한쪽만).
- 커밋 뒤 닫은 이벤트를 `/topic/events` 로 보낸다(RiskEventsDelivered). 워치 경보는 해소된 이벤트라 꺼진다(WatchAlertService 기존 규칙).

### 1.4 Tier 0 가 돌아오면 Tier 0 를 따른다

- 마감한 이벤트에 Tier 0 가 더 높은 `event_sequence` 의 RAISED·UPDATED 를 보내면 그대로 반영하고 마감 투영(`resolved_at`·`resolution`)을
  비운다(RiskEventRepository.APPLY). RESOLVED 를 보내면 Tier 0 의 관측 해소로 바뀐다(`resolution` null).
- 스냅샷에 활성으로 실리면 스냅샷 `lifecycle` 로 되돌리고(reopenServerClosed), 동기화 중에 바뀐 이벤트처럼 분류한다(근거 있으면
  DELAYED_ACTIVE 알림, 없으면 AUDIT_ONLY).
- 예: GPU 세션 없이 환경 변수 토큰으로 붙던 Tier 0 가 잠깐 끊긴 사이 작업이 끝나면 WORK_ENDED 로 닫히고, 다시 붙으면 스냅샷으로
  되돌아갔다가 종료 묶음 tick 의 RESOLVED 로 닫힌다.

### 1.5 계약: I2 `risk_event` 2.5.0 만 바꾼다

- 공통 `$defs/server_close_resolution` 신설, `risk_event.resolution` 의 `oneOf` 에 추가. 2.4.x 이하에 이 종류는 금지(if/then).
- I1 `risk_event_transition` 은 바꾸지 않는다(Tier 0 는 이 종류를 보내지 않는다. I1 LIVE 계약은 Lead 승인 대상).
- OPERATOR_CLOSED 를 재사용하지 않은 이유: `operator_resolution` 은 `channel CONTROL_ROOM`·`actor_role ADMIN`·관리자 `actor_id`·
  `event_close` 멱등 키를 요구하고 "관리자가 확인하고 닫았다"는 뜻이다. 자동 마감에 쓰면 누가 닫았는지를 지어내게 된다
  (17차의 `lead-cleanup` 은 이 칸을 억지로 채운 것). 화면도 운영자 종료와 Tier 0 소멸을 구분할 수 없다.

### 1.6 화면

- 「최근 해소」(EventPanel → `resolvedGroups`)에서 EC2 마감 이벤트를 뺀다. 「진행 중」에는 원래 없다(해소). 「지난 관제 · 확인 필요」는
  해소 전 이벤트만이라 마감 뒤 거기서도 빠진다.
- 상태 문구·타임라인은 '해소'·'운영자 종료' 대신 `작업 종료로 마감`·`GPU 세션 종료로 마감`·`Tier 0 기록 없음으로 마감`.
- 응답 검사(zod)가 새 종류를 받는다. 받지 않으면 목록 전체가 `INVALID_RESPONSE`(-664 선례)라 EC2·화면을 함께 배포한다.

### 1.7 다시 보기(REPLAY)

- 시각 t 의 활성 이벤트(ReplayRepository.ACTIVE_AT) 후보에 `closed_at > t` 인 마감 이벤트를 더했다. t 가 마지막 전이와 마감 사이면
  활성, 마감 뒤면 후보가 아니다. 전에는 해소 전이가 없는 이벤트가 마지막 전이 뒤 모든 시각에 활성으로 나왔다.

## 2. 코드·시험

| 내용 | 위치 |
|---|---|
| 마감 SQL(기록 + 투영 한 문장) | `server/.../tier0/event/ServerEventCloser.java` |
| 세션 종료·작업 종료·주기 트리거 | `server/.../tier0/event/OrphanEventSweeper.java`, `server/.../work/WorkEnded.java`(WorkSessionService.end 가 발행) |
| 스냅샷 분류(NOT_IN_SNAPSHOT·되돌리기) | `EventStateSnapshotIngestor.classify`, `RiskEventRepository.reopenServerClosed`·`APPLY` |
| 표 | `V79__risk_event_server_close.sql` |
| I2 판 | `Tier0Relay.RISK_EVENT_VERSION` 2.5.0 |
| 다시 보기 | `ReplayRepository.ACTIVE_AT` |
| 화면 | `front/apps/admin/src/features/events/{types,entries,format,incidents,incidentView,constants}.ts` |
| 시험 | `OrphanEventSweeperIntegrationTest`(SESSION_ENDED·열린 세션이면 그대로·WORK_ENDED), `RiskEventJournalIntegrationTest`(NOT_IN_SNAPSHOT·전이 안 만듦·해소된 이벤트 그대로·Tier 0 복귀 되돌리기), `StompRelayIntegrationTest`(새 Tier 0 붙으면 마감 + 2.5.0 검증), front `events.test.ts`(응답 검사·최근 해소 제외·문구), 계약 예시·반례 |

기존 시험 고침: `EventAckIntegrationTest`·`EventEscalationIntegrationTest` 의 스냅샷 도우미가 활성 이벤트를 비운 채 보내고 있었다(실제
Tier 0 는 들고 있는 이벤트를 싣는다). 이제 빠진 이벤트는 마감되므로 도우미가 그 시험의 이벤트를 근거 없음(AUDIT_ONLY, 전과 같은 분류)으로
싣는다. `StompRelayIntegrationTest` 의 "새 Tier 0 가 붙어도 이전 이벤트는 해소하지 않는다"(-595)는 이 티켓의 결정으로 뒤집혔다(4·6).

## 3. 명세에 없어 정한 값

| 값 | 근거 |
|---|---|
| 주기 60 s(`simai.event-close.sweep-interval`) | 관리 명령(다른 프로세스)이 끝낸 세션은 커밋 뒤 이벤트가 오지 않는다. 무응답 판정이 10분이라 60 s 면 충분하고 질의는 열린 이벤트의 gateway 수만큼 |
| `reason` 문구(한국어 한 줄, 500자 자름) | 계약 maxLength 500 |
| FK `ON DELETE CASCADE` | 운영은 `risk_event` 를 지우지 않는다(시험 정리·첫 운영 초기화뿐). 이벤트 없는 마감 기록은 뜻이 없고, 지우는 시험 17곳을 고치지 않는다 |

## 4. 버린 대안

| 대안 | 버린 이유 |
|---|---|
| OPERATOR_CLOSED 재사용 | 1.5 |
| 작업 종료 때 살아 있는 Tier 0 에 `event_close` 일괄 송신 | 사람 대상·HIGH·CRITICAL·지금 판정이 거부된다(6.4). -679 가 같은 tick 에 관측 해소로 이미 닫는다. 두 경로가 겹치면 거부 결과만 쌓인다 |
| EC2 가 `risk_event_update` 에 RESOLVED 행을 만듦 | journal_offset·msg_id 를 지어내야 하고 3.2 를 깬다. Tier 0 가 돌아오면 같은 sequence 가 충돌한다 |
| I1 `risk_event_transition` 에 새 종류 추가 | Tier 0 는 이 종류를 낼 일이 없다(Tier 0 가 없을 때의 일). I1 LIVE 계약 변경은 Lead 승인 대상 |
| -595 처럼 해소 전 이벤트를 「지난 관제 · 확인 필요」로 둔 채 둠 | 3,937건이 쌓였다. 해소 전이가 다시 올 수 없는 이벤트를 영원히 '확인 필요'로 두면 목록·워치·보고가 오염된다 |
| GPU 세션 하나가 끝날 때 그 gateway 의 이벤트를 무조건 닫음 | gateway_id 는 세션마다 같아 새 세션 Tier 0 의 이벤트까지 닫을 수 있다. 모든 세션이 끝났고 연결이 없을 때만 닫는다 |

## 5. 잔여 위험

- 환경 변수 토큰 Tier 0 가 잠깐 끊긴 사이 작업이 끝나면 WORK_ENDED 로 닫혔다가 다시 붙을 때 되돌아간다(1.4). 화면에 잠깐 해소로 보일 수 있다.
- 다시 보기 구간 전이(TRANSITIONS)에는 마감이 전이로 나오지 않는다. 재생 중 그 시각을 지나도 카드가 남고, 다음 시각 이동(ACTIVE_AT)에서 빠진다.
- 스냅샷 되돌리기(reopenServerClosed)는 전이 기록 없이 투영만 되돌린다. 근거는 마감 기록 + 이어지는 Tier 0 전이로 다시 찾을 수 있다.
- 마감한 이벤트 수만큼 `/topic/events` 를 보낸다. 3,937건 같은 백로그는 17차에 이미 정리했다. 다시 쌓여도 발행 대기열 한도(10,000) 안이다.

## 6. 명세 갱신 필요(명세 정리 MR 에 넘김)

- 6.4: "관측이 사라져 해소를 확인할 수 없으면 활성으로 남는다. 자동 해소하지 않는다"는 Tier 0 가 그 이벤트를 들고 있을 때의 규칙이다.
  Tier 0 가 들고 있지 않아(세션·작업 종료로 사라짐, 스냅샷에 없음) 해소 전이가 다시 올 수 없으면 EC2 가 `resolution` EC2 마감
  (WORK_ENDED·SESSION_ENDED·NOT_IN_SNAPSHOT)으로 닫는다를 운영자 종료·병합 흡수 마감 옆에 더한다.
- 3.2: "EC2 는 전이를 만들지 않는다"에 "EC2 마감은 Tier 0 전이가 아니라 EC2 기록(risk_event_server_close)과 투영 변경이다"를 덧붙인다.
  재동기화 규칙에 NOT_IN_SNAPSHOT·되돌리기를 더한다. 복원 이벤트 자동 해소 금지(Tier 0 가 들고 있음)와 충돌하지 않는다.
- 12.4: `risk_event` 2.5.0. `risk_event` 계약 설명의 2.3.0 문장 "이벤트 자체(lifecycle)는 바꾸지 않는다"(-595)는 들고 있지 않음 표시에
  대한 것이고, 2.5.0 설명에 EC2 마감이 유일한 예외임을 적었다.
- 13.4: 「최근 해소」는 관측 해소·운영자 종료·병합 흡수만, EC2 마감은 빼고 문구를 구분한다.
- `docs/design/db-tables.md` E절에 `risk_event_server_close`.
- 차이 보고: -595 설계(`docs/design/S15P31A101-595-stale-live-events.md`)의 "새 Tier 0 가 붙어도 이전 이벤트는 해소하지 않고 「지난 관제」로 내린다"를 이 티켓이
  "들고 있지 않음으로 내리고 EC2 마감으로 닫는다"로 바꿨다.
