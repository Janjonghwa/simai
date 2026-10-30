# EC2 이벤트 전이 수신과 재동기화

S15P31A101-106. 명세 3.2(이벤트 소유권·재동기화 규칙·저널 계약), 6.4, 12.3, 계약 `risk_event_transition`·`event_state_snapshot`·`journal_ack`.
코드는 `server/.../domain/tier0/event/`. I1 연결·인증은 S15P31A101-103(`domain/tier0/i1/`).

## 흐름

1. Tier 0 가 I1 에 붙으면 연결마다 **동기화 모드**로 시작한다. 첫 저널 메시지에서 `journal_cursor` 의 `acked_offset` 을 읽는다.
2. Tier 0 는 마지막 ack 다음부터 전이를 다시 보내고(`resent=true`), `event_state_snapshot` 을 한 번 보낸 뒤 실시간 전이를 보낸다.
3. EC2 는 전이마다 `risk_event_update` 에 기록하고(추가만), 그 이벤트에서 가장 높은 `event_sequence` 일 때만 `risk_event` 에 반영한다.
4. 스냅샷을 받으면 빠진 구간이 없는지 보고, 없으면 동기화를 끝내며 이벤트의 `delivery` 를 정한다. 그 뒤 전이는 실시간이다.
5. EC2 는 `journal_ack` 로 "빈틈없이 저장한 마지막 offset" 을 알린다. 빠진 구간은 `resend_from_offset` 으로 다시 요청한다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| "최근 유효 관측 근거" | 스냅샷의 `last_valid_observation_at` 이 있으면 DELAYED_ACTIVE, null 이면 AUDIT_ONLY(활성으로 두되 알리지 않음). 시간 창은 두지 않는다 | 3.2 "경과 시간만으로 억제하지 않는다". 수치 기준이 명세에 없다 |
| 동기화 중 받은 전이 | `delivery_mode` AUDIT_ONLY 로 기록. 이벤트 `delivery` 는 스냅샷 때 정한다 | `delivery_mode` 는 NOT NULL 이고 기록은 추가만이라 나중에 바꾸지 않는다 |
| 동기화 중 바뀌었고 스냅샷에 없는 이벤트 | AUDIT_ONLY | 그사이 해소된 이벤트의 실시간 재알림 0(G3 D-G3-05) |
| 동기화 중 바뀌지 않은 이벤트 | `delivery` 를 그대로 둔다 | 끊기기 전에 이미 알렸다 |
| 스냅샷의 활성 이벤트가 EC2 에 없음 | 스냅샷 값으로 `risk_event` 를 만들고 같은 규칙으로 분류(`metrics` 는 `{}`) | 저널 정리로 전이를 받을 수 없는 경우 |
| 기준 offset | 저장된 커서가 없으면 그 연결에서 처음 받은 offset 바로 앞. 전이 없이 스냅샷만 오면 스냅샷 offset | Tier 0 는 연결할 때 자기 acked 다음부터 보낸다 |
| 빈 구간 판단 | `journal_offset` 기준(전역·연속). 이벤트별 sequence 빈칸은 따로 보지 않는다 | `journal_ack` 는 offset 으로만 다시 요청한다 |
| 늦게 온 낮은 sequence | 기록만 하고 현재 상태는 바꾸지 않는다. 부모 이벤트가 없으면 그 전이로 만든다(FK) | 3.2 |
| ack 시점 | 빈 구간을 찾았을 때·스냅샷 때는 바로, 그 밖에는 나아간 acked 를 1초마다 | Tier 0 는 ack 를 기다리지 않지만 ack 가 없으면 저널을 정리하지 못한다 |
| 저널이 비었을 때(스냅샷 offset null) | ack 를 보내지 않고 EC2 쪽만 동기화를 끝낸다 | `acked_offset` 은 0 이상이어야 하고 Tier 0 는 저널 끝을 넘는 ack 를 거부한다 |
| `journal_cursor.acked_offset` 의 -1 | 아직 받은 것이 없다는 테이블 안의 표시. Tier 0 에 보내지 않는다 | 컬럼이 NOT NULL |
| `ack_id`·`close_id` | 그 행이 이미 있을 때만 잇고 없으면 null | 현장 버튼 확인(`event_ack`)은 S15P31A101-121 |
| `related_event_id`(기록) | 받은 값을 그대로 저장. 현재 상태(`risk_event`)에는 그 이벤트가 있을 때만 잇는다 | `risk_event_update.related_event_id` 에는 FK 가 없고 기록은 고치지 않는다 |
| 계약 위반 전이(형식·타입·읽을 수 없는 메이저) | 그 offset 을 건너뛴 것으로 치고 `simai.i1.journal.skipped` 를 세며 오류 로그. DB 장애는 건너뛰지 않는다 | 다시 받아도 같아서 빈 구간이 영원히 남고 동기화가 끝나지 않는다 |
| Tier 0 저널이 새로 시작됨(Pod 교체) | 같은 세대·offset 에 다른 `msg_id` 가 오거나, 스냅샷 offset 이 EC2 acked 보다 작거나 null 인데 EC2 가 받은 위치를 가지면 `journal_epoch` 를 올린다(V4). 스냅샷으로 알게 됐고 새 저널에 기록이 있으면 연결을 닫아 처음부터 다시 받는다 | 세대가 없으면 새 저널의 전이가 옛 기록과 겹쳐 조용히 버려진다 |
| 분류 시점 | 분류 트랜잭션이 커밋된 뒤에만 동기화를 끝낸다. 스냅샷의 활성 이벤트 중 `delivery` 가 아직 없는 것(끝나지 못한 이전 동기화)도 분류한다 | 분류가 실패하면 다음 스냅샷이 다시 분류한다 |
| 기준 offset 의 시점 | 저장 성공이 아니라 처음 **받은** offset 으로 잡는다 | 첫 offset 저장이 실패했을 때 다음 성공이 그것을 건너뛰고 ack 하지 않게 |
| `raised_at` | RAISED 를 받으면 sequence 순서와 상관없이 비어 있을 때 채운다 | RAISED 가 늦게 와도 처음 시각이 남게 |
| 커서 저장 시점 | ack 를 보낼 때만(1초 몰아 보내기·빈 구간·스냅샷). 만들기·저장·보내기를 연결마다 한 잠금으로 묶는다 | 쓰기를 줄이고, 두 스레드가 보낸 ack 순서가 뒤집히지 않게 |

## 관리자 VLM 상향 승인·거절과 Tier 0 반영 (S15P31A101-276)

명세 5.4 "운영자 확인 상향", 12.3(`event_escalate`·`event_escalate_result`), 12.6(`POST /api/events/{id}/escalation`).
계약: `event_escalate` 1.0.0, `event_escalate_result` 1.0.0, `operator_escalation`, `risk_event_transition` 2.1.0, `risk_event` 2.1.0.
코드: `EventController`·`EventEscalationService`·`EventQueryRepository`(`domain/event/`), `EventEscalationForwarder`·`EventEscalateResultIngestor`·`RiskEventRepository`·`EventStateSnapshotIngestor`(`domain/tier0/event/`), `Tier0Relay`(`domain/realtime/`), `V19__event_escalation.sql`.

### 1. 흐름

1. **관리자 결정(`POST /api/events/{eventId}/escalation`, `ADMIN` 전용)**:
   - 본문 `{ assessmentId, decision: "APPROVED" | "REJECTED", note? }`. `assessmentId` 는 `vlm_assessment.assessment_id`(숫자) 또는 `job_id`(문자열)를 모두 받는다.
   - **거절(`REJECTED`)**: `vlm_assessment` 에 `decision = 'REJECTED'`, `decided_by`, `decided_at` 을 기록하고 `risk_event.proposed_severity = NULL` 로 내린 뒤 `/topic/events` 로 발행한다. `event_escalation` 생성이나 I1 `event_escalate` 송신은 하지 않으며 `risk_event.severity` 는 그대로다.
   - **승인(`APPROVED`)**: 현재 활성 이벤트(`lifecycle <> 'RESOLVED'`)이고 `proposed_severity > severity` 일 때만 받는다. `vlm_assessment(decision='APPROVED')` 와 `event_escalation` 행(`escalation_id`, `basis='VLM_PROPOSAL'`, `target_severity`, `actor_user_id`, `requested_at`, `note`)을 커밋한 뒤, 열린 I1 연결로 `event_escalate` 1.0.0 을 보낸다. **EC2 는 승인 시점에 `risk_event.severity` 를 직접 바꾸지 않는다**(AGENTS.md 규칙 4).
2. **Tier 0 결과(`event_escalate_result` 1.0.0) 및 저널 전이(`risk_event_transition` 2.1.0)**:
   - Tier 0 가 상향을 저널에 커밋하면 `event_escalate_result(ACCEPTED, event_sequence)` 와 같은 `event_id` 의 `UPDATED` 전이(`escalation` 객체 포함, 변경 사유 `SEVERITY`·`ESCALATED`)를 보낸다.
   - `RiskEventRepository` 는 전이를 `risk_event_update` 에 기록하고(`escalation_id` 연결), `event_escalation` 에 저널 반영(`ACCEPTED`, `event_sequence`)을 남기며, `risk_event.severity` 갱신과 함께 `risk_event.escalations` 배열에 시간순(`requested_at`, `escalation_id` 중복 제거)으로 누적 투영한다. `Tier0Relay` 는 `/topic/events` 로 `risk_event` 2.1.0(`escalations` 포함)을 발행한다.

### 2. `event_escalate_result` 결과 코드 처리 표

| `result` | `reject_reason` | `event_sequence` | EC2 처리 (`EventEscalateResultIngestor` / `EventEscalationForwarder`) | 재시도 여부 |
|---|---|---|---|---|
| `ACCEPTED` | 없음(금지) | 필수(`>= 1`) | `event_escalation` 에 `result='ACCEPTED'`, `event_sequence`, `decided_at` 저장. 등급·`escalations` 투영은 함께 오는 `UPDATED` 저널 전이가 반영 | 재시도 안 함 |
| `DUPLICATE` | 없음(금지) | 선택(`>= 1`) | 이미 `ACCEPTED` 면 그대로 유지하고, 아니면 `result='DUPLICATE'`, `reject_reason=NULL`, `decided_at` 저장(멱등 완료) | 재시도 안 함 |
| `REJECTED` | `NOT_ACTIVE` | 없음(금지) | 그사이 Tier 0 에서 해소·종료된 이벤트. `result='REJECTED'`, `reject_reason='NOT_ACTIVE'`, `decided_at` 저장 및 남은 `proposed_severity` 해제 | 재시도 안 함 |
| `REJECTED` | `NOT_UPWARD` | 없음(금지) | 그사이 규칙 판정 등으로 이미 같거나 더 높은 등급이 됨. `result='REJECTED'`, `reject_reason='NOT_UPWARD'`, `decided_at` 저장 및 남은 `proposed_severity` 해제 | 재시도 안 함 |
| `REJECTED` | `JOURNAL_UNAVAILABLE` | 없음(금지) | Tier 0 저널 일시 오류로 상태 변경 없이 거부됨. `result='REJECTED'`, `reject_reason='JOURNAL_UNAVAILABLE'` 저장 | **같은 `escalation_id` 로 재시도** (주기 재시도 및 스냅샷 동기화 직후) |

### 3. 9차 GPU 세션 확인 방법

1. RunPod Tier 0 + 분석 워커(`vlm_rationale`)가 연결된 상태에서 `HIGH` 또는 `CAUTION` 이벤트에 VLM 상향 의견(`proposed_severity: "CRITICAL"`)이 달리면 관제 화면 상세에서 관리자 계정으로 **승인**을 누른다.
2. EC2 로그에서 `event_escalate` 송신 → Tier 0 `event_escalate_result(result=ACCEPTED, seq=...)` 수신 → Tier 0 `risk_event_transition(UPDATED, severity=CRITICAL, escalation)` 수신 순서와 지연을 확인한다.
3. `GET /api/events/{id}` 및 `/topic/events` 메시지에서 `severity == "CRITICAL"`, `proposed_severity == null`, `escalations[0]`(`escalation_id`, `assessment_job_id`, `target_severity`, `actor_id`, `requested_at`, `note`)와 현장 상향 재경보(`local_alert`)·클립 자동 보존(`retention_hold = true`)이 함께 반영되는지 확인한다.

## 남은 일

- `/topic/events` 발송은 S15P31A101-119(`server-stomp-relay.md`), Web Push 는 M1.
- 운영자 확인(S15P31A101-121): `POST /api/events/{id}/ack` 가 `event_ack` 를 저장하고 I1 `event_ack` 로 보낸다. Tier 0 가 끊겨 있으면
  저장만 하고, 다시 붙어 동기화가 끝나면 아직 저널에 잡히지 않은 확인(10분 안, 해소 안 된 이벤트)을 같은 `ack_id` 로 다시 보낸다.
  ACKNOWLEDGED 전이의 확인은 `event_ack` 에 없으면 만들고(현장 버튼), `journal_event_sequence` 를 채운다.
- 스냅샷에 없는데 EC2 에서 활성인 이벤트는 경고 로그만 남긴다(EC2 는 전이를 만들지 않는다).
