# EC2 STOMP 발행(I2)

S15P31A101-119. 명세 3.7(4단계 "기록한 뒤 발행"), 12.2·12.4(I2 토픽), 14장(STOMP 토픽 권한), 3.2(재동기화 뒤 알림 규칙), 계약 `world_state`·`risk_event`.
코드는 `server/.../domain/realtime/`(토픽·발행), `global/config/StompConfig.java`, `global/security/StompAuthInterceptor.java`.
받는 쪽은 관제 앱 `front/apps/admin/src/lib/stomp.ts`.

## 흐름

1. 관제 앱이 `/ws`(STOMP over WebSocket, SockJS 없음)에 붙고 CONNECT 프레임의 `Authorization: Bearer <JWT>` 로 인증한다.
2. 앱은 `/topic/world`·`/topic/events`·`/topic/health` 를 구독한다.
3. I1 로 받은 `world_state` 를 저장하고 커밋하면, payload 를 바꾸지 않고 봉투만 새로 써서(`source_id` `ec2`, 새 `msg_id`·`sent_at`) `/topic/world` 로 보낸다.
4. 이벤트 전이를 저장하고 커밋하면 알릴 이벤트의 **현재 상태**를 `risk_event`(2.0.0)로 읽어 `/topic/events` 로 보낸다. 관제 화면·PWA 가 확인(`POST /api/events/{id}/ack`)해도 확인을 붙인 상태를 다시 보낸다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 엔드포인트 | `/ws`, SockJS 없음, 출처는 CORS 목록(`simai.auth.cors-allowed-origins`), 비면 같은 출처만 | 관제 앱 `VITE_STOMP_URL=ws://…/ws`. Nginx `location /ws` 가 I1(`/ws/tier0`)과 함께 넘긴다 |
| 인증 | CONNECT 의 Bearer JWT(REST 와 같은 키·`role` 클레임). 없거나 틀리면 ERROR 프레임 뒤 연결 닫음. HTTP 업그레이드 요청 자체는 공개 | 브라우저 WebSocket 은 업그레이드에 헤더를 싣지 못한다 |
| 연결 중 토큰 만료 | 이미 구독한 토픽은 끊지 않는다. 새 SUBSCRIBE 는 CONNECT 때 받은 토큰의 만료를 다시 보고 거부한다. 재연결 때 새 토큰을 검사한다 | 관제 중 끊김을 만들지 않는다(10장). 앱은 재연결마다 새 토큰을 싣는다. 계정 비활성화는 연결 중에 반영되지 않는다(재연결 때 토큰 갱신이 막힌다) |
| 구독 권한 | 열람 이상: `/topic/world`·`/topic/events`·`/topic/health`·`/topic/avatar`. 관리자 전용 토픽은 없다(`/topic/admin/events` 는 S15P31A101-411 로 지웠다). 그 밖의 경로와 SEND 는 거부(연결 닫음) | 14장. 명령은 REST(12.6) |
| 하트비트 | 양쪽 10초 | 관제 앱 `HEARTBEAT_MS` 와 같다. 끊김 표시(6장, 13.4) |
| 발행 시점 | 저장 트랜잭션 커밋 뒤(`@TransactionalEventListener`), 토픽별 스레드(`stomp-world`·`stomp-events`)에서. 이벤트 대기열 상한 10,000 | 3.7 "기록한 뒤 발행". 브로커·DB 가 느려도 I1 수신을 붙잡지 않고, 이벤트 조회가 world 를, world 가 이벤트를 막지 않는다 |
| `/topic/world` 밀림 | 발행이 밀리면 Tier 0(`source_id`)마다 최신 `world_state` 만 보내고 사이 것은 버린다(`replaced` 로 셈) | 12.4 "밀리면 최신 것만". 다른 Tier 0 의 것을 버리지 않는다 |
| `/topic/events` 대상 | 동기화 뒤 현재 상태를 바꾼 실시간 전이(REALTIME)와, 동기화를 끝낼 때 DELAYED_ACTIVE 로 정한 이벤트(스냅샷 순서). 늦게 온 낮은 sequence 와 AUDIT_ONLY 는 보내지 않는다 | 3.2, G3 D-G3-05(동기화 중 해소된 이벤트 실시간 재알림 0) |
| `risk_event` 내용 | 전이 원문이 아니라 `risk_event` 행의 현재 상태로, REST 목록(`EventQueryRepository`, S15P31A101-121)과 같은 투영이다(값 없는 선택 칸은 뺌). `acks` 의 현장 버튼 ↔ SITE 짝이 틀린 행은 뺀다. 이벤트 하나 조회가 실패해도 나머지는 보낸다. delivery 가 AUDIT_ONLY 인 이벤트는 확인해도 보내지 않는다 | 관제 앱이 REST·STOMP 를 한 캐시에 합친다. 행 하나 때문에 메시지 전체가 계약 검사에서 버려지지 않게 한다 |
| `risk_event` 판 | 2.0.0. `escalations`(2.1.0)는 아직 읽지 않는다 | 상향 기록 저장이 아직 없다 |
| 지표 | `simai.i2.messages{topic, result=sent·replaced·failed}` | I1 의 `simai.i1.messages` 와 같은 모양 |
| 주기 작업 스케줄러 | `@Scheduled` 는 따로 둔 `taskScheduler`(`scheduling-`) | 브로커가 만드는 `messageBrokerTaskScheduler` 때문에 Spring Boot 기본 스케줄러가 빠지면 journal_ack 송신이 막힐 때 STOMP 하트비트가 밀린다 |

## 시험

`StompRelayIntegrationTest`: 실제 포트로 I1 에 보내고 `/ws` 를 구독한다. 받은 메시지는 `contracts/` JSON Schema(networknt, 시험 의존성)로 검사한다.
CONNECT 거부, `world_state` 그대로 중계, 동기화 중 해소 이벤트 미발행·DELAYED_ACTIVE·실시간 전이·늦게 온 낮은 sequence 미발행, 계약에 맞지 않는 확인 행 제외, 관리자 토픽·허용 밖 토픽·만료 토큰 구독 거부, 스케줄러 분리.

## 남은 일

- `/topic/health`(`viewer_health`) 조립·1초 발행은 S15P31A101-122. 그전까지 관제 앱은 health 가 오지 않아 연결을 끊김으로 표시한다.
- Web Push(I3)는 M1 알림 이슈.
- 브로커는 메모리(simple broker)다. EC2 한 대 기준이며, 여러 대로 늘리면 외부 브로커가 필요하다.
