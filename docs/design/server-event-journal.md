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

## 남은 일

- 실제 알림 발송(`/topic/events`, Web Push)은 S15P31A101-119·M1. 지금은 `risk_event.delivery` 까지 정한다.
- 현장 버튼 ACK 를 `event_ack` 행으로 남기기, EC2 → Tier 0 `event_ack` 전달은 S15P31A101-121.
- 스냅샷에 없는데 EC2 에서 활성인 이벤트는 경고 로그만 남긴다(EC2 는 전이를 만들지 않는다).
