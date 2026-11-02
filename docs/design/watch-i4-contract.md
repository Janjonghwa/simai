# I4 워치 ↔ EC2 메시지 계약 (S15P31A101-334)

워치 알림 확장(명세 부록 C.6, FR-43·44·45·46)의 구간 계약. 워치 앱(Wear OS)과 EC2 가 wss 지속 연결로 주고받는 네 메시지를
`contracts/messages/` 에 공통 봉투로 만들었다(계약 묶음 6.10.0). 이 문서는 계약이 적지 않는 연결·전송 규칙과, 명세가 비워 둔 곳에서
정한 값을 적는다. 필드 정의의 정본은 스키마다.

- 관련 명세: 부록 C.6, 12.1(봉투), 12.2(I4 행), 7.3(개인 알림 등급), 3.2(운영자 확인과의 구분), 11.4-5(이름 금지), 21.2(생성 타입)
- 스키마: `contracts/messages/watch_alert.schema.json`, `watch_heartbeat.schema.json`, `watch_ack.schema.json`, `gait_features.schema.json`
- 예시: `contracts/examples/watch_*.json`, `gait_features*.json`, 반례 `contracts/examples/invalid/watch_*.json`, `gait_features.*.json`
- 생성 타입: Python `ai/src/simai_ai/contracts/models.py`(다시 만듦), Java `server/build/generated-sources/js2p`(빌드 산출물),
  TS 는 I2 메시지만 만들므로 바뀌지 않음
- 뒤 이슈: -336(EC2 워치 등록·연결·하트비트), -337(워치 앱 연결·ACK), -338(EC2 발송·격상), -339(워치 가속도 특징), -340(1:1 배정)

## 메시지와 흐름

| 메시지 | 방향 | 언제 | 핵심 필드 |
|---|---|---|---|
| `watch_alert` | EC2 → 워치 | 진입 예측(R5) 판정이 BOUND 워치에 닿을 때 | `event_id`·`event_sequence`, `severity`(CRITICAL·HIGH·CAUTION), 선택 `vibration_pattern`·`direction`, `word`, `ack_within_ms`, `needs_check`. 알림 ID 는 봉투 `msg_id` |
| `watch_ack` | 워치 → EC2 | 진동 시작 즉시(VIBRATED), 작업자가 확인 버튼을 누르면(CONFIRMED) | `alert_msg_id`, `kind`, `acked_at`(워치 시계), 선택 `received_at` |
| `watch_heartbeat` | 양방향 | 워치가 `interval_ms`(기본 10초)마다, EC2 는 받을 때마다 바로 답 | `heartbeat_seq`, `echo{seq, received_at}`, EC2 → 워치만 `interval_ms`, 워치 → EC2 만 `watch{battery_pct, transport, app_version}` |
| `gait_features` | 워치 → EC2 | 4초 창을 2초마다(절반 겹침) | `clock_basis` WATCH, `clock_offset_ms`(null 허용), 창 시작·끝, `sample_rate_hz`, `step_times_ms`, `motion_transitions`, `band_magnitude{rate_hz, values, rms, peak_freq_hz}` |

```
워치                                  EC2
 │── (wss 연결, 기기 토큰) ───────────▶│   -336: device.kind=WATCH 로 등록된 토큰만
 │── watch_heartbeat seq0 (watch 블록)▶│
 │◀─ watch_heartbeat seq0 (echo, interval_ms)│
 │── gait_features (2초마다) ─────────▶│   -339/-340: watch_feature 저장·배정
 │                                     │
 │◀─ watch_alert (msg_id=A) ───────────│   -338: watch_alert 행 생성, ack_within_ms 타이머
 │   진동 시작                          │
 │── watch_ack A VIBRATED ────────────▶│   acked_at 기록, 타이머 해제
 │   작업자 확인 버튼                    │
 │── watch_ack A CONFIRMED ───────────▶│   confirmed_at 기록
 │                                     │
 │   (ACK 없이 ack_within_ms 경과)      │   -338: 현장 경보·관리자 알림으로 격상(escalated_at)
```

## 연결·전송 규칙 (스키마 밖)

- 연결은 EC2 의 wss 한 개(경로는 -336 에서 정함, 기본 `/ws/watch`). 워치는 기기별 토큰으로 인증하고, 토큰은 메시지에 싣지 않는다
  (-335 의 adb 설정 입구로 넣은 값). 봉투 `source_id` 는 워치의 `device.device_id`(`watch-01` 꼴, `instance_id` 패턴), EC2 는 `ec2`.
- 끊김 판정: EC2 는 워치 하트비트가 `interval_ms × 3` 동안 없으면 그 워치를 LOST 로 보고 System Health 에 반영한다(-336).
  워치는 자기 하트비트 3회에 EC2 답(echo)이 없으면 연결을 닫고 지수 백오프(1·2·4…최대 30초)로 다시 잇는다(-337).
- 중복·순서: EC2 는 `watch_ack` 를 `(alert_msg_id, kind)` 로, `gait_features` 를 봉투 `msg_id` 로 중복 제거한다. 워치는 같은
  `watch_alert.msg_id` 를 다시 받으면 진동하지 않고 ACK 만 다시 보낸다. 끊긴 동안 만든 ACK 는 다시 이어지면 원래 `acked_at` 그대로 보낸다.
- 시계: 워치 시계는 믿지 않는다. 시계 차이는 하트비트 `echo` 로 NTP 처럼 잰다: 워치가 seq n 을 t1(`sent_at`)에 보내고 EC2 가
  `echo{n, t2}` 를 t3(`sent_at`)에 답해 워치가 t4 에 받으면 워치 − EC2 ≈ ((t1 − t2) + (t4 − t3)) / 2(워치가 d 만큼 빠르면 +d). EC2 → 워치 답에는 echo 가 필수다. 워치는 이 값을
  `gait_features.clock_offset_ms` 에 싣고, EC2 는 `watch_alert` 의 확인 기한을 자기 `sent_at` 기준으로만 센다.
- 지연 측정(C.6 목표 '위험 진입 → 진동 p95 1.5초'): EC2 시계 기준 `(watch_ack.received_at − clock_offset_ms) − watch_alert.sent_at` 을 -338 이 기록한다(clock_offset_ms 는 그 워치의 최근 gait_features 또는 하트비트 echo 로 EC2 가 직접 센 값). 확인 기한 판정은 ACK 가 EC2 에 닿은 EC2 시각으로만 한다(워치 시계 acked_at 은 기록용).

## 명세가 비워 둔 곳에서 정한 것

| 항목 | 정한 값 | 근거 |
|---|---|---|
| 메시지 이름 | `watch_alert`·`watch_heartbeat`·`watch_ack`·`gait_features` (C.6 의 `heartbeat`·`ack` 에 `watch_` 접두) | 생성 클래스가 `Heartbeat`·`Ack` 처럼 모호해지고, 기존 이름 규칙(`laptop_heartbeat`·`site_ack`·`job_heartbeat`)과 맞춤. C.6 본문을 같이 고침 |
| 운영자 확인 채널 | `ack_channel`(CONTROL_ROOM·FOREMAN_PWA·SITE_BUTTON)은 그대로. `watch_ack` 는 `operator_ack` 를 쓰지 않는 별도 메시지 | 워치 ACK 는 위험에 처한 당사자의 수신 확인이지 운영자 확인(3.2)이 아니다. 이벤트 상태(ACKNOWLEDGED)를 바꾸지 않고 현장 경보도 멈추지 않는다. 그래서 `event_ack`·프론트 상수·생성 타입을 건드리지 않는다. 워치 ACK 로 경보를 멈출지는 -338 에서 따로 정한다 |
| 알림 ID | 봉투 `msg_id` 를 알림 ID 로 쓰고 `watch_ack.alert_msg_id` 로 돌려준다 | 필드를 늘리지 않고 중복 제거 키와 같은 값을 쓴다. `watch_alert` 표에 `alert_msg_id` UQ 추가 |
| 보내는 등급 | CRITICAL·HIGH·CAUTION. INFO 불가 | 7.3 개인 알림. 현장 경보·푸시는 CRITICAL·HIGH 만이지만 FR-45 가 CAUTION 약한 진동을 정했으므로 CAUTION 포함 |
| 진동 패턴 | 선택 `vibration_pattern` DANGER_LONG·CAUTION_DOUBLE·CAUTION_WEAK. 없으면 워치가 등급으로: CRITICAL → DANGER_LONG, HIGH → CAUTION_DOUBLE, CAUTION → CAUTION_WEAK | FR-45. -390 이 앱 안에서만 쓰던 CAUTION_WEAK 를 계약 값으로 올리고 `watch_alert` 표 CK 에 더함. EC2 가 격상 재발송 등으로 등급보다 센 패턴을 고를 수 있게 선택 필드 |
| 방향 기준 | 작업자 기준 FRONT·BACK·LEFT·RIGHT. Tier 0 트랙 `velocity`(World XY)의 진행 방향이 FRONT. 모르면(정지·속도 작음) 싣지 않음 | 워치에는 나침반 기준을 쓸 수 없다(실내, 손목 방향 불명). 속도 임계값은 -338 에서 정함 |
| 한 단어 | 1~8자, 앞뒤 공백 없이 | -390 화면 시험(갤럭시워치6 원형 화면 한 줄) |
| 확인 기한 | `ack_within_ms` 길이(1 ms ~ 10분), 기본 10초. 절대 시각 불가 | 워치 시계 차이에 흔들리지 않게. 10초는 '진입 → 진동 p95 1.5초' 와 재연결 백오프(최대 30초)를 고려한 초깃값, -338 에서 구성값으로 |
| ACK 종류 | VIBRATED(자동, 격상 판단 기준) + CONFIRMED(사람 조작, 선택) | FR-46 '진동 후 ACK' 는 기기가 받았다는 증거라 자동이어야 한다. -390 의 확인 버튼은 CONFIRMED 로 남긴다 |
| 하트비트 주기·끊김 | EC2 가 `interval_ms`(1~60초, 기본 10초) 지정, 3회 누락 = 끊김 | 노트북(1초)보다 길게: 배터리 8시간 목표(C.6). 3회 규칙은 노트북 하트비트와 같음 |
| 전송 경로 | `watch.transport` WIFI·PHONE_BLUETOOTH·CELLULAR·UNKNOWN | -337 완료 조건 '휴대폰 경유와 Wi-Fi 직접 연결의 지연 차이 기록' |
| 걸음 창 | 4초 창, 2초마다(절반 겹침), 원본 50 Hz, 대역 시계열 10 Hz, 걸음 시점 ≤ 64, 시계열 ≤ 512 | C.6 '몇 초 창의 점수표'. 0.5~3 Hz 신호를 10 Hz 로 표본화하면 나이퀴스트(5 Hz) 안. 4초 × 10 Hz = 40 값, 메시지 1 KB 안팎. 창 길이·주기는 -339 측정 뒤 바꿀 수 있다(스키마는 상한만) |
| 시계 기준 | 모든 시각은 워치 시계(`clock_basis` WATCH 고정), `clock_offset_ms`·`peak_freq_hz` 는 모르면 null 로라도 싣는다(필수 nullable) | 이슈 '시계 기준 명시'. 없음과 모름을 구분해 EC2 가 받은 시각으로 대략 맞출지 정확히 환산할지 알 수 있게. 생성 Java 타입은 선택 필드의 null 과 없음을 구분하지 못하므로 구분이 필요한 값은 필수로 둔다 |
| 가속도 원본 | 싣지 않는다. `band_magnitude` 는 닫혀 원본 키를 거부하고, payload 최상위는 봉투 규칙상 열려 있어 거기 싣지 않는 것은 워치 앱 시험(-339)에서 본다 | 11장 데이터 최소화. 대조에는 0.5~3 Hz 크기 신호와 걸음 시점이면 된다(C.6) |

## 시험

- `uv run contracts/scripts/validate_examples.py`: 예시 9건 통과, 반례 16건 거부(아래 표). 기존 예시·반례 그대로.
- `ai/`: `uv run python ../contracts/scripts/generate_python_models.py` 로 모델 다시 만듦, `uv run pytest tests/contracts` 409 통과
  (새 모델 `WatchAlert`·`WatchHeartbeat`·`WatchAck`·`GaitFeatures`, 예시 왕복, 조건 밖 반례 거부).
- `server/`: `./gradlew test --tests '*ContractExamplesRoundTripTest'` 117 통과 — 생성 Java 클래스로 새 예시 왕복(서버 코드 변경 없음).

| 반례 | 막는 규칙 |
|---|---|
| `watch_alert.info_severity` | INFO 불가 |
| `watch_alert.word_too_long`·`word_padded` | 한 단어 8자, 앞뒤 공백 없이 |
| `watch_alert.ack_within_zero`·`absolute_deadline` | 기한 1 ms~10분 길이 |
| `watch_alert.unknown_direction` | 작업자 기준 4방향 |
| `watch_ack.unknown_kind`·`missing_alert_msg_id` | 종류 2개, 알림 ID 필수 |
| `watch_heartbeat.watch_block_from_ec2`·`missing_watch_block`·`interval_from_watch`·`ec2_without_echo` | 방향별 블록 규칙, EC2 답의 echo 필수 |
| `gait_features.unknown_clock_basis`·`missing_clock_offset`·`duplicate_steps`·`raw_samples` | 시계 기준·차이 필수·걸음 중복·원본 불가 |

스키마로 못 막는 규칙(창 끝 > 시작, 시점이 창 안·오름차순, 시계열 길이, `heartbeat_seq` 단조 증가, `alert_msg_id` 가 받은 알림의 ID)은
워치 앱 시험(-337·-339)과 EC2 수신 시험(-336·-338)에서 본다.

## 남은 일

- Lead 확인(새 구간 계약, MR). 명세 7.3 채널 표에 워치 개인 채널(CAUTION 포함) 예외를 더했다(수정 45).
- `watch/` 앱이 계약을 읽게 되면(-337) Jenkinsfile 의 WATCH 파트에 `contracts/` 를 더하고, `AlertFields`(`vibration` → `vibration_pattern`,
  `sent_at_ms` → 봉투 `sent_at`)를 계약 이름으로 맞춘다.
- `watch_alert` 표의 `alert_msg_id`·`ack_within_ms`·`confirmed_at` 열과 CK 변경은 테이블을 만드는 이슈(-336·-338)의 마이그레이션에서.
- 워치 ACK 로 현장 경보를 멈출지, 격상 뒤 늦은 ACK 의 처리는 -338.
