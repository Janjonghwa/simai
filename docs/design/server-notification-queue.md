# EC2 알림 대기열(LIVE 이벤트만)

S15P31A101-123. 명세 7.3(CRITICAL·HIGH 만 반장 PWA 로, 신뢰도가 낮으면 "확인 필요"), 7.4(PLANNED·WHAT_IF·REPLAY 는 실제 알림 채널로 보내지 않음),
10장(Web Push, 짧은 TTL, `Topic`=event_id), 11.4-3(notification 은 LIVE 만, DB 기록 차단과 실제 발송 차단을 각각 시험).
코드는 `server/.../domain/notification/NotificationQueue`, 시험은 `NotificationQueueIntegrationTest`. 실제 발송(Web Push VAPID)은 S15P31A101-124,
구독은 S15P31A101-230.

## 두 겹 차단

1. **DB 기록 차단**(V1): `notification.source` 는 `CHECK (source IN ('LIVE'))`, `(event_id, source)` 는 `risk_event(event_id, source)` 복합 FK.
   비LIVE 이벤트를 가리키는 행은 source 를 무엇으로 적어도 들어가지 않는다(일반 CHECK 는 다른 표를 볼 수 없어 복합 FK 를 쓴다).
2. **발송 차단**(`NotificationQueue`):
   - `enqueue`: 알릴 전이만 QUEUED 행으로 넣는다. 아니면 이유를 돌려준다(`NOT_LIVE`·`NOT_ALERTING`·`BELOW_ALERT_SEVERITY`·`AUDIT_ONLY`·
     `UNKNOWN_TRANSITION`·`DUPLICATE`·`TARGET_MISSING`·`INVALID_REQUEST`). 예외를 던지지 않아 구독마다 넣는 반복이 끊기지 않는다.
   - `claim(limit, lease)`: 발송 워커는 이것으로만 보낼 알림을 받는다. LIVE 이벤트의 QUEUED 알림만 임대로 꺼낸다.
   - `markSent`·`markFailed`: 보낸 뒤 끝낸다. 끝냈으면 true, 이미 끝났거나 없으면 false.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 알릴 전이 | LIVE 이벤트, 이벤트가 아직 해소되지 않음(`lifecycle <> RESOLVED`), 전이 종류 RAISED·UPDATED, CRITICAL·HIGH, `delivery_mode` REALTIME·DELAYED_ACTIVE | 7.3 채널 표, 7.4 출처 분리, 해소된 이벤트는 감사 기록으로만(RESOLVED·ACKNOWLEDGED 전이와 해소된 이벤트의 옛 전이를 다시 알리지 않는다). Tier 0 의 AUDIT_ONLY 표시에만 기대지 않는다 |
| 넣기 | 위 조건을 `INSERT … SELECT … WHERE` 로 DB 행에 걸고 `RETURNING` 으로 넣었는지 본다. 못 넣었을 때만 이유를 진단한다 | 정상 경로는 왕복 한 번. 읽은 뒤 바뀌어도 비LIVE 행이 생기지 않고, 경합으로 0행이면 실제 이유(중복인지 조건인지)를 다시 가린다. source 는 risk_event 에서 가져와 비LIVE 면 CHECK·복합 FK 에도 걸린다 |
| 대상 | 구독·사용자가 없으면 `TARGET_MISSING`(FK 예외 대신). 채널은 WEB_PUSH·WATCH, 요약은 비어 있으면 안 됨 | 410 정리로 구독이 지워진 사이에 넣어도 다른 구독의 알림이 막히지 않게 |
| 중복 | 같은 전이·채널·대상(`subscription_id`·`target_user_id`, NULL 끼리도 같음)은 한 행(V13 유일 색인 `NULLS NOT DISTINCT`) | Tier 0 재전송·EC2 재처리에도 알림은 한 번. 운영 DB 는 PostgreSQL 17(`deploy/ec2/compose.yaml`) |
| 꺼내기 | 임대: 한 문장(`UPDATE … SET claimed_until … WHERE id IN (SELECT … FOR UPDATE SKIP LOCKED) RETURNING`)으로 빌리고 바로 커밋. 임대 중인 알림은 다른 워커가 받지 않는다 | 트랜잭션 밖에서 불러도 두 워커가 같은 알림을 받지 않고, 외부 푸시를 부르는 동안 행 잠금·DB 연결을 쥐지 않는다(풀 고갈 방지) |
| 끝내기 전 워커가 죽음 | 임대가 지나면 다시 꺼낸다(최소 한 번). 같은 이벤트의 푸시는 `Topic`=event_id 로 앞 알림을 대체한다 | 알림을 잃는 것보다 다시 보내는 것이 낫다. 기기에는 한 번만 보인다(10장) |
| 순서 | CRITICAL 먼저, 그 안에서 REALTIME 먼저, 같으면 먼저 넣은 것 | 밀린 대기열 뒤의 CRITICAL 이 늦지 않게 |
| 오래된 알림 | 관측 시각(`observation_at`)이 `simai.notification.max-age`(기본 10분)보다 오래된 대기 알림은 꺼내기 전에 EXPIRED 로 끝낸다 | 10장 "짧은 TTL". 오래전 위험을 지금 일어난 것처럼 보내지 않는다 |
| 확인 필요 | 꺼낸 알림에 그 전이의 `needs_check`·`confidence`·`observation_at` 을 함께 준다 | 7.3 "신뢰도가 낮으면 같은 채널에 '확인 필요'를 붙여 보낸다", 지연 측정 기준 시각 |
| 실패 | `markFailed` 는 FAILED 로 끝내고 다시 꺼내지 않는다. 재시도 여부는 발송 쪽(-124)이 정해 다시 넣는다 | 410(구독 만료)은 다시 보내도 실패 |
| 마이그레이션 | V13: 유일 색인, `claimed_until` 칸, `status='QUEUED'` 부분 색인 | 위 넣기·꺼내기 |

## 시험

`NotificationQueueIntegrationTest`: LIVE CRITICAL 전이가 한 번만 들어가고(재전송 `DUPLICATE`, 다른 대상은 따로) 발송 뒤 다시 꺼내지 않음·다시 끝내면 false,
REPLAY·해소 전이·해소된 이벤트·CAUTION·AUDIT_ONLY·모르는 전이·틀린 채널·빈 요약·없는 구독·없는 사용자는 넣지 않음(행 0),
DB 는 비LIVE 행을 source REPLAY(CHECK)·LIVE(복합 FK) 어느 쪽으로도 거부, 발송 워커가 REPLAY 를 한 건도 보내지 않고 LIVE 만 보냄,
임대 중에는 다른 워커가 받지 않고 임대가 지나면 다시 꺼내며 실패는 다시 꺼내지 않음, CRITICAL·REALTIME 먼저·확인 필요 전달·오래된 대기 알림 만료.

## 남은 일

- S15P31A101-124: 위험 이벤트 전이를 받을 때 구독(-230)마다 `enqueue` 하고, 발송 워커가 `claim` → Web Push(`Topic`=event_id, 짧은 TTL) →
  `markSent`·`markFailed` 한다. 발송은 이 대기열을 거쳐야 한다(다른 길로 보내면 LIVE 차단을 건너뛴다). 임대 기간은 푸시 요청 제한 시간보다 길게.
- 시험 알림(`POST /api/push/test`, -163)은 RiskEvent 가 아니라 이 대기열을 쓰지 않는다(3.10 "시험" 표시).
