# EC2 워치 진동 알림·ACK·격상

S15P31A101-338. 명세 부록 C.6(BOUND 워치 개인 진동, ACK 없으면 격상), FR-45(등급별 진동·한 단어), FR-46(ACK·격상), 7.3(워치 개인 채널:
BOUND 작업자에게 CRITICAL·HIGH·CAUTION), 7.4·11.4-3(LIVE 만), 12.4(`/topic/watch`). 계약 `watch_alert`·`watch_ack` 1.0.0(-334),
`watch_escalation` 1.0.0(이 이슈, 계약 묶음 7.7.0) → 1.1.0(수동 알림, S15P31A101-475, 7.12.0). 연결은 [`server-watch-link.md`](server-watch-link.md)(-336).
수동 알림·조회 API 는 아래 "수동 알림" 절(-475).

- 코드: `server/.../domain/watch/` — `WatchAlertService`(보내기·ACK·격상·수동 발송·조회), `WatchBindings`·`WatchBindingController`(임시 수동 배정),
  `WatchAlertController`(수동 알림·조회, -475), `WatchHub.send`·`isConnected`, `WatchAckReceived`
- 표: V35 `watch_binding`·`watch_alert`, V40 `watch_alert.origin`·`requested_by_user_id`(-475)
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
| 한 단어 | 규칙별 R1 위험구역·R2 개구부·R3 고소·R4 낙상·R5 진입주의·R6 구역확인·R7 감시저하·R8 장비·R9 안전모·R10 점유주의·R11 조끼(PPE 는 워치 그림·종류 칸 없이 글자만, R11 은 S15P31A101-549), 모르면 위험 | 계약 1~8자, 원형 화면 한 줄(-390) |
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

## 수동 알림·조회 API (S15P31A101-475)

알림이 Tier 0 위험 이벤트로만 나가면 사람이 작업자를 직접 부를 길(비상·호출)도, Tier 0 없이 워치 경로를 시연·시험할 길도 없다(사용자 요청
2026-11-04). 수동 배정과 같은 명세 밖 운영 입구라 계약(contracts/)에는 두지 않고 REST 로만 연다.

- `POST /api/watch-alerts`(ADMIN·FOREMAN) `{"device_id","severity","word","direction"?,"ack_within_ms"?,"needs_check"?}` → 보낸 `watch_alert` 행.
  등록 안 된 워치 404 `WATCH_NOT_FOUND`, 연결 안 됨 409 `WATCH_NOT_CONNECTED`, 값 오류 400 `INVALID_ALERT`
- `GET /api/watch-alerts?device_id=&limit=`(열람 이상, 기본 20·최대 200, `sent_at` 내림차순) → 최근 알림과 진동(`acked_at`·`ack_received_at`)·
  확인(`confirmed_at`·`confirm_received_at`)·격상(`escalated_at`·`escalation_reason`) 시각. 시연·시험에서 "ACK 가 돌아왔나" 를 여기서 본다

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 누가 | ADMIN·FOREMAN 이 보내고, 조회는 열람 이상 | 반장 PWA 가 현장에서 작업자를 부를 수 있어야 한다. 이벤트 조회(`EventController`)와 같은 역할 조합. 조회는 STOMP 토픽과 같이 열람 이상(14장) |
| 배정 | 필요 없다. 배정이 있으면 `world_id` 를 싣는다 | 수동 알림은 사람이 워치를 고른 것이다. Tier 0 없이도 보내야 한다(시험 입구) |
| 연결 안 된 워치 | 사전 검사에 걸리면 409 `WATCH_NOT_CONNECTED`(행 없음). 보내는 사이 끊기면 행이 SEND_FAILED 로 남아 격상되고 502 `WATCH_SEND_FAILED` | 보낼 수 없는 알림을 대기열에 두지 않는다(워치가 붙었을 때 몰아서 울리지 않게). 두 경우를 코드로 갈라 호출자가 재시도할지(409) 격상을 볼지(502) 정하게 한다 |
| 표 | 같은 `watch_alert` 에 `origin` MANUAL, `source` NULL, `event_id` 는 그 알림만의 UUIDv7, `event_sequence` 1, `resume_count` 0, `requested_by_user_id` | 계약 `watch_alert.event_id` 가 필수(UUIDv7)라 워치 쪽은 바꾸지 않는다. 복합 FK 는 MATCH SIMPLE 이라 `source` NULL 이면 검사하지 않는다(별도 표를 두면 ACK·격상 경로가 둘이 된다) |
| 격상 | 이벤트 알림과 같은 NO_ACK·SEND_FAILED, `/topic/watch` `watch_escalation` 1.1.0(`origin`, `world_id` null 가능) | 보낸 사람이 "작업자가 못 느꼈다" 를 알아야 한다. MANUAL 은 이벤트가 없어 해소 여부 조건을 건너뛴다 |
| 검증 | 등급 CRITICAL·HIGH·CAUTION, 단어는 앞뒤 공백을 지운 뒤 계약 정규식(`^\S(.*\S)?$`, 가운데 공백·줄바꿈 불가)·코드포인트 1~8자, 방향 FRONT·BACK·LEFT·RIGHT, 기한 1000~600000 ms(기본 `simai.watch.ack-within`). 조회 `limit` 은 1~200 으로 끌어온다 | 계약 `watch_alert` 의 범위 그대로(앞뒤 공백만 지워 준다: 사람이 치는 값). 기한 하한 1초: 왕복보다 짧은 기한은 정상 전송도 NO_ACK 로 격상된다 |
| 격상 창 | 기한이 지난 뒤 10분까지만 격상(`sent_at + ack_within_ms > now − 10분`) | 전에는 `sent_at` 기준 10분이라 기한 10분짜리 수동 알림은 격상 조건과 동시에 성립할 수 없었다(리뷰). EC2 재기동 뒤 지난 알림을 한꺼번에 격상하지 않는 뜻은 그대로 |
| 계약 1.1.0 | `origin` 선택(없으면 EVENT), `world_id` 는 MANUAL 에서만 null(if/else) | 필드 추가는 마이너. 1.0.0 수신자(아직 없음, -342)는 `origin` 없이 EVENT 로 읽는다 |

시험(`WatchAlertIntegrationTest` 수동 알림 절): 열람 403·토큰 없음 401·반장 200, 워치가 계약 `watch_alert`(방향 포함, `event_sequence` 1)를 받고
VIBRATED·CONFIRMED 가 조회에 보이며 기한 뒤 격상 없음; 연결 없는 워치 409(행 없음)·없는 워치 404·등급/단어(길이·줄바꿈)/방향/기한(1초 미만) 오류 400;
ACK 없으면 기한 뒤 `watch_escalation`(origin MANUAL, 배정된 `world_id`, 계약 1.1.0 통과)과 조회의 NO_ACK; 배정 없는 워치의 기한 10분 알림도
격상되고 `world_id` null 이 계약을 통과하며 기기 없는 조회는 전체를 준다.

리뷰에서 두기로 한 것(-475): 같은 요청 연타에 멱등 키·빈도 제한이 없다(관리자·반장만 부르고 `requested_by_user_id` 로 남는다. 필요하면 후속);
격상 뒤 도착한 ACK 는 기록만 되고 격상은 철회되지 않는다(이벤트 알림과 같음); 삭제된 사용자의 유효 JWT 는 FK 위반으로 500(토큰 수명 30분 안의 드문 경우).

## 배정 알림 (S15P31A101-510)

시연 체크인 장면: 워치가 연결됐는데 배정이 없으면 워치가 "워치 찬 손을 3초 흔들어 주세요"를 보이고, 손을 흔들어 배정되면 짧게 진동하며 "연결됨"으로 바뀐다. 이를 위해 EC2 → 워치 `watch_binding_notice` 1.0.0(계약 묶음 7.15.0)을 둔다.

- **계약**: `state` BOUND·UNBOUND, `method` MOTION_MATCH·WAVE·MANUAL(UNBOUND 면 null. WAVE 는 -508 손 흔들기 체크인용으로 미리 둔 값), `world_id`(익명, UNBOUND 면 null), 선택 `unbind_reason`(UNBOUND 만. 닫힌 enum 이 아니라 대문자 문자열 — -508 이 사유를 더해도 계약을 올리지 않으려는 것), `sent_at`. 닫힌 객체라 사람 이름 같은 키는 못 싣는다(11.4-5).
- **구성**: `WatchBindings`(배정이 생기고 끝나는 지점: 수동 배정·대조 배정·대조 해제·관리자 풀기)가 `WatchBindingChanged(deviceId, 풀린 사유)` 를, `WatchHub` 가 연결이 열릴 때 `WatchConnected(deviceId)` 를 이벤트로 낸다. 보내는 쪽은 별도 클래스 `WatchBindingNotifier` 다(대조기 `WatchMotionMatcher`·`MotionMatch` 는 건드리지 않았다. -508 이 `WatchBindings` 에 새 배정·이동 메서드를 더해도 같은 이벤트만 내면 알림이 붙는다).
- **보내는 규칙**: 이벤트는 "이 워치를 다시 보라"는 신호이고 지금 상태는 알림 스레드(한 줄)가 DB 에서 다시 읽는다. 연결된 워치에만 보내고, 보낸 상태 키(`BOUND|world_id` 또는 `UNBOUND`)가 마지막으로 보낸 것과 같으면 보내지 않는다(같은 World ID 를 다시 이어 REPLACED→새 행이 되어도, 방식만 MOTION_MATCH→MANUAL 로 바뀌어도 반복 없음). 끊긴 워치에는 보내지 않고 보낸 기록을 지운다. 워치가 (재)연결되면 같은 상태여도 한 번 보낸다(초기 동기화: 워치 쪽 상태는 사라졌을 수 있다). 배정이 다른 워치로 옮겨 가면 옛 워치는 UNBOUND(REPLACED), 새 워치는 BOUND 를 받는다.
- **워치의 몫**: 알림(`watch_alert`)이 떠 있으면 안내·진동으로 가리지 않는다(경보 우선). 새로 BOUND 가 된 때만 한 번 진동하므로(UNBOUND→BOUND), 앱이 다시 뜨거나 재연결 동기화로 같은 BOUND 를 받아도 울리지 않는다. `docs/design/watch-app.md` 배정 안내 절.
- **시험**: `WatchBindingNoticeIntegrationTest`(연결 때 UNBOUND 1회, 배정·풀기·대조 배정·대조 해제 각 1회와 계약 검사, 같은 상태 반복 없음, 다른 워치로 이동, 끊긴 동안은 보내지 않고 재연결 동기화 1회). 기존 워치 시험의 시험용 클라이언트는 연결 직후 오는 이 알림을 건너뛴다.
- **버린 대안**: (1) `WatchBindings` 가 `WatchHub` 로 직접 보내기 — 배정을 만드는 대조 스레드·REST 트랜잭션이 WebSocket 송신·DB 재조회에 묶이고 `WatchHub`↔`WatchBindings` 순환이 생긴다. (2) 이벤트에 상태를 싣기 — 같은 순간 겹친 변경(REPLACED 후 재배정)에서 낡은 상태를 보낼 수 있어 받는 쪽이 DB 에서 다시 읽는다.
- **남은 위험**: 배정 이벤트는 커밋 뒤에 나가 지연이 한 스레드 큐 길이만큼이다(수 ms). 알림 스레드가 DB 에서 막히면 이후 알림이 줄 선다(워치 알림 `WatchAlertService` 와 스레드가 달라 위험 알림은 밀리지 않는다). 폐기된 워치는 `isConnected` 가 이미 거짓이라 보내지 않는다.

## 시연 스위치: 모든 위험 이벤트를 지정 워치로 (S15P31A101-793)

시연은 사람이 하나고 걸음 대조는 그 사람이 6초쯤 걸어야 붙어 시연 흐름이 끊겼다. `simai.watch.demo-device`(compose `SIMAI_WATCH_DEMO_DEVICE`, 예 `watch-01`)에 기기 ID 를 넣으면
`WatchAlertService.alert` 가 배정과 상관없이 모든 LIVE 위험 이벤트(CRITICAL·HIGH·CAUTION, 해소 전)를 그 워치로도 보낸다. 비우면(기본) 꺼진다.

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 배정 표 | 건드리지 않음(알림 행만, `world_id` 는 이벤트의 첫 사람) | 배정은 워치·사람 1:1 이라 "들어오는 사람 모두"를 배정으로는 표현할 수 없다. 알림 단계에서 보내면 1:1 과 걸음 대조는 그대로 두고 효과만 얻는다 |
| 배정된 워치와의 관계 | 배정된 워치가 따로 있으면 둘 다 받는다. 지정 워치가 그 사람에게 배정돼 있으면 한 번만 | `send` 의 같은 이벤트·워치·등급 중복 거름을 그대로 탄다 |
| 켜짐 표시 | 기동 때 WARN 로그 한 줄 | 본 운영에서 켜 둔 채 잊지 않게. 화면 표시는 두지 않는다(시연 전용) |
| 재연결 동기화·해소 지움 | 해소 지움(`watch_alert_clear`)은 알림 행 기준이라 지정 워치도 받는다. 재연결 때 못 보낸 열린 이벤트 되돌리기(`resync`)는 배정 기준이라 지정 워치는 새 이벤트부터 받는다 | 시연 중 워치가 끊겼다 붙는 짧은 틈은 다음 이벤트로 메워진다. 되돌리기까지 넣으면 배정 없는 모든 열린 이벤트를 쏟게 된다 |

시험: `WatchAlertIntegrationTest.시연_스위치가_켜지면_배정_없는_사람의_이벤트도_지정_워치로_가고_끄면_가지_않는다`(배정된 워치와 지정 워치 둘 다 수신, 배정 없는 사람 이벤트 수신·중복 없음, 끄면 안 감).

## 리뷰에서 두기로 한 것

- `/topic/watch` 를 열람 계정도 구독한다: 명세 14장이 토픽 조회를 열람 이상으로 두고 관리자 전용 토픽을 두지 않는다(-411 에서 지움). 익명 World ID·기기 ID 만 싣는다.

## 남은 일

- 실제 워치로 '위험 진입 → 진동 → ACK 기록' 을 끝까지 돌리는 확인(이슈 완료 조건 2): 운영 EC2 에 워치 토큰 등록·배정 뒤 -341 과 함께. 이 이슈는 시험용 클라이언트로만 확인했다.
  2026-11-04 갤럭시워치6 1대를 운영 EC2(`wss://simai.live/ws/watch`)에 등록해 붙였고(`EC2 연결됨`, 서버 재시작 뒤 자동 재연결), 알림 경로는 -475 의
  `POST /api/watch-alerts` 로 Tier 0 없이 확인한다.
- CAUTION 이벤트의 현장 경보 격상: EC2 → Tier 0(또는 노트북) 경보 명령 계약이 필요하다(사용자 결정으로 이번에는 두지 않음).
- 방향(`direction`): 트랙 `velocity` 로 작업자 기준 방향을 내는 규칙과 속도 임계값.
- 지연 측정(C.6 '진입 → 진동 p95 1.5초'): `ack_received_at − sent_at` 은 왕복 상한이다. 워치 시계 차이(`gait_features.clock_offset_ms`, -340 저장)로 `acked_at` 을 EC2 시각으로 바꿔 재는 것은 -341.
- -340(완료, [`server-watch-match.md`](server-watch-match.md)): 움직임 대조 배정(`MOTION_MATCH`)과 `channel_state` 기록. 수동 배정 API 는 관리자 덮어쓰기로 남겼다.
- -342: 관제 화면이 `/topic/watch` 를 받아 격상 표시, 배정 화면.
