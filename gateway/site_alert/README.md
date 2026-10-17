# gateway/site_alert — 노트북 현장 경보 단말

RunPod Tier 0 가 LIVE 로 판정한 CRITICAL·HIGH 경보(`local_alert`)를 I0 제어 채널로 받아 노트북 스피커·화면으로 울린다
(S15P31A101-90). EC2 를 거치지 않는 가장 짧은 경로다(명세 3.2, 3.10, 7.3).

| 명세·계약 | 이 단말에서 |
|---|---|
| 3.10 노트북 로컬 경보 | `local_alert` RAISED·UPDATED 에 화면 표시와 등급별 소리 반복. 소리는 확인까지, 표시는 해소(RESOLVED)까지 |
| 7.3 채널 표, AGENTS.md 규칙 5 | `source` 가 LIVE 가 아니거나(없으면 포함) `severity` 가 CRITICAL·HIGH 가 아니면 거부. 신뢰도 LOW 는 등급을 그대로 두고 '확인 필요' |
| 3.2 운영자 ACK, `alarm_stop` | 소리만 멈추고 표시는 유지. 누가 확인했는지(채널·계정 ID) 표시. 확인 뒤 같은 이벤트가 HIGH → CRITICAL 로 올라가면 확인을 풀고 다시 울림(같은 등급·하향은 유지) |
| 3.10 현장 확인, `site_ack` | 버튼을 누르면 즉시 소리 정지 → `site_ack` 송신. Tier 0 기록(`alarm_stop`)이나 해소를 받기 전에 끊기면 재접속 때 같은 봉투(같은 `ack_id`)로 다시 보냄 |
| 3.10 시험 알림, 18.2 시작 절차 | 시작할 때 시험 경보(“시험” 표시, 실제 경보와 다른 소리). RiskEvent·`site_ack` 를 만들지 않음 |
| 계약 `event_sequence` | `(event_id, event_sequence)` 로 중복·역순 무시. 해소된 이벤트의 sequence 도 기억해 늦은 RAISED 로 되살리지 않음 |
| 부록 D-G0-04 | 수신 → 소리 시작 호출 지연을 `bench` 로 잰다 |

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
  transport.py  I0 제어 채널 인터페이스와 WebSocket 구현(wss, 기기 토큰), 시험용 InMemory 채널
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

상태는 '활성 경보 집합'이다. `health.active_site_alerts` 로 이 집합을 맞추는 재동기화(목록에 있으면 표시, `acknowledged`
면 소리만 정지, 빠지면 해소)는 S15P31A101-161 에서 붙인다. 지금은 `health` 를 받아 `core.last_health` 에 두기만 한다.
`core.alarm_status()` 는 `laptop_heartbeat.alarm` 형식이다(하트비트 송신은 이 티켓 밖).

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
   `SIMAI_TIER0_CONTROL_URL`(wss), `SIMAI_DEVICE_TOKEN`(비밀), `SIMAI_LAPTOP_ID`.
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

- Tier 0 공개키 지문 고정과 EC2 엔드포인트 조회(S15P31A101-159). `WebSocketControlChannel(peer_verifier=...)` 자리만 있다.
  인증서 검증을 끄는 경로는 없고, `ws://` 는 loopback 시험에서만 허용한다.
- 활성 현장 경보 목록 재동기화(S15P31A101-161).
- `laptop_heartbeat` 송신, 장애별 현장 표시(“관제 중단”·“원격 관제 장애”) 판정, 시계 차이 교환.
- 실제 Tier 0 연결.
