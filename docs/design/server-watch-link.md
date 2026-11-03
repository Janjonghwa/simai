# EC2 워치 I4 연결과 연결 상태

S15P31A101-336. 명세 부록 C.6(채널 상태 BOUND·UNBOUND·LOST, I4 wss·기기별 토큰), FR-44, 12.2 I4 행, 12.4(`/topic/health`), 14(기기 토큰).
계약 `watch_heartbeat` 1.0.0(-334), `viewer_health` 1.2.0(이 이슈, 계약 묶음 7.1.0). 연결·끊김 규칙의 정본은 [`watch-i4-contract.md`](watch-i4-contract.md).
받는 쪽은 워치 앱 `watch/.../i4/`(-337), 화면 표시는 -342.

- 코드: `server/.../domain/watch/` — `WatchHub`(연결·하트비트·끊김), `WatchWebSocketHandler`, `WatchHandshakeInterceptor`, `WatchRepository`, `WatchProperties`, `WatchPaths`
- 연결: `SecurityConfig.deviceFilterChain`(기기 토큰), `WebSocketConfig`(`/ws/watch`), `ViewerHealthService`(`watches`), `HealthPublisher`(바뀌면 즉시)
- 표: V28 `watch_connection`(연결 기록), `device.last_heartbeat_at`(하트비트 수신)
- 시험: `server/src/test/.../domain/watch/WatchLinkIntegrationTest.java`

## 흐름

1. 워치 등록·토큰 발급·회전·폐기는 기존 관리 명령을 그대로 쓴다(새 API 없음, 14장·16장 "기기 생성은 관리 명령").
   `admin device register --id watch-01 --kind WATCH` 가 토큰 원문을 한 번 출력하고, 워치에는 -335 의 adb 설정 입구로 넣는다.
   `rotate`·`revoke`·`reactivate` 도 노트북과 같다.
2. 워치가 `wss://<EC2>/ws/watch` 로 `Authorization: Bearer <토큰>` 을 싣고 업그레이드한다. 기기 토큰 체인이 토큰 없음·틀림·폐기를 401 로,
   `WatchHandshakeInterceptor` 가 워치가 아닌 기기(노트북)를 403 으로 막는다. Nginx `location /ws` 가 그대로 넘긴다(읽기 시한 1시간).
3. 붙으면 `watch_connection` 에 한 행을 연다. 같은 워치의 열린 연결이 있으면 그 행을 REPLACED 로 닫고 옛 WebSocket 을 4001 로 닫는다.
4. 워치가 `watch_heartbeat`(watch 블록: 배터리·전송 경로·앱 버전)를 보내면 EC2 는 계약 스키마로 검사하고, 이 연결을 연 토큰이 아직 유효한지
   보며 `device.last_heartbeat_at` 을 적은 뒤, 바로 `watch_heartbeat`(연결별 `heartbeat_seq`, `echo{seq, received_at}`, `interval_ms` 20000)로 답한다.
5. `watch-sweeper` 스레드가 1초마다 마지막 하트비트(연결 직후는 연결 시각)에서 `interval × 3`(60초)이 지난 연결을 LOST 로 보고 4000 으로 닫는다.
6. 연결·끊김마다 `HealthState.watchesChanged()` 로 `viewer_health` 를 바로 다시 보낸다(워치별 link 가 바뀌는 때). 하트비트로는 link 가
   바뀌지 않으므로 알리지 않고, 배터리·마지막 수신 시각은 1초 주기에 실린다.

## viewer_health.watches (1.2.0)

```json
"watches": {
  "registered": 2, "connected": 1,
  "items": [
    { "device_id": "watch-01", "link": "CONNECTED", "last_heartbeat_at": 1760342392000, "battery_pct": 72, "transport": "WIFI" },
    { "device_id": "watch-02", "link": "LOST", "last_heartbeat_at": 1760338800000, "battery_pct": null, "transport": null }
  ]
}
```

-342 의 "추적 인원 vs 활성 워치 수" 에서 활성 워치 수는 `connected` 다. BOUND·UNBOUND(사람과의 배정)는 -340 이 정한다.

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 경로 | `/ws/watch` | 워치 앱(-337)이 이미 쓰는 값(`WATCH_WS_PATH`), I4 계약 문서의 기본값. Nginx `/ws` 아래라 설정 변경 없음 |
| 인증 | 기존 기기 토큰 체인 + kind WATCH 만 | 12.2 "기기별 토큰". CCTV 수신(LAPTOP 만)과 같은 방식. 노트북 토큰으로 워치 행세 금지 |
| 하트비트 주기 | `simai.watch.heartbeat-interval` 20초, 끊김 60초(3회) | 실기기 절전 중 하트비트 최대 27초(watch-app.md). 3회 규칙은 계약·노트북과 같다 |
| 끊김 시계 | EC2 단조 시계(System.nanoTime), 판정 간격 1초. 닫기·기록은 연결마다 가상 스레드로 넘긴다 | FR-11 받는 쪽 단조 시계. 판정 지연은 최대 1초. 반쯤 열린 TCP 나 느린 DB 가 다른 워치의 판정을 밀지 않게 |
| 끊김 뒤 처리 | 연결을 닫는다(4000 `heartbeat_timeout`) | 반쯤 열린 연결을 붙잡고 있지 않게. 워치는 닫힘을 보고 바로 백오프 재연결(-337) |
| 연결 직후 | 하트비트 전에도 CONNECTED, 연결 시각부터 센다 | 첫 하트비트는 연결 직후 온다. 하트비트 없이 붙기만 한 연결도 60초 뒤 닫힌다 |
| 같은 워치 두 연결 | 새 연결이 이긴다(옛 연결 4001 `replaced`) | 워치는 반쯤 열린 연결을 모른 채 다시 붙는다. 열린 연결 하나(부분 유일 색인) |
| 연결 중 폐기·회전 | 다음 하트비트에서 닫는다(4003 `token_invalid`). 이 연결을 연 토큰의 SHA-256 을 세션에 두고 `device.token_hash` 와 대조 | 관리 명령은 다른 프로세스라 EC2 에 알림을 줄 수 없다. 하트비트마다 이미 쓰는 UPDATE 한 번에 조건을 붙여 추가 조회 없음 |
| DB 장애 중 하트비트 | 답하고 연결을 둔다 | 개인 알림 채널을 DB 때문에 끊지 않는다. 토큰은 업그레이드 때 확인했다. 다음 하트비트에 다시 본다 |
| 틀린 메시지 | 버리고 연결 유지(봉투 오류·판 다름·source_id 다름·계약 위반) | I1 과 같은 까닭(끊으면 다시 붙어 되풀이). 계속 틀리면 하트비트가 없으니 60초 뒤 닫힌다 |
| watch_ack·gait_features | 받기만 한다 | 격상은 -338, 걸음 특징 저장·배정은 -340 |
| System Health 반영 | `viewer_health.watches`(선택 필드, 마이너). `system_health`·`remote_state`·장애 기록(`system_outage`)에는 넣지 않는다 | 워치는 개인 보조 채널(C.6)이다. 워치 하나가 끊겼다고 관제 정지·UNSAFE 로 보이면 안 된다 |
| LOST 와 한 번도 안 붙음 | 둘 다 LOST(`last_heartbeat_at` 으로 구분: 받은 적 없으면 null) | C.6 상태 셋(BOUND·UNBOUND·LOST)에 연결 단계 값은 LOST 뿐. 화면은 시각으로 구분할 수 있다 |
| 배터리·전송 경로 | CONNECTED 일 때만, 그 밖 null | 끊긴 워치의 옛 배터리를 지금 값처럼 보이지 않게 |
| 연결 기록 표 | `watch_connection`(연결·끊김 경계만, 하트비트마다 쓰지 않음). 하트비트 수신은 `device.last_heartbeat_at` | -341 연결 비율과 LOST 근거. `device_heartbeat` 는 노트북 전용 칸(NN `tier0_control` 등)이라 쓰지 않는다. 다른 확장 표(`watch_binding`·`channel_state`·`watch_feature`·`watch_alert`)는 쓰는 이슈(-338·-340)에서 만든다 |
| 종료·재시작 | 정상 종료는 열린 연결을 EC2_RESTART 로 닫는다(1001). 비정상 종료 뒤에는 뜰 때 열린 행을 EC2_RESTART 로 닫되, 끝 시각은 기동 시각이 아니라 그 연결의 마지막 하트비트(없으면 연결 시각) | EC2 가 내려가 있던 시간을 연결 시간에 넣지 않는다(-341 연결 비율). 메모리 상태는 비어 있다가 워치 재연결로 찬다 |
| 끝 사유 구분 | 워치·네트워크가 닫으면 CLOSED, 송신이 밀려 데코레이터가 닫으면 SEND_FAILED | 서버 원인 끊김을 워치 쪽 끊김으로 세지 않게 |
| 기록하는 마지막 하트비트 | `device.last_heartbeat_at` 이 그 연결이 열린 뒤 값일 때만 옮긴다 | 하트비트 없이 끝난 연결에 앞 연결의 시각을 적지 않게 |
| 겹친 재연결 | 워치별 잠금 안에서 행 열기와 메모리 교체를 함께 한다 | DB 의 열린 행과 메모리의 연결이 늘 같은 연결을 가리키게 |
| 송신 | 세션마다 `ConcurrentWebSocketSessionDecorator`(10초, 64 KiB) | -338 이 다른 스레드에서 `watch_alert` 를 보낼 때 하트비트 답과 겹치지 않게 |

## 시험

`WatchLinkIntegrationTest`(시험 설정: 하트비트 1초 → 끊김 3초, 판정 0.2초). 실제 포트 + `StandardWebSocketClient`.

- 업그레이드: 토큰 없음·틀림·노트북 토큰·폐기된 워치 거부, 연결 기록 없음
- 하트비트 답: 계약 스키마 통과, `source_id` ec2, EC2 seq 0·1, echo seq·수신 시각, `interval_ms` 1000. `viewer_health.watches` CONNECTED·배터리·전송 경로,
  연결 기록의 첫 하트비트·전송 경로 변경·앱 버전, `device.last_heartbeat_at`
- 하트비트를 0.5초마다 4초 이어 가면 유지, 멈추면 2~4.5초 안에 4000 으로 닫히고 LOST(배터리·전송 경로 null, 마지막 수신 시각 유지), 기록 HEARTBEAT_TIMEOUT
- 하트비트 없이 붙기만 한 연결도 기한 뒤 닫힘
- 연결 중 폐기 → 다음 하트비트에서 4003, 답 없음, 기록 TOKEN_INVALID, 목록에서 빠짐
- 같은 워치 재연결 → 옛 연결 4001, 기록 REPLACED + 새 행 열림
- 워치가 닫음 → 기록 CLOSED, LOST
- source_id 다름·watch 블록 없음·JSON 아님 → 답 없음, 그 뒤 정상 하트비트에 답(연결 유지)
- `viewer_health` 1.2.0 계약 통과, registered = 항목 수, connected = CONNECTED 수, device_id 오름차순, 노트북 제외

## 리뷰에서 두기로 한 것

- `viewer_health` 를 만들 때마다(1초) 등록된 워치를 `device` 표에서 다시 읽는다: 색인 조회 한 번이고 `viewer_health` 는 이미 매초 쿼리 4개를 쓴다. 워치 등록·폐기가 바로 보이는 쪽을 택했다.
- `deviceKind` 조회가 하트비트·CCTV·워치 리포지토리에 따로 있다: 도메인마다 자기 리포지토리를 두는 기존 방식을 따랐다.

## 남은 일

- 실제 워치(갤럭시워치6)를 운영 EC2 에 붙여 보는 확인은 워치 앱 스토리(-337 W4)·측정(-341)에서 한다. 이 이슈는 시험용 클라이언트로만 확인했다.
- 운영 EC2 에 워치 토큰 등록(`admin device register --kind WATCH`)과 워치 앱 설정 입구로 넣기.
- -338: `watch_alert` 보내기(`WatchHub` 에 기기별 송신 추가), `watch_ack` 처리·격상, `watch_alert` 표.
- -340: `gait_features` 저장(`watch_feature`)·배정(`watch_binding`·`channel_state`), BOUND·UNBOUND.
- -342: 관제 화면이 `watches` 를 읽어 활성 워치 수·LOST 표시.
- LOST 를 반장 PWA 로 Web Push 하는 것은 Web Push 이슈 몫(지금은 관제 화면 `/topic/health` 만).
