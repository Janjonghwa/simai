# 운영자 종료 API (S15P31A101-221)

감시 확보 근거가 오지 않아 자동 해소되지 않는 구역 대상 이벤트(카메라 철거, 구역 제거, 재시작 복원)를 관제 화면 관리자가 닫는다.
EC2 는 이벤트를 직접 닫지 않고 I1 `event_close` 로 Tier 0 에 요청한다. 종료는 Tier 0 저널의 RESOLVED 전이(`resolution.close_id`)로 반영된다.

## 근거

| 출처 | 내용 |
|---|---|
| 명세 6.4 운영자 종료 | 구역 대상만, 검증 상태 불변, 사람 대상·HIGH·CRITICAL·조건 성립은 거부, 같은 명령은 한 번만 기록 |
| 명세 12.6 | `POST /api/events/{id}/close`, 관리자, 본문 사유 1~500자·요청 멱등 키, 결과 `event_close_result` 를 돌려줌 |
| 명세 16장 "운영자 종료 멱등 키" | REST 멱등 키는 `event_close.idempotency_key`(유일)에 따로, `close_id` 는 EC2 발급 |
| FR-78 | 관측 근거로만 해소, 운영자 종료 예외 |
| 계약 | `contracts/messages/event_close.schema.json` 1.0.0, `event_close_result.schema.json` 1.1.0 |
| 표 | `event_close`(V1, `docs/design/db-tables.md`). 이번에 표는 바꾸지 않았다 |

## 코드

| 파일 | 하는 일 |
|---|---|
| `server/.../domain/event/EventController.java` | `POST /api/events/{id}/close` (관리자) |
| `server/.../domain/event/EventCloseService.java` | 검증, 멱등 키 처리, 행 저장, 커밋 뒤 송신, 결과 대기, 응답 |
| `server/.../domain/tier0/event/EventCloseForwarder.java` | I1 `event_close` 송신, 재연결 동기화 뒤·10초 주기 다시 보내기 |
| `server/.../domain/tier0/event/EventCloseResultIngestor.java` | I1 `event_close_result` 검증·기록, 커밋 뒤 기다리는 요청 깨우기 |
| `server/.../domain/tier0/event/EventCloseWaiters.java` | close_id 별 결과 대기 |
| `server/.../domain/tier0/event/EventStateSnapshotIngestor.java` | 동기화가 끝나면 확인·상향과 함께 종료도 다시 보냄 |

## 흐름

1. 관리자가 `{"reason": "...", "idempotency_key": "..."}` 를 보낸다.
2. 같은 키의 행이 있으면 새 행·새 I1 없이 그 행의 지금 결과를 돌려준다(같은 관리자·같은 이벤트일 때만, 아니면 409).
3. 없으면 `risk_event`(LIVE)를 잠그고 없으면 404, RESOLVED 면 409. `close_id` 를 발급해 `event_close` 에 넣는다(`result` null).
4. 커밋 뒤 그 이벤트의 Tier 0 연결로 I1 `event_close` 를 보내고 `event_close_result` 를 최대 3초 기다린다.
5. 결과가 오면 200 (ACCEPTED·DUPLICATE·REJECTED, 거부 사유 그대로). 연결이 없거나 시간 안에 안 오면 202 (`result` null).
6. Tier 0 가 ACCEPTED 뒤 RESOLVED 전이(`resolution.close_id`)를 저널로 보내면 `RiskEventRepository` 가 `risk_event_update.close_id` 로 잇고 이벤트가 닫힌다.

응답 본문(snake_case, 시각은 epoch ms):

```json
{
  "close_id": "uuid", "event_id": "uuid", "reason": "...", "requested_at": 1760342460000,
  "result": "ACCEPTED | DUPLICATE | REJECTED | null",
  "reject_reason": "NOT_ACTIVE | TARGET_NOT_ZONE | SEVERITY_NOT_ALLOWED | CONDITION_PRESENT | JOURNAL_UNAVAILABLE | null",
  "event_sequence": 4, "decided_at": 1760342460030
}
```

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 멱등 키 위치·이름 | 본문 `idempotency_key`(별칭 `idempotencyKey`), 1~128자, 앞뒤 공백 제거 | 12.6 이 "본문"이라 함. 다른 본문 칸과 같은 snake_case. UUID·클라이언트 생성 문자열이 들어갈 만한 길이 |
| 사유 | 앞뒤 공백 제거 뒤 1~500자 | 계약 `reason` minLength 1·maxLength 500. 공백만 있는 사유는 비운 것과 같다 |
| 결과 대기 | 3초(`simai.event-close.result-wait-ms`) | Tier 0 는 저널에 넣고 바로 답한다. 화면이 한 번에 거부 사유를 보이게 하되, 끊긴 Tier 0 때문에 요청이 오래 묶이지 않게 |
| 결과 전 응답 | 202 + `result` null. `JOURNAL_UNAVAILABLE`(일시 거부)도 202 로 그대로 싣는다 | 저장은 했고 다시 보낸다. 화면은 같은 키로 다시 물어 결과를 받는다(같은 키는 새 I1 없이 지금 행을 돌려줌) |
| 거부 응답 코드 | 200 + `result: REJECTED` | Tier 0 의 판단 결과라 요청 오류가 아니다. 이슈 완료 조건 "거부 사유가 그대로 응답됨" |
| 보낼 Tier 0 를 모름 | `risk_event.gateway_id` 가 null 이면 409 `CLOSE_NO_GATEWAY` | 다시 보내기도 gateway 로 찾으므로 결과 없이 영영 남는다 |
| 입력 글자 | 사유·키는 코드 포인트로 세고 NUL 은 거부 | 계약 maxLength 와 같은 단위. PostgreSQL text 는 NUL 을 받지 않는다 |
| EC2 사전 검사 | 이벤트 없음 404, RESOLVED 409 만 | 사람 대상·등급·지금 조건은 Tier 0 가 마지막 판정으로 판단(EC2 는 마지막 판정을 모른다). 해소된 이벤트는 Tier 0 가 NOT_ACTIVE 로 거부할 것이라 미리 막는다(확인 API 와 같음) |
| 같은 키, 다른 이벤트·관리자 | 409 `IDEMPOTENCY_KEY_REUSED` | 키 충돌을 처음 결과로 덮으면 다른 이벤트의 결과를 받게 된다 |
| 다시 보내기 | 결과 없음 또는 `JOURNAL_UNAVAILABLE`, 요청 뒤 10분 안, 이벤트가 아직 RESOLVED 가 아닐 때. 동기화 직후 + 10초 주기(5초 지난 것) | 상향(`EventEscalationForwarder`)과 같은 규칙. 같은 close_id 라 Tier 0 가 DUPLICATE 로 거른다 |
| 늦은 결과 | REJECTED 는 ACCEPTED·DUPLICATE 를 덮지 않고, DUPLICATE 는 ACCEPTED 를 덮지 않는다 | 재전송 응답 순서가 바뀌어도 수락이 남게 |

## 시험

- `server/src/test/java/.../domain/event/EventCloseIntegrationTest.java` (실제 포트, Tier 0 WebSocket 흉내)
  - 송신 내용(close_id ≠ 멱등 키), ACCEPTED 200, 결과만으로는 닫히지 않고 RESOLVED 전이가 와야 닫힘, 같은 키 재요청은 행 하나·같은 결과·새 I1 없음
  - REJECTED(TARGET_NOT_ZONE) 사유 그대로 200, 응답 없는 Tier 0 는 202
  - 끊긴 상태 202 → 재연결 동기화 뒤 같은 close_id 로 송신 → JOURNAL_UNAVAILABLE 다시 송신 → ACCEPTED, 늦은 DUPLICATE·REJECTED 가 덮지 않음
  - 반장 403, 미인증 401, 없는 이벤트 404, 해소 409, 사유 공백·501자 400, 키 없음·129자 400, 500자 경계 통과, 키 재사용 409
- `server/src/test/java/.../domain/tier0/event/EventCloseContractTest.java`: 계약 invalid 예시와 결과·사유 조합 위반

## 남은 일

- 다시 보내기는 요청 뒤 10분까지만 한다(상향과 같음). 그보다 오래 Tier 0 가 끊겨 있었으면 그 행은 결과 없이 남고 같은 키는 계속 202 다. 화면은 새 키로 다시 요청해야 한다.
- 첫 ACCEPTED 응답을 놓치고 다시 보낸 것에 DUPLICATE 가 먼저 오면 결과가 `DUPLICATE`(event_sequence 없을 수 있음)로 남는다. 화면은 DUPLICATE 를 성공으로 보면 된다.

- 관제 화면의 종료 버튼(추창만, 프론트). 202 를 받으면 같은 키로 다시 물어 결과를 보이면 된다.
- 결과를 따로 알리는 토픽은 없다. 수신 시 `RiskEventsDelivered` 로 그 이벤트를 다시 발행하지만 이벤트 항목에 종료 요청 상태 칸은 없다. 필요하면 이벤트 상세에 `closes[]` 를 더한다.
- 실제 Tier 0(ai/)와의 종단 확인은 GPU 세션에서.
