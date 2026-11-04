# EC2 워치 진동 알림·ACK·격상

S15P31A101-338. 명세 부록 C.6(BOUND 워치 개인 진동, ACK 없으면 격상), FR-45(등급별 진동·한 단어), FR-46(ACK·격상), 7.3(워치 개인 채널:
BOUND 작업자에게 CRITICAL·HIGH·CAUTION), 7.4·11.4-3(LIVE 만), 12.4(`/topic/watch`). 계약 `watch_alert`·`watch_ack` 1.0.0(-334),
`watch_escalation` 1.0.0(이 이슈, 계약 묶음 7.7.0). 연결은 [`server-watch-link.md`](server-watch-link.md)(-336).

- 코드: `server/.../domain/watch/` — `WatchAlertService`(보내기·ACK·격상), `WatchBindings`·`WatchBindingController`(임시 수동 배정),
  `WatchHub.send`·`isConnected`, `WatchAckReceived`
- 표: V35 `watch_binding`·`watch_alert`
- 시험: `server/src/test/.../domain/watch/WatchAlertIntegrationTest.java`

## 흐름

1. 관리자가 `PUT /api/watch-bindings/{deviceId}` `{"world_id": "w-17"}` 로 워치와 익명 World ID 를 잇는다(임시 수동 배정, 명세 밖 임시안).
   그 워치나 그 World ID 의 옛 배정은 REPLACED 로 끝난다. `GET` 은 지금 배정과 채널 상태(BOUND·LOST), `DELETE` 는 풀기.
2. Tier 0 위험 이벤트 전이를 저장하고 커밋하면 `RiskEventsDelivered` 가 나온다(관제 화면 `/topic/events` 와 같은 신호).
   `WatchAlertService` 가 `watch-alert` 스레드에서 이벤트를 읽어, LIVE·해소 안 됨·CRITICAL·HIGH·CAUTION 이면 `world_ids` 마다
   배정된 워치를 찾는다. 배정이 없거나(UNBOUND) 워치가 끊겼으면(LOST) 보내지 않는다(기존 경로: 현장 경보·반장 PWA·관리자 화면).
3. 보낼 워치가 있으면 `watch_alert` 행을 먼저 넣고(같은 이벤트·워치에 같거나 높은 등급을 이미 보냈으면 넣지 않음) 그 워치의 I4 연결로
   `watch_alert` 를 보낸다(봉투 `msg_id` = `alert_msg_id`). 보내지 못하면 바로 SEND_FAILED 로 격상한다.
4. 워치가 진동을 시작하며 `watch_ack` VIBRATED, 작업자가 확인을 누르면 CONFIRMED 를 보낸다. EC2 는 계약 검사 뒤 그 워치에 보낸 알림만
   고친다(VIBRATED → `acked_at`·`ack_received_at`, CONFIRMED → `confirmed_at`·`confirm_received_at`, 처음 값만 남음).
5. `watch-escalation` 스레드가 1초마다 `sent_at + ack_within_ms` 가 지나도록 `ack_received_at` 이 없는 알림을 NO_ACK 로 격상하고,
   관리자 화면 `/topic/watch` 로 `watch_escalation` 을 보낸다. UPDATE … RETURNING 으로 행을 먼저 바꿔 한 알림에 한 번만 나간다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 배정 | 관리자 임시 수동 배정 API(`/api/watch-bindings`, ADMIN 만), 이름 없이 World ID ↔ 기기 ID. 계약(contracts/)에 두지 않음 | 이슈 '1단계 임시 수동 배정(명세 밖, W7 에서 움직임 대조로 바꿈)'. 11.4-5 이름 금지 |
| 배정 수명 | 풀 때까지(관리자 DELETE 또는 다른 배정이 REPLACED). 워치가 끊겨도 남고, 끊긴 동안 채널은 LOST | 이슈 '세션 단위': 시연 한 번 동안 이어 두는 배정. 재연결마다 다시 잇지 않게 |
| 보낼 이벤트 | LIVE, `lifecycle <> RESOLVED`, CRITICAL·HIGH·CAUTION, 규칙은 가리지 않음 | 7.3 워치 개인 채널(INFO 제외), 7.4 출처 분리. C.6 의 'R5 진입 예측 시점' 은 R5 이벤트가 그 시점에 RAISED 되는 것으로 맞춘다 |
| 보낼 때 | `RiskEventsDelivered`(REALTIME 전이·동기화 끝 DELAYED_ACTIVE, 운영자 확인·요약 갱신 재발행 포함) | 관제 화면에 알리는 것과 같은 때. AUDIT_ONLY 는 오지 않는다 |
| 중복 | 같은 이벤트(재개마다 따로, `resume_count`)·워치에 같거나 높은 등급을 보냈으면 다시 보내지 않는다. 등급이 오르면 다시. 보내지 못한(SEND_FAILED) 알림은 세지 않아 다시 붙으면 다시 보낸다(부분 UQ `event_id, resume_count, device_id, severity`) | 재발행마다 진동하면 작업자가 무시한다. 상향·재진입(resumed, 6.4)은 새 위험이다 |
| 한 단어 | 규칙별 R1 위험구역·R2 개구부·R3 고소·R4 낙상·R5 진입주의·R6 구역확인·R7 감시저하·R8 장비·R9 안전모(PPE MVP 는 안전모만, 워치 그림·종류 칸 없이 글자만)·R10 점유주의, 모르면 위험 | 계약 1~8자, 원형 화면 한 줄(-390) |
| 진동 패턴 | 등급으로: CRITICAL DANGER_LONG, HIGH CAUTION_DOUBLE, CAUTION CAUTION_WEAK. 메시지와 표 모두 적는다 | FR-45, 계약의 기본 대응과 같다 |
| 방향 | 싣지 않는다 | 계약은 선택. 작업자 기준 방향은 트랙 속도가 필요하고 임계값이 정해지지 않았다(남은 일) |
| 확인 기한 | `simai.watch.ack-within` 10초 | 계약 초깃값(watch-i4-contract.md). EC2 시각(`ack_received_at`)으로만 판단 |
| 격상 대상 | 관제 화면(`/topic/watch`, 새 I2 `watch_escalation`, 구독은 열람 이상 — 14장 "STOMP 토픽 조회는 열람 이상", 익명 ID 만 싣는다)만 | 사용자 결정(2026-11-03): CRITICAL·HIGH 는 Tier 0·노트북 현장 경보가 이미 울리고, CAUTION 을 현장 경보로 올리는 EC2 → Tier 0·노트북 명령은 계약에 없다(`event_escalate` 는 VLM 상향 승인 전용) |
| 격상 사유 | NO_ACK(기한 안 VIBRATED 없음), SEND_FAILED(보내지 못함) | 보내지 못한 것을 기한까지 기다리지 않는다 |
| 늦은 ACK | 기록만 하고 격상은 그대로 | 격상은 이미 관리자에게 나갔다. 지연 측정에 남긴다 |
| 받은 것으로 보는 ACK | VIBRATED 또는 CONFIRMED 가 기한 안에 EC2 에 닿으면 | VIBRATED 를 잃어도 작업자가 확인을 눌렀으면 받은 것이다(거짓 격상 방지) |
| 격상하지 않는 것 | 해소된 이벤트의 알림, 보낸 지 10분 넘은 알림 | 이미 끝난 위험을 알리지 않는다. EC2 가 오래 내려갔다 뜰 때 지난 알림이 한꺼번에 격상되지 않게(알림 대기열 max-age 와 같은 10분) |
| ACK 기록 스레드 | 받은 WebSocket 스레드가 아니라 `watch-alert` 스레드 | 느린 DB 가 그 워치의 하트비트 처리를 밀어 LOST 로 잘못 보지 않게 |
| 동시 배정 | 같은 워치·World ID 를 동시에 이으면 늦은 쪽 409 `BINDING_CONFLICT` | 부분 유일 색인 위반을 500 으로 내지 않는다 |
| 보냄의 뜻 | 세션 송신 버퍼에 넣으면 보낸 것으로 본다 | 반쯤 끊긴 TCP 는 EC2 가 알 수 없다. 그런 알림은 기한 뒤 NO_ACK 로 격상된다 |
| 워치 ACK 의 효과 | 이벤트 상태·현장 경보를 바꾸지 않는다 | 운영자 확인(3.2)이 아니다(-334 결정) |
| 표 | `watch_alert` 를 `notification` 상세가 아닌 독립 표로. LIVE 두 겹 차단은 source CK + `risk_event(event_id, source)` 복합 FK | `notification` 은 구독·사용자 대상이고 CRITICAL·HIGH 만 받는다(CAUTION 불가) |
| 격상 판정 | DB 시각 조건(`sent_at + ack_within_ms <= now`)을 `simai.watch.escalation-interval`(1초)마다 훑는다 | EC2 재시작 뒤에도 남은 알림이 격상된다. 메모리 타이머는 재시작에 사라진다 |
| 스레드 | 보내기는 `watch-alert` 한 스레드, 격상은 `watch-escalation` 한 스레드 | I1 수신·커밋 스레드를 붙잡지 않고, 같은 이벤트의 전이가 받은 순서대로 나간다 |

## 시험

`WatchAlertIntegrationTest`(시험 설정: 확인 기한 1초, 격상 판정은 시험이 직접 부름 — 시험 컨텍스트가 여럿이라 주기 판정을 꺼 둔다).

- 배정 API: 열람 403·토큰 없음 401, 관리자 200(채널 LOST), 같은 World ID 를 다른 워치에 → 옛 배정 REPLACED, 목록, 풀기 204·다시 404, 없는 워치 404, 빈 World ID 400
- `RiskEventsDelivered` 경로로 BOUND 워치에 계약 `watch_alert`(HIGH → CAUTION_DOUBLE, R5 → 진입주의, 기한 1000) → VIBRATED·CONFIRMED 기록 → 기한 뒤에도 격상 없음
- ACK 없음 → 기한 뒤 `/topic/watch` 에 계약 `watch_escalation`(NO_ACK, 알림 ID·워치·World ID), 다시 판정해도 두 번째 없음, 늦은 ACK 는 기록만
- UNBOUND·해소·REPLAY·INFO·LOST(끊긴 워치) 에는 보내지 않고 행도 없음
- 같은 이벤트 재발행은 한 번, HIGH → CRITICAL 이면 다시, CRITICAL → CAUTION 이면 보내지 않음
- 다른 워치가 보낸 ACK 로는 알림이 고쳐지지 않음
- 해소 뒤 다시 활성(resume_count 증가)이면 같은 등급이라도 다시 알림
- CONFIRMED 만 온 알림·해소된 이벤트의 알림은 기한 뒤에도 격상하지 않음

## 리뷰에서 두기로 한 것

- `/topic/watch` 를 열람 계정도 구독한다: 명세 14장이 토픽 조회를 열람 이상으로 두고 관리자 전용 토픽을 두지 않는다(-411 에서 지움). 익명 World ID·기기 ID 만 싣는다.

## 남은 일

- 실제 워치로 '위험 진입 → 진동 → ACK 기록' 을 끝까지 돌리는 확인(이슈 완료 조건 2): 운영 EC2 에 워치 토큰 등록·배정 뒤 -341 과 함께. 이 이슈는 시험용 클라이언트로만 확인했다.
- CAUTION 이벤트의 현장 경보 격상: EC2 → Tier 0(또는 노트북) 경보 명령 계약이 필요하다(사용자 결정으로 이번에는 두지 않음).
- 방향(`direction`): 트랙 `velocity` 로 작업자 기준 방향을 내는 규칙과 속도 임계값.
- 지연 측정(C.6 '진입 → 진동 p95 1.5초'): `ack_received_at − sent_at` 은 왕복 상한이다. 워치 시계 차이(`gait_features.clock_offset_ms`, -340 저장)로 `acked_at` 을 EC2 시각으로 바꿔 재는 것은 -341.
- -340: 움직임 대조 배정(`MOTION_MATCH`)과 `channel_state` 기록, 수동 배정 API 정리.
- -342: 관제 화면이 `/topic/watch` 를 받아 격상 표시, 배정 화면.
