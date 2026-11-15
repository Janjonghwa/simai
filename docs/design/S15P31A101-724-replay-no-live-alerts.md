# S15P31A101-724 녹화 재생(시험 입력) 중에는 실제 알림을 내보내지 않는다

관련 명세·규칙: AGENTS 규칙 5(LIVE 만 실제 알림, PLANNED·WHAT_IF·REPLAY 는 알림 채널 금지), 3.2(이중 감시·하트비트), 3.10(현장 경보),
6.5, 7.3(알림 채널), 부록 C.6(워치). 계약 `messages/laptop_heartbeat` 1.3.0(묶음 v7.58.0). DB 마이그레이션 없음.
앞 문서: `S15P31A101-721-replay-test-path.md` 3절(재생 시험 경로, 이 티켓이 그 제안 1·3 의 알림 부분을 먼저 구현한다).

## 문제(17차 재생 시험, -716)

노트북 영상 중계(`gateway/relay`)가 녹화 파일(`kind = "file"`)을 재생해도 Tier 0·EC2 는 재생인지 모른다. Tier 0 는 재생 영상을 LIVE 로
판정해 R2 CRITICAL 현장 경보 27건 등이 실제 알림 경로(현장 경보 단말 소리, 워치 진동)로 나갔다. 규칙 5 위반이다.

## 결정

- **입력 종류의 정본은 중계가 실제로 연 입력이다.** 사람이 켜는 토글(운영에서 켜 둔 채 잊으면 실제 경보가 꺼진다)은 쓰지 않는다.
- **알림 경로에서 막는다.** 이벤트 `source` 를 REPLAY 로 바꾸지 않는다(I1 LIVE 계약 의미 변경, Lead 결정 대상). 이벤트는 그대로 LIVE 로
  기록·화면 표시된다(아래 「남는 일」 1).
- 시험 입력 = 카메라 하나라도 `FILE` 또는 `SYNTHETIC`. `source_kind` 가 없으면(이전 중계·단말) RTSP 로 본다(실제 경보를 끄는 쪽이므로
  확실한 값만 센다).

## 흐름과 막는 자리

```
relay (CameraLinkStats.source_kind = spec.source.kind)
  └─ 127.0.0.1 UDP 상태 보고(simai-relay-status/1, cameras[].source_kind)  ─▶ site_alert 단말
        ├─ ① 단말 코어: 시험 입력이면 소리 끔 + 「시험 입력 — 실제 경보 꺼짐」 띠
        └─ laptop_heartbeat 1.3.0 cameras[].source_kind ─▶ Tier 0(내용 안 읽음, 변화 없음)
                                                        └▶ EC2 HealthState ─▶ ② WatchAlertService: 워치 진동 안 보냄
```

| 경로 | 막는 곳 | 이유 |
|---|---|---|
| 현장 경보 소리(Tier 0 `local_alert` → 단말) | ① `gateway/site_alert` 코어 `_target()` | 단말이 중계 보고를 직접 받는다(같은 노트북, 지연 없음). Tier 0 를 고치면 하트비트 왕복·I0 변경이 필요하다. 소리를 내는 마지막 자리라 가장 확실하다 |
| 워치 진동(EC2 → 워치) | ② `WatchAlertService.alert`·`resync` | 워치 알림은 EC2 만 보낸다. 하트비트(1초)가 EC2 에도 오므로 같은 판단을 바로 할 수 있다 |
| 반장 PWA Web Push | 막지 않음(해당 경로 없음) | `NotificationQueue` 는 아직 운영 코드에서 부르는 곳이 없다(발송 미구현). 붙일 때 같은 판단을 넣는다(남는 일 3) |
| 관리자 화면 표시·소리 | 막지 않음(넘김) | 남는 일 2 |

### ① 현장 경보 단말(`gateway/site_alert`)

- `relay_status._camera_entry` 가 `source_kind` 를 그대로 넘기고(모르는 값이면 보고 통째로 버림, 다른 필드와 같다), `RelayReport.test_input`
  이 FILE·SYNTHETIC 하나라도 있으면 true.
- `SiteAlertApp.on_relay_report` 가 보고마다 `core.set_test_input()` 을 부른다. 보고가 끊겨도 마지막 값을 유지한다(중계가 멈췄으면 새 영상이
  없고, 실제 카메라로 다시 띄우면 첫 보고가 1초 안에 바꾼다).
- 코어: 시험 입력 중 활성인 경보를 「소리 끔 목록」에 넣고 해소될 때까지 울리지 않는다. RTSP 로 돌아와도 그 경보는 조용하다(재생 영상의
  경보가 실제 관제 중에 울리지 않게). 해소 뒤 재개는 새로 판단한다. 표시·현장 확인·재동기화는 그대로, 하트비트 `alarm.sounding_event_ids`
  는 소리 끈 경보를 빼고 보낸다. 시작 시 시험 경보(소리 확인)는 그대로 울린다.
- 화면: 「시험 입력 — 실제 경보 꺼짐 (…)」 띠(보라 바탕), 경보 줄의 상태 문구 「확인 전 · 시험 입력이라 소리 없음」.

### ② EC2 워치(`server`)

- `HealthState.testInputActive()`: 노트북마다 마지막 하트비트가 `TEST_INPUT_HOLD_MS`(60초) 안이고 `cameras[]` 에 FILE·SYNTHETIC 이 있으면 true.
- `WatchAlertService`: 시험 입력 중 받은 이벤트는 보내지 않고 그 ID 를 메모리(최대 1만 개, 오래된 것부터 잊음)에 기억해 실제 카메라로
  돌아온 뒤 갱신·워치 재연결 재동기화로 다시 와도 보내지 않는다. 수동 알림은 막지 않는다(사람이 직접 보냄).

## 명세에 없어 정한 수치와 근거

| 값 | 근거 |
|---|---|
| EC2 유지 60초(`TEST_INPUT_HOLD_MS`) | 하트비트가 아예 끊긴 직후(단말을 끈 직후) Tier 0 가 남은 재생 프레임으로 낸 이벤트를 막는다. RTSP 로 바꾸면 다음 하트비트(1초)가 바로 풀므로 LIVE 알림을 늦추지 않는다. 중계만 멈추면 단말은 3초 뒤 `cameras[]` 를 빼고 보내 EC2 는 그때부터 RTSP 로 본다(그 뒤엔 새 영상이 없다) |
| 기억 1만 개 | 재생 한 번에 이벤트 수십 건(17차 27건). 메모리 몇 백 KB |
| SYNTHETIC 도 시험 입력 | 실제 카메라가 아니다. 과제 문구는 FILE 이지만 같은 이유로 넣었다 |

## 버린 대안

- **Tier 0 가 하트비트로 `local_alert` 자체를 안 보냄**: 단말 하나에서 막는 것과 효과가 같고 Tier 0·I0 경로를 고쳐야 한다. 단말은 중계 보고를
  1초보다 빨리 받으며 소리를 내는 마지막 자리다.
- **이벤트 `source = REPLAY`**: I1 LIVE 계약 의미 변경이다(Lead 결정, 남는 일 1).
- **사람이 켜는 「시험 모드」 토글**: 켠 채 잊으면 실제 경보가 꺼진다.

## 시험

- `gateway/site_alert/tests/test_test_input.py`: 보고 형식(통과·모르는 값 버림), 시험 입력 판정, 하트비트 1.3.0 계약 통과, FILE 이면 표시만·소리
  없음·띠, 도중에 FILE 로 바뀌면 소리 멈춤, RTSP 로 돌아와도 시험 입력 경보는 해소까지 조용·새 경보는 울림, **RTSP 는 지금처럼 울림**, 시작 시험
  경보는 울림.
- `gateway/relay/tests/test_relay.py`·`test_status_report.py`: `source_kind` 싣기(모르는 값은 안 실음)와 단말까지 전달.
- `server` `HealthStateTestInputTest`(판정·노트북별 마지막 값·유지 시간), `WatchAlertIntegrationTest`(FILE 하트비트 중 이벤트는 워치로 안 감,
  RTSP 로 돌아와 갱신·재동기화해도 안 감, 새 RTSP 이벤트는 감).
- 계약: `validate_examples.py`, Python 생성 모델 `--check`, `ai/tests/contracts`.

## 잔여 위험

- 셋(중계·단말·EC2)이 모두 새 판이어야 막힌다. 어느 하나라도 옛 판이면 지금처럼 알림이 나간다(옛 판과 섞여도 실제 RTSP 동작은 그대로).
- EC2 의 기억은 메모리라 EC2 를 재생 직후 다시 띄우면 그 재생 이벤트가 워치 재연결 재동기화(최근 10분 갱신분)로 나갈 수 있다.
- 단말의 소리 끔 목록도 메모리다. 재생 중 단말을 다시 띄우고 RTSP 로 바꾸면, Tier 0 재동기화 목록에 남은 재생 경보가 다시 울릴 수 있다.
- 재생과 실제 카메라를 섞어 띄우면(한 대만 FILE) 그동안 실제 카메라의 위험도 소리·진동이 꺼진다(의도: 시험 입력이 섞인 판정은 믿지 않는다).

## 남는 일(Lead 결정·후속 티켓)

1. **이벤트 출처 표시**(규칙 7, 시험 지표가 제품 성능으로 쓰이지 않게): RiskEvent `source` 는 LIVE 로 두고 별도 필드(예:
   `input_provenance = REPLAY_FILE`)를 `risk_event_transition`(I1)·`risk_event`(I2) 마이너로 더하는 안. I1 LIVE 계약이라 Lead 승인 필요.
2. **관제 화면(관리자)**: 시험 입력 중임을 `viewer_health`(I2)에 싣고(예: `laptop.test_input`) 화면에 띠를 띄우고 `alarmSound.ts` 경보음을 끄기.
   I2 계약·front 변경이 커서 넘긴다. 지금은 관리자 화면에 재생 이벤트가 LIVE 처럼 보이고 경보음도 난다.
3. **반장 PWA Web Push**: 발송(`NotificationQueue` 사용처)을 붙일 때 `HealthState.testInputActive()` 로 같은 판단을 넣는다.
4. -721 3절 제안 2·4(세션 `input_kind = REPLAY_FILE`, 걷기 확인 건너뛰기, 재사용 후보 제외)는 이 계약 필드를 그대로 쓴다.
