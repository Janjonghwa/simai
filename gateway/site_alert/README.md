# gateway/site_alert — 노트북 현장 경보 단말

RunPod Tier 0 가 LIVE 로 판정한 CRITICAL·HIGH 경보(`local_alert`)를 I0 제어 채널로 받아 노트북 스피커·화면으로 울린다
(S15P31A101-90). EC2 를 거치지 않는 가장 짧은 경로다(명세 3.2, 3.10, 7.3).

| 명세·계약 | 이 단말에서 |
|---|---|
| 3.10 노트북 로컬 경보 | `local_alert` RAISED·UPDATED 에 화면 표시와 등급별 소리 반복. 소리는 확인까지, 표시는 해소(RESOLVED)까지 |
| 7.3 채널 표, AGENTS.md 규칙 5 | `source` 가 LIVE 가 아니거나(없으면 포함) `severity` 가 CRITICAL·HIGH 가 아니면 거부. 신뢰도 LOW 는 등급을 그대로 두고 '확인 필요' |
| 3.2 운영자 ACK, `alarm_stop` | 소리만 멈추고 표시는 유지. 누가 확인했는지(채널·계정 ID) 표시. 확인 뒤 같은 이벤트가 HIGH → CRITICAL 로 올라가면 확인을 풀고 다시 울림(같은 등급·하향은 유지). `event_sequence`(확인 대상 상태)가 그 경보 단계 시작(상향·재개)보다 앞이면 늦은 정지로 보고 멈추지 않음 |
| 3.10 현장 확인, `site_ack` | 버튼을 누르면 즉시 소리 정지 → `site_ack`(누를 때 표시하던 `event_sequence` 포함) 송신. Tier 0 기록(`alarm_stop`)이나 해소를 받기 전에 끊기면 재접속 때 같은 봉투(같은 `ack_id`)로 다시 보냄 |
| 3.10 재동기화, `health.active_site_alerts` | 목록에 맞춰 켜고 끔: 처음 보는 이벤트는 표시, `acknowledged` 면 소리만 정지·아니면 반복, 빠진 이벤트는 해소. `active_site_alerts_as_of` 와 같거나 뒤에 보낸 `local_alert` 로 켠 경보는 목록에 없어도 유지, 확인은 `as_of` 보다 뒤에 보낸 `alarm_stop`·`local_alert` 만 유지 |
| 3.10 시험 알림, 18.2 시작 절차 | 시작할 때 시험 경보(“시험” 표시, 실제 경보와 다른 소리). RiskEvent·`site_ack` 를 만들지 않음 |
| 계약 `event_sequence` | `(event_id, event_sequence)` 로 중복·역순 무시. 해소된 이벤트의 sequence 도 기억해 늦은 RAISED 로 되살리지 않음 |
| 부록 D-G0-04 | 수신 → 소리 시작 호출 지연을 `bench` 로 잰다 |
| 3.2 이중 감시, 3.10 표, 6.5 | 장애별 현장 표시(S15P31A101-91): Tier 0·영상이 끊기면 “관제 중단”, EC2 만 끊기면 “원격 관제 장애”, 정상이면 “관제 정상”. 원인과 연결 상세(Tier 0 · 영상 n/4대 · EC2)를 함께 띄운다. 아래 '장애 감시' |
| FR-134, 12.6, 계약 `laptop_heartbeat` | 1초마다 노트북 → EC2 `POST /api/devices/heartbeat`(HTTPS, 기기 토큰)와 노트북 → Tier 0(I0 제어 채널) 하트비트. 내용은 장애 상태 기계가 만든다 |
| 3.12 엔드포인트 등록, 12.7·14 공개키 고정 | Tier 0 주소·포트는 EC2 `GET /api/endpoints/tier0`(기기 토큰)로 조회해 붙고, 끊기거나 연결 중 재조회(30초)에서 바뀌면 다시 붙음. 서버 공개키 지문이 미리 고정한 값과 다르면 기기 토큰을 보내기 전에 끊음(S15P31A101-159) |

## 구조

`ai/`·`gateway/recorder` 와 분리된 독립 uv 프로젝트다. 실행 의존성은 `websockets` 하나이고 소리(`winsound`, `ctypes` winmm)와
화면(`tkinter`)은 표준 라이브러리다. 실행 중에는 계약 스키마를 읽지 않고, 시험이 저장소 `contracts/` 의 스키마·예시로
해석기와 만든 메시지를 검증한다(다른 브랜치 스키마로 돌리려면 `SIMAI_CONTRACTS_DIR`).

```
src/simai_site_alert/
  core.py       경보 코어: 활성 경보 집합(event_id → ActiveAlert), 중복·역순, ACK, 현장 확인, 시험 경보, 소리 판단
  messages.py   I0 메시지 해석(local_alert·alarm_stop·health)과 site_ack 생성
  sound.py      등급별 소리 패턴, 소리 백엔드(WinSound / Fake / Null)
  display.py    화면 문구(순수 함수, 헤드리스 시험)와 tkinter 창
  transport.py  I0 제어 채널 인터페이스와 WebSocket 구현(wss, 기기 토큰, 엔드포인트 조회·재접속), 시험용 InMemory 채널
  endpoint.py   EC2 엔드포인트 조회(tier0_endpoint 해석)와 고정 지문 확인 → 접속 대상
  link_status.py 장애 상태 기계: Tier 0·영상 중계·EC2 연결 → 현장 표시(site_display)·laptop_heartbeat 내용
  heartbeat.py  노트북 → EC2 하트비트(HTTPS POST, 기기 토큰, 대기열 없음)
  relay_status.py 영상 중계 → 단말 상태 보고(127.0.0.1 UDP, 노트북 안 내부 형식)
  pinning.py    Tier 0 TLS 공개키 지문(SPKI SHA-256) 계산과 고정 검사
  winsock.py    Windows WinError 10014 일시 오류만 짧게 다시 시도(relay 도 씀, docs/design/gateway-winsock-retry.md)
  app.py        조립: 제어 채널 → 코어 → 소리·화면, 주기 tick, 재접속 때 site_ack 재전송
  bench.py      수신 → 소리 시작 지연 측정 (loopback WebSocket)
  config.py     .env 설정
  clock.py      주입하는 시계
tests/          시험 (소리를 내지 않는다)
```

코어는 전송·화면을 모른다. 받은 원문과 수신 시각만 받고, 상태가 바뀔 때마다 `ScreenState` 를 화면에 넘긴다.
소리 백엔드는 주입한다(`start(pattern)` 은 반복 재생을 걸고 바로 돌아온다, `stop()`, `check()`).

### 소리

| 패턴 | 한 주기 |
|---|---|
| CRITICAL | 1400/1000 Hz 0.16 s 번갈아 6번(빠른 경보음) + 쉼 0.2 s, 1.4 s 주기 |
| HIGH | 880 Hz 0.3 s 두 번, 1.5 s 주기 |
| TEST(시험) | 660 Hz 0.25 s 두 번, 2 s 주기(낮고 느림) |

여러 경보가 울리면 확인 전 경보 중 가장 높은 등급의 패턴 하나를 반복한다. 실제 경보가 있으면 시험음보다 먼저다.
`WinSoundBackend` 는 패턴마다 WAV 를 시작할 때 만들어 두고 `PlaySound(SND_ASYNC|SND_LOOP)` 로 반복한다.
출력 장치가 없거나(`waveOutGetNumDevs() == 0`) 재생이 실패하면 화면 맨 위에 “소리 장치 오류 - 소리 없이 화면으로만
경보 중”을 크게 띄우고, 1초마다 다시 시도한다. 장치 확인이 실패한 뒤에는 다시 확인될 때까지 오류 표시를 내리지 않는다.

### 상태 모델과 재동기화

상태는 '활성 경보 집합'(event_id → `ActiveAlert`)이다. Tier 0 가 (재)접속 직후와 1Hz `health` 마다 보내는
`active_site_alerts` 로 이 집합을 맞춘다(S15P31A101-161). `local_alert`·`alarm_stop`·해소를 놓치거나 끊겼다 붙어도
다음 목록에서 Tier 0 활성 경보와 같아진다.

- 목록의 `event_sequence` 가 반영한 것보다 낮으면 무시(화면을 되돌리지 않음), 높으면 등급·문구를 맞춘다. 등급이 올라갔으면
  (놓친 상향) 확인을 풀고 다시 울린다. 해소로 기억한 sequence 이하의 항목으로 되살리지 않는다.
- 목록의 `acknowledged` 가 false 인데 노트북이 확인 상태면 다시 울린다(놓친 재개 등). 단 Tier 0 기록 전인 현장 확인,
  `as_of` 보다 뒤에 보낸 `alarm_stop`·`local_alert` 로 받은 확인은 유지한다. 같은 ms 는 유지하지 않는다(소리를 다시 켜는 쪽).
- 목록에서 빠진 경보는 해소로 보고 내린다. `as_of` 와 같거나 뒤에 보낸 `local_alert` 로 켠 경보, 형식이 틀려 판단할 수 없는 항목의
  경보는 이번 목록으로 끄지 않는다. `active_site_alerts` 가 없는 `health`, `event_id` 를 읽을 수 없는 항목이 있는 목록으로도
  끄지 않는다. LIVE·CRITICAL·HIGH 가 아닌 항목은 켜지 않는다.
- 각 경보는 지금 경보 단계를 시작한 sequence(`alarm_sequence`: 처음 받은 경보·상향·재개)를 기억한다. `alarm_stop.event_sequence`
  가 그보다 낮으면 상향 전 상태에 대한 늦은 정지라 멈추지 않는다. 계약 `alarm_stop.event_sequence` 는 확인 대상 상태다.
  Tier 0 내부 `AlarmStop.event_sequence`(ACKNOWLEDGED 전이 번호)가 아니라 `target_sequence` 를 옮긴 값이다
  (`simai_ai.risk.alarm_stop_contract_payload`).
- 한계: 해소(RESOLVED)를 놓친 뒤의 재개는 UPDATED 로 와서 노트북은 재개인지 알 수 없다. 확인된 경보가 재개돼도 다음 목록
  (최대 약 1초)까지 조용하고, 그 사이 옛 단계를 대상으로 한 늦은 `alarm_stop` 도 적용된다. 다음 목록이 바로잡는다.

`core.alarm_status()` 는 `laptop_heartbeat.alarm` 형식이다(하트비트에 그대로 싣는다).

### 장애 감시 (S15P31A101-91)

노트북 쪽 장애 판단은 `link_status.SiteLinkMonitor` 한 곳에서 하고, 화면과 두 하트비트가 그 결과를 쓴다.

| 상황 (명세 3.10 표) | 현장 표시 | 판단 근거 (받는 쪽 단조 시계, 1초 주기 3회 연속 누락 = 3초) |
|---|---|---|
| RunPod Tier 0 중단 | 관제 중단 `CONTROL_HALTED` | 제어 채널 끊김(즉시), health 3초 없음, 처리 정체(`t_latest_global` 이 3초 동안 늘지 않음), `system_health` 가 UNSAFE_UNKNOWN 이거나 없음 |
| 노트북 → RunPod 영상 단절 | 관제 중단 | 영상 중계가 Tier 0 영상 연결이 0대라고 알림(즉시), 중계 상태 보고 3초 없음(모름) |
| EC2 만 단절 | 원격 관제 장애 `REMOTE_FAULT` | EC2 하트비트에 3초 동안 2xx 응답 없음. 현장 경보는 그대로 울린다 |
| 분석 워커 중단 | (만들지 않음) | 노트북에 오는 신호가 없다. `AI_DELAY` 를 지어내지 않는다 |

- 모르면 정상으로 보지 않는다: 시작 직후는 관제 중단, EC2 응답을 받기 전은 원격 장애다.
- 장애로는 즉시 바꾸고, 정상으로는 정상 조건이 2초 이어질 때만 되돌린다(깜빡이는 연결에서 '정상'이 잠깐씩 보이지 않게).
  제어 채널이 끊기면 그 연결에서 받은 health 를 버린다(재접속 뒤 새 health 가 와야 정상). 둘이 겹치면 관제 중단이 먼저다.
- 연결은 살아 있는데 health 가 3초 넘게 없으면 제어 채널을 바로 끊고(닫기 인사를 기다리지 않음) 다시 붙는다(반쯤 열린 연결).
- 판단 주기는 0.5초다. 끊김 3초 + 0.5초 안에 화면이 바뀐다(G3 현장 5초).
- 벽시계는 `last_message_at`·`last_response_at` 기록에만 쓴다. 노트북 시계를 되돌려 `t_latest_global` 이 줄면 기준만 옮기고
  다음 증가를 진행으로 본다.

하트비트 (계약 `laptop_heartbeat` 1.2.0):

- `tier0.control` 은 계약 정의대로 제어 채널이 연결돼 있고 3초 안에 health 를 받았는지다. 처리 정체·판정 불가는
  `site_display=CONTROL_HALTED` 로 알린다. `video_cameras` 는 중계 보고가 살아 있을 때만 채우고(모르면 빈 배열),
  `cameras` 는 그때만 싣는다. `ec2` 는 Tier 0 로 보내는 하트비트에만 싣는다.
- EC2: HTTPS 만(평문 http 는 루프백 시험 서버도 불가), 인증서 검증, 리다이렉트 안 따름. 기기 토큰은 Authorization 헤더에만
  싣고 주소·로그·예외 문장·repr 에 넣지 않는다. 대기열이 없다: 보낼 때마다 그 순간 상태로 만들고 한 번에 하나만 보낸다.
  EC2 가 느리거나 막혀도 쌓이지 않고, 송신은 따로 도는 스레드라 현장 경보 경로를 기다리게 하지 않는다. 원격 장애 판단은
  응답 결과가 아니라 '마지막 2xx 이후 경과'로 하므로 송신 스레드가 막혀도 3초 뒤 표시가 바뀐다.
- Tier 0: 연결돼 있으면 1초마다 제어 채널로 보낸다(기다리지 않음).

영상 상태는 중계(`gateway/relay`, 별도 프로세스)가 1초마다 127.0.0.1 UDP(`SIMAI_RELAY_STATUS_PORT`, 기본 47031)로 알린다
(`relay_status.py`, 노트북 안 내부 형식이라 계약이 아니다). 형식이 틀린 보고는 통째로 버리고 같은 중계의 늦은 보고도 버린다.
포트를 열지 못하면 영상 상태를 모름으로 두고(관제 중단) 경보는 계속 돈다.

## 설치와 시험

`gateway/site_alert/` 에서 실행한다.

```sh
uv sync
uv run pytest
```

시험은 소리를 내지 않는다(가짜 백엔드). 제어 채널 시험은 같은 컴퓨터에 시험용 WebSocket 서버를 띄워 실제 클라이언트로 붙는다.
tkinter 시험은 창을 잠깐 띄웠다 닫는다(화면이 없으면 건너뜀).

## 실행

1. 저장소 루트 `.env` 에 설정을 넣는다(`gateway/site_alert/.env.example` 참고, 커밋 금지).
   `SIMAI_EC2_BASE_URL`(https), `SIMAI_DEVICE_TOKEN`(비밀), `SIMAI_TIER0_PINNED_SPKI_SHA256`(미리 고정한 Tier 0 공개키 지문),
   `SIMAI_LAPTOP_ID`. Tier 0 주소·포트는 적지 않는다(Pod 가 바뀌면 EC2 에서 새 값을 받는다). EC2 하트비트 때문에
   `SIMAI_EC2_BASE_URL` 은 https 여야 한다. 영상 중계와 상태 포트를 바꾸면 `SIMAI_RELAY_STATUS_PORT` 와 중계 `--status-port` 를 같게 한다.
2. 실행하면 먼저 시험 경보가 4초 울린다. 소리가 들리고 “시험” 화면이 보이는지 확인한다. 창의 '시험 경보' 버튼으로 다시 울릴 수 있다.

```sh
uv run simai-site-alert run                    # 창(tkinter) + Windows 소리
uv run simai-site-alert run --fullscreen
uv run simai-site-alert run --offline          # 제어 채널 없이 시험 경보·화면만 확인
uv run simai-site-alert run --offline --headless --sound none   # 창·소리 없이 문구만(소리 장치 오류 표시 확인)
```

### 지연 측정

```sh
uv run simai-site-alert bench --count 300              # 가짜 소리 백엔드
uv run simai-site-alert bench --sound win --count 100  # 실제 winsound (짧은 경보음이 난다)
```

재는 구간은 제어 채널이 메시지를 받은 시각 → 소리 백엔드 `start()` 가 돌아온 시각이다. Tier 0 → 노트북 네트워크와
스피커 출력 지연은 들어가지 않는다(종단 지연은 G0 glass-to-alarm 측정, 명세 15.1).

## 하지 않는 것 (다른 티켓)

- 카메라 영상 TLS 연결(공개키 고정 검사는 `pinning.PinnedPeerVerifier` 를 같이 쓴다), RunPod HTTPS 프록시 예비 경로.
- 시계 차이 교환. “AI 분석 지연” 표시(분석 워커 상태가 노트북에 오지 않는다).
- Tier 0 쪽 `laptop_heartbeat` 수신 감시(`health.gateway_link` 판정)와 EC2 쪽 하트비트 수신·구분 표시(server).
- 실제 Tier 0 연결.
